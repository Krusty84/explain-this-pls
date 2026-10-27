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
from pathlib import Path
import time

from src.messages import message as ui_message
from src.analysis.evidence import lines, source_catalog, valid_path

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
    mcp: dict | None = None


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
            self.limitations.add(ui_message('diagnostic.tool_observer.codex_shell_source_coordinates_and_model_request_events'))
        else:
            self.limitations.add(ui_message('diagnostic.tool_observer.unsupported_backend_event_format'))
        try:
            self.sources = source_catalog(self.context)
        except KeyError:
            self.sources = []
        self.snapshot_identity = digest([s['identity'] for s in self.sources])
        self.mcp_access = self.context.get('_mcp_source_access', {})
        self.mcp_counts = Counter()
        self.mcp_errors = Counter()
        self.mcp_receipts = set()

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
                self.limitations.add(ui_message('diagnostic.tool_observer.invalid_prepared_fragment_delivery_metadata'))

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
            self._invalid(ui_message('diagnostic.tool_observer.invalid_utf_in_tool_event_stream'))
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
            self._invalid(ui_message('diagnostic.tool_observer.malformed_or_unsupported_json_event'))

    def finish(self):
        if self.finished:
            return self.snapshot()
        if not self.triggered:
            try:
                self.buffer += self.decoder.decode(b'', final=True)
            except UnicodeDecodeError:
                self._invalid(ui_message('diagnostic.tool_observer.incomplete_utf_in_tool_event_stream'))
            if self.buffer.strip():
                self._line(self.buffer)
        self.buffer = ''
        self.finished = True
        if self.pending:
            self.limitations.add(ui_message('diagnostic.tool_observer.tool_calls_without_completed_results'))
        if not self.terminal and not self.triggered:
            self.limitations.add(ui_message('diagnostic.tool_observer.no_terminal_backend_event_observed'))
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
                      'error' if type(item.get('exit_code')) is int else ui_message('cli.unknown'))
            output = item.get('aggregated_output')
            complete = (isinstance(output, str) and item.get('status') == 'completed'
                        and isinstance(item.get('command'), str) and not item.get('truncated', False))
            return [CompletedTool(item.get('id'), tool, {'command': item.get('command')},
                                  status, output, complete, self.session_id)]
        if tool == 'mcp_tool_call':
            if (item.get('server') == self.mcp_access.get('server') and
                    item.get('tool') in ('search_symbols', 'read_source')):
                return [self._source_mcp(item)]
            return [CompletedTool(item.get('id'), str(item.get('server')) + '/' + str(item.get('tool')),
                                  item.get('arguments'), 'error' if item.get('error') else 'success',
                                  item.get('result'), item.get('result') is not None, self.session_id)]
        if tool not in ('agent_message', 'reasoning', 'plan', 'todo_list'):
            return [CompletedTool(item.get('id'), str(tool or ui_message('cli.unknown')), None, ui_message('cli.unknown'), item, False,
                                  self.session_id, limitations=[ui_message(
                                      'diagnostic.tool_observer.unsupported_completed_codex_item_type',
                                  )])]
        return []

    def _source_mcp(self, item):
        """Count source text only when a private SERVED receipt matches its bytes."""
        tool, arguments, result = item['tool'], item.get('arguments'), item.get('result')
        observation = {'tool': tool, 'request': arguments, 'delivery_state': 'UNVERIFIED',
                       'inspection_state': 'INSPECTED_UNVERIFIED', 'text_utf8_bytes': 0,
                       'returned': None, 'error': None}
        call = CompletedTool(item.get('id'), item['server'] + '/' + tool, arguments,
                             'error' if item.get('error') or item.get('status') == 'failed' else 'success',
                             result, False, self.session_id, mcp=observation)
        try:
            if item.get('error') or (call.status == 'error' and result is None):
                observation['error'] = 'TOOL_CALL_FAILED'
                raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_call_failed_before_a_verified_result'))
            if (not isinstance(item.get('id'), str) or not item['id'] or
                    not isinstance(arguments, dict) or not isinstance(result, dict) or
                    item.get('status') not in ('completed', 'failed') or item.get('truncated')):
                raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_result_is_incomplete_or_has_an'))
            content = result.get('content')
            if (not isinstance(content, list) or len(content) != 1 or
                    not isinstance(content[0], dict) or content[0].get('type') != 'text' or
                    not isinstance(content[0].get('text'), str)):
                raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_result_text_was_not_observed'))
            payload = json.loads(content[0]['text'])
            if not isinstance(payload, dict):
                raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_result_text_is_not_a_json'))
            for key in ('structuredContent', 'structured_content'):
                if result.get(key) is not None and result[key] != payload:
                    raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_text_differs_from_its_structured_result'))
            meta = payload.get('_meta', {})
            if (not isinstance(meta, dict) or not isinstance(meta.get('request_id'), str) or
                    not meta['request_id'] or meta.get('manifest_sha256') != self.mcp_access.get('manifest_sha256') or
                    meta.get('attempt_id') != self.mcp_access.get('attempt_id') or
                    meta.get('state') != 'SERVED' or meta.get('inspection') != 'INSPECTED_UNVERIFIED'):
                raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_result_identity_does_not_match_this'))
            receipt_found = False
            for raw in Path(self.mcp_access['receipts_path']).read_text(encoding='utf-8').splitlines():
                try:
                    receipt = json.loads(raw)
                except ValueError:
                    continue
                if (isinstance(receipt, dict) and receipt.get('state') == 'SERVED' and
                        receipt.get('inspection') == 'INSPECTED_UNVERIFIED' and
                        receipt.get('attempt_id') == meta['attempt_id'] and
                        receipt.get('request_id') == meta['request_id'] and receipt.get('tool') == tool and
                        receipt.get('arguments') == arguments and receipt.get('result_sha256') == digest(payload)):
                    receipt_found = True
                    break
            if not receipt_found:
                raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_result_has_no_matching_private_served'))
            observation.update(request_id=meta['request_id'], manifest_sha256=meta['manifest_sha256'])
            if isinstance(payload.get('error'), dict):
                observation['error'] = str(payload['error'].get('code', 'TOOL_ERROR'))
                observation['delivery_state'] = 'DELIVERED_OBSERVED'
                call.status, call.complete = 'error', True
            elif result.get('isError') or result.get('is_error'):
                raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_result_has_an_unexplained_protocol_error'))
            elif tool == 'read_source':
                self._source_mcp_lines(call, payload)
                observation['delivery_state'] = 'DELIVERED_OBSERVED'
            else:
                call.complete = True
                observation['delivery_state'] = 'DELIVERED_OBSERVED'
            self.mcp_receipts.add((meta['request_id'], digest(payload)))
        except (KeyError, TypeError, OSError, UnicodeError, ValueError) as exc:
            call.source_lines = []
            call.complete = False
            call.limitations.append(str(exc) if isinstance(exc, ValueError) else
                                    ui_message('diagnostic.tool_observer.source_mcp_result_or_its_private_receipt_could'))
        return call

    def _source_mcp_lines(self, call, payload):
        arguments = call.arguments
        sid, path = arguments.get('source_id'), arguments.get('path')
        source = next((source for source in self.sources if source['id'] == sid), None)
        if source is None or not valid_path(path):
            raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_returned_an_unknown_source_locator'))
        relative = path if source['root'] == '.' else source['root'] + '/' + path
        owners = [source for source in self.sources if source['root'] == '.' or relative.startswith(source['root'] + '/')]
        inventory = {entry['path']: entry.get('sha256') for entry in self.context.get('_inventory', {}).get('entries', [])
                     if entry.get('type') == 'file'}
        if (max(owners, key=lambda source: len(source['root']))['id'] != sid or
                relative not in inventory or not inventory[relative] or payload.get('sha256') != inventory[relative] or
                payload.get('source_id') != sid or payload.get('path') != path):
            raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_result_differs_from_the_pinned_inventory'))
        start, end = payload.get('start_line'), payload.get('end_line')
        requested_start, requested_end = arguments.get('start_line'), arguments.get('end_line')
        total, text = payload.get('total_lines'), payload.get('text')
        complete, more, following = payload.get('complete'), payload.get('has_more'), payload.get('next_start_line')
        if (any(type(number) is not int for number in (start, end, requested_start, requested_end, total)) or
                requested_start < 1 or requested_end < requested_start or total < 0 or
                not isinstance(text, str) or '\r\n' in text or type(complete) is not bool or type(more) is not bool or
                not isinstance(payload.get('encoding'), str)):
            raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_returned_invalid_source_coordinates_or_text'))
        rows = lines(text)
        empty = total == 0 and start == 1 and end == 0 and not rows
        if (start != requested_start or (not empty and (end < start or end > min(total, requested_end))) or
                len(rows) != max(0, end - start + 1) or more == complete or
                (more and (type(following) is not int or following != end + 1)) or
                (not more and following is not None) or
                (complete and not empty and end != requested_end)):
            raise ValueError(ui_message('diagnostic.tool_observer.source_mcp_source_text_does_not_match_its'))
        call.source_lines = [(sid, path, start + offset, line.removesuffix('\n'))
                             for offset, line in enumerate(rows)]
        call.complete = complete
        call.mcp.update(text_utf8_bytes=len(text.encode('utf-8')),
                        returned={'source_id': sid, 'path': path, 'start_line': start, 'end_line': end,
                                  'complete': complete, 'has_more': more, 'next_start_line': following})

    def observe(self, call, *, observed_at=None):
        """Accept a completed normalized result. Timestamp is elapsed monotonic time."""
        if self.triggered:
            return
        now = self.clock() - self.started if observed_at is None else observed_at
        if call.mcp is not None:
            new_rows, repeated_rows = [], []
            for row in call.source_lines:
                sid, path, number, text = row
                previous = self.tool_lines.get((self.snapshot_identity, sid, path, number))
                (repeated_rows if previous == digest(text) else new_rows).append(row)
            call.mcp.update(new_ranges=self._source_ranges(new_rows),
                            repeated_ranges=self._source_ranges(repeated_rows))
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
            call.limitations.append(ui_message('diagnostic.tool_observer.native_tool_call_identity_unavailable'))
        if self.last_completed is not None and now < self.last_completed:
            call.complete = False
            call.limitations.append(ui_message('diagnostic.tool_observer.nonmonotonic_event_observation_time'))
        self.last_completed = now
        self.counts['calls'] += 1
        self.by_kind[call.tool] += 1
        status = call.status if call.status in ('success', 'error') else ui_message('cli.unknown')
        self.counts[status] += 1
        self.limitations.update(call.limitations)
        self.counts['complete_results' if call.complete else 'incomplete_results'] += 1
        if call.mcp is not None:
            observation = call.mcp
            self.mcp_counts['calls'] += 1
            self.mcp_counts[observation['tool'] + '_calls'] += 1
            self.mcp_counts['delivered_observed'] += observation['delivery_state'] == 'DELIVERED_OBSERVED'
            self.mcp_counts['unverified_results'] += observation['delivery_state'] != 'DELIVERED_OBSERVED'
            self.mcp_counts['incomplete_results'] += not call.complete
            self.mcp_counts['text_utf8_bytes'] += observation['text_utf8_bytes']
            self.mcp_counts['unique_lines'] += sum(row['end_line'] - row['start_line'] + 1 for row in observation['new_ranges'])
            self.mcp_counts['repeated_lines'] += sum(row['end_line'] - row['start_line'] + 1 for row in observation['repeated_ranges'])
            if observation.get('error'):
                self.mcp_errors[observation['error']] += 1
                self.mcp_counts['read_errors'] += observation['tool'] == 'read_source'
                self.mcp_counts['decoding_errors'] += observation['error'] in ('DECODE_ERROR', 'ENCODING_MISMATCH', 'BINARY_CONTENT')
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
        elif (call.tool.lower() in ('read', 'command_execution') or
              call.mcp is not None and call.mcp['tool'] == 'read_source' and call.mcp['returned'] is None):
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
                                   'reason': ui_message(
                                       'diagnostic.tool_observer.repeated_completed_tool_results_without_new_observable_material',
                                   ),
                                   'recent_calls': list(self.recent)}
            if self.enabled:
                self.triggered, self.diagnostic = True, self.would_stop

    @staticmethod
    def _source_ranges(rows):
        result = []
        for sid, path, number, _ in rows:
            if (result and result[-1]['source_id'] == sid and result[-1]['path'] == path and
                    result[-1]['end_line'] + 1 == number):
                result[-1]['end_line'] = number
            else:
                result.append({'source_id': sid, 'path': path, 'start_line': number, 'end_line': number})
        return result

    def snapshot(self):
        line_coverage = ('unavailable' if not self.counts['reads_with_coordinates'] else
                         ui_message('run.partial') if self.counts['reads_without_coordinates'] else 'complete')
        complete = self.terminal and not self.pending and not self.limitations and not self.counts['incomplete_results']
        request_available = self.backend != 'codex' and bool(self.requests)
        delivery_available = self.counts['prompt_lines'] or self.counts['reads_with_coordinates']
        result = {'backend': self.backend, 'calls': self.counts['calls'],
                  'calls_by_tool': dict(sorted(self.by_kind.items())),
                  'successful_results': self.counts['success'], 'error_results': self.counts['error'],
                  'unknown_results': self.counts[ui_message(
                      'cli.unknown',
                  )], 'incomplete_results': self.counts['incomplete_results'],
                  'duplicate_events': self.counts['duplicate_events'], 'invalid_events': self.counts['invalid_events'],
                  'unique_lines': self.counts['tool_unique_lines'] if line_coverage != 'unavailable' else None,
                  'repeated_lines': self.counts['tool_repeated_lines'] if line_coverage != 'unavailable' else None,
                  'line_coverage': line_coverage,
                  'line_coverage_reason': ui_message(
                      'diagnostic.tool_observer.only_explicit_source_coordinates_from_verified_completed_tool',
                  ),
                  'mixed_reads': self.counts['mixed_reads'] if line_coverage != 'unavailable' else None,
                  'conflicting_lines': self.counts['conflicting_lines'] if line_coverage != 'unavailable' else None,
                  'fully_repeated_reads': self.counts['fully_repeated_reads'] if line_coverage != 'unavailable' else None,
                  'exact_repeats': self.counts['exact_repeats'],
                  'consecutive_confirmed_repeats': self.streak,
                  'max_repeat_streak': self.max_streak, 'max_repeat_duration_seconds': round(self.max_duration, 6),
                  'last_new_material_seconds': self.last_new,
                  'model_requests': len(self.requests) if request_available else None,
                  'model_requests_coverage': ui_message('run.partial') if request_available else 'unavailable',
                  'model_requests_reason': ui_message('diagnostic.tool_observer.codex_model_request_events_are_unavailable'),
                  'delivery': {'unique_lines': self.counts['delivery_unique_lines'] if delivery_available else None,
                               'repeated_lines': self.counts['delivery_repeated_lines'] if delivery_available else None,
                               'conflicting_lines': self.counts['delivery_conflicting_lines'] if delivery_available else None,
                               'prompt_lines': self.counts['prompt_lines'],
                               'coverage': ui_message(
                                   'run.partial',
                               ) if delivery_available and line_coverage == 'unavailable' else line_coverage},
                  'guard': {'enabled': self.enabled, 'triggered': self.triggered,
                            'state': 'NO_PROGRESS' if self.triggered else 'observing' if self.enabled else 'disabled',
                            'diagnostic': self.diagnostic, 'would_stop': self.would_stop},
                  'observation': {'coverage': 'complete' if complete else ui_message(
                      'run.partial',
                  ) if self.counts['calls'] else 'unavailable',
                                  'limitations': sorted(self.limitations)}}
        result['metric_coverage'] = {
            'tool_calls': {'status': result['observation']['coverage'],
                           'reason': ui_message('diagnostic.tool_observer.counts_contain_only_observed_completed_native_calls')},
            'source_lines': {'status': line_coverage, 'reason': result['line_coverage_reason']},
            'model_requests': {'status': result['model_requests_coverage'], 'reason': result['model_requests_reason']},
        }
        if self.mcp_counts['calls']:
            result['mcp'] = {key: self.mcp_counts[key] for key in (
                'calls', 'search_symbols_calls', 'read_source_calls', 'delivered_observed', 'unverified_results',
                'read_errors', 'decoding_errors', 'incomplete_results', 'text_utf8_bytes', 'unique_lines', 'repeated_lines')}
            result['mcp'].update(errors=dict(sorted(self.mcp_errors.items())),
                                 delivery_state='DELIVERED_OBSERVED' if self.mcp_counts['delivered_observed'] else 'UNVERIFIED',
                                 inspection_state='INSPECTED_UNVERIFIED',
                                 served_state='SERVED', served_observation=ui_message(
                                     'diagnostic.tool_observer.private_server_receipts_not_model_inspection',
                                 ))
        return result
