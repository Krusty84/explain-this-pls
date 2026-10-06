# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Opt-in review: real validation/publication with offline agent responses."""
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from explain import AuditError, Runner, load_config
from src.contracts.contracts import accepted, workflow_satisfied
from src.reports.final_report import render_final_report
from src.runtime.reporting import Reporter
from fixtures.cli_response import cli_result
from fixtures.ledger_response import response, prompt_context
from test_folder import FolderFixture
import test_explain as git_tests


class OptionalReviewConfigTests(FolderFixture):
    def load(self):
        self.config_path.write_text(json.dumps(self.value))
        return load_config(self.config_path)

    def test_default_false_and_strict_boolean(self):
        self.assertFalse(self.load()['execution']['review_enabled'])
        for enabled in (False, True):
            self.value['execution'] = {'review_enabled': enabled}
            cfg = self.load()
            self.assertEqual('review' in cfg['_agents'], enabled)
            self.assertEqual('review' in cfg['_prompt_paths'], enabled)
            self.assertEqual('revise' in cfg['_prompt_paths'], enabled)
        for value in (None, 0, 1, 'false', [], {}):
            self.value['execution'] = {'review_enabled': value}
            with self.subTest(value=value), self.assertRaisesRegex(AuditError, 'review_enabled must be boolean'):
                self.load()

    def test_inactive_review_agent_and_prompts_are_not_resolved(self):
        self.value.update(stage_agents={'review': {'backend': 'missing', 'executable': '/missing/cli'}},
                          prompts={'review': '/missing/review', 'revise': '/missing/revise'})
        self.assertNotIn('review', self.load()['_agents'])
        self.value['execution'] = {'review_enabled': True}
        with self.assertRaises(AuditError):
            self.load()
        self.value['stage_agents'] = {}
        for prompts in ({'review': '/missing/review'}, {'revise': '/missing/revise'}):
            self.value['prompts'] = prompts
            with self.subTest(prompts=prompts), self.assertRaises(AuditError):
                self.load()
        self.value['execution']['max_revision_rounds'] = 0
        self.assertNotIn('revise', self.load()['_prompt_paths'])

    def test_all_examples_explicitly_disable_review(self):
        paths = list(Path(__file__).resolve().parents[1].glob('config*.example.jsonc'))
        self.assertEqual(len(paths), 4)
        for path in paths:
            with self.subTest(path=path), patch('explain.shutil.which', return_value=sys.executable):
                cfg = load_config(path)
                self.assertFalse(cfg['execution']['review_enabled'])
                self.assertNotIn('review', cfg['_agents'])


class PipelineMixin:
    def run_pipeline(self, cfg, change=None):
        directory = self.base / ('optional-' + str(len(list(self.base.glob('optional-*')))))
        stdout, stderr = io.StringIO(), io.StringIO()
        reporter = Reporter(stdout=stdout, stderr=stderr, progress=False)
        self.addCleanup(reporter.close)
        runner = Runner(cfg, directory, reporter=reporter)
        calls = []

        def process(command, cwd, env, payload, **kwargs):
            context = prompt_context(payload)
            calls.append(context)
            data = response(context)
            if change:
                data = change(context, data)
            return cli_result(command, data)

        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process):
            manifest, code = runner.run()
        self.calls, self.run_dir, self.manifest = calls, directory, manifest
        self.reporter, self.stdout = reporter, stdout
        self.messages = stderr.getvalue()
        return manifest, code

    def finish_summary(self, manifest, code):
        self.reporter.finish({'status': manifest['status'], 'exit_code': code,
            'manifest': str(self.run_dir / 'manifest.json'), 'metrics': manifest['metrics']},
            manifest, check_only=False, config_path=self.base / 'config.json', run_dir=self.run_dir)
        return self.stdout.getvalue()


