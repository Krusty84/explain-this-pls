# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Owned subprocess groups with nonblocking pipes and execution guards."""
from __future__ import annotations
import contextlib
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from src.messages import message as ui_message
from src.runtime.reporting import NullReporter
from src.runtime.execution import Budget
from src.runtime.errors import AuditError

def cli_env(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(PWD=str(cwd.resolve()), NO_COLOR='1', GIT_TERMINAL_PROMPT='0')
    return env

def process(command: list[str], cwd: Path, env: dict[str, str], input_data: bytes = b'',
            *, reporter=None, context: dict | None = None, log_dir: Path | None = None,
            clock=time.monotonic, progress_interval: float = 30.0, budget=None, observer=None) -> dict:
    """Nonblocking pipe I/O; do not pass artifact FDs into the CLI child."""
    reporter = reporter or NullReporter()
    context = context or {}
    # ExitStack also closes the files if opening the second file or Popen fails.
    with contextlib.ExitStack() as logs:
        streams = {}
        if log_dir is not None:
            for name in ('stdout', 'stderr'):
                fd = os.open(log_dir / (name + '.log'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                stream = logs.enter_context(os.fdopen(fd, 'wb', buffering=0))
                os.fchmod(stream.fileno(), 0o600)
                streams[name] = stream
        return _process(command, cwd, env, input_data, reporter,
                        context, clock, progress_interval, streams, budget or Budget(3600, clock=clock), observer)

def _process(command, cwd, env, input_data, reporter, context,
             clock, progress_interval, logs, budget, observer=None):
    budget.check()
    start = clock()
    last_output = None
    next_progress = start + progress_interval
    # A stage-owned Reporter also covers silent subprocess waits.
    # Standalone process callers retain their own waiting events.
    managed_progress = reporter.cli_started()
    out, err = bytearray(), bytearray()
    p = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True, close_fds=True)
    selector = selectors.DefaultSelector()
    offset = 0
    try:
        for stream, name in ((p.stdout, 'stdout'), (p.stderr, 'stderr')):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if input_data:
            os.set_blocking(p.stdin.fileno(), False)
            selector.register(p.stdin, selectors.EVENT_WRITE, 'stdin')
        else:
            p.stdin.close()
        while selector.get_map() or p.poll() is None:
            budget.check()
            current = clock()
            if reporter.progress and not managed_progress and current >= next_progress:
                reporter.emit('process_waiting', **context, elapsed_seconds=current - start,
                              last_output_seconds=None if last_output is None else current - last_output)
                next_progress = current + progress_interval
            # select() also bounds the wait after both output pipes have closed.
            for key, _ in selector.select(0.2):
                stream, name = key.fileobj, key.data
                if name == 'stdin':
                    try:
                        offset += os.write(stream.fileno(), input_data[offset:offset + 65536])
                    except BrokenPipeError:
                        offset = len(input_data)
                    except BlockingIOError:
                        continue
                    if offset == len(input_data):
                        selector.unregister(stream)
                        stream.close()
                else:
                    try:
                        block = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(stream)
                        stream.close()
                        if name == 'stdout' and observer is not None:
                            observer.finish()
                            check_read_loop(observer)
                    else:
                        last_output = clock()
                        if managed_progress:
                            reporter.cli_output()
                        budget.activity()
                        target = out if name == 'stdout' else err
                        target.extend(block)
                        if name in logs:
                            remaining = memoryview(block)
                            while remaining:
                                remaining = remaining[logs[name].write(remaining):]
                            logs[name].flush()
                        if name == 'stdout' and observer is not None:
                            observer.feed(block)
                            check_read_loop(observer)
    except KeyboardInterrupt:
        reporter.stop_requested()
        raise
    finally:
        failure = sys.exc_info()[1]
        failed = failure is not None
        cleanup_error = None
        selector.close()
        # Kill ordinary descendants remaining in this invocation's process group,
        # including on errors/interrupts; detached hostile daemons are not supported.
        if p.poll() is None:
            reporter.emit('process_stopping', **context)
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            cleanup_error = exc
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            cleanup_error = exc
        for stream in (p.stdin, p.stdout, p.stderr):
            with contextlib.suppress(Exception):
                stream.close()
        if cleanup_error is not None and not failed:
            raise AuditError(ui_message('diagnostic.process.could_not_finish_cleanup_of_the_owned_cli'),
                             code='CLI_FAILED', failure_kind='BACKEND_ERROR', failure_layer='cleanup') from cleanup_error
        if cleanup_error is not None and failure is not None:
            failure.cleanup_failed = True
    return {'stdout': bytes(out), 'stderr': bytes(err), 'returncode': p.returncode,
            'duration_seconds': round(clock() - start, 3)}

def check_read_loop(observer):
    if observer.triggered:
        raise AuditError(ui_message('diagnostic.process.repeated_tool_results_produced_no_new_observable_material'),
                         code='NO_PROGRESS', failure_kind='NO_PROGRESS', failure_layer='execution',
                         details=observer.diagnostic)
