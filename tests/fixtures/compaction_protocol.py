#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Synthetic upstream-reference shapes; NOT a recording of a XXX continuation.

Only auto/non-overflow user + empty summary prefix was reported for real XXX.
The format-preserving continuation here describes the required compatibility,
not evidence that the proprietary runtime implements it.
"""
import copy

# Kept independent of the production recognizer so changing its shape breaks tests.
CONTINUE_TEXT = 'Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.'


def part(mid, kind, **fields):
    return {'id': 'prt_' + mid.removeprefix('msg_') + kind.replace('-', ''),
            'messageID': mid, 'sessionID': 'ses_test', 'type': kind, **fields}


def body():
    return {'messageID': 'msg_root', 'agent': 'audit',
            'model': {'providerID': 'provider', 'modelID': 'study'},
            'format': {'type': 'json_schema', 'schema': {'type': 'object'}, 'retryCount': 0},
            'parts': [{'type': 'text', 'text': 'Original task'}]}


def root(request=None, session='ses_test'):
    request = request or body()
    mid = request['messageID']
    return {'info': {'id': mid, 'sessionID': session, 'role': 'user', 'time': {'created': 0},
                    'agent': request['agent'], 'model': request.get('model', {'providerID': 'provider', 'modelID': 'study'}),
                    'format': copy.deepcopy(request['format'])},
            'parts': [part(mid, 'text', text=request['parts'][0]['text']) | {'sessionID': session}]}


def assistant(mid='msg_final', parent='msg_root', *, summary=False):
    info = {'id': mid, 'sessionID': 'ses_test', 'parentID': parent, 'role': 'assistant',
            'agent': 'compaction' if summary else 'audit', 'mode': 'compaction' if summary else 'audit',
            'providerID': 'provider', 'modelID': 'small' if summary else 'study',
            'time': {'created': 1, 'completed': 2}, 'path': {'cwd': '/fixture', 'root': '/fixture'},
            'cost': 0.01, 'tokens': {'input': 3, 'output': 1, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}},
            'finish': 'stop'}
    if summary:
        info['summary'] = True
    else:
        info['structured'] = {'ok': True}
    parts = [part(mid, 'text', text='Synthetic summary' if summary else 'Untrusted prose')]
    if not summary:
        parts.append(part(mid, 'tool', tool='StructuredOutput', callID='call_' + mid,
                          state={'status': 'completed', 'input': info['structured'], 'output': '',
                                 'title': '', 'metadata': {}, 'time': {'start': 1, 'end': 2}}))
    return {'info': info, 'parts': parts}


def chain(rounds=1, request=None, final=None):
    request = request or body()
    initial = root(request)
    messages = [initial]
    parent = request['messageID']
    for n in range(rounds):
        previous = assistant('msg_step' + str(n), parent)
        previous['info'].pop('structured')
        previous['parts'] = [part(previous['info']['id'], 'step-finish', tokens=previous['info']['tokens'], cost=.01)]
        messages.append(previous)
        service = copy.deepcopy(initial)
        service['info']['id'] = 'msg_compact' + str(n)
        service['info'].pop('format')
        service['info']['time']['created'] = n + 3
        service['parts'] = [part(service['info']['id'], 'compaction', auto=True, overflow=False)]
        messages.append(service)
        messages.append(assistant('msg_summary' + str(n), service['info']['id'], summary=True))
        continuation = copy.deepcopy(initial)
        parent = 'msg_continue' + str(n)
        continuation['info']['id'] = parent
        continuation['info']['time']['created'] = n + 4
        continuation['parts'] = [part(parent, 'text', text=CONTINUE_TEXT, synthetic=True,
                                      time={'start': n + 4, 'end': n + 4})]
        messages.append(continuation)
    last = copy.deepcopy(final) if final else assistant(parent=parent)
    last['info']['parentID'] = parent
    messages.append(last)
    # Preserve stage identity for fake HTTP-generated requests.
    session = last['info']['sessionID']
    model = request.get('model') or {'providerID': last['info']['providerID'], 'modelID': last['info']['modelID']}
    for message in messages:
        info = message['info']
        info['sessionID'] = session
        if info['role'] == 'user':
            info.update(agent=request['agent'], model=copy.deepcopy(model))
        elif not info.get('summary'):
            info.update(agent=request['agent'], mode=request['agent'])
        for item in message['parts']:
            item['sessionID'] = session
    return messages


def snapshots(messages):
    """A user/message and its parts need not arrive in the same HTTP snapshot."""
    result = []
    for i, message in enumerate(messages):
        if message['info'].get('summary') is True:
            partial = copy.deepcopy(message)
            partial['parts'] = []
            partial['info']['time'].pop('completed')
            partial['info'].pop('finish')
            result.extend([copy.deepcopy(messages[:i]) + [partial]] * 2)
            partial = copy.deepcopy(message)
            partial['parts'] = []
            result.append(copy.deepcopy(messages[:i]) + [partial])
        elif message['info']['role'] == 'user':
            partial = copy.deepcopy(message)
            partial['parts'] = []
            result.append(copy.deepcopy(messages[:i]) + [partial])
        result.append(copy.deepcopy(messages[:i + 1]))
    return result


def text_completion_snapshots(messages):
    """OpenCode 1.2.27 text-end resets start before the native result arrives."""
    final = copy.deepcopy(messages)
    message = final[-1]
    message['info']['time'] = {'created': 90, 'completed': 130}
    message['parts'][0].update(text='Result.', time={'start': 120, 'end': 121})
    message['parts'][-1]['state']['time'] = {'start': 122, 'end': 129}
    closed = copy.deepcopy(final)
    pending = closed[-1]
    pending['info']['time'].pop('completed')
    for key in ('structured', 'finish'):
        pending['info'].pop(key)
    pending['parts'] = pending['parts'][:1]
    opened = copy.deepcopy(closed)
    opened[-1]['parts'][0].update(text='', time={'start': 100})
    return [opened, closed, final]


def metadata_stream_snapshots(messages):
    """Synthetic metadata + text/reasoning lifecycle, including coalesced final chunk."""
    messages = copy.deepcopy(messages)
    result = [copy.deepcopy(messages[:1])]
    messages[0]['info']['summary'] = {'diffs': []}
    result.extend([copy.deepcopy(messages[:1])] * 2)
    messages[0]['info']['summary'] = {'title': 'Synthetic title', 'body': 'Metadata only', 'diffs': []}
    result.extend(snapshots(messages[:-1]))
    final = messages[-1]
    final['info']['time'] = {'created': 90, 'completed': 130}
    final['parts'][-1]['state']['time'] = {'start': 122, 'end': 129}
    text = final['parts'][0]
    text.update(text='Результат.', time={'start': 120, 'end': 121})
    reasoning = part(final['info']['id'], 'reasoning', text='Ход\nГотово.', time={'start': 100, 'end': 121})
    reasoning['sessionID'] = final['info']['sessionID']
    final['parts'].insert(1, reasoning)
    for content, thought in (('Результат', 'Ход'), ('Результат.\n', 'Ход\n')):
        partial = copy.deepcopy(messages)
        pending = partial[-1]
        for key in ('structured', 'finish'):
            pending['info'].pop(key, None)
        pending['info']['time'].pop('completed')
        pending['parts'] = pending['parts'][:2]
        for item, value in zip(pending['parts'], (content, thought)):
            item['text'] = value
            item['time'] = {'start': 100}
        result.append(partial)
    # Both parts close while the message is still running; the text start resets,
    # whereas reasoning-end preserves it. Native StructuredOutput arrives later.
    closed = copy.deepcopy(messages)
    closed[-1]['info']['time'].pop('completed')
    for key in ('structured', 'finish'):
        closed[-1]['info'].pop(key)
    closed[-1]['parts'] = closed[-1]['parts'][:2]
    result.append(closed)
    result.append(messages)
    return result
