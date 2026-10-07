# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""XXX / OpenCode 1.2.27 CLI: one run, a verified export, owned-session cleanup."""
import copy
import json
import re
import secrets
import sys

from src.contracts.contracts import ContractError, response_error, strict_json, transport_json
from src.runtime.execution import Budget
from src.runtime.metrics import measurement, model_entry, native_usage, number, sum_usage

PROFILE = 'opencode-v1.2.27-cli'
CLEANUP_SECONDS = 300
EXPORT_RETRIES = 2
CONTINUE_TEXT = 'Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.'
OVERFLOW_TEXT = ("The previous request exceeded the provider's size limit due to large media attachments. "
                 'The conversation was compacted and media files were removed from context. '
                 'If the user was asking about attached images or files, explain that the attachments were too '
                 'large to process and suggest they try again with smaller or fewer files.\n\n')
PARTS = {'step_start': 'step-start', 'step_finish': 'step-finish',
         'text': 'text', 'reasoning': 'reasoning', 'tool_use': 'tool'}


def fail(message, kind='TRANSPORT_ERROR'):
    raise response_error(kind, 'result' if kind in ('INVALID_JSON', 'INCOMPLETE_OUTPUT') else
                         'backend' if kind == 'BACKEND_ERROR' else 'transport', message)


def identifier(value, prefix):
    if type(value) is not str or not re.fullmatch(prefix + r'_[A-Za-z0-9]+', value):
        fail('Invalid XXX protocol identity.')
    return value


def same(left, right):
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def decode_output(raw):
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        fail('XXX returned invalid UTF-8.')


def part_content(part):
    """Exclude native pruning and display metadata when comparing completed tool parts."""
    value = copy.deepcopy(part)
    if value.get('type') == 'tool' and type(value.get('state')) is dict:
        timing = value['state'].get('time')
        if type(timing) is dict and 'compacted' in timing:
            if number(timing['compacted']) is None:
                fail('Invalid XXX tool compaction timestamp.')
            del timing['compacted']
        value['state'].pop('metadata', None)
    return value


def help_command(executable):
    return [executable, 'run', '--help']


def required_flags(mode):
    return ['--format', '--agent', '--model', '--title']


def prepare_environment(env, stage, *, source_snapshot=False):
    try:
        config = strict_json(env.get('OPENCODE_CONFIG_CONTENT') or '{}')
        if type(config) is not dict or type(config.get('agent', {})) is not dict:
            raise ValueError()
    except (ContractError, ValueError) as exc:
        raise response_error('BACKEND_INCOMPATIBLE', 'compatibility',
                             'OPENCODE_CONFIG_CONTENT and agent must be JSON objects.') from exc
    permission = {'*': 'deny'}
    if stage != 'compare':
        permission.update(read='allow', glob='allow', grep='allow', list='allow')
        if source_snapshot:
            permission['external_directory'] = 'deny'
    name = 'architecture-audit-' + secrets.token_hex(16)
    config.setdefault('agent', {})[name] = {'mode': 'primary', 'permission': permission}
    config['share'] = 'disabled'
    env['OPENCODE_AUTO_SHARE'] = '0'
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
    return name


def build_command(agent, stage, mode, schema, schema_path, *, agent_name):
    command = [agent['executable'], 'run', '--format', 'json', '--agent', agent_name,
               '--title', agent_name]
    if agent.get('model'):
        command += ['--model', agent['model']]
    return command


def events(output):
    session, seen = None, {}
    for line in output.splitlines():
        if not line.strip():
            continue
        event = transport_json(line)
        if type(event) is not dict or event.get('type') not in (*PARTS, 'error'):
            fail('Invalid XXX CLI event.')
        sid = identifier(event.get('sessionID'), 'ses')
        if session is not None and session != sid:
            fail('Conflicting XXX session identities.')
        session = sid
        if event['type'] != 'error':
            part = event.get('part')
            if (type(part) is not dict or part.get('type') != PARTS[event['type']]
                    or part.get('sessionID') != sid):
                fail('Invalid XXX CLI message part.')
            key = (identifier(part.get('messageID'), 'msg'), identifier(part.get('id'), 'prt'))
            if key in seen:
                if not same(part_content(seen[key]), part_content(part)):
                    fail('Conflicting XXX message parts.')
                continue
            seen[key] = part
            if event['type'] in ('text', 'reasoning') and type(part.get('text')) is not str:
                fail('Invalid XXX text part.')
        yield event


