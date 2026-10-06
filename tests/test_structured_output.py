# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""XXX CLI output and shared result acceptance."""
import copy
import json
import os
from pathlib import Path
import time
import unittest
from src.reports.document_rendering import materialize_study, recover_sections
from unittest.mock import patch

from src.contracts.contracts import ContractError, SCHEMAS, schema_diagnostics, validate_result
from src.runtime.execution import Budget, execution_settings
from explain import AuditError, Folder, Runner, atomic, load_config
from src.backends import xxx
from src.model.structured_output import blocked_comparison, required_unresolved
from test_explain import doc, review
from fixtures.ledger_response import response
import test_xxx as xxx_fixtures


class XXXOutputTests(unittest.TestCase):
    setUp = xxx_fixtures.XXXTests.setUp
    recorded = xxx_fixtures.XXXTests.recorded
    prompts = xxx_fixtures.XXXTests.prompts

    def config(self):
        path = self.root / 'config.json'
        self.value.setdefault('execution', {}).setdefault('review_enabled', True)
        path.write_text(json.dumps(self.value))
        return load_config(path)

    def stage(self, backend, scenario, repairs=0, stage='study', settings=None):
        config = self.config()
        config['execution'] = {'structured_output_repair_attempts': repairs, **(settings or {})}
        destination = self.root / f'case-{len(list(self.root.glob("case-*")))}' / (stage + '.logs')
        runner = Runner(config, destination.parent)
        runner.versions[backend + ':' + str(self.cli)] = '1.2.27'
        context = {'source_mode': 'folder', 'source_directory': str(self.source),
                   'source_fingerprint': Folder(self.source).snapshot()['source_fingerprint']}
        if stage == 'review':
            context = runner.freeze_review(materialize_study(response(context)), context, destination.parent)
        self.destination, self.last_runner = destination, runner
        with patch.dict(os.environ, self.env | {'AUDIT_FAKE_CASE': scenario}):
            return runner.invoke(stage, context, destination)

    def test_normalization_retains_original_cli_json(self):
        with patch.dict(os.environ, {'AUDIT_FAKE_LOCAL_REFS': '1'}):
            data, meta = self.stage('xxx', '', repairs=2)
        attempt = self.destination / 'attempt-001'
        original = json.loads((attempt / 'extracted.json').read_text())
        normalized = json.loads((attempt / 'normalized.json').read_text())
        self.assertEqual(original['claims'][0]['evidence_ids'], ['E-001'])
        self.assertEqual(normalized['claims'][0]['evidence_ids'], ['study:E-001'])
        self.assertTrue(meta['publication_complete'])
        self.assertEqual(len(self.prompts()), 1)

    def test_schema_errors_never_consume_legacy_repair_setting(self):
        for repairs in (0, 1, 2):
            before = len(self.prompts())
            with self.assertRaises(ContractError) as caught:
                self.stage('xxx', 'schema-extra', repairs)
            self.assertEqual(caught.exception.failure_kind, 'SCHEMA_ERROR')
            self.assertEqual(len(self.prompts()) - before, 1)
            self.assertFalse((self.destination / 'attempt-002').exists())

    def test_local_schema_and_semantic_checks_are_still_authoritative(self):
        for scenario, stage, kind in (('claims-string', 'study', 'SCHEMA_ERROR'),
                                     ('missing-claims', 'study', 'SCHEMA_ERROR'),
                                     ('claims-44', 'review', 'SCHEMA_ERROR'),
                                     ('material-review', 'review', 'SEMANTIC_ERROR')):
            with self.subTest(scenario=scenario), self.assertRaises(ContractError) as caught:
                self.stage('xxx', scenario, 2, stage=stage)
            self.assertEqual(caught.exception.failure_kind, kind)
            self.assertFalse((self.destination.parent / (stage + '.json')).exists())


class ComparisonAcceptanceTests(unittest.TestCase):
    def context(self):
        return {'baseline_branch': 'master', 'baseline_commit': 'abc',
                'requested_branches': ['master', 'master_bnt3_glm', 'master_bnt3_deepseek'],
                'branches': [{'branch': b, 'study': doc(b, 'abc'), 'review': None, 'accepted': True}
                             for b in ['master', 'master_bnt3_glm', 'master_bnt3_deepseek']]}

    def test_model_cannot_hide_two_unaccepted_reviews_or_trust_accepted_flag(self):
        context = self.context()
        data = blocked_comparison(context)
        validate_result('compare', data, context)
        data['unresolved_branches'] = ['master_bnt3_deepseek']
        with self.assertRaises(ContractError) as caught:
            validate_result('compare', data, context)
        self.assertEqual(caught.exception.failure_kind, 'SEMANTIC_ERROR')

    def test_unaccepted_baseline_is_required_even_with_other_accepted_inputs(self):
        context = self.context()
        for item in context['branches'][1:]:
            item['review'] = review(item['branch'], 'abc')
        # Raw wire fixtures have no orchestrator checks/publication record and
        # cannot be promoted merely by attaching a review response.
        self.assertEqual(required_unresolved(context), context['requested_branches'])
        data = blocked_comparison(context)
        data['unresolved_branches'] = []
        with self.assertRaises(ContractError):
            validate_result('compare', data, context)

    def test_diagnostics_are_bounded_counted_and_do_not_expose_extra_keys(self):
        data = review('master', 'abc')
        data['claims'] = [dict(data['claims'][0], secret_key=['private']) for _ in range(150)]
        original = copy.deepcopy(data)
        public = schema_diagnostics(data, SCHEMAS['review'])
        self.assertEqual(public['total_violations'], 150)
        self.assertEqual(len(public['violations']), 100)
        self.assertTrue(public['truncated'])
        self.assertNotIn('secret_key', json.dumps(public))
        self.assertNotIn('private', json.dumps(public))
        self.assertEqual(data, original)


