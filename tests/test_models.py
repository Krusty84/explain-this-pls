# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline model catalogs, explicit access checks and surgical config edits."""
from argparse import Namespace
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from textual.widgets import Button, DataTable, Select, Static

from explain import load_config, main
from src.backends import codex_models
from src.contracts.contracts import jsonc
from src.tui.app import ExplainApp
from src.tui.config_edit import replace_model, save_model
from src.tui.models import ModelChooser

ROOT = Path(__file__).resolve().parents[1]
MODELS = [dict(slug='public-model', display_name='Public', visibility='list', supported_in_api=False),
          dict(slug='hidden-model', display_name='Hidden [bold]', visibility='hide', supported_in_api=True)]


class ModelFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.cli = self.base / 'codex with spaces'
        self.cli.write_text('#!' + sys.executable + '\n' + (ROOT / 'tests/fixtures/model_cli.py').read_text())
        self.cli.chmod(0o700)
        self.state_path, self.calls_path = self.base / 'state.json', self.base / 'calls.jsonl'
        self.state = {'models': MODELS}
        self.write_state()
        environment = patch.dict(os.environ, {'EXPLAIN_TEST_MODELS_STATE': str(self.state_path),
                                             'EXPLAIN_TEST_MODELS_CALLS': str(self.calls_path)})
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.base / 'project.jsonc'
        self.config = {'mode': 'folder', 'folder_mode': {'path': './source'}, 'reports_dir': './reports',
                       'agent': {'backend': 'codex', 'executable': './' + self.cli.name}}
        self.write_config()

    def write_state(self, **changes):
        self.state.update(changes)
        self.state_path.write_text(json.dumps(self.state))

    def write_config(self):
        self.path.write_text(json.dumps(self.config, indent=2) + '\n')

    def calls(self):
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()] if self.calls_path.exists() else []


