# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline regressions for partial documents, strict compatibility and final assembly."""
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from contracts import ContractError, validate_result
from explain import AuditError, Runner, atomic, load_config
from reporting import Reporter
from structured_output import blocked_comparison
from test_explain import doc, review
import test_explain as git_fixtures
import test_folder as folder_fixtures
import test_structured_output as native_fixtures
import test_xxx as xxx_fixtures


def semantic_failure(data):
    data['claims'][0].update(outcome='UNVERIFIABLE', limitation='Static inspection is insufficient.')
    data['limitations'] = ['Operational evidence was unavailable.']
    return data


def response_for(context):
    if 'baseline_branch' in context:
        data = blocked_comparison(context)
        data.update(completion_status='PARTIAL' if data['unresolved_branches'] else 'COMPLETE',
                    report_markdown='# Compared available inputs', limitations=data['limitations'] if data['unresolved_branches'] else [])
        return data
    data = (review if 'architecture_document' in context else doc)(context.get('branch', ''), context.get('source_commit', ''))
    if context.get('source_mode') == 'folder':
        del data['branch'], data['source_commit']
        data.update(source_directory=context['source_directory'], source_fingerprint=context['source_fingerprint'])
    return data


class CompromiseFolder(folder_fixtures.FolderFixture):
    def run_case(self, change=None, *, backend='codex', policy=None, continue_on_error=True, atomic_override=None):
        config = self.config()
        config.pop('result_policy')  # Exercise Runner's default as well as load_config's default.
        if policy is not None:
            config['result_policy'] = policy
        config['continue_on_error'] = continue_on_error
        for agent in config['_agents'].values():
            agent['backend'] = backend
        run_dir = self.base / ('run-' + str(len(list(self.base.glob('run-*')))))
        runner = Runner(config, run_dir)
        calls, originals = [], {}

        def process(command, cwd, env, payload, **kwargs):
            context = json.loads(payload.decode().split('# Authoritative orchestration context (data)\n')[1]
                                 .split('\n\n# Required final JSON Schema')[0])
            stage = 'review' if 'architecture_document' in context else 'study'
            calls.append((stage, context))
            data = response_for(context)
            outcome = change(stage, data) if change else data
            if outcome == 'failure':
                return {'returncode': 17, 'stdout': b'', 'stderr': b'private backend error'}
            if outcome == 'malformed':
                return {'returncode': 0, 'stdout': b'{broken', 'stderr': b''}
            originals[stage] = copy.deepcopy(outcome)
            encoded = outcome if backend == 'codex' else {'is_error': False, 'structured_output': outcome}
            return {'returncode': 0, 'stdout': json.dumps(encoded).encode(), 'stderr': b''}

        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process), \
                patch('explain.atomic', side_effect=atomic_override or atomic):
            manifest, code = runner.run()
        self.manifest, self.calls, self.originals, self.run_dir = manifest, calls, originals, run_dir
        return manifest, code

    def test_semantic_review_is_retained_for_both_cli_adapters(self):
        for backend in ('codex', 'claude-code'):
            with self.subTest(backend=backend):
                manifest, code = self.run_case(lambda s, d: semantic_failure(d) if s == 'review' else d, backend=backend)
                self.assertEqual((manifest['status'], code), ('PARTIAL', 2))
                self.assertFalse(manifest['accepted'])
                self.assertIsNone(manifest['review'])
                self.assertTrue(manifest['review_usable'])
                self.assertFalse((self.run_dir / 'review.json').exists())
                self.assertFalse((self.run_dir / 'ARCHITECTURE_REVIEW.md').exists())
                text = Path(manifest['final_report']).read_text()
                self.assertIn('Независимая проверка не завершена', text)
                self.assertIn('C-001', text)
                self.assertIn('Operational evidence was unavailable.', text)
                self.assertIn(self.originals['study']['report_markdown'], text)
                self.assertIn(self.originals['review']['report_markdown'], text)
                self.assertEqual([s for s, _ in self.calls], ['study', 'review'])
                raw = json.loads((self.run_dir / 'review.logs/attempt-001/extracted.json').read_text())
                self.assertEqual(raw, self.originals['review'])
                self.assertEqual(json.loads((self.run_dir / 'review.logs/attempt-001/invocation.json').read_text())['status'], 'FAILED')

    def test_missing_study_field_is_reviewed_without_promoting_original(self):
        def change(stage, data):
            if stage == 'study':
                del data['limitations']
            return data
        manifest, code = self.run_case(change)
        self.assertEqual(code, 2)
        self.assertIsNone(manifest['study'])
        self.assertIsNotNone(manifest['review'])
        self.assertFalse(manifest['accepted'])
        self.assertFalse(self.calls[1][1]['document_strictly_valid'])
        self.assertIn('validation_issues', self.calls[1][1]['architecture_document'])
        self.assertFalse((self.run_dir / 'study.json').exists())

    def test_malformed_ledger_is_not_used_as_structured_evidence(self):
        def change(stage, data):
            if stage == 'review':
                data['claims'][0]['evidence'] = {'secret': 'not an evidence array'}
            return data
        manifest, code = self.run_case(change)
        self.assertEqual(code, 2)
        self.assertNotIn('review_ledger', manifest['review_material'])
        self.assertNotIn('not an evidence array', Path(manifest['final_report']).read_text())

    def test_low_finding_unknown_link_and_wrong_verdict_are_all_visible(self):
        def change(stage, data):
            if stage == 'review':
                semantic_failure(data)
                data['claims'][0]['finding_ids'] = ['F-001', 'F-999']
                data['findings'] = [{'id': 'F-001', 'severity': 'LOW', 'type': 'UNSUPPORTED_ASSERTION',
                    'claim_ids': ['C-001'], 'location': 'overview', 'evidence': ['app.py'],
                    'impact': 'Uncertain claim', 'proposed_correction': 'Qualify the claim'}]
                data['verdict'] = 'CHANGES_REQUIRED'
            return data
        manifest, code = self.run_case(change)
        self.assertEqual(code, 2)
        issues = manifest['review_material']['validation_issues']['semantic_diagnostics']['violations']
        self.assertTrue({'UNKNOWN_FINDING', 'MISSING_MATERIAL_FINDING', 'INCONSISTENT_VERDICT', 'MISSING_REPORT_ID'} <= {i['code'] for i in issues})
        self.assertEqual(self.originals['review']['findings'][0]['severity'], 'LOW')

    def test_failed_review_preserves_study_for_both_continuation_settings(self):
        for cont in (True, False):
            manifest, code = self.run_case(lambda s, d: 'failure' if s == 'review' else d, continue_on_error=cont)
            self.assertEqual((manifest['status'], code), ('PARTIAL', 2))
            self.assertTrue(manifest['has_usable_material'])
            self.assertFalse(manifest['critical_failure'])
            self.assertNotIn('private backend error', Path(manifest['final_report']).read_text())

    def test_recovered_study_stops_calls_when_requested(self):
        def change(stage, data):
            data['extra'] = True
            return data
        manifest, code = self.run_case(change, continue_on_error=False)
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('study_material', manifest)

    def test_broken_json_wrong_identity_and_wrong_task_are_not_recovered(self):
        for kind in ('malformed', 'identity', 'task', 'failure'):
            def change(stage, data):
                if kind in ('malformed', 'failure'):
                    return kind
                data['source_fingerprint' if kind == 'identity' else 'task'] = 'wrong'
                return data
            manifest, code = self.run_case(change)
            self.assertEqual((manifest['status'], code), ('FAILED', 1))
            self.assertNotIn('study_material', manifest)
            self.assertFalse(manifest['has_usable_material'])
            self.assertIn('Диагностическая сводка', Path(manifest['final_report']).read_text())
            self.assertEqual(len(self.calls), 1)

    def test_changed_source_remains_fatal_and_current_response_unpublished(self):
        def change(stage, data):
            if stage == 'review':
                semantic_failure(data)
                (self.source / 'app.py').write_text('changed')
            return data
        manifest, code = self.run_case(change)
        self.assertEqual(code, 1)
        self.assertTrue(manifest['critical_failure'])
        self.assertTrue(manifest['has_usable_material'])
        self.assertNotIn('review_material', manifest)

    def test_interrupt_preserves_prior_material_and_code(self):
        def change(stage, data):
            if stage == 'review':
                raise KeyboardInterrupt()
            return data
        manifest, code = self.run_case(change)
        self.assertEqual(code, 130)
        self.assertTrue(Path(manifest['final_report']).is_file())
        self.assertTrue(manifest['has_usable_material'])

    def test_final_publication_failure_is_fatal(self):
        def fail(path, data):
            if path.name == 'FINAL_REPORT.md':
                raise OSError('fixture full disk')
            atomic(path, data)
        manifest, code = self.run_case(atomic_override=fail)
        self.assertEqual((manifest['status'], code), ('FAILED', 1))
        self.assertTrue(manifest['critical_failure'])
        self.assertNotIn('final_report', manifest)

    def test_strict_retains_failure_and_only_validated_reports(self):
        manifest, code = self.run_case(lambda s, d: semantic_failure(d) if s == 'review' else d, policy='strict')
        self.assertEqual((manifest['status'], code), ('FAILED', 1))
        self.assertNotIn('review_material', manifest)
        self.assertTrue(Path(manifest['final_report']).is_file())

    def test_full_acceptance_is_complete_and_final_report_is_first_report(self):
        manifest, code = self.run_case()
        self.assertEqual((manifest['status'], code), ('COMPLETE', 0))
        out = io.StringIO()
        reporter = Reporter(mode='text', stdout=out, stderr=io.StringIO())
        self.addCleanup(reporter.close)
        reporter.finish({'status': manifest['status'], 'exit_code': code, 'manifest': str(self.run_dir / 'manifest.json')},
            manifest, check_only=False, config_path=self.config_path, run_dir=self.run_dir)
        self.assertLess(out.getvalue().index('FINAL_REPORT.md'), out.getvalue().index('ARCHITECTURE.md'))

    def test_config_defaults_validation_and_check_only(self):
        self.config_path.write_text(json.dumps(self.value))
        config = load_config(self.config_path)
        self.assertEqual(config['result_policy'], 'compromise')
        runner = Runner(config, self.base / 'check')
        with patch.object(runner, 'check_cli', return_value={}), patch.object(runner, 'invoke') as invoke:
            manifest, code = runner.run(check_only=True)
        self.assertEqual(code, 0)
        invoke.assert_not_called()
        self.assertNotIn('final_report', manifest)
        self.assertFalse((runner.run_dir / 'FINAL_REPORT.md').exists())
        self.value['result_policy'] = 'unknown'
        self.config_path.write_text(json.dumps(self.value))
        with self.assertRaisesRegex(AuditError, 'result_policy'):
            load_config(self.config_path)


