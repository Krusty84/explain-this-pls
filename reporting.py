# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Presentation and a private stdlib logging sink for one audit run."""
from __future__ import annotations
from collections import deque
from dataclasses import asdict, dataclass, field, replace
import datetime as dt
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import sys
import subprocess
import threading
import time
import traceback
import unicodedata
from typing import Any
from contracts import ContractError, json_error_details

LABEL_COLORS = {'[WARN]': '\x1b[33m', '[RUN]': '\x1b[36m', '[OK]': '\x1b[32m',
                '[FAIL]': '\x1b[31m', '[SKIP]': '\x1b[90m'}
STATUS_COLORS = {'Complete': '\x1b[32m', 'Failed': '\x1b[31m',
                 'May be incomplete': '\x1b[33m', 'Not started': '\x1b[90m'}
STAGE_MESSAGES = {
    'catalog': ('Cataloging subsystems…', 'Subsystem catalog', 'Subsystem catalog created.'),
    'study': ('Analyzing project…', 'Project analysis', 'Architecture report created.'),
    'revise': ('Revising architecture report…', 'Architecture revision', 'Revised architecture report created.'),
    'review': ('Reviewing report…', 'Report review', 'Review complete. No significant issues reported.'),
    'compare': ('Comparing branch reports…', 'Branch report comparison', 'Branch report comparison ready.'),
}

SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
FRAME_INTERVAL = 0.125
ANIMATION_DELAY = 0.5


