# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Stage-owned presentation: deterministic ticks and real offline PTY/HTTP I/O."""
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch

from src.contracts.contracts import ContractError
from src.runtime.execution import Budget
from src.runtime.metrics import RunMetrics
from explain import Runner
from src.runtime.reporting import Reporter, SPINNER, cell_width
import test_xxx as xxx_fixtures
import test_reporting as cli_fixtures

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = {'branch': 'master', 'stage': 'study', 'backend': 'xxx'}
SGR = re.compile(r'\x1b\[[0-9;]*m')


class Terminal(io.StringIO):
    encoding = 'utf-8'

    def isatty(self):
        return True


class ManualReporter(Reporter):
    """Drive the production tick explicitly; no sleeps or background races."""
    columns = 100

    def _terminal_columns(self):
        return self.columns

    def _start_worker(self, state):
        pass


class ProgressTests(unittest.TestCase):
    def reporter(self, *, tty=True, out_tty=False, env=None, **options):
        self.now = 100.0
        self.out, self.err = Terminal() if out_tty else io.StringIO(), Terminal() if tty else io.StringIO()
        with patch.dict(os.environ, {'TERM': 'xterm', 'NO_COLOR': '1'} | (env or {})):
            r = ManualReporter(stdout=self.out, stderr=self.err, clock=lambda: self.now, **options)
        self.addCleanup(r.close)
        return r

    def tick(self, r, elapsed):
        self.now = 100 + elapsed
        r._tick_progress(r._progress)

    def start(self, r, **context):
        r.emit('stage_started', _started=self.now, **(CONTEXT | context))
        return r._progress

    def test_immediate_line_delayed_animation_and_stage_elapsed(self):
        r = self.reporter()
        state = self.start(r)
        initial = self.err.getvalue()
        self.assertEqual(initial, '[RUN] master / Analyzing project…  ⠋ 00:00\x1b[K')
        for elapsed in (0.125, 0.25, 0.499):
            self.tick(r, elapsed)
        self.assertEqual(self.err.getvalue(), initial)
        for elapsed, frame, timer in ((.5, '⠙', '00:00'), (.625, '⠹', '00:00'),
                                      (1, '⠴', '00:01'), (134, None, '02:14'), (3661, None, '01:01:01')):
            self.tick(r, elapsed)
            line = self.err.getvalue().split('\r')[-1]
            self.assertTrue(line.startswith('[RUN] master / Analyzing project…  '))
            self.assertIn((frame + ' ' if frame else '') + timer, line)
        r.emit('stage_completed', **CONTEXT, status='COMPLETE', elapsed_seconds=3661,
               report_path='/reports/深い folder/ARCHITECTURE.md')
        self.assertTrue(state.stop.is_set())
        self.assertIsNone(r._progress)
        before = self.err.getvalue()
        r._tick_progress(state)
        self.assertEqual(self.err.getvalue(), before)
        self.assertTrue(before.endswith('[OK] master / Architecture report created. | Elapsed: 01:01:01\n'
                                        '      Report: /reports/深い folder/ARCHITECTURE.md\n'))

    def test_runner_only_announces_existing_report_and_escapes_its_path(self):
        r = self.reporter(tty=False, mode='json', progress=False)
        runner = Runner.__new__(Runner)
        runner.reporter, runner.mode, runner.source_path = r, 'git', Path('/source')
        runner.metrics = RunMetrics(r.clock)
        runner.cfg = {'_agents': {'study': {'backend': 'codex'}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '深い\n\x1b[31m.md'
            for exists in (False, True):
                if exists:
                    path.write_text('report')
                self.err.seek(0)
                self.err.truncate()
                runner.stage_finished('study', {}, {'completion_status': 'PARTIAL'}, self.now,
                                      report_path=path)
                text = self.err.getvalue()
                self.assertEqual('      Report: ' in text, exists)
                if exists:
                    self.assertIn('深い\\n\\x1b[31m.md\n', text)
                    self.assertNotIn('\x1b', text)
                self.assertIn('[WARN]', text)
                self.assertEqual(self.out.getvalue(), '')

    def test_short_stage_has_only_initial_frame(self):
        r = self.reporter()
        self.start(r)
        self.tick(r, .4)
        r.emit('stage_completed', **CONTEXT, status='PARTIAL', elapsed_seconds=.4)
        self.assertEqual(sum(self.err.getvalue().count(c) for c in SPINNER), 1)
        self.assertIn('master / Project analysis may be incomplete. See details below. | Elapsed: 00:00\n', self.err.getvalue())

    def test_runner_context_retries_and_next_stage_reset(self):
        r = self.reporter()
        runner = Runner.__new__(Runner)
        runner.reporter, runner.mode, runner.source_path = r, 'git', Path('/source/name')
        runner.metrics = RunMetrics(r.clock)
        runner.cfg = {'_agents': {stage: {'backend': backend} for stage, backend in
                      (('study', 'xxx'), ('review', 'codex'), ('compare', 'claude-code'))}}
        started = runner.stage_started('study', {'branch': 'topic'})
        self.assertEqual(started, r._progress.started)
        state = r._progress
        self.tick(r, 1)
        # Retries, repairs and ordinary warnings leave the stage clock intact.
        r.cli_started()
        self.tick(r, 2)
        r.cli_started()
        r.emit('stage_recovered', message='Partial material retained')
        self.assertIs(r._progress, state)
        self.assertEqual(r._progress.started, started)
        runner.stage_finished('study', {'branch': 'topic'}, {'completion_status': 'COMPLETE'}, started)
        runner.mode = 'folder'
        started = runner.stage_started('review', {})
        self.assertIn('[RUN] name / Reviewing report…  ⠋ 00:00', self.err.getvalue())
        runner.stage_finished('review', {}, {'completion_status': 'COMPLETE', 'verdict': 'INCONCLUSIVE'}, started)
        self.assertIn('name / Report review may be incomplete. See details below.', self.err.getvalue())
        runner.mode = 'git'
        started = runner.stage_started('compare', {})
        self.assertIn('[RUN] Comparing branch reports…  ⠋ 00:00', self.err.getvalue())
        runner.stage_finished('compare', {}, {'completion_status': 'BLOCKED'}, started)
        self.assertIsNone(r._progress)

    def test_plain_waiting_and_cli_activity_share_one_schedule(self):
        for tty, env in ((False, {}), (True, {'TERM': 'dumb'})):
            with self.subTest(tty=tty, env=env):
                r = self.reporter(tty=tty, env=env)
                self.start(r)
                self.tick(r, 29.9)
                self.assertNotIn('Elapsed:', self.err.getvalue())
                self.tick(r, 30)
                self.assertNotIn('CLI', self.err.getvalue())
                self.assertTrue(r.cli_started())
                self.tick(r, 50)
                r.cli_output()
                self.tick(r, 60)
                self.tick(r, 60)
                self.assertNotIn('CLI', self.err.getvalue())
                r.cli_started()
                self.tick(r, 90)
                self.assertNotIn('CLI', self.err.getvalue())
                for timer in ('00:30', '01:00', '01:30'):
                    self.assertIn('[RUN] master / Analyzing project…\n      Elapsed: ' + timer,
                                  self.err.getvalue())
                self.assertEqual(self.err.getvalue().count('Elapsed:'), 3)
                self.assertNotIn('\x1b', self.err.getvalue())
                r.close()

    def test_no_progress_and_non_analysis_events_never_start_worker(self):
        r = self.reporter(progress=False)
        with patch.object(r, '_start_worker') as worker:
            self.start(r)
            r.emit('process_waiting', **CONTEXT, elapsed_seconds=60)
            r.emit('stage_completed', **CONTEXT, status='COMPLETE', elapsed_seconds=60)
            r.error(OSError('test failure'), phase='stage', **CONTEXT)
            worker.assert_not_called()
        self.assertNotIn('\x1b', self.err.getvalue())
        self.assertEqual(self.err.getvalue().count('Elapsed:'), 1)
        self.assertIn('[RUN] master / Analyzing project…\n', self.err.getvalue())
        self.assertIn('[FAIL] master / Project analysis failed.', self.err.getvalue())
        r = self.reporter()
        for event, context in (('preflight_started', {'check': 'configuration'}),
                               ('branch_started', {'branch': 'master', 'commit': 'a' * 40}),
                               ('restoration_started', {})):
            r.emit(event, **context)
            self.assertIsNone(r._progress)

    def test_warning_multiline_verbose_and_log_failure_restore_line(self):
        r = self.reporter(verbose=True)
        self.start(r)
        self.tick(r, .625)
        r.write(self.err, '[WARN] retained material\nsecond line')
        r.emit('stage_recovered', message='still running')
        r.disable_log()
        text = self.err.getvalue()
        self.assertIn('\r\x1b[2K[WARN] retained material\nsecond line\n[RUN] master / Analyzing project…  ⠹', text)
        self.assertIn('Detail: stage_recovered', text)
        self.assertIn('Error details could not be saved. Analysis will continue.', text)
        self.assertTrue(text.endswith('[RUN] master / Analyzing project…  ⠹ 00:00\x1b[K'))
        self.assertIsNotNone(r._progress)

    def test_log_write_failure_inside_tick_warns_once_and_keeps_stage_active(self):
        r = self.reporter()
        with tempfile.TemporaryDirectory() as raw:
            r.attach_log(Path(raw))
            state = self.start(r)
            handler = r.handler
            with patch.object(handler, 'emit', side_effect=OSError('disk full')):
                self.tick(r, 30)
                self.tick(r, 60)
            self.assertEqual(self.err.getvalue().count('Error details could not be saved. Analysis will continue.'), 1)
            self.assertTrue(handler.stream.closed)
            self.assertIs(r._progress, state)
            self.assertFalse(state.stop.is_set())
            self.assertTrue(self.err.getvalue().endswith('01:00\x1b[K'))
            r.close()

    def test_log_frequency_and_idle_budget_are_independent_of_frames(self):
        r = self.reporter()
        with tempfile.TemporaryDirectory() as raw:
            r.attach_log(Path(raw))
            self.start(r)
            budget = Budget(120, 1, clock=lambda: self.now)
            for i in range(241):
                self.tick(r, i * .125)
            records = [json.loads(line) for line in (Path(raw) / 'run.log').read_text().splitlines()]
            self.assertEqual([x['event'] for x in records], ['stage_started', 'process_waiting'])
            self.assertEqual(records[1]['elapsed_seconds'], 30)
            self.assertNotIn('last_output_seconds', records[1])
            self.assertNotIn('\n', self.err.getvalue())
            self.assertNotIn('\x1b', (Path(raw) / 'run.log').read_text())
            r.cli_started()
            self.tick(r, 45)
            r.cli_output()
            self.tick(r, 60)
            r.cli_started()
            self.tick(r, 90)
            records = [json.loads(line) for line in (Path(raw) / 'run.log').read_text().splitlines()]
            self.assertEqual(records[-2]['last_output_seconds'], 15)
            self.assertIsNone(records[-1]['last_output_seconds'])
            self.assertNotIn('CLI', self.err.getvalue())
            with self.assertRaises(ContractError) as caught:
                budget.check()
            self.assertEqual(caught.exception.failure_kind, 'IDLE_TIMEOUT')
            r.close()

    def test_json_uses_only_stderr_tty_and_color_is_independent(self):
        for out_tty in (False, True):
            for err_tty in (False, True):
                for no_color in ('', '1'):
                    r = self.reporter(tty=err_tty, out_tty=out_tty, mode='json', env={'NO_COLOR': no_color})
                    self.start(r)
                    self.tick(r, 1)
                    result = {'status': 'COMPLETE', 'exit_code': 0, 'manifest': None}
                    r.finish(result, {}, check_only=False, config_path=Path('config.json'))
                    self.assertEqual(json.loads(r.stdout.getvalue()), result)
                    self.assertNotIn('\x1b', r.stdout.getvalue())
                    self.assertEqual('\r' in self.err.getvalue(), err_tty)
                    self.assertEqual(bool(SGR.search(self.err.getvalue())), err_tty and not no_color)
                    r.close()

    def test_encoding_width_escaping_redaction_and_resize(self):
        r = self.reporter()
        r.columns = 50
        r.secrets = ('private-token',)
        self.start(r, branch='private-token\n\x1b[31m\u202e深e\u0301' * 20)
        for columns in (50, 42, 31, 2, 80):
            r.columns = columns
            self.tick(r, 1)
            line = r._progress_line(r._progress)
            if line is not None:
                self.assertLessEqual(cell_width(SGR.sub('', line)), columns - 1)
                self.assertIn('Analyzing project…', line)
                self.assertNotIn('private-token', line)
                self.assertNotIn('\u202e', line)
                self.assertNotIn('\x1b[31m', line)
        self.assertEqual(cell_width('深e\u0301'), 3)
        r.close()
        with patch.object(Terminal, 'encoding', 'ascii'):
            r = self.reporter()
        self.start(r, branch='深e\u0301')
        for elapsed in (.5, .625, .75):
            self.tick(r, elapsed)
        self.err.getvalue().encode('ascii')
        self.assertEqual(r.frames, '|/-\\')
        self.assertIn('  \\ 00:00', self.err.getvalue())

    def test_truncated_source_keeps_stage_column_when_hours_appear(self):
        r = self.reporter()
        r.columns = 45
        self.start(r, branch='long source ' * 20)
        self.tick(r, 3599)
        before = r._progress_line(r._progress)
        self.tick(r, 3600)
        after = r._progress_line(r._progress)
        self.assertEqual(before.index('/ Analyzing project…'), after.index('/ Analyzing project…'))
        self.assertIn('59:59', before)
        self.assertIn('01:00:00', after)
        self.assertLessEqual(cell_width(after), 44)

    def test_tty_and_width_failures_fall_back_safely(self):
        for error in (OSError(), ValueError(), RuntimeError()):
            with patch.object(Terminal, 'isatty', side_effect=error):
                r = self.reporter()
            self.start(r)
            self.tick(r, 30)
            self.assertNotIn('\x1b', self.err.getvalue())
            self.assertIn('Elapsed: 00:30', self.err.getvalue())
            r.close()
        r = self.reporter()
        # Exercise the real width lookup on a fileno-less terminal.
        r._terminal_columns = lambda: Reporter._terminal_columns(r)
        self.start(r)
        self.tick(r, 30)
        self.assertNotIn('\x1b', self.err.getvalue())
        self.assertIn('Elapsed: 00:30', self.err.getvalue())

    def test_terminal_failure_once_and_cleanup_are_idempotent(self):
        for failure in (BrokenPipeError(), ValueError('closed'), UnicodeError()):
            r = self.reporter()
            state = self.start(r)
            with patch.object(self.err, 'write', side_effect=failure) as write:
                self.tick(r, 1)
                self.tick(r, 2)
                r.error(OSError('original failure'))
                r.stop_progress()
                r.close()
                r.close()
                self.assertEqual(write.call_count, 1)
            self.assertTrue(state.stop.is_set())
            self.assertIsNone(r._progress)

    def test_strict_ascii_stream_keeps_final_status_and_closed_stream_stops_worker(self):
        r = self.reporter()
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding='ascii', errors='strict')
        self.addCleanup(stream.close)
        with patch.object(stream, 'isatty', return_value=True), \
                patch.dict(os.environ, {'TERM': 'xterm', 'NO_COLOR': '1'}):
            r = ManualReporter(stderr=stream, stdout=self.out, clock=lambda: self.now)
        self.addCleanup(r.close)
        self.start(r, branch='深い')
        self.tick(r, .75)
        r.emit('stage_completed', **CONTEXT, status='COMPLETE', elapsed_seconds=.75)
        self.assertIn(b'  \\ 00:00', buffer.getvalue())
        self.assertIn(b'Architecture report created. | Elapsed: 00:00\n', buffer.getvalue())
        self.assertNotIn(id(stream), r.failed_streams)
        state = self.start(r)
        stream.close()
        self.tick(r, 1.5)
        self.assertTrue(state.stop.is_set())
        self.assertIn(id(stream), r.failed_streams)
        r.close()

    def test_all_terminal_events_stop_and_error_can_continue(self):
        for event, kwargs in (('stage_completed', CONTEXT | {'status': 'BLOCKED', 'elapsed_seconds': 1}),
                              ('error', {'phase': 'run', 'code': 'INTERRUPTED', 'message': 'Interrupted'}),
                              ('stop_requested', {}), ('restoration_started', {}), ('run_completed', {})):
            r = self.reporter()
            state = self.start(r)
            r.emit(event, **kwargs)
            before = self.err.getvalue()
            r._tick_progress(state)
            self.assertEqual(before, self.err.getvalue())
            self.assertIsNone(r._progress)
            self.assertTrue(state.stop.is_set())
            r.close()
        r = self.reporter()
        self.start(r)
        r.error(OSError('failed'), phase='stage', **CONTEXT)
        self.start(r, branch='next')
        self.assertIn('[RUN] next / Analyzing project…  ⠋ 00:00', self.err.getvalue())

    def test_real_worker_stops_during_tick_without_join_deadlock(self):
        r = self.reporter()
        r._start_worker = lambda state: Reporter._start_worker(r, state)
        entered, release = threading.Event(), threading.Event()
        original = r._tick_progress
        def tick(state):
            entered.set()
            release.wait(2)
            original(state)
        r._tick_progress = tick
        state = self.start(r)
        self.assertTrue(entered.wait(2))
        closer = threading.Thread(target=r.close)
        closer.start()
        self.assertTrue(state.stop.wait(2))
        release.set()
        closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertFalse(state.thread.is_alive())
        self.assertNotIn('\x1b[?25', self.err.getvalue())
        before = self.err.getvalue()
        original(state)
        self.assertEqual(before, self.err.getvalue())


class XXXProgressIntegrationTests(unittest.TestCase):
    setUp = xxx_fixtures.XXXTests.setUp
    run_case = xxx_fixtures.XXXTests.run_case
    recorded = xxx_fixtures.XXXTests.recorded
    prompts = xxx_fixtures.XXXTests.prompts
    git = xxx_fixtures.XXXTests.git
    init_git = xxx_fixtures.XXXTests.init_git

    def test_silent_http_study_and_review_update_before_response_in_real_pty(self):
        self.init_git(['main'])
        self.git('branch', '-m', 'master')
        self.value['git_mode'].update(branches=['master'], baseline_branch='master')
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.value))
        gate = self.root / 'gate'
        gate.mkdir()
        env = os.environ | self.env | {'TERM': 'xterm', 'NO_COLOR': '1',
            'AUDIT_FAKE_CASE': 'progress-barrier', 'AUDIT_FAKE_PROGRESS_GATE': str(gate)}
        master, slave = pty.openpty()
        termios.tcsetwinsize(slave, (24, 100))
        child = subprocess.Popen([sys.executable, '-B', str(ROOT / 'explain.py'),
            '--config', str(path), '--output', 'json'], env=env, stdout=subprocess.PIPE, stderr=slave)
        raw = bytearray()
        try:
            for stage, label, completed in (
                    ('catalog', 'Cataloging subsystems…', 'Subsystem catalog created.'),
                    ('study', 'Analyzing project…', 'Architecture report created.'),
                    ('review', 'Reviewing report…', 'Review complete.')):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        raw.extend(os.read(master, 65536))
                    text = raw.decode('utf-8')
                    frames = re.findall(r'\[RUN\] master / ' + re.escape(label) + r'  (.) (\d\d:\d\d)', text)
                    if ((gate / (stage + '.ready')).exists() and
                            len({frame for frame, _ in frames}) >= 3 and
                            any(timer != '00:00' for _, timer in frames)):
                        break
                    self.assertIsNone(child.poll(), text)
                else:
                    self.fail('No moving spinner/timer during pending HTTP: ' + raw.decode())
                self.assertNotIn('[OK] master / ' + completed, text)
                self.assertFalse((gate / (stage + '.release')).exists())
                # Frames never scroll; there is no newline between their writes.
                span = text.split('[RUN] master / ' + label, 1)[1]
                self.assertNotIn('\n', span)
                (gate / (stage + '.release')).touch()
            stdout, _ = child.communicate(timeout=12)
            while select.select([master], [], [], .1)[0]:
                raw.extend(os.read(master, 65536))
            text = raw.decode()
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
            os.close(master)
            os.close(slave)
        self.assertEqual(child.returncode, 0, text)
        result = json.loads(stdout)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertNotIn(b'\x1b', stdout)
        self.assertNotRegex(text, SGR)
        self.assertIn('[OK] master / Architecture report created. | Elapsed:', text)
        self.assertIn('[OK] master / Review complete. No significant issues reported. | Elapsed:', text)
        self.assertNotIn('checkout hierarchy', text)
        self.assertNotIn('      Elapsed:', text)
        self.assertNotIn('\x1b[?25', text)
        tail = text.split('[OK] master / Review complete.', 1)[1]
        self.assertFalse(any(c in tail for c in SPINNER))
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'master')
        run = Path(result['manifest']).parent
        for artifact in [run / 'run.log', run / 'manifest.json', *run.rglob('*.md'), *run.rglob('stdout.log'),
                         *run.rglob('stderr.log')]:
            content = artifact.read_text()
            self.assertNotIn('\x1b', content, str(artifact))
            self.assertFalse(any(c in content for c in SPINNER), str(artifact))
        self.assertEqual(len(self.prompts()), 3)

    def test_http_plain_fallback_is_live_without_duplicate_waits(self):
        for term, tty in (('xterm', False), ('dumb', True)):
            with self.subTest(term=term, tty=tty):
                gate = self.root / term
                gate.mkdir()
                self.env['AUDIT_FAKE_PROGRESS_GATE'] = str(gate)
                records = []
                class ObservedReporter(Reporter):
                    def emit(observer, event, *args, **context):
                        super().emit(event, *args, **context)
                        if event == 'process_waiting':
                            records.append(context)
                            stage = context['stage']
                            if (gate / (stage + '.ready')).exists():
                                (gate / (stage + '.release')).touch()
                err = Terminal() if tty else io.StringIO()
                with patch.dict(os.environ, {'TERM': term, 'NO_COLOR': '1'}):
                    r = ObservedReporter(stderr=err, stdout=io.StringIO(), progress_interval=.05)
                self.addCleanup(r.close)
                manifest, code = self.run_case('progress-barrier', reporter=r)
                self.assertEqual(code, 0, manifest)
                self.assertIsNone(r._progress)
                self.assertNotIn('\x1b', err.getvalue())
                self.assertNotIn('CLI output', err.getvalue())
                self.assertTrue(records)
                for stage in ('catalog', 'study', 'review'):
                    times = [x['elapsed_seconds'] for x in records if x['stage'] == stage]
                    self.assertTrue(times)
                    self.assertTrue(all(b - a >= .049 for a, b in zip(times, times[1:])))
                r.close()

    def test_http_timeout_with_live_spinner_preserves_deadline_and_cleanup(self):
        self.value['execution'] = {'stage_timeout_seconds': 5, 'idle_timeout_seconds': .6}
        class SizedReporter(Reporter):
            def _terminal_columns(self):
                return 100
        with patch.dict(os.environ, {'TERM': 'xterm', 'NO_COLOR': '1'}):
            r = SizedReporter(stderr=Terminal(), stdout=io.StringIO())
        self.addCleanup(r.close)
        manifest, code = self.run_case('slow', reporter=r)
        self.assertEqual(code, 1, manifest)
        self.assertEqual(manifest['diagnostics'][0]['failure_kind'], 'IDLE_TIMEOUT')
        self.assertIsNone(r._progress)
        self.assertFalse(any(t.name == 'audit-progress' for t in threading.enumerate()))
        self.assertIn('[FAIL] source / Project analysis failed.', r.stderr.getvalue())
        for pid in {c['server_pid'] for c in self.recorded()}:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_real_format_repairs_keep_one_stage_clock(self):
        self.value['execution'] = {'structured_output_repair_attempts': 1}
        now, attempts = [100.0], []
        with patch.dict(os.environ, {'TERM': 'xterm', 'NO_COLOR': '1'}):
            r = ManualReporter(stderr=Terminal(), stdout=io.StringIO(), clock=lambda: now[0])
        self.addCleanup(r.close)
        original = Runner._invoke_once
        def invoke(runner, stage, *args, **kwargs):
            attempts.append((stage, r._progress.started, kwargs['repair_index']))
            now[0] += 1.25
            r._tick_progress(r._progress)
            return original(runner, stage, *args, **kwargs)
        with patch.object(Runner, '_invoke_once', invoke):
            manifest, code = self.run_case('repair-ok', reporter=r)
        self.assertEqual(code, 0, manifest)
        self.assertEqual(attempts, [('catalog', 100, 0), ('study', 101.25, 0), ('study', 101.25, 1),
                                    ('review', 103.75, 0), ('review', 103.75, 1)])
        for label in ('Analyzing project…', 'Reviewing report…'):
            frames = re.findall(r'/ ' + re.escape(label) + r'  . (\d\d:\d\d)', r.stderr.getvalue())
            self.assertEqual(frames, ['00:00', '00:01', '00:02'])
        self.assertEqual(len(self.prompts()), 5)
        self.assertIsNone(r._progress)

    def test_real_stage_error_then_next_branch_and_comparison_stop_cleanly(self):
        self.init_git(['main', 'other'])
        with patch.dict(os.environ, {'TERM': 'xterm', 'NO_COLOR': '1'}):
            r = ManualReporter(stderr=Terminal(), stdout=io.StringIO())
        self.addCleanup(r.close)
        manifest, code = self.run_case('fail-main-study', reporter=r)
        self.assertEqual(code, 1, manifest)
        self.assertIsNone(r._progress)
        self.assertIn('[FAIL] main / Project analysis failed.', r.stderr.getvalue())
        self.assertIn('[RUN] other / Analyzing project…', r.stderr.getvalue())
        self.assertIn('[RUN] Comparing branch reports…', r.stderr.getvalue())
        self.assertTrue(manifest['restoration']['restored'])
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')

    def test_ctrl_c_clears_live_http_spinner_and_no_progress_remains_plain(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.value))
        for quiet in (False, True):
            gate = self.root / str(quiet)
            gate.mkdir()
            env = os.environ | self.env | {'TERM': 'xterm', 'NO_COLOR': '1',
                'AUDIT_FAKE_CASE': 'progress-barrier', 'AUDIT_FAKE_PROGRESS_GATE': str(gate)}
            master, slave = pty.openpty()
            termios.tcsetwinsize(slave, (24, 100))
            child = subprocess.Popen([sys.executable, '-B', str(ROOT / 'explain.py'),
                '--config', str(path), '--output', 'json', *(['--no-progress'] if quiet else [])],
                env=env, stdout=subprocess.PIPE, stderr=slave)
            raw = bytearray()
            try:
                deadline = time.monotonic() + 10
                while not (gate / 'catalog.ready').exists() and time.monotonic() < deadline:
                    if select.select([master], [], [], .05)[0]:
                        raw.extend(os.read(master, 65536))
                    self.assertIsNone(child.poll(), raw.decode())
                self.assertTrue((gate / 'catalog.ready').exists())
                child.send_signal(signal.SIGINT)
                stdout, _ = child.communicate(timeout=10)
                while select.select([master], [], [], .1)[0]:
                    raw.extend(os.read(master, 65536))
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)
                os.close(master)
                os.close(slave)
            text = raw.decode()
            self.assertEqual(child.returncode, 130, text)
            self.assertEqual(json.loads(stdout)['exit_code'], 130)
            self.assertIn('Stopping analysis…', text)
            self.assertIn('Analysis interrupted.', text)
            self.assertNotIn('INTERRUPTED', text)
            if quiet:
                self.assertNotIn('\x1b', text)
                self.assertFalse(any(c in text for c in SPINNER))
            else:
                self.assertIn('⠋', text)
                self.assertIn('\r\x1b[2K[WARN] Stopping analysis…', text)
            self.assertFalse(any(c in text.split('Stopping analysis…', 1)[1] for c in SPINNER))
            for pid in {c['server_pid'] for c in self.recorded()}:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)


