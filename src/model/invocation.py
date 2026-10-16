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
from src.contracts.contracts import ContractError, strict_json, result_diagnostics, CONTRACT_ID, ARTIFACT_FORMAT, response_error
from src.runtime.reporting import diagnostic
from src.runtime.execution import Budget
from src.runtime.tool_observer import ToolObserver
from src.backends import codex, opencode_cli, xxx
from src.backends.json_response import json_object_response
from src.model.structured_output import retry_policy
from src.reports.final_report import recoverable_material
from src.analysis.ledger import prepare_result
from src.analysis.evidence import source_catalog, SourceChanged
from src.model.model_context import (CONTEXT_FORMAT, build_model_prompt, input_measurements,
                                     serialize_model_input)
from src.analysis.coverage_plan import inventory_summary, build_coverage_plan
from src.runtime.errors import AuditError, UnsafeRepository
from src.runtime.artifacts import now, digest, private_directory, neutral_temporary_base, append_observation
from src.runtime.process import cli_env
from src.backends.preflight import CLI_ADAPTERS, command
from src.model.attempts import AttemptStore
from src.model.validation import ResultValidator
from src.runtime import artifacts, process as process_runtime

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
            return self._invoke_once(stage, context, destination, budget=budget,
                                     evidence_pins=evidence_pins, evidence_metadata=evidence_metadata, binding=binding,
                                     binding_hashes=binding_hashes, attempt_hashes=attempt_hashes,
                                     preparation=preparation)
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
                     preparation=None):
        attempt_started = self.session.reporter.clock()
        self.attempts.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
        agent = self.session.cfg['_agents'][stage]
        variant = context.get('prompt_variant', stage)
        template = Path(self.session.cfg['_prompt_paths'][variant]).read_text()
        reports_only = stage == 'compare'
        # Assemble only this stage's inputs; the configured CLI profile remains available.
        projected = binding.project(context)
        context_json = serialize_model_input(projected)
        schema = binding.schema
        schema_json = serialize_model_input(schema)
        prompt = build_model_prompt(template, context_json, schema_json, agent['backend'],
                                    source_index='source_context' in projected)
        measurements = input_measurements(template, context_json, schema_json, prompt,
            source_context=context.get('source_context'),
            transmitted_source_context=projected.get('source_context'))
        if preparation is not None and 'source_index' in measurements:
            index_sizes = measurements['source_index']
            preparation = dict(preparation, original_bytes=index_sizes['original_utf8_bytes'],
                transmitted_bytes=index_sizes['transmitted_utf8_bytes'],
                saved_bytes=index_sizes['saved_utf8_bytes'], reduction_percent=index_sizes['reduction_percent'])
        payload = prompt.encode('utf-8')
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
        meta = {'stage': stage, 'invocation_id': str(uuid.uuid4()), 'started_at': now(),
            'backend': agent['backend'], 'executable': agent['executable'],
            'model_requested': agent.get('model'), 'cli_version': self.session.versions.get(agent['backend'] + ':' + agent['executable']),
            'prompt_sha256': digest(payload), 'template_sha256': digest(template.encode()),
            'schema_sha256': digest(json.dumps(schema, sort_keys=True).encode()),
            'input_bytes': len(payload), 'status': 'RUNNING'}
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
        if agent['backend'] == 'xxx':
            meta['retry_policy'] = retry_policy(0, 0, None)
            meta['compatibility_profile'] = xxx.PROFILE
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
        observer = ToolObserver(agent['backend'], enabled=self.session.execution['read_loop_guard'],
                                clock=self.session.reporter.clock, context=context, attempt_id=meta['invocation_id'],
                                event_sink=lambda event: append_observation(attempt / 'tool-events.jsonl', event))
        observer.seed_prompt(context.get('source_context', {}).get('fragments', []))
        try:
            self.attempts.save_binding(binding, None, None, attempt, meta)
            budget.check()
            self.publisher.assert_coverage_file(context)
            if self.session.folder:
                self.session.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.session.assert_source()
            with tempfile.TemporaryDirectory(prefix='archaudit-invocation-', dir=neutral_temporary_base(self.session.source_path)) as raw:
                state = Path(raw).resolve()
                cwd = state if reports_only else self.session.source_path
                env = cli_env(cwd)
                if self.session.repo:
                    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
                        env.pop(key, None)
                schema_path = state / 'output.schema.json'
                artifacts.save_json(schema_path, schema)
                try:
                    if agent['backend'] == 'xxx':
                        cmd = self.command(stage, state, agent, schema_path, env, reports_only=reports_only, schema=schema)
                        data, provider_meta = xxx.invoke(cmd, cwd, env, payload, process=process_runtime.process,
                            artifacts=attempt, budget=budget, meta=meta, observer=observer, process_options={
                                'reporter': self.session.reporter, 'context': self.session.stage_context(stage, context),
                                'clock': self.session.reporter.clock, 'progress_interval': self.session.reporter.progress_interval})
                        meta['backend_result_valid'] = True
                        meta['provider_metadata'] = {k: v for k, v in provider_meta.items() if k != 'metrics'}
                        artifacts.save_json(attempt / 'extracted.json', data)
                        self.attempts.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
                        data = self.validator.validate_attempt(stage, data, context, attempt, meta, binding,
                            expected_files=evidence_pins, expected_metadata=evidence_metadata)
                    else:
                        if agent['backend'] == 'opencode':
                            opencode_cli.verify_version(meta['cli_version'])
                        cmd = self.command(stage, state, agent, schema_path, env, reports_only=reports_only, schema=schema)
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
                            raise AuditError(f'{agent["backend"]} exited with {r["returncode"]}; inspect private attempt logs.',
                                code='CLI_FAILED', failure_kind='BACKEND_ERROR', failure_layer='backend')
                        output = (codex.read_response(schema_path)
                                  if agent['backend'] == 'codex' else r['stdout'].decode('utf-8'))
                        if agent['backend'] == 'opencode':
                            try:
                                answer, provider_meta = opencode_cli.completed_response(output)
                            except ContractError as exc:
                                if exc.failure_kind != 'INCOMPLETE_OUTPUT' or not exc.details.get('session_id'):
                                    raise
                                # V2 can exit before emitting step_finish; export confirms the same answer.
                                export_dir = attempt / 'session-export'
                                private_directory(export_dir)
                                exported = process_runtime.process([agent['executable'], 'session', 'export', '--standalone',
                                                    exc.details['session_id']], cwd, env, reporter=self.session.reporter,
                                                   context=self.session.stage_context(stage, context), log_dir=export_dir,
                                                   clock=self.session.reporter.clock, budget=budget)
                                if exported['returncode']:
                                    raise response_error('INCOMPLETE_OUTPUT', 'result',
                                                         'OpenCode could not export the final session for verification.') from exc
                                answer, provider_meta = opencode_cli.completed_response(
                                    output, exported=exported['stdout'].decode('utf-8'),
                                    agent_name=cmd[cmd.index('--agent') + 1])
                            # Completion and usage survive a later JSON or contract failure.
                            meta['provider_metadata'] = provider_meta
                            meta.update({key: provider_meta[key] for key in ('session_id', 'message_id', 'finish_reason')})
                            meta['metrics'] = opencode_cli.collect_metrics(
                                output, agent.get('model'), completed=provider_meta.get('exported_finish'))
                            try:
                                data, response_meta = json_object_response(answer, allow_intro=True)
                            except ContractError as exc:
                                if 'response_normalization' in exc.details:
                                    provider_meta['response_normalization'] = exc.details['response_normalization']
                                raise
                            provider_meta.update(response_meta)
                        else:
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
                data = prepare_result(stage, data, context, evidence_pins, expected_metadata=evidence_metadata,
                                      citation_corrections=meta.get('citation_corrections'))
                if stage == 'study' and data.get('advisory'):
                    meta['advisory_diagnostics'] = data['advisory']['diagnostics']
                    self.attempts.save_attempt_value(attempt, 'advisory.json', data['advisory'], meta)
                for warning in data.get('program_checks', {}).get('citation_warnings', []):
                    self.session.reporter.emit('citation_warning', **(warning | self.session.stage_context(stage, context)),
                                       evidence_branch=warning.get('branch'))
                if stage in ('study', 'review'):
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
            if agent['backend'] == 'xxx' and meta.get('model_actual'):
                meta['model_actual_source'] = 'backend assistant-message metadata'
            if stage == 'review':
                self.publisher.assert_review_files(context, destination.parent)
            self.publisher.assert_coverage_file(context)
            self.attempts.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
            report_hash = self.publisher.publish_result(stage, data, destination)
            artifact_hashes = {name: {'sha256': digest(blob), 'bytes': len(blob)}
                               for name, blob in self.publisher.artifact_contents(stage, data).items()}
            meta.update(status='SUCCEEDED', finished_at=now(), report_sha256=report_hash, publication_complete=True)
            meta['artifact_hashes'] = artifact_hashes
            meta['duration_seconds'] = round(budget.clock() - budget.started, 3)
            self.session.record_attempt_metrics(meta, context, attempt_started)
            artifacts.save_json(attempt / 'invocation.json', meta)
            artifacts.save_json(destination / 'invocation.json', meta)
            return data, meta
        except BaseException as exc:
            meta['tool_observation'] = observer.snapshot()
            if getattr(exc, 'cleanup_failed', False):
                self.session.critical_failure = True
                meta.setdefault('cleanup_errors', []).append('process_cleanup_failed')
            if meta.get('cleanup_errors'):
                self.session.critical_failure = True
            meta.update(status='FAILED', finished_at=now(), error=asdict(diagnostic(exc)),
                        publication_complete=False, duration_seconds=round(budget.clock() - budget.started, 3))
            self.session.record_attempt_metrics(meta, context, attempt_started)
            # A failing diagnostic write must not mask the original exception.
            with contextlib.suppress(OSError):
                artifacts.save_json(attempt / 'invocation.json', meta)
                artifacts.save_json(destination / 'invocation.json', meta)
            raise

    def command(self, stage, state, agent, schema_path, env, *, reports_only=False, schema=None):
        return command(self.session, stage, state, agent, schema_path, env,
                       reports_only=reports_only, schema=schema)