class OptionalReviewPipelineTests(PipelineMixin, FolderFixture):
    def unreviewed_config(self, policy='strict'):
        # Bypass the legacy fixture's opt-in; exercise the actual omitted default.
        self.value.update(result_policy=policy, output_language='English')
        self.config_path.write_text(json.dumps(self.value))
        return load_config(self.config_path)

    def test_success_without_review_in_both_policies(self):
        for policy in ('strict', 'compromise'):
            with self.subTest(policy=policy):
                manifest, code = self.run_pipeline(self.unreviewed_config(policy))
                self.assertEqual((manifest['status'], code), ('COMPLETE', 0))
                self.assertTrue(manifest['workflow_satisfied'])
                self.assertTrue(workflow_satisfied(manifest))
                self.assertFalse(manifest['accepted'])
                self.assertFalse(accepted(manifest))
                self.assertEqual([c['stage'] for c in self.calls], ['catalog', 'study'])
                self.assertEqual(manifest['selected_revision'], '001')
                self.assertEqual(len(manifest['revisions']), 1)
                for item in (manifest, manifest['revisions'][0], manifest['revision_history'][0]):
                    self.assertFalse(item['review_enabled'])
                    self.assertEqual(item['review_skipped'], 'disabled_by_config')
                    self.assertTrue(item['workflow_satisfied'])
                self.assertIsNone(manifest['review'])
                self.assertNotIn('review_invocation', manifest)
                for name in ('review.json', 'review.original.md', 'ARCHITECTURE_REVIEW.md', 'review.logs'):
                    self.assertFalse(list(self.run_dir.rglob(name)), name)
                self.assertTrue((self.run_dir / 'review.plan.json').is_file())
                skipped = next(s for s in manifest['metrics']['stages'] if s['stage'] == 'review')
                self.assertEqual((skipped['status'], skipped['attempts'], skipped['usage']['total_tokens']), ('SKIPPED', 0, 0))
                self.assertEqual(manifest['metrics']['attempts'], 2)
                report = (self.run_dir / 'FINAL_REPORT.md').read_text()
                self.assertIn('Review is disabled by configuration', report)
                self.assertNotIn('review: Result unavailable', report)
                self.assertNotIn('Registry review has not completed', report)
                self.assertNotIn('Policy checks not satisfied', report)
                self.assertIn('Review disabled by configuration', self.messages)
                self.assertNotIn('no architecture report available', self.messages)
                russian, _ = render_final_report(manifest, self.value['folder_mode'], 'folder', 'Russian')
                self.assertIn('Ревью отключено конфигурацией; отчёт не проходил отдельную проверку.', russian)
                legacy = copy.deepcopy(manifest)
                legacy.pop('review_enabled')
                self.assertFalse(workflow_satisfied(legacy))
                old_report, _ = render_final_report(legacy, self.value['folder_mode'], 'folder', 'English')
                self.assertIn('Registry review has not completed', old_report)
                summary = self.finish_summary(manifest, code)
                self.assertIn('Analysis complete.', summary)
                self.assertIn('Review is disabled by configuration', summary)

    def test_incomplete_recovered_and_changed_sources_cannot_succeed(self):
        for case in ('partial', 'evidence', 'coverage', 'catalog', 'recovered', 'changed'):
            def change(ctx, data):
                if ctx['stage'] == 'catalog' and case == 'catalog':
                    data['subsystems'][0]['paths'] = ['missing']
                if ctx['stage'] == 'study':
                    if case == 'partial':
                        data.update(completion_status='PARTIAL', limitations=['Incomplete inspection.'])
                    elif case == 'evidence':
                        data['evidence'][0]['end_line'] = 99
                    elif case == 'coverage':
                        data['coverage'][0].update(status='PARTIALLY_INSPECTED', limitation='Missing inspection.')
                    elif case == 'recovered':
                        data['unexpected'] = True
                    elif case == 'changed':
                        (self.source / 'app.py').write_text('changed source\n')
                return data
            with self.subTest(case=case):
                manifest, code = self.run_pipeline(self.unreviewed_config('compromise'), change)
                self.assertNotEqual(code, 0)
                self.assertFalse(manifest['workflow_satisfied'])
                self.assertFalse(manifest['accepted'])
                self.assertNotIn('review', [c['stage'] for c in self.calls])
                if case == 'changed':
                    self.assertEqual((manifest['status'], code), ('FAILED', 1))

    def test_study_only_success_still_requires_published_bound_artifacts(self):
        manifest, code = self.run_pipeline(self.unreviewed_config())
        self.assertEqual(code, 0)
        for case in ('publication', 'format', 'source', 'document', 'plan', 'critical'):
            item = copy.deepcopy(manifest)
            if case == 'publication':
                item['study_invocation']['publication_complete'] = False
            elif case == 'format':
                item['study_invocation']['artifact_format'] = 'unsupported'
            elif case == 'source':
                item['source_fingerprint'] = 'another-source'
            elif case == 'document':
                item['study']['report_markdown'] += 'Changed after publication.'
            elif case == 'plan':
                item['study']['review_plan']['plan_sha256'] = 'changed'
            else:
                item['critical_failure'] = True
            with self.subTest(case=case):
                self.assertFalse(workflow_satisfied(item))


