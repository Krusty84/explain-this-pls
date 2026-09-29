"""Owned loopback OpenCode HTTP transport (Python stdlib only).

Wire contract inspected at upstream v1.2.27. See docs/opencode-protocol.md for
the upstream retry defect and the deliberately closed production capability gate.
"""
from __future__ import annotations
import base64
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import threading
import time

from contracts import ContractError, response_error, strict_json, transport_json
from execution import Budget

WIRE_VERSION = '1.2.27'
STARTUP_SECONDS = 20
HTTP_SECONDS = 5
CLEANUP_SECONDS = 5


def incompatible(message):
    return response_error('BACKEND_INCOMPATIBLE', 'compatibility', message)


def verify_version(version):
    if version != WIRE_VERSION:
        known = version if type(version) is str and re.fullmatch(r'\d+\.\d+\.\d+', version) else None
        raise response_error('BACKEND_INCOMPATIBLE', 'compatibility',
            'OpenCode HTTP adapter requires the inspected 1.2.27 wire interface; '
            'this CLI version is unsupported. No model request was sent.', cli_version=known)


def verify_native_retries():
    # Do not accept a mock, a documented field, or a successful happy-path model
    # call as proof of enforcement. Even retryCount=0 is not an enforced bound on
    # tool-validation corrections in the inspected upstream loop.
    raise incompatible('OpenCode 1.2.27 does not enforce format.retryCount (including zero). '
                       'Bounded native structured-output retries are not supported by this build. '
                       'No model request was sent; see docs/opencode-protocol.md.')


def object_value(value, label):
    if type(value) is not dict:
        raise response_error('TRANSPORT_ERROR', 'transport', f'Invalid {label}: expected object.')
    return value


def identifier(value, prefix):
    if type(value) is not str or not re.fullmatch(prefix + r'_[A-Za-z0-9]+', value):
        raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid protocol identifier.')
    return value


def native_error(error):
    object_value(error, 'backend error')
    name = error.get('name')
    if type(name) is not str or type(error.get('data')) is not dict:
        raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid backend error envelope.')
    if name == 'StructuredOutputError':
        retries = error['data'].get('retries')
        if type(retries) not in (int, float) or retries < 0:
            raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid structured-output retry counter.')
        raise response_error('STRUCTURED_OUTPUT_EXHAUSTED', 'backend',
                             'OpenCode could not produce structured output.', native_retries_reported=retries)
    if name == 'MessageOutputLengthError':
        raise response_error('INCOMPLETE_OUTPUT', 'result', 'OpenCode output was truncated.')
    raise response_error('BACKEND_ERROR', 'backend', 'OpenCode reported a backend error.')


