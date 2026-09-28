# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Deterministic subprocess fixture; never contacts a model or reads real credentials."""
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
profile = json.loads((Path(os.environ['HOME']) / 'audit-profile.json').read_text())
assert profile['model'] == 'configured-model'
assert not any(key.endswith('_API_KEY') for key in os.environ)
call = {'args': args, 'cwd': str(Path.cwd()), 'home': os.environ['HOME'],
        'euid': os.geteuid(),
        'git_config_env': {k: v for k, v in os.environ.items() if k.startswith('GIT_CONFIG')}}
if '--version' in args:
    print('Fixture startup warning', file=sys.stderr)
    print('fixture-cli 1.0')
elif '--help' in args:
    print('--ephemeral --output-schema --sandbox --skip-git-repo-check --no-session-persistence --json-schema '
          '--tools --allowedTools --disallowedTools --permission-mode --format --model --agent')
else:
    prompt = sys.stdin.read()
    raw = prompt.split('# Authoritative orchestration context (data)\n', 1)[1]
    context = json.loads(raw.split('\n\n# Required final JSON Schema', 1)[0])
    call['context'] = context
    data = {'schema_version': '2.0', 'completion_status': 'COMPLETE',
            'report_markdown': '# Report: configured-model\nC-001\n', 'limitations': []}
    if 'baseline_branch' in context:
        data.update(task='architecture_comparison', baseline_branch=context['baseline_branch'],
                    baseline_commit=context['baseline_commit'], unresolved_branches=[], differences=[],
                    compared_branches=[b for b in context['requested_branches'] if b != context['baseline_branch']])
    else:
        if context.get('source_mode') == 'folder':
            data.update(schema_version='3.0', source_directory=context['source_directory'],
                        source_fingerprint=context['source_fingerprint'])
            # Exercise rejection of a changed tree using an actual subprocess.
            if os.environ.get('AUDIT_TEST_MUTATE_SOURCE'):
                (Path(context['source_directory']) / 'modified.txt').write_text('agent modification')
        else:
            data.update(branch=context['branch'], source_commit=context['source_commit'])
        if 'architecture_document' in context:
            data.update(task='architecture_review', verdict='PASS', claim_inventory_complete=True,
                        claims=[{'id': 'C-001', 'location': 'overview', 'statement': 'Has an entry point',
                                 'outcome': 'SUPPORTED', 'evidence': ['app.py:main'],
                                 'limitation': '', 'finding_ids': []}], findings=[])
        else:
            data['task'] = 'architecture_documentation'
    if '--format' in args:
        config = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
        name = args[args.index('--agent') + 1]
        call['permissions'] = config['agent'][name]['permission']
        assert config['provider']['custom']['options']['baseURL'] == 'https://example.invalid'
        print(json.dumps({'type': 'text', 'sessionID': 'fixture',
                          'part': {'id': 'p1', 'messageID': 'm1', 'text': json.dumps(data)}}))
        print(json.dumps({'type': 'step_finish', 'part': {'messageID': 'm1', 'reason': 'stop'}}))
    elif '--output-format' in args:
        print(json.dumps({'is_error': False, 'structured_output': data}))
    else:
        print(json.dumps(data))

with open(os.environ['AUDIT_TEST_CALL_LOG'], 'a') as stream:
    stream.write(json.dumps(call) + '\n')