class OptionalReviewGitTests(PipelineMixin, unittest.TestCase):
    setUp = git_tests.RepoFixture.setUp
    tearDown = git_tests.RepoFixture.tearDown
    git = git_tests.RepoFixture.git

    def config(self, policy='strict', branches=None):
        cfg = git_tests.RepoFixture.config(self)
        cfg.update(result_policy=policy, execution={'review_enabled': False}, output_language='English')
        cfg['git_mode']['branches'] = branches or ['master', 'test01']
        cfg['_agents'].pop('review')
        cfg['_prompt_paths'].pop('review')
        cfg['_prompt_paths'].pop('revise')
        return cfg

    @staticmethod
    def difference(ctx, data):
        if ctx['stage'] == 'compare':
            refs = [dict(branch=b['branch'], artifact='study', revision_id=b['selected_revision'],
                         claim_id=b['study']['claims'][0]['id'],
                         review_target_id=b['study']['review_plan']['review_target_id']) for b in ctx['branches']]
            data['differences'] = [dict(id='D-001', branch='test01', category='flow',
                classification='REPORTED_UNVERIFIED', baseline_statement='Baseline reported flow.',
                branch_statement='Other reported flow.', evidence_refs=refs,
                explanation='Reported by the authors; no separate review.')]
        return data

    def test_one_and_multiple_branches_in_both_policies(self):
        for policy in ('strict', 'compromise'):
            for branches in (['master'], ['master', 'test01']):
                with self.subTest(policy=policy, branches=branches):
                    manifest, code = self.run_pipeline(self.config(policy, branches), self.difference)
                    self.assertEqual((manifest['status'], code), ('COMPLETE', 0), manifest.get('diagnostics'))
                    stages = ['catalog', 'study'] * len(branches) + (['compare'] if len(branches) > 1 else [])
                    self.assertEqual([c['stage'] for c in self.calls], stages)
                    for branch in manifest['branches']:
                        self.assertTrue(branch['workflow_satisfied'])
                        self.assertFalse(branch['accepted'])
                    summary = self.finish_summary(manifest, code)
                    self.assertIn('Branch master: Complete', summary)
                    self.assertNotIn('May be incomplete', summary)
                    if len(branches) > 1:
                        self.assertEqual(self.calls[-1]['required_unresolved_branches'], [])
                        self.assertFalse(self.calls[-1]['review_enabled'])
                        self.assertTrue(manifest['comparison']['program_checks']['policy_satisfied'])

    def test_bad_comparison_support_never_completes_processing(self):
        for case in ('one_side', 'insufficient', 'confirmed', 'wrong_revision', 'unknown_claim', 'hypothesis'):
            def change(ctx, data):
                if case == 'hypothesis' and ctx['stage'] == 'study':
                    data['claims'][0].update(epistemic_kind='HYPOTHESIS', uncertainty='Requires another check.')
                self.difference(ctx, data)
                if ctx['stage'] == 'compare':
                    diff = data['differences'][0]
                    if case == 'one_side':
                        diff['evidence_refs'] = diff['evidence_refs'][:1]
                    elif case == 'insufficient':
                        diff['classification'] = 'INSUFFICIENT_EVIDENCE'
                    elif case == 'confirmed':
                        diff['classification'] = 'CONFIRMED_DIFFERENCE'
                    elif case == 'wrong_revision':
                        diff['evidence_refs'][0]['revision_id'] = '002'
                    elif case == 'unknown_claim':
                        diff['evidence_refs'][0]['claim_id'] = 'C-999'
                return data
            with self.subTest(case=case):
                manifest, code = self.run_pipeline(self.config('compromise'), change)
                self.assertNotEqual(code, 0)
                self.assertFalse(manifest['workflow_satisfied'])

    def test_missing_baseline_blocks_model_comparison(self):
        def change(ctx, data):
            if ctx['stage'] == 'study' and ctx['branch'] == 'master':
                data.update(completion_status='BLOCKED', limitations=['Cannot inspect baseline.'])
            return data
        manifest, code = self.run_pipeline(self.config('compromise'), change)
        self.assertNotEqual(code, 0)
        self.assertNotIn('compare', [c['stage'] for c in self.calls])
        self.assertEqual(manifest['comparison']['completion_status'], 'BLOCKED')
        self.assertIn('master', manifest['comparison']['unresolved_branches'])
        self.assertNotIn('review: no validated', manifest['comparison']['report_markdown'])
