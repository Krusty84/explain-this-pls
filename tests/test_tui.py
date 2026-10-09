# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Headless UI and real spawned-worker checks; no provider or model calls."""
from argparse import Namespace
import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, DataTable, Input, Static, TabbedContent

from explain import main
from src.runtime.metrics import usage
from src.runtime.reporting import Reporter
from src.tui.app import ExplainApp, FolderChooser, WorkerMessage
from src.tui.settings import config_files, read_settings, save_settings
from src.tui.state import RunState
from src.tui.worker import AuditProcess, PipeReporter
import test_startup as startup


def options(config=None, **changes):
    return Namespace(**({'config': config, 'check': False, 'trust_repository': False,
                         'output': 'auto', 'verbose': False, 'no_progress': False, 'tui': True} | changes))


def measured(context, status='COMPLETE', **changes):
    return dict(context, status=status, duration_seconds=3, attempts=1,
                usage=usage({'total_tokens': 42, 'cost_usd': .01}, source='test'),
                by_model=[{'backend': 'codex', 'model_requested': 'example', 'model_actual': None}], **changes)


class TuiRoutingTests(unittest.TestCase):
    def test_real_terminal_launch_and_quit_restore_terminal_settings(self):
        import termios
        root = Path(__file__).resolve().parents[1]
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        process = subprocess.Popen([sys.executable, '-B', str(root / 'explain.py'), '--tui',
                                    '--config', '/no/such/config.json'], stdin=slave, stdout=slave, stderr=slave,
                                   env=os.environ | {'TERM': 'xterm-256color'})
        data, quit_sent = bytearray(), False
        deadline = time.monotonic() + 10
        try:
            while process.poll() is None:
                self.assertLess(time.monotonic(), deadline, 'The real terminal UI did not exit.')
                if select.select([master], [], [], .05)[0]:
                    data.extend(os.read(master, 65536))
                if b'explain-this-pls' in data and not quit_sent:
                    os.write(master, b'\x11')  # Ctrl+Q, also works while a text input is focused.
                    quit_sent = True
            self.assertEqual(process.returncode, 0, data.decode(errors='replace'))
            self.assertTrue(quit_sent)
            self.assertEqual(termios.tcgetattr(slave), original)
            self.assertNotIn(b'Traceback', data)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            os.close(master)
            os.close(slave)

    def test_noninteractive_and_conflicting_arguments_create_no_run(self):
        for args in ([], ['--tui'], ['--check'], ['--tui', '--output', 'json'], ['--tui', '--output', 'text']):
            with self.subTest(args=args), patch('sys.stdin.isatty', return_value=False), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr, \
                    patch('explain.run_audit') as audit, self.assertRaises(SystemExit) as exc:
                main(args)
            self.assertEqual(exc.exception.code, 2)
            self.assertIn('usage:', stderr.getvalue())
            audit.assert_not_called()

    def test_interactive_no_args_and_explicit_tui_route_without_audit(self):
        for args in ([], ['--tui'], ['--tui', '--config', 'project.jsonc']):
            with self.subTest(args=args), patch('sys.stdin.isatty', return_value=True), \
                    patch('sys.stdout.isatty', return_value=True), patch.dict(os.environ, {'TERM': 'xterm'}), \
                    patch('src.tui.app.launch', return_value=130) as launch, patch('explain.run_audit') as audit:
                self.assertEqual(main(args), 130)
                launch.assert_called_once()
                audit.assert_not_called()

    def test_console_events_are_sanitized_and_not_replayed_on_log_attach(self):
        events = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EXAMPLE_API_KEY': 'secret-value'}):
            reporter = Reporter(stdout=io.StringIO(), stderr=io.StringIO(), progress=False, event_sink=events.append)
            reporter.emit('run_started', check_only=True, source='secret-value')
            reporter.attach_log(Path(directory))
            reporter.close()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].context['source'], '[REDACTED]')