class CompromiseGit(unittest.TestCase):
    setUp = git_fixtures.RepoFixture.setUp
    tearDown = git_fixtures.RepoFixture.tearDown
    git = git_fixtures.RepoFixture.git
    config = git_fixtures.RepoFixture.config

    def pipeline(self, *, failure_branch='test01', compare_failure=False, cont=True, single=False, restore_failure=False):
        config = self.config()
        config.pop('result_policy')
        config['continue_on_error'] = cont
        if single:
            config['branches'] = ['master']
        runner = Runner(config, self.base / 'run')
        original_restore = runner.repo.restore
        def restore(*args):
            result = original_restore(*args)
            if restore_failure:
                raise OSError('fixture restoration verification failed')
            return result
        calls = []
        def process(command, cwd, env, payload, **kwargs):
            context = json.loads(payload.decode().split('# Authoritative orchestration context (data)\n')[1]
                                 .split('\n\n# Required final JSON Schema')[0])
            stage = 'compare' if 'baseline_branch' in context else 'review' if 'architecture_document' in context else 'study'
            calls.append((stage, context))
            if context.get('branch') == failure_branch or (stage == 'compare' and compare_failure):
                return {'returncode': 0, 'stdout': b'{broken', 'stderr': b''}
            data = response_for(context)
            if stage == 'review':
                semantic_failure(data)
            return {'returncode': 0, 'stdout': json.dumps(data).encode(), 'stderr': b''}
        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process), \
                patch.object(runner.repo, 'restore', side_effect=restore):
            manifest, code = runner.run()
        self.assertEqual(self.repo.symbolic(), 'master')
        self.assertEqual(self.repo.head(), self.master)
        return manifest, code, calls

    def test_reported_three_branch_failure_still_compares_and_produces_document(self):
        manifest, code, calls = self.pipeline()
        self.assertEqual((manifest['status'], code), ('PARTIAL', 2))
        self.assertFalse(any(b['accepted'] for b in manifest['branches']))
        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[-1][0], 'compare')
        self.assertEqual(set(manifest['comparison']['unresolved_branches']), set(self.config()['branches']))
        text = Path(manifest['final_report']).read_text()
        for branch in self.config()['branches']:
            self.assertIn(branch, text)
        self.assertEqual(sum(b.get('study_usable', False) for b in manifest['branches']), 2)

    def test_missing_baseline_does_not_invent_comparison(self):
        manifest, code, calls = self.pipeline(failure_branch='master')
        self.assertEqual(code, 2)
        self.assertFalse(any(s == 'compare' for s, _ in calls))
        self.assertEqual(manifest['comparison']['completion_status'], 'BLOCKED')
        self.assertTrue(Path(manifest['final_report']).is_file())

    def test_compare_failure_preserves_available_studies(self):
        manifest, code, calls = self.pipeline(compare_failure=True)
        self.assertEqual(code, 2)
        self.assertNotIn('comparison', manifest)
        self.assertTrue(manifest['has_usable_material'])

    def test_single_branch_and_stop_after_recovered_review(self):
        manifest, code, calls = self.pipeline(single=True, cont=False)
        self.assertEqual(code, 2)
        self.assertEqual([s for s, _ in calls], ['study', 'review'])
        self.assertNotIn('comparison', manifest)

    def test_restoration_failure_stays_failed_with_prior_material(self):
        manifest, code, calls = self.pipeline(restore_failure=True)
        self.assertEqual((manifest['status'], code), ('FAILED', 1))
        self.assertTrue(manifest['critical_failure'])
        self.assertTrue(manifest['has_usable_material'])
        self.assertIn('Критическая ошибка', Path(manifest['final_report']).read_text())

    def test_comparison_cannot_confirm_unaccepted_pair(self):
        context = {'result_policy': 'compromise', 'baseline_branch': 'master', 'baseline_commit': 'abc',
                   'requested_branches': ['master', 'other'], 'branches': []}
        data = response_for(context)
        data['report_markdown'] += '\nD-001'
        data['differences'] = [{'id': 'D-001', 'branch': 'other', 'category': 'runtime',
            'classification': 'CONFIRMED_DIFFERENCE', 'baseline_statement': 'A', 'branch_statement': 'B',
            'evidence_refs': ['master/a.py', 'other/b.py'], 'explanation': 'Change'}]
        with self.assertRaisesRegex(ContractError, 'accepted inputs'):
            validate_result('compare', data, context)


