# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Synthetic CLI/export shapes, independently based on OpenCode v1.2.27.
Source: anomalyco/opencode@4ee426ba549131c4903a71dfb6259200467aca81:
packages/opencode/src/cli/cmd/{run,export,session}.ts and session/compaction.ts.
These are fixtures, not captured model output or a production parser.
"""
import copy
import json

CONTINUE = 'Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.'
TOKENS = {'input': 5, 'output': 5, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}}


def transcript(data, *, session='ses_test', agent='audit', prompt='Test prompt',
               cwd='/fixture', model='fixture/configured-model', rounds=0, scenario=''):
    provider, model_id = model.split('/', 1)
    selected = {'providerID': provider, 'modelID': model_id}
    def part(mid, kind, **values):
        return dict(id='prt_' + mid[4:] + kind.replace('-', ''), sessionID=session,
                    messageID=mid, type=kind, **values)
    def user(mid, parts):
        return {'info': dict(id=mid, sessionID=session, role='user', agent=agent,
                             model=copy.deepcopy(selected), time={'created': 1}), 'parts': parts}
    def assistant(mid, parent, text, summary=False):
        info = dict(id=mid, sessionID=session, role='assistant', parentID=parent,
                    agent='compaction' if summary else agent, mode='compaction' if summary else agent,
                    providerID=provider, modelID='small' if summary else model_id, time={'created': 1, 'completed': 2},
                    tokens=copy.deepcopy(TOKENS), cost=0, finish='stop')
        if summary: info['summary'] = True
        return {'info': info, 'parts': [part(mid, 'step-start'),
                part(mid, 'text', text=text, time={'start': 1, 'end': 2}),
                part(mid, 'step-finish', reason='stop', tokens=copy.deepcopy(TOKENS), cost=0)]}
    messages = [user('msg_root', [part('msg_root', 'text', text='\n' + prompt)])]
    parent = 'msg_root'
    for n in range(rounds):
        mid = 'msg_compact' + str(n)
        messages.append(user(mid, [part(mid, 'compaction', auto=True)]))
        messages.append(assistant('msg_summary' + str(n), mid, 'Preserved task context.', True))
        parent = 'msg_continue' + str(n)
        messages.append(user(parent, [part(parent, 'text', text=CONTINUE, synthetic=True)]))
    text = data if isinstance(data, str) else json.dumps(data)
    final = assistant('msg_final', parent, text)
    messages.append(final)
    info = final['info']
    if scenario == 'no-final': info['time'].pop('completed')
    if scenario == 'foreign-request': info['parentID'] = 'msg_foreign'
    if scenario == 'summary-only': info.update(summary=True, agent='compaction')
    if scenario in ('truncated', 'unknown-finish', 'unknown-compare-finish'):
        info['finish'] = 'length' if scenario == 'truncated' else 'PRIVATE_UNKNOWN_FINISH'
        final['parts'][-1]['reason'] = info['finish']
    if scenario == 'backend-error':
        info['error'] = {'name': 'APIError', 'data': {'message': 'SECRET_RESPONSE'}}
    if scenario == 'unfinished-tool':
        final['parts'].insert(1, part('msg_final', 'tool', tool='read', state={'status': 'running'}))
    kinds = {'step-start': 'step_start', 'text': 'text', 'step-finish': 'step_finish', 'tool': 'tool_use'}
    stream = [dict(type=kinds[p['type']], timestamp=2, sessionID=session, part=copy.deepcopy(p))
              for m in messages if m['info']['role'] == 'assistant' for p in m['parts']]
    if scenario == 'missing-finish': stream.pop()
    if scenario == 'duplicate': stream.insert(1, copy.deepcopy(stream[0]))
    if scenario == 'conflicting-duplicate':
        duplicate = copy.deepcopy(stream[1]); duplicate['part']['text'] = 'Conflicting'
        stream.append(duplicate)
    if scenario == 'foreign-session': stream[-1]['sessionID'] = 'ses_foreign'
    if scenario == 'backend-error':
        stream.append(dict(type='error', timestamp=3, sessionID=session, error=info['error']))
    exported = {'info': dict(id=session, title=agent, directory=cwd), 'messages': messages}
    if scenario == 'export-foreign': exported['info']['title'] = 'foreign'
    if scenario == 'export-prompt': messages[0]['parts'][0]['text'] = 'foreign'
    if scenario == 'export-text': final['parts'][1]['text'] = '{"substituted":true}'
    output = '\n'.join(json.dumps(e) for e in stream) + '\n'
    if scenario == 'malformed-event': output += '{broken\n'
    return output, exported

