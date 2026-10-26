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

from textual.widgets import Button, DataTable, OptionList, Select, Static

from src.runtime.config import load_config
from src.cli import main
from src.backends import codex_models
from src.contracts.contracts import jsonc
from src.tui.app import ExplainApp
from src.tui.config_edit import replace_model, save_model
from src.tui.models import ModelChooser, model_targets

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
                       'execution': {'review_enabled': False},
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
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch('src.cli.run_audit') as audit:
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
                self.assertIn('public-model', stdout)
                self.assertNotIn('hidden-model', stdout)
                self.assertIn('Supported in API', stdout)
            else:
                group, = json.loads(stdout)['catalogs']
                self.assertEqual(group, {'executable': str(self.cli), 'stages': ['study'], 'models': MODELS[:1]})
        self.assertEqual([call['args'] for call in self.calls()], [['debug', 'models']] * 3)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(all(call['cwd'] != str(self.base) for call in self.calls()))

    def test_all_hidden_catalog_is_empty_in_text_and_json(self):
        self.write_state(models=MODELS[1:])
        for output in ('text', 'json'):
            code, stdout, stderr = self.invoke('--output', output)
            self.assertEqual(code, 0, stderr)
            self.assertNotIn('hidden-model', stdout)
            if output == 'text':
                self.assertIn('No models returned.', stdout)
            else:
                self.assertEqual(json.loads(stdout)['catalogs'][0]['models'], [])
        self.assertEqual([call['args'] for call in self.calls()], [['debug', 'models']] * 2)

    def test_unique_executables_group_stage_catalogs(self):
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

    def test_unsupported_backend_invalid_catalog_and_conflicting_flags(self):
        self.config['agent'] = {'backend': 'xxx', 'executable': sys.executable}
        self.write_config()
        code, stdout, stderr = self.invoke()
        self.assertEqual((code, stdout), (1, ''))
        self.assertIn('Unsupported backend: xxx', stderr)
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
    async def test_catalog_filters_hidden_keeps_api_false_and_ignores_instruction_fields(self):
        self.write_state(models=[dict(model, base_instructions='Never expose these instructions.') for model in MODELS])
        self.assertEqual(await codex_models.catalog(str(self.cli)), MODELS[:1])
        self.assertEqual([call['args'] for call in self.calls()], [['debug', 'models']])

    async def test_catalog_keeps_unknown_visibility_and_api_support(self):
        self.write_state(models=[{'slug': 'unknown-model'}, dict(MODELS[1], visibility='HIDE')])
        self.assertEqual(await codex_models.catalog(str(self.cli)), [
            dict(slug='unknown-model', display_name='unknown-model', visibility='unknown', supported_in_api=None),
            dict(MODELS[1], visibility='HIDE')])

    async def test_invalid_and_duplicate_catalogs(self):
        for value in ('[]', '{"models": null}', '{"models": [1]}',
                      json.dumps({'models': [MODELS[0], MODELS[0]]}),
                      json.dumps({'models': [MODELS[1], MODELS[1]]}),
                      json.dumps({'models': [dict(MODELS[0], supported_in_api='yes')]}),
                      json.dumps({'models': [dict(MODELS[1], supported_in_api='yes')]})):
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
    def test_model_edit_preserves_effort_in_shared_and_stage_sections(self):
        for field in ('agent.model', 'stage_agents.study.model',
                      'stage_agents.review.model', 'stage_agents.compare.model'):
            for effort in ('high', None):
                section = '{"model": "old", /* keep effort */ "reasoning_effort": ' + json.dumps(effort) + '}'
                text = section
                for key in reversed(field.split('.')[:-1]):
                    text = '{"' + key + '": ' + text + '}'
                with self.subTest(field=field, effort=effort):
                    self.assertEqual(replace_model(text, field, 'new'), text.replace('"old"', '"new"'))

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
            for field in ('agent.model', 'stage_agents.study.model', 'stage_agents.review.model', 'stage_agents.compare.model'):
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


