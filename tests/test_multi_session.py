# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline multi-session orchestration through ordinary backend invocations."""
import copy
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from explain import AuditError, Runner, prepare_result
from src.analysis.evidence import canonical, sha
from src.analysis.study_shards import OBSERVATION_FIELDS, empty_shard_result, synthesis_inputs
from src.contracts.contracts import ContractError, validate_result, validate_schema, workflow_satisfied
from src.contracts.saved_contracts import SAVED_FOLDER_SCHEMAS, SAVED_SCHEMAS
from src.runtime.reporting import Reporter
from src.tui.state import RunState
from src.reports.document_rendering import validate_materialized
from fixtures.cli_response import cli_result
from fixtures.ledger_response import response, prompt_context
from fixtures.xxx_protocol import transcript
from test_folder import FolderFixture
from test_revision_pipeline import initial_finding
import test_explain as git_fixtures


class MultiSessionTests(FolderFixture):
    def run_case(self, *, backend='codex', large=True, enabled=True, change=None, review=False,
                 policy='strict', keep_going=True, catalog_paths=None, reporter=None):
        paths = ['app.py'] + [f'part{i}.py' for i in range(2, 10)] if large else ['app.py']
        for path in paths:
            (self.source / path).write_text('def main(): return 42\n')
        self.value.update(multi_session=enabled, result_policy=policy, continue_on_error=keep_going,
                          execution={'review_enabled': review}, stage_agents={'study': {'model': 'fixture/study-model'}})
        self.value['agent']['backend'] = backend
        cfg = self.config()
        runner = Runner(cfg, self.base / f'run-{len(list(self.base.glob("run-*")))}', reporter=reporter)
        runner.versions[backend + ':' + cfg['_agents']['study']['executable']] = '2.0.0' if backend == 'opencode' else 'fixture'
        calls, exports = [], {}

        def process(command, cwd, env, payload=b'', **kwargs):
            if backend == 'xxx' and command[1] == 'export':
                return dict(returncode=0, stdout=json.dumps(exports[command[2]]).encode(), stderr=b'')
            if backend == 'xxx' and command[1:3] == ['session', 'delete']:
                exports.pop(command[3])
                return dict(returncode=0, stdout=b'', stderr=b'')
            context = prompt_context(payload)
            if context['stage'] == 'study-shard':
                self.assertEqual([s['id'] for s in runner.manifest['study_shards'] if s['status'] == 'RUNNING'],
                                 [context['analysis_shard']['id']])
            calls.append(dict(context=context, command=command, cwd=cwd, env=env.copy(), prompt=payload.decode()))
            data = response(context)
            if context['stage'] == 'study' and not context.get('prompt_variant') and not enabled:
                # The ordinary fixture cites app.py only. Give each primary area
                # its own source pointer to make the legacy study fully covered.
                data['evidence'] = [dict(id=f'E-{i + 1:03d}', source_id='source-001', path=p,
                                        start_line=1, end_line=1, quote='') for i, p in enumerate(paths)]
                for i, area in enumerate(data['coverage'], 1):
                    area['evidence_ids'] = [f'study:E-{i:03d}']
            if change:
                change(runner, context, data)
            if backend == 'claude-code':
                return dict(returncode=0, stdout=json.dumps(dict(is_error=False, structured_output=data)).encode(), stderr=b'')
            if backend == 'xxx':
                sid = f'ses_multi{len(calls)}'
                output, exported = transcript(data, session=sid, agent=command[command.index('--agent') + 1],
                    prompt=payload.decode(), cwd=str(cwd), model='fixture/study-model')
                exports[sid] = exported
                return dict(returncode=0, stdout=output.encode(), stderr=b'')
            return cli_result(command, data)

        with patch.dict(os.environ, {'AUDIT_TEST_SUBSYSTEM_PATHS': json.dumps(catalog_paths or paths)}), \
                patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process):
            manifest, code = runner.run()
        self.runner, self.calls, self.manifest = runner, calls, manifest
        return manifest, code

    def test_small_or_disabled_uses_one_ordinary_study(self):
        self.value['max_source_bytes_per_session'] = 22
        for large, enabled in ((False, True), (True, False)):
            with self.subTest(large=large, enabled=enabled):
                manifest, code = self.run_case(large=large, enabled=enabled)
                self.assertEqual(code, 0, manifest.get('diagnostics'))
                self.assertEqual([c['context']['stage'] for c in self.calls], ['catalog', 'study'])
                self.assertEqual(manifest['required_sessions'], 1)
                self.assertEqual(manifest['study_origin'], 'direct_single_session')
                self.assertEqual(manifest['study_shards'], [])
                self.assertNotIn('synthesis_invocation', manifest)
                self.assertEqual(manifest['analysis_plan']['thresholds']['max_source_bytes_per_session'], 22)
                self.assertEqual(manifest['analysis_plan']['shards'][0]['over_capacity'], large)

    def test_success_uses_same_plan_all_backends_and_disables_synthesis_tools(self):
        assignments = []
        for backend in ('codex', 'claude-code', 'opencode', 'xxx'):
            with self.subTest(backend=backend):
                manifest, code = self.run_case(backend=backend)
                self.assertEqual(code, 0, manifest.get('diagnostics'))
                plan = manifest['analysis_plan']
                assignments.append(canonical(plan))
                self.assertEqual([c['context']['stage'] for c in self.calls],
                                 ['catalog', 'study-shard', 'study-shard', 'study-shard', 'study'])
                self.assertEqual([s['status'] for s in manifest['study_shards']], ['SUCCEEDED'] * 3)
                self.assertEqual([c['context']['analysis_shard']['id'] for c in self.calls[1:4]],
                                 ['R-001', 'R-002', 'R-003'])
                self.assertEqual(manifest['study_origin'], 'multi_session_synthesis')
                self.assertEqual(manifest['synthesis_status'], 'SUCCEEDED')
                self.assertTrue(workflow_satisfied(manifest))
                validate_schema(manifest['study'], SAVED_FOLDER_SCHEMAS['study'])
                validate_materialized(manifest['study'])
                self.assertTrue((self.runner.run_dir / 'ARCHITECTURE.md').exists())
                self.assertEqual(json.loads((self.runner.run_dir / manifest['analysis_plan_path']).read_text()), plan)
                synthesis = self.calls[-1]
                self.assertEqual(synthesis['context']['prompt_variant'], 'synthesis')
                self.assertNotEqual(synthesis['cwd'], self.source)
                self.assertEqual(len(synthesis['context']['validated_shards']), 3)
                self.assertEqual(len(synthesis['context']['synthesis_claims']), 9)
                self.assertNotIn('_synthesis_resolutions', synthesis['context'])
                self.assertNotIn('shard_id_mappings', synthesis['context'])
                attempt = Path(manifest['synthesis_invocation']['artifact_directory'])
                raw = json.loads((attempt / 'extracted.json').read_text())
                expanded = json.loads((attempt / 'expanded.json').read_text())
                normalized = json.loads((attempt / 'normalized.json').read_text())
                schema = json.loads((attempt / 'schema.json').read_text())
                self.assertEqual(set(raw), {'task', 'source_directory', 'source_snapshot_id',
                                           'completion_status', 'limitations', 'report_sections'})
                self.assertEqual(set(schema['properties']), set(raw))
                self.assertEqual(schema, json.loads(synthesis['prompt'].split('# Required final JSON Schema\n')[1]))
                if backend == 'claude-code':
                    command = synthesis['command']
                    self.assertEqual(schema, json.loads(command[command.index('--json-schema') + 1]))
                inputs = synthesis_inputs(plan, manifest['study_shards'])
                before = copy.deepcopy(synthesis['context'])
                before['shard_id_mappings'] = inputs['shard_id_mappings']
                for shard, full in zip(before['validated_shards'], inputs['validated_shards']):
                    for field in ('evidence', 'claims', 'coverage'):
                        self.assertNotIn(field, shard)
                        shard[field] = full[field]
                context_json = json.dumps(synthesis['context'], ensure_ascii=False)
                before_prompt = synthesis['prompt'].replace(context_json, json.dumps(before, ensure_ascii=False))
                self.assertLess(len(synthesis['prompt'].encode('utf-8')), len(before_prompt.encode('utf-8')))
                saved = copy.deepcopy(manifest['study'])
                for claim in saved['claims']:
                    claim.pop('document_locators')
                for field in ('evidence', 'claims', 'coverage'):
                    expected = inputs['synthesis_' + field]
                    self.assertEqual(synthesis['context']['synthesis_' + field], expected)
                    self.assertEqual(expanded[field], expected)
                    self.assertEqual(normalized[field], expected)
                    self.assertEqual(saved[field], expected)
                self.assertEqual([e['id'] for e in saved['evidence']], [f'E-{i:03d}' for i in range(1, 10)])
                self.assertEqual([c['id'] for c in saved['claims']], [f'C-{i:03d}' for i in range(1, 10)])
                self.assertEqual([c['evidence_ids'] for c in saved['claims']],
                                 [[f'study:E-{i:03d}'] for i in range(1, 10)])
                binding = json.loads((attempt / 'binding.json').read_text())
                self.assertEqual(binding['wire_sha256'], sha(canonical(raw)))
                self.assertEqual(binding['expanded_sha256'], sha(canonical(expanded)))
                if backend == 'codex':
                    self.assertIn('features.shell_tool=false', synthesis['command'])
                elif backend == 'claude-code':
                    self.assertEqual(synthesis['command'][synthesis['command'].index('--tools') + 1], '')
                else:
                    config = json.loads(synthesis['env']['OPENCODE_CONFIG_CONTENT'])
                    name = synthesis['command'][synthesis['command'].index('--agent') + 1]
                    if backend == 'xxx':
                        self.assertEqual(config['agent'][name]['permission'], {'*': 'deny'})
                    else:
                        self.assertEqual(config['agents'][name]['permissions'],
                                         [{'action': '*', 'resource': '*', 'effect': 'deny'}])
                for state, call in zip(manifest['study_shards'], self.calls[1:4]):
                    meta = state['study-shard_invocation']
                    self.assertEqual((meta['backend'], meta['model_requested']), (backend, 'fixture/study-model'))
                    self.assertTrue(meta['backend_result_valid'] and meta['local_validation'] and meta['publication_complete'])
                    context = call['context']
                    assigned = context['analysis_shard']['subsystem_ids']
                    self.assertEqual([a['id'] for a in context['coverage_plan']['areas']], assigned)
                    self.assertNotIn('analysis_plan', context)
                    self.assertNotIn('_inventory', context)
                    self.assertNotIn('catalog', context)
                    self.assertIn('Do not select your own scope', call['prompt'])
                    path = state['directory'] + '/study-shard.json'
                    blob = (self.runner.run_dir / path).read_bytes()
                    self.assertEqual(json.loads(blob), state['study-shard'])
                    self.assertEqual(inputs['_shard_artifact_hashes'][path], {'sha256': sha(blob), 'bytes': len(blob)})
                self.assertEqual(manifest['metrics']['attempts'], 5)
                rows = [s for s in manifest['metrics']['stages'] if s['stage'] == 'study-shard']
                self.assertEqual([s['shard_id'] for s in rows], ['R-001', 'R-002', 'R-003'])
                self.assertTrue(all(s['attempts'] == 1 for s in rows))
                self.assertEqual(manifest['synthesis_invocation']['model_requested'], 'fixture/study-model')
                self.assertFalse(manifest['synthesis_invocation']['source_tools_enabled'])
        self.assertEqual(len(set(assignments)), 1)

    def test_overlapping_subsystems_keep_shared_evidence_and_distinct_claims(self):
        def change(runner, context, data):
            if context['stage'] != 'study-shard': return
            for claim in list(data['claims']):
                cid = f'C-{len(data["claims"]) + 1:03d}'
                data['claims'].append(claim | {'id': cid, 'statement': 'Uses the shared entry point.'})
                data['relationships'].append(dict(subsystem_id=claim['scope'], related_path='part2.py',
                    description='Shares the entry point with other subsystems.', claim_ids=[claim['id'], cid]))
            data['limitations'] = ['Runtime behavior was not checked.']
        manifest, code = self.run_case(catalog_paths=['.'] * 9, change=change)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        context = self.calls[-1]['context']
        inputs = synthesis_inputs(manifest['analysis_plan'], manifest['study_shards'])
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(len(context['synthesis_evidence']), 9)
        self.assertEqual(len(context['synthesis_claims']), 18)
        self.assertEqual({e['path'] for e in context['synthesis_evidence']}, {'app.py'})
        claims = {c['id']: c for c in context['synthesis_claims']}
        for shard, full in zip(context['validated_shards'], inputs['validated_shards']):
            self.assertEqual(shard['relationships'], full['relationships'])
            self.assertEqual(shard['limitations'], full['limitations'])
            for relation in shard['relationships']:
                self.assertTrue(all(claims[c]['scope'] == relation['subsystem_id'] for c in relation['claim_ids']))
        for field in ('evidence', 'claims', 'coverage'):
            self.assertEqual(context['synthesis_' + field], inputs['synthesis_' + field])
        validate_materialized(manifest['study'])

    def test_shard_schema_semantic_and_completion_failures_block_synthesis(self):
        for kind in ('schema', 'semantic', 'evidence', 'coverage', 'partial_coverage', 'incomplete', 'identity', 'backend'):
            for policy in ('strict', 'compromise'):
                def change(runner, context, data):
                    if context.get('analysis_shard', {}).get('id') != 'R-002':
                        return
                    if kind == 'schema': data.pop('components')
                    elif kind == 'semantic': data['claims'][0]['evidence_ids'] = ['study:E-999']
                    elif kind == 'evidence': data['evidence'][0]['quote'] = 'not the source'
                    elif kind == 'coverage': data['coverage'].pop()
                    elif kind == 'partial_coverage':
                        data['coverage'][0].update(status='PARTIALLY_INSPECTED', limitation='Only the entry point was inspected.')
                    elif kind == 'incomplete': data.update(completion_status='PARTIAL', limitations=['Missing inspection.'])
                    elif kind == 'identity': data['assigned_subsystem_ids'] = ['S-001']
                    else: raise AuditError('Synthetic backend failure.', code='CLI_FAILED', failure_layer='backend')
                with self.subTest(kind=kind, policy=policy):
                    manifest, code = self.run_case(change=change, policy=policy)
                    self.assertEqual((manifest['status'], code),
                                     ('PARTIAL', 2) if policy == 'compromise' else ('FAILED', 1))
                    self.assertTrue(manifest['has_usable_material'])
                    self.assertFalse(manifest['workflow_satisfied'])
                    self.assertFalse(manifest['accepted'])
                    self.assertIsNone(manifest['study'])
                    self.assertEqual(len(self.calls), 4)
                    self.assertEqual([s['status'] for s in manifest['study_shards']], ['SUCCEEDED', 'FAILED', 'SUCCEEDED'])
                    self.assertEqual(manifest['synthesis_status'], 'SKIPPED')
                    self.assertFalse(any(c['context'].get('prompt_variant') == 'synthesis' for c in self.calls))
                    self.assertFalse((self.runner.run_dir / 'ARCHITECTURE.md').exists())
                    for sid in ('R-001', 'R-003'):
                        self.assertTrue((self.runner.run_dir / 'study-shards' / sid / 'study-shard.json').exists())
                    report = (self.runner.run_dir / 'FINAL_REPORT.md').read_text()
                    self.assertIn('R-001/C-001', report)
                    self.assertIn('R-003/study:E-001', report)
                    self.assertIn('R-002', report)
                    name = 'invocation.json' if kind == 'backend' else 'validation.json'
                    self.assertTrue((self.runner.run_dir / 'study-shards/R-002/study-shard.logs/attempt-001' / name).exists())
                    if kind in ('partial_coverage', 'incomplete'):
                        error = manifest['study_shards'][1]['study-shard_invocation']['error']
                        reason = 'coverage_unfinished=1' if kind == 'partial_coverage' else 'completion_status=PARTIAL'
                        self.assertIn(reason, error['message'])
                        self.assertEqual(error['details']['code'], 'SHARD_POLICY_UNSATISFIED')

    def test_source_quote_snippets_and_failures_across_backends_and_policies(self):
        for backend in ('codex', 'claude-code', 'opencode', 'xxx'):
            for policy in ('strict', 'compromise'):
                for valid in (True, False):
                    reporter = Reporter(stdout=io.StringIO(), stderr=io.StringIO(), progress=False)
                    self.addCleanup(reporter.close)
                    def change(runner, context, data):
                        if context['stage'] == 'catalog':
                            reporter.attach_log(runner.run_dir)
                        if context['stage'] != 'study-shard': return
                        for evidence in data['evidence']:
                            evidence['quote'] = 'return 42'
                        if not valid and context['analysis_shard']['id'] == 'R-002':
                            data['evidence'][0]['quote'] = 'private invalid quote'
                    with self.subTest(backend=backend, policy=policy, valid=valid):
                        manifest, code = self.run_case(backend=backend, policy=policy, change=change, reporter=reporter)
                        self.assertEqual(code, 0 if valid else 2 if policy == 'compromise' else 1,
                                         manifest.get('diagnostics'))
                        self.assertEqual(manifest['synthesis_status'], 'SUCCEEDED' if valid else 'SKIPPED')
                        if valid:
                            self.assertTrue(workflow_satisfied(manifest))
                            self.assertTrue((self.runner.run_dir / 'ARCHITECTURE.md').exists())
                            continue
                        invocation = manifest['study_shards'][1]['study-shard_invocation']
                        error = invocation['error']
                        self.assertIn('QUOTE_MISMATCH=1', error['message'])
                        self.assertEqual(error['details']['code'], 'SHARD_POLICY_UNSATISFIED')
                        self.assertEqual(error['details']['completion_status'], 'COMPLETE')
                        self.assertEqual(error['details']['evidence_counts']['QUOTE_MISMATCH'], 1)
                        self.assertNotIn('private invalid quote', json.dumps(error))
                        attempt = Path(invocation['artifact_directory'])
                        validation = json.loads((attempt / 'validation.json').read_text())
                        self.assertTrue(validation['valid'])
                        self.assertFalse(validation['policy_valid'])
                        self.assertEqual(validation['policy_error'], error)
                        prepared = json.loads((attempt / 'prepared.json').read_text())
                        self.assertFalse(prepared['program_checks']['policy_satisfied'])
                        self.assertEqual(error['details']['coverage'], prepared['program_checks']['coverage'])
                        event = next(e for e in manifest['diagnostics'] if e.get('shard_id') == 'R-002')
                        self.assertEqual(event['message'], error['message'])
                        state = RunState()
                        state.apply('error', event)
                        self.assertEqual(state.row(event)['result'], error['message'])
                        logged = [json.loads(line) for line in (self.runner.run_dir / 'run.log').read_text().splitlines()]
                        self.assertTrue(any(e.get('event') == 'error' and e.get('message') == error['message'] for e in logged))

    def test_fail_fast_keeps_remaining_shards_planned(self):
        def change(runner, context, data):
            if context.get('analysis_shard', {}).get('id') == 'R-002': data.pop('components')
        manifest, code = self.run_case(change=change, keep_going=False, policy='compromise')
        self.assertEqual((manifest['status'], code), ('PARTIAL', 2))
        self.assertEqual(manifest['synthesis_status'], 'SKIPPED')
        self.assertEqual([s['status'] for s in manifest['study_shards']], ['SUCCEEDED', 'FAILED', 'PLANNED'])
        report = (self.runner.run_dir / 'FINAL_REPORT.md').read_text()
        self.assertIn('PLANNED', report)
        self.assertIn('R-001/C-001', report)

    def test_shard_normalization_keeps_original_and_qualifies_existing_references(self):
        def change(runner, context, data):
            if context['stage'] != 'study-shard': return
            for field in ('claims', 'coverage'):
                for record in data[field]:
                    record['evidence_ids'] = [ref.removeprefix('study:') for ref in record['evidence_ids']]
        manifest, code = self.run_case(change=change)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual(len(self.calls), 5)
        for state in manifest['study_shards']:
            meta = state['study-shard_invocation']
            attempt = Path(meta['artifact_directory'])
            original = json.loads((attempt / 'extracted.json').read_text())
            normalized = json.loads((attempt / 'normalized.json').read_text())
            for field in ('claims', 'coverage'):
                self.assertEqual(original[field][0]['evidence_ids'], ['E-001'])
                self.assertEqual(normalized[field][0]['evidence_ids'], ['study:E-001'])
            provenance = meta['normalization_provenance']
            self.assertGreater(provenance['replacement_count'], 0)
            self.assertEqual(provenance, state['study-shard']['normalization_provenance'])
            self.assertTrue(json.loads((attempt / 'validation.json').read_text())['policy_valid'])
        for shard in self.calls[-1]['context']['validated_shards']:
            self.assertNotIn('normalization_provenance', shard)

    def test_all_failed_shards_produce_no_usable_material(self):
        def change(runner, context, data):
            if context['stage'] == 'study-shard': data['claims'][0]['evidence_ids'] = ['study:E-999']
        manifest, code = self.run_case(change=change, policy='compromise')
        self.assertEqual((manifest['status'], code), ('FAILED', 1))
        self.assertFalse(manifest['has_usable_material'])
        self.assertEqual(len(self.calls), 4)

    def test_empty_file_metadata_reaches_synthesis_and_source_guard(self):
        empty = self.source / '.codex'
        paths = ['.codex', 'app.py'] + [f'part{i}.py' for i in range(2, 10)]
        for mutate in (False, True):
            empty.write_bytes(b'')
            def change(runner, context, data):
                if context['stage'] == 'study-shard':
                    empty_ids = {'study:' + e['id'] for e in data['evidence'] if e['path'] == '.codex'}
                    removed = {c['id'] for c in data['claims'] if set(c['evidence_ids']) & empty_ids}
                    data['evidence'] = [e for e in data['evidence'] if 'study:' + e['id'] not in empty_ids]
                    data['claims'] = [c for c in data['claims'] if c['id'] not in removed]
                    for field in OBSERVATION_FIELDS:
                        data[field] = [r for r in data[field] if not set(r['claim_ids']) & removed]
                    for area in data['coverage']:
                        if set(area['evidence_ids']) & empty_ids:
                            area.update(evidence_ids=[], limitation='Empty file; no source lines.')
                elif mutate and context.get('prompt_variant') == 'synthesis':
                    empty.write_text('changed after metadata verification\n')
            with self.subTest(mutate=mutate):
                manifest, code = self.run_case(change=change, catalog_paths=paths, policy='compromise')
                if mutate:
                    self.assertEqual((code, manifest['status'], manifest['critical_failure']), (1, 'FAILED', True))
                    self.assertFalse(manifest['has_usable_material'])
                else:
                    self.assertEqual(code, 0, manifest.get('diagnostics'))
                    coverage = manifest['study']['program_checks']['coverage']
                    self.assertEqual(coverage['metadata_verified_empty_paths'], ['.codex'])
                    self.assertEqual(manifest['analysis_plan']['totals']['source_files'], 10)
                    self.assertIn('Empty files verified from pinned metadata: .codex',
                                  (self.runner.run_dir / 'FINAL_REPORT.md').read_text())

    def test_interrupted_run_preserves_exit_code_and_published_parts(self):
        def change(runner, context, data):
            if context.get('analysis_shard', {}).get('id') == 'R-002': raise KeyboardInterrupt()
        manifest, code = self.run_case(change=change, policy='compromise')
        self.assertEqual((manifest['status'], code), ('FAILED', 130))
        self.assertFalse(manifest['workflow_satisfied'])
        self.assertEqual(manifest['synthesis_status'], 'SKIPPED')
        self.assertEqual([s['status'] for s in manifest['study_shards']], ['SUCCEEDED', 'FAILED', 'PLANNED'])
        self.assertTrue((self.runner.run_dir / 'study-shards/R-001/study-shard.json').is_file())

    def test_source_plan_and_published_shard_mutations_stop_the_run(self):
        for kind in ('source', 'plan', 'shard'):
            def change(runner, context, data):
                if context.get('analysis_shard', {}).get('id') != 'R-002': return
                path = {'source': self.source / 'app.py', 'plan': runner.run_dir / 'analysis.plan.json',
                        'shard': runner.run_dir / 'study-shards/R-001/study-shard.json'}[kind]
                path.write_bytes(path.read_bytes() + b'\nchanged')
            with self.subTest(kind=kind):
                manifest, code = self.run_case(change=change)
                self.assertEqual((code, manifest['status'], manifest['critical_failure']), (1, 'FAILED', True))
                self.assertFalse(any(c['context'].get('prompt_variant') == 'synthesis' for c in self.calls))
                self.assertEqual([s['status'] for s in manifest['study_shards']], ['SUCCEEDED', 'FAILED', 'PLANNED'])
                self.assertFalse((self.runner.run_dir / 'ARCHITECTURE.md').exists())

    def test_source_change_between_shards_is_detected_before_next_model_call(self):
        original = Runner.stage_started
        def change(runner, stage, context):
            if context.get('analysis_shard', {}).get('id') == 'R-002':
                (self.source / 'app.py').write_text('changed between sessions\n')
            return original(runner, stage, context)
        with patch.object(Runner, 'stage_started', change):
            manifest, code = self.run_case()
        self.assertEqual(code, 1)
        self.assertEqual([c['context']['stage'] for c in self.calls], ['catalog', 'study-shard'])
        self.assertEqual([s['status'] for s in manifest['study_shards']], ['SUCCEEDED', 'FAILED', 'PLANNED'])

    def test_changed_configured_limit_stops_later_integrity_checks(self):
        for variant in ('study', 'study-shard', 'synthesis'):
            self.value['max_source_bytes_per_session'] = 66
            def change(runner, context, data):
                if context.get('prompt_variant', context['stage']) == variant:
                    runner.cfg['max_source_bytes_per_session'] = 67
            with self.subTest(variant=variant):
                manifest, code = self.run_case(change=change, large=variant != 'study')
                self.assertEqual((code, manifest['critical_failure']), (1, True))
                self.assertIn('ANALYSIS_PLAN_CHANGED', {d['code'] for d in manifest['diagnostics']})
                self.assertFalse((self.runner.run_dir / 'ARCHITECTURE.md').exists())

    def test_shard_mutation_during_synthesis_prevents_revision_publication(self):
        def change(runner, context, data):
            if context.get('prompt_variant') == 'synthesis':
                path = runner.run_dir / 'study-shards/R-001/study-shard.json'
                path.write_bytes(path.read_bytes() + b'changed')
        manifest, code = self.run_case(change=change)
        self.assertEqual((code, manifest['critical_failure']), (1, True))
        self.assertFalse((self.runner.run_dir / 'revisions/001/study.json').exists())
        self.assertFalse((self.runner.run_dir / 'ARCHITECTURE.md').exists())

    def test_invalid_catalog_uses_directory_fallback_through_synthesis(self):
        def change(runner, context, data):
            if context['stage'] == 'catalog': data.pop('exclusions')
        manifest, code = self.run_case(change=change, policy='compromise')
        self.assertEqual((code, manifest['status']), (2, 'PARTIAL'))
        self.assertIsNone(manifest.get('catalog'))
        self.assert_partial_synthesis(manifest, 'DIRECTORY_FALLBACK')

    def assert_partial_synthesis(self, manifest, origin):
        self.assertEqual(manifest['synthesis_status'], 'SUCCEEDED')
        self.assertEqual([s['status'] for s in manifest['study_shards']], ['SUCCEEDED'] * 3)
        self.assertEqual(manifest['coverage_plan']['origin'], origin)
        self.assertFalse(manifest['coverage_plan']['policy_satisfied'])
        self.assertFalse(manifest['accepted'])
        self.assertFalse(workflow_satisfied(manifest))
        self.assertFalse(manifest['study']['program_checks']['policy_satisfied'])
        self.assertEqual(manifest['study']['review_plan']['coverage_plan'], manifest['coverage_plan'])
        self.assertEqual(manifest['selected_revision'], '001')
        self.assertTrue(manifest['has_usable_material'])
        self.assertTrue(manifest['selection_publication_complete'])
        validate_materialized(manifest['study'])
        validate_schema(manifest['study'], SAVED_FOLDER_SCHEMAS['study'])
        self.assertTrue((self.runner.run_dir / 'ARCHITECTURE.md').exists())
        final = Path(manifest['final_report']).read_text()
        self.assertIn(origin + ' / PARTIAL', final)
        self.assertIn(manifest['study']['report_markdown'], final)
        for limitation in manifest['coverage_plan']['limitations']:
            self.assertIn(limitation, final)
        self.assertTrue(any(c['context'].get('prompt_variant') == 'synthesis' for c in self.calls))

    def test_salvaged_and_all_unknown_catalogs_publish_partial_synthesis_including_xxx(self):
        for backend in ('codex', 'xxx'):
            for all_unknown in (False, True):
                for review in (False, True):
                    def change(runner, context, data):
                        if context['stage'] == 'catalog':
                            data['limitations'] = ['Original catalog limitation.']
                            for subsystem in data['subsystems']:
                                subsystem['paths'] = ([] if all_unknown else subsystem['paths']) + ['private-unknown']
                    with self.subTest(backend=backend, all_unknown=all_unknown, review=review):
                        manifest, code = self.run_case(backend=backend, change=change, policy='compromise', review=review)
                        self.assertEqual((code, manifest['status']), (2, 'PARTIAL'), manifest.get('diagnostics'))
                        self.assert_partial_synthesis(manifest, 'DIRECTORY_FALLBACK' if all_unknown else 'AGENT_SALVAGED')
                        self.assertIn('Original catalog limitation.', manifest['coverage_plan']['limitations'])
                        self.assertNotIn('private-unknown', Path(manifest['final_report']).read_text())
                        if review:
                            self.assertEqual(manifest['review']['verdict'], 'INCONCLUSIVE')

    def test_salvaged_unclassified_entries_are_studied_without_hiding_catalog_limits(self):
        def change(runner, context, data):
            if context['stage'] == 'catalog':
                data['subsystems'][-1]['paths'] = ['unknown']
        manifest, code = self.run_case(change=change, policy='compromise', catalog_paths=['app.py'] * 9)
        self.assertEqual((code, manifest['status']), (2, 'PARTIAL'))
        self.assertEqual(manifest['coverage_plan']['origin'], 'AGENT_SALVAGED')
        unclassified = next(a for a in manifest['study']['coverage'] if a['area_id'] == 'UNCLASSIFIED')
        self.assertEqual(unclassified['status'], 'INSPECTED')
        self.assertTrue(unclassified['evidence_ids'])
        assigned = [s for s in manifest['analysis_plan']['shards'] if 'UNCLASSIFIED' in s['subsystem_ids']]
        self.assertEqual({p for s in assigned for p in s['primary_file_paths'] if p != 'app.py'},
                         set(manifest['coverage_plan']['unclassified_paths']))
        self.assertFalse(manifest['accepted'])

    def test_catalog_identity_failure_and_strict_unknown_paths_block_analysis(self):
        for kind, policy in (('identity', 'compromise'), ('unknown', 'strict')):
            def change(runner, context, data):
                if context['stage'] != 'catalog': return
                if kind == 'identity': data['source_snapshot_id'] = 'another'
                else: data['subsystems'][0]['paths'].append('unknown')
            with self.subTest(kind=kind):
                manifest, code = self.run_case(change=change, policy=policy)
                self.assertEqual((code, manifest['status']), (1, 'FAILED'))
                self.assertEqual([c['context']['stage'] for c in self.calls], ['catalog'])
                self.assertFalse((self.runner.run_dir / 'ARCHITECTURE.md').exists())

    def test_fallback_and_salvaged_plans_keep_shard_and_integrity_failures_blocking(self):
        for origin in ('AGENT_SALVAGED', 'DIRECTORY_FALLBACK'):
            for kind in ('schema', 'evidence', 'identity', 'source', 'coverage_plan', 'analysis_plan', 'recovery'):
                def change(runner, context, data):
                    if context['stage'] == 'catalog':
                        for subsystem in data['subsystems']:
                            subsystem['paths'] = ([] if origin == 'DIRECTORY_FALLBACK' else subsystem['paths']) + ['unknown']
                    if context.get('analysis_shard', {}).get('id') != 'R-002': return
                    if kind == 'schema': data.pop('components')
                    elif kind == 'evidence': data['evidence'][0]['quote'] = 'not the source'
                    elif kind == 'identity': data['source_snapshot_id'] = 'another'
                    else:
                        path = {'source': self.source / 'app.py',
                                'coverage_plan': runner.run_dir / 'coverage.plan.json',
                                'analysis_plan': runner.run_dir / 'analysis.plan.json',
                                'recovery': runner.run_dir / 'catalog.logs/attempt-001/recovery.json'}[kind]
                        path.write_bytes(path.read_bytes() + b'changed')
                with self.subTest(origin=origin, kind=kind):
                    manifest, code = self.run_case(change=change, policy='compromise')
                    recoverable = kind in ('schema', 'evidence', 'identity')
                    self.assertEqual((code, manifest['status']), (2, 'PARTIAL') if recoverable else (1, 'FAILED'))
                    self.assertEqual(manifest['has_usable_material'], recoverable)
                    self.assertFalse(any(c['context'].get('prompt_variant') == 'synthesis' for c in self.calls))
                    self.assertFalse((self.runner.run_dir / 'ARCHITECTURE.md').exists())
                    self.assertFalse(manifest['accepted'])
                    if kind not in ('schema', 'evidence', 'identity'):
                        self.assertTrue(manifest['critical_failure'])

    def test_synthesis_failure_preserves_shards_without_publishing_stale_study(self):
        for kind in ('claims', 'evidence', 'coverage', 'unexpected', 'source_snapshot_id',
                     'source_directory', 'task', 'missing_claim_ids', 'unbound_claim',
                     'unknown_claim', 'duplicate_claim', 'section_order', 'missing_section', 'backend'):
            for policy in ('strict', 'compromise'):
                def change(runner, context, data):
                    if context.get('prompt_variant') != 'synthesis': return
                    block = data['report_sections'][0]['blocks'][0]
                    if kind in ('claims', 'evidence', 'coverage'):
                        data[kind] = copy.deepcopy(context['synthesis_' + kind])
                    elif kind in ('source_snapshot_id', 'source_directory', 'task', 'unexpected'):
                        data[kind] = 'invalid'
                    elif kind == 'missing_claim_ids': block.pop('claim_ids')
                    elif kind == 'unbound_claim': block['claim_ids'].pop()
                    elif kind == 'unknown_claim': block['claim_ids'].append('C-999')
                    elif kind == 'duplicate_claim': block['claim_ids'].append(block['claim_ids'][0])
                    elif kind == 'section_order': data['report_sections'].reverse()
                    elif kind == 'missing_section': data['report_sections'].pop()
                    else: raise AuditError('Synthetic backend failure.', code='CLI_FAILED', failure_layer='backend')
                with self.subTest(kind=kind, policy=policy):
                    manifest, code = self.run_case(change=change, policy=policy, review=True)
                    self.assertEqual((manifest['status'], code),
                                     ('PARTIAL', 2) if policy == 'compromise' else ('FAILED', 1))
                    self.assertTrue(manifest['has_usable_material'])
                    self.assertFalse(manifest['accepted'])
                    self.assertEqual(manifest['synthesis_status'], 'FAILED')
                    self.assertEqual([s['status'] for s in manifest['study_shards']], ['SUCCEEDED'] * 3)
                    self.assertFalse((self.runner.run_dir / 'ARCHITECTURE.md').exists())
                    self.assertFalse((self.runner.run_dir / 'revisions/001/study.json').exists())
                    self.assertFalse((self.runner.run_dir / 'revisions/001/study.material.json').exists())
                    self.assertFalse(any(c['context']['stage'] == 'review' for c in self.calls))
                    if kind in ('claims', 'evidence', 'coverage', 'unexpected', 'source_snapshot_id', 'source_directory', 'task'):
                        attempt = Path(manifest['synthesis_invocation']['artifact_directory'])
                        self.assertTrue((attempt / 'extracted.json').exists())
                        self.assertFalse((attempt / 'expanded.json').exists())

    def test_synthesis_reuses_resolved_evidence_and_review_consumes_global_study(self):
        from src.analysis.ledger import resolve_evidence
        with patch('src.analysis.ledger.resolve_evidence', wraps=resolve_evidence) as resolver:
            manifest, code = self.run_case(review=True)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual(resolver.call_count, 4)  # Three source shards and one review; no synthesis read.
        self.assertEqual(self.calls[-1]['context']['stage'], 'review')
        self.assertEqual(len(self.calls[-1]['context']['claim_registry']), 9)
        self.assertTrue(manifest['accepted'])
        self.assertEqual(manifest['review']['claim_registry'], manifest['study']['claims'])

    def test_revision_runs_normally_after_synthesis_without_rerunning_shards(self):
        def change(runner, context, data):
            if context.get('prompt_variant') == 'revise':
                previous = context['previous_revision']['study']
                data['claims'] = [{k: v for k, v in c.items() if k != 'document_locators'} for c in previous['claims']]
                data['evidence'], data['coverage'] = copy.deepcopy(previous['evidence']), copy.deepcopy(previous['coverage'])
                data['report_sections'][0]['blocks'][0]['claim_ids'] = [c['id'] for c in data['claims']]
            initial_finding(context['stage'], context, data)
        manifest, code = self.run_case(review=True, change=change)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual(manifest['selected_revision'], '002')
        self.assertEqual([c['context'].get('prompt_variant', c['context']['stage']) for c in self.calls],
                         ['catalog', 'study-shard', 'study-shard', 'study-shard', 'synthesis', 'review', 'revise', 'review'])

    def test_file_and_byte_thresholds_split_one_subsystem_and_merge_coverage(self):
        for threshold in ('files', 'bytes'):
            for backend in ('codex', 'claude-code', 'opencode', 'xxx'):
                setting = (patch('src.analysis.analysis_plan.MAX_SOURCE_FILES_PER_SESSION', 3) if threshold == 'files'
                           else patch.dict(self.value, max_source_bytes_per_session=66))
                with self.subTest(threshold=threshold, backend=backend), \
                        setting, \
                        patch('explain.validate_result', wraps=validate_result) as validator:
                    manifest, code = self.run_case(catalog_paths=['.'], backend=backend)
                self.assertEqual(code, 0, manifest.get('diagnostics'))
                self.assertEqual(manifest['required_sessions'], 3)
                plan, states = manifest['analysis_plan'], manifest['study_shards']
                self.assertTrue(all(not s['over_capacity'] and s['source_files'] == 3 for s in plan['shards']))
                self.assertEqual(plan['thresholds']['max_source_bytes_per_session'], 66 if threshold == 'bytes' else 262144)
                self.assertEqual(plan['totals']['source_bytes'], 198)
                self.assertTrue(all(s['source_bytes'] == 66 for s in plan['shards']))
                self.assertEqual([s['subsystem_ids'] for s in plan['shards']], [['S-001']] * 3)
                self.assertEqual({p for s in plan['shards'] for p in s['primary_file_paths']},
                                 {'app.py'} | {f'part{i}.py' for i in range(2, 10)})
                self.assertEqual([s['id'] for s in states], ['R-001', 'R-002', 'R-003'])
                self.assertEqual([c['context']['stage'] for c in self.calls],
                                 ['catalog', 'study-shard', 'study-shard', 'study-shard', 'study'])
                validated = [c.args[1]['shard_id'] for c in validator.call_args_list if c.args[0] == 'study-shard']
                self.assertEqual(validated, ['R-001', 'R-002', 'R-003'])
                self.assertEqual(manifest['metrics']['attempts'], 5)
                rows = [s for s in manifest['metrics']['stages'] if s['stage'] == 'study-shard']
                self.assertEqual([s['attempts'] for s in rows], [1, 1, 1])
                for planned, state in zip(plan['shards'], states):
                    data, meta = state['study-shard'], state['study-shard_invocation']
                    self.assertEqual((state['status'], meta['status']), ('SUCCEEDED', 'SUCCEEDED'))
                    self.assertNotIn('generated_by', meta)
                    self.assertTrue(meta['local_validation'] and meta['publication_complete'] and meta['backend_result_valid'])
                    self.assertIn(data['evidence'][0]['path'], planned['primary_file_paths'])
                    self.assertFalse(data['program_checks']['coverage']['completeness_measured'])
                    validate_schema(data, SAVED_FOLDER_SCHEMAS['study-shard'])
                inputs = synthesis_inputs(plan, states)
                self.assertEqual([s['shard_id'] for s in inputs['validated_shards']], ['R-001', 'R-002', 'R-003'])
                self.assertEqual(inputs['synthesis_coverage'], [dict(area_id='S-001', status='INSPECTED',
                    evidence_ids=['study:E-001', 'study:E-002', 'study:E-003'], limitation='')])
                self.assertEqual(manifest['study']['coverage'], inputs['synthesis_coverage'])
                self.assertEqual(len(inputs['synthesis_evidence']), 3)
                self.assertEqual(len(inputs['synthesis_claims']), 3)
                for state in states:
                    path = state['directory'] + '/study-shard.json'
                    blob = (self.runner.run_dir / path).read_bytes()
                    self.assertEqual(inputs['_shard_artifact_hashes'][path], {'sha256': sha(blob), 'bytes': len(blob)})

    def test_synthesis_requires_valid_local_provenance_and_normal_backend_validation(self):
        with patch('src.analysis.analysis_plan.MAX_SOURCE_FILES_PER_SESSION', 3):
            manifest, code = self.run_case(catalog_paths=['.'])
        self.assertEqual(code, 0)
        # Retain validation coverage for locally completed empty assignments even
        # though the planner no longer produces them alongside file workloads.
        plan = copy.deepcopy(manifest['analysis_plan'])
        empty = dict(id='R-004', subsystem_ids=[], primary_file_paths=[])
        plan['shards'].append(empty)
        data = prepare_result('study-shard', empty_shard_result(empty, manifest),
                              manifest | {'analysis_shard': empty})
        meta = dict(generated_by='orchestrator', reason='empty_subsystem_assignment',
                    source_integrity_verified=True, local_validation=True, publication_complete=True, artifact_hashes={})
        original_states = manifest['study_shards'] + [dict(id='R-004', status='SUCCEEDED', directory='study-shards/R-004',
                                                         **{'study-shard': data, 'study-shard_invocation': meta})]
        synthesis_inputs(plan, original_states)
        for kind in ('marker', 'reason', 'source', 'validation', 'publication', 'policy', 'assignment',
                     'id', 'completion', *OBSERVATION_FIELDS, 'evidence', 'claims', 'coverage', 'limitations',
                     'model_backend', 'model_as_local', 'order', 'missing'):
            states = copy.deepcopy(original_states)
            data, meta = states[-1]['study-shard'], states[-1]['study-shard_invocation']
            if kind == 'marker': meta.pop('generated_by')
            elif kind == 'reason': meta['reason'] = 'another'
            elif kind in ('source', 'validation', 'publication'):
                meta[{'source': 'source_integrity_verified', 'validation': 'local_validation', 'publication': 'publication_complete'}[kind]] = False
            elif kind == 'policy': data['program_checks']['policy_satisfied'] = False
            elif kind == 'assignment': data['assigned_subsystem_ids'] = ['S-001']
            elif kind == 'id': data['shard_id'] = 'R-999'
            elif kind == 'completion': data['completion_status'] = 'PARTIAL'
            elif kind == 'model_backend': states[0]['study-shard_invocation']['backend_result_valid'] = False
            elif kind == 'model_as_local': states[0]['study-shard_invocation'] = copy.deepcopy(meta)
            elif kind == 'order': states.reverse()
            elif kind == 'missing': states.pop()
            else: data[kind] = ['unexpected content']
            with self.subTest(kind=kind), self.assertRaises(ContractError):
                synthesis_inputs(plan, states)

    def test_incomplete_split_scope_and_dependency_only_evidence_block_synthesis(self):
        for kind in ('partial', 'dependency_only'):
            for policy in ('strict', 'compromise'):
                def change(runner, context, data):
                    if context.get('analysis_shard', {}).get('id') != 'R-002':
                        return
                    if kind == 'partial':
                        data['coverage'][0].update(status='PARTIALLY_INSPECTED', limitation='Only one assigned file was read.')
                    else:
                        # The source evidence is valid and belongs to the same
                        # catalog area, but it is outside this batch's scope.
                        data['evidence'][0]['path'] = 'app.py'
                with self.subTest(kind=kind, policy=policy), patch('src.analysis.analysis_plan.MAX_SOURCE_FILES_PER_SESSION', 3):
                    manifest, code = self.run_case(catalog_paths=['.'], change=change, policy=policy)
                self.assertEqual(code, 2 if policy == 'compromise' else 1)
                self.assertEqual(manifest['synthesis_status'], 'SKIPPED')
                self.assertFalse(workflow_satisfied(manifest))
                self.assertEqual([s['status'] for s in manifest['study_shards']], ['SUCCEEDED', 'FAILED', 'SUCCEEDED'])
                error = manifest['study_shards'][1]['study-shard_invocation']['error']['details']['coverage']
                self.assertEqual(error['unfinished_ids' if kind == 'partial' else 'unsupported_ids'], ['S-001'])
                self.assertFalse(error['completeness_measured'])

    def test_dependency_evidence_outside_primary_scope_remains_valid_for_claims(self):
        def change(runner, context, data):
            if context.get('analysis_shard', {}).get('id') != 'R-002':
                return
            data['evidence'].append(dict(id='E-002', source_id='source-001', path='app.py', start_line=1, end_line=1, quote=''))
            data['claims'][0]['evidence_ids'].append('study:E-002')
            data['relationships'].append(dict(subsystem_id='S-001', related_path='app.py',
                description='Dependency outside the primary scope.', claim_ids=['C-001']))
        with patch('src.analysis.analysis_plan.MAX_SOURCE_FILES_PER_SESSION', 3):
            manifest, code = self.run_case(catalog_paths=['.'], change=change)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual(manifest['synthesis_status'], 'SUCCEEDED')
        inputs = synthesis_inputs(manifest['analysis_plan'], manifest['study_shards'])
        self.assertEqual(inputs['validated_shards'][1]['relationships'][0]['related_path'], 'app.py')
        self.assertEqual(inputs['synthesis_claims'][1]['evidence_ids'], ['study:E-002', 'study:E-003'])

    def test_split_shard_source_and_plan_guards_prevent_publication(self):
        for kind in ('source', 'coverage_plan', 'analysis_plan', 'previous_shard'):
            def change(stage, data, context, *args, **kwargs):
                result = prepare_result(stage, data, context, *args, **kwargs)
                if context.get('analysis_shard', {}).get('id') == 'R-002':
                    directory = Path(context['_analysis_plan_path']).parent
                    path = {'source': self.source / 'app.py', 'coverage_plan': directory / 'coverage.plan.json',
                            'analysis_plan': directory / 'analysis.plan.json',
                            'previous_shard': directory / 'study-shards/R-001/study-shard.json'}[kind]
                    path.write_bytes(path.read_bytes() + b'changed')
                return result
            with self.subTest(kind=kind), patch('src.analysis.analysis_plan.MAX_SOURCE_FILES_PER_SESSION', 3), \
                    patch('explain.prepare_result', side_effect=change):
                manifest, code = self.run_case(catalog_paths=['.'])
            self.assertEqual((code, manifest['critical_failure']), (1, True))
            self.assertEqual([s['status'] for s in manifest['study_shards']], ['SUCCEEDED', 'FAILED', 'PLANNED'])
            if kind != 'previous_shard':
                self.assertFalse((self.runner.run_dir / 'study-shards/R-002/study-shard.json').exists())
            self.assertFalse((self.runner.run_dir / 'ARCHITECTURE.md').exists())
            self.assertEqual([c['context']['stage'] for c in self.calls], ['catalog', 'study-shard', 'study-shard'])

    def test_split_shard_artifact_mutation_blocks_synthesis_publication(self):
        def change(runner, context, data):
            if context.get('prompt_variant') == 'synthesis':
                path = runner.run_dir / 'study-shards/R-002/study-shard.json'
                path.write_bytes(path.read_bytes() + b'changed')
        with patch('src.analysis.analysis_plan.MAX_SOURCE_FILES_PER_SESSION', 3):
            manifest, code = self.run_case(catalog_paths=['.'], change=change)
        self.assertEqual((code, manifest['critical_failure']), (1, True))
        self.assertFalse((self.runner.run_dir / 'revisions/001/study.json').exists())

    def test_assigned_subsystem_without_regular_files_still_invokes_model(self):
        (self.source / 'link.py').symlink_to('app.py')
        with patch('src.analysis.analysis_plan.MAX_SOURCE_FILES_PER_SESSION', 3):
            manifest, code = self.run_case(catalog_paths=['link.py'])
        shard = manifest['analysis_plan']['shards'][0]
        self.assertEqual((shard['subsystem_ids'], shard['source_files']), (['S-001', 'UNCLASSIFIED'], 3))
        self.assertEqual([c['context']['stage'] for c in self.calls], ['catalog'] + ['study-shard'] * 3)
        self.assertNotEqual(code, 0)  # The fixture cannot resolve evidence through a symlink.
        self.assertNotIn('generated_by', manifest['study_shards'][0]['study-shard_invocation'])

    def test_unassigned_sources_preserve_coverage_and_empty_study_policy(self):
        for excluded in (False, True):
            for policy in ('strict', 'compromise'):
                def change(runner, context, data):
                    if context['stage'] == 'catalog':
                        data['subsystems'] = []
                        data['exclusions'] = [dict(path='.', reason='Excluded fixture.')] if excluded else []
                    elif excluded and context['stage'] == 'study':
                        data.update(claims=[], evidence=[])
                        for section in data['report_sections']:
                            for block in section['blocks']:
                                block.update(markdown='All source files were excluded.', claim_ids=[])
                with self.subTest(excluded=excluded, policy=policy), \
                        patch('src.analysis.analysis_plan.MAX_SOURCE_FILES_PER_SESSION', 3):
                    manifest, code = self.run_case(change=change, policy=policy)
                self.assertEqual([c['context']['stage'] for c in self.calls],
                                 ['catalog'] + ([] if excluded else ['study-shard'] * 3) + ['study'])
                self.assertEqual([s['status'] for s in manifest['study_shards']], [] if excluded else ['SUCCEEDED'] * 3)
                self.assertEqual([s['subsystem_ids'] for s in manifest['analysis_plan']['shards']],
                                 [[]] if excluded else [['UNCLASSIFIED']] * 3)
                self.assertEqual(manifest['metrics']['attempts'], 2 if excluded else 5)
                self.assertEqual(manifest.get('synthesis_status'), None if excluded else 'SUCCEEDED')
                if excluded:
                    self.assertEqual(manifest['source_metrics'], dict(subsystems=0, source_files=0, source_bytes=0))
                    self.assertEqual(manifest['study_origin'], 'direct_single_session')
                self.assertEqual((code, manifest['status']), (2, 'PARTIAL'))
                self.assertFalse(workflow_satisfied(manifest))
                self.assertFalse(manifest['study']['program_checks']['policy_satisfied'])
                self.assertEqual(len(manifest['study']['claims']), 0 if excluded else 3)
                self.assertEqual(len(manifest['study']['evidence']), 0 if excluded else 3)
                self.assertEqual([a['area_id'] for a in manifest['study']['coverage']], [] if excluded else ['UNCLASSIFIED'])


