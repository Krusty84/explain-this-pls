# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Syntax validation and bounded, tool-free corrections through the model boundary."""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fixtures.ledger_response import response
from fixtures.study_runner import run_case
from src.analysis.evidence import canonical, sha
from src.contracts.contracts import ContractError
from src.reports.plantuml import PlantUML, VALID_SOURCE, INVALID_SOURCE, diagrams, replace_diagrams
from src.runtime.errors import AuditError
from src.runtime.execution import Budget
from src.runtime.reporting import NullReporter
from src.runtime.tool_observer import ToolObserver
from test_folder import FolderFixture
import test_revision_pipeline as revisions

REAL_PLANTUML = shutil.which('plantuml')
BAD = '```plantuml\n@startuml\ncomponent "Приложение" aass App\n@enduml\n```'


def break_diagram(runner, context, data):
    if context.get('phase') == 'write':
        data['report_sections'][2]['blocks'][-1]['markdown'] = BAD


class PlantUMLProcessTests(unittest.TestCase):
    def setUp(self):
        self.checker = PlantUML(Path.cwd(), NullReporter())

    def test_missing_installation_prevents_agent_preflight(self):
        from src.runtime.runner import Runner
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'source'
            source.mkdir()
            runner = Runner(dict(mode='folder', folder_mode={'path': str(source)}), Path(raw) / 'reports')
            with patch('src.reports.plantuml.shutil.which', return_value=None), \
                    patch('src.reports.plantuml.sys.executable', str(Path(raw) / 'python')), \
                    patch('src.runtime.runner.check_cli') as agent_check, self.assertRaises(AuditError) as caught:
                runner.check_cli()
            self.assertEqual(caught.exception.code, 'PLANTUML_UNAVAILABLE')
            agent_check.assert_not_called()

    def test_local_launcher_is_found_without_virtual_environment_activation(self):
        with tempfile.TemporaryDirectory() as raw:
            python = Path(raw) / 'bin' / 'python'
            python.parent.mkdir()
            launcher = python.with_name('plantuml')
            launcher.write_text('fixture launcher')
            with patch('src.reports.plantuml.shutil.which', return_value=None), \
                    patch('src.reports.plantuml.sys.executable', str(python)), \
                    patch('src.reports.plantuml.process', return_value=dict(
                        returncode=0, stdout=b'SEQUENCE\n(2 participants)\n', stderr=b'')) as process:
                self.assertTrue(self.checker.check(VALID_SOURCE, Budget(10))['valid'])
            self.assertEqual(process.call_args.args[0][0], str(launcher))

    def test_installed_launcher_takes_precedence_over_a_global_plantuml(self):
        with tempfile.TemporaryDirectory() as raw:
            python = Path(raw) / 'python'
            launcher = python.with_name('plantuml')
            launcher.write_text('fixture launcher')
            with patch('src.reports.plantuml.shutil.which', return_value='/old/plantuml') as which, \
                    patch('src.reports.plantuml.sys.executable', str(python)), \
                    patch('src.reports.plantuml.process', return_value=dict(
                        returncode=0, stdout=b'SEQUENCE\n(2 participants)\n', stderr=b'')):
                self.checker.check(VALID_SOURCE, Budget(10))
            self.assertEqual(self.checker.executable, str(launcher))
            which.assert_not_called()

    def test_error_protocol_is_checked_even_with_zero_exit_code(self):
        result = dict(returncode=0, stdout=b'ERROR\n1\nSyntax Error?\n', stderr=b'')
        with patch('src.reports.plantuml.process', return_value=result) as process:
            self.checker.executable = '/configured/plantuml'
            checked = self.checker.check(INVALID_SOURCE, Budget(10))
        self.assertFalse(checked['valid'])
        self.assertEqual(checked['line'], 2)
        call = process.call_args
        self.assertEqual(call.args[0], ['/configured/plantuml', '-charset', 'UTF-8', '-syntax'])
        self.assertEqual(call.args[2]['PLANTUML_SECURITY_PROFILE'], 'SANDBOX')
        self.assertEqual(call.args[3], INVALID_SOURCE.encode('utf-8'))

    def test_unknown_output_and_process_failures_are_not_syntax_errors(self):
        for stdout, code in ((b'', 0), (b'JVM failed', 1), (b'ERROR\nx\nError', 0),
                             (b'UNRECOGNIZED\n', 0), (b'DESCRIPTION\n', 1)):
            with self.subTest(stdout=stdout), patch('src.reports.plantuml.process', return_value=
                    dict(returncode=code, stdout=stdout, stderr=b'')), self.assertRaises(AuditError) as caught:
                self.checker.executable = '/configured/plantuml'
                self.checker.check(VALID_SOURCE, Budget(10))
            self.assertEqual(caught.exception.code, 'PLANTUML_FAILED')

    def test_probe_rejects_an_executable_that_accepts_invalid_input(self):
        with patch.object(self.checker, '_run', return_value=dict(returncode=0,
                stdout=b'PlantUML version 1.2026.9 (fixture)\n', stderr=b'')), \
                patch.object(self.checker, 'check', return_value={'valid': True}), \
                self.assertRaises(AuditError) as caught:
            self.checker.probe()
        self.assertEqual(caught.exception.code, 'PLANTUML_INCOMPATIBLE')