class ModelTargetTests(ModelFixture, unittest.TestCase):
    def test_all_codex_stages_inherit_shared_settings_when_inactive(self):
        config = load_config(self.path)
        targets = model_targets(config, self.path)
        self.assertEqual(list(targets), ['agent.model', 'stage_agents.study.model',
                                        'stage_agents.review.model', 'stage_agents.compare.model'])
        self.assertTrue(all(agent['executable'] == str(self.cli) for agent in targets.values()))
        self.assertEqual(list(config['_agents']), ['study'])
        self.assertFalse(config['execution']['review_enabled'])

    def test_inactive_codex_overrides_use_their_executable_and_model(self):
        self.config['agent'] = {'backend': 'codex', 'executable': sys.executable, 'model': 'shared'}
        self.config['stage_agents'] = {'review': {'backend': 'codex', 'executable': './' + self.cli.name, 'model': 'review'},
                                       'compare': {'model': 'compare'}}
        self.write_config()
        targets = model_targets(load_config(self.path), self.path)
        shared = {'backend': 'codex', 'executable': str(Path(sys.executable).resolve()), 'model': 'shared'}
        self.assertEqual(targets, {'agent.model': shared, 'stage_agents.study.model': shared,
            'stage_agents.review.model': {'backend': 'codex', 'executable': str(self.cli), 'model': 'review'},
            'stage_agents.compare.model': shared | {'model': 'compare'}})


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
            self.assertEqual(screen.query_one('#model-table', DataTable).row_count, 1)
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

    async def test_hidden_current_model_selects_visible_model_and_requires_manual_check(self):
        self.config['agent']['model'] = 'hidden-model'
        self.config['agent']['reasoning_effort'] = 'high'
        self.write_config()
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
            self.assertEqual(table.row_count, 1)
            self.assertEqual([column.label.plain for column in table.columns.values()],
                             ['Model', 'Name', 'Visibility', 'Access via API', 'Access via ChatGPT'])
            self.assertEqual(str(screen.query_one('#model-file', Static).content), 'Config: ' + str(self.path))
            self.assertEqual(str(screen.query_one('#model-scope', Static).content), 'Use for')
            self.assertEqual(str(screen.query_one('#model-apply', Button).label), 'Use')
            select = screen.query_one('#model-target', Select)
            self.assertEqual(select.value, 'agent.model')
            self.assertEqual([option.prompt for option in select.query_one(OptionList).options],
                             ['global', 'study', 'review', 'compare'])
            self.assertEqual(str(screen.query_one('#model-check', Button).label), 'Check ChatGPT Access')
            self.assertIn('Check ChatGPT Access', str(screen.query_one('#model-status', Static).content))
            for button in screen.query(Button):
                self.assertTrue(screen.query_one('#model-actions').content_region.contains_region(button.region))
                self.assertGreaterEqual(button.content_region.width, len(str(button.label)))
            self.assertTrue(screen.query_one('#model-apply', Button).disabled)
            table.focus()
            await pilot.press('down')
            self.assertEqual(screen.selected, 'public-model')
            await pilot.click('#model-check')
            await self.ready(pilot, screen)
            self.assertFalse(screen.query_one('#model-apply', Button).disabled)
            await pilot.click('#model-apply')
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ModelChooser)
            self.assertEqual(jsonc(self.path.read_text())['agent']['model'], 'public-model')
            self.assertEqual(jsonc(self.path.read_text())['agent']['reasoning_effort'], 'high')
            self.assertEqual(app.query_one('#configs', DataTable).get_row_at(0)[3].plain, 'public-model')
        self.assertIn('"path": "./source"', self.path.read_text())
        self.assertIn('"reports_dir": "./reports"', self.path.read_text())
        self.assertNotEqual(original, self.path.read_text())
        self.assertFalse((self.base / 'reports').exists())

    async def test_hidden_only_and_empty_catalog_disable_check_and_apply(self):
        original = self.path.read_bytes()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click('#models')
            screen = app.screen
            await self.ready(pilot, screen)
            for models in (MODELS[1:], []):
                self.write_state(models=models)
                await pilot.click('#model-refresh')
                await self.ready(pilot, screen)
                self.assertEqual(screen.query_one('#model-table', DataTable).row_count, 0)
                self.assertIsNone(screen.selected)
                self.assertTrue(screen.query_one('#model-check', Button).disabled)
                self.assertTrue(screen.query_one('#model-apply', Button).disabled)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(all(call['args'] == ['debug', 'models'] for call in self.calls()))

    async def test_apply_creates_inactive_review_and_compare_without_enabling_them(self):
        for stage in ('review', 'compare'):
            with self.subTest(stage=stage):
                self.write_config()
                original = self.path.read_text() + '// keep\n'
                self.path.write_text(original)
                app = self.app()
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.click('#models')
                    screen = app.screen
                    await self.ready(pilot, screen)
                    screen.query_one('#model-target', Select).value = 'stage_agents.' + stage + '.model'
                    await pilot.pause()
                    await self.ready(pilot, screen)
                    await pilot.click('#model-check')
                    await self.ready(pilot, screen)
                    await pilot.click('#model-apply')
                    await pilot.pause()
                    self.assertNotIsInstance(app.screen, ModelChooser)
                self.assertEqual(jsonc(self.path.read_text()),
                                 dict(self.config, stage_agents={stage: {'model': 'public-model'}}))
                self.assertTrue(self.path.read_text().endswith('// keep\n'))
                config = load_config(self.path)
                self.assertFalse(config['execution']['review_enabled'])
                self.assertEqual(list(config['_agents']), ['study'])

    async def test_stage_target_refresh_resets_checks_and_detects_external_change(self):
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.click('#models')
            screen = app.screen
            await self.ready(pilot, screen)
            await pilot.click('#model-check')
            await self.ready(pilot, screen)
            select = screen.query_one('#model-target', Select)
            select.focus()
            await pilot.press('enter', 'down', 'enter')
            await pilot.pause()
            await self.ready(pilot, screen)
            self.assertEqual(select.value, 'stage_agents.study.model')
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
            self.assertEqual(select.value, 'stage_agents.study.model')
            self.assertEqual([option.prompt for option in select.query_one(OptionList).options],
                             ['global', 'study', 'review', 'compare'])
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