class RunStateTests(unittest.TestCase):
    def test_sources_revisions_partial_skip_error_and_final_metrics(self):
        state = RunState(clock=lambda: 10)
        contexts = [dict(branch=branch, stage=stage, revision_id=revision, backend='codex')
                    for branch, stage, revision in (('main', 'study', '001'), ('main', 'revise', '002'),
                                                   ('other', 'study', '001'), ('main', 'review', '002'))]
        for context in contexts[:3]:
            state.apply('stage_started', context)
            state.apply('stage_completed', dict(context, status='PARTIAL', elapsed_seconds=3,
                                               metrics=measured(context, 'PARTIAL'), report_path='/tmp/[report].md'))
        state.apply('stage_skipped', dict(contexts[3], reason='disabled_by_config'))
        compare = dict(branch='all branches', stage='compare', revision_id=None)
        state.apply('stage_started', compare)
        state.apply('error', dict(compare, code='BACKEND_ERROR', message='Provider failed.', phase='stage'))
        state.finish({'result': {'status': 'PARTIAL', 'exit_code': 2,
                                'metrics': {'stages': [measured(contexts[0], 'PARTIAL')]}}, 'diagnostics': []})
        self.assertEqual(len(state.rows), 6)
        self.assertEqual(state.rows[('main', 'study', '001')]['status'], 'PARTIAL')
        self.assertEqual(state.rows[('main', 'revise', '002')]['report_path'], '/tmp/[report].md')
        self.assertEqual(state.rows[('main', 'review', '002')]['status'], 'SKIPPED')
        self.assertEqual(state.rows[('all branches', 'compare', None)]['status'], 'FAILED')

    def test_interruption_retains_completed_steps(self):
        state = RunState()
        catalog = dict(branch='folder', stage='catalog', revision_id=None)
        study = dict(branch='folder', stage='study', revision_id='001')
        state.apply('stage_completed', dict(catalog, status='COMPLETE', elapsed_seconds=1))
        state.apply('stage_started', study)
        state.finish({'result': {'status': 'FAILED', 'exit_code': 130}})
        self.assertEqual(state.rows[('folder', 'catalog', None)]['status'], 'COMPLETE')
        self.assertEqual(state.rows[('folder', 'study', '001')]['status'], 'INTERRUPTED')