class CompromiseNative(unittest.TestCase):
    setUp = native_fixtures.NativeStructuredOutputTests.setUp
    close = native_fixtures.NativeStructuredOutputTests.close
    stage = native_fixtures.NativeStructuredOutputTests.stage
    prompts = native_fixtures.NativeStructuredOutputTests.prompts

    def config(self):
        config = native_fixtures.NativeStructuredOutputTests.config(self)
        config.pop('result_policy')
        return config

    def test_both_native_adapters_recover_format_only_after_bounded_attempts(self):
        for backend in ('xxx', 'opencode'):
            for repairs in (0, 1, 2):
                before = len(self.prompts())
                data, meta = self.stage(backend, 'repair-invalid', repairs)
                self.assertIsNone(data)
                self.assertIn('usable_material', meta)
                self.assertEqual(len(self.prompts()) - before, repairs + 1)
                self.assertFalse((self.destination.parent / 'study.json').exists())

    def test_invalid_repair_does_not_replace_original_facts(self):
        for backend in ('xxx', 'opencode'):
            for scenario in ('repair-evidence', 'repair-verdict'):
                data, meta = self.stage(backend, scenario, 2, stage='review')
                self.assertIsNone(data)
                original = json.loads((self.destination / 'attempt-001/extracted.json').read_text())
                self.assertEqual(meta['usable_material']['report_markdown'], original['report_markdown'])
                self.assertEqual(meta['recovery_source_attempt'], str(self.destination / 'attempt-001'))

    def test_foreign_session_and_cleanup_failure_cannot_be_recovered(self):
        import opencode
        original_close = opencode.Server.close
        def fail(server):
            original_close(server)
            raise OSError('cleanup failed')
        for backend in ('xxx', 'opencode'):
            with self.assertRaises(ContractError):
                self.stage(backend, 'foreign-session')
            with patch.object(opencode.Server, 'close', fail), self.assertRaises(ContractError):
                self.stage(backend, 'repair-invalid', 1)
            self.assertTrue(self.last_runner.critical_failure)
            self.assertFalse((self.destination.parent / 'study.material.json').exists())

    def test_timed_out_repair_keeps_original_completed_material(self):
        for backend in ('xxx', 'opencode'):
            before = len(self.prompts())
            data, meta = self.stage(backend, 'repair-timeout', 1, settings={'stage_timeout_seconds': 1.5})
            self.assertIsNone(data)
            self.assertEqual(meta['error']['failure_kind'], 'STAGE_TIMEOUT')
            self.assertIn('usable_material', meta)
            self.assertEqual(len(self.prompts()) - before, 2)