def session_candidate(output):
    """A candidate is never ownership proof, including after truncated pipe output."""
    sessions = set()
    for line in output.splitlines():
        try:
            item = transport_json(line)
            if type(item) is dict and 'sessionID' in item:
                sessions.add(identifier(item['sessionID'], 'ses'))
        except ContractError:
            continue
    return next(iter(sessions)) if len(sessions) == 1 else None


def owned_export(raw, session, agent_name, prompt, cwd):
    value = transport_json(raw)
    info = value.get('info') if type(value) is dict else None
    messages = value.get('messages') if type(value) is dict else None
    if (type(info) is not dict or info.get('id') != session or info.get('title') != agent_name
            or info.get('directory') != str(cwd) or type(messages) is not list or not messages):
        fail('XXX export does not identify this invocation.')
    root = messages[0]
    user = root.get('info') if type(root) is dict else None
    parts = root.get('parts') if type(root) is dict else None
    if (type(user) is not dict or user.get('role') != 'user' or user.get('sessionID') != session
            or user.get('agent') != agent_name or type(parts) is not list or len(parts) != 1):
        fail('XXX export does not identify the original request.')
    mid = identifier(user.get('id'), 'msg')
    part = parts[0]
    if (type(part) is not dict or part.get('type') != 'text' or part.get('sessionID') != session
            or part.get('messageID') != mid or part.get('synthetic', False) is not False
            or part.get('text') != '\n' + prompt):
        fail('XXX export does not match the submitted prompt.')
    identifier(part.get('id'), 'prt')
    return value