class ModelCLITests(ModelFixture, unittest.TestCase):
    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch('explain.run_audit') as audit:
            code = main(['--config', str(self.path), '--models', *arguments])
            audit.assert_not_called()
        self.assertFalse((self.base / 'reports').exists())
        return code, stdout.getvalue(), stderr.getvalue()

    def test_json_auto_and_text_do_not_probe_or_create_reports(self):
        original = self.path.read_bytes()
        for arguments in ([], ['--output', 'json'], ['--output', 'text']):
            code, stdout, stderr = self.invoke(*arguments)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(stderr, '')
            if arguments[-1:] == ['text']:
                self.assertIn('Account access has not been checked', stdout)
                self.assertIn('hidden-model', stdout)
                self.assertIn('Supported in API', stdout)
            else:
                group, = json.loads(stdout)['catalogs']
                self.assertEqual(group, {'executable': str(self.cli), 'stages': ['study'], 'models': MODELS})
        self.assertEqual([call['args'] for call in self.calls()], [['debug', 'models']] * 3)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(all(call['cwd'] != str(self.base) for call in self.calls()))

    def test_unique_executables_and_mixed_backends(self):
        second = self.base / 'second-codex'
        second.write_bytes(self.cli.read_bytes())
        second.chmod(0o700)
        self.config.update(mode='git', git_mode={'repository': './source', 'branches': ['main', 'other'], 'baseline_branch': 'main'},
                           execution={'review_enabled': True}, stage_agents={'review': {'model': 'review'}, 'compare': {'executable': './second-codex'}})
        self.write_config()
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        groups = json.loads(stdout)['catalogs']
        self.assertEqual([group['stages'] for group in groups], [['study', 'review'], ['compare']])
        self.assertEqual(len(self.calls()), 2)
        self.config['stage_agents']['review'].update(backend='xxx', executable=sys.executable)
        self.write_config()
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)['catalogs'][0]['stages'], ['study'])

    def test_non_codex_invalid_catalog_and_conflicting_flags(self):
        self.config['agent'] = {'backend': 'xxx', 'executable': sys.executable}
        self.write_config()
        code, stdout, stderr = self.invoke()
        self.assertEqual((code, stdout), (1, ''))
        self.assertIn('no active Codex', stderr)
        self.assertEqual(self.calls(), [])
        self.config['agent'] = {'backend': 'codex', 'executable': str(self.cli)}
        self.write_config()
        self.write_state(catalog_output='not JSON')
        code, stdout, stderr = self.invoke()
        self.assertEqual((code, stdout), (1, ''))
        self.assertIn('invalid model catalog', stderr)
        for arguments in (['--models'], ['--models', '--check'], ['--models', '--tui']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(arguments)
            self.assertEqual(error.exception.code, 2)

    def test_sigterm_stops_catalog_process_and_exits_without_reports(self):
        marker = self.base / 'process.json'
        self.write_state(delay_catalog=60, marker=str(marker), spawn_child=True)
        child = subprocess.Popen([sys.executable, '-B', str(ROOT / 'explain.py'), '--config', str(self.path), '--models'],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            child.send_signal(signal.SIGTERM)
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 130, stderr.decode())
            self.assertEqual(stdout, b'')
            self.assertFalse((self.base / 'reports').exists())
            ModelBackendTests.assert_process_stopped(self, json.loads(marker.read_text()))
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()
            if marker.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(json.loads(marker.read_text())['pid'], signal.SIGKILL)


class ModelBackendTests(ModelFixture, unittest.IsolatedAsyncioTestCase):
    async def test_catalog_keeps_hidden_and_api_false_ignores_instruction_fields(self):
        self.write_state(models=[dict(model, base_instructions='Never expose these instructions.') for model in MODELS])
        self.assertEqual(await codex_models.catalog(str(self.cli)), MODELS)
        self.assertEqual([call['args'] for call in self.calls()], [['debug', 'models']])

    async def test_invalid_and_duplicate_catalogs(self):
        for value in ('[]', '{"models": null}', '{"models": [1]}',
                      json.dumps({'models': [MODELS[0], MODELS[0]]}),
                      json.dumps({'models': [dict(MODELS[0], supported_in_api='yes')]})):
            self.write_state(catalog_output=value)
            with self.assertRaises(ValueError):
                await codex_models.catalog(str(self.cli))
        self.write_state(catalog_exit=1, catalog_output='command not supported')
        with self.assertRaisesRegex(ValueError, 'Cannot load'):
            await codex_models.catalog(str(self.cli))

    async def test_access_probe_uses_chatgpt_read_only_ephemeral_and_selected_model(self):
        result = await codex_models.check_model(str(self.cli), 'hidden-model')
        self.assertEqual(result['status'], 'available')
        login, probe = [call['args'] for call in self.calls()]
        self.assertEqual(login, ['login', 'status'])
        self.assertEqual(probe[0], 'exec')
        self.assertIn('--ephemeral', probe)
        self.assertIn('--skip-git-repo-check', probe)
        self.assertEqual(probe[probe.index('--sandbox') + 1], 'read-only')
        self.assertEqual(probe[probe.index('--model') + 1], 'hidden-model')
        self.assertIn('approval_policy="never"', probe)
        self.assertIn('--json', probe)
        self.assertIn('Не используй инструменты.', probe[-1])

    async def test_only_explicit_unsupported_error_means_unavailable(self):
        for status, expected in [('unavailable', 'unavailable'), ('error', 'unverified'),
                                 ('malformed', 'unverified'), ('failed_event', 'unverified'), ('incomplete', 'unverified')]:
            self.write_state(probe=status)
            self.assertEqual((await codex_models.check_model(str(self.cli), 'public-model'))['status'], expected)
        self.write_state(probe='available', reply='')
        self.assertEqual((await codex_models.check_model(str(self.cli), 'public-model'))['status'], 'unverified')

    async def test_api_login_does_not_probe(self):
        self.write_state(login='Logged in using an API key')
        result = await codex_models.check_model(str(self.cli), 'public-model')
        self.assertEqual(result['status'], 'unverified')
        self.assertIn('Sign in with ChatGPT', result['message'])
        self.assertEqual([call['args'] for call in self.calls()], [['login', 'status']])

    async def test_timeout_and_cancellation_stop_process_groups(self):
        marker = self.base / 'process.json'
        create, children = asyncio.create_subprocess_exec, []

        async def spawn(*args, **kwargs):
            child = await create(*args, **kwargs)
            children.append(child)
            return child

        with patch('src.backends.codex_models.asyncio.create_subprocess_exec', new=spawn):
            self.write_state(delay_catalog=60, marker=str(marker), spawn_child=True)
            with self.assertRaisesRegex(ValueError, 'time limit'):
                await codex_models.catalog(str(self.cli), timeout=.3)
            if marker.exists():
                self.assert_process_stopped(json.loads(marker.read_text()))
            marker.unlink(missing_ok=True)
            self.write_state(delay_catalog=0, delay_probe=60)
            result = await codex_models.check_model(str(self.cli), 'public-model', timeout=.3)
            self.assertEqual(result['status'], 'unverified')
            self.assertIn('time limit', result['message'])
            if marker.exists():
                self.assert_process_stopped(json.loads(marker.read_text()))
        self.assertTrue(children)
        self.assertTrue(all(child.returncode is not None for child in children))
        marker.unlink(missing_ok=True)
        task = asyncio.create_task(codex_models.check_model(str(self.cli), 'public-model'))
        for _ in range(500):
            if marker.exists():
                break
            await asyncio.sleep(.01)
        self.assertTrue(marker.exists())
        processes = json.loads(marker.read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_process_stopped(processes)

    async def test_cancellation_while_spawning_reaps_the_child(self):
        self.write_state(delay_catalog=60, marker=str(self.base / 'process.json'))
        create = asyncio.create_subprocess_exec
        started, release = asyncio.Event(), asyncio.Event()
        children = []

        async def spawn(*args, **kwargs):
            child = await create(*args, **kwargs)
            children.append(child)
            started.set()
            await release.wait()
            return child

        with patch('src.backends.codex_models.asyncio.create_subprocess_exec', side_effect=spawn):
            task = asyncio.create_task(codex_models.catalog(str(self.cli)))
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)

    def assert_process_stopped(self, processes):
        for pid in processes.values():
            if pid:
                result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
                self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith('Z'), result.stdout)


