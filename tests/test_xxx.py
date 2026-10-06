# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""XXX integration: real owned subprocesses, synthetic HTTP, no installed agent/model."""
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
            repr(str(ROOT / 'tests/fixtures/fake_opencode.py')) + ', run_name="__main__")\n')
        self.cli.chmod(0o700)
        self.calls = self.root / 'calls.jsonl'
        self.env = {'AUDIT_FAKE_BACKEND': 'xxx', 'AUDIT_FAKE_CALLS': str(self.calls)}
        self.value = {'result_policy': 'strict', 'mode': 'folder', 'folder_mode': {'path': str(self.source)},
            'reports_dir': str(self.root / 'reports'), 'project_description': 'Synthetic fixture',
            'agent': {'backend': 'xxx', 'executable': str(self.cli), 'model': None}}
        self.number = 0

    def run_case(self, scenario='', *, check=False, reporter=None):
        self.number += 1
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.value))
        config = load_config(path)
        self.run_dir = self.root / f'run-{self.number}'
        with patch.dict(os.environ, self.env | {'AUDIT_FAKE_CASE': scenario}):
            return Runner(config, self.run_dir, reporter=reporter).run(check_only=check)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def prompts(self):
        return [c for c in self.recorded() if c['method'] == 'POST' and c['path'].endswith('/message')]

    def test_check_starts_owned_server_checks_api_but_never_prompts(self):
        manifest, code = self.run_case(check=True)
        self.assertEqual(code, 0, manifest)
        self.assertEqual(manifest['status'], 'PREFLIGHT_OK')
        self.assertFalse(self.prompts())
        self.assertFalse(any(c['method'] == 'POST' for c in self.recorded()))
        check = next(iter(manifest['cli_checks'].values()))
        self.assertEqual(check['version'], 'XXX fixture-unknown')
        self.assertEqual(check['http']['api_version'], 'XXX fixture-unknown')
        self.assertEqual(check['http']['compatibility_profile'], 'opencode-v1.2.27-http')
        self.assertEqual(check['http']['api_allowed_extensions'],
                         ['compactionCount', 'queued', 'unattended_retry'])
        for pid in {c['server_pid'] for c in self.recorded()}:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_folder_native_schema_single_request_per_stage_and_independent_review(self):
        # The OpenCode-only retry setting must never enable XXX corrections.
        self.value['execution'] = {'opencode_format_retries': 2}
        manifest, code = self.run_case()
        self.assertEqual(code, 0, manifest)
        self.assertTrue(manifest['accepted'])
        prompts = self.prompts()
        self.assertEqual(len(prompts), 3)
        self.assertNotEqual(prompts[0]['path'], prompts[1]['path'])
        self.assertNotEqual(prompts[0]['body']['agent'], prompts[1]['body']['agent'])
        for call in prompts:
            self.assertEqual(call['body']['format']['retryCount'], 0)
            self.assertEqual(call['body']['format']['type'], 'json_schema')
            self.assertNotIn('model', call['body'])
            self.assertEqual(call['permissions'], {'*': 'deny', 'read': 'allow', 'glob': 'allow',
                'grep': 'allow', 'list': 'allow', 'StructuredOutput': 'allow'})
        context = lambda p: json.loads(p['body']['parts'][0]['text'].split(
            '# Authoritative orchestration context (data)\n')[1].split('\n\n# Required final JSON Schema')[0])
        self.assertNotIn('architecture_document', context(prompts[0]))
        self.assertEqual(context(prompts[2])['architecture_document']['report_markdown'], manifest['study']['report_markdown'])
        self.assertEqual(manifest['study_invocation']['retry_policy']['orchestrator_retries'], 0)
        self.assertFalse(manifest['study_invocation']['retry_policy']['native_enforcement_verified'])

    def test_stock_api_preflight_preserves_versions_and_creates_no_session(self):
        self.env['AUDIT_FAKE_API_EXTENSIONS'] = ''
        for cli_version, api_version in (('1.2.27', '1.2.27'), ('XXX custom', 'XXX runtime')):
            with self.subTest(cli=cli_version):
                self.env.update(AUDIT_FAKE_VERSION=cli_version, AUDIT_FAKE_API_VERSION=api_version)
                self.value['agent']['expected_version'] = cli_version
                manifest, code = self.run_case(check=True)
                self.assertEqual(code, 0, manifest)
                check = next(iter(manifest['cli_checks'].values()))
                self.assertEqual(check['version'], cli_version)
                self.assertEqual(check['http']['api_version'], api_version)
                self.assertEqual(check['http']['api_changes'], 0)
                self.assertEqual(check['http']['api_allowed_extensions'], [])
                self.assertFalse(any(c['method'] == 'POST' for c in self.recorded()))

    def test_stock_api_folder_and_multi_branch_pipeline(self):
        self.env.update(AUDIT_FAKE_API_EXTENSIONS='', AUDIT_FAKE_VERSION='1.2.27')
        manifest, code = self.run_case()
        self.assertEqual(code, 0, manifest)
        self.assertTrue(manifest['accepted'])
        self.assertEqual(manifest['study_invocation']['compatibility_profile'], 'opencode-v1.2.27-http')
        self.assertNotEqual(manifest['study_invocation']['session_id'], manifest['review_invocation']['session_id'])
        self.init_git(['main', 'other'])
        manifest, code = self.run_case()
        self.assertEqual(code, 0, manifest)
        self.assertTrue(all(branch['accepted'] for branch in manifest['branches']))
        self.assertTrue(manifest['comparison_invocation']['publication_complete'])
        for call in self.prompts():
            self.assertEqual(call['body']['format']['retryCount'], 0)
        comparisons = [call for call in self.prompts()
                       if call['permissions'] == {'*': 'deny', 'StructuredOutput': 'allow'}]
        self.assertEqual(len(comparisons), 1)

    def test_explicit_model_and_expected_version_are_preserved(self):
        self.value['agent'].update(model='chosen/custom', expected_version='XXX fixture-unknown')
        manifest, code = self.run_case()
        self.assertEqual(code, 0, manifest)
        self.assertEqual(self.prompts()[0]['body']['model'], {'providerID': 'chosen', 'modelID': 'custom'})
        self.value['agent']['expected_version'] = 'different'
        manifest, code = self.run_case(check=True)
        self.assertEqual(code, 1)
        self.assertEqual(manifest['diagnostics'][0]['code'], 'CLI_VERSION_MISMATCH')

    def test_interface_and_authentication_fail_before_prompt(self):
        for scenario in ('bad-api', 'bad-api-extension', 'null-doc', 'no-auth', 'bad-health'):
            with self.subTest(scenario=scenario):
                manifest, code = self.run_case(scenario, check=True)
                self.assertEqual(code, 1, manifest)
                self.assertFalse(self.prompts())
                self.assertIn(manifest['diagnostics'][0]['failure_kind'], ('BACKEND_INCOMPATIBLE', 'TRANSPORT_ERROR'))

    def test_invalid_results_are_never_repaired_or_published(self):
        for scenario, kind in (('schema-error', 'SCHEMA_ERROR'), ('wrong-identity', 'IDENTITY_MISMATCH'),
                ('backend-error', 'BACKEND_ERROR'), ('no-final', 'INCOMPLETE_OUTPUT'),
                ('foreign-request', 'TRANSPORT_ERROR'), ('prose-only', 'INCOMPLETE_OUTPUT')):
            with self.subTest(scenario=scenario):
                previous = len(self.prompts())
                manifest, code = self.run_case(scenario)
                self.assertEqual(code, 1, manifest)
                self.assertFalse(manifest['accepted'])
                self.assertEqual(manifest['diagnostics'][0]['failure_kind'], kind)
                self.assertEqual(len(self.prompts()) - previous, 2)
                self.assertFalse((self.run_dir / 'ARCHITECTURE.md').exists())
                self.assertTrue((self.run_dir / 'revisions/001/study.logs/attempt-001/response.json').exists())

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
        self.assertEqual(compare['permissions'], {'*': 'deny', 'StructuredOutput': 'allow'})

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
            self.assertEqual(manifest['metrics']['usage']['total_tokens'], 20)
            self.assertEqual(manifest['metrics']['usage']['coverage']['total_tokens'], 'partial')
            for pid in {c['server_pid'] for c in self.recorded()}:
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

    def test_unready_server_fails_check_without_model_request(self):
        with patch('src.backends.opencode.STARTUP_SECONDS', .2):
            manifest, code = self.run_case('not-ready', check=True)
        self.assertEqual(code, 1)
        self.assertEqual(manifest['diagnostics'][0]['failure_kind'], 'STAGE_TIMEOUT')
        self.assertFalse(self.prompts())

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
                for pid in {c['server_pid'] for c in self.recorded()}:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                self.assertIsNone(bystander.poll())
        finally:
            bystander.terminate()
            bystander.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