class NativeFinalPipeline(unittest.TestCase):
    git = xxx_fixtures.XXXTests.git
    init_git = xxx_fixtures.XXXTests.init_git
    run_case = xxx_fixtures.XXXTests.run_case
    recorded = xxx_fixtures.XXXTests.recorded
    prompts = xxx_fixtures.XXXTests.prompts

    def setUp(self):
        xxx_fixtures.XXXTests.setUp(self)
        self.value.pop('result_policy')

    def test_native_pipeline_generates_final_without_extra_model_call(self):
        for backend in ('xxx', 'opencode'):
            self.value['agent']['backend'] = backend
            self.env['AUDIT_FAKE_BACKEND'] = backend
            for scenario in ('partial-review', 'material-review'):
                before = len(self.prompts())
                with patch('opencode.verify_native_retries'):
                    manifest, code = self.run_case(scenario)
                self.assertEqual(code, 2)
                self.assertEqual(len(self.prompts()) - before, 2)
                self.assertTrue(Path(manifest['final_report']).is_file())
                if scenario == 'material-review':
                    self.assertIsNone(manifest['review'])
                    self.assertIn('review_material', manifest)

    def test_cleanup_failure_remains_fatal_even_with_a_study(self):
        import opencode
        original_close = opencode.Server.close
        def close(server):
            original_close(server)
            if server.meta.get('stage') == 'review':
                raise OSError('fixture review cleanup failure')
        with patch.object(opencode.Server, 'close', close):
            manifest, code = self.run_case('material-review')
        self.assertEqual((manifest['status'], code), ('FAILED', 1))
        self.assertTrue(manifest['critical_failure'])
        self.assertTrue(manifest['has_usable_material'])
        self.assertNotIn('review_material', manifest)


if __name__ == '__main__':
    unittest.main()