class ModelConfigTests(unittest.TestCase):
    def test_replacement_preserves_comments_strings_spacing_and_crlf(self):
        text = '{\r\n\t// "model": "fake"\r\n\t"agent": {"backend":"codex", "model" : null /*keep*/},\r\n' \
               '\t"project_description":"escaped \\" model and // comment",\r\n\t"reports_dir":"../reports",\r\n}\r\n'
        updated = replace_model(text, 'agent.model', 'hidden-model')
        self.assertEqual(updated, text.replace('null /*keep*/', '"hidden-model" /*keep*/'))
        self.assertEqual(jsonc(updated)['agent']['model'], 'hidden-model')

    def test_insert_missing_model_and_stage_sections_preserves_existing_values(self):
        texts = ['{"agent":{"backend":"codex"}}', '{"agent": { /*keep*/ }}',
                 '{\n  "agent": {\n    "backend": "codex" // keep with backend\n  }\n}\n',
                 '{\n  "agent": {\n    "backend": "codex", /* trailing comma */\n  },\n}\n',
                 '{"agent":{"backend":"codex"},"stage_agents":{"review":{}}}']
        for text in texts:
            for field in ('agent.model', 'stage_agents.study.model', 'stage_agents.review.model'):
                with self.subTest(text=text, field=field):
                    original = jsonc(text)
                    updated = replace_model(text, field, 'selected')
                    expected = json.loads(json.dumps(original))
                    section = expected
                    for key in field.split('.')[:-1]:
                        section = section.setdefault(key, {})
                    section['model'] = 'selected'
                    self.assertEqual(jsonc(updated), expected)
                    if '// keep with backend' in text:
                        comma = ',' if field == 'agent.model' else ''
                        self.assertIn('"backend": "codex"' + comma + ' // keep with backend', updated)
                    if '/* trailing comma */' in text:
                        self.assertIn('/* trailing comma */', updated)
        for field in ('agent.backend', 'stage_agents.revise.model'):
            with self.assertRaises(ValueError):
                replace_model(texts[0], field, 'selected')

    def test_atomic_save_rejects_external_changes_and_keeps_other_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.jsonc'
            original = b'{"agent":{"backend":"codex", "model":"old"}, "reports_dir":"../reports"}\r\n'
            path.write_bytes(original)
            save_model(path, 'agent.model', 'new', original)
            self.assertEqual(path.read_bytes(), original.replace(b'"old"', b'"new"'))
            external = path.read_bytes() + b'// external edit\r\n'
            path.write_bytes(external)
            with self.assertRaisesRegex(ValueError, 'changed.*Refresh'):
                save_model(path, 'agent.model', 'another', original)
            self.assertEqual(path.read_bytes(), external)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])