def verified_history(exported, session, agent_name, requested=None):
    """Accept the submitted user and stock automatic compaction continuations."""
    messages = exported['messages']
    users, mids, pids = {}, set(), set()
    current = summary = None
    assistants = []
    ordinary_users = []
    compactions = 0
    for index, message in enumerate(messages):
        info = message.get('info') if type(message) is dict else None
        parts = message.get('parts') if type(message) is dict else None
        if type(info) is not dict or type(parts) is not list or info.get('sessionID') != session:
            fail('Invalid XXX exported message.')
        mid = identifier(info.get('id'), 'msg')
        if mid in mids:
            fail('Duplicate XXX exported message.')
        mids.add(mid)
        for part in parts:
            if (type(part) is not dict or part.get('sessionID') != session
                    or part.get('messageID') != mid or type(part.get('type')) is not str):
                fail('Invalid XXX exported part identity.')
            pid = identifier(part.get('id'), 'prt')
            if pid in pids:
                fail('Duplicate XXX exported part.')
            pids.add(pid)
            if part['type'] in ('text', 'reasoning') and type(part.get('text')) is not str:
                fail('Invalid XXX exported text.')
        if info.get('role') == 'user':
            model = info.get('model')
            if (info.get('agent') != agent_name or type(model) is not dict
                    or any(type(model.get(k)) is not str or not model[k] for k in ('providerID', 'modelID'))):
                fail('Invalid XXX user settings.')
            if requested and model['providerID'] + '/' + model['modelID'] != requested:
                fail('XXX did not use the requested model.')
            if index:
                root = messages[0]['info']
                if any(not same(info.get(k), root.get(k)) for k in ('model', 'agent', 'tools', 'system', 'variant', 'format')):
                    fail('XXX continuation changed task settings.')
                service = (len(parts) == 1 and parts[0]['type'] == 'compaction'
                           and parts[0].get('auto') is True
                           and ('overflow' not in parts[0] or type(parts[0]['overflow']) is bool))
                overflow = bool(current in users and any(p['type'] == 'compaction' and p.get('overflow') is True
                                                        for p in users[current]['parts']))
                continuation = (summary == current and len(parts) == 1 and parts[0]['type'] == 'text'
                                and parts[0].get('synthetic') is True
                                and parts[0]['text'] == (OVERFLOW_TEXT if overflow else '') + CONTINUE_TEXT)
                # Native overflow may replay the preceding ordinary user. This
                # does not authorize a new orchestrator prompt or arbitrary text.
                projection = lambda ps: [{k: v for k, v in p.items() if k not in ('id', 'messageID', 'sessionID')}
                                         for p in ps]
                replay = (summary == current and overflow and len(ordinary_users) >= 2
                          and same(projection(parts), projection(ordinary_users[-1]['parts'])))
                if not (service or continuation or replay):
                    fail('Unsupported XXX conversation continuation.')
                if service:
                    compactions += 1
            users[mid] = message
            if not any(p['type'] == 'compaction' for p in parts):
                ordinary_users.append(message)
            current, summary = mid, None
        elif info.get('role') == 'assistant':
            if info.get('parentID') != current or current not in users:
                fail('Foreign XXX assistant parent.')
            is_summary = info.get('summary') is True
            if info.get('agent') != ('compaction' if is_summary else agent_name):
                fail('Foreign XXX assistant agent.')
            if 'summary' in info and type(info['summary']) is not bool:
                fail('Invalid XXX summary marker.')
            parent = users[current]
            if is_summary != any(p['type'] == 'compaction' for p in parent['parts']):
                fail('Invalid XXX compaction membership.')
            if any(type(info.get(k)) is not str or not info[k] for k in ('providerID', 'modelID')):
                fail('Missing XXX model identity.')
            if not is_summary and any(info[k] != parent['info']['model'][k] for k in ('providerID', 'modelID')):
                fail('XXX assistant model differs from its request.')
            timing = info.get('time')
            if (type(timing) is not dict or number(timing.get('created')) is None
                    or number(timing.get('completed')) is None or timing['completed'] < timing['created']):
                fail('XXX assistant is unfinished.', 'INCOMPLETE_OUTPUT')
            if info.get('error') is not None:
                fail('XXX exported a backend error.', 'BACKEND_ERROR')
            if info.get('finish') not in ('stop', 'tool-calls'):
                fail('XXX assistant did not complete successfully.', 'INCOMPLETE_OUTPUT')
            if any(p['type'] == 'tool' and (type(p.get('state')) is not dict
                   or p['state'].get('status') not in ('completed', 'error')) for p in parts):
                fail('XXX has unfinished tools.', 'INCOMPLETE_OUTPUT')
            if is_summary:
                if info['finish'] != 'stop':
                    fail('XXX compaction summary is unfinished.', 'INCOMPLETE_OUTPUT')
                summary = current
            assistants.append(message)
        else:
            fail('Invalid XXX message role.')
    if (not assistants or messages[-1] is not assistants[-1] or assistants[-1]['info'].get('summary') is True
            or assistants[-1]['info']['finish'] != 'stop'):
        fail('XXX did not export a completed final answer.', 'INCOMPLETE_OUTPUT')
    return assistants, compactions


def deliverable_text(message):
    return ''.join(p['text'] for p in message['parts'] if p['type'] == 'text')

def summary_deliverable(assistants):
    """Compaction summaries are the only fallback deliverable when the final message is prose."""
    for message in reversed(assistants[:-1]):
        if message['info'].get('summary') is not True:
            continue
        try:
            data = strict_json(deliverable_text(message).strip())
        except ContractError:
            continue
        if type(data) is dict:
            return message, data
    return None