def extract_result(value, session_id, request_id, agent_name):
    envelope = object_value(value, 'message envelope')
    info = object_value(envelope.get('info'), 'assistant info')
    parts = envelope.get('parts')
    if type(parts) is not list:
        raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid message parts.')
    mid = identifier(info.get('id'), 'msg')
    identifier(info.get('sessionID'), 'ses')
    identifier(info.get('parentID'), 'msg')
    if info['sessionID'] != session_id or info['parentID'] != request_id or info.get('agent') != agent_name:
        raise response_error('TRANSPORT_ERROR', 'transport', 'Response identity does not match this request/session/stage.')
    if info.get('role') != 'assistant':
        raise response_error('TRANSPORT_ERROR', 'transport', 'Expected assistant response.')
    if 'error' in info:
        native_error(info['error'])
    timing = object_value(info.get('time'), 'message time')
    if type(timing.get('created')) not in (int, float):
        raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid message creation time.')
    if type(timing.get('completed')) not in (int, float) or timing['completed'] < timing['created']:
        raise response_error('INCOMPLETE_OUTPUT', 'result', 'Assistant message has no completed timestamp.')
    if info.get('finish') not in ('stop', 'tool-calls'):
        raise response_error('INCOMPLETE_OUTPUT', 'result', 'Assistant message has no supported final finish reason.')
    for key in ('providerID', 'modelID', 'mode', 'agent'):
        if type(info.get(key)) is not str or not info[key]:
            raise response_error('TRANSPORT_ERROR', 'transport', 'Missing assistant model/agent metadata.')
    for key in ('cwd', 'root'):
        if type(object_value(info.get('path'), 'assistant path').get(key)) is not str:
            raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid assistant path metadata.')
    tokens = object_value(info.get('tokens'), 'token usage')
    numbers = [info.get('cost'), *(tokens.get(k) for k in ('input', 'output', 'reasoning'))]
    cache = object_value(tokens.get('cache'), 'cache usage')
    numbers.extend(cache.get(k) for k in ('read', 'write'))
    if any(type(n) not in (int, float) or n < 0 for n in numbers):
        raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid numeric usage metadata.')
    ids = set()
    structured_calls = []
    for part in parts:
        object_value(part, 'message part')
        pid = identifier(part.get('id'), 'prt')
        if pid in ids or part.get('messageID') != mid or part.get('sessionID') != session_id:
            raise response_error('TRANSPORT_ERROR', 'transport', 'Mismatched or duplicate message part identity.')
        ids.add(pid)
        if type(part.get('type')) is not str:
            raise response_error('TRANSPORT_ERROR', 'transport', 'Missing message part type.')
        if part['type'] == 'text' and type(part.get('text')) is not str:
            raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid text part snapshot.')
        if part['type'] == 'tool':
            if any(type(part.get(key)) is not str or not part[key] for key in ('tool', 'callID')):
                raise response_error('TRANSPORT_ERROR', 'transport', 'Missing tool identity.')
            state = object_value(part.get('state'), 'tool state')
            object_value(state.get('input'), 'tool input')
            if state.get('status') not in ('pending', 'running', 'completed', 'error'):
                raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid tool state status.')
            if state.get('status') in ('pending', 'running'):
                raise response_error('INCOMPLETE_OUTPUT', 'result', 'Response still contains an unfinished tool call.')
            if part.get('tool') == 'StructuredOutput' and state.get('status') == 'completed':
                timing = object_value(state.get('time'), 'tool time')
                if any(type(timing.get(k)) not in (int, float) for k in ('start', 'end')):
                    raise response_error('TRANSPORT_ERROR', 'transport', 'Missing tool completion time.')
                if any(type(state.get(k)) is not str for k in ('output', 'title')):
                    raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid completed tool metadata.')
                object_value(state.get('metadata'), 'tool metadata')
                structured_calls.append(part)
    if 'structured' not in info:
        raise response_error('INCOMPLETE_OUTPUT', 'result', 'No native structured result; text is not a substitute.')
    if not structured_calls:
        raise response_error('INCOMPLETE_OUTPUT', 'result', 'No completed StructuredOutput tool call.')
    if type(info['structured']) is dict and not any(p['state']['input'] == info['structured'] for p in structured_calls):
        raise response_error('TRANSPORT_ERROR', 'transport', 'Native result differs from its completed tool input.')
    # Its type and contents are checked by validate_result, independently of upstream.
    return info['structured'], {'session_id': session_id, 'request_id': request_id,
        'message_id': mid, 'finish_reason': info['finish'], 'model_actual': info['providerID'] + '/' + info['modelID'],
        'usage': tokens, 'native_retries_reported': None,
        'structured_tool_calls_in_final_message': len(structured_calls)}


