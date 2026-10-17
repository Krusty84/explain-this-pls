# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Headless UI and real spawned-worker checks; no provider or model calls."""
from argparse import Namespace
import contextlib
import datetime as dt
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

from src.cli import main
from src.contracts.contracts import jsonc
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
                    patch('src.cli.run_audit') as audit, self.assertRaises(SystemExit) as exc:
                main(args)
            self.assertEqual(exc.exception.code, 2)
            self.assertIn('usage:', stderr.getvalue())
            audit.assert_not_called()

    def test_interactive_no_args_and_explicit_tui_route_without_audit(self):
        for args in ([], ['--tui'], ['--tui', '--config', 'project.jsonc']):
            with self.subTest(args=args), patch('sys.stdin.isatty', return_value=True), \
                    patch('sys.stdout.isatty', return_value=True), patch.dict(os.environ, {'TERM': 'xterm'}), \
                    patch('src.tui.app.launch', return_value=130) as launch, patch('src.cli.run_audit') as audit:
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
    def test_metadata_warning_is_retained_without_failing_completed_steps(self):
        state = RunState()
        context = dict(branch='folder', stage='study', revision_id='001')
        state.apply('stage_completed', dict(context, status='COMPLETE', elapsed_seconds=1))
        warning = {'code': 'LAST_ANALYSIS_NOT_SAVED', 'message': 'Could not save last_analysis.'}
        state.apply('last_analysis_warning', warning)
        state.finish({'result': {'status': 'COMPLETE', 'exit_code': 0}})
        self.assertEqual(state.rows[('folder', 'study', '001')]['status'], 'COMPLETE')
        self.assertIn(warning, state.diagnostics)

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
        completed = dict(branch='folder', stage='study', revision_id='001')
        review = dict(branch='folder', stage='review', revision_id='001')
        state.apply('stage_completed', dict(completed, status='COMPLETE', elapsed_seconds=1))
        state.apply('stage_started', review)
        state.finish({'result': {'status': 'FAILED', 'exit_code': 130}})
        self.assertEqual(state.rows[('folder', 'study', '001')]['status'], 'COMPLETE')
        self.assertEqual(state.rows[('folder', 'review', '001')]['status'], 'INTERRUPTED')


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
                self.assertEqual(str(app.query_one('#check', Button).label), 'Check config')
                self.assertEqual(str(app.query_one('#change-folder', Button).label), 'Config folder')
                for button in app.query('#picker Button'):
                    self.assertTrue(button.parent.content_region.contains_region(button.region))
                    self.assertGreaterEqual(button.content_region.width, len(str(button.label)))
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
            preview = str(pilot.app.query_one('#config-preview', Static).content)
            self.assertIn('Reports: ' + str(self.base / 'reports' / 'source'), preview)

    async def test_config_columns_show_study_model_and_effort_with_native_variant(self):
        value = jsonc(self.path.read_text())
        cases = [({}, {}, 'CLI default', 'CLI default'),
                 ({'model': 'shared', 'reasoning_effort': 'high'},
                  {'review': {'model': 'other', 'reasoning_effort': 'low'}}, 'shared', 'high'),
                 ({'model': 'shared', 'reasoning_effort': 'high'},
                  {'study': {'model': 'study-model', 'reasoning_effort': 'medium'}}, 'study-model', 'medium'),
                 ({'model': 'shared', 'reasoning_effort': 'high'},
                  {'study': {'model': None, 'reasoning_effort': None}}, 'CLI default', 'CLI default'),
                 ({'backend': 'opencode', 'model': 'provider/model#native'}, {}, 'provider/model#native', 'native'),
                 ({'backend': 'opencode', 'model': 'provider/model#native', 'reasoning_effort': None},
                  {}, 'provider/model#native', 'native'),
                 ({'backend': 'opencode', 'model': 'provider/model', 'reasoning_effort': 'high'},
                  {}, 'provider/model', 'high'),
                 ({'model': 'model#native'}, {}, 'model#native', 'CLI default'),
                 ({'model': '[bold]model\x1b[31m', 'reasoning_effort': '[red]high'},
                  {}, '[bold]model\\x1b[31m', '[red]high')]
        app = self.app(self.path)
        async with app.run_test(size=(80, 24)) as pilot:
            table = app.query_one('#configs', DataTable)
            self.assertEqual([column.label.plain for column in table.columns.values()],
                             ['Config', 'Mode', 'Source', 'Model', 'Effort', 'Results'])
            for agent, stages, model, effort in cases:
                with self.subTest(agent=agent, stages=stages):
                    updated = value | {'agent': value['agent'] | agent, 'stage_agents': stages}
                    self.path.write_text(json.dumps(updated))
                    app.refresh_configs()
                    await pilot.pause()
                    row = [cell.plain for cell in table.get_row_at(0)]
                    self.assertEqual(row, [self.path.name, 'folder', str(self.base / 'source'), model, effort, '—'])

    async def test_results_survive_reopen_and_show_time_path_and_missing_directory(self):
        directory = self.base / 'reports' / 'saved [bold]'
        directory.mkdir(parents=True)
        record = {'status': 'COMPLETED', 'finished_at': '2026-10-17T15:00:00+03:00',
                  'results_dir': str(directory)}
        value = jsonc(self.path.read_text()) | {'last_analysis': record}
        self.path.write_text(json.dumps(value))
        async with self.app(self.path).run_test(size=(80, 24)) as pilot:
            table = pilot.app.query_one('#configs', DataTable)
            self.assertEqual(table.get_row_at(0)[5].plain, 'COMPLETED')
            preview = str(pilot.app.query_one('#config-preview', Static).content)
            finished = dt.datetime.fromisoformat(record['finished_at']).astimezone().isoformat(sep=' ', timespec='seconds')
            self.assertIn('Finished: ' + finished, preview)
            self.assertIn('Results directory: ' + str(directory), preview)
        value['last_analysis']['status'] = 'PARTIAL'
        self.path.write_text(json.dumps(value))
        async with self.app(self.path).run_test(size=(60, 20)) as pilot:
            app = pilot.app
            table = app.query_one('#configs', DataTable)
            self.assertEqual(table.get_row_at(0)[5].plain, 'PARTIAL')
            await pilot.resize_terminal(100, 30)
            directory.rmdir()
            before = self.path.read_bytes()
            app.refresh_configs()
            await pilot.pause()
            self.assertEqual(table.get_row_at(0)[5].plain, 'Missing')
            self.assertIn('Results: PARTIAL (Missing)', str(app.query_one('#config-preview', Static).content))
            self.assertFalse(app.query_one('#run', Button).disabled)
            self.assertEqual(self.path.read_bytes(), before)
            value['last_analysis'] = None
            self.path.write_text(json.dumps(value))
            app.refresh_configs()
            self.assertEqual(table.get_row_at(0)[5].plain, '—')

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
            self.assertIn('n/a', table.get_row_at(1)[5].plain)
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

    async def test_completed_warning_status_and_citation_details_are_visible(self):
        app = self.app(self.path, no_progress=True)
        async with app.run_test(size=(100, 32)) as pilot:
            app.state = RunState()
            app.query_one('#picker').display = False
            app.query_one('#dashboard').display = True
            context = dict(branch='unit', stage='study', revision_id='001')
            app.on_worker_message(WorkerMessage({'type': 'event', 'event': 'stage_started', 'context': context}))
            warning = dict(evidence_id='study:E-001', source_id='source-001', path='app.py', start_line=1,
                           end_line=2, reason='QUOTE_MISMATCH', action='UNRESOLVED', claim_ids=['C-001'])
            app.on_worker_message(WorkerMessage({'type': 'event', 'event': 'citation_warning', 'context': context | warning}))
            app.on_worker_message(WorkerMessage({'type': 'event', 'event': 'stage_completed',
                'context': context | {'status': 'COMPLETE_WITH_WARNINGS', 'elapsed_seconds': 1}}))
            app.complete({'result': {'status': 'COMPLETE_WITH_WARNINGS', 'exit_code': 0, 'review_enabled': False},
                          'diagnostics': []})
            await pilot.pause()
            self.assertEqual(app.last_exit_code, 0)
            self.assertEqual(app.query_one('#stages', DataTable).get_row_at(1)[2].plain, 'COMPLETE_WITH_WARNINGS')
            summary = ' '.join(cell.plain for row in app.query_one('#summary', DataTable).rows
                               for cell in app.query_one('#summary', DataTable).get_row(row))
            for text in ('COMPLETE_WITH_WARNINGS', 'source-001 / app.py:1-2', 'C-001', 'QUOTE_MISMATCH'):
                self.assertIn(text, summary)

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
                await self.wait_for(pilot, lambda: not app.running and
                                    app.query_one('#run-tabs', TabbedContent).active == 'summary-tab')
                self.assertEqual(app.pending_result['result']['status'], 'PREFLIGHT_OK')
                self.assertEqual(app.pending_result['result']['metrics']['attempts'], 0)
                self.assertNotIn('last_analysis', jsonc(self.config_path.read_text()))
                await pilot.click('#back')
                await pilot.click('#run')
                await self.wait_for(pilot, lambda: not app.running and
                                    app.query_one('#run-tabs', TabbedContent).active == 'summary-tab')
                self.assertEqual(app.pending_result['result']['status'], 'COMPLETE')
                self.assertGreater(app.query_one('#stages', DataTable).row_count, 2)
                self.assertEqual(app.query_one('#run-tabs', TabbedContent).active, 'summary-tab')
                record = jsonc(self.config_path.read_text())['last_analysis']
                self.assertEqual(record['status'], 'COMPLETED')
                self.assertEqual(Path(record['results_dir']), Path(app.pending_result['result']['manifest']).parent)
                await pilot.click('#back')
                await pilot.pause()
                self.assertEqual(app.query_one('#configs', DataTable).get_row_at(0)[5].plain, 'COMPLETED')

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
                await self.wait_for(pilot, lambda: not app.running and
                                    app.query_one('#run-tabs', TabbedContent).active == 'summary-tab')
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
