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
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from contracts import ContractError
from explain import AuditError, Runner, UnsafeRepository, cli_env, load_config, main, process
from reporting import NullReporter, Reporter, duration, output_mode, safe_text
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

    def test_early_buffer_private_log_full_context_and_close(self):
        root_handlers = list(logging.getLogger().handlers)
        r = self.reporter
        r.run_id = 'test-run'
        r.emit('preflight_completed', check='configuration')
        before = self.err.getvalue()
        r.attach_log(self.base)
        self.assertEqual(self.err.getvalue(), before)
        sha = 'a' * 40
        r.emit('stage_started', branch='深い branch', commit=sha, stage='document', backend='codex')
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
        self.assertEqual(console.count('Snapshot:'), 1)
        self.assertEqual(console.count('The submodule is not initialized locally.'), 1)
        self.assertNotIn('Traceback', console)
        self.assertIn('No checkout switches were performed.', console)
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
                self.assertEqual(out.getvalue().count('FAILED'), 1)
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
                             context={'branch': 'master', 'stage': 'document', 'backend': 'opencode'})
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
            self.assertIn('Last CLI output: 00:10 ago' if active else 'No CLI output received yet', text)
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
                self.assertNotIn('Stopping the active CLI process', text)

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
                'import runpy,sys; sys.path.insert(0,sys.argv[1]); import reporting; '
                'original=reporting.Reporter; reporting.Reporter=lambda **kw: original(progress_interval=0.05,**kw); '
                'sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")',
                str(ROOT), str(ROOT / 'explain.py')]
        return subprocess.run(command + args, cwd=self.base, env=self.env, stdout=stdout,
                              stderr=subprocess.PIPE, text=True, timeout=30)

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
                    else:
                        self.assertIn('PREFLIGHT PASSED', output)
                        self.assertIn('No model calls were made.', output)
                    self.assertIn('Source inventory and fingerprint', result.stderr)
                    self.assertNotIn('authentication', result.stderr)

    def test_early_errors_nullable_missing_git_and_unavailable_results(self):
        cases = []
        missing = self.base / 'missing config.json'
        cases.append((['--config', str(missing)], None))
        args = self.prepare('git', check=True)
        self.env['PATH'] = ''
        result = self.run_cli(args)
        self.assertEqual(result.returncode, 1)
        self.assertIn('git was not found', result.stderr)
        self.assertIsNone(json.loads(result.stdout)['manifest'])
        self.assertNotIn('Details:', result.stderr)
        args = self.prepare('folder', check=True)
        self.reports.write_text('a file instead of a directory')
        cases.append((args, 'assigned'))
        for args, assigned in cases:
            result = self.run_cli(args)
            data = json.loads(result.stdout)
            self.assertEqual(data['exit_code'], 1)
            self.assertIsNone(data['manifest'])
            self.assertEqual(data['run_id'] is None, assigned is None)
            self.assertNotIn('Details:', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        text = self.run_cli(['--config', str(missing), '--output', 'text'])
        self.assertIn('FAILED', text.stdout)
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
        self.assertNotIn('Details:', result.stderr)

    def test_internal_error_is_distinct_from_configuration_error(self):
        args = self.prepare('folder', check=True)
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['explain.py', *args]), patch.dict(os.environ, self.env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(Runner, 'check_cli', side_effect=RuntimeError('private internal value')):
            self.assertEqual(main(), 1)
        data = json.loads(out.getvalue())
        log = (Path(data['manifest']).parent / 'run.log').read_text()
        self.assertIn('INTERNAL_ERROR', err.getvalue())
        self.assertIn('Traceback', log)
        self.assertNotIn('Traceback', err.getvalue())
        self.assertNotIn('private internal value', err.getvalue() + log)

    def test_activity_no_progress_and_streaming_log_privacy(self):
        args = self.prepare('folder')
        for kind in ('wait', 'active', 'closed-pipes'):
            for quiet in (False, True):
                with self.subTest(kind=kind, quiet=quiet):
                    self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'document', 'kind': kind, 'seconds': 1.4 if kind == 'closed-pipes' else 0.4})
                    result = self.run_cli(args + ['--verbose'] + (['--no-progress'] if quiet else []), accelerated=True)
                    output = json.loads(result.stdout)
                    self.assertEqual(output['exit_code'], 1 if kind == 'closed-pipes' else 0, result.stderr)
                    self.assertEqual('      Elapsed:' in result.stderr, not quiet)
                    if not quiet:
                        self.assertIn('Last CLI output:' if kind == 'active' else 'No CLI output received yet', result.stderr)
                    self.assertNotIn('private CLI activity', result.stderr)
                    self.assertNotIn('Timeout:', result.stderr)
                    log = Path(output['manifest']).parent / 'run.log'
                    self.assertNotIn('private CLI activity', log.read_text())
                    self.assertNotIn('Authoritative orchestration context', log.read_text())
                    self.assertNotIn('timeout_seconds', log.read_text())
                    if kind == 'closed-pipes':
                        self.assertIn('CLI_FAILED', result.stderr)
                        self.assertNotIn('[OK] folder / document', result.stderr)

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
        self.assertEqual(len(model_calls), 2)
        for call, stage in zip(model_calls, ('document', 'review')):
            self.assertEqual(call['context']['project_description'], description.strip())
            logs = run / (stage + '.logs')
            meta = json.loads((logs / 'invocation.json').read_text())
            payload = (logs / 'input.prompt.txt').read_bytes()
            self.assertEqual(meta['status'], 'SUCCEEDED')
            self.assertEqual(meta['input_bytes'], len(payload))
            self.assertGreater(len(payload), 800000)

    def test_partial_and_invalid_response_are_not_reported_as_success(self):
        args = self.prepare('folder')
        self.env['AUDIT_TEST_PARTIAL'] = '1'
        result = self.run_cli(args + ['--output', 'text'])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(result.stdout.startswith('PARTIAL\n'))
        self.assertIn('[WARN] folder / document / codex — PARTIAL', result.stderr)
        del self.env['AUDIT_TEST_PARTIAL']
        self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'document', 'kind': 'invalid', 'value': 'PRIVATE_MODEL_VALUE'})
        result = self.run_cli(args + ['--verbose'])
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        run = Path(data['manifest']).parent
        self.assertNotIn('PRIVATE_MODEL_VALUE', result.stderr + (run / 'run.log').read_text())
        self.assertIn('PRIVATE_MODEL_VALUE', (run / 'document.logs/stdout.log').read_text())
        self.assertIn('INVALID_RESPONSE', result.stderr)
        self.assertNotIn('[OK] folder / document', result.stderr)
        errors = [json.loads(line) for line in (run / 'run.log').read_text().splitlines()
                  if json.loads(line)['event'] == 'error']
        self.assertEqual((errors[0]['branch'], errors[0]['stage'], errors[0]['backend']),
                         ('folder', 'document', 'codex'))

    def test_interrupt_stops_process_before_verified_restoration(self):
        args = self.prepare('git')
        self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'document', 'kind': 'interrupt'})
        result = self.run_cli(args)
        data = json.loads(result.stdout)
        self.assertEqual(result.returncode, 130, result.stderr)
        manifest = json.loads(Path(data['manifest']).read_text())
        self.assertTrue(manifest['restoration']['restored'])
        expected = ['[WARN] Stop requested.', '[RUN] Stopping the active CLI process.',
                    '[RUN] Restoring the original checkout hierarchy.', '[OK] Original checkout hierarchy restored.']
        positions = [result.stderr.index(line) for line in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(result.stderr.count('Stop requested.'), 1)
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
                patch('reporting.RunLogHandler.emit', side_effect=OSError('disk full')):
            self.assertEqual(main(), 0)
        manifest = json.loads(Path(json.loads(out.getvalue())['manifest']).read_text())
        self.assertTrue(manifest['restoration']['restored'])
        self.assertEqual(broken.write.call_count, 1)


class RecursiveDiagnosticTests(recursive.RecursiveFixture, unittest.TestCase):
    def test_uninitialized_leaf_reports_one_reason_and_real_node(self):
        (self.paths[recursive.LEAF] / '.git').unlink()
        result, manifest = self.execute(check=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.count('Snapshot:'), 1)
        self.assertEqual(result.stderr.count('The submodule is not initialized locally.'), 1)
        detail = manifest['diagnostics'][0]
        self.assertEqual(detail['code'], 'SUBMODULE_NOT_INITIALIZED')
        self.assertEqual(detail['node_path'], recursive.LEAF)
        self.assertEqual(detail['required_commit'], self.expected['master'][recursive.LEAF]['commit'])
        self.assertEqual(manifest['switch_journal'], [])
        self.assertIn('No checkout switches were performed.', result.stderr)

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
        self.assertIn('[FAIL] Restoration failed', err.getvalue())
        self.assertNotIn('[OK] Original checkout hierarchy restored', err.getvalue())
        self.assertEqual(manifest['restoration']['node'], recursive.LEAF)
        self.assertFalse(manifest['restoration']['restored'])
        self.assertGreaterEqual(len(manifest['diagnostics']), 2)


if __name__ == '__main__':
    unittest.main()
