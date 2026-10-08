# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""OpenCode V2 CLI transport; final JSON is validated locally (stdlib only)."""
import json
from pathlib import Path
import re
import secrets

from src.contracts.contracts import ContractError, response_error, strict_json, transport_json
from src.backends.json_response import json_object_response
from src.runtime.metrics import measurement, native_usage, number, sum_usage


def verify_version(version):
    if type(version) is not str or not re.fullmatch(r'(?:opencode v)?2\.\d+\.\d+', version):
        raise response_error('BACKEND_INCOMPATIBLE', 'compatibility',
                             'The OpenCode CLI adapter requires OpenCode V2.')


def help_command(executable: str) -> list[str]:
    return [executable, 'run', '--help']


def required_flags(mode: str) -> list[str]:
    return ['--standalone', '--format', '--model', '--agent']


def prepare_environment(env, stage):
    try:
        config = strict_json(env.get('OPENCODE_CONFIG_CONTENT') or '{}')
    except ContractError as exc:
        raise response_error('BACKEND_INCOMPATIBLE', 'compatibility',
                             'OPENCODE_CONFIG_CONTENT must contain a JSON object.') from exc
    if type(config) is not dict or type(config.get('agents', {})) is not dict:
        raise response_error('BACKEND_INCOMPATIBLE', 'compatibility',
                             'OPENCODE_CONFIG_CONTENT and its agents field must be JSON objects.')
    permissions = [{'action': '*', 'resource': '*', 'effect': 'deny'}]
    if stage not in ('compare', 'repair'):
        permissions += [{'action': action, 'resource': '*', 'effect': 'allow'}
                        for action in ('read', 'glob', 'grep')]
    name = 'architecture-audit-' + secrets.token_hex(16)
    config.setdefault('agents', {})[name] = {'mode': 'primary', 'permissions': permissions}
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
    return name


def build_command(agent: dict, stage: str, mode: str, schema: dict,
                  schema_path: Path, *, agent_name: str) -> list[str]:
    cmd = [agent['executable'], 'run', '--standalone', '--format', 'json', '--agent', agent_name]
    if agent.get('model'):
        cmd += ['--model', agent['model']]
    return cmd


def events(output):
    session = None
    for line in output.splitlines():
        if not line.strip():
            continue
        event = transport_json(line)
        if type(event) is not dict or type(event.get('type')) is not str:
            raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid OpenCode CLI event.')
        if event['type'] == 'error':
            yield event
            continue
        sid = event.get('sessionID')
        if type(sid) is not str or not sid or (session is not None and sid != session):
            raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid OpenCode session identity.')
        session = sid
        if event['type'] in ('step_start', 'step_finish', 'text', 'reasoning', 'tool_use'):
            part = event.get('part')
            part_type = 'tool' if event['type'] == 'tool_use' else event['type'].replace('_', '-')
            if (type(part) is not dict or part.get('sessionID') != sid
                    or part.get('type') != part_type
                    or any(type(part.get(k)) is not str or not part[k] for k in ('id', 'messageID'))):
                raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid OpenCode message part identity.')
        yield event


def verified_export(raw, session, message, text, agent_name):
    """Confirm a missing CLI terminal event against this session's persisted result."""
    exported = transport_json(raw)
    info = exported.get('info') if type(exported) is dict else None
    messages = exported.get('messages') if type(exported) is dict else None
    if (type(info) is not dict or info.get('id') != session
            or type(messages) is not list or not messages
            or any(type(m) is not dict for m in messages)):
        raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid OpenCode session export identity.')
    assistants = [m for m in messages if m.get('type') == 'assistant']
    if (not assistants or assistants[-1].get('id') != message
            or not agent_name or info.get('agent') != agent_name
            or assistants[-1].get('agent') != agent_name):
        raise response_error('TRANSPORT_ERROR', 'transport', 'OpenCode export does not match the final CLI message.')
    final = assistants[-1]
    timing = final.get('time')
    if (info.get('outcome') != 'succeeded' or messages[-1].get('type') != 'idle'
            or messages[-1].get('outcome') != 'succeeded' or final.get('finish') != 'stop'
            or final.get('error') is not None or type(timing) is not dict
            or number(timing.get('created')) is None or number(timing.get('completed')) is None
            or timing['completed'] < timing['created']):
        raise response_error('INCOMPLETE_OUTPUT', 'result', 'OpenCode export does not confirm successful completion.')
    content = final.get('content')
    if (type(content) is not list or any(type(p) is not dict for p in content)
            or any(type(p.get('text')) is not str for p in content if p.get('type') == 'text')
            or ''.join(p['text'] for p in content if p.get('type') == 'text') != text):
        raise response_error('TRANSPORT_ERROR', 'transport', 'OpenCode exported text differs from the CLI answer.')
    return {'session_id': session, 'message_id': message,
            'tokens': final.get('tokens'), 'cost': final.get('cost')}