class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.directory = self.base / 'configs'
        self.directory.mkdir()
        self.settings = self.base / 'explain.config'
        source = self.base / 'source'
        source.mkdir()
        (source / 'app.py').write_text('print(42)\n')
        self.path = self.directory / 'config_深い [bold].jsonc'
        self.path.write_text(json.dumps({'mode': 'folder', 'folder_mode': {'path': '../source'},
            'reports_dir': '../reports', 'agent': {'backend': 'codex', 'executable': sys.executable}}))

    def app(self, config=None, **changes):
        return ExplainApp(options(config, **changes), settings_path=self.settings)

    async def test_first_launch_folder_persistence_and_no_automatic_run(self):
        app = self.app()
        with patch('src.tui.app.AuditProcess') as worker:
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                self.assertIsInstance(app.screen, FolderChooser)
                app.screen.query_one('#folder-path', Input).value = str(self.directory)
                await pilot.click('#folder-save')
                await pilot.pause()
                self.assertEqual(read_settings(self.settings), self.directory)
                self.assertEqual(app.selected, self.path)
                self.assertFalse(app.query_one('#run', Button).disabled)
                self.assertEqual(app.query_one('#configs', DataTable).get_row_at(0)[0].plain, self.path.name)
                worker.assert_not_called()
        async with self.app().run_test() as pilot:
            await pilot.pause()
            self.assertNotIsInstance(pilot.app.screen, FolderChooser)
            self.assertEqual(pilot.app.selected, self.path)

    async def test_explicit_file_bypasses_onboarding_and_does_not_change_settings(self):
        async with self.app(self.path).run_test() as pilot:
            await pilot.pause()
            self.assertEqual(pilot.app.selected, self.path)
            self.assertFalse(self.settings.exists())

    async def test_discovery_validation_refresh_and_removed_selection(self):
        save_settings(self.settings, self.directory)
        invalid = self.directory / 'broken.json'
        invalid.write_text('{')
        (self.directory / 'config.example.jsonc').write_text('{}')
        (self.directory / 'config.example.json').write_text('{}')
        (self.directory / 'nested').mkdir()
        (self.directory / 'nested/hidden.json').write_text('{}')
        self.assertEqual(set(config_files(self.directory)), {invalid, self.path})
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one('#run', Button).disabled)
            table = app.query_one('#configs', DataTable)
            table.focus()
            await pilot.press('down')
            self.assertEqual(app.selected, self.path)
            self.assertFalse(app.query_one('#run', Button).disabled)
            with patch('src.tui.app.AuditProcess') as worker:
                self.path.unlink()
                await pilot.click('#run')
                await pilot.pause()
                worker.assert_not_called()
            self.assertIn('Cannot start:', str(app.query_one('#config-preview', Static).content))
            self.assertEqual(app.selected, self.path)
            self.assertTrue(app.query_one('#run', Button).disabled)

    async def test_change_folder_empty_folder_and_save_error(self):
        app = self.app(self.path)
        empty = self.base / 'empty'
        empty.mkdir()
        async with app.run_test() as pilot:
            await pilot.click('#change-folder')
            await pilot.pause()
            app.screen.query_one('#folder-path', Input).value = str(empty)
            with patch('src.tui.settings.atomic', side_effect=PermissionError('Read-only installation')):
                await pilot.click('#folder-save')
                self.assertIsInstance(app.screen, FolderChooser)
                self.assertIn('Read-only', str(app.screen.query_one('#folder-message', Static).content))
            await pilot.pause(.3)
            await pilot.click('#folder-save')
            await pilot.pause()
            self.assertEqual(read_settings(self.settings), empty)
            self.assertTrue(app.query_one('#run', Button).disabled)
            self.assertEqual(app.query_one('#configs', DataTable).row_count, 0)

    async def test_invalid_settings_and_stale_directory_reopen_chooser(self):
        for contents in ('broken', json.dumps({'config_dir': str(self.base / 'missing')})):
            self.settings.write_text(contents)
            async with self.app().run_test() as pilot:
                await pilot.pause()
                self.assertIsInstance(pilot.app.screen, FolderChooser)
                self.assertEqual(self.settings.read_text(), contents)
                await pilot.press('escape')

    async def test_tables_literal_content_metrics_final_state_and_resize(self):
        app = self.app(self.path, no_progress=True, verbose=True)
        async with app.run_test(size=(80, 24)) as pilot:
            app.state = RunState()
            app.query_one('#picker').display = False
            app.query_one('#dashboard').display = True
            context = dict(branch='[red]main\x1b[31m', stage='study', revision_id='001', backend='codex')
            app.on_worker_message(WorkerMessage({'type': 'event', 'event': 'stage_started', 'context': context}))
            table = app.query_one('#stages', DataTable)
            self.assertEqual(table.get_row_at(1)[0].plain, '[red]main\\x1b[31m')
            self.assertIn('unavailable', table.get_row_at(1)[5].plain)
            metrics = measured(context, 'PARTIAL')
            app.on_worker_message(WorkerMessage({'type': 'event', 'event': 'stage_completed',
                'context': dict(context, status='PARTIAL', elapsed_seconds=3, metrics=metrics, report_path='/tmp/[report].md')}))
            table.move_cursor(row=1)
            await pilot.pause()
            self.assertIn('/tmp/[report].md', str(app.query_one('#step-details', Static).content))
            payload = {'result': {'status': 'PARTIAL', 'exit_code': 2, 'review_enabled': False,
                                 'metrics': dict(metrics, stages=[metrics])},
                       'paths': [('Final report', '/tmp/final.md')],
                       'diagnostics': [{'code': 'TEST_ERROR', 'message': '[bold]literal', 'details': {'x': 1}}]}
            app.complete(payload)
            await pilot.pause()
            self.assertEqual(app.query_one('#run-tabs', TabbedContent).active, 'summary-tab')
            self.assertEqual(app.last_exit_code, 2)
            self.assertFalse(app.query_one('#back', Button).disabled)
            await pilot.resize_terminal(120, 40)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertEqual(app.query_one('#stages', DataTable).row_count, 2)
            summary = ' '.join(cell.plain for row in app.query_one('#summary', DataTable).rows
                               for cell in app.query_one('#summary', DataTable).get_row(row))
            self.assertIn('/tmp/final.md', summary)
            self.assertIn('TEST_ERROR', summary)
            self.assertIn('no separate review', summary)
            await pilot.click('#back')
            self.assertTrue(app.query_one('#picker').display)

    async def test_no_progress_keeps_elapsed_frozen_between_events_and_worker_crash_finishes(self):
        app = self.app(self.path, no_progress=True)
        async with app.run_test() as pilot:
            app.state = RunState(clock=lambda: 0)
            app.running = True
            app.audit = Mock()
            app.render_stages()
            before = app.query_one('#stages', DataTable).get_row_at(0)[3].plain
            app.state.clock = lambda: 20
            await pilot.pause(.1)
            self.assertEqual(app.query_one('#stages', DataTable).get_row_at(0)[3].plain, before)
            app.on_worker_message(WorkerMessage({'type': 'exit', 'exit_code': -9}))
            self.assertFalse(app.running)
            self.assertEqual(app.last_exit_code, 1)
            self.assertEqual(app.state.preparation['status'], 'FAILED')
            self.assertIn('unexpectedly', app.state.diagnostics[-1]['message'])


class LiveTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = startup.StartupCLIIntegrationTests.setUp
    tearDown = startup.StartupCLIIntegrationTests.tearDown
    git = startup.StartupCLIIntegrationTests.git
    prepare = startup.StartupCLIIntegrationTests.prepare

    async def wait_for(self, pilot, condition):
        deadline = time.monotonic() + 15
        while not condition():
            self.assertLess(time.monotonic(), deadline, 'The TUI did not finish the requested action.')
            await pilot.pause(.05)
        await pilot.pause()

    async def test_check_then_run_in_same_ui_publishes_final_summary(self):
        self.prepare('folder')
        app = ExplainApp(options(self.config_path), settings_path=self.base / 'explain.config')
        with patch.dict(os.environ, self.env, clear=True):
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.click('#check')
                await self.wait_for(pilot, lambda: not app.running)
                self.assertEqual(app.pending_result['result']['status'], 'PREFLIGHT_OK')
                self.assertEqual(app.pending_result['result']['metrics']['attempts'], 0)
                await pilot.click('#back')
                await pilot.click('#run')
                await self.wait_for(pilot, lambda: not app.running)
                self.assertEqual(app.pending_result['result']['status'], 'COMPLETE')
                self.assertGreater(app.query_one('#stages', DataTable).row_count, 2)
                self.assertEqual(app.query_one('#run-tabs', TabbedContent).active, 'summary-tab')

    async def test_keyboard_cancel_stops_silent_agent_and_restores_checkout(self):
        self.prepare('git')
        marker = self.base / 'agent-process.json'
        self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'study', 'kind': 'wait', 'seconds': 60,
                                                   'process_marker': str(marker)})
        app = ExplainApp(options(self.config_path), settings_path=self.base / 'explain.config')
        with patch.dict(os.environ, self.env, clear=True):
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.click('#run')
                await self.wait_for(pilot, marker.exists)
                child = json.loads(marker.read_text())
                self.assertTrue(app.running)
                await pilot.press('tab')
                await pilot.resize_terminal(100, 30)
                await pilot.press('ctrl+c')
                await self.wait_for(pilot, lambda: not app.running)
                self.assertEqual(app.last_exit_code, 130)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child['pid'], 0)
                self.assertEqual(Path(child['cwd']), self.repo_path)
                manifest = json.loads(Path(app.pending_result['result']['manifest']).read_text())
                self.assertTrue(manifest['restoration']['restored'])
                self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD').strip(), 'master')
                self.assertEqual(app.query_one('#run-tabs', TabbedContent).active, 'summary-tab')

    async def test_quit_waits_for_worker_cleanup_and_returns_interrupted_code(self):
        self.prepare('folder')
        marker = self.base / 'agent-process.json'
        self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'study', 'kind': 'wait', 'seconds': 60,
                                                   'process_marker': str(marker)})
        app = ExplainApp(options(self.config_path), settings_path=self.base / 'explain.config')
        with patch.dict(os.environ, self.env, clear=True):
            async with app.run_test() as pilot:
                await pilot.click('#run')
                await self.wait_for(pilot, marker.exists)
                await pilot.click('#quit-run')
                await self.wait_for(pilot, lambda: not app.running)
                self.assertEqual(app.return_value, 130)


