# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""One bound model invocation, with validation and guarded publication."""
from __future__ import annotations
import contextlib
import json
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from src.contracts.contracts import ContractError, strict_json, result_diagnostics, CONTRACT_ID, ARTIFACT_FORMAT
from src.runtime.reporting import diagnostic
from src.runtime.execution import Budget
from src.runtime.tool_observer import ToolObserver
from src.backends import codex
from src.backends.codex_capabilities import (MCP_AUXILIARY_TOOLS, MCP_NO_SOURCE_TOOLS,
                                             probe_command, verify_trace, verify_mcp_text_delivery)
from src.reports.final_report import recoverable_material
from src.analysis.ledger import prepare_result
from src.analysis.evidence import source_catalog, resolve_evidence, read_confined, SourceChanged
from src.analysis.study_phases import freeze_collection
from src.model.model_context import (CONTEXT_FORMAT, build_model_prompt, input_measurements,
                                     mcp_source_index, on_demand_source_index, serialize_model_input)
from src.analysis.coverage_plan import inventory_summary, build_coverage_plan
from src.runtime.errors import AuditError, UnsafeRepository
from src.runtime.artifacts import now, digest, private_directory, neutral_temporary_base, append_observation
from src.runtime.process import cli_env
from src.backends.preflight import CLI_ADAPTERS, command, profile_key
from src.model.attempts import AttemptStore
from src.model.validation import ResultValidator
from src.runtime import artifacts, process as process_runtime


def mcp_profile(session, agent):
    profile = getattr(session, 'mcp_capabilities', {}).get(profile_key(agent))
    if profile is None:
        raise AuditError('The configured Codex agent has no verified MCP capability profile.',
                         code='MCP_COMPATIBILITY', failure_layer='compatibility',
                         details={'native_fallback': False})
    return profile

