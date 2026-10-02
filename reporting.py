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
STATUS_COLORS = {'COMPLETE': '\x1b[32m', 'PREFLIGHT PASSED': '\x1b[32m', 'verified': '\x1b[32m',
                 'FAILED': '\x1b[31m', 'PARTIAL': '\x1b[33m', 'BLOCKED': '\x1b[33m',
                 'not started': '\x1b[90m', 'not completed': '\x1b[90m', 'not performed': '\x1b[90m',
                 'not applicable': '\x1b[90m'}

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

    def _labels(self, text):
        if self.colors.get(id(self.stderr)):
            return re.sub(r'^\[(?:WARN|RUN|OK|FAIL|SKIP)\](?= |$)',
                          lambda m: LABEL_COLORS[m[0]] + m[0] + '\x1b[0m', text, flags=re.MULTILINE)
        return text

    def write(self, stream, text):
        with self._lock:
            state = self._progress
            self._clear_progress(state)
            self._write(stream, (self._labels(text) if stream is self.stderr else text) + '\n')
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
        source = display(c.get('source_name') or c.get('branch') or '')
        stage = display(c['stage']) + ' / ' + display(c['backend'])
        fixed = '[RUN]  / ' + stage + tail
        # Reserve the hours field so a truncated source does not move the stage
        # label when MM:SS becomes HH:MM:SS.
        source_columns = columns - cell_width(fixed) - max(0, 8 - len(timer))
        if source_columns >= 1:
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
            self.write(self.stderr, '[WARN] Technical log is unavailable or incomplete; execution and cleanup continue.')

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
        if (event == 'stage_started' and context.get('stage') in ('study', 'review', 'compare')
                and self.progress and not self._closed and not self.finished and not self.stopping
                and id(self.stderr) not in self.failed_streams):
            started = self.clock() if _started is None else _started
            self._progress = StageProgress(item.context, started, started + self.progress_interval)
        lines = self.render(item)
        if lines:
            self.write(self.stderr, '\n'.join(lines))
        if self.verbose and event not in ('process_waiting', 'error'):
            self.write(self.stderr, '[RUN] Detail: ' + self.display(event) + ' ' +
                       self.display(json.dumps(item.context, ensure_ascii=False)))
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
        if name in ('study_normalized', 'review_normalized'):
            stage_label = 'Study' if name == 'study_normalized' else 'Review'
            return [f"[RUN] {stage_label} evidence IDs/references normalized: {s(c['replacement_count'])} replacement(s); "
                    'contract, source and policy checks remain required.']
        if name == 'stage_recovered':
            return ['[WARN] ' + s(c['message'])]
        stage = ' / '.join(s(value) for value in (c.get('source_name') or c.get('branch'),
                                                c.get('stage'), c.get('backend')) if value)
        checks = {'configuration': 'Configuration', 'git': 'Git version and selected branches',
                  'submodules': 'Submodules and required local objects', 'integrity': 'Source integrity',
                  'inventory': 'Source inventory and fingerprint'}
        if name == 'run_started':
            return ['explain-this-pls — ' + ('preflight check' if c['check_only'] else 'source analysis'),
                    'Source: ' + s(c.get('source') or '(not loaded)'), 'Run:    ' + s(item.run_id or '(not assigned)'),
                    'Mode:   ' + ('human-readable' if c['output'] == 'text' else s(c['output'])) +
                    ' / ' + s(c.get('mode') or 'not loaded'), '']
        if name == 'configuration_loaded':
            return ['Repository: ' + s(c['repository_name']),
                    'Branches: ' + (', '.join(map(s, c['branches'])) or '(folder mode)'),
                    'Agents: ' + ', '.join(s(k) + '=' + s(v) for k, v in c['agents'].items())]
        if name == 'root_warning':
            return ['[WARN] Running as root; child CLIs inherit root privileges.']
        if name == 'description_missing':
            return ['[WARN] Project description is missing. Set project_description to describe the system purpose and history.']
        if name == 'preflight_started':
            label = s(c['backend']) + ' executable and required CLI options' if c['check'] == 'cli' else checks.get(c['check'], s(c['check'])).lower()
            return ['[RUN] Checking ' + label + '.']
        if name == 'preflight_completed':
            label = (s(c['backend']) + ' executable and required CLI options' if c['check'] == 'cli'
                     else checks.get(c['check'], s(c['check'])))
            return ['[OK] ' + label]
        if name in ('snapshot_started', 'snapshot_completed'):
            return [('[RUN] Checking local objects for snapshot: ' if name == 'snapshot_started' else
                     '[OK] Local objects for snapshot: ') + s(c['snapshot'])]
        if name == 'branch_started':
            return ['[RUN] Preparing ' + s(c['branch']) + ' at ' + s(c['commit'][:12])]
        if name == 'stage_started':
            if self._progress and self._progress_line(self._progress) is not None:
                return []
            return ['[RUN] ' + stage]
        if name == 'process_waiting':
            if not self.progress or (self._progress and self._progress_line(self._progress) is not None):
                return []
            last = ''
            if 'last_output_seconds' in c:
                last = ' | ' + ('No CLI output received yet' if c['last_output_seconds'] is None else
                                'Last CLI output: ' + duration(c['last_output_seconds']) + ' ago')
            return ['[RUN] ' + stage, '      Elapsed: ' + duration(c['elapsed_seconds']) +
                    last]
        if name == 'stage_completed':
            status = c['status']
            label = '[OK] ' if status == 'COMPLETE' else '[FAIL] ' if status == 'FAILED' else '[WARN] '
            meaning = ('Description generated; agent reports investigation complete in its stated scope'
                       if c.get('stage') == 'study' and status == 'COMPLETE' else
                       'No material issues reported for required registry; policy checks satisfied'
                       if c.get('stage') == 'review' and status == 'COMPLETE' else
                       'Reports-only comparison generated; agent reports completion'
                       if status == 'COMPLETE' else 'Policy checks not completed or not satisfied; see diagnostics')
            return [label + stage + ' — ' + self.status(status, self.stderr) + ': ' + meaning +
                    ' | Elapsed: ' + duration(c['elapsed_seconds'])]
        if name == 'stage_skipped':
            return ['[SKIP] ' + stage + ': No usable architecture document.']
        if name == 'stop_requested':
            return ['[WARN] Stop requested.']
        if name == 'process_stopping':
            return ['[RUN] Stopping the active CLI process.']
        if name == 'restoration_started':
            return ['[RUN] Restoring the original checkout hierarchy.']
        if name == 'restoration_completed':
            return ['[OK] Original checkout hierarchy restored.']
        if name == 'error':
            label = {'preflight': 'Preflight stopped', 'restoration': 'Restoration failed',
                     'stage': stage + ' failed', 'run': 'Run stopped'}[c['phase']]
            lines = ['[FAIL] ' + label, 'Code: ' + s(c['code']), '', 'Reason:', '  ' + s(c['message'])]
            for key, title in (('snapshot', 'Snapshot'), ('node_path', 'Node' if c.get('node_path') == '.' else 'Submodule'),
                               ('required_commit', 'Required commit'), ('hint', 'Next step')):
                if c.get(key):
                    lines += ['', title + ':', *('  ' + s(line) for line in c[key].split('\n'))]
            if c.get('analysis_started') is False:
                lines += ['', 'Analysis has not started.' + (' No checkout switches were performed.'
                          if c.get('switches_performed') is False else '')]
            if existing_file(self.log_path):
                lines += ['', 'Details:', '  ' + s(self.log_path)]
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
        meaning = ('Local launch prerequisites checked. Source analysis was not performed.' if result['status'] == 'PREFLIGHT_OK' else
                   'Policy checks satisfied; factual correctness is not established.' if result['status'] == 'COMPLETE' else
                   'Policy checks not completed or not satisfied; see limitations and diagnostics.')
        lines = [status('PREFLIGHT PASSED' if result['status'] == 'PREFLIGHT_OK' else result['status']) + ': ' + meaning,
                 'Elapsed: ' + duration(elapsed)]
        if result['status'] == 'PREFLIGHT_OK':
            command = ['python3', 'explain.py', '--config', str(config_path)]
            if trust:
                command.append('--trust-repository')
            lines += ['', 'No model calls were made. Source analysis has not started.', '',
                      'Checked: configuration, local source prerequisites, executable version and required CLI options.',
                      'Model availability and provider authorization were not tested.', '',
                      'Start analysis:', '  ' + s(shlex.join(command))]
        elif not check_only:
            for branch in manifest.get('branches', []):
                partial_material = manifest.get('result_policy') == 'compromise' and branch.get('study_usable')
                lines += ['Branch ' + s(branch['branch']) + ': ' +
                          status('COMPLETE' if branch.get('accepted') else 'PARTIAL' if partial_material
                                 else 'FAILED' if branch['errors'] else 'PARTIAL') +
                          (' — processing/review policy satisfied' if branch.get('accepted') else
                           ' — processing incomplete or evidence insufficient')]
            analyzed = {branch['branch'] for branch in manifest.get('branches', [])}
            for branch in manifest.get('pins', {}):
                if branch not in analyzed:
                    lines += ['Branch ' + s(branch) + ': ' + status('not started')]
            if manifest.get('mode') == 'folder':
                lines += ['Source result: ' + status('COMPLETE' if manifest.get('accepted') else result['status']) +
                          (' — processing/review policy satisfied' if manifest.get('accepted') else
                           ' — processing incomplete or evidence insufficient'),
                          'Comparison: ' + status('not applicable') + ' (folder mode)',
                          'Restoration: ' + status('not applicable') + ' (folder mode)']
            else:
                if 'comparison' not in manifest and len(manifest.get('pins', {})) == 1:
                    lines += ['Comparison: ' + status('not applicable') + ' (single branch)']
                else:
                    lines += ['Comparison: ' + status(manifest.get('comparison', {}).get('completion_status', 'not completed')) +
                              ' — supplied reports only; no source inspection in this stage']
                restoration = manifest.get('restoration')
                lines += ['Restoration: ' + status('verified' if restoration and restoration['restored'] else
                          'FAILED' if restoration else 'not performed')]
        paths = [('Manifest', Path(result['manifest']))] if result['manifest'] else []
        if self.log_path:
            paths.append(('Technical log', self.log_path))
        if run_dir and not check_only:
            if manifest.get('final_report'):
                paths.append(('Final report' if manifest.get('has_usable_material') else 'Final report (diagnostic only)',
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