class WorkerIntegrationTests(unittest.TestCase):
    setUp = startup.StartupCLIIntegrationTests.setUp
    tearDown = startup.StartupCLIIntegrationTests.tearDown
    git = startup.StartupCLIIntegrationTests.git
    prepare = startup.StartupCLIIntegrationTests.prepare

    def collect(self, *, cancel_stage=False, cancel_immediately=False):
        with patch.dict(os.environ, self.env, clear=True):
            audit = AuditProcess(options(self.config_path))
            audit.start()
        if cancel_immediately:
            audit.cancel()
            audit.cancel()
        messages = []
        # Bound a regression without leaving an agent process running indefinitely.
        timer = threading.Timer(15, audit.cancel)
        timer.start()
        try:
            for message in audit.messages():
                messages.append(message)
                if cancel_stage and message.get('event') == 'stage_started':
                    audit.cancel()
                    audit.cancel()
            self.assertFalse(audit.process.is_alive())
        finally:
            timer.cancel()
            timer.join()
            audit.close()
        return messages

    def test_real_worker_publishes_events_result_files_and_cli_exit_code(self):
        self.prepare('folder')
        messages = self.collect()
        events = [message['event'] for message in messages if message['type'] == 'event']
        self.assertIn('stage_started', events)
        self.assertIn('stage_completed', events)
        payload = next(message for message in messages if message['type'] == 'result')
        self.assertEqual(payload['result']['status'], 'COMPLETE')
        self.assertEqual(messages[-1], {'type': 'exit', 'exit_code': 0})
        self.assertTrue(all(Path(path).is_file() for label, path in payload['paths']))
        self.assertGreater(len(payload['result']['metrics']['stages']), 1)

    def test_cancel_before_ready_and_during_analysis_preserves_exit_130(self):
        for immediate in (True, False):
            with self.subTest(immediate=immediate):
                self.prepare('folder')
                messages = self.collect(cancel_stage=not immediate, cancel_immediately=immediate)
                self.assertEqual(messages[-1]['exit_code'], 130)
                payload = next(message for message in messages if message['type'] == 'result')
                self.assertEqual(payload['result']['exit_code'], 130)

    def test_pipe_reporter_does_not_send_tracebacks_and_cleans_secrets(self):
        class Connection:
            messages = []
            def send(self, value):
                self.messages.append(value)
        connection = Connection()
        with patch.dict(os.environ, {'MY_API_TOKEN': 'do-not-show'}):
            reporter = PipeReporter(connection)
            reporter.emit('error', phase='run', code='TEST', message='do-not-show [bold]', exception='private traceback')
            reporter.close()
        encoded = json.dumps(connection.messages)
        self.assertNotIn('do-not-show', encoded)
        self.assertNotIn('private traceback', encoded)
        self.assertIn('[REDACTED] [bold]', encoded)


if __name__ == '__main__':
    unittest.main()
