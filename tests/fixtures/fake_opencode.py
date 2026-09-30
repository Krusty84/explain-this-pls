#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline wire fixture derived from v1.2.27 OpenAPI; never calls a provider.

This does NOT simulate enforcement of retryCount, which upstream lacks.
Malformed responses are explicitly synthetic, controlled by AUDIT_FAKE_CASE.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import time
import uuid

args = sys.argv[1:]
xxx = os.environ.get('AUDIT_FAKE_BACKEND') == 'xxx'
version = 'XXX fixture-unknown' if xxx else '1.2.27'
if '--version' in args:
    print(version)
    sys.exit(0)
if '--help' in args:
    print('--hostname --port --mdns')
    sys.exit(0)
assert args[0] == 'serve'
assert args[args.index('--hostname') + 1] == '127.0.0.1'
assert args[args.index('--mdns') + 1] == 'false'
port = int(args[args.index('--port') + 1])
authorization = 'Basic ' + base64.b64encode(('opencode:' + os.environ['OPENCODE_SERVER_PASSWORD']).encode()).decode()
session_id = 'ses_' + uuid.uuid4().hex
messages = []
scenario = os.environ.get('AUDIT_FAKE_CASE')


def result(context):
    data = {'completion_status': 'COMPLETE', 'report_markdown': '# Fixture\nC-001', 'limitations': []}
    if 'baseline_branch' in context:
        unresolved = context['required_unresolved_branches']
        data.update(task='architecture_comparison', baseline_branch=context['baseline_branch'],
            baseline_commit=context['baseline_commit'],
            compared_branches=[b for b in context['requested_branches'] if b != context['baseline_branch']],
            unresolved_branches=unresolved, differences=[])
        if unresolved:
            data.update(completion_status='PARTIAL', limitations=['Missing fixture input.'])
    else:
        if context.get('source_mode') == 'folder':
            data.update(source_directory=context['source_directory'], source_fingerprint=context['source_fingerprint'])
        else:
            data.update(branch=context['branch'], source_commit=context['source_commit'])
        if 'architecture_document' in context:
            data.update(task='architecture_review', verdict='PASS', claim_inventory_complete=True,
                claims=[{'id': 'C-001', 'location': 'overview', 'statement': 'Fixture statement',
                         'outcome': 'SUPPORTED', 'evidence': ['app.py:main'], 'limitation': '', 'finding_ids': []}], findings=[])
        else:
            data['task'] = 'architecture_documentation'
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle_request(self):
        if self.path == '/doc':
            if scenario in ('doc-delay', 'doc-hang'):
                time.sleep(float(os.environ.get('AUDIT_FAKE_DOC_DELAY', '60')))
            if scenario == 'doc-drip-headers':
                with __import__('contextlib').suppress(BrokenPipeError, ConnectionResetError):
                    for byte in b'HTTP/1.1 200 OK\r\nX-Slow: ' + b'x' * 100:
                        self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(.04)
                return
            if scenario == 'doc-drip-body':
                self.send_response(200); self.end_headers()
                with __import__('contextlib').suppress(BrokenPipeError, ConnectionResetError):
                    for byte in b'{"slow": "' + b'x' * 100:
                        self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(.04)
                return
        if scenario != 'no-auth' and self.headers.get('Authorization') != authorization:
            self.send_response(401); self.end_headers(); return
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'null')
        call = {'method': self.command, 'path': self.path, 'body': body, 'cwd': str(Path.cwd()), 'server_pid': os.getpid()}
        if isinstance(body, dict) and 'agent' in body:
            call['permissions'] = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])['agent'][body['agent']]['permission']
        log = os.environ.get('AUDIT_FAKE_CALLS')
        if log:
            with open(log, 'a') as stream:
                stream.write(json.dumps(call) + '\n')
        if scenario == 'http-hang':
            time.sleep(60)
        if self.path == '/global/health':
            response = {'healthy': scenario != 'bad-health', 'version': version}
        elif self.path == '/doc':
            response = json.loads((Path(__file__).parent / 'opencode-v1.2.27/openapi.json').read_text())
            if xxx:
                # Reconstruct the user-reported delta independently of the runtime profile.
                schemas = response['components']['schemas']
                schemas['Session']['properties']['compactionCount'] = {'type': 'number'}
                schemas['Session']['required'].append('compactionCount')
                for name, props in [('unattended_retry', {'attempt': {'type': 'number'},
                        'message': {'type': 'string'}, 'next': {'type': 'number'}}),
                        ('queued', {'runningTaskSize': {'type': 'number'}, 'waitingQueueIndex': {'type': 'number'}})]:
                    props['type'] = {'const': name, 'type': 'string'}
                    schemas['SessionStatus']['anyOf'].append({'type': 'object', 'properties': props, 'required': list(props)})
                if scenario == 'bad-api':
                    del schemas['AssistantMessage']['properties']['structured']
            if scenario == 'null-doc':
                response = None
        elif self.path == '/session' and self.command == 'POST':
            response = {'id': session_id, **({'compactionCount': 0} if xxx else {})}
        elif self.path.endswith('/message') and self.command == 'POST':
            if scenario == 'interrupt':
                import signal
                os.kill(os.getppid(), getattr(signal, os.environ.get('AUDIT_FAKE_SIGNAL', 'SIGTERM')))
                time.sleep(60)
            prompt = body['parts'][0]['text']
            repair = prompt.startswith('Correct only the format')
            raw = prompt.split('# Authoritative orchestration context (data)\n', 1)[1]
            context = json.loads(raw.split('\n\n# Required final JSON Schema\n')[0])
            if scenario == 'progress-barrier':
                # No response/history activity until the parent test observes the UI.
                gate = Path(os.environ['AUDIT_FAKE_PROGRESS_GATE'])
                stage = 'review' if 'architecture_document' in context else 'study'
                (gate / (stage + '.ready')).touch()
                deadline = time.monotonic() + 10
                while not (gate / (stage + '.release')).exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Progress test did not release the HTTP response')
                    time.sleep(.01)
            if scenario == 'source-change':
                (Path.cwd() / 'app.py').write_text('unexpected fixture mutation\n')
            data = result(context)
            if scenario in ('repair-ok', 'repair-invalid', 'repair-semantic', 'repair-timeout'):
                if not repair or scenario == 'repair-invalid':
                    data['extra_private_key'] = ['private value']
                elif scenario == 'repair-semantic':
                    data['source_fingerprint' if 'source_fingerprint' in data else 'source_commit'] = 'wrong'
                elif scenario == 'repair-timeout':
                    time.sleep(2)
            if scenario in ('repair-verdict', 'repair-evidence'):
                if not repair:
                    data['extra_private_key'] = []
                    if scenario == 'repair-verdict':
                        data['verdict'] = 'INCONCLUSIVE'
                elif scenario == 'repair-evidence':
                    data['claims'][0]['evidence'] = ['invented.py:fake']
            if scenario in ('claims-44', 'claims-40', 'claims-empty') and 'architecture_document' in context:
                count = 40 if scenario == 'claims-40' else 44
                data['claims'] = [dict(data['claims'][0], id=f'C-{i + 1:03d}',
                    claim_ids=[] if scenario == 'claims-empty' else ['C-PRIVATE']) for i in range(count)]
                data['report_markdown'] = '\n'.join(c['id'] for c in data['claims'])
            if scenario == 'partial-review' and 'architecture_document' in context:
                data.update(completion_status='PARTIAL', verdict='INCONCLUSIVE', limitations=['Synthetic incomplete review'])
            if scenario == 'material-review' and 'architecture_document' in context:
                data['claims'][0].update(outcome='UNVERIFIABLE', limitation='Insufficient static evidence')
            if scenario == 'wrong-identity':
                data['source_fingerprint' if 'source_fingerprint' in data else 'source_commit'] = 'wrong'
            if scenario == 'schema-error' or (scenario == 'fail-main-study' and
                    context.get('branch') == 'main' and 'architecture_document' not in context):
                data['completion_status'] = 'INVALID'
            model = body.get('model', {'providerID': 'fixture', 'modelID': 'configured-model'})
            mid = 'msg_' + uuid.uuid4().hex
            info = {'id': mid, 'sessionID': session_id, 'role': 'assistant',
                    'parentID': body['messageID'], 'agent': body['agent'], 'mode': body['agent'],
                    'time': {'created': 1, 'completed': 2}, 'providerID': model['providerID'],
                    'modelID': model['modelID'], 'path': {'cwd': str(Path.cwd()), 'root': str(Path.cwd())},
                    'cost': 0, 'tokens': {'input': 5, 'output': 5, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}},
                    'finish': 'tool-calls', 'structured': data}
            response = {'info': info, 'parts': [
                {'id': 'prt_text', 'sessionID': session_id, 'messageID': mid, 'type': 'text',
                 'text': 'Ordinary prose must never become the result.'},
                {'id': 'prt_output', 'sessionID': session_id, 'messageID': mid, 'type': 'tool',
                 'tool': 'StructuredOutput', 'callID': 'call_fixture', 'state': {'status': 'completed',
                    'input': data, 'output': 'Structured output captured successfully.', 'title': 'Structured Output',
                    'metadata': {'valid': True}, 'time': {'start': 1, 'end': 2}}}]}
            if scenario == 'backend-error':
                info['error'] = {'name': 'APIError', 'data': {'message': 'SECRET_RESPONSE', 'isRetryable': False}}
            if scenario == 'exhausted':
                info['error'] = {'name': 'StructuredOutputError', 'data': {'message': 'No output', 'retries': 0}}
            if scenario == 'prose-only':
                del info['structured']
            if scenario == 'no-final':
                del info['time']['completed']
            if scenario == 'foreign-session':
                info['sessionID'] = 'ses_foreign'
            if scenario == 'foreign-request':
                info['parentID'] = 'msg_old'
            messages[:] = [response]
            if scenario in ('slow', 'active'):
                for tick in range(40):
                    if scenario == 'active':
                        response['parts'][0]['text'] += str(tick)
                    time.sleep(.05)
            if scenario == 'malformed':
                self.send_response(200); self.end_headers(); self.wfile.write(b'{broken'); return
        elif self.path.endswith('/message'):
            response = messages
        else:
            response = True
        raw = json.dumps(response).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        with __import__('contextlib').suppress(BrokenPipeError):
            self.wfile.write(raw)

    do_GET = do_POST = do_DELETE = handle_request


if scenario == 'not-ready':
    time.sleep(60)
else:
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    brand = 'XXX' if xxx and sys.platform.startswith('linux') else 'opencode'
    print(f'{brand} server listening on http://127.0.0.1:{port}', flush=True)
    server.serve_forever()
