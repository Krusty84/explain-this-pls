# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Wire tests bypass ONLY the known upstream retry-capability gate, explicitly.

They validate HTTP integration, not a working upstream native retry implementation.
"""
import copy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from src.contracts.contracts import ContractError, SCHEMAS, validate_result
from src.runtime.execution import Budget
from explain import Runner, atomic, cli_env
from src.backends.opencode import Server, extract_result, prepare_environment, validate_history, verify_native_retries, verify_version
from src.runtime.reporting import Reporter

ROOT = Path(__file__).resolve().parents[1]


class HTTPFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'; self.source.mkdir()
        (self.source / 'app.py').write_text('print(1)')
        self.cli = self.root / 'opencode'
        self.cli.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path(' +
                            repr(str(ROOT / 'tests/fixtures/fake_opencode.py')) + ', run_name="__main__")\n')
        self.cli.chmod(0o700)
        self.calls = self.root / 'calls.jsonl'
        self.env = cli_env(self.source) | {'AUDIT_FAKE_CALLS': str(self.calls)}
        self.artifacts = self.root / 'artifacts'; self.artifacts.mkdir(mode=0o700)
        self.meta = {}
        self.server = None
        self.addCleanup(self.close)

    def close(self):
        if self.server:
            server, self.server = self.server, None
            server.close()

    def invoke(self, scenario=None, seconds=5, idle=None):
        if scenario:
            self.env['AUDIT_FAKE_CASE'] = scenario
        name = prepare_environment(self.env, 'study')
        self.server = Server(str(self.cli), self.source, self.env, self.artifacts,
                             Budget(seconds, idle), atomic, self.meta)
        self.server.start()
        self.server.verify_api()
        context = {'branch': 'main', 'source_commit': 'abc'}
        prompt = '# Authoritative orchestration context (data)\n' + json.dumps(context) + '\n\n# Required final JSON Schema\n{}'
        result = self.server.invoke(prompt, SCHEMAS['study'], name, 'chosen/model', 0)
        return result

    def test_machine_schema_and_separate_structured_result_and_private_artifacts(self):
        data = self.invoke()
        validate_result('study', data, {'branch': 'main', 'source_commit': 'abc'})
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        prompts = [c for c in calls if c['method'] == 'POST' and c['path'].endswith('/message')]
        self.assertEqual(len(prompts), 1)
        body = prompts[0]['body']
        self.assertEqual(body['format'], {'type': 'json_schema', 'schema': SCHEMAS['study'], 'retryCount': 0})
        self.assertEqual(body['model'], {'providerID': 'chosen', 'modelID': 'model'})
        self.assertEqual(self.meta['model_actual'], 'chosen/model')
        self.assertNotIn('Ordinary prose', json.dumps(data))
        self.assertEqual(json.loads((self.artifacts / 'extracted.json').read_text()), data)
        self.close()
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([c['method'] for c in calls[-2:]], ['POST', 'DELETE'])
        for file in self.artifacts.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            self.assertNotIn(self.server.authorization if self.server else 'Basic ', file.read_text())

    def test_backend_error_wins_over_structured_object(self):
        with self.assertRaises(ContractError) as caught:
            self.invoke('backend-error')
        self.assertEqual(caught.exception.failure_kind, 'BACKEND_ERROR')
        self.assertNotIn('SECRET_RESPONSE', str(caught.exception))
        self.assertFalse((self.artifacts / 'extracted.json').exists())
        self.assertTrue((self.artifacts / 'response.json').exists())

    def test_malformed_transport_is_not_json_result_failure(self):
        with self.assertRaises(ContractError) as caught:
            self.invoke('malformed')
        self.assertEqual(caught.exception.failure_kind, 'TRANSPORT_ERROR')
        self.assertIn('position', caught.exception.details)

    def test_no_native_result_is_incomplete(self):
        with self.assertRaises(ContractError) as caught:
            self.invoke('prose-only')
        self.assertEqual(caught.exception.failure_kind, 'INCOMPLETE_OUTPUT')

    def test_exhaustion_keeps_reported_counter_without_inventing_attempts(self):
        with self.assertRaises(ContractError) as caught:
            self.invoke('exhausted')
        self.assertEqual(caught.exception.failure_kind, 'STRUCTURED_OUTPUT_EXHAUSTED')
        self.assertEqual(caught.exception.details['native_retries_reported'], 0)

    def test_active_backend_cannot_extend_total_deadline(self):
        with self.assertRaises(ContractError) as caught:
            self.invoke('active', seconds=.7)
        self.assertEqual(caught.exception.failure_kind, 'STAGE_TIMEOUT')
        started = time.monotonic()
        process = self.server.process
        self.close()
        self.assertIsNotNone(process.poll())
        self.assertLess(time.monotonic() - started, 8)

    def test_idle_timeout_ignores_unchanged_poll_responses(self):
        with self.assertRaises(ContractError) as caught:
            self.invoke('slow', seconds=5, idle=.5)
        self.assertEqual(caught.exception.failure_kind, 'IDLE_TIMEOUT')

    def test_not_ready_server_is_bounded(self):
        with self.assertRaises(ContractError) as caught:
            self.invoke('not-ready', seconds=.3)
        self.assertEqual(caught.exception.failure_kind, 'STAGE_TIMEOUT')

    def test_hung_http_is_bounded(self):
        with self.assertRaises(ContractError) as caught:
            self.invoke('http-hang', seconds=.3)
        self.assertEqual(caught.exception.failure_kind, 'STAGE_TIMEOUT')

    def test_envelope_malformed_types_and_identity(self):
        self.invoke()
        envelope = json.loads((self.artifacts / 'response.json').read_text())
        info = envelope['info']
        def extract(value):
            return extract_result(value, info['sessionID'], info['parentID'], info['agent'])
        for value in (None, [], 'text', {}, {'info': None}, {'info': info, 'parts': None}):
            with self.subTest(value=value), self.assertRaises(ContractError) as caught:
                extract(value)
            self.assertEqual(caught.exception.failure_kind, 'TRANSPORT_ERROR')
        for key, value in (('sessionID', 'ses_other'), ('parentID', 'msg_old'), ('id', []), ('agent', 'other')):
            wrong = copy.deepcopy(envelope); wrong['info'][key] = value
            with self.subTest(key=key), self.assertRaises(ContractError):
                extract(wrong)
        for change in ({'finish': 'length'}, {'time': {'created': 1}}, {'finish': 'unknown'}):
            wrong = copy.deepcopy(envelope); wrong['info'].update(change)
            with self.assertRaises(ContractError) as caught:
                extract(wrong)
            self.assertEqual(caught.exception.failure_kind, 'INCOMPLETE_OUTPUT')
        newer = copy.deepcopy(envelope); newer['info']['id'] = 'msg_new'
        newer['info']['time'].pop('completed')
        newer['parts'] = []
        with self.assertRaises(ContractError) as caught:
            validate_history([envelope, newer], info['sessionID'], info['parentID'], info['id'])
        self.assertEqual(caught.exception.failure_kind, 'INCOMPLETE_OUTPUT')

    def test_closed_finish_diagnostics_preserve_existing_success_and_envelope_gates(self):
        self.invoke()
        envelope = json.loads((self.artifacts / 'response.json').read_text())
        info = envelope['info']
        def extract(value):
            return extract_result(value, info['sessionID'], info['parentID'], info['agent'])
        for finish in ('stop', 'tool-calls'):
            value = copy.deepcopy(envelope); value['info']['finish'] = finish
            data, meta = extract(value)
            self.assertEqual(data, info['structured'])
            self.assertEqual(meta['finish_reason'], finish)
            self.assertEqual(meta['finish_classification'], 'SUCCESS')
            for mutation in ('pending', 'mismatch', 'foreign', 'missing-tool', 'missing-native'):
                invalid = copy.deepcopy(value)
                if mutation == 'pending': invalid['parts'][1]['state']['status'] = 'running'
                if mutation == 'mismatch': invalid['parts'][1]['state']['input'] = {}
                if mutation == 'foreign': invalid['info']['sessionID'] = 'ses_foreign'
                if mutation == 'missing-tool': invalid['parts'] = invalid['parts'][:1]
                if mutation == 'missing-native': del invalid['info']['structured']
                with self.subTest(finish=finish, mutation=mutation), self.assertRaises(ContractError): extract(invalid)
        for finish, code in ((None, 'FINISH_INVALID_TYPE'), ([], 'FINISH_INVALID_TYPE'),
                ('length', 'FINISH_TRUNCATED'), ('error', 'FINISH_ERROR'),
                ('SECRET_FINISH\n\x1b[31m', 'FINISH_UNKNOWN'), ('', 'FINISH_UNKNOWN')):
            invalid = copy.deepcopy(envelope); invalid['info']['finish'] = finish
            with self.assertRaises(ContractError) as caught: extract(invalid)
            self.assertEqual(caught.exception.details, {'code': code})
            self.assertNotIn('SECRET_FINISH', caught.exception.safe_message)
        invalid = copy.deepcopy(envelope); del invalid['info']['finish']
        with self.assertRaises(ContractError) as caught: extract(invalid)
        self.assertEqual(caught.exception.details['code'], 'FINISH_MISSING')
        changed_history = copy.deepcopy(envelope)
        changed_history['parts'][1]['state']['status'] = 'pending'
        with self.assertRaises(ContractError) as caught:
            validate_history([changed_history], info['sessionID'], info['parentID'], info['id'], envelope)
        self.assertEqual(caught.exception.details['code'], 'FINAL_SNAPSHOT_MISMATCH')

    def config(self):
        return {'result_policy': 'strict', 'mode': 'folder', 'folder_mode': {'path': str(self.source)},
            'reports_dir': str(self.root / 'reports'), 'project_description': 'fixture',
            'priority_scenarios': [], 'output_language': 'English', 'continue_on_error': True,
            '_agents': {s: {'backend': 'opencode', 'executable': str(self.cli), 'model': None} for s in ('catalog', 'study', 'review')},
            '_prompt_paths': {s: str(ROOT / 'prompts' / (s + '.md')) for s in ('catalog', 'study', 'review', 'revise')}}

    def test_real_preflight_rejects_unenforced_retries_without_model_request(self):
        for repairs in (0, 1, 2):
            config = self.config()
            config['execution'] = {'structured_output_repair_attempts': repairs}
            runner = Runner(config, self.root / f'preflight-{repairs}')
            result, code = runner.run(check_only=True)
            self.assertEqual(code, 1)
            self.assertEqual(result['diagnostics'][0]['code'], 'BACKEND_INCOMPATIBLE')
            self.assertFalse(self.calls.exists())

    def test_wire_pipeline_only_with_explicit_test_gate_bypass(self):
        # This bypass is confined to this test. It proves no upstream retry behavior.
        with patch('src.backends.opencode.verify_native_retries'), patch.dict(os.environ, self.env):
            runner = Runner(self.config(), self.root / 'pipeline')
            result, code = runner.run()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['accepted'])
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        prompts = [c for c in calls if c['method'] == 'POST' and c['path'].endswith('/message')]
        self.assertEqual(len(prompts), 3)
        self.assertNotEqual(prompts[0]['path'], prompts[1]['path'])
        contexts = [json.loads(p['body']['parts'][0]['text'].split('# Authoritative orchestration context (data)\n')[1]
                              .split('\n\n# Required final JSON Schema')[0]) for p in prompts]
        self.assertNotIn('architecture_document', contexts[0])
        self.assertEqual(contexts[2]['architecture_document']['report_markdown'], result['study']['report_markdown'])
        self.assertNotIn('claims', contexts[2]['architecture_document'])
        self.assertEqual([c['id'] for c in contexts[2]['claim_registry']], [c['id'] for c in result['study']['claims']])

    def test_signal_cleans_only_owned_server_and_preserves_exit_code(self):
        config = {'mode': 'folder', 'folder_mode': {'path': str(self.source)},
                  'reports_dir': str(self.root / 'reports'), 'project_description': 'fixture',
                  'agent': {'backend': 'opencode', 'executable': str(self.cli)}}
        path = self.root / 'config.json'; path.write_text(json.dumps(config))
        # The subprocess is test-owned. No installed OpenCode or user profile is used.
        bootstrap = ('import sys; sys.path.insert(0,sys.argv.pop(1)); '
                     'from src.backends import opencode; opencode.verify_native_retries=lambda:None; '
                     'import explain; sys.exit(explain.main())')
        bystander = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            for sig in ('SIGINT', 'SIGTERM'):
                env = self.env | {'AUDIT_FAKE_CASE': 'interrupt', 'AUDIT_FAKE_SIGNAL': sig}
                completed = subprocess.run([sys.executable, '-B', '-c', bootstrap, str(ROOT),
                    '--config', str(path)], env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(completed.returncode, 130, completed.stderr)
                manifest = json.loads(Path(json.loads(completed.stdout)['manifest']).read_text())
                self.assertFalse(manifest['accepted'])
                self.assertEqual(manifest['diagnostics'][0]['code'], 'INTERRUPTED')
                calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
                for pid in {c['server_pid'] for c in calls}:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                self.assertIsNone(bystander.poll())
        finally:
            bystander.terminate(); bystander.wait(timeout=2)


class CapabilityTests(unittest.TestCase):
    def test_unverified_version_and_native_retry_enforcement_fail_closed(self):
        for version in ('1.18.33', '0.0.0', None, '1.2.27-custom'):
            with self.assertRaises(ContractError) as caught:
                verify_version(version)
            self.assertEqual(caught.exception.failure_kind, 'BACKEND_INCOMPATIBLE')
        verify_version('1.2.27')
        with self.assertRaises(ContractError):
            verify_native_retries()

    def test_permissions_and_user_profile_are_preserved(self):
        original = {'provider': {'private': {'options': {'baseURL': 'https://example.invalid'}}},
                    'plugin': ['auth-plugin'], 'model': 'configured/model', 'agent': {'custom': {'mode': 'primary'}}}
        for stage in ('catalog', 'study', 'review', 'compare'):
            env = {'OPENCODE_CONFIG_CONTENT': json.dumps(original), 'HOME': '/home/profile'}
            name = prepare_environment(env, stage)
            overlay = json.loads(env['OPENCODE_CONFIG_CONTENT'])
            custom = overlay['agent'].pop(name)
            self.assertEqual(overlay, original)
            permissions = {'*': 'deny', 'StructuredOutput': 'allow'}
            if stage != 'compare':
                permissions.update(read='allow', glob='allow', grep='allow', list='allow')
            self.assertEqual(custom['permission'], permissions)
            self.assertEqual(env['HOME'], '/home/profile')


class GitHTTPPipelineTests(unittest.TestCase):
    setUp = HTTPFixture.setUp
    close = HTTPFixture.close
    config = HTTPFixture.config

    def test_multiple_branches_http_pipeline_and_reports_only_compare(self):
        repo = self.source
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
        git('init', '-b', 'main')
        git('config', 'user.name', 'Fixture'); git('config', 'user.email', 'fixture@example.invalid')
        git('add', '.'); git('commit', '-m', 'initial')
        original = git('rev-parse', 'HEAD')
        git('branch', 'topic')
        config = self.config()
        config.update(mode='git', git_mode={'repository': str(repo), 'branches': ['main', 'topic'], 'baseline_branch': 'main'})
        config['_agents']['compare'] = dict(config['_agents']['study'])
        config['_prompt_paths']['compare'] = str(ROOT / 'prompts/compare.md')
        # The orchestrator's comparison cwd must not follow an inherited TMPDIR
        # back into the inspected tree. Child profile variables remain preserved.
        self.env['TMPDIR'] = str(repo)
        with patch('src.backends.opencode.verify_native_retries'), patch.dict(os.environ, self.env):
            runner = Runner(config, self.root / 'git-pipeline')
            self.addCleanup(runner.repo.close)
            manifest, code = runner.run()
        self.assertEqual(code, 0, manifest)
        self.assertTrue(manifest['restoration']['restored'])
        self.assertEqual(git('rev-parse', 'HEAD'), original)
        self.assertEqual(git('symbolic-ref', '--short', 'HEAD'), 'main')
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        prompts = [c for c in calls if c['method'] == 'POST' and c['path'].endswith('/message')]
        self.assertEqual(len(prompts), 7)
        self.assertEqual(len({c['path'] for c in prompts}), 7)
        self.assertNotEqual(prompts[-1]['cwd'], str(repo))
        self.assertTrue(all(c['cwd'] == str(repo) for c in prompts[:-1]))


if __name__ == '__main__':
    unittest.main()
