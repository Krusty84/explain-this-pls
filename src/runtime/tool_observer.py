# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Conservative completed-tool accounting shared by live execution and replay.

Only transport-owned envelopes are removed. Tool arguments and output retain
all source text, numbers and whitespace. Source delivery and stop evidence use
separate histories; a prompt fragment can never initiate a repeat streak.
"""
from __future__ import annotations

import codecs
from collections import Counter, deque
from dataclasses import dataclass, field
import hashlib
import json
import time

from src.analysis.evidence import lines, source_catalog

REPEAT_CALLS = 50
REPEAT_SECONDS = 120


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


@dataclass
class CompletedTool:
    call_id: str | None
    tool: str
    arguments: object
    status: str
    result: object
    complete: bool
    session_id: str = ''
    source_lines: list = field(default_factory=list)
    limitations: list = field(default_factory=list)


class ToolObserver:
    def __init__(self, backend, *, enabled=True, clock=time.monotonic, context=None,
                 attempt_id='', session_id='', event_sink=None):
        self.backend, self.enabled, self.clock = backend, enabled, clock
        self.context = context or {}
        self.attempt_id, self.session_id = attempt_id, session_id
        self.event_sink = event_sink
        self.started = clock()
        self.decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.buffer = ''
        self.pending, self.seen, self.requests = {}, set(), set()
        self.exact, self.delivered, self.tool_lines = set(), {}, {}
        self.conflicted = set()
        self.counts, self.by_kind = Counter(), Counter()
        self.streak, self.streak_started = 0, None
        self.max_streak, self.max_duration = 0, 0.0
        self.last_new, self.last_completed = None, None
        self.triggered, self.diagnostic, self.would_stop = False, None, None
        self.recent = deque(maxlen=10)
        self.limitations = set()
        self.finished = False
        self.terminal = False
        if backend == 'codex':
            self.limitations.add('Codex shell source coordinates and model request events are unavailable.')
        else:
            self.limitations.add('Unsupported backend event format.')
        try:
            self.sources = source_catalog(self.context)
        except KeyError:
            self.sources = []
        self.snapshot_identity = digest([s['identity'] for s in self.sources])

    def _reset(self):
        self.streak, self.streak_started = 0, None

    def _invalid(self, reason):
        self.limitations.add(reason)
        self.counts['invalid_events'] += 1
        self._reset()
        self._record(kind='invalid', reason=reason)

    def _record(self, **values):
        if self.event_sink:
            self.event_sink({'format': 'tool-observer', 'attempt_id': self.attempt_id,
                             'observed_at_seconds': self.clock() - self.started, **values})

    def _request(self, identity):
        self.requests.add(identity)
        self.terminal = False
        self._record(kind='model_request', identity=list(identity))

    def _terminal(self):
        self.terminal = True
        self._record(kind='terminal')

    def seed_prompt(self, fragments):
        self._record(kind='prompt', fragments=fragments)
        for fragment in fragments:
            try:
                sid, path, start = fragment['source_id'], fragment['path'], fragment['start_line']
                for offset, line in enumerate(lines(fragment['text'])):
                    self._delivery((sid, path, start + offset, line.removesuffix('\n')), prompt=True)
            except (KeyError, TypeError):
                self.limitations.add('Invalid prepared fragment delivery metadata.')

    def _delivery(self, row, *, prompt=False):
        sid, path, number, text = row
        key = (self.snapshot_identity, sid, path, number)
        fingerprint = digest(text)
        previous = self.delivered.get(key)
        if previous == fingerprint:
            self.counts['delivery_repeated_lines'] += 1
        else:
            self.counts['delivery_unique_lines'] += 1
            if previous is not None:
                self.counts['delivery_conflicting_lines'] += 1
            self.delivered[key] = fingerprint
        if prompt:
            self.counts['prompt_lines'] += 1

    def feed(self, block):
        if self.finished or self.triggered:
            return
        try:
            self.buffer += self.decoder.decode(block) if isinstance(block, bytes) else block
        except UnicodeDecodeError:
            self._invalid('Invalid UTF-8 in tool event stream.')
            self.decoder.reset()
            self.buffer = ''
            return
        while '\n' in self.buffer and not self.triggered:
            line, self.buffer = self.buffer.split('\n', 1)
            self._line(line)

    def _line(self, line):
        if not line.strip():
            return
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError()
            self.event(event)
        except (ValueError, TypeError):
            self._invalid('Malformed or unsupported JSON event.')

    def finish(self):
        if self.finished:
            return self.snapshot()
        if not self.triggered:
            try:
                self.buffer += self.decoder.decode(b'', final=True)
            except UnicodeDecodeError:
                self._invalid('Incomplete UTF-8 in tool event stream.')
            if self.buffer.strip():
                self._line(self.buffer)
        self.buffer = ''
        self.finished = True
        if self.pending:
            self.limitations.add('Tool calls without completed results.')
        if not self.terminal and not self.triggered:
            self.limitations.add('No terminal backend event observed.')
        return self.snapshot()

    def event(self, event):
        """Accept one decoded native event; also used by timestamp-aware replay."""
        if self.triggered:
            return
        if self.backend == 'codex':
            completed = self._codex(event)
        else:
            completed = []
        for call in completed:
            if self.triggered:
                break
            self.observe(call)

    def _codex(self, event):
        kind = event.get('type')
        if kind == 'thread.started':
            self.session_id = event.get('thread_id', self.session_id)
        if kind in ('turn.completed', 'turn.failed'):
            self._terminal()
        item = event.get('item')
        if kind != 'item.completed' or not isinstance(item, dict):
            return []
        tool = item.get('type')
        if tool == 'command_execution':
            status = ('success' if type(item.get('exit_code')) is int and item['exit_code'] == 0 else
                      'error' if type(item.get('exit_code')) is int else 'unknown')
            output = item.get('aggregated_output')
            complete = (isinstance(output, str) and item.get('status') == 'completed'
                        and isinstance(item.get('command'), str) and not item.get('truncated', False))
            return [CompletedTool(item.get('id'), tool, {'command': item.get('command')},
                                  status, output, complete, self.session_id)]
        if tool == 'mcp_tool_call':
            return [CompletedTool(item.get('id'), str(item.get('server')) + '/' + str(item.get('tool')),
                                  item.get('arguments'), 'error' if item.get('error') else 'success',
                                  item.get('result'), item.get('result') is not None, self.session_id)]
        if tool not in ('agent_message', 'reasoning', 'plan', 'todo_list'):
            return [CompletedTool(item.get('id'), str(tool or 'unknown'), None, 'unknown', item, False,
                                  self.session_id, limitations=['Unsupported completed Codex item type.'])]
        return []

    def observe(self, call, *, observed_at=None):
        """Accept a completed normalized result. Timestamp is elapsed monotonic time."""
        if self.triggered:
            return
        now = self.clock() - self.started if observed_at is None else observed_at
        self._record(observed_at_seconds=now, call=call.__dict__)
        identity = (self.attempt_id, call.session_id, call.call_id)
        if call.call_id and identity in self.seen:
            self.counts['duplicate_events'] += 1
            return
        if call.call_id:
            self.seen.add(identity)
            self.pending.pop((call.session_id, call.call_id), None)
        else:
            call.complete = False
            call.limitations.append('Native tool call identity unavailable.')
        if self.last_completed is not None and now < self.last_completed:
            call.complete = False
            call.limitations.append('Nonmonotonic event observation time.')
        self.last_completed = now
        self.counts['calls'] += 1
        self.by_kind[call.tool] += 1
        status = call.status if call.status in ('success', 'error') else 'unknown'
        self.counts[status] += 1
        self.limitations.update(call.limitations)
        self.counts['complete_results' if call.complete else 'incomplete_results'] += 1
        signature = digest([call.tool, call.arguments, call.result])
        exact = signature in self.exact
        new, repeated, conflicting = 0, 0, 0
        for row in call.source_lines:
            self._delivery(row)
            sid, path, number, text = row
            key, fingerprint = (self.snapshot_identity, sid, path, number), digest(text)
            previous = self.tool_lines.get(key)
            if previous is not None and previous != fingerprint:
                self.conflicted.add(key)
            if key in self.conflicted:
                conflicting += 1
            if previous == fingerprint:
                repeated += 1
            else:
                new += 1
                self.tool_lines[key] = fingerprint
        self.counts['tool_unique_lines'] += new
        self.counts['tool_repeated_lines'] += repeated
        self.counts['conflicting_lines'] += conflicting
        if call.source_lines:
            self.counts['reads_with_coordinates'] += 1
        elif call.tool.lower() in ('read', 'command_execution'):
            self.counts['reads_without_coordinates'] += 1
        if new and repeated:
            self.counts['mixed_reads'] += 1
        valid = status == 'success' and call.complete and not conflicting
        confirmed = valid and (not new and bool(repeated) if call.source_lines else exact)
        if valid:
            self.exact.add(signature)
            if exact:
                self.counts['exact_repeats'] += 1
            if call.source_lines and repeated and not new:
                self.counts['fully_repeated_reads'] += 1
        if confirmed:
            if self.streak == 0:
                self.streak_started = now
            self.streak += 1
            duration = now - self.streak_started
            self.max_streak = max(self.max_streak, self.streak)
            self.max_duration = max(self.max_duration, duration)
        else:
            self._reset()
            duration = 0.0
            if valid:
                self.last_new = now
        recent = {'call': self.counts['calls'], 'call_id': call.call_id, 'tool': call.tool,
                  'observed_at_seconds': round(now, 6), 'status': status, 'complete': call.complete,
                  'confirmed_repeat': confirmed, 'new_lines': new, 'repeated_lines': repeated,
                  'result_sha256': signature}
        self.recent.append(recent)
        if confirmed and self.streak >= REPEAT_CALLS and duration >= REPEAT_SECONDS:
            if self.would_stop is None:
                self.would_stop = {'kind': 'NO_PROGRESS', 'call': self.counts['calls'],
                                   'observed_at_seconds': round(now, 6), 'consecutive_confirmed_repeats': self.streak,
                                   'repeat_streak_duration_seconds': round(duration, 6),
                                   'reason': 'Repeated completed tool results without new observable material.',
                                   'recent_calls': list(self.recent)}
            if self.enabled:
                self.triggered, self.diagnostic = True, self.would_stop

    def snapshot(self):
        line_coverage = ('unavailable' if not self.counts['reads_with_coordinates'] else
                         'partial' if self.counts['reads_without_coordinates'] else 'complete')
        complete = self.terminal and not self.pending and not self.limitations and not self.counts['incomplete_results']
        request_available = self.backend != 'codex' and bool(self.requests)
        delivery_available = self.counts['prompt_lines'] or self.counts['reads_with_coordinates']
        result = {'backend': self.backend, 'calls': self.counts['calls'],
                  'calls_by_tool': dict(sorted(self.by_kind.items())),
                  'successful_results': self.counts['success'], 'error_results': self.counts['error'],
                  'unknown_results': self.counts['unknown'], 'incomplete_results': self.counts['incomplete_results'],
                  'duplicate_events': self.counts['duplicate_events'], 'invalid_events': self.counts['invalid_events'],
                  'unique_lines': self.counts['tool_unique_lines'] if line_coverage != 'unavailable' else None,
                  'repeated_lines': self.counts['tool_repeated_lines'] if line_coverage != 'unavailable' else None,
                  'line_coverage': line_coverage,
                  'line_coverage_reason': 'Only explicit native source line coordinates are counted.',
                  'mixed_reads': self.counts['mixed_reads'] if line_coverage != 'unavailable' else None,
                  'conflicting_lines': self.counts['conflicting_lines'] if line_coverage != 'unavailable' else None,
                  'fully_repeated_reads': self.counts['fully_repeated_reads'] if line_coverage != 'unavailable' else None,
                  'exact_repeats': self.counts['exact_repeats'],
                  'consecutive_confirmed_repeats': self.streak,
                  'max_repeat_streak': self.max_streak, 'max_repeat_duration_seconds': round(self.max_duration, 6),
                  'last_new_material_seconds': self.last_new,
                  'model_requests': len(self.requests) if request_available else None,
                  'model_requests_coverage': 'partial' if request_available else 'unavailable',
                  'model_requests_reason': 'Codex model request events are unavailable.',
                  'delivery': {'unique_lines': self.counts['delivery_unique_lines'] if delivery_available else None,
                               'repeated_lines': self.counts['delivery_repeated_lines'] if delivery_available else None,
                               'conflicting_lines': self.counts['delivery_conflicting_lines'] if delivery_available else None,
                               'prompt_lines': self.counts['prompt_lines'],
                               'coverage': 'partial' if delivery_available and line_coverage == 'unavailable' else line_coverage},
                  'guard': {'enabled': self.enabled, 'triggered': self.triggered,
                            'state': 'NO_PROGRESS' if self.triggered else 'observing' if self.enabled else 'disabled',
                            'diagnostic': self.diagnostic, 'would_stop': self.would_stop},
                  'observation': {'coverage': 'complete' if complete else 'partial' if self.counts['calls'] else 'unavailable',
                                  'limitations': sorted(self.limitations)}}
        result['metric_coverage'] = {
            'tool_calls': {'status': result['observation']['coverage'],
                           'reason': 'Counts contain only observed completed native calls.'},
            'source_lines': {'status': line_coverage, 'reason': result['line_coverage_reason']},
            'model_requests': {'status': result['model_requests_coverage'], 'reason': result['model_requests_reason']},
        }
        return result