def validate_history(messages, session_id, request_id, final_id=None):
    if type(messages) is not list:
        raise response_error('TRANSPORT_ERROR', 'transport', 'Expected session message list.')
    seen = set()
    assistants = []
    for message in messages:
        info = object_value(object_value(message, 'session message').get('info'), 'session message info')
        mid = identifier(info.get('id'), 'msg')
        if mid in seen or info.get('sessionID') != session_id or type(message.get('parts')) is not list:
            raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid or foreign session message.')
        seen.add(mid)
        if info.get('role') == 'assistant':
            if info.get('parentID') != request_id:
                raise response_error('TRANSPORT_ERROR', 'transport', 'Session contains a response to another request.')
            if 'error' in info:
                native_error(info['error'])
            assistants.append(mid)
        elif info.get('role') != 'user' or mid != request_id:
            raise response_error('TRANSPORT_ERROR', 'transport', 'Unexpected session message role/request.')
        for part in message['parts']:
            object_value(part, 'history part')
            identifier(part.get('id'), 'prt')
            if part.get('sessionID') != session_id or part.get('messageID') != mid:
                raise response_error('TRANSPORT_ERROR', 'transport', 'Foreign part in session history.')
    if final_id is not None and (not assistants or assistants[-1] != final_id):
        raise response_error('INCOMPLETE_OUTPUT', 'result', 'Returned result is not the latest assistant response.')


def prepare_environment(env, stage):
    try:
        config = strict_json(env.get('OPENCODE_CONFIG_CONTENT') or '{}')
    except ContractError as exc:
        raise incompatible('OPENCODE_CONFIG_CONTENT must contain a JSON object.') from exc
    if type(config) is not dict or type(config.get('agent', {})) is not dict:
        raise incompatible('OPENCODE_CONFIG_CONTENT and its agent field must be JSON objects.')
    permission = {'*': 'deny'}
    if stage != 'compare':
        permission.update(read='allow', glob='allow', grep='allow', list='allow')
    # v1.2.27 LLM.resolveTools filters this exact, case-sensitive tool name.
    permission['StructuredOutput'] = 'allow'
    name = 'architecture-audit-' + secrets.token_hex(16)
    config.setdefault('agent', {})[name] = {'mode': 'primary', 'permission': permission}
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
    return name


class Request:
    """A cancellable HTTP operation; watchdog also bounds slow/dripping headers."""
    def __init__(self, port, authorization, method, path, body=None, timeout=HTTP_SECONDS):
        self.connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
        self.socket = None
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.status = None
        self.body = bytearray()
        self.error = None
        self.started = time.monotonic()
        def run():
            try:
                self.connection.connect()
                self.socket = self.connection.sock
                if self.cancelled.is_set():
                    return
                payload = None if body is None else json.dumps(body).encode()
                self.connection.request(method, path, payload,
                    {'Authorization': authorization, 'Content-Type': 'application/json', 'Connection': 'close'})
                response = self.connection.getresponse()
                self.status = response.status
                while block := response.read1(65536):
                    self.body.extend(block)
            except Exception as exc:
                self.error = exc
            finally:
                self.connection.close()
                self.done.set()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def close(self):
        self.cancelled.set()
        connection = self.connection
        if self.socket:
            with contextlib.suppress(OSError):
                self.socket.shutdown(socket.SHUT_RDWR)
        connection.close()
        self.thread.join(timeout=.2)