class ModelInvoker:
    def __init__(self, session, publisher):
        self.session, self.publisher = session, publisher
        self.attempts = AttemptStore(session)
        self.validator = ResultValidator(session, self.attempts)

    def invoke(self, stage: str, context: dict, destination: Path) -> tuple[dict | None, dict]:
        destination = destination.resolve()
        context = dict(context)
        context['stage'] = stage
        context['result_policy'] = 'compromise' if self.session.compromise else 'strict'
        initial_study = stage == 'study' and not context.get('previous_revision') and context.get('prompt_variant') != 'revise'
        if initial_study:
            context['phase'] = 'collect'
        self.publisher.assert_coverage_file(context)
        if stage != 'compare':
            context['sources'] = source_catalog(context)
        budget = Budget(self.session.execution['stage_timeout_seconds'], self.session.execution['idle_timeout_seconds'])
        evidence_pins = None
        evidence_metadata = None
        if stage != 'compare':
            # Every stage uses the initial inventory and verifies its metadata.
            if self.session.repo:
                self.session.assert_source()
            inventory, evidence_metadata = self.session.source_inventory()
            if context.get('_inventory') and inventory['source_fingerprint'] != context['_inventory']['source_fingerprint']:
                raise UnsafeRepository('Source changed from the inventory used for the coverage plan.', failure_layer='integrity')
            if stage == 'study' and not context.get('coverage_plan'):
                context['inventory_summary'] = inventory_summary(inventory)
            if self.session.folder and inventory['source_fingerprint'] != context['source_fingerprint']:
                raise UnsafeRepository('Source folder changed from the pinned snapshot before invocation.')
            if self.session.repo:
                self.session.assert_source()
            evidence_pins = {e['path']: e['sha256'] for e in inventory['entries'] if e['type'] == 'file'}
            context['_inventory'] = inventory
            if stage == 'study' and context.get('coverage_plan') is None:
                context['coverage_plan'] = build_coverage_plan(inventory, context)
        try:
            prepared_context, preparation = self.session.source_context.prepare(
                stage, context, evidence_pins, expected_metadata=evidence_metadata)
        except SourceChanged as exc:
            raise UnsafeRepository('Source changed during context preparation.', code='SOURCE_CHANGED',
                                   failure_kind='SOURCE_CHANGED', failure_layer='integrity') from exc
        context.pop('source_context', None)
        if prepared_context is not None:
            context['source_context'] = prepared_context
        binding = self.session.bindings.bind(stage, context, self.session.mode)
        binding_hashes = {}
        attempt_hashes = {}
        candidate = None
        candidate_attempt = None
        try:
            collection_meta = None
            if initial_study:
                package, collection_meta = self._invoke_once(stage, context, destination, budget=budget,
                    evidence_pins=evidence_pins, evidence_metadata=evidence_metadata, binding=binding,
                    binding_hashes=binding_hashes, attempt_hashes=attempt_hashes, preparation=preparation)
                context = dict(context, phase='write', evidence_package=package)
                context.pop('source_context', None)
                binding = self.session.bindings.bind(stage, context, self.session.mode)
                preparation = None
            return self._invoke_once(stage, context, destination, budget=budget,
                                     evidence_pins=evidence_pins, evidence_metadata=evidence_metadata, binding=binding,
                                     binding_hashes=binding_hashes, attempt_hashes=attempt_hashes,
                                     preparation=preparation, collection_meta=collection_meta)
        except ContractError as exc:
            self.attempts.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
            if not (destination / 'invocation.json').is_file():
                raise
            meta = strict_json((destination / 'invocation.json').read_text())
            if meta.get('cleanup_errors'):
                self.session.critical_failure = True
                raise
            attempt = Path(meta['artifact_directory'])
            # Retain only the verified original response under the compromise policy.
            if (self.session.compromise
                    and context.get('phase') != 'collect'
                    and meta.get('source_integrity_verified')
                    and meta.get('validation_failed') and meta.get('backend_result_valid')
                    and meta.get('model_identity_verified')
                    and exc.failure_kind in ('SCHEMA_ERROR', 'SEMANTIC_ERROR')):
                invalid = self.attempts.read_attempt_value(attempt, 'expanded.json', attempt_hashes)
                validation = self.attempts.read_attempt_value(attempt, 'validation.json', attempt_hashes)
                checked = (self.attempts.read_attempt_value(attempt, 'normalized.json', attempt_hashes)
                           if validation.get('validated_object') == 'normalized.json' else invalid)
                candidate = recoverable_material(stage, invalid, context, self.session.mode,
                    result_diagnostics(stage, checked, context, self.session.mode))
                if candidate is not None:
                    candidate['contract_failure'] = asdict(diagnostic(exc))
                    if meta.get('normalization_provenance'):
                        candidate['normalization_provenance'] = meta['normalization_provenance']
                candidate_attempt = str(attempt)

            if candidate is None:
                raise
            # Recheck the source before retaining contract-invalid material.
            if self.session.folder:
                self.session.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.session.assert_source()
            candidate['artifact_directory'] = candidate_attempt
            artifacts.save_json(destination.parent / (stage + '.material.json'), candidate)
            material_bytes = (json.dumps(candidate, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            meta = meta | {'status': 'PARTIAL', 'usable_material': candidate,
                           'local_validation': False, 'recovery_source_attempt': candidate_attempt,
                           'material_retained': True, 'material_hashes': {
                               stage + '.material.json': {'sha256': digest(material_bytes), 'bytes': len(material_bytes)}}}
            # Per-attempt files remain the original failed validation record.
            artifacts.save_json(destination / 'invocation.json', meta)
            return None, meta

    def _invoke_once(self, stage, context, destination, *, budget,
                     evidence_pins=None, evidence_metadata=None, binding, binding_hashes, attempt_hashes,
                     preparation=None, collection_meta=None):
        attempt_started = self.session.reporter.clock()
        self.attempts.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
        agent = self.session.cfg['_agents'][stage]
        phase = context.get('phase') if stage == 'study' else None
        variant = context.get('prompt_variant', stage)
        template_path = Path(__file__).resolve().parents[2] / 'prompts' / 'collect.md' if phase == 'collect' else Path(self.session.cfg['_prompt_paths'][variant])
        template = template_path.read_text()
        reports_only = stage == 'compare' or phase == 'write'
        mcp_mode = self.session.cfg.get('source_access', {}).get('mode', 'native') == 'mcp'
        invocation_id = str(uuid.uuid4())
        private_directory(destination)
        # Exclusive creation preserves every orchestrator-managed attempt.
        number = 1
        while True:
            attempt = destination / f'attempt-{number:03d}'
            try:
                attempt.mkdir(mode=0o700)
                break
            except FileExistsError:
                number += 1
        # Assemble only this stage's permitted inputs.
        projected = binding.project(context)
        source_maps_directory, source_maps = None, {}
        if ('source_context' in projected and
                self.session.source_context.settings['mode'] == 'on-demand'):
            source_maps_directory = attempt / 'source-maps'
            private_directory(source_maps_directory)
            projected['source_context'], source_maps = on_demand_source_index(
                projected['source_context'], source_maps_directory)
        meta = {'attempt_hashes': attempt_hashes}
        for name, value in source_maps.items():
            self.attempts.save_attempt_value(attempt, 'source-maps/' + name, value, meta)
        stored_bytes = sum(attempt_hashes[str((attempt / 'source-maps' / name)
                           .relative_to(destination.parent))]['bytes'] for name in source_maps)
        mcp_manifest, mcp_manifest_sha256 = None, None
        mcp_catalog = None
        profile = None
        if mcp_mode:
            profile = mcp_profile(self.session, agent)
            mcp_catalog = attempt / 'model-catalog.json'
            self.attempts.save_attempt_value(attempt, 'model-catalog.json', profile['catalog'], meta)
        if mcp_mode and not reports_only:
            if source_maps_directory is None:
                raise AuditError('MCP source access requires the prepared on-demand source index.',
                                 code='MCP_PREPARATION_FAILED', failure_layer='integrity')
            from src.mcp.service import prepare_manifest
            map_files = {name: dict(path=str(source_maps_directory / name),
                **attempt_hashes[str((source_maps_directory / name).relative_to(destination.parent))])
                for name in source_maps}
            manifest = prepare_manifest(context, context['source_context'], projected['source_context'],
                map_files, attempt_id=invocation_id, expected_metadata=evidence_metadata)
            self.attempts.save_attempt_value(attempt, 'mcp/manifest.json', manifest, meta)
            mcp_manifest = attempt / 'mcp' / 'manifest.json'
            mcp_manifest_sha256 = attempt_hashes[str(mcp_manifest.relative_to(destination.parent))]['sha256']
            projected['source_context'] = mcp_source_index(projected['source_context'])
        context_json = serialize_model_input(projected)
        schema = binding.schema
        schema_json = serialize_model_input(schema)
        prompt = build_model_prompt(template, context_json, schema_json,
                                    source_index='source_context' in projected,
                                    source_access='mcp' if mcp_mode else 'native')
        measurements = input_measurements(template, context_json, schema_json, prompt,
            source_context=context.get('source_context'),
            transmitted_source_context=projected.get('source_context'), stored_map_utf8_bytes=stored_bytes)
        if preparation is not None and 'source_index' in measurements:
            index_sizes = measurements['source_index']
            preparation = dict(preparation, original_bytes=index_sizes['original_utf8_bytes'],
                transmitted_bytes=index_sizes['transmitted_utf8_bytes'],
                saved_bytes=index_sizes['saved_utf8_bytes'], reduction_percent=index_sizes['reduction_percent'])
            preparation['delivery'] = 'mcp' if mcp_mode else self.session.source_context.settings['mode']
            preparation['packed_full_index_utf8_bytes'] = index_sizes['packed_full_index_utf8_bytes']
            preparation['stored_map_utf8_bytes'] = stored_bytes
            if source_maps_directory is not None:
                preparation['files'] = [dict(file, symbols_transmitted=0) for file in preparation['files']]
        payload = prompt.encode('utf-8')
        meta.update({'stage': stage, 'invocation_id': invocation_id, 'started_at': now(),
            'backend': agent['backend'], 'executable': agent['executable'],
            'model_requested': agent.get('model'), 'reasoning_effort_requested': agent.get('reasoning_effort'),
            'cli_version': self.session.versions.get(agent['backend'] + ':' + agent['executable']),
            'prompt_sha256': digest(payload), 'template_sha256': digest(template.encode()),
            'schema_sha256': digest(json.dumps(schema, sort_keys=True).encode()),
            'input_bytes': len(payload), 'status': 'RUNNING'})
        meta['input_measurements'] = measurements
        meta.update(attempt=attempt.name, artifact_directory=str(attempt), api_version=None,
                    model_actual=None, request_id=None, session_id=None, message_id=None,
                    finish_reason=None, output_bytes=None, execution=self.session.execution)
        meta['binding_hashes'] = binding_hashes
        meta['attempt_hashes'] = attempt_hashes
        meta.update(contract_id=CONTRACT_ID, artifact_format=ARTIFACT_FORMAT,
                    context_format=CONTEXT_FORMAT,
                    revision_id=context.get('revision_id'), prompt_variant=context.get('prompt_variant', stage),
                    model_actual_source='unknown: backend has not reported model identity',
                    review_quality='NOT_MEASURED', publication_complete=False)
        meta.update(source_tools_enabled=not reports_only)
        if mcp_mode:
            meta['source_access'] = {'mode': 'mcp', 'enabled': not reports_only}
            if mcp_manifest is not None:
                meta['source_access'].update(manifest=str(mcp_manifest), manifest_sha256=mcp_manifest_sha256)
        if phase:
            meta['phase'] = phase
            self.session.reporter.emit('study_phase_started', **self.session.stage_context(stage, context))
        if collection_meta:
            meta['phase_invocations'] = [{key: collection_meta[key] for key in
                ('phase', 'invocation_id', 'artifact_directory', 'status', 'duration_seconds')}]
            for key in ('citation_corrections', 'citation_correction_provenance'):
                if key in collection_meta:
                    meta[key] = collection_meta[key]
        if self.session.repo:
            meta['submodules'] = context.get('submodules', [])
        artifacts.save_json(destination / 'invocation.json', meta)
        # Saved by the parent only, for reproducibility; includes only this stage's permitted input.
        artifacts.atomic(attempt / 'input.prompt.txt', payload)
        artifacts.save_json(attempt / 'schema.json', schema)
        artifacts.save_json(attempt / 'validation.json', {'valid': False, 'status': 'not_run'})
        if preparation is not None:
            artifacts.save_json(attempt / 'source-context.manifest.json', preparation)
            meta['source_context_manifest'] = str(attempt / 'source-context.manifest.json')
        observer_context = context
        if mcp_manifest is not None:
            observer_context = dict(context, _mcp_source_access={
                'server': 'explain_this_pls', 'manifest_sha256': mcp_manifest_sha256,
                'attempt_id': invocation_id, 'receipts_path': str(mcp_manifest.parent / 'served.jsonl')})
        observer = ToolObserver(agent['backend'], enabled=self.session.execution['read_loop_guard'],
                                clock=self.session.reporter.clock, context=observer_context, attempt_id=meta['invocation_id'],
                                event_sink=lambda event: append_observation(attempt / 'tool-events.jsonl', event))
        observer.seed_prompt(context.get('source_context', {}).get('fragments', []))
        try:
            self.attempts.save_binding(binding, None, None, attempt, meta)
            self.attempts.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
            budget.check()
            self.publisher.assert_coverage_file(context)
            if self.session.folder:
                self.session.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.session.assert_source()
            with tempfile.TemporaryDirectory(prefix='archaudit-invocation-', dir=neutral_temporary_base(self.session.source_path)) as raw:
                state = Path(raw).resolve()
                cwd = state if reports_only or mcp_mode else self.session.source_path
                env = cli_env(cwd)
                if self.session.repo:
                    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
                        env.pop(key, None)
                schema_path = state / 'output.schema.json'
                artifacts.save_json(schema_path, schema)
                try:
                    launch_agent = dict(agent, model=profile['proof']['model']) if mcp_mode else agent
                    cmd = self.command(stage, state, launch_agent, schema_path, env, reports_only=reports_only,
                                       schema=schema, writer_only=phase == 'write',
                                       **({'mcp_catalog': mcp_catalog} if mcp_mode else {}),
                                       **({'mcp_manifest': mcp_manifest, 'mcp_manifest_sha256': mcp_manifest_sha256}
                                          if mcp_manifest is not None else {}))
                    if mcp_mode:
                        expected_tools = set(MCP_NO_SOURCE_TOOLS)
                        if mcp_manifest is not None:
                            expected_tools.update(MCP_AUXILIARY_TOOLS)
                            expected_tools.update('mcp__' + codex.MCP_SERVER_NAME + '__' + tool
                                                  for tool in ('search_symbols', 'read_source'))
                        proof = probe_command(cmd, state, env, expected_tools=expected_tools,
                                              process_fn=process_runtime.process,
                                              reporter=self.session.reporter, budget=budget)
                        self.attempts.save_attempt_value(attempt, 'mcp-capabilities.json', proof, meta)
                        meta['source_access'].update(proof)
                        self.attempts.assert_binding_files(binding, context, destination.parent,
                                                           binding_hashes | attempt_hashes)
                        trace_root = attempt / 'codex-trace'
                        private_directory(trace_root)
                        env['CODEX_ROLLOUT_TRACE_ROOT'] = str(trace_root)
                    r = None
                    meta['prompt_sent'] = True
                    try:
                        r = process_runtime.process(cmd, cwd, env, payload, reporter=self.session.reporter,
                                    context=self.session.stage_context(stage, context), log_dir=attempt,
                                    clock=self.session.reporter.clock, progress_interval=self.session.reporter.progress_interval,
                                    budget=budget, observer=observer)
                    finally:
                        # The private pipe log survives timeout/interruption even without a process result.
                        with contextlib.suppress(OSError):
                            output = r['stdout'] if r is not None else (attempt / 'stdout.log').read_bytes()
                            meta['metrics'] = CLI_ADAPTERS[agent['backend']].collect_metrics(
                                output.decode('utf-8', errors='replace'), agent.get('model'))
                    meta.update(returncode=r['returncode'], output_bytes=len(r['stdout']))
                    if r['returncode']:
                        if mcp_mode:
                            mcp_error = codex.mcp_failure(r['stdout'], r['stderr'])
                            if mcp_error is not None:
                                raise mcp_error
                        raise AuditError(f'{agent["backend"]} exited with {r["returncode"]}; inspect private attempt logs.',
                            code='CLI_FAILED', failure_kind='BACKEND_ERROR', failure_layer='backend')
                    if mcp_mode:
                        actual = verify_trace(trace_root, expected_tools, expected_model=profile['proof']['model'])
                        if actual['toolset_sha256'] != proof['toolset_sha256']:
                            raise AuditError('Codex changed its verified MCP tool schemas during the model run.',
                                             code='MCP_COMPATIBILITY', failure_layer='compatibility',
                                             details={'native_fallback': False})
                        delivery = verify_mcp_text_delivery(trace_root, r['stdout'])
                        meta['source_access'].update(delivery)
                        self.attempts.save_attempt_value(attempt, 'mcp-capabilities-observed.json', actual, meta)
                        for path in sorted(trace_root.rglob('*')):
                            if path.is_file() or path.is_symlink():
                                blob = read_confined(self.session.run_dir, str(path.relative_to(self.session.run_dir)))
                                attempt_hashes[str(path.relative_to(destination.parent))] = {
                                    'sha256': digest(blob), 'bytes': len(blob)}
                        meta['source_access']['inference_requests'] = actual['inference_requests']
                        meta['model_actual'] = actual['model']
                        meta['model_actual_source'] = 'verified Codex inference request trace'
                    if phase == 'write' and observer.snapshot()['calls']:
                        raise AuditError('The report author used a tool outside its evidence-only scope.',
                                         code='WRITER_TOOL_CALL', failure_layer='execution')
                    output = codex.read_response(schema_path)
                    data, provider_meta = CLI_ADAPTERS[agent['backend']].parse_output(output)
                    meta['backend_result_valid'] = True
                    meta['provider_metadata'] = provider_meta
                    model_usage = provider_meta.get('modelUsage')
                    if type(model_usage) is dict and model_usage:
                        meta['models_reported'] = list(model_usage)
                        if len(model_usage) == 1:
                            meta['model_actual'] = next(iter(model_usage))
                        meta['model_actual_source'] = 'backend modelUsage; may include multiple models'
                    artifacts.save_json(attempt / 'extracted.json', data)
                    self.attempts.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
                    data = self.validator.validate_attempt(stage, data, context, attempt, meta, binding,
                        expected_files=evidence_pins, expected_metadata=evidence_metadata)
                finally:
                    observer.finish()
                    meta['tool_observation'] = observer.snapshot()
                    if mcp_manifest is not None:
                        receipts = mcp_manifest.parent / 'served.jsonl'
                        served = []
                        if receipts.exists() or receipts.is_symlink():
                            blob = read_confined(self.session.run_dir, str(receipts.relative_to(self.session.run_dir)))
                            served = [strict_json(line) for line in blob.decode('utf-8').splitlines() if line.strip()]
                            if any(type(row) is not dict or row.get('attempt_id') != invocation_id or row.get('state') != 'SERVED'
                                   or type(row.get('request_id')) is not str or type(row.get('result_sha256')) is not str
                                   for row in served):
                                raise AuditError('MCP receipts do not belong to this attempt.',
                                                 code='MCP_RECEIPT_CHANGED', failure_layer='integrity')
                            attempt_hashes[str(receipts.relative_to(destination.parent))] = {
                                'sha256': digest(blob), 'bytes': len(blob)}
                        if not observer.mcp_receipts <= {(row['request_id'], row['result_sha256']) for row in served}:
                            raise AuditError('Observed MCP receipts disappeared or changed.',
                                             code='MCP_RECEIPT_CHANGED', failure_layer='integrity')
                        meta['source_access']['served_calls'] = len(served)
                        if (any(row.get('result_summary', {}).get('error', {}).get('code') == 'SOURCE_CHANGED'
                                for row in served) or
                                meta['tool_observation'].get('mcp', {}).get('errors', {}).get('SOURCE_CHANGED')):
                            raise UnsafeRepository('MCP detected a changed pinned source or private artifact.',
                                code='SOURCE_CHANGED', failure_kind='SOURCE_CHANGED', failure_layer='integrity')
                    self.attempts.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
                    self.publisher.assert_coverage_file(context)
                    if self.session.folder:
                        self.session.folder.assert_snapshot(context['source_fingerprint'])
                    else:
                        self.session.assert_source()
                    meta['source_integrity_verified'] = True
                budget.check()
            # Publish after the CLI exits and temporary invocation files are removed.
            try:
                if phase == 'collect':
                    resolutions = resolve_evidence('study', data['evidence'], context, evidence_pins,
                                                   expected_metadata=evidence_metadata)
                    data = freeze_collection(data, resolutions, citation_corrections=meta.get('citation_corrections'),
                                             normalization_provenance=meta['normalization_provenance'])
                else:
                    data = prepare_result(stage, data, context, evidence_pins, expected_metadata=evidence_metadata,
                                          citation_corrections=meta.get('citation_corrections'))
                if phase == 'write':
                    data['program_checks']['completion_self_assessment'] = meta['author_completion_self_assessment']
                if stage == 'study' and data.get('advisory'):
                    meta['advisory_diagnostics'] = data['advisory']['diagnostics']
                    self.attempts.save_attempt_value(attempt, 'advisory.json', data['advisory'], meta)
                for warning in data.get('program_checks', {}).get('citation_warnings', []):
                    self.session.reporter.emit('citation_warning', **(warning | self.session.stage_context(stage, context)),
                                       evidence_branch=warning.get('branch'))
                if stage in ('study', 'review') and phase != 'collect':
                    data['normalization_provenance'] = meta['normalization_provenance']
            except SourceChanged as exc:
                meta['source_integrity_verified'] = False
                meta['source_check_status'] = 'CHANGED_DURING_EVIDENCE_RESOLUTION'
                raise UnsafeRepository('Source changed during evidence resolution.', code='SOURCE_CHANGED',
                                       failure_kind='SOURCE_CHANGED', failure_layer='integrity') from exc
            if self.session.folder:
                self.session.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.session.assert_source()
            meta['source_check_status'] = 'MATCHED_AT_BOUNDARIES'
            budget.check()
            if stage == 'review':
                self.publisher.assert_review_files(context, destination.parent)
            self.publisher.assert_coverage_file(context)
            self.attempts.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
            if phase == 'collect':
                content = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
                package_path = destination.parent / 'study.evidence.json'
                if package_path.exists() or package_path.is_symlink():
                    try:
                        existing = read_confined(self.session.run_dir,
                            str(package_path.relative_to(self.session.run_dir)), len(content))
                        if existing != content:
                            raise ValueError('changed')
                    except (OSError, ValueError, SourceChanged) as exc:
                        self.session.critical_failure = True
                        raise AuditError('Frozen evidence package cannot be replaced.',
                                         code='MODEL_BINDING_CHANGED', failure_layer='integrity') from exc
                else:
                    artifacts.atomic(package_path, content)
                attempt_hashes['study.evidence.json'] = {'sha256': digest(content), 'bytes': len(content)}
                self.attempts.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
                meta.update(status='SUCCEEDED', finished_at=now(), collection_complete=True,
                            duration_seconds=round(self.session.reporter.clock() - attempt_started, 3))
                self.session.record_attempt_metrics(meta, context, attempt_started)
                artifacts.save_json(attempt / 'invocation.json', meta)
                artifacts.save_json(destination / 'invocation.json', meta)
                self.session.reporter.emit('study_phase_completed', **self.session.stage_context(stage, context))
                return data, meta
            report_hash = self.publisher.publish_result(stage, data, destination)
            artifact_hashes = {name: {'sha256': digest(blob), 'bytes': len(blob)}
                               for name, blob in self.publisher.artifact_contents(stage, data).items()}
            meta.update(status='SUCCEEDED', finished_at=now(), report_sha256=report_hash, publication_complete=True)
            meta['artifact_hashes'] = artifact_hashes
            meta['duration_seconds'] = round(self.session.reporter.clock() - attempt_started, 3)
            self.session.record_attempt_metrics(meta, context, attempt_started)
            artifacts.save_json(attempt / 'invocation.json', meta)
            artifacts.save_json(destination / 'invocation.json', meta)
            if phase:
                self.session.reporter.emit('study_phase_completed', **self.session.stage_context(stage, context))
            return data, meta
        except BaseException as exc:
            meta['tool_observation'] = observer.snapshot()
            if getattr(exc, 'cleanup_failed', False):
                self.session.critical_failure = True
                meta.setdefault('cleanup_errors', []).append('process_cleanup_failed')
            if meta.get('cleanup_errors'):
                self.session.critical_failure = True
            meta.update(status='FAILED', finished_at=now(), error=asdict(diagnostic(exc)),
                        publication_complete=False, duration_seconds=round(self.session.reporter.clock() - attempt_started, 3))
            self.session.record_attempt_metrics(meta, context, attempt_started)
            # A failing diagnostic write must not mask the original exception.
            with contextlib.suppress(OSError):
                artifacts.save_json(attempt / 'invocation.json', meta)
                artifacts.save_json(destination / 'invocation.json', meta)
            raise

    def command(self, stage, state, agent, schema_path, env, *, reports_only=False, schema=None, writer_only=False,
                mcp_manifest=None, mcp_manifest_sha256=None, mcp_catalog=None):
        return command(self.session, stage, state, agent, schema_path, env,
                       reports_only=reports_only, schema=schema, writer_only=writer_only,
                       **({'mcp_catalog': mcp_catalog} if mcp_catalog is not None else {}),
                       **({'mcp_manifest': mcp_manifest, 'mcp_manifest_sha256': mcp_manifest_sha256}
                          if mcp_manifest is not None else {}))