class NativeGitComparisonTests(unittest.TestCase):
    setUp = xxx_fixtures.XXXTests.setUp
    git = xxx_fixtures.XXXTests.git
    init_git = xxx_fixtures.XXXTests.init_git
    run_case = xxx_fixtures.XXXTests.run_case
    recorded = xxx_fixtures.XXXTests.recorded
    prompts = xxx_fixtures.XXXTests.prompts

    def test_unknown_compare_finish_preserves_fixed_study_review_and_diagnostics(self):
        self.init_git(['main', 'other'])
        for backend in ('xxx',):
            for policy in ('strict', 'compromise'):
                self.value.update(result_policy=policy)
                self.value['agent']['backend'] = backend
                self.env['AUDIT_FAKE_BACKEND'] = backend
                before = len(self.prompts())
                manifest, code = self.run_case('unknown-compare-finish')
                self.assertNotEqual(code, 0)
                self.assertEqual(len(self.prompts()) - before, 7)
                self.assertTrue(all(b['accepted'] for b in manifest['branches']))
                self.assertNotIn('comparison', manifest)
                final = Path(manifest['final_report']).read_text()
                self.assertIn('INCOMPLETE_OUTPUT', final)
                self.assertNotIn('SYNTHETIC_PRIVATE_UNKNOWN_FINISH', final)
                self.assertIn('не означает отсутствия различий', final)
                attempt = self.run_dir / 'comparison/compare.logs/attempt-001'
                meta = json.loads((attempt / 'invocation.json').read_text())
                self.assertFalse(meta['publication_complete'])
                self.assertFalse((attempt / 'extracted.json').exists())
                self.assertFalse((self.run_dir / 'comparison/compare.json').exists())
                for branch in manifest['branches']:
                    for stage in ('study', 'review'):
                        self.assertTrue(branch[stage + '_invocation']['publication_complete'])

    def test_no_accepted_inputs_never_call_compare_and_keep_failure_exit_code(self):
        self.init_git(['main', 'other'])
        original = self.git('rev-parse', 'HEAD')
        for backend in ('xxx',):
            self.value['agent']['backend'] = backend
            self.env['AUDIT_FAKE_BACKEND'] = backend
            for scenario, expected_code in (('claims-44', 1), ('schema-error', 1), ('partial-review', 2)):
                with self.subTest(backend=backend, scenario=scenario):
                    start = len(self.prompts())
                    manifest, code = self.run_case(scenario)
                self.assertEqual(code, expected_code, manifest)
                self.assertFalse(any(b['accepted'] for b in manifest['branches']))
                self.assertEqual(len(self.prompts()) - start, 4 if scenario == 'schema-error' else 6)
                result = manifest['comparison']
                self.assertEqual(result['completion_status'], 'BLOCKED')
                self.assertEqual(result['compared_branches'], ['other'])
                self.assertEqual(result['unresolved_branches'], ['main', 'other'])
                self.assertEqual(result['differences'], [])
                self.assertEqual(len(result['limitations']), 2)
                meta = manifest['comparison_invocation']
                self.assertEqual(meta['generated_by'], 'orchestrator')
                self.assertFalse(set(meta) & {'session_id', 'request_id', 'message_id', 'model_actual'})
                inputs = json.loads((self.run_dir / 'comparison/inputs.json').read_text())
                self.assertEqual(inputs['required_unresolved_branches'], ['main', 'other'])
                self.assertTrue(all(not b['accepted'] for b in inputs['branches']))
                wire = {k: result[k] for k in SCHEMAS['compare']['properties']}
                validate_result('compare', wire, inputs)
                from src.reports.presentation import render_stage
                self.assertEqual((self.run_dir / 'comparison/BRANCH_COMPARISON.md').read_text(), render_stage('compare', result, 'Russian'))
                self.assertEqual((self.run_dir / 'comparison/compare.original.md').read_text(), result['report_markdown'])
                self.assertEqual(self.git('rev-parse', 'HEAD'), original)
                self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')
                self.assertEqual(self.git('status', '--porcelain'), '')

    def test_partial_acceptance_passes_authoritative_baseline_status(self):
        self.init_git(['main', 'other'])
        for backend in ('xxx',):
            self.value['agent']['backend'] = backend
            self.env['AUDIT_FAKE_BACKEND'] = backend
            manifest, code = self.run_case('fail-main-study')
            self.assertEqual(code, 1, manifest)
            inputs = json.loads((self.run_dir / 'comparison/inputs.json').read_text())
            self.assertEqual(inputs['required_unresolved_branches'], ['main'])
            self.assertEqual([b['accepted'] for b in inputs['branches']], [False, True])
            self.assertEqual(manifest['comparison']['unresolved_branches'], ['main'])


if __name__ == '__main__':
    unittest.main()
