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


def claude_tokens(raw, *, model=False):
    names = ('inputTokens', 'outputTokens', 'cacheReadInputTokens', 'cacheCreationInputTokens') if model else (
        'input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
    incoming, outgoing, read, write = [number(raw.get(key), tokens=True) for key in names]
    incoming = add(incoming, read, write)
    return {'input_tokens': incoming, 'output_tokens': outgoing,
            'cache_read_tokens': read, 'cache_write_tokens': write,
            'total_tokens': add(incoming, outgoing)}


def claude_usage(envelope, requested=None):
    if type(envelope) is not dict:
        return measurement('claude-code', requested)
    models = envelope.get('modelUsage')
    entries = []
    if type(models) is dict and models:
        for name, raw in models.items():
            values = claude_tokens(raw, model=True) if type(raw) is dict else {}
            values['cost_usd'] = raw.get('costUSD') if type(raw) is dict else None
            entries.append(model_entry('claude-code', requested, name,
                                       usage(values, source='claude.modelUsage')))
        measured = sum_usage(entry['usage'] for entry in entries)
    else:
        raw = envelope.get('usage')
        measured = usage(claude_tokens(raw) if type(raw) is dict else {}, source='claude.usage')
    # The envelope cost and per-model costs describe the same work, never add them.
    cost = number(envelope.get('total_cost_usd'))
    measured.update(cost_usd=cost, cost_is_estimate=cost is not None)
    measured['coverage']['cost_usd'] = 'complete' if cost is not None else 'unavailable'
    if cost is not None:
        measured['sources'] = sorted(set(measured['sources']) | {'claude.total_cost_usd'})
    if not entries:
        entries = [model_entry('claude-code', requested, None, measured)]
    # Crash envelopes can zero every counter. Preserve reported zeros but flag
    # unknown remaining spend rather than claiming that accounting is complete.
    if envelope.get('subtype') == 'error_during_execution':
        for item in [measured, *(entry['usage'] for entry in entries)]:
            for key in FIELDS:
                item['coverage'][key] = 'partial' if item[key] is not None else 'unavailable'
    return {'usage': measured, 'by_model': entries}


def native_usage(raw, cost, source, complete):
    raw = raw if type(raw) is dict else {}
    cache = raw.get('cache') if type(raw.get('cache')) is dict else {}
    incoming, outgoing, read, write, reasoning, total = [number(value, tokens=True) for value in (
        raw.get('input'), raw.get('output'), cache.get('read'), cache.get('write'),
        raw.get('reasoning'), raw.get('total'))]
    incoming = add(incoming, read, write)
    estimated = total is None
    total = add(incoming, outgoing) if estimated else total
    return usage({'input_tokens': incoming, 'output_tokens': outgoing, 'cache_read_tokens': read,
                  'cache_write_tokens': write, 'reasoning_tokens': reasoning,
                  'total_tokens': total, 'cost_usd': cost},
                 source=source, complete=complete, estimated=estimated and total is not None)


class HTTPUsage:
    """Replace message snapshots, and choose steps OR a completed message's totals."""
    def __init__(self, backend, requested, session, request, agent):
        self.backend, self.requested = backend, requested
        self.session, self.request, self.agent = session, request, agent
        self.messages = {}
        self.origins = {}

    def validated(self, history, *, complete=False):
        """Consume exactly the transport's accepted membership, including summaries.

        Never reclassify a raw user/assistant envelope independently of transport.
        A failed history remains partial even if a caller asks for complete totals.
        """
        self.messages = {m['info']['id']: m for m, _ in history.usage_messages()}
        self.origins = {m['info']['id']: origin for m, origin in history.usage_messages()}
        return self._measure(complete=complete and not history.failed and history.phase == 'stage')

    def observe(self, messages, *, complete=False):
        valid = type(messages) is list
        for message in messages if valid else []:
            info = message.get('info') if type(message) is dict else None
            if (type(info) is not dict or info.get('sessionID') != self.session
                    or type(info.get('id')) is not str):
                valid = False
                continue
            if info.get('role') == 'user' and info['id'] == self.request:
                continue
            if (info.get('role') != 'assistant' or info.get('parentID') != self.request
                    or info.get('agent') != self.agent or type(message.get('parts')) is not list):
                valid = False
                continue
            self.messages[info['id']] = message
        return self._measure(complete=complete and valid)

    def _measure(self, *, complete):
        valid = True
        entries = []
        for mid, message in self.messages.items():
            info = message['info']
            parts = {}
            for part in message['parts']:
                if (type(part) is not dict or part.get('sessionID') != self.session
                        or part.get('messageID') != mid or type(part.get('id')) is not str):
                    valid = False
                    continue
                if part.get('type') == 'step-finish':
                    parts[part['id']] = part
            finished = (type(info.get('time')) is dict and
                        number(info['time'].get('completed')) is not None)
            if not finished or 'error' in info:
                complete = False
            actual = (info['providerID'] + '/' + info['modelID'] if
                      all(type(info.get(k)) is str and info[k] for k in ('providerID', 'modelID')) else None)
            origin = self.origins.get(mid, 'stage')
            requested = self.requested if origin == 'stage' else None

            def entry(measured):
                return model_entry(self.backend, requested, actual, measured) | {
                    'origin': origin, 'agent': info.get('agent')}

            samples = list(parts.values()) if parts else [info] if finished else []
            if not samples:
                entries.append(entry(usage()))
            for sample in samples:
                measured = native_usage(sample.get('tokens'), sample.get('cost'),
                                        self.backend + ('.step-finish' if parts else '.assistant'), True)
                entries.append(entry(measured))
        if not complete or not valid:
            for entry in entries:
                measured = entry['usage']
                measured['coverage'] = {k: 'partial' if measured[k] is not None else 'unavailable' for k in FIELDS}
        return {'usage': sum_usage(e['usage'] for e in entries), 'by_model': entries} if entries else measurement(
            self.backend, self.requested)


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