class CLIProgressIntegrationTests(unittest.TestCase):
    setUp = cli_fixtures.ReportingCLIIntegrationTests.setUp
    tearDown = cli_fixtures.ReportingCLIIntegrationTests.tearDown
    git = cli_fixtures.ReportingCLIIntegrationTests.git
    prepare = cli_fixtures.ReportingCLIIntegrationTests.prepare

    def test_live_cli_spinner_preserves_private_raw_output_and_no_waiting_scroll(self):
        args = self.prepare('folder')
        self.env.update(TERM='xterm', NO_COLOR='1', AUDIT_TEST_ACTION=json.dumps(
            {'stage': 'study', 'kind': 'active', 'seconds': .8}))
        master, slave = pty.openpty()
        termios.tcsetwinsize(slave, (24, 100))
        command = [sys.executable, '-B', '-c',
            'import runpy,sys; sys.path.insert(0,sys.argv[1]); from src.runtime import reporting; '
            'original=reporting.Reporter; reporting.Reporter=lambda **kw: original(progress_interval=0.1,**kw); '
            'sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")',
            str(ROOT), str(ROOT / 'explain.py'), *args, '--output', 'json']
        child = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=slave)
        raw = bytearray()
        try:
            deadline = time.monotonic() + 15
            while child.poll() is None:
                self.assertLess(time.monotonic(), deadline, raw.decode(errors='replace'))
                if select.select([master], [], [], .1)[0]:
                    raw.extend(os.read(master, 65536))
            stdout, _ = child.communicate(timeout=5)
            while select.select([master], [], [], .1)[0]:
                raw.extend(os.read(master, 65536))
            console = raw.decode()
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
            os.close(master)
            os.close(slave)
        self.assertEqual(child.returncode, 0, console)
        run = Path(json.loads(stdout)['manifest']).parent
        self.assertIn('[RUN] source folder / Analyzing project…  ⠋ 00:00', console)
        self.assertIn('⠙', console)
        self.assertNotIn('      Elapsed:', console)
        self.assertNotIn('private CLI activity', console)
        for name in ('ARCHITECTURE.md', 'ARCHITECTURE_REVIEW.md'):
            self.assertIn('      Report: ' + str(run / 'revisions/001' / name) + '\r\n', console)
        self.assertEqual((run / 'revisions/001/study.logs/attempt-001/stderr.log').read_bytes(), b'private CLI activity\n')
        records = [json.loads(line) for line in (run / 'run.log').read_text().splitlines()]
        times = [x['elapsed_seconds'] for x in records if x['event'] == 'process_waiting' and x['stage'] == 'study']
        self.assertGreater(len(times), 1)
        self.assertTrue(all(b - a >= .099 for a, b in zip(times, times[1:])))
        self.assertTrue(any('last_output_seconds' in x for x in records if x['event'] == 'process_waiting'))


if __name__ == '__main__':
    unittest.main()
