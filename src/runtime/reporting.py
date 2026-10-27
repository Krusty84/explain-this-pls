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
from src.messages import STAGE_MESSAGES, message as ui_message
from src.contracts.contracts import ContractError, json_error_details

LABEL_COLORS = {'[WARN]': '\x1b[33m', '[RUN]': '\x1b[36m', '[OK]': '\x1b[32m',
                '[FAIL]': '\x1b[31m', '[SKIP]': '\x1b[90m'}
STATUS_COLORS = {ui_message('run.complete'): '\x1b[32m', ui_message('run.complete_with_warnings'): '\x1b[33m', ui_message('run.failed'): '\x1b[31m',
                 ui_message('run.may_be_incomplete'): '\x1b[33m', ui_message('run.not_started'): '\x1b[90m'}

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
            data = replace(data, hint=ui_message('run.prepare_this_submodule_and_check_again_initialization_may'))
        if data.node_path is None and getattr(exc, 'node', None) is not None:
            return Diagnostic(**(asdict(data) | {'node_path': exc.node}))
        return data
    if isinstance(exc, KeyboardInterrupt):
        return Diagnostic('INTERRUPTED', ui_message('run.the_was_interrupted'))
    if isinstance(exc, subprocess.TimeoutExpired):
        return Diagnostic('COMMAND_TIMEOUT', ui_message('run.local_command_exceeded_its_time_limit'),
                          failure_kind='STAGE_TIMEOUT', failure_layer='execution')
    if isinstance(exc, json.JSONDecodeError):
        return Diagnostic('INVALID_RESPONSE', ui_message('run.invalid_json_syntax'), failure_kind='INVALID_JSON',
                          failure_layer='result', details=json_error_details(exc))
    if isinstance(exc, ContractError):
        # Contract exceptions can embed arbitrary values returned by a model.
        return Diagnostic('BACKEND_INCOMPATIBLE' if exc.failure_kind == 'BACKEND_INCOMPATIBLE' else 'INVALID_RESPONSE', exc.safe_message,
                          hint=ui_message('run.inspect_the_private_attempt_artifacts_listed_in_invocation'),
                          failure_kind=exc.failure_kind, failure_layer=exc.failure_layer, details=exc.details)
    if isinstance(exc, UnicodeError):
        return Diagnostic('INVALID_ENCODING', ui_message('run.input_or_cli_output_could_not_be_decoded'))
    if isinstance(exc, OSError):
        return Diagnostic('IO_ERROR', str(exc), hint=ui_message('run.check_the_path_available_disk_space_and_access'))
    return Diagnostic('INTERNAL_ERROR', ui_message('run.an_unexpected_internal_error_stopped_the'),
                      hint=ui_message('run.inspect_the_technical_log_and_report_this_error'))


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


def metric_value(measured, key):
    value = measured.get(key)
    if value is None:
        return ui_message('tui.n_a')
    text = f'${value:.2f}' if key == 'cost_usd' else f'{value:,}'
    notes = []
    if measured.get('coverage', {}).get(key) == 'partial':
        notes.append(ui_message('run.partial'))
    if key == 'total_tokens' and measured.get('total_tokens_estimated'):
        notes.append(ui_message('run.estimated'))
    return text + (' (' + ', '.join(notes) + ')' if notes else '')


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


