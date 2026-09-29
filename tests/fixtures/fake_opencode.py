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
if '--version' in args:
    print('1.2.27')
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
        unresolved = [b['branch'] for b in context['branches'] if not b.get('study') or not b.get('review')]
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
        if self.headers.get('Authorization') != authorization:
            self.send_response(401); self.end_headers(); return
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'null')
        call = {'method': self.command, 'path': self.path, 'body': body, 'cwd': str(Path.cwd()), 'server_pid': os.getpid()}
        log = os.environ.get('AUDIT_FAKE_CALLS')
        if log:
            with open(log, 'a') as stream:
                stream.write(json.dumps(call) + '\n')
        if scenario == 'http-hang':
            time.sleep(60)
        if self.path == '/global/health':
            response = {'healthy': True, 'version': '1.2.27'}
        elif self.path == '/doc':
            response = json.loads((Path(__file__).parent / 'opencode-v1.2.27/openapi.json').read_text())
        elif self.path == '/session' and self.command == 'POST':
            response = {'id': session_id}
        elif self.path.endswith('/message') and self.command == 'POST':
            if scenario == 'interrupt':
                import signal
                os.kill(os.getppid(), getattr(signal, os.environ.get('AUDIT_FAKE_SIGNAL', 'SIGTERM')))
                time.sleep(60)
            prompt = body['parts'][0]['text']
            raw = prompt.split('# Authoritative orchestration context (data)\n', 1)[1]
            context = json.loads(raw.split('\n\n# Required final JSON Schema\n')[0])
            data = result(context)
            if scenario == 'wrong-identity':
                data['source_fingerprint' if 'source_fingerprint' in data else 'source_commit'] = 'wrong'
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
    print(f'opencode server listening on http://127.0.0.1:{port}', flush=True)
    server.serve_forever()