class Server:
    def __init__(self, executable, cwd, env, artifacts, budget, save, meta):
        self.executable, self.cwd, self.env = executable, cwd, env.copy()
        self.artifacts, self.budget, self.save, self.meta = artifacts, budget, save, meta
        self.process = None
        self.session_id = None
        self.requests = []
        self.files = []
        self.counter = 0
        self.authorization = ''

    def start(self):
        password = secrets.token_urlsafe(32)
        self.env.update(OPENCODE_SERVER_PASSWORD=password, OPENCODE_SERVER_USERNAME='opencode')
        self.authorization = 'Basic ' + base64.b64encode(('opencode:' + password).encode()).decode()
        # A free explicit port avoids the CLI's port=0 preference for 4096. A bind
        # race fails the owned CLI's listener announcement; we never attach to
        # another server merely because it answers health requests on that port.
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        for name in ('server.stdout.log', 'server.stderr.log'):
            fd = os.open(self.artifacts / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.files.append(os.fdopen(fd, 'wb', buffering=0))
        self.budget.check()
        self.process = subprocess.Popen([self.executable, 'serve', '--hostname', '127.0.0.1',
            '--port', str(self.port), '--mdns', 'false'], cwd=self.cwd, env=self.env,
            stdin=subprocess.DEVNULL, stdout=self.files[0], stderr=self.files[1],
            start_new_session=True, close_fds=True)
        startup_end = min(self.budget.deadline, time.monotonic() + STARTUP_SECONDS)
        while True:
            self.budget.check()
            if time.monotonic() >= startup_end:
                raise response_error('STAGE_TIMEOUT', 'execution', 'OpenCode server readiness timed out.')
            if self.process.poll() is not None:
                raise response_error('BACKEND_ERROR', 'backend', 'Owned OpenCode server exited during startup.')
            announcement = f'opencode server listening on http://127.0.0.1:{self.port}'.encode()
            with (self.artifacts / 'server.stdout.log').open('rb') as stream:
                announced = announcement in stream.read(1024 * 1024).splitlines()
            if not announced:
                time.sleep(.05)
                continue
            try:
                health = self.request('GET', '/global/health', budget=Budget(max(.01, startup_end - time.monotonic())))
            except ConnectionRefusedError:
                time.sleep(.05)
                continue
            object_value(health, 'health response')
            if health.get('healthy') is not True or health.get('version') != WIRE_VERSION:
                raise incompatible('Owned OpenCode server returned an incompatible API version.')
            self.meta['api_version'] = health['version']
            return

    def begin(self, method, path, body=None, *, timeout=HTTP_SECONDS):
        self.counter += 1
        request = Request(self.port, self.authorization, method, path, body, timeout=timeout)
        request.number = self.counter
        self.requests.append(request)
        return request

    def finish(self, request):
        if request in self.requests:
            self.requests.remove(request)
        raw = bytes(request.body)
        self.save(self.artifacts / f'response-{request.number:03d}.json', raw)
        self.meta.setdefault('http_responses', []).append({'artifact': f'response-{request.number:03d}.json',
            'status': request.status, 'bytes': len(raw)})
        if request.error:
            if isinstance(request.error, ConnectionRefusedError):
                raise request.error
            raise response_error('BACKEND_ERROR', 'backend', 'OpenCode HTTP operation failed.') from request.error
        if request.status not in (200, 204):
            raise response_error('BACKEND_ERROR', 'backend', 'OpenCode HTTP API returned an error.', http_status=request.status)
        if request.status == 204:
            return None
        if not raw:
            raise response_error('INCOMPLETE_OUTPUT', 'result', 'OpenCode HTTP operation returned no final result.')
        return transport_json(raw)

    def request(self, method, path, body=None, *, budget=None, cleanup=False):
        budget = budget or self.budget
        budget.check()
        request = self.begin(method, path, body)
        try:
            while not request.done.wait(.02):
                budget.check()
                if not cleanup:
                    self.budget.check()
                if time.monotonic() - request.started >= HTTP_SECONDS:
                    raise response_error('STAGE_TIMEOUT', 'execution', 'OpenCode HTTP operation exceeded its time limit.')
            budget.check()
            return self.finish(request)
        finally:
            request.close()

    def verify_api(self):
        spec = object_value(self.request('GET', '/doc'), 'OpenAPI document')
        try:
            props = spec['paths']['/session/{sessionID}/message']['post']['requestBody']['content']['application/json']['schema']['properties']
            schemas = spec['components']['schemas']
            assert props['format']['$ref'] == '#/components/schemas/OutputFormat'
            assert 'structured' in schemas['AssistantMessage']['properties']
            assert schemas['OutputFormatJsonSchema']['properties']['type']['const'] == 'json_schema'
            assert 'retryCount' in schemas['OutputFormatJsonSchema']['properties']
            for path in ('/session', '/session/{sessionID}/abort', '/session/{sessionID}/message'):
                assert 'post' in spec['paths'][path]
            assert 'delete' in spec['paths']['/session/{sessionID}']
        except (KeyError, TypeError, AssertionError):
            raise incompatible('OpenCode /doc does not match the inspected structured-output interface.') from None

    def invoke(self, prompt, schema, agent_name, model, retries):
        created = object_value(self.request('POST', '/session', {}), 'session response')
        self.session_id = identifier(created.get('id'), 'ses')
        request_id = 'msg_' + f'{int(time.time() * 1000) * 4096:012x}' + secrets.token_hex(7)
        self.meta.update(session_id=self.session_id, request_id=request_id)
        body = {'messageID': request_id, 'agent': agent_name,
                'format': {'type': 'json_schema', 'schema': schema, 'retryCount': retries},
                'parts': [{'type': 'text', 'text': prompt}]}
        if model is not None:
            if '/' not in model or not all(model.split('/', 1)):
                raise incompatible('OpenCode model must have provider/model syntax.')
            provider, model_id = model.split('/', 1)
            body['model'] = {'providerID': provider, 'modelID': model_id}
        self.save(self.artifacts / 'request.json', json.dumps(body))
        self.budget.check()
        pending = self.begin('POST', f'/session/{self.session_id}/message', body,
                             timeout=self.budget.deadline - self.budget.clock())
        # Prompt's synchronous response confirms the native loop returned. Polling
        # is observational, never a second prompt or an outer repair loop.
        fingerprint = None
        while not pending.done.wait(.1):
            self.budget.check()
            if self.process.poll() is not None:
                raise response_error('BACKEND_ERROR', 'backend', 'Owned OpenCode server exited during the request.')
            messages = self.request('GET', f'/session/{self.session_id}/message')
            validate_history(messages, self.session_id, request_id)
            current = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).digest()
            if current != fingerprint and messages:
                self.budget.activity()
            fingerprint = current
        self.budget.check()
        self.meta['output_bytes'] = len(pending.body)
        envelope = self.finish(pending)
        self.save(self.artifacts / 'response.json', bytes(pending.body))
        info = envelope.get('info') if type(envelope) is dict else None
        if type(info) is dict:
            # Private metadata, never rendered as a provider error message.
            for source, target in (('id', 'message_id'), ('finish', 'finish_reason')):
                if type(info.get(source)) is str:
                    self.meta[target] = info[source]
            if type(info.get('providerID')) is str and type(info.get('modelID')) is str:
                self.meta['model_actual'] = info['providerID'] + '/' + info['modelID']
            error = info.get('error')
            if type(error) is dict and error.get('name') == 'StructuredOutputError':
                detail = error.get('data')
                if type(detail) is dict and type(detail.get('retries')) in (int, float):
                    self.meta['native_retries_reported'] = detail['retries']
        if type(info) is dict and 'structured' in info:
            self.save(self.artifacts / 'extracted.json', json.dumps(info['structured']))
        data, details = extract_result(envelope, self.session_id, request_id, agent_name)
        history = self.request('GET', f'/session/{self.session_id}/message')
        validate_history(history, self.session_id, request_id, details['message_id'])
        self.meta.update(details, output_bytes=len(pending.body))
        return data

    def close(self):
        errors = []
        cleanup = Budget(CLEANUP_SECONDS)
        try:
            for request in list(self.requests):
                try:
                    request.close()
                    self.save(self.artifacts / f'response-{request.number:03d}.partial', bytes(request.body))
                except Exception:
                    errors.append('request_cleanup_failed')
            if self.session_id and self.process:
                if self.process.poll() is not None:
                    errors.append('session_cleanup_unavailable')
                else:
                    for method, suffix in (('POST', '/abort'), ('DELETE', '')):
                        try:
                            result = self.request(method, '/session/' + self.session_id + suffix, budget=cleanup, cleanup=True)
                            if result is not True:
                                errors.append('session_cleanup_not_confirmed')
                        except BaseException:
                            errors.append('session_abort_failed' if suffix else 'session_delete_failed')
        finally:
            if self.process:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    errors.append('process_group_stop_failed')
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    errors.append('process_wait_failed')
            for stream in self.files:
                with contextlib.suppress(OSError):
                    stream.close()
            self.meta['cleanup_errors'] = errors
        if errors:
            raise response_error('BACKEND_ERROR', 'cleanup', 'Owned OpenCode resources could not be fully cleaned up.')
