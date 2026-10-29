# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""An isolated audit process; only sanitized presentation data crosses its pipe."""
import asyncio
import contextlib
import multiprocessing
from pathlib import Path
import signal
import sys
import threading

from src.messages import message as ui_message
from src.backends.codex_limits import read_limits
from src.runtime.audit import run_audit
from src.runtime.metrics import RunMetrics
from src.runtime.reporting import Reporter, existing_file
from src.reports.review import review_summary


class PipeReporter(Reporter):
    def __init__(self, connection, *, verbose=False):
        self.connection = connection
        self.disconnected = False
        self.stage_executables, self.limit_snapshots = {}, set()
        super().__init__(mode='json', verbose=verbose, progress=False, event_sink=self.send_event)

    def send(self, message):
        if self.disconnected:
            return
        try:
            self.connection.send(self.clean(message))
        except (BrokenPipeError, EOFError, OSError):
            self.disconnected = True
            # Let the normal audit cleanup run if its UI disappears.
            raise KeyboardInterrupt(ui_message('tui.the_interactive_interface_disconnected'))

    def send_event(self, event):
        self.send({'type': 'event', 'event': event.event, 'context': event.context})
        context = event.context
        if not context.get('stage'):
            return
        key = RunMetrics.key(context)
        if event.event == 'stage_started' and context.get('backend') == 'codex':
            self.stage_executables[key] = context.get('executable')
        executable = self.stage_executables.get(key)
        if (event.event == 'stage_completed' or event.event == 'error' and context.get('phase') == 'stage'):
            if (not executable or key in self.limit_snapshots or self.stopping
                    or context.get('code') == 'INTERRUPTED' or context.get('generated_by') == 'orchestrator'):
                return
            self.limit_snapshots.add(key)
            snapshot = asyncio.run(read_limits(executable))
            self.send({'type': 'event', 'event': 'stage_rate_limits', 'context': {
                'branch': key[0], 'stage': key[1], 'revision_id': key[2], 'rate_limits': snapshot}})

    def _write(self, stream, text):
        # The parent owns the terminal; the existing private log remains active.
        pass

    def finish(self, result, manifest, **options):
        if self.finished:
            return
        super().finish(result, manifest, **options)
        paths = []
        for label, value in ((ui_message('tui.final_report'), result.get('final_report')),
                             (ui_message('tui.manifest'), result.get('manifest')), (ui_message('tui.technical_log'), self.log_path)):
            if value and existing_file(Path(value)):
                paths.append((label, str(value)))
        reviews = [(entry.get('branch', ''), review_summary(entry,
                    review_enabled=manifest.get('review_enabled', True)))
                   for entry in manifest.get('branches') or [manifest]] if not options['check_only'] else []
        self.send({'type': 'result', 'result': result, 'paths': paths, 'reviews': reviews,
                   'diagnostics': manifest.get('diagnostics', []), 'check_only': options['check_only']})


def audit_worker(args, connection):
    reporter = PipeReporter(connection, verbose=args.verbose)
    def interrupted(signum, frame):
        raise KeyboardInterrupt(ui_message('tui.analysis_interrupted'))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    code = 1
    try:
        # Cancellation before the first run event is safe once this is sent.
        reporter.send({'type': 'ready'})
        code = run_audit(args, reporter)
    except BaseException as exc:
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
        reporter.send({'type': 'result', 'result': {'status': 'FAILED', 'exit_code': code},
                       'paths': [], 'check_only': args.check,
                       'diagnostics': [{'message': ui_message('tui.analysis_interrupted_before_starting') if code == 130
                                        else ui_message('tui.the_audit_worker_stopped_unexpectedly')}]})
    finally:
        reporter.close()
        connection.close()
    raise SystemExit(code)


class AuditProcess:
    def __init__(self, args):
        context = multiprocessing.get_context('spawn')
        self.reader, self.writer = context.Pipe(duplex=False)
        self.process = context.Process(target=audit_worker, args=(args, self.writer))
        self.lock = threading.Lock()
        self.ready = self.cancelled = self.signalled = False

    def start(self):
        try:
            # Textual's captured stderr has fileno() == -1. The spawn resource
            # tracker needs the real stream while it creates its subprocess.
            with contextlib.redirect_stderr(sys.__stderr__):
                self.process.start()
        except BaseException:
            self.reader.close()
            self.process.close()
            raise
        finally:
            self.writer.close()

    def cancel(self):
        with self.lock:
            self.cancelled = True
            self._signal()

    def _signal(self):
        if self.ready and self.cancelled and not self.signalled:
            self.signalled = True
            if self.process.is_alive():
                self.process.terminate()

    def messages(self):
        try:
            while True:
                message = self.reader.recv()
                if message['type'] == 'ready':
                    with self.lock:
                        self.ready = True
                        self._signal()
                yield message
        except (EOFError, OSError):
            pass
        finally:
            self.reader.close()
            self.process.join()
        yield {'type': 'exit', 'exit_code': self.process.exitcode}

    def close(self):
        self.process.close()