class ModelTUITests(ModelFixture, unittest.IsolatedAsyncioTestCase):
    def app(self):
        args = Namespace(config=self.path, tui=True, check=False, models=False, trust_repository=False,
                         output='auto', verbose=False, no_progress=True)
        return ExplainApp(args, settings_path=self.base / 'explain.config')

    async def ready(self, pilot, screen):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not screen.busy:
                await pilot.pause(.35)
                return
            await pilot.pause(.01)
        self.fail('Model operation did not finish: ' + str(screen.query_one('#model-status', Static).content)
                  + ' | Tasks: ' + repr(screen.tasks) + ' | Calls: ' + repr(self.calls()))

    async def test_slow_catalog_load_and_cancel_keep_ui_responsive(self):
        marker = self.base / 'process.json'
        self.write_state(delay_catalog=1.2, marker=str(marker))
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.click('#models')
            screen = app.screen
            self.assertTrue(screen.busy)
            self.assertTrue(screen.query_one('#model-apply', Button).disabled)
            await self.ready(pilot, screen)
            self.assertEqual(screen.query_one('#model-table', DataTable).row_count, 2)
            self.write_state(delay_catalog=60, spawn_child=True)
            marker.unlink()
            await pilot.click('#model-refresh')
            for _ in range(500):
                if marker.exists():
                    break
                await pilot.pause(.01)
            self.assertTrue(marker.exists())
            await pilot.click('#model-close')
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ModelChooser)
        ModelBackendTests.assert_process_stopped(self, json.loads(marker.read_text()))

    async def test_hidden_model_requires_manual_check_then_updates_preview(self):
        original = self.path.read_text()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(self.calls(), [])
            await pilot.click('#models')
            screen = app.screen
            self.assertIsInstance(screen, ModelChooser)
            await self.ready(pilot, screen)
            self.assertEqual([call['args'] for call in self.calls()], [['debug', 'models']])
            table = screen.query_one('#model-table', DataTable)
            self.assertEqual(table.row_count, 2)
            self.assertTrue(screen.query_one('#model-apply', Button).disabled)
            table.focus()
            await pilot.press('down')
            self.assertEqual(screen.selected, 'hidden-model')
            await pilot.click('#model-check')
            await self.ready(pilot, screen)
            self.assertFalse(screen.query_one('#model-apply', Button).disabled)
            await pilot.click('#model-apply')
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ModelChooser)
            self.assertEqual(jsonc(self.path.read_text())['agent']['model'], 'hidden-model')
            self.assertIn('hidden-model', str(app.query_one('#config-preview', Static).content))
        self.assertIn('"path": "./source"', self.path.read_text())
        self.assertIn('"reports_dir": "./reports"', self.path.read_text())
        self.assertNotEqual(original, self.path.read_text())
        self.assertFalse((self.base / 'reports').exists())

    async def test_stage_target_refresh_resets_checks_and_detects_external_change(self):
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.click('#models')
            screen = app.screen
            await self.ready(pilot, screen)
            await pilot.click('#model-check')
            await self.ready(pilot, screen)
            screen.query_one('#model-target', Select).value = 'stage_agents.study.model'
            await pilot.pause()
            await self.ready(pilot, screen)
            self.assertTrue(screen.query_one('#model-apply', Button).disabled)
            await pilot.click('#model-check')
            await self.ready(pilot, screen)
            external = self.path.read_bytes() + b'// external\n'
            self.path.write_bytes(external)
            await pilot.click('#model-apply')
            self.assertEqual(self.path.read_bytes(), external)
            self.assertIn('Refresh', str(screen.query_one('#model-status', Static).content))
            await pilot.click('#model-refresh')
            await self.ready(pilot, screen)
            self.assertTrue(screen.query_one('#model-apply', Button).disabled)
            await pilot.click('#model-check')
            await self.ready(pilot, screen)
            await pilot.click('#model-apply')
            await pilot.pause()
        value = jsonc(self.path.read_text())
        self.assertEqual(value['stage_agents']['study']['model'], 'public-model')
        self.assertNotIn('model', value['agent'])
        self.assertTrue(self.path.read_bytes().endswith(b'// external\n'))
        self.assertEqual(load_config(self.path)['_agents']['study']['model'], 'public-model')

    async def test_unavailable_error_and_timeout_never_enable_apply(self):
        original = self.path.read_bytes()
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.click('#models')
            screen = app.screen
            await self.ready(pilot, screen)
            for status, expected in [('unavailable', 'unavailable'), ('error', 'unverified')]:
                self.write_state(probe=status)
                await pilot.click('#model-check')
                await self.ready(pilot, screen)
                self.assertEqual(screen.verifications['public-model']['status'], expected)
                self.assertTrue(screen.query_one('#model-apply', Button).disabled)
            check = codex_models.check_model
            self.write_state(probe='available', delay_probe=60, marker=str(self.base / 'process.json'))

            async def timed_check(*args):
                return await check(*args, timeout=.2)

            with patch('src.tui.models.codex_models.check_model', new=timed_check):
                await pilot.click('#model-check')
                await self.ready(pilot, screen)
            self.assertIn('time limit', str(screen.query_one('#model-status', Static).content))
            self.assertTrue(screen.query_one('#model-apply', Button).disabled)
            await pilot.press('escape')
            await pilot.pause()
        self.assertEqual(self.path.read_bytes(), original)

    async def test_cancel_stops_pending_check_and_keeps_config(self):
        original = self.path.read_bytes()
        marker = self.base / 'process.json'
        self.write_state(delay_probe=60, marker=str(marker), spawn_child=True)
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.click('#models')
            screen = app.screen
            await self.ready(pilot, screen)
            await pilot.click('#model-check')
            for _ in range(500):
                if marker.exists():
                    break
                await pilot.pause(.01)
            self.assertTrue(marker.exists())
            await pilot.press('ctrl+c')
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ModelChooser)
        ModelBackendTests.assert_process_stopped(self, json.loads(marker.read_text()))
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
