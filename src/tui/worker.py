# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""An isolated audit process; only sanitized presentation data crosses its pipe."""
import contextlib
import multiprocessing
from pathlib import Path
import signal
import sys
import threading

from explain import run_audit
from src.runtime.reporting import Reporter, existing_file


class PipeReporter(Reporter):
    def __init__(self, connection, *, verbose=False):
        self.connection = connection
        self.disconnected = False
        super().__init__(mode='json', verbose=verbose, progress=False, event_sink=self.send_event)

    def send(self, message):
        if self.disconnected:
            return
        try:
            self.connection.send(self.clean(message))
        except (BrokenPipeError, EOFError, OSError):
            self.disconnected = True
            # Let the normal audit cleanup run if its UI disappears.
            raise KeyboardInterrupt('The interactive interface disconnected.')

    def send_event(self, event):
        self.send({'type': 'event', 'event': event.event, 'context': event.context})

    def _write(self, stream, text):
        # The parent owns the terminal; the existing private log remains active.
        pass

    def finish(self, result, manifest, **options):
        if self.finished:
            return
        super().finish(result, manifest, **options)
        paths = []
        for label, value in (('Final report', result.get('final_report')),
                             ('Manifest', result.get('manifest')), ('Technical log', self.log_path)):
            if value and existing_file(Path(value)):
                paths.append((label, str(value)))
        self.send({'type': 'result', 'result': result, 'paths': paths,
                   'diagnostics': manifest.get('diagnostics', []), 'check_only': options['check_only']})


def audit_worker(args, connection):
    reporter = PipeReporter(connection, verbose=args.verbose)
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Analysis interrupted.')
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
                       'diagnostics': [{'message': 'Analysis interrupted before starting.' if code == 130
                                        else 'The audit worker stopped unexpectedly.'}]})
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