class PlantUMLBlockTests(unittest.TestCase):
    def test_replacement_cannot_escape_the_original_fence_or_include_external_files(self):
        value = response(dict(branch='main', source_commit='abc'))
        original = copy.deepcopy(value)
        target = diagrams(value)[0]
        for source in ('@startuml\n!include private.puml\n@enduml',
                       '@startuml\nA --> B\n@enduml\n```\nChanged prose.\n```plantuml'):
            replacement = {key: target[key] for key in ('section_key', 'block_index')} | {'source': source}
            with self.subTest(source=source), self.assertRaises(ContractError):
                replace_diagrams(value, [replacement], [target])
            self.assertEqual(value, original)


class PlantUMLPipelineTests(FolderFixture):
    run_case = run_case

    def phases(self):
        return [call['context'].get('phase') for call in self.calls]

    def test_valid_diagrams_do_not_request_corrections(self):
        manifest, code = self.run_case(large=False)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual(self.phases(), ['collect', 'write'])
        self.assertEqual(manifest['study_invocation']['plantuml_validation'],
                         dict(version='PlantUML version 1.2026.9 (fixture)', valid=True, rounds=1))

    def test_corrects_only_failed_bodies_including_extra_sections_and_russian_labels(self):
        def change(runner, context, data):
            break_diagram(runner, context, data)
            if context.get('phase') == 'write':
                data['report_sections'][1]['blocks'].append(dict(markdown=BAD.replace('```', '~~~'), claim_ids=['C-001']))
                data['report_sections'][4]['blocks'][-1]['markdown'] = BAD

        manifest, code = self.run_case(large=False, change=change)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual(self.phases(), ['collect', 'write', 'plantuml_repair'])
        repair = self.calls[-1]
        self.assertEqual({item['section_key'] for item in repair['context']['diagrams']},
                         {'context', 'components', 'data_and_state'})
        self.assertNotIn('evidence_package', repair['context'])
        self.assertNotIn('source_context', repair['context'])
        self.assertIn('--ignore-user-config', repair['command'])
        self.assertIn('features.shell_tool=false', repair['command'])
        self.assertNotEqual(repair['cwd'], self.source)
        self.assertTrue(all(call['budget'] is self.calls[0]['budget'] for call in self.calls))
        self.assertIn('Приложение', manifest['study']['report_markdown'])
        self.assertNotIn('aass', manifest['study']['report_markdown'])
        parent = Path(manifest['study_invocation']['artifact_directory'])
        before = json.loads((parent / 'normalized.json').read_text())
        after = json.loads((parent / 'plantuml-corrected.json').read_text())
        original = copy.deepcopy(after)
        for i, section in enumerate(before['report_sections']):
            for j, block in enumerate(section['blocks']):
                original['report_sections'][i]['blocks'][j]['markdown'] = block['markdown']
        self.assertEqual(original, before)
        provenance = json.loads((parent / 'provenance.json').read_text())['plantuml_correction']
        self.assertEqual(provenance['input_sha256'], sha(canonical(before)))
        self.assertEqual(provenance['corrected_sha256'], sha(canonical(after)))
        checks = json.loads((parent / 'plantuml-validation.json').read_text())
        self.assertEqual(len(checks['rounds']), 2)
        self.assertEqual(manifest['metrics']['attempts'], 3)
        self.assertEqual(manifest['metrics']['usage']['total_tokens'], 360)

    def test_second_request_is_used_only_when_the_first_still_has_syntax_errors(self):
        repairs = []
        def change(runner, context, data):
            break_diagram(runner, context, data)
            if context.get('phase') == 'plantuml_repair':
                repairs.append(context['repair_id'])
                if len(repairs) == 1:
                    data['diagrams'][0]['source'] = context['diagrams'][0]['source']

        manifest, code = self.run_case(large=False, change=change)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual(self.phases(), ['collect', 'write', 'plantuml_repair', 'plantuml_repair'])
        self.assertEqual(len(set(repairs)), 2)
        self.assertEqual(manifest['metrics']['attempts'], 4)
        self.assertEqual(len(manifest['revisions']), 1)
        directory = self.runner.session.run_dir / 'revisions' / '001'
        for path in directory.rglob('attempt-*/invocation.json'):
            record = json.loads(path.read_text())
            for name, expected in record['attempt_hashes'].items():
                self.assertEqual(sha((directory / name).read_bytes()), expected['sha256'], (path, name))

    def test_later_requests_only_include_remaining_defects_and_retain_previous_fixes(self):
        targets = []
        def change(runner, context, data):
            break_diagram(runner, context, data)
            if context.get('phase') == 'write':
                data['report_sections'][4]['blocks'][-1]['markdown'] = BAD
            elif context.get('phase') == 'plantuml_repair':
                targets.append([item['section_key'] for item in context['diagrams']])
                for item, original in zip(data['diagrams'], context['diagrams']):
                    if item['section_key'] == 'data_and_state':
                        item['source'] = original['source']

        manifest, code = self.run_case(large=False, policy='compromise', change=change)
        self.assertEqual((manifest['status'], code), ('PARTIAL', 2))
        self.assertEqual(targets, [['components', 'data_and_state'], ['data_and_state']])
        retained = manifest['study_material']['report_markdown']
        self.assertEqual(retained.count('aass'), 1)

    def test_exhausted_corrections_obey_result_policy_and_report_the_location(self):
        def change(runner, context, data):
            break_diagram(runner, context, data)
            if context.get('phase') == 'plantuml_repair':
                data['diagrams'][0]['source'] = context['diagrams'][0]['source']

        for policy in ('strict', 'compromise'):
            with self.subTest(policy=policy):
                manifest, code = self.run_case(large=False, policy=policy, change=change)
                self.assertEqual(self.phases(), ['collect', 'write', 'plantuml_repair', 'plantuml_repair'])
                self.assertEqual((manifest['status'], code), ('PARTIAL', 2) if policy == 'compromise' else ('FAILED', 1))
                self.assertIsNone(manifest['study'])
                self.assertFalse(manifest['workflow_satisfied'])
                if policy == 'compromise':
                    failure = manifest['study_material']['contract_failure']['details']
                    self.assertEqual((failure['code'], failure['section_key'], failure['block_index'], failure['line']),
                                     ('INVALID_PLANTUML_SYNTAX', 'components', 1, 2))
                    self.assertIn('aass', manifest['study_material']['report_markdown'])
                    self.assertIn('INVALID_PLANTUML_SYNTAX', Path(manifest['final_report']).read_text())
                else:
                    self.assertNotIn('study_material', manifest)

    def test_foreign_targets_claim_changes_and_identity_errors_are_rejected(self):
        for defect in ('target', 'duplicate', 'missing', 'claim', 'identity'):
            def change(runner, context, data):
                break_diagram(runner, context, data)
                if context.get('phase') != 'plantuml_repair':
                    return
                if defect == 'target':
                    data['diagrams'][0]['section_key'] = 'scope'
                elif defect == 'duplicate':
                    data['diagrams'].append(copy.deepcopy(data['diagrams'][0]))
                elif defect == 'missing':
                    data['diagrams'] = []
                elif defect == 'claim':
                    data['claims'] = []
                else:
                    data['repair_id'] = 'foreign-request'

            with self.subTest(defect=defect):
                manifest, code = self.run_case(large=False, change=change)
                self.assertNotEqual(code, 0)
                self.assertIsNone(manifest['study'])
                self.assertEqual(self.phases(), ['collect', 'write', 'plantuml_repair'])

    def test_invalid_repair_schema_keeps_the_syntax_warning_in_recovered_material(self):
        def change(runner, context, data):
            break_diagram(runner, context, data)
            if context.get('phase') == 'plantuml_repair':
                data['claims'] = []

        manifest, code = self.run_case(large=False, policy='compromise', change=change)
        self.assertEqual((manifest['status'], code), ('PARTIAL', 2))
        warning = manifest['study_material']['contract_failure']['message']
        self.assertIn('PlantUML syntax is invalid: components, block 1, diagram line 2.', warning)
        self.assertEqual(self.phases(), ['collect', 'write', 'plantuml_repair'])

    def test_source_or_private_artifact_change_during_repair_blocks_publication(self):
        for target in ('source', 'artifact'):
            def change(runner, context, data):
                break_diagram(runner, context, data)
                if context.get('phase') == 'plantuml_repair':
                    path = (self.source / 'app.py' if target == 'source' else
                            next(runner.session.run_dir.rglob('normalized.json')))
                    path.write_bytes(path.read_bytes() + b'changed\n')

            with self.subTest(target=target):
                manifest, code = self.run_case(large=False, policy='compromise', change=change)
                self.assertEqual((manifest['status'], code), ('FAILED', 1))
                self.assertTrue(manifest['critical_failure'])
                self.assertNotIn('study_material', manifest)
                self.assertFalse((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())

    def test_observed_tool_use_during_repair_blocks_publication_and_recovery(self):
        def observe(*args, **kwargs):
            observer = ToolObserver(*args, **kwargs)
            if kwargs['context'].get('phase') == 'plantuml_repair':
                observer.feed((json.dumps({'type': 'item.completed', 'item': {
                    'id': 'repair-read', 'type': 'command_execution', 'command': 'cat app.py',
                    'aggregated_output': 'Source text.', 'exit_code': 0, 'status': 'completed'}}) + '\n').encode())
            return observer

        with patch('src.model.invocation.ToolObserver', side_effect=observe):
            manifest, code = self.run_case(large=False, policy='compromise', change=break_diagram)
        self.assertEqual((manifest['status'], code), ('FAILED', 1))
        self.assertTrue(manifest['critical_failure'])
        self.assertNotIn('study_material', manifest)

    def test_repair_cannot_reset_the_stage_timeout(self):
        current = [0]
        budget = Budget(10, clock=lambda: current[0])
        def change(runner, context, data):
            break_diagram(runner, context, data)
            if context.get('phase') == 'plantuml_repair':
                current[0] = 10

        with patch('src.model.invocation.Budget', return_value=budget):
            manifest, code = self.run_case(large=False, policy='compromise', change=change)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.phases(), ['collect', 'write', 'plantuml_repair'])
        self.assertEqual(manifest['revisions'][0]['study_invocation']['error']['failure_kind'], 'STAGE_TIMEOUT')

    def test_revision_uses_the_same_syntax_repair_without_an_extra_architecture_revision(self):
        def change(stage, context, data):
            revisions.initial_finding(stage, context, data)
            if context.get('prompt_variant') == 'align' and context.get('phase') != 'plantuml_repair':
                data['report_sections'][2]['blocks'][-1]['markdown'] = BAD
            return data

        manifest, code = revisions.RevisionPipelineTests.run_case(self, change)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual(len(manifest['revisions']), 2)
        self.assertEqual([item.get('phase') for item in self.calls].count('plantuml_repair'), 1)
        self.assertNotIn('aass', manifest['study']['report_markdown'])


