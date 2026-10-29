# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline Codex catalog/login/probe fixture; never reads real credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

state = json.loads(Path(os.environ['EXPLAIN_TEST_MODELS_STATE']).read_text())
args = sys.argv[1:]
with Path(os.environ['EXPLAIN_TEST_MODELS_CALLS']).open('a') as stream:
    stream.write(json.dumps({'args': args, 'cwd': os.getcwd(), 'pid': os.getpid()}) + '\n')
kind = ('catalog' if args == ['debug', 'models'] else 'login' if args == ['login', 'status'] else
        'limits' if args == ['app-server', '--stdio'] else 'probe')
if state.get('delay_' + kind):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) if state.get('spawn_child') else None
    marker = Path(state['marker'])
    temporary = marker.with_name(marker.name + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps({'pid': os.getpid(), 'child': child.pid if child else None}))
    temporary.replace(marker)
    time.sleep(state['delay_' + kind])
if kind == 'limits':
    initialized = False
    for line in sys.stdin:
        request = json.loads(line)
        if state.get('rpc_log'):
            with Path(state['rpc_log']).open('a') as stream:
                stream.write(json.dumps(request) + '\n')
        method = request['method']
        if method == 'initialize':
            print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
        elif method == 'initialized':
            initialized = True
        else:
            assert initialized and method == 'account/rateLimits/read', request
            if state.get('limits_exit'):
                sys.exit(1)
            if state.get('limits_notification'):
                print(json.dumps({'method': 'account/rateLimits/updated', 'params': {}}), flush=True)
                print(json.dumps({'id': 999, 'result': {}}), flush=True)
            response = {'id': request['id'], 'result': state.get('limits', {'rateLimits': {
                'limitId': 'codex',
                'primary': {'usedPercent': 25, 'windowDurationMins': 300, 'resetsAt': 1793286000},
                'secondary': {'usedPercent': 50, 'windowDurationMins': 10080, 'resetsAt': 1793804400}}})}
            if state.get('limits_error'):
                response = {'id': request['id'], 'error': {'code': -32000, 'message': state['limits_error']}}
            print(state.get('limits_output', json.dumps(response)), flush=True)
    sys.exit(0)
if kind == 'catalog':
    print(state.get('catalog_output', json.dumps({'models': state['models']})))
    sys.exit(state.get('catalog_exit', 0))
if kind == 'login':
    print(state.get('login', 'Logged in using ChatGPT'), file=sys.stderr)
    sys.exit(state.get('login_exit', 0))
status = state.get('probe', 'available')
if status == 'unavailable':
    print(json.dumps({'type': 'error', 'message': "The model is not supported when using Codex with a ChatGPT account"}))
    sys.exit(1)
if status == 'error':
    print('Connection failed.', file=sys.stderr)
    sys.exit(1)
if status == 'malformed':
    print('not JSON')
    sys.exit(0)
if status == 'failed_event':
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'Provider failed.'}}))
    sys.exit(0)
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': state.get('reply', 'OK')}}))
if status != 'incomplete':
    print(json.dumps({'type': 'turn.completed'}))