def balanced_json_end(text, start):
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def recover_json_object(text):
    """Recover the outermost complete JSON object in the final answer. Prose-only,
    non-object and malformed output stays rejected; recovered JSON still passes the
    full identity, schema and semantic validation."""
    search = 0
    best = None
    best_span = -1
    while True:
        start = text.find('{', search)
        if start == -1:
            break
        end = balanced_json_end(text, start)
        if end is not None:
            try:
                value = strict_json(text[start:end])
            except ContractError:
                pass
            else:
                if type(value) is dict and end - start >= best_span:
                    best, best_span = value, end - start
        search = start + 1
    return best

def parse_output(output, *, exported, agent_name, prompt, cwd, requested=None):
    stream = list(events(output))
    if any(e['type'] == 'error' for e in stream):
        fail('XXX returned an error event.', 'BACKEND_ERROR')
    if not stream:
        fail('XXX returned no events.', 'INCOMPLETE_OUTPUT')
    session = stream[0]['sessionID']
    value = owned_export(exported, session, agent_name, prompt, cwd)
    assistants, count = verified_history(value, session, agent_name, requested)
    final = assistants[-1]
    mid = final['info']['id']
    known = {(m['info']['id'], p['id']): p for m in value['messages'] for p in m['parts']}
    streamed, finishes = {}, []
    for event in stream:
        part = event['part']
        persisted = known.get((part['messageID'], part['id']))
        if persisted is None or not same(part_content(persisted), part_content(part)):
            fail('XXX CLI part differs from the persisted session.')
        if event['type'] == 'step_finish' and part.get('reason') not in ('stop', 'tool-calls'):
            fail('XXX CLI reported an unsuccessful step.', 'INCOMPLETE_OUTPUT')
        if event['type'] == 'text':
            streamed.setdefault(part['messageID'], []).append(part['text'])
        elif event['type'] == 'step_finish':
            finishes.append(part.get('reason'))
    final_text = deliverable_text(final)
    emitted = ''.join(streamed.get(mid, []))
    if not emitted or emitted != final_text:
        fail('XXX CLI did not emit the complete final text.', 'INCOMPLETE_OUTPUT')
    if finishes and finishes[-1] != 'stop':
        fail('XXX CLI completion contradicts the export.', 'INCOMPLETE_OUTPUT')
    source, data = final, None
    invalid = None
    try:
        data = strict_json(final_text.strip())
    except ContractError as exc:
        invalid = exc
    if type(data) is not dict:
        fallback = summary_deliverable(assistants)
        if fallback is not None:
            source, data = fallback
        elif invalid is not None:
            raise invalid
        else:
            fail('XXX final answer must be a JSON object.', 'INVALID_JSON')
    if source is not final:
        emitted = ''.join(streamed.get(source['info']['id'], []))
        if not emitted or emitted != deliverable_text(source):
            fail('XXX CLI did not emit the complete summary text.', 'INCOMPLETE_OUTPUT')
    info = source['info']
    return data, {'session_id': session, 'request_id': value['messages'][0]['info']['id'],
                  'message_id': info['id'], 'finish_reason': 'stop', 'completion_source': 'session_export',
                  'model_actual': info['providerID'] + '/' + info['modelID'],
                  'compaction': {'completed': count},
                  'metrics': export_metrics(assistants, requested)}


def export_metrics(assistants, requested=None):
    entries = []
    for message in assistants:
        info = message['info']
        # Choose steps OR message totals, never both.
        steps = [p for p in message['parts'] if p['type'] == 'step-finish']
        measured = sum_usage(native_usage(p.get('tokens'), p.get('cost'), 'xxx.session_export', True)
                             for p in steps) if steps else native_usage(
                                 info.get('tokens'), info.get('cost'), 'xxx.session_export', True)
        entry = model_entry('xxx', None if info.get('summary') is True else requested,
                            info['providerID'] + '/' + info['modelID'], measured)
        entry['origin'] = 'compaction' if info.get('summary') is True else 'stage'
        entries.append(entry)
    return {'usage': sum_usage(e['usage'] for e in entries), 'by_model': entries}