@unittest.skipUnless(os.environ.get('EXPLAIN_PLANTUML_INTEGRATION') == '1', 'Real PlantUML integration is opt-in')
class RealPlantUMLTests(unittest.TestCase):
    def test_real_parser_reports_a_line_and_does_not_create_images(self):
        checker = PlantUML(Path.cwd(), NullReporter())
        checker.executable = REAL_PLANTUML
        self.assertIn('PlantUML version ', checker.probe()['version'])
        valid = VALID_SOURCE.replace('component A', 'component "Приложение" as A')
        self.assertTrue(checker.check(valid, Budget(30))['valid'])
        result = checker.check(INVALID_SOURCE, Budget(30))
        self.assertFalse(result['valid'])
        self.assertEqual(result['line'], 2)


@unittest.skipUnless(os.environ.get('EXPLAIN_PLANTUML_INTEGRATION') == '1', 'Real PlantUML integration is opt-in')
class RealPlantUMLPipelineTests(FolderFixture):
    run_case = run_case

    def test_real_parser_rejects_then_accepts_the_model_correction(self):
        self.assertIsNotNone(REAL_PLANTUML, 'The integration job must install PlantUML')
        def change(runner, context, data):
            runner.invoker.plantuml.executable = REAL_PLANTUML
            break_diagram(runner, context, data)

        manifest, code = self.run_case(large=False, change=change)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual([call['context'].get('phase') for call in self.calls],
                         ['collect', 'write', 'plantuml_repair'])
        meta = manifest['study_invocation']
        self.assertTrue(meta['plantuml_validation']['valid'])
        self.assertNotIn('fixture', meta['plantuml_validation']['version'])
        checks = json.loads((Path(meta['artifact_directory']) / 'plantuml-validation.json').read_text())
        self.assertFalse(checks['rounds'][0]['diagrams'][0]['valid'])
        self.assertTrue(all(item['valid'] for item in checks['rounds'][1]['diagrams']))