def citation_warning_message(warning):
    """Locator metadata only; source text must never enter warning output."""
    source = (str(warning['evidence_branch']) + ' / ' if warning.get('evidence_branch') else '') + str(warning.get('source_id', ''))
    values = dict(evidence_id=warning.get('evidence_id', ''), source=source, path=warning.get('path', ''),
                  start_line=warning.get('start_line', '?'), end_line=warning.get('end_line', '?'),
                  reason=warning.get('reason', ''), action=warning.get('action', ''),
                  claims=', '.join(str(value) for value in warning.get('claim_ids', [])))
    if 'original_start_line' in warning and 'original_end_line' in warning:
        return ui_message('run.citation_warning_with_original', **values,
                          original_start_line=warning['original_start_line'],
                          original_end_line=warning['original_end_line'])
    return ui_message('run.citation_warning', **values)


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
                 clock=time.monotonic, progress_interval=30.0, event_sink=None):
        self.mode, self.verbose, self.progress = mode, verbose, progress
        self.event_sink = event_sink
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

    def progress_message(self, context):
        if 'check_only' in context:
            return ui_message('run.checking_local_setup') if context['check_only'] else ui_message('tui.preparing_analysis')
        return STAGE_MESSAGES.get(context.get('stage'), STAGE_MESSAGES['study'])[0]

    def metric_models(self, metrics):
        if metrics.get('generated_by') == 'orchestrator':
            return 'orchestrator'
        labels = []
        for entry in metrics.get('by_model', []):
            model = entry.get('model_actual')
            if model:
                label = entry['backend'] + ': ' + model
            else:
                requested = entry.get('model_requested')
                label = entry['backend'] + ': ' + (requested + ' (requested)' if requested else ui_message('run.model_n_a'))
            if label not in labels:
                labels.append(label)
        return self.display('; '.join(labels) or ui_message('run.no_model_calls'))

    def metric_lines(self, metrics, *, compact=False):
        if metrics is None:
            return []
        measured = metrics['usage']
        value = lambda key: metric_value(measured, key)
        if compact:
            return [ui_message(
                'run.tokens_agent_estimated_cost_attempts',
                value=value('total_tokens'),
                other_value=value('cost_usd'),
                attempts=str(metrics['attempts']),
                other_other_value=self.metric_models(metrics),
            )]
        return [ui_message(
            'run.tokens_input_output',
            value=value('total_tokens'),
            other_value=value('input_tokens'),
            other_other_value=value('output_tokens'),
        ),
                ui_message(
                    'run.cache_read_write_reasoning',
                    value=value('cache_read_tokens'),
                    other_value=value('cache_write_tokens'),
                    other_other_value=value('reasoning_tokens'),
                ),
                ui_message(
                    'run.agent_estimated_cost_attempts',
                    value=value('cost_usd'),
                    attempts=str(metrics['attempts']),
                    other_value=self.metric_models(metrics),
                )]

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
        stage = display(self.progress_message(c))
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
            self.write(self.stderr, ui_message('run.error_details_could_not_be_saved_analysis_will'))

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
        if event in ('run_started', 'stage_started', 'stage_completed', 'error', 'stop_requested',
                     'restoration_started', 'run_completed'):
            self.stop_progress()
        with self._lock:
            self._emit(event, level, exception=exception, _started=_started, **context)

    def _emit(self, event, level, *, exception, _started, **context):
        if event in ('root_warning', 'description_missing', 'stop_requested', 'citation_warning', 'last_analysis_warning'):
            level = logging.WARNING
        if event == 'citation_warning':
            fields = ('branch', 'source_name', 'commit', 'stage', 'revision_id', 'generated_by', 'backend',
                      'evidence_id', 'evidence_branch', 'source_id', 'path', 'start_line', 'end_line', 'reason',
                      'claim_ids', 'action', 'original_start_line', 'original_end_line')
            context = {key: context[key] for key in fields if key in context}
        item = Event(event, level, dt.datetime.now(dt.timezone.utc).isoformat(), self.run_id,
                     self.clean(context), exception)
        self.log(item)
        if self.event_sink is not None:
            self.event_sink(item)
        if ((event == 'run_started' or event == 'stage_started' and context.get('stage') in STAGE_MESSAGES)
                and self.progress and not self._closed and not self.finished and not self.stopping
                and id(self.stderr) not in self.failed_streams):
            started = self.clock() if _started is None else _started
            self._progress = StageProgress(item.context, started, started + self.progress_interval)
        lines = self.render(item)
        if lines:
            self.write(self.stderr, '\n'.join(lines))
        if self.verbose and event not in ('process_waiting', 'error'):
            details = item.context | {'run_id': item.run_id} if event == 'run_started' else item.context
            self.write(self.stderr, ui_message(
                'run.detail',
                event=self.display(event),
                value=self.display(json.dumps(details, ensure_ascii=False)),
            ))
        if event in ('run_started', 'stage_started') and self._progress:
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
            return ['[WARN] ' + self.stage_message(c, ui_message('run.report_text_saved_but_it_did_not_pass'))]
        active, title, complete = STAGE_MESSAGES.get(c.get('stage'), (ui_message('run.analyzing_project'), 'Analysis', ui_message('run.analysis_complete')))
        if name == 'run_started':
            lines = [ui_message('run.source', source=s(c['source']))] if c.get('source') else []
            if not self._progress or self._progress_line(self._progress) is None:
                lines.insert(0, '[RUN] ' + (ui_message('run.checking_local_setup.render') if c['check_only'] else ui_message('run.preparing_analysis')))
            return lines
        if name == 'root_warning':
            return [ui_message('run.running_with_administrator_privileges')]
        if name == 'description_missing':
            return [ui_message('run.project_description_is_missing_set_project_description_to')]
        if name == 'citation_warning':
            return ['[WARN] ' + self.stage_message(c, citation_warning_message(c))]
        if name == 'last_analysis_warning':
            return ['[WARN] ' + s(c['message'])]
        if name == 'stage_started':
            if self._progress and self._progress_line(self._progress) is not None:
                return []
            return ['[RUN] ' + self.stage_message(c, active)]
        if name == 'study_phase_started':
            message = ui_message('tui.collecting_claims_and_source_evidence') if c['phase'] == 'collect' else ui_message('run.writing_the_architecture_report_from_the_frozen_evidence')
            return ['[RUN] ' + self.stage_message(c, message)]
        if name == 'process_waiting':
            if not self.progress or (self._progress and self._progress_line(self._progress) is not None):
                return []
            last = ''
            if self.verbose and 'last_output_seconds' in c:
                last = ' | ' + (ui_message('run.no_cli_output_received_yet') if c['last_output_seconds'] is None else
                                ui_message('run.last_cli_output_ago', value=duration(c['last_output_seconds'])))
            return ['[RUN] ' + self.stage_message(c, self.progress_message(c)), ui_message(
                'run.elapsed',
                value=duration(c['elapsed_seconds']),
                last=last,
            )]
        if name == 'stage_completed':
            status = c['status']
            label = '[OK] ' if status == 'COMPLETE' else '[FAIL] ' if status == 'FAILED' else '[WARN] '
            meaning = (complete if status == 'COMPLETE' else
                       ui_message(
                           'run.complete_with_citation_warnings_see_affected_claims',
                           title=title,
                       ) if status == 'COMPLETE_WITH_WARNINGS' else
                       ui_message('run.stage_failed' if status == 'FAILED' else 'run.stage_incomplete', title=title))
            return [ui_message(
                'run.elapsed.render',
                label=label,
                value=self.stage_message(c, meaning),
                other_value=duration(c['elapsed_seconds']),
            ),
                    *([ui_message('run.report', report_path=s(c['report_path']))] if c.get('report_path') else []),
                    *self.metric_lines(c.get('metrics'), compact=True)]
        if name == 'stage_skipped':
            title = ui_message('tui.review.complete') if c.get('stage') == 'review' else title
            if c.get('reason') == 'disabled_by_config':
                return ['[SKIP] ' + self.stage_message(c, ui_message(
                    'run.review_disabled_by_configuration_no_separate_review_performed',
                ))]
            return ['[SKIP] ' + self.stage_message(c, ui_message('run.skipped_no_architecture_report_available', title=title))]
        if name == 'stop_requested':
            return [ui_message('run.stopping_analysis')]
        if name == 'restoration_started':
            return [ui_message('run.restoring_the_original_checkout_hierarchy')]
        if name == 'restoration_completed':
            return [ui_message('run.original_checkout_hierarchy_restored') if c.get('restored') else
                    ui_message('run.original_checkout_hierarchy_was_not_fully_restored_see')]
        if name == 'error':
            label = {'preflight': ui_message('run.could_not_prepare_analysis'),
                      'restoration': ui_message('run.could_not_restore_the_original_checkout'),
                      'cleanup': ui_message('run.could_not_remove_temporary_analysis_data'),
                     'stage': self.stage_message(c, ui_message(
                         'run.stage_error',
                         title=title,
                     ))}.get(c['phase'], ui_message('tui.analysis_failed'))
            interrupted = c['code'] == 'INTERRUPTED'
            messages = {
                'INVALID_RESPONSE': ui_message('run.the_response_could_not_be_used'),
                'MATERIALIZATION_ERROR': ui_message('run.the_architecture_report_could_not_be_created'),
                'ARTIFACT_CONTRACT_ERROR': ui_message('run.the_report_could_not_be_saved_because_it'),
            }
            message = c['message'] if self.verbose else messages.get(c['code'], c['message'])
            if not self.verbose:
                if c.get('failure_layer') == 'cleanup':
                    message = ui_message('run.the_agent_could_not_be_stopped_or_cleaned')
                elif c.get('failure_kind') == 'STAGE_TIMEOUT':
                    message = ui_message('run.the_operation_exceeded_its_time_limit')
                elif c.get('failure_kind') == 'IDLE_TIMEOUT':
                    message = ui_message('run.no_activity_was_detected_within_the_time_limit')
            lines = [ui_message('run.analysis_interrupted') if interrupted else '[FAIL] ' + label]
            if self.verbose:
                lines += [ui_message('run.code', code=s(c['code']))]
            if not interrupted or self.verbose:
                lines += ['', ui_message('run.reason'), '  ' + s(message)]
            fields = [('node_path', ui_message('run.path') if c.get('node_path') == '.' else ui_message('run.submodule'))]
            if self.verbose:
                fields = [('snapshot', ui_message('run.snapshot')), *fields, ('required_commit', ui_message('run.required_commit'))]
            for key, title in fields:
                if c.get(key):
                    lines += ['', title + ':', *('  ' + s(line) for line in c[key].split('\n'))]
            hint = c.get('hint')
            if not self.verbose and c['code'] in messages:
                hint = ui_message('run.use_verbose_for_details')
            elif not self.verbose and c['code'] == 'INTERNAL_ERROR':
                hint = ui_message('run.use_verbose_for_details_and_report_this_error')
            if hint:
                lines += ['', ui_message('run.next_step'), *('  ' + s(line) for line in hint.split('\n'))]
            if c.get('analysis_started') is False:
                lines += ['', ui_message('run.analysis_has_not_started')]
                if self.verbose and c.get('switches_performed') is False:
                    lines[-1] += ui_message('run.no_checkout_switches_were_performed')
            if self.verbose and c.get('details'):
                lines += ['', ui_message('run.diagnostic_details'), '  ' + s(json.dumps(c['details'], ensure_ascii=False))]
            if existing_file(self.log_path):
                lines += ['', ui_message('run.error_details'), '  ' + s(self.log_path)]
            lines += self.metric_lines(c.get('metrics'), compact=True)
            return lines
        return []

    def finish(self, result, manifest, *, check_only, config_path, run_dir=None, trust=False):
        self.stop_progress()
        if self.finished:
            return
        self.finished = True
        metrics = result.get('metrics')
        elapsed = metrics['duration_seconds'] if metrics else self.clock() - self.started
        self.emit('run_completed', status=result['status'], exit_code=result['exit_code'], elapsed_seconds=elapsed,
                  **({'metrics': metrics} if metrics else {}))
        if self.mode == 'json':
            self.write(self.stdout, json.dumps(result, ensure_ascii=True))
            return
        s = self.display
        status = lambda value: self.status(value, self.stdout)
        meaning = (ui_message('run.analysis_interrupted') if result['exit_code'] == 130 else
                   ui_message('run.local_setup_checked_analysis_has_not_started') if result['status'] == 'PREFLIGHT_OK' else
                   ui_message('run.analysis_complete_reports_may_still_contain_errors') if result['status'] == 'COMPLETE' else
                   ui_message(
                       'run.analysis_complete_with_citation_warnings_check_affected_claims',
                   ) if result['status'] == 'COMPLETE_WITH_WARNINGS' else
                   ui_message('run.analysis_may_be_incomplete_see_available_results_and') if result['status'] == 'PARTIAL' else
                   ui_message('run.analysis_failed'))
        lines = [meaning, ui_message('run.elapsed.finish', value=duration(elapsed))]
        if not check_only:
            from src.reports.review import review_summary
            for entry in manifest.get('branches') or [manifest]:
                source = (ui_message('run.branch', branch=s(entry['branch']))) if entry.get('branch') else ''
                lines += [source + review_summary(entry, review_enabled=manifest.get('review_enabled', True))]
        if metrics:
            if not metrics['attempts']:
                lines += [ui_message('run.no_model_calls_were_made')]
            else:
                lines += ['', ui_message('run.stage_status_elapsed_attempts_tokens_agent_estimated_cost')]
                for row in metrics['stages']:
                    title = self.stage_source(row)
                    title = (title + ' / ' if title else '') + row['stage']
                    if row.get('revision_id'):
                        title += ' #' + row['revision_id']
                    lines += [s(title) + ' | ' + s(row['status']) + ' | ' + duration(row['duration_seconds']) +
                              ' | ' + str(row['attempts']) + ' | ' + metric_value(row['usage'], 'total_tokens') +
                              ' | ' + metric_value(row['usage'], 'cost_usd') + ' | ' + self.metric_models(row)]
                lines += ['', ui_message('run.total_usage'), *self.metric_lines(metrics)]
        if result['status'] == 'PREFLIGHT_OK':
            command = ['python3', 'explain.py', '--config', str(config_path)]
            if trust:
                command.append('--trust-repository')
            lines += ['', ui_message('run.ai_service_access_and_model_availability_were_not'), '',
                      ui_message('run.start_analysis'), '  ' + s(shlex.join(command))]
        elif not check_only:
            for branch in manifest.get('branches', []):
                partial_material = manifest.get('result_policy') == 'compromise' and branch.get('study_usable')
                warned = any((branch.get(stage) or {}).get('program_checks', {}).get('citation_warnings')
                             for stage in ('study', 'review'))
                lines += [ui_message(
                    'run.branch.finish', branch=s(branch['branch']),
                    value=status(
                        ui_message('run.complete_with_warnings') if warned and branch.get('processing_completed') else
                        ui_message('run.complete') if branch.get('workflow_satisfied', branch.get('accepted')) else
                        ui_message('run.may_be_incomplete') if partial_material else
                        ui_message('run.failed') if branch['errors'] else ui_message('run.may_be_incomplete')))]
            analyzed = {branch['branch'] for branch in manifest.get('branches', [])}
            for branch in manifest.get('pins', {}):
                if branch not in analyzed:
                    lines += [ui_message('run.branch.finish', branch=s(branch), value=status(ui_message('run.not_started')))]
        paths = [(ui_message('tui.manifest'), Path(result['manifest']))] if self.verbose and result['manifest'] else []
        if self.verbose and self.log_path:
            paths.append((ui_message('tui.technical_log'), self.log_path))
        if run_dir and not check_only:
            if manifest.get('final_report'):
                paths.append((ui_message('tui.final_report') if manifest.get('has_usable_material') else ui_message('run.error_summary'),
                              Path(manifest['final_report'])))
            for branch in manifest.get('branches', []):
                for name in ('ARCHITECTURE.md', 'ARCHITECTURE_REVIEW.md'):
                    stage = 'study' if name == 'ARCHITECTURE.md' else 'review'
                    if branch.get(stage + '_invocation', {}).get('publication_complete'):
                        paths.append((ui_message(
                            'run.report.finish',
                            branch=s(branch['branch']),
                        ), run_dir / branch['directory'] / name))
            for relative in ('ARCHITECTURE.md', 'ARCHITECTURE_REVIEW.md', 'comparison/BRANCH_COMPARISON.md'):
                stage = {'ARCHITECTURE.md': 'study', 'ARCHITECTURE_REVIEW.md': 'review'}.get(relative, 'comparison')
                if manifest.get(stage + '_invocation', {}).get('publication_complete'):
                    paths.append((ui_message('run.report.finish.label'), run_dir / relative))
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