def collect_metrics(output, requested=None):
    """Pipe counters are partial until the full export has been verified."""
    steps = []
    try:
        for event in events(output):
            if event['type'] == 'step_finish':
                part = event['part']
                steps.append(native_usage(part.get('tokens'), part.get('cost'), 'xxx.step_finish', False))
    except ContractError:
        pass
    return measurement('xxx', requested, measured=sum_usage(steps) if steps else None)


def invoke(command, cwd, env, payload, *, process, artifacts, budget, meta, process_options):
    """Run, export and clean up using the existing subprocess implementation."""
    name = command[command.index('--agent') + 1]
    requested = meta.get('model_requested')
    prompt = payload.decode('utf-8')
    output, session, owned, export_attempted = '', None, False, False

    def local(args, directory, operation_budget):
        directory.mkdir(mode=0o700, exist_ok=True)
        return process([command[0], *args], cwd, env, log_dir=directory,
                       budget=operation_budget, **process_options)

    try:
        meta['prompt_sent'] = True
        result = process(command, cwd, env, payload, log_dir=artifacts, budget=budget, **process_options)
        output = decode_output(result['stdout'])
        meta.update(returncode=result['returncode'], output_bytes=len(result['stdout']))
        meta['metrics'] = collect_metrics(output, requested)
        session = session_candidate(output)
        if result['returncode']:
            fail('XXX CLI failed; inspect private attempt logs.', 'BACKEND_ERROR')
        list(events(output))
        if session is None:
            fail('XXX did not identify a session.', 'INCOMPLETE_OUTPUT')
        export_attempted = True
        result = local(['export', session], artifacts / 'session-export', budget)
        if result['returncode']:
            fail('XXX could not export the final session.', 'INCOMPLETE_OUTPUT')
        exported = decode_output(result['stdout'])
        for attempt in range(EXPORT_RETRIES + 1):
            try:
                value = owned_export(exported, session, name, prompt, cwd)
                break
            except ContractError as exc:
                if attempt >= EXPORT_RETRIES or 'json_error' not in exc.details:
                    raise
                budget.check()
                result = local(['export', session], artifacts / 'session-export', budget)
                if result['returncode']:
                    fail('XXX could not export the final session.', 'INCOMPLETE_OUTPUT')
                exported = decode_output(result['stdout'])
        owned = True
        data, provider = parse_output(output, exported=exported, agent_name=name,
                                      prompt=prompt, cwd=cwd, requested=requested)
        meta.update(provider)
        return data, provider
    finally:
        prior_error = sys.exc_info()[0] is not None
        if not output:
            try:
                output = (artifacts / 'stdout.log').read_text(encoding='utf-8', errors='replace')
            except OSError:
                pass
            meta['metrics'] = collect_metrics(output, requested)
        session = session or session_candidate(output)
        cleanup = Budget(CLEANUP_SECONDS, clock=budget.clock, label='xxx_cleanup')
        try:
            # A failed/aborted run gets one local export to prove ownership.
            # An already failed export is never retried; no session is guessed.
            if session and not owned and not export_attempted:
                result = local(['export', session], artifacts / 'session-export', cleanup)
                if not result['returncode']:
                    owned_export(decode_output(result['stdout']), session, name, prompt, cwd)
                    owned = True
            if owned:
                result = local(['session', 'delete', session], artifacts / 'session-delete', cleanup)
                if result['returncode']:
                    fail('XXX session cleanup failed.', 'BACKEND_ERROR')
                meta['session_cleanup'] = 'deleted'
            else:
                meta['session_cleanup'] = 'ownership_unverified'
                meta.setdefault('cleanup_errors', []).append('xxx_session_ownership_unverified')
        except BaseException:
            meta['session_cleanup'] = 'failed'
            meta.setdefault('cleanup_errors', []).append('xxx_session_cleanup_failed')
            if not prior_error:
                raise
