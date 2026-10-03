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


sys.path.insert(0, str(Path(__file__).parent))
sys.dont_write_bytecode = True
from ledger_response import response as result

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
            stage = context.get('stage') or ('compare' if 'baseline_branch' in context else
                                            'review' if 'architecture_document' in context else 'study')
            if scenario == 'progress-barrier':
                # No response/history activity until the parent test observes the UI.
                gate = Path(os.environ['AUDIT_FAKE_PROGRESS_GATE'])
                (gate / (stage + '.ready')).touch()
                deadline = time.monotonic() + 10
                while not (gate / (stage + '.release')).exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Progress test did not release the HTTP response')
                    time.sleep(.01)
            if scenario == 'source-change' and stage != 'catalog':
                (Path.cwd() / 'app.py').write_text('unexpected fixture mutation\n')
            data = result(context)
            if scenario == 'legacy' and stage == 'study':
                del data['claims']
            if stage != 'catalog' and scenario in ('repair-ok', 'repair-invalid', 'repair-semantic', 'repair-timeout'):
                if not repair or scenario == 'repair-invalid':
                    data['extra_private_key'] = ['private value']
                elif scenario == 'repair-semantic':
                    data['source_fingerprint' if 'source_fingerprint' in data else 'source_commit'] = 'wrong'
                elif scenario == 'repair-timeout':
                    time.sleep(2)
            if stage != 'catalog' and scenario in ('repair-verdict', 'repair-evidence'):
                if not repair:
                    data['extra_private_key'] = []
                    if scenario == 'repair-verdict':
                        data['completion_status'] = 'PARTIAL'; data['limitations'] = ['Original partial self-assessment']
                elif scenario == 'repair-evidence':
                    data['evidence'][0]['path'] = 'invented.py'
            if scenario in ('claims-44', 'claims-40', 'claims-empty') and stage == 'review':
                count = 40 if scenario == 'claims-40' else 44
                data['claims'] = [dict(data['claims'][0], id=f'C-{i + 1:03d}',
                    claim_ids=[] if scenario == 'claims-empty' else ['C-PRIVATE']) for i in range(count)]
                data['report_markdown'] = '\n'.join(c['id'] for c in data['claims'])
            if scenario == 'partial-review' and stage == 'review':
                data.update(completion_status='PARTIAL', limitations=['Synthetic incomplete review'])
            if scenario == 'material-review' and stage == 'review':
                data['claims'][0].update(outcome='UNVERIFIABLE', limitation='Insufficient static evidence')
            if scenario == 'wrong-identity' and stage != 'catalog':
                data['source_fingerprint' if 'source_fingerprint' in data else 'source_commit'] = 'wrong'
            if (scenario == 'schema-error' and stage != 'catalog') or (scenario == 'fail-main-study' and
                    context.get('branch') == 'main' and stage == 'study'):
                data['completion_status'] = 'INVALID'
            if stage != 'catalog' and os.environ.get('AUDIT_FAKE_LOCAL_REFS'):
                data['claims'][0]['evidence_ids'] = ['E-001']
            if stage != 'catalog' and os.environ.get('AUDIT_FAKE_SHORT_IDS'):
                namespace = stage
                data['evidence'][0]['id'] = namespace + ':E-1'
                data['claims'][0]['evidence_ids'] = [namespace + ':E-1']
            if scenario == 'claims-string' and stage != 'catalog':
                data['claims'] = '[{"private":"' + 'x' * 21295
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
            if scenario == 'backend-error' and stage != 'catalog':
                info['error'] = {'name': 'APIError', 'data': {'message': 'SECRET_RESPONSE', 'isRetryable': False}}
            if scenario == 'exhausted' and stage != 'catalog':
                info['error'] = {'name': 'StructuredOutputError', 'data': {'message': 'No output', 'retries': 0}}
            if scenario == 'prose-only' and stage != 'catalog':
                del info['structured']
            if scenario == 'no-final' and stage != 'catalog':
                del info['time']['completed']
            if scenario == 'unknown-compare-finish' and 'baseline_branch' in context:
                info['finish'] = 'SYNTHETIC_PRIVATE_UNKNOWN_FINISH'
            if scenario == 'tool-input-mismatch' and stage != 'catalog':
                different = json.loads(json.dumps(data))
                different['claims'][0]['evidence_ids'] = ['study:E-001']
                response['parts'][1]['state']['input'] = different
            if scenario == 'foreign-session' and stage != 'catalog':
                info['sessionID'] = 'ses_foreign'
            if scenario == 'foreign-request' and stage != 'catalog':
                info['parentID'] = 'msg_old'
            messages[:] = [response]
            if scenario in ('slow', 'active') and stage != 'catalog':
                for tick in range(40):
                    if scenario == 'active':
                        response['parts'][0]['text'] += str(tick)
                    time.sleep(.05)
            if scenario == 'malformed' and stage != 'catalog':
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
