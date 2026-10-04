# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Console, diagnostic, process activity and real offline CLI regressions."""
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import pty
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.contracts.contracts import ContractError, jsonc
from explain import AuditError, Runner, UnsafeRepository, cli_env, load_config, main, process
from src.runtime.reporting import NullReporter, Reporter, duration, output_mode, safe_text
import test_startup as startup
import test_submodules as recursive

ROOT = Path(__file__).resolve().parents[1]


class ReporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.reporter = Reporter(stdout=self.out, stderr=self.err)
        self.addCleanup(self.reporter.close)

    def test_output_selection_and_explicit_precedence(self):
        for tty in (False, True):
            stream = Mock()
            stream.isatty.return_value = tty
            for flag in ('text', 'json', 'auto'):
                self.assertEqual(output_mode(flag, stream), ('text' if tty else 'json') if flag == 'auto' else flag)
            self.assertEqual(stream.isatty.call_count, 1)

    def test_safe_unicode_paths_and_durations(self):
        self.assertEqual(safe_text('/source/深い folder\n\x1b[31m\r\u202e'), '/source/深い folder\\n\\x1b[31m\\r\\u202e')
        self.assertEqual([duration(x) for x in (0, 30, 120, 1800, 3661)], ['00:00', '00:30', '02:00', '30:00', '01:01:01'])

    def test_colors_only_known_leading_labels_and_resets_before_text(self):
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), \
                patch.object(self.err, 'isatty', return_value=True):
            reporter = Reporter(stdout=self.out, stderr=self.err)
        self.addCleanup(reporter.close)
        lines = [f'[{label}] /source/深い folder [OK]' for label in ('WARN', 'RUN', 'OK', 'FAIL', 'SKIP')]
        lines += ['text [WARN]', '  [FAIL] indented', '[UNKNOWN] text', '[OK]suffix']
        reporter.write(self.err, '\n'.join(lines))
        expected = [f'\x1b[{color}m[{label}]\x1b[0m /source/深い folder [OK]'
                    for label, color in (('WARN', 33), ('RUN', 36), ('OK', 32), ('FAIL', 31), ('SKIP', 90))]
        self.assertEqual(self.err.getvalue(), '\n'.join(expected + lines[5:]) + '\n')
        reporter.write(self.out, '[OK] stdout remains plain')
        self.assertEqual(self.out.getvalue(), '[OK] stdout remains plain\n')

    def test_color_detection_is_independent_per_stream_and_cached(self):
        for out_tty, err_tty in ((False, False), (False, True), (True, False), (True, True)):
            for no_color in (None, '', '1', '0'):
                for term in (None, 'xterm', 'dumb'):
                    with self.subTest(stdout=out_tty, stderr=err_tty, no_color=no_color, term=term):
                        env = {} if term is None else {'TERM': term}
                        if no_color is not None:
                            env['NO_COLOR'] = no_color
                        out, err = io.StringIO(), io.StringIO()
                        with patch.dict(os.environ, env, clear=True), \
                                patch.object(out, 'isatty', return_value=out_tty) as stdout_tty, \
                                patch.object(err, 'isatty', return_value=err_tty) as stderr_tty:
                            reporter = Reporter(stdout=out, stderr=err)
                            # Changes after construction must not alter the selected policy.
                            os.environ['NO_COLOR'] = '1' if not no_color else ''
                            stdout_tty.return_value = not out_tty
                            stderr_tty.return_value = not err_tty
                            reporter.emit('root_warning')
                            reporter.emit('root_warning')
                            reporter.finish({'status': 'FAILED', 'exit_code': 1, 'manifest': None}, {},
                                            check_only=True, config_path=Path('config.jsonc'))
                            stderr_tty.assert_called_once_with()
                            stdout_tty.assert_called_once_with()
                        self.assertEqual('\x1b[' in out.getvalue(), out_tty and not no_color and term != 'dumb')
                        self.assertEqual('\x1b[' in err.getvalue(), err_tty and not no_color and term != 'dumb')
                        reporter.close()

    def test_color_detection_failure_falls_back_to_plain_text(self):
        for error in (OSError('unavailable'), ValueError('closed stream'), AttributeError('no isatty'),
                      RuntimeError('custom stream failure')):
            with self.subTest(error=error), patch.dict(os.environ, {}, clear=True), \
                    patch.object(self.err, 'isatty', side_effect=error):
                reporter = Reporter(stdout=self.out, stderr=self.err)
                reporter.emit('root_warning')
                self.assertNotIn('\x1b', self.err.getvalue())
                reporter.close()

    def test_stage_status_colors_do_not_color_names_or_logs(self):
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), \
                patch.object(self.err, 'isatty', return_value=True):
            reporter = Reporter(stdout=self.out, stderr=self.err)
        self.addCleanup(reporter.close)
        reporter.attach_log(self.base)
        for status, color, label, message in (
                ('COMPLETE', 32, 'OK', 'Architecture report created.'),
                ('PARTIAL', 33, 'WARN', 'Project analysis may be incomplete. See details below.'),
                ('BLOCKED', 33, 'WARN', 'Project analysis may be incomplete. See details below.'),
                ('FAILED', 31, 'FAIL', 'Project analysis failed. See details below.')):
            reporter.emit('stage_completed', branch='folder', source_name='COMPLETE 深い [FAIL]',
                          stage='study', backend='codex', status=status, elapsed_seconds=1)
            self.assertIn(f'\x1b[{color}m[{label}]\x1b[0m COMPLETE 深い [FAIL] / {message}'
                          ' | Elapsed: 00:01', self.err.getvalue())
        self.assertNotIn(' / study / codex', self.err.getvalue())
        records = [json.loads(line) for line in (self.base / 'run.log').read_text().splitlines()]
        self.assertEqual([r['status'] for r in records], ['COMPLETE', 'PARTIAL', 'BLOCKED', 'FAILED'])
        self.assertTrue(all(r['branch'] == 'folder' for r in records))
        self.assertNotIn('\x1b', (self.base / 'run.log').read_text())

    def test_folder_name_in_all_stage_messages_and_comparison_without_synthetic_branch(self):
        r = self.reporter
        context = {'branch': 'folder', 'source_name': 'source 深い\n\x1b[31m',
                   'stage': 'study', 'backend': 'codex'}
        r.emit('stage_started', **context)
        r.emit('process_waiting', **context, elapsed_seconds=30, last_output_seconds=None)
        r.emit('stage_completed', **context, status='COMPLETE', elapsed_seconds=31)
        r.emit('stage_skipped', **context)
        r.error(AuditError('test failure'), phase='stage', **context)
        text = self.err.getvalue()
        self.assertEqual(text.count('source 深い\\n\\x1b[31m / '), 5)
        self.assertNotIn('\x1b', text)
        self.assertNotIn(' / study / codex', text)
        r.emit('stage_started', branch='master', stage='study', backend='codex')
        self.assertIn('[RUN] master / Analyzing project…', self.err.getvalue())
        r.emit('stage_started', branch='all branches', stage='compare', backend='codex')
        self.assertIn('[RUN] Comparing branch reports…', self.err.getvalue())
        self.assertNotIn('all branches', self.err.getvalue())

    def test_final_summary_colors_labels_and_human_branch_statuses(self):
        manifests = [
            ({'status': 'PREFLIGHT_OK'}, {}, ['\x1b[32m[OK]\x1b[0m Local setup checked. Analysis has not started.']),
            ({'status': 'COMPLETE'}, {'mode': 'folder', 'accepted': True},
             ['\x1b[32m[OK]\x1b[0m Analysis complete. Reports may still contain errors.']),
            ({'status': 'PARTIAL'}, {'branches': [
                {'branch': 'COMPLETE', 'accepted': True, 'errors': []},
                {'branch': 'FAILED', 'accepted': False, 'errors': ['error']},
                {'branch': 'PARTIAL', 'accepted': False, 'errors': []}], 'pins': {'BLOCKED': 'sha'},
                'comparison': {'completion_status': 'BLOCKED'}, 'restoration': {'restored': True}},
             ['\x1b[33m[WARN]\x1b[0m Analysis may be incomplete. See available results and limitations below.',
              'Branch COMPLETE: \x1b[32mComplete\x1b[0m', 'Branch FAILED: \x1b[31mFailed\x1b[0m',
              'Branch PARTIAL: \x1b[33mMay be incomplete\x1b[0m', 'Branch BLOCKED: \x1b[90mNot started\x1b[0m']),
            ({'status': 'FAILED'}, {}, ['\x1b[31m[FAIL]\x1b[0m Analysis failed.']),
            ({'status': 'FAILED', 'exit_code': 130}, {}, ['\x1b[33m[WARN]\x1b[0m Analysis interrupted.']),
            ({'status': 'FAILED'}, {'restoration': {'restored': False}}, ['\x1b[31m[FAIL]\x1b[0m Analysis failed.'])]
        for result, manifest, expected in manifests:
            with self.subTest(result=result, manifest=manifest):
                out = io.StringIO()
                with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), patch.object(out, 'isatty', return_value=True):
                    r = Reporter(stdout=out, stderr=self.err)
                self.addCleanup(r.close)
                r.finish({'manifest': None, 'exit_code': 0} | result, manifest,
                         check_only=False, config_path=Path('config.jsonc'))
                for line in expected:
                    self.assertIn(line, out.getvalue())
                self.assertNotIn('not applicable', out.getvalue())
                self.assertNotIn('Source result:', out.getvalue())
                self.assertNotIn('Restoration: \x1b[32m', out.getvalue())
                if 'comparison' not in manifest:
                    self.assertNotIn('Comparison:', out.getvalue())

    def test_colored_verbose_and_log_warning_preserve_escaping_and_file_data(self):
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), \
                patch.object(self.out, 'isatty', return_value=True), \
                patch.object(self.err, 'isatty', return_value=True):
            reporter = Reporter(mode='json', verbose=True, stdout=self.out, stderr=self.err)
        self.addCleanup(reporter.close)
        reporter.attach_log(self.base)
        reporter.emit('stage_started', branch='深い\x1b[31m\n[FAIL]', stage='study', backend='codex')
        lines = self.err.getvalue().splitlines()
        self.assertTrue(lines[0].startswith('\x1b[36m[RUN]\x1b[0m 深い\\x1b[31m\\n[FAIL]'))
        self.assertTrue(lines[1].startswith('\x1b[36m[RUN]\x1b[0m Detail: '))
        self.assertEqual(self.err.getvalue().count('\x1b'), 4)
        self.assertNotIn('\x1b', (self.base / 'run.log').read_text())
        reporter.disable_log()
        self.assertIn('\x1b[33m[WARN]\x1b[0m Error details could not be saved.', self.err.getvalue())
        result = {'run_id': None, 'status': 'FAILED', 'manifest': None, 'exit_code': 1}
        reporter.finish(result, {}, check_only=True, config_path=Path('config.jsonc'))
        self.assertEqual(json.loads(self.out.getvalue()), result)
        self.assertNotIn('\x1b', self.out.getvalue())

    def test_internal_events_are_hidden_but_preserved_in_verbose_output_and_log(self):
        events = [
            ('study_normalized', {'replacement_count': 142}),
            ('review_normalized', {'replacement_count': 5}),
            ('configuration_loaded', {'repository_name': 'example', 'branches': ['main'], 'agents': {'study': 'codex'}}),
            ('preflight_started', {'check': 'configuration'}),
            ('preflight_completed', {'check': 'inventory'}),
            ('snapshot_started', {'snapshot': 'original'}),
            ('snapshot_completed', {'snapshot': 'original'}),
            ('branch_started', {'branch': 'main', 'commit': 'a' * 40}),
            ('process_stopping', {}), ('restoration_started', {}), ('restoration_completed', {}),
        ]
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                err = io.StringIO()
                reporter = Reporter(stdout=self.out, stderr=err, verbose=verbose)
                self.addCleanup(reporter.close)
                directory = self.base / str(verbose)
                directory.mkdir()
                reporter.attach_log(directory)
                for event, context in events:
                    reporter.emit(event, **context)
                if verbose:
                    for event, context in events:
                        self.assertIn('[RUN] Detail: ' + event, err.getvalue())
                    self.assertIn('142', err.getvalue())
                else:
                    self.assertEqual(err.getvalue(), '')
                records = [json.loads(line) for line in (directory / 'run.log').read_text().splitlines()]
                self.assertEqual([record['event'] for record in records], [event for event, _ in events])
                self.assertEqual(records[0]['replacement_count'], 142)
                self.assertEqual(records[7]['commit'], 'a' * 40)

    def test_stage_outcomes_are_specific_and_recovered_text_is_not_success(self):
        r = self.reporter
        for stage, noun, completed in (
                ('study', 'Project analysis', 'Architecture report created.'),
                ('review', 'Report review', 'Review complete. No significant issues reported.'),
                ('compare', 'Branch report comparison', 'Branch report comparison ready.')):
            for status, label, meaning in (
                    ('COMPLETE', 'OK', completed),
                    ('PARTIAL', 'WARN', noun + ' may be incomplete. See details below.'),
                    ('BLOCKED', 'WARN', noun + ' may be incomplete. See details below.'),
                    ('FAILED', 'FAIL', noun + ' failed. See details below.')):
                with self.subTest(stage=stage, status=status):
                    self.err.seek(0)
                    self.err.truncate()
                    r.emit('stage_completed', branch='all branches' if stage == 'compare' else 'main',
                           stage=stage, backend='codex', status=status, elapsed_seconds=62)
                    prefix = '' if stage == 'compare' else 'main / '
                    self.assertEqual(self.err.getvalue(), f'[{label}] {prefix}{meaning} | Elapsed: 01:02\n')
        self.err.seek(0)
        self.err.truncate()
        r.emit('stage_recovered', branch='main', stage='study', backend='codex',
               message='Text retained; policy checks not completed')
        r.emit('stage_skipped', branch='main', stage='review', backend='codex')
        self.assertIn('[WARN] main / Report text saved, but it did not pass all checks.', self.err.getvalue())
        self.assertIn('[SKIP] main / Review skipped: no architecture report available.', self.err.getvalue())
        self.assertNotIn('[OK]', self.err.getvalue())
        self.assertNotIn('policy', self.err.getvalue())

    def test_normal_errors_use_clear_reasons_and_keep_useful_paths_and_next_steps(self):
        self.reporter.attach_log(self.base)
        errors = [
            (ContractError('unexpected enum value PRIVATE_MODEL_VALUE'), 'The response could not be used.'),
            (AuditError('ArchitectureDocument renderer failed at $.document', code='MATERIALIZATION_ERROR'),
             'The architecture report could not be created.'),
            (AuditError('Computed artifact failed local validation.', code='ARTIFACT_CONTRACT_ERROR'),
             'The report could not be saved because it failed validation.'),
            (FileNotFoundError(2, 'No such file or directory', '/missing/report.md'), '/missing/report.md'),
            (PermissionError(13, 'Permission denied', '/reports'), 'Permission denied'),
        ]
        for error, expected in errors:
            with self.subTest(error=type(error).__name__, expected=expected):
                self.err.seek(0)
                self.err.truncate()
                self.reporter.error(error, phase='stage', branch='main', stage='study', backend='codex')
                text = self.err.getvalue()
                self.assertIn(expected, text)
                self.assertIn(str(self.base / 'run.log'), text)
                self.assertNotIn('Code:', text)
                self.assertNotIn('$.document', text)
                self.assertNotIn('PRIVATE_MODEL_VALUE', text)
                if isinstance(error, OSError):
                    self.assertIn('Next step:', text)
                    self.assertIn('access permissions', text)

    def test_timeout_and_cleanup_errors_keep_distinct_reasons_and_verbose_diagnostics(self):
        for kind, layer, expected in (
                ('BACKEND_ERROR', 'cleanup', 'The agent could not be stopped or cleaned up completely.'),
                ('STAGE_TIMEOUT', 'execution', 'The operation exceeded its time limit.'),
                ('IDLE_TIMEOUT', 'execution', 'No activity was detected within the time limit.')):
            for verbose in (False, True):
                with self.subTest(kind=kind, layer=layer, verbose=verbose):
                    err = io.StringIO()
                    reporter = Reporter(stdout=self.out, stderr=err, verbose=verbose)
                    self.addCleanup(reporter.close)
                    message = f'Detailed failure: {kind} / {layer}'
                    error = ContractError(message, failure_kind=kind, failure_layer=layer, safe=True)
                    reporter.error(error, phase='stage', branch='main', stage='study')
                    self.assertIn(message if verbose else expected, err.getvalue())
                    self.assertEqual('Code: INVALID_RESPONSE' in err.getvalue(), verbose)
                    self.assertNotIn('The response could not be used.', err.getvalue())

    def test_summary_shows_existing_reports_and_diagnostic_paths_only_when_verbose(self):
        final_report = self.base / 'FINAL_REPORT.md'
        report = self.base / 'ARCHITECTURE.md'
        manifest_path = self.base / 'manifest.json'
        for path in (final_report, report, manifest_path):
            path.write_text('fixture')
        manifest = {'mode': 'folder', 'accepted': True, 'has_usable_material': True,
                    'final_report': str(final_report),
                    'study_invocation': {'publication_complete': True},
                    'review_invocation': {'publication_complete': True}}
        result = {'run_id': 'test-run', 'status': 'COMPLETE', 'manifest': str(manifest_path), 'exit_code': 0}
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                out = io.StringIO()
                reporter = Reporter(stdout=out, stderr=self.err, verbose=verbose)
                self.addCleanup(reporter.close)
                logs = self.base / str(verbose)
                logs.mkdir()
                reporter.attach_log(logs)
                reporter.finish(result, manifest, check_only=False,
                                config_path=Path('config.jsonc'), run_dir=self.base)
                text = out.getvalue()
                self.assertIn(str(final_report), text)
                self.assertIn(str(report), text)
                self.assertNotIn('ARCHITECTURE_REVIEW.md', text)
                self.assertEqual('Manifest:' in text, verbose)
                self.assertEqual(str(manifest_path) in text, verbose)
                self.assertEqual('Technical log:' in text, verbose)
                self.assertEqual(str(logs / 'run.log') in text, verbose)
                for redundant in ('Source result:', 'Comparison:', 'Restoration:', 'test-run'):
                    self.assertNotIn(redundant, text)

    def test_early_buffer_private_log_full_context_and_close(self):
        root_handlers = list(logging.getLogger().handlers)
        r = self.reporter
        r.run_id = 'test-run'
        r.emit('preflight_completed', check='configuration')
        before = self.err.getvalue()
        r.attach_log(self.base)
        self.assertEqual(self.err.getvalue(), before)
        sha = 'a' * 40
        r.emit('stage_started', branch='深い branch', commit=sha, stage='study', backend='codex')
        self.assertNotIn(sha, self.err.getvalue())
        self.assertNotIn('Timeout:', self.err.getvalue())
        records = [json.loads(line) for line in (self.base / 'run.log').read_text().splitlines()]
        self.assertEqual([r['event'] for r in records], ['preflight_completed', 'stage_started'])
        self.assertEqual(records[1]['commit'], sha)
        self.assertEqual(records[1]['run_id'], 'test-run')
        self.assertNotIn('timeout_seconds', records[1])
        self.assertTrue(records[0]['time'].endswith('+00:00'))
        self.assertEqual(records[0]['level'], 'INFO')
        self.assertEqual(stat.S_IMODE((self.base / 'run.log').stat().st_mode), 0o600)
        handler = r.handler
        r.close()
        self.assertTrue(handler.stream.closed)
        self.assertEqual(r.logger.handlers, [])
        self.assertEqual(logging.getLogger().handlers, root_handlers)

    def test_errors_have_one_root_context_and_safe_full_chain_in_log(self):
        r = self.reporter
        r.attach_log(self.base)
        try:
            try:
                raise AuditError('The submodule is not initialized locally.', code='SUBMODULE_NOT_INITIALIZED',
                                 node_path='lib/deep child', required_commit='b' * 40)
            except AuditError as cause:
                raise cause.with_context(snapshot='original', node_path='.') from cause
        except AuditError as exc:
            self.assertIsNotNone(exc.__cause__)
            self.assertEqual(exc.node_path, 'lib/deep child')
            r.error(exc, phase='preflight', analysis_started=False, switches_performed=False)
        console = self.err.getvalue()
        self.assertNotIn('Snapshot:', console)
        self.assertNotIn('Required commit:', console)
        self.assertNotIn('Code:', console)
        self.assertIn('lib/deep child', console)
        self.assertEqual(console.count('The submodule is not initialized locally.'), 1)
        self.assertNotIn('Traceback', console)
        self.assertIn('Analysis has not started.', console)
        self.assertNotIn('No checkout switches were performed.', console)
        self.assertIn('Traceback', (self.base / 'run.log').read_text())
        self.assertIn('direct cause', (self.base / 'run.log').read_text())
        unsafe = UnsafeRepository('dirty').with_context(node_path='child').with_context(node_path='.')
        self.assertIsInstance(unsafe, UnsafeRepository)
        self.assertEqual(unsafe.node_path, 'child')

    def test_verbose_redacts_credentials_and_never_prints_model_values(self):
        secret = 'fixture-credential-value'
        with patch.dict(os.environ, {'SERVICE_TOKEN': secret}):
            r = Reporter(stdout=self.out, stderr=self.err, verbose=True)
        self.addCleanup(r.close)
        r.attach_log(self.base)
        r.error(AuditError('Cannot open https://user:password@host/path?token=abc&x=1 ' + secret))
        r.error(ContractError('unexpected enum value FULL_MODEL_RESPONSE'))
        r.error(RuntimeError('FULL_PROMPT_CONTENT'))
        text = self.err.getvalue() + (self.base / 'run.log').read_text()
        for value in (secret, 'user:password', 'token=abc', 'FULL_MODEL_RESPONSE', 'FULL_PROMPT_CONTENT'):
            self.assertNotIn(value, text)
        self.assertIn('INTERNAL_ERROR', text)
        self.assertIn('INVALID_RESPONSE', text)
        self.assertEqual(self.err.getvalue().count('The agent response failed contract validation.'), 1)

    def test_failed_console_and_file_sink_are_disabled_once(self):
        broken = Mock()
        broken.write.side_effect = BrokenPipeError()
        r = Reporter(stderr=broken)
        self.addCleanup(r.close)
        r.attach_log(self.base)
        handler = r.handler
        with patch.object(handler, 'emit', side_effect=OSError('disk full')):
            r.emit('root_warning')
            r.emit('root_warning')
        self.assertEqual(broken.write.call_count, 1)
        self.assertTrue(handler.stream.closed)
        self.assertIsNone(r.handler)

    def test_finish_once_json_nullable_and_no_missing_paths_in_text(self):
        result = {'run_id': None, 'status': 'FAILED', 'manifest': None, 'exit_code': 1}
        for mode in ('json', 'text'):
            out = io.StringIO()
            r = Reporter(mode=mode, stdout=out, stderr=io.StringIO())
            for _ in range(2):
                r.finish(result, {}, check_only=True, config_path=Path('config with spaces.json'), run_dir=self.base)
            if mode == 'json':
                self.assertEqual(json.loads(out.getvalue()), result)
            else:
                self.assertEqual(out.getvalue().count('[FAIL] Analysis failed.'), 1)
                self.assertNotIn('Manifest:', out.getvalue())
                self.assertNotIn('Technical log:', out.getvalue())
            r.close()


