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


class Reporter(NullReporter):
    def __init__(self, *, mode='text', verbose=False, progress=True, stdout=None, stderr=None,
                 clock=time.monotonic, progress_interval=30.0):
        self.mode, self.verbose, self.progress = mode, verbose, progress
        self.stdout = sys.stdout if stdout is None else stdout
        self.stderr = sys.stderr if stderr is None else stderr
        self.colors = {}
        for stream in (self.stdout, self.stderr):
            try:
                self.colors[id(stream)] = bool(stream.isatty()) and not os.environ.get('NO_COLOR') and os.environ.get('TERM') != 'dumb'
            except Exception:
                self.colors[id(stream)] = False
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

    def write(self, stream, text):
        if id(stream) in self.failed_streams:
            return
        if stream is self.stderr and self.colors.get(id(stream)):
            text = re.sub(r'^\[(?:WARN|RUN|OK|FAIL|SKIP)\](?= |$)',
                          lambda match: LABEL_COLORS[match[0]] + match[0] + '\x1b[0m',
                          text, flags=re.MULTILINE)
        try:
            stream.write(text + '\n')
            stream.flush()
        except (OSError, UnicodeError, ValueError):
            self.failed_streams.add(id(stream))

    def attach_log(self, run_dir: Path):
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
        if self.handler:
            self.logger.removeHandler(self.handler)
            self.handler.close()
            self.handler = None
        if not self.log_failed:
            self.log_failed = True
            self.write(self.stderr, '[WARN] Technical log is unavailable or incomplete; execution and cleanup continue.')

    def log(self, event):
        if self.handler:
            try:
                self.logger.log(event.level, event.event, extra={'audit_event': event})
            except (OSError, ValueError):
                self.disable_log()
        elif not self.log_failed:
            self.early.append(event)

    def emit(self, event, level=logging.INFO, *, exception=None, **context):
        if event in ('root_warning', 'description_missing', 'stop_requested'):
            level = logging.WARNING
        item = Event(event, level, dt.datetime.now(dt.timezone.utc).isoformat(), self.run_id,
                     self.clean(context), exception)
        self.log(item)
        lines = self.render(item)
        if lines:
            self.write(self.stderr, '\n'.join(lines))
        if self.verbose and event not in ('process_waiting', 'error'):
            self.write(self.stderr, '[RUN] Detail: ' + self.display(event) + ' ' +
                       self.display(json.dumps(item.context, ensure_ascii=False)))

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
            return ['[RUN] ' + stage]
        if name == 'process_waiting':
            if not self.progress:
                return []
            last = ('No CLI output received yet' if c['last_output_seconds'] is None else
                    'Last CLI output: ' + duration(c['last_output_seconds']) + ' ago')
            return ['[RUN] ' + stage, '      Elapsed: ' + duration(c['elapsed_seconds']) +
                    ' | ' + last]
        if name == 'stage_completed':
            status = c['status']
            label = '[OK] ' if status == 'COMPLETE' else '[FAIL] ' if status == 'FAILED' else '[WARN] '
            return [label + stage + ' — ' + self.status(status, self.stderr) +
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
        lines = [status('PREFLIGHT PASSED' if result['status'] == 'PREFLIGHT_OK' else result['status']),
                 'Elapsed: ' + duration(elapsed)]
        if result['status'] == 'PREFLIGHT_OK':
            command = ['python3', 'explain.py', '--config', str(config_path)]
            if trust:
                command.append('--trust-repository')
            lines += ['', 'No model calls were made. Source analysis has not started.', '',
                      'Start analysis:', '  ' + s(shlex.join(command))]
        elif not check_only:
            for branch in manifest.get('branches', []):
                lines += ['Branch ' + s(branch['branch']) + ': ' +
                          status('COMPLETE' if branch.get('accepted') else 'FAILED' if branch['errors'] else 'PARTIAL')]
            analyzed = {branch['branch'] for branch in manifest.get('branches', [])}
            for branch in manifest.get('pins', {}):
                if branch not in analyzed:
                    lines += ['Branch ' + s(branch) + ': ' + status('not started')]
            if manifest.get('mode') == 'folder':
                lines += ['Source result: ' + status('COMPLETE' if manifest.get('accepted') else result['status']),
                          'Comparison: ' + status('not applicable') + ' (folder mode)',
                          'Restoration: ' + status('not applicable') + ' (folder mode)']
            else:
                if 'comparison' not in manifest and len(manifest.get('pins', {})) == 1:
                    lines += ['Comparison: ' + status('not applicable') + ' (single branch)']
                else:
                    lines += ['Comparison: ' + status(manifest.get('comparison', {}).get('completion_status', 'not completed'))]
                restoration = manifest.get('restoration')
                lines += ['Restoration: ' + status('verified' if restoration and restoration['restored'] else
                          'FAILED' if restoration else 'not performed')]
        paths = [('Manifest', Path(result['manifest']))] if result['manifest'] else []
        if self.log_path:
            paths.append(('Technical log', self.log_path))
        if run_dir and not check_only:
            for branch in manifest.get('branches', []):
                for name in ('ARCHITECTURE.md', 'ARCHITECTURE_REVIEW.md'):
                    paths.append(('Report (' + s(branch['branch']) + ')', run_dir / branch['directory'] / name))
            for relative in ('ARCHITECTURE.md', 'ARCHITECTURE_REVIEW.md', 'comparison/BRANCH_COMPARISON.md'):
                paths.append(('Report', run_dir / relative))
        for title, path in paths:
            if existing_file(path):
                lines += ['', title + ':', '  ' + s(path)]
        self.write(self.stdout, '\n'.join(lines))

    def close(self):
        if self.handler:
            self.logger.removeHandler(self.handler)
            self.handler.close()
            self.handler = None
        self.early.clear()
