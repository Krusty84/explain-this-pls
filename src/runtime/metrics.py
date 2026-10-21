# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Backend-reported usage and monotonic run accounting; no pricing or model calls."""
from collections import defaultdict
import math

FIELDS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens',
          'reasoning_tokens', 'total_tokens', 'cost_usd')


def number(value, *, tokens=False):
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    if tokens:
        return int(value) if value == int(value) else None
    return value


def add(*values):
    return sum(values) if all(value is not None for value in values) else None


def usage(values=None, *, source=None, complete=True, estimated=False):
    values = values or {}
    result = {key: number(values.get(key), tokens=key != 'cost_usd') for key in FIELDS}
    return result | {'coverage': {key: ('unavailable' if value is None else
                                      'complete' if complete else 'partial')
                                  for key, value in result.items()},
                     'total_tokens_estimated': estimated,
                     'cost_is_estimate': result['cost_usd'] is not None and source != 'not_sent',
                     'sources': [source] if source else []}


def sum_usage(items):
    items = list(items)
    result = usage({key: 0 for key in FIELDS}) if not items else usage()
    for key in FIELDS:
        known = [item[key] for item in items if item[key] is not None]
        if known:
            result[key] = number(sum(known), tokens=key != 'cost_usd')
            result['coverage'][key] = ('unavailable' if result[key] is None else
                                      'complete' if all(item['coverage'][key] == 'complete' for item in items) else 'partial')
    result['sources'] = sorted({source for item in items for source in item['sources']})
    result['total_tokens_estimated'] = any(item['total_tokens_estimated'] for item in items)
    result['cost_is_estimate'] = any(item['cost_is_estimate'] for item in items)
    return result


def model_entry(backend, requested, actual, measured):
    return {'backend': backend, 'model_requested': requested, 'model_actual': actual, 'usage': measured}


def measurement(backend, requested=None, actual=None, measured=None):
    measured = measured if measured is not None else usage()
    return {'usage': measured, 'by_model': [model_entry(backend, requested, actual, measured)]}


def codex_usage(events, requested=None, *, malformed=False):
    turns = {}
    turn = 0
    completed = False
    for event in events:
        if type(event) is not dict:
            malformed = True
            continue
        kind = event.get('type')
        if kind == 'turn.started':
            turn += 1
            completed = False
        elif kind == 'turn.completed':
            raw = event.get('usage')
            if type(raw) is not dict:
                raw = {}
            values = {'input_tokens': raw.get('input_tokens'), 'output_tokens': raw.get('output_tokens'),
                      'cache_read_tokens': raw.get('cached_input_tokens'),
                      'reasoning_tokens': raw.get('reasoning_output_tokens')}
            values['total_tokens'] = add(number(values['input_tokens'], tokens=True),
                                         number(values['output_tokens'], tokens=True))
            turns[turn] = usage(values, source='codex.turn.completed')
            completed = True
        elif kind in ('turn.failed', 'error'):
            completed = False
    measured = sum_usage(turns.values()) if turns else usage(source='codex.turn.completed')
    if not completed or malformed:
        measured['coverage'] = {k: 'partial' if measured[k] is not None else 'unavailable' for k in FIELDS}
    return measurement('codex', requested, measured=measured)


def group_usage(entries, keys):
    grouped = defaultdict(list)
    for entry in entries:
        grouped[tuple(entry.get(key) for key in keys)].append(entry['usage'])
    return [dict(zip(keys, key)) | {'usage': sum_usage(values)} for key, values in grouped.items()]


def summarize(attempts):
    attempts = list(attempts)
    return {'attempts': len(attempts), 'usage': sum_usage(a['metrics']['usage'] for a in attempts),
            'by_backend': group_usage([{'backend': a['backend'], 'usage': a['metrics']['usage']}
                                       for a in attempts], ('backend',)),
            'by_model': group_usage([entry for a in attempts for entry in a['metrics']['by_model']],
                                    ('backend', 'model_actual', 'model_requested', 'origin'))}


class RunMetrics:
    def __init__(self, clock):
        self.clock, self.started = clock, clock()
        self.stages, self.attempts = {}, {}
        self.finished = None

    @staticmethod
    def key(context):
        return (context.get('branch'), context['stage'], context.get('revision_id'))

    def start(self, context):
        key = self.key(context)
        if key not in self.stages:
            self.stages[key] = dict(context, status='RUNNING', started=self.clock(), duration_seconds=None)

    def record(self, context, meta):
        self.start(context)
        self.attempts[meta['invocation_id']] = {'backend': meta['backend'], 'metrics': meta['metrics'],
                                               'stage_key': self.key(context)}

    def finish_stage(self, context, status):
        self.start(context)
        row = self.stages[self.key(context)]
        row.update(status=status, duration_seconds=round(self.clock() - row['started'], 3))
        return self.stage(context)

    def stage(self, context):
        key = self.key(context)
        row = self.stages[key]
        return {k: v for k, v in row.items() if k != 'started'} | summarize(
            a for a in self.attempts.values() if a['stage_key'] == key) | {
                'duration_seconds': row['duration_seconds'] if row['duration_seconds'] is not None else
                round(self.clock() - row['started'], 3)}

    def snapshot(self, *, finish=False):
        if finish and self.finished is None:
            self.finished = self.clock()
        return summarize(self.attempts.values()) | {
            'duration_seconds': round((self.finished if self.finished is not None else self.clock()) - self.started, 3),
            'stages': [self.stage(row) for row in self.stages.values()]}
