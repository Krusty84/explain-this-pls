#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Independent XXX CLI subprocess fixture; no server, provider or credentials."""
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from ledger_response import response, prompt_context
from xxx_protocol import transcript

args = sys.argv[1:]
scenario = os.environ.get('AUDIT_FAKE_CASE', '')
log = Path(os.environ['AUDIT_FAKE_CALLS'])
store = log.parent / (log.stem + '-sessions')
store.mkdir(exist_ok=True)
call = {'args': args, 'cwd': str(Path.cwd()), 'pid': os.getpid()}


def record():
    with log.open('a') as stream:
        stream.write(json.dumps(call) + '\n')


if args == ['--version']:
    record()
    print(os.environ.get('AUDIT_FAKE_VERSION', 'XXX fixture-unknown'))
elif '--help' in args:
    record()
    print('--format --agent --model --title' if args[0] == 'run' else 'sessionID')
    if scenario == 'missing-flags': sys.exit(2)
elif args[0] == 'export':
    record()
    if scenario == 'export-failed': sys.exit(3)
    print((store / (args[1] + '.json')).read_text())
elif args[:2] == ['session', 'delete']:
    record()
    if scenario == 'delete-failed': sys.exit(3)
    (store / (args[2] + '.json')).unlink()
    print('Deleted owned fixture session')
else:
    assert args[0] == 'run'
    assert not any(a in args for a in ('--standalone', '--attach', '--continue', '--session'))
    prompt = sys.stdin.read()
    context = prompt_context(prompt.encode())
    stage = context['stage']
    agent = args[args.index('--agent') + 1]
    config = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
    model = args[args.index('--model') + 1] if '--model' in args else 'fixture/configured-model'
    session = 'ses_' + uuid.uuid4().hex
    call.update(context=context, prompt=prompt, agent=agent, model=model, session_id=session,
                permissions=config['agent'][agent]['permission'], config=config)
    if os.environ.get('AUDIT_FAKE_CAPTURE_SOURCES') and stage != 'compare':
        call['source_files'] = {str(p.relative_to(Path.cwd())): p.read_text(errors='replace')
                               for p in Path.cwd().rglob('*') if p.is_file() and not p.is_symlink()}
    record()
    data = response(context)
    if scenario == 'missing-claims' and stage == 'study': del data['claims']
    if scenario == 'markdown-study' and stage == 'study':
        del data['report_sections']; data['report_markdown'] = '# Unsupported study'
    if scenario == 'schema-extra' and stage != 'catalog':
        data['extra_private_key'] = ['private value']
    if scenario in ('claims-44', 'claims-40', 'claims-empty') and stage == 'review':
        data['claims'] = [dict(data['claims'][0], id=f'C-{i+1:03d}',
                         claim_ids=[] if scenario == 'claims-empty' else ['C-PRIVATE'])
                         for i in range(40 if scenario == 'claims-40' else 44)]
        data['report_markdown'] = '\n'.join(c['id'] for c in data['claims'])
    if scenario == 'partial-review' and stage == 'review':
        data.update(completion_status='PARTIAL', limitations=['Synthetic incomplete review'])
    if scenario == 'material-review' and stage == 'review':
        data['claims'][0].update(outcome='UNVERIFIABLE', limitation='Insufficient static evidence')
    if scenario == 'wrong-identity' and stage != 'catalog':
        data['source_snapshot_id' if 'source_snapshot_id' in data else 'source_commit'] = 'wrong'
    if (scenario == 'schema-error' and stage != 'catalog') or (
            scenario == 'fail-main-study' and context.get('branch') == 'main' and stage == 'study'):
        data['completion_status'] = 'INVALID'
    if scenario == 'claims-string' and stage != 'catalog': data['claims'] = '[{"private":"' + 'x' * 21295
    if stage != 'catalog' and os.environ.get('AUDIT_FAKE_LOCAL_REFS'):
        data['claims'][0]['evidence_ids'] = ['E-001']
    if stage != 'catalog' and os.environ.get('AUDIT_FAKE_SHORT_IDS'):
        data['evidence'][0]['id'] = stage + ':E-1'
        data['claims'][0]['evidence_ids'] = [stage + ':E-1']
    if stage == 'review' and scenario == 'review-bare-unique':
        data['evidence'][0]['id'] = 'E-002'
        data['claims'][0]['evidence_ids'] = ['E-002', 'E-001']
        data['findings'] = [dict(id='F-001', severity='LOW', type='SCOPE_MISMATCH', claim_ids=['C-001'],
            location='C-001', evidence_ids=['E-002'], impact='Synthetic', proposed_correction='Synthetic')]
    chosen = scenario if stage != 'catalog' else ''
    if scenario == 'unknown-compare-finish' and stage != 'compare': chosen = ''
    if chosen in ('invalid-json', 'prose-only', 'fences'):
        data = {'invalid-json': '{broken', 'prose-only': 'Ordinary prose',
                'fences': chr(96)*3 + 'json\n{}\n' + chr(96)*3}[chosen]
    rounds = int(os.environ.get('AUDIT_FAKE_COMPACTIONS', '0'))
    output, exported = transcript(data, session=session, agent=agent, prompt=prompt,
                                  cwd=str(Path.cwd()), model=model, rounds=rounds, scenario=chosen)
    (store / (session + '.json')).write_text(json.dumps(exported))
    if scenario == 'progress-barrier':
        gate = Path(os.environ['AUDIT_FAKE_PROGRESS_GATE'])
        (gate / (stage + '.ready')).touch()
        deadline = time.monotonic() + 12
        while not (gate / (stage + '.release')).exists():
            if time.monotonic() > deadline: raise RuntimeError('Progress fixture was not released')
            time.sleep(.01)
    if scenario == 'source-change' and stage != 'catalog':
        (Path.cwd() / 'app.py').write_text('unexpected fixture mutation\n')
    if chosen in ('slow', 'active', 'interrupt'):
        print(output.splitlines()[0], flush=True)
        if chosen == 'interrupt':
            os.kill(os.getppid(), getattr(signal, os.environ.get('AUDIT_FAKE_SIGNAL', 'SIGTERM')))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if chosen == 'active': print('private CLI activity', file=sys.stderr, flush=True)
            time.sleep(.05)
    print(output, end='', flush=True)
    if chosen == 'exit-error': sys.exit(17)