class MultiSessionGitTests(unittest.TestCase):
    setUp = git_fixtures.RepoFixture.setUp
    tearDown = git_fixtures.RepoFixture.tearDown
    git = git_fixtures.RepoFixture.git
    config = git_fixtures.RepoFixture.config

    def test_git_split_shards_preserve_identity_and_source_guards(self):
        for name in ('part2.py', 'part3.py'):
            (self.repo_path / name).write_text('pass\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Add source files for shard thresholds')
        for kind in ('mixed', 'all_empty', 'source_changed'):
            cfg = self.config()
            cfg['git_mode']['branches'] = ['master']
            cfg['execution']['review_enabled'] = False
            cfg['max_source_bytes_per_session'] = 5
            runner = Runner(cfg, self.base / kind)
            calls = []
            def process(command, cwd, env, payload, **kwargs):
                context = prompt_context(payload)
                calls.append(context)
                data = response(context)
                if kind == 'all_empty' and context['stage'] == 'catalog':
                    data.update(subsystems=[], exclusions=[dict(path='.', reason='Excluded fixture.')])
                elif kind == 'all_empty' and context['stage'] == 'study':
                    data.update(claims=[], evidence=[])
                    for section in data['report_sections']:
                        for block in section['blocks']:
                            block.update(markdown='All source files were excluded.', claim_ids=[])
                return cli_result(command, data)
            def change(stage, data, context, *args, **kwargs):
                result = prepare_result(stage, data, context, *args, **kwargs)
                if kind == 'source_changed' and context.get('analysis_shard', {}).get('id') == 'R-002':
                    (Path(context['repository']) / 'app.py').write_text('changed\n')
                return result
            with self.subTest(kind=kind), \
                    patch.dict(os.environ, {'AUDIT_TEST_SUBSYSTEM_PATHS': json.dumps(['.'])}), \
                    patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process), \
                    patch('explain.prepare_result', side_effect=change):
                manifest, code = runner.run()
            branch = manifest['branches'][0]
            self.assertEqual(branch['analysis_plan']['thresholds']['max_source_bytes_per_session'], 5)
            states = branch['study_shards']
            if kind == 'all_empty':
                self.assertEqual(states, [])
                self.assertEqual(branch['required_sessions'], 1)
                self.assertEqual([c['stage'] for c in calls], ['catalog', 'study'])
                self.assertEqual(branch['study_origin'], 'direct_single_session')
                self.assertEqual(code, 2, manifest.get('diagnostics'))
                validate_schema(branch['study'], SAVED_SCHEMAS['study'])
                self.assertEqual((branch['study']['branch'], branch['study']['source_commit']),
                                 (branch['branch'], branch['source_commit']))
                continue
            self.assertEqual([s['id'] for s in states], ['R-001', 'R-002', 'R-003'])
            self.assertEqual(sum(c['stage'] == 'study-shard' for c in calls), 2 if kind == 'source_changed' else 3)
            if kind == 'source_changed':
                self.assertEqual((code, manifest['critical_failure']), (1, True))
                self.assertEqual([s['status'] for s in states], ['SUCCEEDED', 'FAILED', 'PLANNED'])
                self.assertFalse((runner.run_dir / states[1]['directory'] / 'study-shard.json').exists())
                continue
            self.assertEqual(code, 0, manifest.get('diagnostics'))
            self.assertEqual([s['status'] for s in states], ['SUCCEEDED'] * 3)
            for state in states[1:]:
                data = state['study-shard']
                validate_schema(data, SAVED_SCHEMAS['study-shard'])
                self.assertEqual((data['branch'], data['source_commit']), (branch['branch'], branch['source_commit']))
                self.assertNotIn('generated_by', state['study-shard_invocation'])
            synthesis_inputs(branch['analysis_plan'], states)

    def test_git_snapshots_synthesize_review_and_compare_without_cross_source_plan_checks(self):
        cfg = self.config()
        cfg['git_mode']['branches'] = ['master', 'test01']
        runner = Runner(cfg, self.base / 'multi-git')
        calls = []
        def process(command, cwd, env, payload, **kwargs):
            context = prompt_context(payload)
            calls.append(context)
            return cli_result(command, response(context))
        with patch.dict(os.environ, {'AUDIT_TEST_SUBSYSTEM_PATHS': json.dumps(['.'] * 9)}), \
                patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process):
            manifest, code = runner.run()
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertTrue(manifest['comparison']['program_checks']['policy_satisfied'])
        self.assertEqual(len(calls), 13)
        self.assertEqual(sum(c.get('prompt_variant') == 'synthesis' for c in calls), 2)
        for branch in manifest['branches']:
            self.assertEqual(branch['required_sessions'], 3)
            self.assertTrue(branch['accepted'])
            inventory = json.loads((runner.run_dir / branch['directory'] / 'source.inventory.json').read_text())
            self.assertEqual([e['source_lines'] for e in inventory['entries'] if e['type'] == 'file'], [1])
            context = next(c for c in calls if c.get('prompt_variant') == 'synthesis' and c['branch'] == branch['branch'])
            inputs = synthesis_inputs(branch['analysis_plan'], branch['study_shards'])
            self.assertNotIn('shard_id_mappings', context)
            for shard, full in zip(context['validated_shards'], inputs['validated_shards']):
                self.assertEqual(shard, {k: v for k, v in full.items()
                                         if k not in ('evidence', 'claims', 'coverage', 'normalization_provenance')})
            for field in ('evidence', 'claims', 'coverage'):
                self.assertEqual(context['synthesis_' + field], inputs['synthesis_' + field])
        self.assertEqual(self.repo.symbolic(), 'master')
        self.assertEqual(self.repo.head(), self.master)