def parse_output(output: str, *, exported=None, agent_name=None) -> tuple[dict, dict]:
    texts, finishes, seen = {}, {}, {}
    messages = set()
    latest = session = None
    for event in events(output):
        kind = event['type']
        if kind == 'error':
            raise response_error('BACKEND_ERROR', 'backend', 'OpenCode returned an error event.')
        if kind not in ('step_start', 'step_finish', 'text', 'reasoning', 'tool_use'):
            continue
        part = event['part']
        key = (part['messageID'], part['id'])
        if key in seen:
            if seen[key] != part:
                raise response_error('TRANSPORT_ERROR', 'transport', 'Conflicting OpenCode message parts.')
            continue
        seen[key] = part
        session = event['sessionID']
        if part['messageID'] not in messages:
            latest = part['messageID']
            messages.add(latest)
        if kind == 'text':
            if type(part.get('text')) is not str:
                raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid OpenCode text part.')
            texts.setdefault(part['messageID'], []).append(part['text'])
        elif kind in ('step_start', 'step_finish'):
            latest = part['messageID']
            if kind == 'step_finish':
                finishes[latest] = part.get('reason')
    recovered = None
    if latest is not None and latest not in finishes and latest in texts:
        if not re.fullmatch(r'ses_[A-Za-z0-9]+', session):
            raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid OpenCode session identity.')
        if exported is None:
            raise response_error('INCOMPLETE_OUTPUT', 'result',
                                 'OpenCode omitted the final completion event.',
                                 session_id=session, message_id=latest)
        recovered = verified_export(exported, session, latest, ''.join(texts[latest]), agent_name)
        finishes[latest] = 'stop'
    if latest is None or finishes.get(latest) != 'stop' or latest not in texts:
        raise response_error('INCOMPLETE_OUTPUT', 'result', 'OpenCode did not return a completed final answer.')
    data, response_meta = json_object_response(''.join(texts[latest]))
    meta = {'session_id': session, 'message_id': latest, 'finish_reason': finishes[latest], **response_meta}
    if recovered is not None:
        meta.update(completion_source='session_export', exported_finish=recovered)
    return data, meta


def collect_metrics(output: str, requested=None, *, completed=None) -> dict:
    steps = {}
    messages = set()
    complete = False
    failed = False
    session = latest = None
    try:
        for event in events(output):
            kind = event['type']
            if kind in ('step_start', 'step_finish', 'text', 'reasoning', 'tool_use'):
                session = event['sessionID']
                mid = event['part']['messageID']
                if mid not in messages:
                    latest = mid
                    complete = False
                    messages.add(mid)
            if kind == 'step_start':
                complete = False
            elif kind == 'step_finish':
                part = event['part']
                key = (event['sessionID'], part['messageID'], part['id'])
                if key in steps and steps[key] != part:
                    failed = True
                steps[key] = part
                complete = part.get('reason') == 'stop'
            elif kind == 'error':
                failed = True
    except ContractError:
        failed = True
    measured_steps = [native_usage(p.get('tokens'), p.get('cost'), 'opencode.step_finish', True)
                      for p in steps.values()]
    if (completed is not None and not failed and completed['session_id'] == session
            and completed['message_id'] == latest and not any(key[1] == latest for key in steps)):
        measured_steps.append(native_usage(completed.get('tokens'), completed.get('cost'),
                                           'opencode.session_export', True))
        complete = True
    measured = sum_usage(measured_steps) if measured_steps else None
    if measured is not None and (failed or not complete):
        measured['coverage'] = {k: 'partial' if measured[k] is not None else 'unavailable'
                                for k in measured['coverage']}
    return measurement('opencode', requested, measured=measured)