class ProcessReportingTests(unittest.TestCase):
    def simulate(self, *, active=False, closed=False, progress=True, duration=100):
        current = [0.0]
        clock = lambda: current[0]
        out, err = io.StringIO(), io.StringIO()
        r = Reporter(stdout=out, stderr=err, clock=clock, progress=progress)
        p = Mock(pid=123456, returncode=0)
        p.stdin, p.stdout, p.stderr = Mock(), Mock(), Mock()
        p.stdout.fileno.return_value = 1
        p.stderr.fileno.return_value = 2
        p.poll.side_effect = lambda: 0 if current[0] >= duration else None
        pending = {}
        selector = Mock()
        def register(stream, mask, name):
            pending[stream] = SimpleNamespace(fileobj=stream, data=name)
        selector.register.side_effect = register
        selector.unregister.side_effect = lambda stream: pending.pop(stream)
        selector.get_map.side_effect = lambda: pending
        def select(wait):
            current[0] += 10
            if (active and current[0] == 20) or (closed and current[0] >= 10) or current[0] >= duration:
                return [(item, 1) for item in list(pending.values())]
            return []
        selector.select.side_effect = select
        def read(fd, size):
            return b'private bytes' if active and current[0] == 20 else b''
        with patch('explain.subprocess.Popen', return_value=p) as spawn, \
                patch('explain.selectors.DefaultSelector', return_value=selector), \
                patch('explain.os.set_blocking'), patch('explain.os.read', side_effect=read), \
                patch('explain.os.killpg') as kill:
            result = process(['fake'], Path('/tmp'), {}, reporter=r, clock=clock,
                             context={'branch': 'master', 'stage': 'study', 'backend': 'opencode'})
        self.assertTrue(spawn.call_args.kwargs['close_fds'])
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        kill.assert_called_once_with(p.pid, signal.SIGKILL)
        r.close()
        return result, err.getvalue()

    def test_silent_active_intervals_and_no_progress_with_injected_clock(self):
        for active in (False, True):
            result, text = self.simulate(active=active)
            self.assertEqual(result['returncode'], 0)
            self.assertEqual(text.count('Elapsed:'), 3)
            for elapsed in ('00:30', '01:00', '01:30'):
                self.assertIn('Elapsed: ' + elapsed, text)
            self.assertEqual(text.count('[RUN] master / Analyzing project…'), 3)
            self.assertNotIn('CLI output', text)
            self.assertNotIn('private bytes', text)
            self.assertNotIn('Timeout:', text)
            _, quiet = self.simulate(active=active, progress=False)
            self.assertNotIn('Elapsed:', quiet)

    def test_long_process_waits_for_exit_even_with_closed_pipes(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                result, text = self.simulate(closed=closed, duration=1900)
                self.assertEqual(result['returncode'], 0)
                self.assertEqual(result['duration_seconds'], 1900)
                self.assertIn('Elapsed: 31:30', text)
                self.assertNotIn('Timeout:', text)
                self.assertNotIn('Stopping analysis', text)

    def test_streams_are_written_before_exit_without_changing_parser_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = root / 'release'
            payload = b'{"message":"Unicode \\u6df1"}\n'
            script = '''import os, pathlib, sys, time
os.write(1, %r)
os.write(2, b'private stderr\\n')
release = pathlib.Path(%r)
end = time.monotonic() + 3
while not release.exists() and time.monotonic() < end:
    time.sleep(0.01)
sys.exit(0 if release.exists() else 11)
''' % (payload, str(release))
            class Observer(NullReporter):
                progress = True
                def emit(observer, event, **context):
                    if event == 'process_waiting' and (root / 'stdout.log').read_bytes():
                        self.assertEqual((root / 'stdout.log').read_bytes(), payload)
                        self.assertEqual((root / 'stderr.log').read_bytes(), b'private stderr\n')
                        release.touch()
            result = process([sys.executable, '-B', '-c', script], root, cli_env(root),
                             log_dir=root, reporter=Observer(), progress_interval=0.05)
            self.assertEqual(result['returncode'], 0)
            self.assertEqual(result['stdout'], payload)
            self.assertEqual(result['stderr'], (root / 'stderr.log').read_bytes())
            for name in ('stdout', 'stderr'):
                self.assertEqual(stat.S_IMODE((root / (name + '.log')).stat().st_mode), 0o600)

    def test_broken_stdin_and_closed_real_pipes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = 'import os; os.close(0); os.write(1,b"x"*65536); os.write(2,b"y"*65536)'
            result = process([sys.executable, '-c', script], root, cli_env(root), b'prompt' * 100000,
                             log_dir=root)
            self.assertEqual(result['returncode'], 0)
            self.assertEqual(result['stdout'], b'x' * 65536)
            self.assertEqual(result['stderr'], b'y' * 65536)
            self.assertEqual(result['stdout'], (root / 'stdout.log').read_bytes())
            self.assertEqual(result['stderr'], (root / 'stderr.log').read_bytes())
            script = 'import os,time; os.close(1); os.close(2); time.sleep(0.3); raise SystemExit(17)'
            result = process([sys.executable, '-c', script], root, cli_env(root))
            self.assertEqual(result['returncode'], 17)
            self.assertGreaterEqual(result['duration_seconds'], 0.3)

    def test_log_descriptors_are_private_and_closed_after_spawn_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = '''import json, os, pathlib
targets = {(p.stat().st_dev, p.stat().st_ino) for p in pathlib.Path('.').glob('*.log')}
inherited = []
for fd in range(3, 64):
    try:
        info = os.fstat(fd)
    except OSError:
        continue
    if (info.st_dev, info.st_ino) in targets:
        inherited.append(fd)
print(json.dumps(inherited))
'''
            result = process([sys.executable, '-c', script], root, cli_env(root), log_dir=root)
            self.assertEqual(json.loads(result['stdout']), [])
            opened = []
            real_fdopen = os.fdopen
            def track(*args, **kwargs):
                stream = real_fdopen(*args, **kwargs)
                opened.append(stream)
                return stream
            with patch('explain.os.fdopen', side_effect=track), self.assertRaises(FileNotFoundError):
                process(['/no/such/cli'], root, cli_env(root), log_dir=root)
            self.assertEqual(len(opened), 2)
            self.assertTrue(all(stream.closed for stream in opened))


class ReportingCLIIntegrationTests(unittest.TestCase):
    setUp = startup.StartupCLIIntegrationTests.setUp
    tearDown = startup.StartupCLIIntegrationTests.tearDown
    git = startup.StartupCLIIntegrationTests.git
    prepare = startup.StartupCLIIntegrationTests.prepare

    def run_cli(self, args, *, accelerated=False, stdout=subprocess.PIPE):
        command = [sys.executable, '-B', str(ROOT / 'explain.py')]
        if accelerated:
            # Real entry point + real child CLI; shorten only presentation's interval.
            command = [sys.executable, '-B', '-c',
                'import runpy,sys; sys.path.insert(0,sys.argv[1]); from src.runtime import reporting; '
                'original=reporting.Reporter; reporting.Reporter=lambda **kw: original(progress_interval=0.05,**kw); '
                'sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")',
                str(ROOT), str(ROOT / 'explain.py')]
        return subprocess.run(command + args, cwd=self.base, env=self.env, stdout=stdout,
                              stderr=subprocess.PIPE, text=True, timeout=30)

    def test_single_branch_git_check_and_analysis(self):
        self.git('branch', '-D', 'test01', 'dev_01_customerA')
        self.assertEqual(self.git('branch', '--format=%(refname:short)').strip(), 'master')
        self.env['OPENCODE_CONFIG_CONTENT'] = json.dumps({
            'provider': {'custom': {'options': {'baseURL': 'https://example.invalid'}}}})
        for backend in ('codex', 'claude-code', 'opencode'):
            for check in (True, False):
                with self.subTest(backend=backend, check=check):
                    args = self.prepare('git', check=check)
                    value = jsonc(self.config_path.read_text())
                    value['git_mode']['branches'] = ['master']
                    value['agent']['backend'] = backend
                    value['stage_agents'] = {'compare': {'executable': '/missing/compare-cli'}}
                    value['prompts'] = {'compare': '/missing/compare-prompt'}
                    self.config_path.write_text(json.dumps(value))
                    result = self.run_cli(args + ['--output', 'text'])
                    if backend == 'opencode':
                        self.assertEqual(result.returncode, 1, result.stderr)
                        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
                        self.assertFalse(any('context' in call for call in calls))
                        self.assertEqual(self.repo.head(), self.master)
                        self.assertEqual(self.repo.symbolic(), 'master')
                        continue
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout.startswith('[OK] Local setup checked.' if check else '[OK] Analysis complete.'))
                    calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
                    invocations = [call for call in calls if 'context' in call]
                    self.assertEqual(len(calls), 2 if check else 5)
                    self.assertEqual(len(invocations), 0 if check else 3)
                    self.assertNotIn('Comparing branch reports', result.stderr)
                    self.assertNotIn('Branch report comparison ready', result.stderr)
                    manifests = list(self.reports.glob('*/manifest.json'))
                    path = max(manifests, key=lambda p: p.stat().st_mtime_ns)
                    manifest = json.loads(path.read_text())
                    self.assertEqual(manifest['pins'], {'master': self.master})
                    self.assertNotIn('comparison', manifest)
                    self.assertNotIn('comparison_invocation', manifest)
                    self.assertFalse((path.parent / 'comparison').exists())
                    if check:
                        self.assertEqual(manifest['switch_journal'], [])
                    else:
                        self.assertNotIn('Comparison:', result.stdout)
                        self.assertNotIn('Restoration:', result.stdout)
                        self.assertTrue(manifest['branches'][0]['accepted'])
                        branch_dir = path.parent / manifest['branches'][0]['directory']
                        for stage, report in (('study', 'ARCHITECTURE.md'),
                                              ('review', 'ARCHITECTURE_REVIEW.md')):
                            data = json.loads((branch_dir / (stage + '.json')).read_text())
                            self.assertEqual(data['branch'], 'master')
                            self.assertEqual(data['source_commit'], self.master)
                            self.assertTrue((branch_dir / report).is_file())
                        for call in invocations:
                            self.assertEqual(call['context']['source_mode'], 'git')
                            self.assertEqual(Path(call['cwd']), self.repo_path)
                    self.assertEqual(self.repo.symbolic(), 'master')
                    self.assertEqual(self.repo.head(), self.master)
                    self.repo.clean()

    def test_repository_names_from_origin_and_local_fallback(self):
        cases = [(None, self.repo_path.name), ('', self.repo_path.name),
                 ('https://user:private-password@example.test/org/remote-name.git/', 'remote-name'),
                 ('ssh://git@example.test:2222/org/remote-name.git', 'remote-name'),
                 ('git@example.test:org/remote-name.git', 'remote-name'),
                 ('git@example.test:remote-name.git', 'remote-name'),
                 ('/local/深い remote.git/', '深い remote'), ('../remote-name', 'remote-name'),
                 ('https://example.test/', self.repo_path.name),
                 ('ssh://[invalid/path', self.repo_path.name),
                 ('/local/unsafe\n\x1b[31m.git', 'unsafe\n\x1b[31m')]
        for origin, expected in cases:
            with self.subTest(origin=origin):
                if origin is not None:
                    self.git('config', 'remote.origin.url', origin)
                self.assertEqual(self.repo.display_name(), expected)

    def test_headers_and_folder_stage_names_in_checks_and_analysis(self):
        self.git('config', 'remote.origin.url', 'https://user:private-password@example.test/org/remote-name.git')
        for mode in ('folder', 'git'):
            for check in (True, False):
                with self.subTest(mode=mode, check=check):
                    result = self.run_cli(self.prepare(mode, check=check) + ['--output', 'text'])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stderr.startswith('[RUN] Checking local setup.' if check else '[RUN] Preparing analysis.'))
                    self.assertNotIn('Mode:', result.stderr)
                    self.assertIn(f'Source: {self.folder if mode == "folder" else self.repo_path}', result.stderr)
                    self.assertNotIn('Agents:', result.stderr)
                    self.assertNotIn('private-password', result.stdout + result.stderr)
                    if not check:
                        if mode == 'folder':
                            for activity, complete in (('Analyzing project…', 'Architecture report created.'),
                                                       ('Reviewing report…', 'Review complete. No significant issues reported.')):
                                self.assertIn(f'[RUN] {self.folder.name} / {activity}', result.stderr)
                                self.assertIn(f'[OK] {self.folder.name} / {complete}', result.stderr)
                        else:
                            self.assertIn('[RUN] master / Analyzing project…', result.stderr)
                            self.assertIn('[RUN] Comparing branch reports…', result.stderr)
                            self.assertNotIn('all branches /', result.stderr)

    def test_stage_report_paths_in_both_modes_with_json_and_no_progress(self):
        for mode in ('folder', 'git'):
            for quiet in (False, True):
                with self.subTest(mode=mode, no_progress=quiet):
                    args = self.prepare(mode) + ['--output', 'json'] + (['--no-progress'] if quiet else [])
                    result = self.run_cli(args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    summary = json.loads(result.stdout)
                    run_dir = Path(summary['manifest']).parent
                    manifest = json.loads(Path(summary['manifest']).read_text())
                    expected = []
                    for source in manifest['branches'] if mode == 'git' else [manifest]:
                        directory = run_dir / source.get('directory', '.')
                        expected.append(directory / 'SUBSYSTEM_CATALOG.md')
                        for revision in source['revisions']:
                            expected.extend(run_dir / revision['directory'] / name
                                            for name in ('ARCHITECTURE.md', 'ARCHITECTURE_REVIEW.md'))
                    if mode == 'git':
                        expected.append(run_dir / 'comparison/BRANCH_COMPARISON.md')
                    lines = result.stderr.splitlines()
                    indices = [i for i, line in enumerate(lines) if line.startswith('      Report: ')]
                    self.assertEqual([lines[i] for i in indices], ['      Report: ' + str(p) for p in expected])
                    for index, path in zip(indices, expected):
                        self.assertIn(' | Elapsed: ', lines[index - 1])
                        self.assertTrue(lines[index - 1].startswith('[OK] '))
                        self.assertTrue(path.is_file(), path)
                    records = [json.loads(line) for line in (run_dir / 'run.log').read_text().splitlines()]
                    completed = [r for r in records if r['event'] == 'stage_completed']
                    self.assertEqual([r['report_path'] for r in completed], [str(p) for p in expected])
                    self.assertNotIn('Report:', result.stdout)
                    self.assertNotIn('\x1b', result.stderr)

    def test_missing_origin_falls_back_without_masking_preflight_errors(self):
        args = self.prepare('git', check=True)
        result = self.run_cli(args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'Source: {self.repo_path}', result.stderr)
        (self.repo_path / '.git' / 'HEAD').write_text('invalid HEAD\n')
        result = self.run_cli(args)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f'Source: {self.repo_path}', result.stderr)
        self.assertIn('Invalid detached HEAD', result.stderr)
        self.assertIsNotNone(json.loads(result.stdout)['manifest'])

    def test_modes_real_tty_and_redirection(self):
        args = self.prepare('folder', check=True)
        for mode in ('auto', 'text', 'json'):
            for tty in (False, True):
                with self.subTest(mode=mode, tty=tty):
                    if tty:
                        master, slave = pty.openpty()
                        try:
                            result = self.run_cli(args + ['--output', mode], stdout=slave)
                            self.assertTrue(select.select([master], [], [], 2)[0])
                            output = os.read(master, 65536).decode()
                        finally:
                            os.close(master)
                            if slave is not None:
                                os.close(slave)
                    else:
                        result = self.run_cli(args + ['--output', mode])
                        output = result.stdout
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if mode == 'json' or mode == 'auto' and not tty:
                        self.assertEqual(json.loads(output)['status'], 'PREFLIGHT_OK')
                        self.assertNotIn('Mode:', result.stderr)
                    else:
                        self.assertIn('Local setup checked. Analysis has not started.', output)
                        self.assertIn('AI service access and model availability were not checked.', output)
                        self.assertIn('Start analysis:', output)
                    self.assertNotIn('Source inventory and fingerprint', result.stderr)
                    self.assertNotIn('Checking source inventory', result.stderr)
                    self.assertNotIn('authentication', result.stderr)
                    self.assertNotIn('\x1b', result.stderr)

    def test_real_stderr_tty_colors_labels_while_json_and_logs_stay_plain(self):
        args = self.prepare('folder')
        self.env['TERM'] = 'xterm'
        self.env.pop('NO_COLOR', None)
        master, slave = pty.openpty()
        chunks, stop = [], threading.Event()
        def drain_console():
            while not stop.is_set() or select.select([master], [], [], 0)[0]:
                if select.select([master], [], [], .05)[0]:
                    chunks.append(os.read(master, 65536))
        reader = threading.Thread(target=drain_console)
        reader.start()
        try:
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'explain.py'), *args, '--output', 'json'],
                cwd=self.base, env=self.env, stdout=subprocess.PIPE, stderr=slave, text=True, timeout=30)
        finally:
            stop.set()
            reader.join(timeout=2)
            os.close(master)
            os.close(slave)
        console = b''.join(chunks).decode()
        self.assertEqual(result.returncode, 0, console)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['status'], 'COMPLETE')
        self.assertNotIn('\x1b', result.stdout)
        self.assertIn('\x1b[36m[RUN]\x1b[0m source folder / Analyzing project…', console)
        self.assertIn('\x1b[32m[OK]\x1b[0m source folder / Architecture report created. | Elapsed:', console)
        self.assertNotIn('Configuration', console)
        run_dir = Path(summary['manifest']).parent
        logs = [run_dir / 'run.log', *run_dir.glob('**/*.logs/attempt-001/stdout.log'), *run_dir.glob('**/*.logs/attempt-001/stderr.log')]
        self.assertEqual(len(logs), 7)
        for path in logs:
            self.assertNotIn(b'\x1b', path.read_bytes(), str(path))

    def test_real_stdout_tty_colors_text_summary_but_never_json(self):
        args = self.prepare('folder', check=True)
        self.env['TERM'] = 'xterm'
        self.env.pop('NO_COLOR', None)
        for mode in ('text', 'json'):
            with self.subTest(mode=mode):
                master, slave = pty.openpty()
                try:
                    result = self.run_cli(args + ['--output', mode], stdout=slave)
                    self.assertTrue(select.select([master], [], [], 2)[0])
                    output = os.read(master, 65536).decode()
                finally:
                    os.close(master)
                    os.close(slave)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('\x1b', result.stderr)
                if mode == 'text':
                    self.assertIn('\x1b[32m[OK]\x1b[0m Local setup checked.', output)
                else:
                    self.assertEqual(json.loads(output)['status'], 'PREFLIGHT_OK')
                    self.assertNotIn('\x1b', output)

    def test_early_errors_nullable_missing_git_and_unavailable_results(self):
        cases = []
        missing = self.base / 'missing config.json'
        cases.append((['--config', str(missing)], None))
        args = self.prepare('git', check=True)
        self.env['PATH'] = ''
        result = self.run_cli(args)
        self.assertEqual(result.returncode, 1)
        self.assertIn('git was not found', result.stderr)
        self.assertIn(f'Source: {self.repo_path}', result.stderr)
        self.assertIsNone(json.loads(result.stdout)['manifest'])
        self.assertNotIn('Error details:', result.stderr)
        args = self.prepare('folder', check=True)
        self.reports.write_text('a file instead of a directory')
        cases.append((args, 'assigned'))
        for args, assigned in cases:
            result = self.run_cli(args)
            data = json.loads(result.stdout)
            self.assertEqual(data['exit_code'], 1)
            self.assertIsNone(data['manifest'])
            self.assertEqual(data['run_id'] is None, assigned is None)
            self.assertNotIn('Error details:', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        text = self.run_cli(['--config', str(missing), '--output', 'text'])
        self.assertIn('[FAIL] Analysis failed.', text.stdout)
        self.assertNotIn('Manifest:', text.stdout)

    def test_argparse_exceptions_create_no_run(self):
        for args in (['--help'], [], ['--config', 'x', '--output', 'invalid']):
            result = self.run_cli(args)
            self.assertIn('usage:', result.stdout + result.stderr)
            self.assertNotIn('run_id', result.stdout)
            self.assertFalse(self.reports.exists())

    def test_control_characters_in_paths_are_escaped_and_json_paths_remain_exact(self):
        target = self.base / 'source 深い with spaces\n\x1b[31m'
        self.folder.rename(target)
        self.folder = target
        args = self.prepare('folder', check=True)
        result = self.run_cli(args + ['--output', 'text', '--verbose'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(safe_text(target), result.stderr)
        self.assertIn('Source: ' + safe_text(target), result.stderr)
        self.assertNotIn('\x1b', result.stdout + result.stderr)
        result = self.run_cli(args)
        data = json.loads(result.stdout)
        self.assertEqual(json.loads(Path(data['manifest']).read_text())['source_directory'], str(target))

    def test_broken_stdout_does_not_replace_exit_code_or_skip_cleanup(self):
        args = self.prepare('git')
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            result = self.run_cli(args, stdout=write_fd)
        finally:
            os.close(write_fd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('BrokenPipe', result.stderr)
        self.assertEqual(self.repo.symbolic(), 'master')
        manifests = list(self.reports.glob('*/manifest.json'))
        self.assertEqual(len(manifests), 1)
        self.assertTrue(json.loads(manifests[0].read_text())['restoration']['restored'])

    def test_broken_stderr_preserves_success_and_early_failure_exit_codes(self):
        success = self.prepare('git')
        for args, expected in ((success, 0), (['--config', str(self.base / 'missing')], 1)):
            with self.subTest(expected=expected):
                read_fd, write_fd = os.pipe()
                os.close(read_fd)
                try:
                    result = subprocess.run([sys.executable, '-B', str(ROOT / 'explain.py'), *args],
                        cwd=self.base, env=self.env, stdout=subprocess.PIPE, stderr=write_fd,
                        text=True, timeout=30)
                finally:
                    os.close(write_fd)
                self.assertEqual(result.returncode, expected)
                self.assertEqual(json.loads(result.stdout)['exit_code'], expected)
        self.assertEqual(self.repo.symbolic(), 'master')

    @unittest.skipIf(os.geteuid() == 0, 'Root can access this directory; non-root permissions test.')
    def test_inaccessible_results_directory_still_returns_one_result(self):
        args = self.prepare('folder', check=True)
        self.reports.mkdir(mode=0o700)
        self.reports.chmod(0)
        try:
            result = self.run_cli(args)
        finally:
            self.reports.chmod(0o700)
        data = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(data['exit_code'], 1)
        self.assertIsNone(data['manifest'])
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('Error details:', result.stderr)

    def test_internal_error_is_distinct_from_configuration_error(self):
        args = self.prepare('folder', check=True)
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['explain.py', *args]), patch.dict(os.environ, self.env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(Runner, 'check_cli', side_effect=RuntimeError('private internal value')):
            self.assertEqual(main(), 1)
        data = json.loads(out.getvalue())
        log = (Path(data['manifest']).parent / 'run.log').read_text()
        self.assertNotIn('INTERNAL_ERROR', err.getvalue())
        self.assertIn('INTERNAL_ERROR', log)
        self.assertIn('Traceback', log)
        self.assertNotIn('Traceback', err.getvalue())
        self.assertNotIn('private internal value', err.getvalue() + log)

    def test_activity_no_progress_and_streaming_log_privacy(self):
        args = self.prepare('folder')
        for kind in ('wait', 'active', 'closed-pipes'):
            for quiet in (False, True):
                with self.subTest(kind=kind, quiet=quiet):
                    self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'study', 'kind': kind, 'seconds': 1.4 if kind == 'closed-pipes' else 0.4})
                    result = self.run_cli(args + ['--verbose'] + (['--no-progress'] if quiet else []), accelerated=True)
                    output = json.loads(result.stdout)
                    self.assertEqual(output['exit_code'], 1 if kind == 'closed-pipes' else 0, result.stderr)
                    self.assertEqual('      Elapsed:' in result.stderr, not quiet)
                    if not quiet:
                        self.assertIn('Last CLI output:' if kind == 'active' else 'No CLI output received yet', result.stderr)
                        self.assertIn('[RUN] source folder / Analyzing project…\n      Elapsed:', result.stderr)
                    self.assertNotIn('private CLI activity', result.stderr)
                    self.assertNotIn('Timeout:', result.stderr)
                    log = Path(output['manifest']).parent / 'run.log'
                    self.assertNotIn('private CLI activity', log.read_text())
                    self.assertNotIn('Authoritative orchestration context', log.read_text())
                    self.assertNotIn('timeout_seconds', log.read_text())
                    if kind == 'closed-pipes':
                        self.assertIn('CLI_FAILED', result.stderr)
                        self.assertNotIn('[OK] source folder / Architecture report created.', result.stderr)
                        self.assertIn('[FAIL] source folder / Project analysis failed', result.stderr)

    def test_large_stage_input_reaches_cli_without_truncation(self):
        args = self.prepare('folder')
        cfg = json.loads(self.config_path.read_text().split('\n', 1)[1])
        description = 'Контекст\n' * 100000
        cfg['project_description'] = description
        self.config_path.write_text(json.dumps(cfg))
        result = self.run_cli(args)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        run = Path(output['manifest']).parent
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        model_calls = [call for call in calls if 'context' in call]
        self.assertEqual(len(model_calls), 3)
        for call, stage in zip(model_calls, ('catalog', 'study', 'review')):
            self.assertEqual(call['context']['project_description'], description.strip())
            logs = (run if stage == 'catalog' else run / 'revisions/001') / (stage + '.logs')
            meta = json.loads((logs / 'invocation.json').read_text())
            payload = (logs / meta['attempt'] / 'input.prompt.txt').read_bytes()
            self.assertEqual(meta['status'], 'SUCCEEDED')
            self.assertEqual(meta['input_bytes'], len(payload))
            self.assertGreater(len(payload), 800000)

    def test_partial_and_invalid_response_are_not_reported_as_success(self):
        args = self.prepare('folder')
        self.env['AUDIT_TEST_PARTIAL'] = '1'
        result = self.run_cli(args + ['--output', 'text'])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(result.stdout.startswith('[WARN] Analysis may be incomplete. See available results and limitations below.'))
        self.assertIn('[WARN] source folder / Project analysis may be incomplete. See details below.', result.stderr)
        del self.env['AUDIT_TEST_PARTIAL']
        self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'study', 'kind': 'invalid', 'value': 'PRIVATE_MODEL_VALUE'})
        result = self.run_cli(args + ['--verbose'])
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        run = Path(data['manifest']).parent
        self.assertNotIn('PRIVATE_MODEL_VALUE', result.stderr + (run / 'run.log').read_text())
        self.assertIn('PRIVATE_MODEL_VALUE', (run / 'revisions/001/study.logs/attempt-001/stdout.log').read_text())
        self.assertIn('INVALID_RESPONSE', result.stderr)
        self.assertNotIn('[OK] source folder / Architecture report created.', result.stderr)
        errors = [json.loads(line) for line in (run / 'run.log').read_text().splitlines()
                  if json.loads(line)['event'] == 'error']
        self.assertEqual((errors[0]['branch'], errors[0]['stage'], errors[0]['backend']),
                         ('folder', 'study', 'codex'))
        self.assertEqual(errors[0]['source_name'], 'source folder')

    def test_interrupt_stops_process_before_verified_restoration(self):
        args = self.prepare('git')
        self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'study', 'kind': 'interrupt'})
        result = self.run_cli(args)
        data = json.loads(result.stdout)
        self.assertEqual(result.returncode, 130, result.stderr)
        manifest = json.loads(Path(data['manifest']).read_text())
        self.assertTrue(manifest['restoration']['restored'])
        self.assertEqual(data['metrics'], manifest['metrics'])
        self.assertEqual(data['metrics']['usage']['total_tokens'], 120)
        self.assertEqual(data['metrics']['usage']['coverage']['total_tokens'], 'partial')
        self.assertEqual(result.stderr.count('[WARN] Stopping analysis…'), 1)
        for message in ('Stopping the active CLI process', 'Restoring the original checkout hierarchy',
                        'Original checkout hierarchy restored'):
            self.assertNotIn(message, result.stderr)
        records = [json.loads(line) for line in (Path(data['manifest']).parent / 'run.log').read_text().splitlines()]
        events = [record['event'] for record in records]
        expected = ['stop_requested', 'process_stopping', 'restoration_started', 'restoration_completed']
        positions = [events.index(event) for event in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(self.repo.symbolic(), 'master')

    def test_broken_console_log_failure_and_repeated_main_cleanup(self):
        args = self.prepare('git', check=True)
        handlers = list(logging.getLogger().handlers)
        reporters = []
        def factory(**kw):
            reporter = Reporter(**kw)
            reporters.append(reporter)
            return reporter
        for _ in range(2):
            out, err = io.StringIO(), io.StringIO()
            with patch('sys.argv', ['explain.py', *args]), patch.dict(os.environ, self.env, clear=True), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch('explain.Reporter', side_effect=factory):
                self.assertEqual(main(), 0)
            data = json.loads(out.getvalue())
            records = [json.loads(x) for x in (Path(data['manifest']).parent / 'run.log').read_text().splitlines()]
            self.assertEqual(sum(x['event'] == 'run_completed' for x in records), 1)
            self.assertEqual(sum(x['event'] == 'run_started' for x in records), 1)
            self.assertFalse(reporters[-1].logger.handlers)
        self.assertEqual(logging.getLogger().handlers, handlers)
        args = self.prepare('git')
        broken = Mock()
        broken.write.side_effect = BrokenPipeError()
        out = io.StringIO()
        with patch('sys.argv', ['explain.py', *args]), patch.dict(os.environ, self.env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(broken), \
                patch('src.runtime.reporting.RunLogHandler.emit', side_effect=OSError('disk full')):
            self.assertEqual(main(), 0)
        manifest = json.loads(Path(json.loads(out.getvalue())['manifest']).read_text())
        self.assertTrue(manifest['restoration']['restored'])
        self.assertEqual(broken.write.call_count, 1)


class RecursiveDiagnosticTests(recursive.RecursiveFixture, unittest.TestCase):
    def test_uninitialized_leaf_reports_one_reason_and_real_node(self):
        (self.paths[recursive.LEAF] / '.git').unlink()
        result, manifest = self.execute(check=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Snapshot:', result.stderr)
        self.assertIn(recursive.LEAF, result.stderr)
        self.assertEqual(result.stderr.count('The submodule is not initialized locally.'), 1)
        detail = manifest['diagnostics'][0]
        self.assertEqual(detail['code'], 'SUBMODULE_NOT_INITIALIZED')
        self.assertEqual(detail['node_path'], recursive.LEAF)
        self.assertEqual(detail['required_commit'], self.expected['master'][recursive.LEAF]['commit'])
        self.assertEqual(manifest['switch_journal'], [])
        self.assertIn('Analysis has not started.', result.stderr)
        self.assertNotIn('No checkout switches were performed.', result.stderr)

    def test_primary_and_restoration_failures_are_both_visible(self):
        self.write_config()
        out, err = io.StringIO(), io.StringIO()
        reporter = Reporter(stdout=out, stderr=err)
        self.addCleanup(reporter.close)
        runner = Runner(load_config(self.config_path), self.base / 'run', reporter=reporter)
        def fail(*args):
            (self.paths[recursive.LEAF] / 'app.py').write_text('external change')
            raise AuditError('PRIMARY agent failure')
        with patch.object(runner, 'check_cli', return_value={}), patch.object(runner, 'invoke', side_effect=fail):
            manifest, code = runner.run()
        self.assertEqual(code, 1)
        self.assertIn('PRIMARY agent failure', err.getvalue())
        self.assertIn('[FAIL] Could not return the repository to its original state.', err.getvalue())
        self.assertEqual(manifest['restoration']['node'], recursive.LEAF)
        self.assertFalse(manifest['restoration']['restored'])
        self.assertGreaterEqual(len(manifest['diagnostics']), 2)


if __name__ == '__main__':
    unittest.main()
