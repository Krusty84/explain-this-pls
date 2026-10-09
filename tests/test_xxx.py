# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""XXX integration: real owned subprocesses, synthetic CLI, no installed agent/model."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from explain import Runner, load_config

ROOT = Path(__file__).resolve().parents[1]


class XXXTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'app.py').write_text('print(1)\n')
        self.cli = self.root / 'xxx'
        self.cli.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path(' +
            repr(str(ROOT / 'tests/fixtures/fake_xxx.py')) + ', run_name="__main__")\n')
        self.cli.chmod(0o700)
        self.calls = self.root / 'calls.jsonl'
        self.env = {'AUDIT_FAKE_BACKEND': 'xxx', 'AUDIT_FAKE_CALLS': str(self.calls)}
        self.value = {'result_policy': 'strict', 'mode': 'folder', 'folder_mode': {'path': str(self.source)},
            'execution': {'review_enabled': True},
            'reports_dir': str(self.root / 'reports'), 'project_description': 'Synthetic fixture',
            'agent': {'backend': 'xxx', 'executable': str(self.cli), 'model': None}}
        self.number = 0

    def run_case(self, scenario='', *, check=False, reporter=None):
        self.number += 1
        self.value.setdefault('execution', {}).setdefault('review_enabled', True)
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.value))
        config = load_config(path)
        self.run_dir = self.root / f'run-{self.number}'
        with patch.dict(os.environ, self.env | {'AUDIT_FAKE_CASE': scenario}):
            return Runner(config, self.run_dir, reporter=reporter).run(check_only=check)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_review_disabled_skips_review_and_revision_in_xxx(self):
        self.value['execution'] = {'review_enabled': False, 'max_revision_rounds': 1}
        self.value['stage_agents'] = {'review': {'backend': 'missing', 'executable': '/missing/review-cli'}}
        manifest, code = self.run_case()
        self.assertEqual((manifest['status'], code), ('COMPLETE', 0), manifest.get('diagnostics'))
        self.assertTrue(manifest['workflow_satisfied'])
        self.assertFalse(manifest['accepted'])
        self.assertEqual(len(self.prompts()), 2)
        self.assertIsNone(manifest['review'])
        self.assertFalse((self.run_dir / 'revisions/001/review.logs').exists())

    def prompts(self):
        return [c for c in self.recorded() if c['args'][0] == 'run' and '--help' not in c['args']]

    def test_incomplete_catalogs_publish_partial_multi_session_reports(self):
        paths = ['app.py'] + [f'part{i}.py' for i in range(2, 10)]
        for path in paths:
            (self.source / path).write_text('print(1)\n')
        self.value['result_policy'] = 'compromise'
        self.env['AUDIT_TEST_SUBSYSTEM_PATHS'] = json.dumps(paths)
        for scenario, origin in (('catalog-mixed', 'AGENT_SALVAGED'), ('catalog-unknown', 'DIRECTORY_FALLBACK')):
            with self.subTest(scenario=scenario):
                previous = len(self.prompts())
                manifest, code = self.run_case(scenario)
                self.assertEqual((code, manifest['status'], manifest['accepted']), (2, 'PARTIAL', False), manifest.get('diagnostics'))
                self.assertEqual(manifest['coverage_plan']['origin'], origin)
                self.assertFalse(manifest['coverage_plan']['policy_satisfied'])
                self.assertEqual(manifest['synthesis_status'], 'SUCCEEDED')
                self.assertEqual([s['status'] for s in manifest['study_shards']], ['SUCCEEDED'] * 3)
                self.assertEqual(len(self.prompts()) - previous, 6)
                self.assertEqual(self.prompts()[-2]['permissions'], {'*': 'deny'})
                self.assertTrue((self.run_dir / 'ARCHITECTURE.md').exists())
                self.assertIn(origin + ' / PARTIAL', Path(manifest['final_report']).read_text())
                self.assertIn('Synthetic catalog limitation.', Path(manifest['final_report']).read_text())
                self.assertEqual(manifest['review']['verdict'], 'INCONCLUSIVE')


    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), *args], stderr=subprocess.STDOUT).decode().strip()

    def init_git(self, branches):
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        self.git('add', '.')
        self.git('commit', '-m', 'fixture')
        if len(branches) > 1:
            self.git('branch', 'other')
        self.value.update(mode='git', git_mode={'repository': str(self.source),
            'branches': branches, 'baseline_branch': 'main'})

    def test_git_one_and_multiple_branches_and_compare_permissions(self):
        self.init_git(['main', 'other'])
        for branches in (['main'], ['main', 'other']):
            self.value['git_mode']['branches'] = branches
            previous = len(self.prompts())
            manifest, code = self.run_case()
            self.assertEqual(code, 0, manifest)
            self.assertEqual(manifest['status'], 'COMPLETE')
            self.assertTrue(all(branch['accepted'] for branch in manifest['branches']))
            self.assertEqual(len(self.prompts()) - previous, 3 if len(branches) == 1 else 7)
            self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')
        compare = self.prompts()[-1]
        self.assertNotEqual(compare['cwd'], str(self.source))
        self.assertEqual(compare['permissions'], {'*': 'deny'})

    def test_continue_on_error_preserves_unresolved_comparison(self):
        self.init_git(['main', 'other'])
        manifest, code = self.run_case('fail-main-study')
        # Operational branch errors retain the existing FAILED/exit 1 semantics,
        # even when the unresolved comparison itself is a valid PARTIAL result.
        self.assertEqual(code, 1, manifest)
        self.assertEqual(manifest['status'], 'FAILED')
        self.assertFalse(manifest['branches'][0]['accepted'])
        self.assertEqual(manifest['comparison']['unresolved_branches'], ['main'])
        self.assertEqual(len(self.prompts()), 6)
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')
        self.value['continue_on_error'] = False
        previous = len(self.prompts())
        manifest, code = self.run_case('fail-main-study')
        self.assertEqual(code, 1, manifest)
        self.assertEqual(len(self.prompts()) - previous, 2)
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')

    def test_stage_timeout_and_cleanup_do_not_touch_other_process(self):
        self.value['execution'] = {'stage_timeout_seconds': .8}
        bystander = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            manifest, code = self.run_case('slow')
            self.assertEqual(code, 1, manifest)
            self.assertEqual(manifest['diagnostics'][0]['failure_kind'], 'STAGE_TIMEOUT')
            self.assertEqual(manifest['metrics']['attempts'], 2)
            self.assertEqual(manifest['metrics']['usage']['total_tokens'], 10)
            self.assertEqual(manifest['metrics']['usage']['coverage']['total_tokens'], 'partial')
            for pid in {c['pid'] for c in self.recorded()}:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            self.assertIsNone(bystander.poll())
        finally:
            bystander.terminate()
            bystander.wait(timeout=3)

    def test_idle_and_active_stream_deadlines(self):
        for scenario, settings, expected in (
                ('slow', {'stage_timeout_seconds': 5, 'idle_timeout_seconds': .5}, 'IDLE_TIMEOUT'),
                ('active', {'stage_timeout_seconds': .8}, 'STAGE_TIMEOUT')):
            self.value['execution'] = settings
            manifest, code = self.run_case(scenario)
            self.assertEqual(code, 1, manifest)
            self.assertEqual(manifest['diagnostics'][0]['failure_kind'], expected)


    def test_profile_files_and_artifacts_are_private_and_user_config_is_unchanged(self):
        user_config = self.root / 'user-config.json'
        user_config.write_text('{"model":"private/model"}')
        self.env['OPENCODE_CONFIG'] = str(user_config)
        manifest, code = self.run_case()
        self.assertEqual(code, 0, manifest)
        self.assertEqual(user_config.read_text(), '{"model":"private/model"}')
        for path in self.run_dir.rglob('*'):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, str(path))
                if path.suffix == '.json':
                    self.assertNotIn('Authorization', path.read_text())
            else:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, str(path))

    def test_publication_failure_never_accepts_result(self):
        import explain
        original = explain.atomic
        def fail_report(path, data):
            if path.name == 'ARCHITECTURE.md':
                raise OSError('synthetic publication failure')
            return original(path, data)
        with patch('explain.atomic', side_effect=fail_report):
            manifest, code = self.run_case()
        self.assertEqual(code, 1)
        self.assertFalse(manifest['accepted'])
        self.assertFalse((self.run_dir / 'ARCHITECTURE.md').exists())
        invocation = json.loads((self.run_dir / 'revisions/001/study.logs/invocation.json').read_text())
        self.assertEqual(invocation['status'], 'FAILED')

    def test_source_changes_are_preserved_and_prevent_acceptance(self):
        manifest, code = self.run_case('source-change')
        self.assertEqual(code, 1)
        self.assertFalse(manifest['accepted'])
        self.assertEqual((self.source / 'app.py').read_text(), 'unexpected fixture mutation\n')
        self.assertFalse((self.run_dir / 'ARCHITECTURE.md').exists())
        self.assertEqual(len(self.prompts()), 2)

    def test_signals_cleanup_real_xxx_pipeline(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.value))
        bystander = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            for sig in ('SIGINT', 'SIGTERM'):
                env = os.environ | self.env | {'AUDIT_FAKE_CASE': 'interrupt', 'AUDIT_FAKE_SIGNAL': sig,
                                              'AUDIT_FAKE_INTERRUPT_STAGE': 'study'}
                process = subprocess.run([sys.executable, '-B', str(ROOT / 'explain.py'), '--config', str(path)],
                    env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(process.returncode, 130, process.stderr)
                manifest = json.loads(Path(json.loads(process.stdout)['manifest']).read_text())
                self.assertFalse(manifest['accepted'])
                self.assertEqual(manifest['diagnostics'][0]['code'], 'INTERRUPTED')
                self.assertEqual(manifest['metrics']['attempts'], 2)
                self.assertEqual(manifest['metrics']['usage']['total_tokens'], 10)
                self.assertEqual(manifest['metrics']['usage']['coverage']['total_tokens'], 'partial')
                for pid in {c['pid'] for c in self.recorded()}:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                self.assertIsNone(bystander.poll())
        finally:
            bystander.terminate()
            bystander.wait(timeout=3)


    def test_preflight_checks_cli_only_and_preserves_branded_version(self):
        self.value['agent']['expected_version'] = 'XXX fixture-unknown'
        manifest, code = self.run_case(check=True)
        self.assertEqual(code, 0, manifest)
        self.assertFalse(self.prompts())
        self.assertEqual([c['args'] for c in self.recorded()], [
            ['--version'], ['run', '--help'], ['export', '--help'], ['session', 'delete', '--help']])
        check = next(iter(manifest['cli_checks'].values()))
        self.assertEqual(check['compatibility_profile'], 'opencode-v1.2.27-cli')
        self.assertNotIn('http', check)
        self.value['agent']['expected_version'] = 'different'
        manifest, code = self.run_case(check=True)
        self.assertEqual(code, 1)
        self.assertEqual(manifest['diagnostics'][0]['code'], 'CLI_VERSION_MISMATCH')

    def test_folder_one_run_per_stage_export_delete_and_separate_review(self):
        self.value['execution'] = {'structured_output_repair_attempts': 2, 'opencode_format_retries': 2}
        manifest, code = self.run_case()
        self.assertEqual(code, 0, manifest)
        self.assertTrue(manifest['accepted'])
        calls = self.prompts()
        self.assertEqual([c['context']['stage'] for c in calls], ['catalog', 'study', 'review'])
        self.assertEqual(len({c['agent'] for c in calls}), 3)
        self.assertEqual(len({c['session_id'] for c in calls}), 3)
        self.assertEqual(len([c for c in self.recorded() if c['args'][0] == 'export' and '--help' not in c['args']]), 3)
        self.assertEqual(len([c for c in self.recorded() if c['args'][:2] == ['session', 'delete'] and '--help' not in c['args']]), 3)
        for call in calls:
            self.assertEqual(call['permissions'], {'*': 'deny', 'read': 'allow', 'glob': 'allow', 'grep': 'allow', 'list': 'allow', 'external_directory': 'deny'})
            self.assertNotIn('--model', call['args'])
            self.assertEqual(call['config']['share'], 'disabled')
        self.assertEqual(manifest['study_invocation']['retry_policy']['orchestrator_retries'], 0)
        self.assertEqual(manifest['study_invocation']['model_actual'], 'fixture/configured-model')
        self.assertEqual(calls[-1]['context']['architecture_document']['report_markdown'], manifest['study']['report_markdown'])
        self.assertFalse(list((self.root / 'calls-sessions').glob('*.json')))

    def test_explicit_model_and_automatic_compaction(self):
        self.value['agent']['model'] = 'chosen/custom'
        self.env['AUDIT_FAKE_COMPACTIONS'] = '2'
        manifest, code = self.run_case()
        self.assertEqual(code, 0, manifest)
        self.assertEqual(manifest['study_invocation']['model_actual'], 'chosen/custom')
        self.assertEqual(manifest['study_invocation']['compaction']['completed'], 2)
        self.assertEqual(manifest['metrics']['usage']['total_tokens'], 90)
        self.assertEqual(len(self.prompts()), 3)

    def test_fenced_json_completes_publication_with_original_logs(self):
        import explain
        process = explain.process
        captured = {}
        def capture(*args, **kwargs):
            result = process(*args, **kwargs)
            if 'log_dir' in kwargs:
                captured[kwargs['log_dir'] / 'stdout.log'] = result['stdout']
            return result
        self.env['AUDIT_FAKE_COMPACTIONS'] = '1'
        with patch('explain.process', side_effect=capture):
            manifest, code = self.run_case('fences')
        self.assertEqual((code, manifest['status'], manifest['accepted']), (0, 'COMPLETE', True))
        self.assertEqual([c['context']['stage'] for c in self.prompts()], ['catalog', 'study', 'review'])
        self.assertTrue((self.run_dir / 'ARCHITECTURE.md').exists())
        self.assertTrue(Path(manifest['final_report']).exists())
        paths = list(self.run_dir.rglob('attempt-001/invocation.json'))
        self.assertEqual(len(paths), 3)
        for path in paths:
            meta = json.loads(path.read_text())
            self.assertTrue(meta['publication_complete'])
            self.assertEqual(meta['retry_policy']['orchestrator_retries'], 0)
            self.assertEqual(meta['compaction']['completed'], 1)
            provenance = meta['provider_metadata']['response_normalization']
            self.assertEqual(provenance['kind'], 'markdown_json_fence')
            raw = (path.parent / 'stdout.log').read_bytes()
            self.assertEqual(raw, captured[path.parent / 'stdout.log'])
            events = [json.loads(line) for line in raw.splitlines()]
            original = ''.join(e['part']['text'] for e in events if e['type'] == 'text'
                               and e['part']['messageID'] == meta['message_id'])
            self.assertTrue(original.startswith(' \t\r\n```JSON \t\r\n'))
            self.assertTrue(original.endswith('\r\n``` \r\n\t'))
            exported_bytes = (path.parent / 'session-export/stdout.log').read_bytes()
            self.assertEqual(exported_bytes, captured[path.parent / 'session-export/stdout.log'])
            exported = json.loads(exported_bytes)
            self.assertEqual(original, ''.join(p['text'] for p in exported['messages'][-1]['parts']
                                              if p['type'] == 'text'))
            payload = original[provenance['payload_start']:provenance['payload_end']]
            self.assertEqual(json.loads(payload), json.loads((path.parent / 'extracted.json').read_bytes()))
            self.assertIn(b'\\r\\n', raw)
            self.assertTrue(json.loads((path.parent / 'validation.json').read_bytes())['valid'])

    def test_mixed_stage_agents_keep_other_cli_commands_and_results(self):
        import explain
        from fixtures.cli_response import cli_result
        from fixtures.ledger_response import prompt_context, response
        original = explain.process
        mock_cli = self.root / 'mock-review'
        mock_cli.write_text('#!' + sys.executable + '\n')
        mock_cli.chmod(0o700)
        for backend in ('codex', 'claude-code', 'opencode'):
            calls = []
            self.value['stage_agents'] = {'review': {'backend': backend, 'executable': str(mock_cli)}}
            def process(command, cwd, env, payload=b'', **kwargs):
                if command[0] != str(mock_cli):
                    return original(command, cwd, env, payload, **kwargs)
                if '--version' in command:
                    return {'returncode': 0, 'stdout': b'2.0.23', 'stderr': b''}
                if '--help' in command:
                    flags = explain.CLI_ADAPTERS[backend].required_flags('folder')
                    return {'returncode': 0, 'stdout': ' '.join(flags).encode(), 'stderr': b''}
                calls.append(command)
                data = response(prompt_context(payload))
                if backend == 'claude-code': data = {'is_error': False, 'structured_output': data}
                return cli_result(command, data)
            before = len(self.prompts())
            with patch('explain.process', side_effect=process):
                manifest, code = self.run_case()
            with self.subTest(backend=backend):
                self.assertEqual(code, 0, manifest)
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(self.prompts()) - before, 2)
                self.assertEqual(manifest['review_invocation']['backend'], backend)
                self.assertNotIn('--title', calls[0])
                self.assertEqual('--standalone' in calls[0], backend == 'opencode')

    def test_invalid_results_do_not_retry_or_publish(self):
        self.value['execution'] = {'structured_output_repair_attempts': 2}
        for scenario, kind in (
                ('schema-error', 'SCHEMA_ERROR'), ('schema-extra', 'SCHEMA_ERROR'),
                ('wrong-identity', 'IDENTITY_MISMATCH'), ('backend-error', 'BACKEND_ERROR'),
                ('no-final', 'INCOMPLETE_OUTPUT'), ('foreign-request', 'TRANSPORT_ERROR'),
                ('invalid-json', 'INVALID_JSON'), ('prose-only', 'INVALID_JSON'),
                ('truncated', 'INCOMPLETE_OUTPUT'), ('export-text', 'TRANSPORT_ERROR'),
                ('summary-only', 'TRANSPORT_ERROR'), ('exit-error', 'BACKEND_ERROR'),
                ('unfinished-tool', 'INCOMPLETE_OUTPUT')):
            with self.subTest(scenario=scenario):
                previous = len(self.prompts())
                manifest, code = self.run_case(scenario)
                self.assertEqual(code, 1, manifest)
                self.assertEqual(manifest['diagnostics'][0]['failure_kind'], kind)
                self.assertEqual(len(self.prompts()) - previous, 2)
                self.assertFalse(manifest['accepted'])
                self.assertFalse((self.run_dir / 'ARCHITECTURE.md').exists())
                self.assertFalse(list(self.run_dir.rglob('attempt-002')))
                self.assertTrue((self.run_dir / 'revisions/001/study.logs/attempt-001/stdout.log').exists())

    def test_missing_finish_is_verified_without_repeating_run(self):
        manifest, code = self.run_case('missing-finish')
        self.assertEqual(code, 0, manifest)
        self.assertEqual(len(self.prompts()), 3)
        self.assertEqual(manifest['study_invocation']['completion_source'], 'session_export')

    def test_foreign_export_is_never_deleted_and_failed_cleanup_blocks_publication(self):
        for scenario in ('export-foreign', 'export-prompt', 'delete-failed', 'export-failed'):
            previous = len(self.recorded())
            manifest, code = self.run_case(scenario)
            with self.subTest(scenario=scenario):
                self.assertEqual(code, 1, manifest)
                self.assertTrue(manifest['critical_failure'])
                self.assertFalse(manifest['accepted'])
                calls = self.recorded()[previous:]
                if scenario in ('export-foreign', 'export-prompt', 'export-failed'):
                    # Catalog is healthy except when the export command itself fails.
                    study = [c['session_id'] for c in calls if c.get('context', {}).get('stage') == 'study']
                    self.assertFalse(any(c['args'][:2] == ['session', 'delete'] and c['args'][-1] in study for c in calls))


if __name__ == '__main__':
    unittest.main()