def cell_width(text):
    """Width of already escaped, unstyled text (no terminal control sequences)."""
    return sum(0 if unicodedata.category(c).startswith('M') else
               2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


def clip_cells(text, limit):
    if cell_width(text) <= limit:
        return text
    result, used = [], 0
    for char in text:
        width = cell_width(char)
        if used + width > limit - 1:
            break
        result.append(char)
        used += width
    return ''.join(result) + '~' if limit > 0 else ''


@dataclass
class StageProgress:
    context: dict
    started: float
    next_wait: float
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    cli: bool = False
    last_output: float | None = None
    drawn: bool = False
    last_frame: str | None = None


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    snapshot: str | None = None
    node_path: str | None = None
    required_commit: str | None = None
    hint: str | None = None
    failure_kind: str | None = None
    failure_layer: str | None = None
    details: dict | None = None


def diagnostic(exc: BaseException) -> Diagnostic:
    if hasattr(exc, 'diagnostic'):
        data = exc.diagnostic()
        if data.code == 'SUBMODULE_NOT_INITIALIZED' and data.hint is None:
            data = replace(data, hint='Prepare this submodule and run --check again.\n'
                                     'Initialization may download data and change its checkout.')
        if data.node_path is None and getattr(exc, 'node', None) is not None:
            return Diagnostic(**(asdict(data) | {'node_path': exc.node}))
        return data
    if isinstance(exc, KeyboardInterrupt):
        return Diagnostic('INTERRUPTED', 'The run was interrupted.')
    if isinstance(exc, subprocess.TimeoutExpired):
        return Diagnostic('COMMAND_TIMEOUT', 'Local command exceeded its time limit.',
                          failure_kind='STAGE_TIMEOUT', failure_layer='execution')
    if isinstance(exc, json.JSONDecodeError):
        return Diagnostic('INVALID_RESPONSE', 'Invalid JSON syntax.', failure_kind='INVALID_JSON',
                          failure_layer='result', details=json_error_details(exc))
    if isinstance(exc, ContractError):
        # Contract exceptions can embed arbitrary values returned by a model.
        return Diagnostic('BACKEND_INCOMPATIBLE' if exc.failure_kind == 'BACKEND_INCOMPATIBLE' else 'INVALID_RESPONSE', exc.safe_message,
                          hint='Inspect the private attempt artifacts listed in invocation.json.',
                          failure_kind=exc.failure_kind, failure_layer=exc.failure_layer, details=exc.details)
    if isinstance(exc, UnicodeError):
        return Diagnostic('INVALID_ENCODING', 'Input or CLI output could not be decoded.')
    if isinstance(exc, OSError):
        return Diagnostic('IO_ERROR', str(exc), hint='Check the path, available disk space and access permissions.')
    return Diagnostic('INTERNAL_ERROR', 'An unexpected internal error stopped the run.',
                      hint='Inspect the technical log and report this error with its run ID.')


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


def output_mode(requested: str, stdout) -> str:
    return ('text' if stdout.isatty() else 'json') if requested == 'auto' else requested


def safe_text(value: Any) -> str:
    """Escape terminal controls (including bidi controls), retaining readable Unicode."""
    result = []
    for char in str(value):
        if unicodedata.category(char).startswith('C') or char in '\u2028\u2029':
            result.append({'\n': r'\n', '\r': r'\r', '\t': r'\t'}.get(char,
                          f'\\x{ord(char):02x}' if ord(char) < 256 else f'\\u{ord(char):04x}'))
        else:
            result.append(char)
    return ''.join(result)


def existing_file(path: Path | None) -> bool:
    try:
        return path is not None and path.is_file()
    except OSError:
        return False


@dataclass(frozen=True)
class Event:
    event: str
    level: int
    time: str
    run_id: str | None
    context: dict[str, Any] = field(default_factory=dict)
    exception: str | None = None


class EventFormatter(logging.Formatter):
    def format(self, record):
        event = record.audit_event
        return json.dumps({'time': event.time, 'level': record.levelname,
                           'event': event.event, 'run_id': event.run_id,
                           **event.context, **({'exception': event.exception} if event.exception else {})},
                          ensure_ascii=True)


class RunLogHandler(logging.StreamHandler):
    def handleError(self, record):
        # Let Reporter disable the sink once; never invoke logging's stderr fallback.
        raise

    def close(self):
        with contextlib.suppress(OSError, ValueError):
            self.stream.close()
        super().close()


class NullReporter:
    clock = staticmethod(time.monotonic)
    progress_interval = 30.0
    progress = False

    def emit(self, event, level=logging.INFO, **context):
        pass

    def error(self, exc, *, phase='run', **context):
        return asdict(diagnostic(exc))

    def stop_requested(self):
        pass

    def stop_progress(self):
        pass

    def cli_started(self):
        return False


class Reporter(NullReporter):
    def __init__(self, *, mode='text', verbose=False, progress=True, stdout=None, stderr=None,
                 clock=time.monotonic, progress_interval=30.0):
        self.mode, self.verbose, self.progress = mode, verbose, progress
        self.stdout = sys.stdout if stdout is None else stdout
        self.stderr = sys.stderr if stderr is None else stderr
        self.colors = {}
        self.redraw = False
        for stream in (self.stdout, self.stderr):
            try:
                tty = bool(stream.isatty()) and os.environ.get('TERM') != 'dumb'
            except Exception:
                tty = False
            self.colors[id(stream)] = tty and not os.environ.get('NO_COLOR')
            if stream is self.stderr:
                self.redraw = tty
        self._lock = threading.RLock()
        self._progress = None
        self._closed = False
        self.frames = SPINNER
        self._encoding = getattr(self.stderr, 'encoding', None)
        if not isinstance(self._encoding, str):
            self._encoding = 'utf-8'
        try:
            SPINNER.encode(self._encoding)
        except LookupError:
            self._encoding, self.redraw = 'utf-8', False
        except UnicodeError:
            self.frames = '|/-\\'
        self.clock, self.progress_interval = clock, progress_interval
        self.started = clock()
        self.run_id = None
        self.log_path = None
        self.logger = logging.Logger('explain-this-pls', logging.DEBUG)
        self.logger.propagate = False
        self.handler = None
        self.early = deque(maxlen=100)
        self.failed_streams = set()
        self.finished = self.stopping = self.log_failed = False
        # Only redact known credential values, never record the environment itself.
        self.secrets = tuple(value for key, value in os.environ.items()
                             if re.search(r'(?:TOKEN|PASSWORD|SECRET|API_KEY|CREDENTIAL)', key, re.I)
                             and len(value) >= 4)

    def redact(self, value: str) -> str:
        value = re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+@', r'\1[REDACTED]@', value)
        value = re.sub(r'(?i)((?:api[_-]?key|token|password|secret)=)[^\s&]+', r'\1[REDACTED]', value)
        for secret in self.secrets:
            value = value.replace(secret, '[REDACTED]')
        return value

    def clean(self, value):
        if isinstance(value, dict):
            return {self.redact(str(k)): self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        return self.redact(str(value)) if isinstance(value, (str, Path)) else value

    def display(self, value):
        return safe_text(self.redact(str(value)))

    def status(self, value, stream):
        text = self.display(value)
        if self.colors.get(id(stream)) and value in STATUS_COLORS:
            return STATUS_COLORS[value] + text + '\x1b[0m'
        return text

    def stage_source(self, context):
        return '' if context.get('stage') == 'compare' else context.get('source_name') or context.get('branch') or ''

    def stage_message(self, context, message):
        source = self.stage_source(context)
        return (self.display(source) + ' / ' if source else '') + self.display(message)

    def _write(self, stream, text):
        """All console writes, including frames, hold _lock and flush explicitly."""
        if id(stream) in self.failed_streams:
            return
        try:
            if stream is self.stderr:
                # Keep status/diagnostic lines readable on the same restricted
                # encoding as the spinner (including a strict ASCII stream).
                text = text.encode(self._encoding, errors='backslashreplace').decode(self._encoding)
            stream.write(text)
            stream.flush()
        except (OSError, UnicodeError, ValueError):
            self.failed_streams.add(id(stream))
            if stream is self.stderr and self._progress:
                self._progress.stop.set()

    def _labels(self, text, stream=None):
        if self.colors.get(id(self.stderr if stream is None else stream)):
            return re.sub(r'^\[(?:WARN|RUN|OK|FAIL|SKIP)\](?= |$)',
                          lambda m: LABEL_COLORS[m[0]] + m[0] + '\x1b[0m', text, flags=re.MULTILINE)
        return text

    def write(self, stream, text):
        with self._lock:
            state = self._progress
            self._clear_progress(state)
            self._write(stream, self._labels(text, stream) + '\n')
            if state and not state.stop.is_set():
                self._draw_progress(state)

    def _terminal_columns(self):
        try:
            # Query stderr itself, never stdout or the COLUMNS environment variable.
            return os.get_terminal_size(self.stderr.fileno()).columns
        except Exception:
            return 0

    def _progress_line(self, state):
        if not self.redraw:
            return None
        columns = self._terminal_columns() - 1
        elapsed = max(0, self.clock() - state.started)
        index = 0 if elapsed < ANIMATION_DELAY else 1 + int((elapsed - ANIMATION_DELAY) / FRAME_INTERVAL)
        timer = duration(elapsed)
        tail = '  ' + self.frames[index % len(self.frames)] + ' ' + timer
        c = state.context
        # Encode before measuring: ASCII replacement may expand escaped characters.
        def display(value):
            text = self.display(value)
            return text.encode(self._encoding, errors='backslashreplace').decode(self._encoding)
        source = display(self.stage_source(c))
        stage = display(STAGE_MESSAGES[c['stage']][0])
        fixed = '[RUN]  / ' + stage + tail
        # Reserve the hours field so a truncated source does not move the stage
        # label when MM:SS becomes HH:MM:SS.
        source_columns = columns - cell_width(fixed) - max(0, 8 - len(timer))
        if source and source_columns >= 1:
            line = '[RUN] ' + clip_cells(source, source_columns) + ' / ' + stage + tail
        else:
            # Keep stage and time when the source cannot fit; tiny/unknown terminals
            # use the regular, infrequent line-oriented fallback.
            fixed = '[RUN] ' + stage + tail
            if columns < cell_width(fixed):
                return None
            line = fixed
        return self._labels(line)

    def _clear_progress(self, state):
        if state and state.drawn:
            self._write(self.stderr, '\r\x1b[2K')
            state.drawn = False

    def _draw_progress(self, state):
        line = self._progress_line(state)
        if line is None:
            self._clear_progress(state)
            return
        if not state.drawn or line != state.last_frame:
            # Overwrite in one flushed write; erase only the old trailing suffix.
            self._write(self.stderr, ('\r' if state.drawn else '') + line + '\x1b[K')
            state.drawn, state.last_frame = True, line

    def _start_worker(self, state):
        state.thread = threading.Thread(target=self._refresh_progress, args=(state,),
                                        name='audit-progress', daemon=True)
        state.thread.start()

    def _refresh_progress(self, state):
        while not state.stop.wait(FRAME_INTERVAL if self.redraw else self.progress_interval):
            self._tick_progress(state)

    def _tick_progress(self, state):
        """One presentation tick; never touches an execution budget or transport."""
        with self._lock:
            if self._progress is not state or state.stop.is_set():
                return
            self._draw_progress(state)
            current = self.clock()
            if current >= state.next_wait:
                state.next_wait = current + self.progress_interval
                activity = ({'last_output_seconds': None if state.last_output is None else
                             current - state.last_output} if state.cli else {})
                self.emit('process_waiting', **state.context,
                          elapsed_seconds=current - state.started, **activity)

    def cli_started(self):
        with self._lock:
            if self._progress is None:
                return False
            self._progress.cli, self._progress.last_output = True, None
            return True

    def cli_output(self):
        with self._lock:
            if self._progress:
                self._progress.last_output = self.clock()

    def stop_progress(self):
        with self._lock:
            state, self._progress = self._progress, None
            if state:
                state.stop.set()
                self._clear_progress(state)
        # The worker needs _lock to finish a tick. Never join while holding it.
        if (state and state.thread and state.thread.ident is not None
                and state.thread is not threading.current_thread()):
            state.thread.join()

    def attach_log(self, run_dir: Path):
        with self._lock:
            self._attach_log(run_dir)

    def _attach_log(self, run_dir):
        if self.handler or self.log_failed:
            return
        path = run_dir / 'run.log'
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            stream = os.fdopen(fd, 'w', encoding='utf-8')
            self.handler = RunLogHandler(stream)
            self.handler.setFormatter(EventFormatter())
            self.logger.addHandler(self.handler)
            self.log_path = path
            for event in self.early:
                self.log(event)
            self.early.clear()
        except (OSError, ValueError):
            self.disable_log()

    def disable_log(self):
        with self._lock:
            self._disable_log()

    def _disable_log(self):
        if self.handler:
            self.logger.removeHandler(self.handler)
            self.handler.close()
            self.handler = None
        if not self.log_failed:
            self.log_failed = True
            self.write(self.stderr, '[WARN] Error details could not be saved. Analysis will continue.')

    def log(self, event):
        with self._lock:
            if self.handler:
                try:
                    self.logger.log(event.level, event.event, extra={'audit_event': event})
                except (OSError, ValueError):
                    self.disable_log()
            elif not self.log_failed:
                self.early.append(event)

    def emit(self, event, level=logging.INFO, *, exception=None, _started=None, **context):
        if event in ('stage_started', 'stage_completed', 'error', 'stop_requested',
                     'restoration_started', 'run_completed'):
            self.stop_progress()
        with self._lock:
            self._emit(event, level, exception=exception, _started=_started, **context)

    def _emit(self, event, level, *, exception, _started, **context):
        if event in ('root_warning', 'description_missing', 'stop_requested'):
            level = logging.WARNING
        item = Event(event, level, dt.datetime.now(dt.timezone.utc).isoformat(), self.run_id,
                     self.clean(context), exception)
        self.log(item)
        if (event == 'stage_started' and context.get('stage') in STAGE_MESSAGES
                and self.progress and not self._closed and not self.finished and not self.stopping
                and id(self.stderr) not in self.failed_streams):
            started = self.clock() if _started is None else _started
            self._progress = StageProgress(item.context, started, started + self.progress_interval)
        lines = self.render(item)
        if lines:
            self.write(self.stderr, '\n'.join(lines))
        if self.verbose and event not in ('process_waiting', 'error'):
            details = item.context | {'run_id': item.run_id} if event == 'run_started' else item.context
            self.write(self.stderr, '[RUN] Detail: ' + self.display(event) + ' ' +
                       self.display(json.dumps(details, ensure_ascii=False)))
        if event == 'stage_started' and self._progress:
            self._draw_progress(self._progress)
            if not self._progress.stop.is_set():
                self._start_worker(self._progress)

    def exception_chain(self, exc):
        """Keep every stack and cause, without locals or model-supplied exception values."""
        trace = traceback.TracebackException.from_exception(exc, capture_locals=False)
        def scrub(item, error):
            item._str = self.redact(diagnostic(error).message)
            if item.__cause__ is not None:
                scrub(item.__cause__, error.__cause__)
            if item.__context__ is not None:
                scrub(item.__context__, error.__context__)
        scrub(trace, exc)
        return self.redact(''.join(trace.format()))

    def error(self, exc, *, phase='run', **context):
        data = asdict(diagnostic(exc))
        self.emit('error', logging.ERROR, phase=phase, **context, **data,
                  exception=self.exception_chain(exc))
        return data

    def stop_requested(self):
        if not self.stopping:
            self.stopping = True
            self.emit('stop_requested', logging.WARNING)

    def render(self, item):
        c, name = item.context, item.event
        s = self.display
        if name == 'stage_recovered':
            return ['[WARN] ' + self.stage_message(c, 'Report text saved, but it did not pass all checks.')]
        active, title, complete = STAGE_MESSAGES.get(c.get('stage'), ('Analyzing project…', 'Analysis', 'Analysis complete.'))
        if name == 'run_started':
            return ['[RUN] ' + ('Checking local setup.' if c['check_only'] else 'Preparing analysis.'),
                    *(['Source: ' + s(c['source'])] if c.get('source') else [])]
        if name == 'root_warning':
            return ['[WARN] Running with administrator privileges.']
        if name == 'description_missing':
            return ['[WARN] Project description is missing. Set project_description to describe the system purpose and history.']
        if name == 'stage_started':
            if self._progress and self._progress_line(self._progress) is not None:
                return []
            return ['[RUN] ' + self.stage_message(c, active)]
        if name == 'process_waiting':
            if not self.progress or (self._progress and self._progress_line(self._progress) is not None):
                return []
            last = ''
            if self.verbose and 'last_output_seconds' in c:
                last = ' | ' + ('No CLI output received yet' if c['last_output_seconds'] is None else
                                'Last CLI output: ' + duration(c['last_output_seconds']) + ' ago')
            return ['[RUN] ' + self.stage_message(c, active), '      Elapsed: ' + duration(c['elapsed_seconds']) +
                    last]
        if name == 'stage_completed':
            status = c['status']
            label = '[OK] ' if status == 'COMPLETE' else '[FAIL] ' if status == 'FAILED' else '[WARN] '
            meaning = complete if status == 'COMPLETE' else title + (
                ' failed. See details below.' if status == 'FAILED' else ' may be incomplete. See details below.')
            return [label + self.stage_message(c, meaning) +
                    ' | Elapsed: ' + duration(c['elapsed_seconds'])]
        if name == 'stage_skipped':
            title = 'Review' if c.get('stage') == 'review' else title
            return ['[SKIP] ' + self.stage_message(c, title + ' skipped: no architecture report available.')]
        if name == 'stop_requested':
            return ['[WARN] Stopping analysis…']
        if name == 'error':
            label = {'preflight': 'Could not prepare analysis.',
                     'restoration': 'Could not return the repository to its original state.',
                     'stage': self.stage_message(c, title + ' failed.')}.get(c['phase'], 'Analysis failed.')
            interrupted = c['code'] == 'INTERRUPTED'
            messages = {
                'INVALID_RESPONSE': 'The response could not be used.',
                'MATERIALIZATION_ERROR': 'The architecture report could not be created.',
                'ARTIFACT_CONTRACT_ERROR': 'The report could not be saved because it failed validation.',
            }
            message = c['message'] if self.verbose else messages.get(c['code'], c['message'])
            if not self.verbose:
                if c.get('failure_layer') == 'cleanup':
                    message = 'The agent could not be stopped or cleaned up completely.'
                elif c.get('failure_kind') == 'STAGE_TIMEOUT':
                    message = 'The operation exceeded its time limit.'
                elif c.get('failure_kind') == 'IDLE_TIMEOUT':
                    message = 'No activity was detected within the time limit.'
            lines = ['[WARN] Analysis interrupted.' if interrupted else '[FAIL] ' + label]
            if self.verbose:
                lines += ['Code: ' + s(c['code'])]
            if not interrupted or self.verbose:
                lines += ['', 'Reason:', '  ' + s(message)]
            fields = [('node_path', 'Path' if c.get('node_path') == '.' else 'Submodule')]
            if self.verbose:
                fields = [('snapshot', 'Snapshot'), *fields, ('required_commit', 'Required commit')]
            for key, title in fields:
                if c.get(key):
                    lines += ['', title + ':', *('  ' + s(line) for line in c[key].split('\n'))]
            hint = c.get('hint')
            if not self.verbose and c['code'] in messages:
                hint = 'Use --verbose for details.'
            elif not self.verbose and c['code'] == 'INTERNAL_ERROR':
                hint = 'Use --verbose for details and report this error.'
            if hint:
                lines += ['', 'Next step:', *('  ' + s(line) for line in hint.split('\n'))]
            if c.get('analysis_started') is False:
                lines += ['', 'Analysis has not started.']
                if self.verbose and c.get('switches_performed') is False:
                    lines[-1] += ' No checkout switches were performed.'
            if self.verbose and c.get('details'):
                lines += ['', 'Diagnostic details:', '  ' + s(json.dumps(c['details'], ensure_ascii=False))]
            if existing_file(self.log_path):
                lines += ['', 'Error details:', '  ' + s(self.log_path)]
            return lines
        return []

    def finish(self, result, manifest, *, check_only, config_path, run_dir=None, trust=False):
        self.stop_progress()
        if self.finished:
            return
        self.finished = True
        elapsed = self.clock() - self.started
        self.emit('run_completed', status=result['status'], exit_code=result['exit_code'], elapsed_seconds=elapsed)
        if self.mode == 'json':
            self.write(self.stdout, json.dumps(result, ensure_ascii=True))
            return
        s = self.display
        status = lambda value: self.status(value, self.stdout)
        meaning = ('[WARN] Analysis interrupted.' if result['exit_code'] == 130 else
                   '[OK] Local setup checked. Analysis has not started.' if result['status'] == 'PREFLIGHT_OK' else
                   '[OK] Analysis complete. Reports may still contain errors.' if result['status'] == 'COMPLETE' else
                   '[WARN] Analysis may be incomplete. See available results and limitations below.' if result['status'] == 'PARTIAL' else
                   '[FAIL] Analysis failed.')
        lines = [meaning, 'Elapsed: ' + duration(elapsed)]
        if result['status'] == 'PREFLIGHT_OK':
            command = ['python3', 'explain.py', '--config', str(config_path)]
            if trust:
                command.append('--trust-repository')
            lines += ['', 'AI service access and model availability were not checked.', '',
                      'Start analysis:', '  ' + s(shlex.join(command))]
        elif not check_only:
            for branch in manifest.get('branches', []):
                partial_material = manifest.get('result_policy') == 'compromise' and branch.get('study_usable')
                lines += ['Branch ' + s(branch['branch']) + ': ' +
                          status('Complete' if branch.get('accepted') else 'May be incomplete' if partial_material
                                 else 'Failed' if branch['errors'] else 'May be incomplete')]
            analyzed = {branch['branch'] for branch in manifest.get('branches', [])}
            for branch in manifest.get('pins', {}):
                if branch not in analyzed:
                    lines += ['Branch ' + s(branch) + ': ' + status('Not started')]
        paths = [('Manifest', Path(result['manifest']))] if self.verbose and result['manifest'] else []
        if self.verbose and self.log_path:
            paths.append(('Technical log', self.log_path))
        if run_dir and not check_only:
            if manifest.get('final_report'):
                paths.append(('Final report' if manifest.get('has_usable_material') else 'Error summary',
                              Path(manifest['final_report'])))
            for branch in manifest.get('branches', []):
                for name in ('ARCHITECTURE.md', 'ARCHITECTURE_REVIEW.md'):
                    stage = 'study' if name == 'ARCHITECTURE.md' else 'review'
                    if branch.get(stage + '_invocation', {}).get('publication_complete'):
                        paths.append(('Report (' + s(branch['branch']) + ')', run_dir / branch['directory'] / name))
            for relative in ('ARCHITECTURE.md', 'ARCHITECTURE_REVIEW.md', 'comparison/BRANCH_COMPARISON.md'):
                stage = {'ARCHITECTURE.md': 'study', 'ARCHITECTURE_REVIEW.md': 'review'}.get(relative, 'comparison')
                if manifest.get(stage + '_invocation', {}).get('publication_complete'):
                    paths.append(('Report', run_dir / relative))
        for title, path in paths:
            if existing_file(path):
                lines += ['', title + ':', '  ' + s(path)]
        self.write(self.stdout, '\n'.join(lines))

    def close(self):
        self._closed = True
        self.stop_progress()
        with self._lock:
            if self.handler:
                self.logger.removeHandler(self.handler)
                self.handler.close()
                self.handler = None
            self.early.clear()
