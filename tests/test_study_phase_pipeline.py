# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Initial evidence collection and document writing share one guarded Study."""
import copy
import json
from unittest.mock import patch

from fixtures.cli_response import cli_result
from fixtures.ledger_response import prompt_context, response
from fixtures.study_runner import run_case
from src.runtime.errors import AuditError
from src.runtime.execution import Budget
from src.runtime.runner import Runner
from test_folder import FolderFixture


class StudyPhasePipelineTests(FolderFixture):
    run_case = run_case

    def phases(self):
        return [call['context'].get('phase') for call in self.calls]

    def extracted(self):
        return [json.loads(path.read_text()) for path in self.runner.session.run_dir.rglob('extracted.json')]

    def test_initial_phases_share_agent_and_budget_and_isolate_writer(self):
        manifest, code = self.run_case(large=False)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual(self.phases(), ['collect', 'write'])
        collect, write = self.calls
        self.assertEqual([call['context']['stage'] for call in self.calls], ['study', 'study'])
        self.assertIs(collect['budget'], write['budget'])
        self.assertEqual(collect['cwd'], self.source)
        self.assertNotEqual(write['cwd'], self.source)
        self.assertIn('source_context', collect['context'])
        self.assertNotIn('source_context', write['context'])
        self.assertNotIn('features.shell_tool=false', collect['command'])
        self.assertIn('features.shell_tool=false', write['command'])
        for call in self.calls:
            command = call['command']
            self.assertEqual(command[command.index('--model') + 1], 'fixture/study-model')
        schemas = [json.loads(call['prompt'].split('# Required final JSON Schema\n', 1)[1])
                   for call in self.calls]
        self.assertNotIn('report_sections', schemas[0]['properties'])
        self.assertNotIn('claims', schemas[1]['properties'])
        self.assertNotIn('evidence', schemas[1]['properties'])
        package = write['context']['evidence_package']
        self.assertEqual(manifest['study']['evidence'], package['study']['evidence'])
        self.assertEqual([{key: value for key, value in claim.items() if key != 'document_locators'}
                          for claim in manifest['study']['claims']], package['study']['claims'])
        metrics = next(row for row in manifest['metrics']['stages'] if row['stage'] == 'study')
        self.assertEqual(metrics['attempts'], 2)
        raw = self.extracted()
        self.assertEqual(len(raw), 2)
        self.assertEqual(sum('evidence' in value and 'report_sections' not in value for value in raw), 1)
        self.assertEqual(sum('report_sections' in value and 'claims' not in value for value in raw), 1)
        package_path = self.runner.session.run_dir / 'revisions' / '001' / 'study.evidence.json'
        saved = json.loads(package_path.read_text())
        for key in ('claims', 'evidence', 'completion_status', 'limitations'):
            self.assertEqual(saved['study'][key], package['study'][key])
        for expected, received in zip(saved['evidence_checks'], package['evidence_checks'], strict=True):
            for key in ('id', 'source_id', 'path', 'start_line', 'end_line', 'status'):
                self.assertEqual(expected[key], received[key])
        attempts = sorted((package_path.parent / 'study.logs').glob('attempt-*/invocation.json'))
        self.assertEqual([json.loads(path.read_text())['phase'] for path in attempts], ['collect', 'write'])

    def test_unresolved_locators_force_partial_in_both_policies(self):
        cases = ({'quote': 'missing quotation'}, {'end_line': 99}, {'path': 'missing.py'})
        for policy in ('strict', 'compromise'):
            for change in cases:
                with self.subTest(policy=policy, change=change):
                    def invalidate(runner, context, data):
                        if context.get('phase') == 'collect':
                            data['evidence'][0].update(change)

                    manifest, code = self.run_case(large=False, policy=policy, change=invalidate)
                    self.assertEqual(self.phases(), ['collect', 'write'])
                    self.assertEqual((manifest['status'], code), ('PARTIAL', 2), manifest.get('diagnostics'))
                    self.assertEqual(manifest['study']['completion_status'], 'PARTIAL')
                    self.assertTrue(manifest['study']['limitations'])
                    self.assertFalse(manifest['study']['program_checks']['policy_satisfied'])
                    self.assertFalse(manifest['processing_completed'])
                    package = self.calls[-1]['context']['evidence_package']
                    self.assertTrue(any(item['status'] != 'RESOLVED' for item in package['evidence_checks']))
                    self.assertEqual(manifest['study']['evidence'], package['study']['evidence'])

    def test_collection_failure_blocks_writer_without_recovered_document(self):
        for policy in ('strict', 'compromise'):
            with self.subTest(policy=policy):
                def invalidate(runner, context, data):
                    if context.get('phase') == 'collect':
                        del data['claims']

                manifest, code = self.run_case(large=False, policy=policy, change=invalidate)
                self.assertEqual(self.phases(), ['collect'])
                self.assertEqual((manifest['status'], code), ('FAILED', 1))
                self.assertIsNone(manifest['study'])
                self.assertNotIn('study_material', manifest['revisions'][0])
                self.assertFalse((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())

    def test_decoding_failure_without_source_context_keeps_initial_study_partial(self):
        (self.source / 'app.py').write_bytes(b'\xff')
        self.value['source_context'] = {'enabled': False}
        for policy in ('strict', 'compromise'):
            with self.subTest(policy=policy):
                manifest, code = self.run_case(large=False, policy=policy)
                self.assertEqual(self.phases(), ['collect', 'write'])
                self.assertNotIn('source_context', self.calls[0]['context'])
                self.assertEqual((manifest['status'], code), ('PARTIAL', 2))
                package = self.calls[-1]['context']['evidence_package']
                self.assertEqual(package['evidence_checks'][0]['status'], 'DECODE_ERROR')
                study = manifest['study']
                self.assertEqual(study['completion_status'], 'PARTIAL')
                self.assertIn('Evidence study:E-001: DECODE_ERROR.', study['limitations'])
                self.assertEqual(study['program_checks']['evidence_counts'], {'DECODE_ERROR': 1})
                self.assertFalse(study['program_checks']['policy_satisfied'])
                self.assertFalse(manifest['processing_completed'])
                self.assertEqual((self.source / 'app.py').read_bytes(), b'\xff')

    def test_observed_writer_tool_call_blocks_publication_and_recovery(self):
        for guard in (False, True):
            with self.subTest(read_loop_guard=guard):
                self.value.update(result_policy='compromise',
                    execution={'review_enabled': False, 'read_loop_guard': guard})
                runner = Runner(self.config(), self.base / ('writer-tool-' + str(guard)))
                phases = []

                def process(command, cwd, env, payload, *, observer=None, **kwargs):
                    context = prompt_context(payload)
                    phases.append(context['phase'])
                    if context['phase'] == 'write':
                        event = {'type': 'item.completed', 'item': {
                            'id': 'writer-read', 'type': 'command_execution', 'command': 'cat app.py',
                            'aggregated_output': 'Source content.', 'exit_code': 0, 'status': 'completed'}}
                        observer.feed((json.dumps(event) + '\n').encode())
                    return cli_result(command, response(context))

                with patch.object(runner, 'check_cli', return_value={}), \
                        patch('src.runtime.process.process', side_effect=process):
                    manifest, code = runner.run()
                self.assertEqual(phases, ['collect', 'write'])
                self.assertEqual((manifest['status'], code), ('FAILED', 1))
                revision = manifest['revisions'][0]
                meta = revision['study_invocation']
                self.assertEqual(meta['error']['code'], 'WRITER_TOOL_CALL')
                self.assertEqual(meta['tool_observation']['calls'], 1)
                self.assertFalse(meta['publication_complete'])
                self.assertIsNone(revision['study'])
                self.assertNotIn('study_material', revision)
                self.assertTrue((runner.session.run_dir / 'revisions/001/study.evidence.json').exists())
                self.assertFalse((runner.session.run_dir / 'ARCHITECTURE.md').exists())
                self.assertFalse((runner.session.run_dir / 'revisions/001/ARCHITECTURE.md').exists())

    def test_citation_repair_precedes_freeze_and_preserves_raw_collection(self):
        original_quote = '    def main(): return 42\n'

        def indent(runner, context, data):
            if context.get('phase') == 'collect':
                data['evidence'][0]['quote'] = original_quote

        manifest, code = self.run_case(large=False, policy='compromise', change=indent)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        package = self.calls[-1]['context']['evidence_package']
        self.assertEqual(package['study']['evidence'][0]['quote'], 'def main(): return 42\n')
        self.assertTrue(package['citation_corrections'])
        raw_collection = next(value for value in self.extracted() if 'evidence' in value)
        self.assertEqual(raw_collection['evidence'][0]['quote'], original_quote)
        self.assertEqual(manifest['study']['evidence'], package['study']['evidence'])

    def test_invalid_writer_recovers_only_authored_blocks(self):
        authored = {}

        def omit_diagram(runner, context, data):
            if context.get('phase') == 'write':
                data['report_sections'][2]['blocks'] = data['report_sections'][2]['blocks'][:1]
                authored.update(copy.deepcopy(data))

        manifest, code = self.run_case(large=False, policy='compromise', change=omit_diagram)
        self.assertEqual(self.phases(), ['collect', 'write'])
        self.assertEqual((manifest['status'], code), ('PARTIAL', 2), manifest.get('diagnostics'))
        revision = manifest['revisions'][0]
        self.assertIsNone(revision['study'])
        self.assertFalse(revision['study_material']['strict_valid'])
        self.assertNotIn('claims', revision['study_material'])
        self.assertNotIn('evidence', revision['study_material'])
        self.assertIn(authored, self.extracted())
        self.assertFalse((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())

    def test_total_timeout_applies_across_both_phases(self):
        state = {'now': 0}
        self.value['execution'] = {'stage_timeout_seconds': 10}

        def advance(runner, context, data):
            state['now'] = 9 if context.get('phase') == 'collect' else 11

        def budget(seconds, idle):
            return Budget(seconds, idle, clock=lambda: state['now'])

        with patch('src.model.invocation.Budget', side_effect=budget):
            manifest, code = self.run_case(large=False, change=advance)
        self.assertEqual(self.phases(), ['collect', 'write'])
        self.assertEqual((manifest['status'], code), ('FAILED', 1))
        self.assertTrue(any(item.get('failure_kind') == 'STAGE_TIMEOUT'
                            for item in manifest['diagnostics']))
        self.assertFalse((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())

    def test_source_change_during_collection_is_fatal(self):
        def mutate(runner, context, data):
            if context.get('phase') == 'collect':
                (self.source / 'app.py').write_text('changed source\n')

        manifest, code = self.run_case(large=False, policy='compromise', change=mutate)
        self.assertEqual(self.phases(), ['collect'])
        self.assertEqual((manifest['status'], code), ('FAILED', 1))
        self.assertTrue(manifest['critical_failure'])
        self.assertFalse((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())

    def test_frozen_package_tampering_is_fatal(self):
        def tamper(runner, context, data):
            if context.get('phase') == 'write':
                package = runner.session.run_dir / 'revisions' / '001' / 'study.evidence.json'
                package.write_bytes(package.read_bytes() + b'\n')

        manifest, code = self.run_case(large=False, policy='compromise', change=tamper)
        self.assertEqual(self.phases(), ['collect', 'write'])
        self.assertEqual((manifest['status'], code), ('FAILED', 1))
        self.assertTrue(manifest['critical_failure'])
        self.assertFalse((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())

    def test_repeated_collection_cannot_replace_published_revision_package(self):
        manifest, code = self.run_case(large=False)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        directory = self.runner.session.run_dir / 'revisions/001'
        names = ('study.evidence.json', 'ARCHITECTURE.md', 'study.json', 'claim.registry.json', 'review.plan.json')
        original = {name: (directory / name).read_bytes() for name in names}
        phases = []

        def process(command, cwd, env, payload, **kwargs):
            context = prompt_context(payload)
            phases.append(context['phase'])
            data = response(context)
            if context['phase'] == 'collect':
                data['claims'][0]['statement'] = 'A different collected claim.'
            return cli_result(command, data)

        context = {'source_directory': str(self.source), 'source_fingerprint': manifest['source_fingerprint'],
                   'revision_id': '001'}
        with patch('src.runtime.process.process', side_effect=process), self.assertRaises(AuditError) as caught:
            self.runner.invoke('study', context, directory / 'study.logs')
        self.assertEqual(caught.exception.code, 'MODEL_BINDING_CHANGED')
        self.assertEqual(caught.exception.failure_layer, 'integrity')
        self.assertTrue(self.runner.session.critical_failure)
        self.assertEqual(phases, ['collect'])
        self.assertEqual({name: (directory / name).read_bytes() for name in names}, original)
        self.assertEqual((self.runner.session.run_dir / 'ARCHITECTURE.md').read_bytes(), original['ARCHITECTURE.md'])

    def test_revision_retains_full_contract_and_source_access(self):
        def finding(runner, context, data):
            if context['stage'] == 'review' and context['revision_id'] == '001':
                data['claims'][0]['outcome'] = 'CONTRADICTED'
                data['findings'] = [dict(id='F-001', severity='HIGH', type='FACTUAL_ERROR',
                    claim_ids=['C-001'], location='Entry point description.', evidence_ids=['review:E-001'],
                    impact='The reported flow is incorrect.', proposed_correction='Correct the flow.')]

        manifest, code = self.run_case(large=False, review=True, change=finding)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual([(call['context']['stage'], call['context'].get('phase')) for call in self.calls],
                         [('study', 'collect'), ('study', 'write'), ('review', None),
                          ('study', None), ('review', None)])
        revise = self.calls[3]
        self.assertEqual(revise['context']['prompt_variant'], 'revise')
        self.assertIn('source_context', revise['context'])
        self.assertNotIn('evidence_package', revise['context'])
        self.assertEqual(revise['cwd'], self.source)
        self.assertNotIn('features.shell_tool=false', revise['command'])
        schema = json.loads(revise['prompt'].split('# Required final JSON Schema\n', 1)[1])
        self.assertTrue({'report_sections', 'claims', 'evidence'} <= set(schema['properties']))
        self.assertEqual(manifest['selected_revision'], '002')
