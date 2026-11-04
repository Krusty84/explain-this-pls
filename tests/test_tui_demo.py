# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline demo navigation, process isolation, argument routing and cleanup."""
import contextlib
import io
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button, DataTable, Static, TabbedContent

from src.cli import main
from src.tui.demo import DemoApp, launch_demo
from test_tui import options


class DemoRoutingTests(unittest.TestCase):
    def test_demo_routes_to_its_launcher_without_an_audit(self):
        for arguments in (['--demo'], ['--tui', '--demo', '--verbose', '--no-progress', '--output', 'auto']):
            with self.subTest(arguments=arguments), patch('sys.stdin.isatty', return_value=True), \
                    patch('sys.stdout.isatty', return_value=True), patch.dict(os.environ, {'TERM': 'xterm'}), \
                    patch('src.tui.demo.launch_demo', return_value=0) as launch, patch('src.cli.run_audit') as audit:
                self.assertEqual(main(arguments), 0)
                launch.assert_called_once()
                audit.assert_not_called()

    def test_conflicts_and_noninteractive_terminals_do_not_launch(self):
        conflicts = (['--config', 'config.json'], ['--check'], ['--models'], ['--trust-repository'],
                     ['--output', 'text'], ['--output', 'json'])
        cases = [(flags, True, True, 'xterm', '--demo cannot be combined') for flags in conflicts]
        cases += [([], False, True, 'xterm', 'interactive terminal'),
                  ([], True, False, 'xterm', 'interactive terminal'),
                  ([], True, True, 'dumb', 'interactive terminal')]
        for flags, stdin, stdout, term, message in cases:
            with self.subTest(flags=flags, stdin=stdin, stdout=stdout, term=term), \
                    patch('sys.stdin.isatty', return_value=stdin), patch('sys.stdout.isatty', return_value=stdout), \
                    patch.dict(os.environ, {'TERM': term}), contextlib.redirect_stderr(io.StringIO()) as stderr, \
                    patch('src.tui.demo.launch_demo') as launch, patch('src.cli.run_audit') as audit, \
                    self.assertRaises(SystemExit) as error:
                main(['--demo', *flags])
            self.assertEqual(error.exception.code, 2)
            self.assertIn(message, stderr.getvalue())
            launch.assert_not_called()
            audit.assert_not_called()

    def test_launcher_removes_samples_after_return_and_exception(self):
        for fail in (False, True):
            directories = []

            def run(args, *, app):
                directories.append(app.demo_directory)
                self.assertTrue(app.settings_path.is_file())
                app.presenter.close()
                if fail:
                    raise RuntimeError('UI failed')
                return 0

            with self.subTest(fail=fail), patch('src.tui.demo.launch', side_effect=run):
                if fail:
                    with self.assertRaisesRegex(RuntimeError, 'UI failed'):
                        launch_demo(options())
                else:
                    self.assertEqual(launch_demo(options()), 0)
            self.assertEqual(len(directories), 1)
            self.assertFalse(directories[0].exists())

    def test_real_terminal_demo_needs_no_codex_and_restores_the_terminal(self):
        import termios
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            master, slave = pty.openpty()
            original = termios.tcgetattr(slave)
            process = subprocess.Popen([sys.executable, '-B', str(root / 'explain.py'), '--demo'],
                stdin=slave, stdout=slave, stderr=slave,
                env=os.environ | {'TERM': 'xterm-256color', 'PATH': directory, 'TMPDIR': directory})
            data, quit_sent = bytearray(), False
            deadline = time.monotonic() + 10
            try:
                while process.poll() is None:
                    self.assertLess(time.monotonic(), deadline, 'The demo did not exit.')
                    if select.select([master], [], [], .05)[0]:
                        data.extend(os.read(master, 65536))
                    if b'DEMO' in data and b'01-success' in data and not quit_sent:
                        os.write(master, b'\x11')
                        quit_sent = True
                self.assertTrue(quit_sent, data.decode(errors='replace'))
                self.assertEqual(process.returncode, 0, data.decode(errors='replace'))
                self.assertNotIn(b'Traceback', data)
                self.assertEqual(termios.tcgetattr(slave), original)
                self.assertEqual(list(Path(directory).glob('explain-demo-*')), [])
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                os.close(master)
                os.close(slave)


class DemoUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.directory = self.base / 'demo'
        self.directory.mkdir()
        self.saved_folder = self.base / 'user'
        self.saved_folder.mkdir()
        (self.saved_folder / 'explain.config').write_text('saved user settings\n', encoding='utf-8')
        self.enterContext(patch('src.tui.app.ROOT', self.saved_folder))
        self.enterContext(patch.dict(os.environ, {'PATH': str(self.base / 'no-codex')}))
        for target in ('src.tui.app.AuditProcess', 'src.cli.run_audit', 'src.tui.worker.run_audit',
                       'subprocess.Popen', 'src.tui.app.save_settings'):
            self.enterContext(patch(target, side_effect=AssertionError('Demo attempted a real operation: ' + target)))
        for target in ('asyncio.create_subprocess_exec', 'src.tui.models.codex_models.catalog',
                       'src.tui.models.codex_models.check_model', 'src.tui.models.codex_limits.read_limits'):
            self.enterContext(patch(target, new_callable=AsyncMock,
                                   side_effect=AssertionError('Demo attempted a backend operation: ' + target)))

    def app(self):
        return DemoApp(options(), self.directory)

    async def test_all_scenarios_navigation_details_resize_and_repeat(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            self.assertIn('DEMO', app.title)
            configs = app.query_one('#configs', DataTable)
            self.assertEqual(configs.row_count, 4)
            self.assertEqual([configs.get_row_at(i)[1].plain for i in range(4)], ['git', 'folder', 'folder', 'Invalid'])
            for row, expected in ((0, 'COMPLETE'), (1, 'PARTIAL'), (2, 'FAILED'), (0, 'COMPLETE')):
                configs.move_cursor(row=row)
                await pilot.pause()
                for name in ('check', 'models', 'change-folder'):
                    self.assertTrue(app.query_one('#' + name, Button).disabled)
                await pilot.click('#run')
                stages = app.query_one('#stages', DataTable)
                tabs = app.query_one('#run-tabs', TabbedContent)
                self.assertFalse(app.query_one('#picker').display)
                self.assertEqual(tabs.active, 'steps-tab')
                self.assertEqual(app.pending_result['result']['status'], expected)
                self.assertEqual(app.last_exit_code, 0)
                self.assertFalse(app.running)
                self.assertIsNone(app.audit)
                self.assertGreater(stages.max_scroll_x, 0)
                self.assertIn('study #001', app.query_one('#step-details', Static).content.plain)
                if expected == 'COMPLETE':
                    self.assertIn('align #002', [stages.get_row_at(i)[1].plain for i in range(stages.row_count)])
                    self.assertIn('Account limits:', app.query_one('#step-metrics', Static).content.plain)
                    self.assertIn('5h: Used 24%', app.query_one('#step-metrics', Static).content.plain)
                    self.assertIn('7d: Used 61%', app.query_one('#step-metrics', Static).content.plain)
                else:
                    self.assertIn('SKIPPED', [stages.get_row_at(i)[2].plain for i in range(stages.row_count)])
                    details = app.query_one('#step-diagnostics', Static)
                    self.assertTrue(details.display)
                    self.assertIn('QUOTE_MISMATCH' if expected == 'PARTIAL' else 'static failure sample', details.content.plain)
                frozen = [stages.get_row_at(i) for i in range(stages.row_count)]
                app.tick()
                self.assertEqual([stages.get_row_at(i) for i in range(stages.row_count)], frozen)
                await pilot.resize_terminal(120, 40)
                await pilot.resize_terminal(80, 24)
                tabs.active = 'summary-tab'
                await pilot.pause()
                summary = app.query_one('#summary', DataTable)
                self.assertEqual(summary.get_row_at(0)[1].plain, expected)
                content = ' '.join(cell.plain for key in summary.rows for cell in summary.get_row(key))
                self.assertIn('DEMO: static sample', content)
                self.assertIn(str(self.directory / 'reports'), content)
                if expected == 'COMPLETE':
                    self.assertIn('Review completed and accepted', content)
                tabs.active = 'steps-tab'
                await pilot.pause()
                await pilot.click('#back')
                self.assertTrue(app.query_one('#picker').display)
            configs.move_cursor(row=3)
            await pilot.pause()
            self.assertTrue(app.query_one('#run', Button).disabled)
            self.assertIn('Cannot start:', str(app.query_one('#config-preview', Static).content))
            app.start_audit()
            self.assertTrue(app.query_one('#picker').display)
            self.assertEqual((self.saved_folder / 'explain.config').read_text(), 'saved user settings\n')

    async def test_refresh_restores_samples_and_disabled_handlers_cannot_run(self):
        app = self.app()
        async with app.run_test() as pilot:
            original = app.selected.read_bytes()
            app.selected.write_text('{}\n', encoding='utf-8')
            app.select_config(str(app.selected))
            self.assertTrue(app.query_one('#run', Button).disabled)
            await pilot.click('#refresh')
            self.assertEqual(app.selected.read_bytes(), original)
            self.assertFalse(app.query_one('#run', Button).disabled)
            app.start_audit(check_only=True)
            app.choose_model()
            app.choose_folder()
            for name in ('check', 'models', 'change-folder'):
                app.on_button_pressed(Button.Pressed(app.query_one('#' + name, Button)))
            await pilot.pause()
            self.assertIsNone(app.state)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertTrue(app.query_one('#picker').display)

    async def test_quit_and_ctrl_c_after_a_failed_sample_return_zero(self):
        for key in ('q', 'ctrl+c'):
            app = self.app()
            async with app.run_test() as pilot:
                app.query_one('#configs', DataTable).move_cursor(row=2)
                await pilot.pause()
                await pilot.click('#run')
                self.assertEqual(app.pending_result['result']['exit_code'], 1)
                await pilot.press(key)
            self.assertEqual(app.return_value, 0)
