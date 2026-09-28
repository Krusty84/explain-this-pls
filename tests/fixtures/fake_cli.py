# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Deterministic subprocess fixture; never contacts a model or reads real credentials."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def inspect_sources(context):
    expected_path = os.environ.get('AUDIT_TEST_EXPECTED')
    expected = (json.loads(Path(expected_path).read_text())[context['branch']] if expected_path else
                {'.': {'commit': context['source_commit']}})
    observed = {}
    for relative, record in expected.items():
        path = Path(context['repository']) / relative
        # The fixture makes its own explicit read authorization, independently of
        # runner trust. Never import runner Git configuration into the child.
        with tempfile.TemporaryDirectory(prefix='fake-cli-git-', dir='/tmp') as neutral:
            config = Path(neutral) / 'config'
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(config))
            subprocess.run(['git', 'config', '--file', str(config), 'safe.directory', str(path)],
                           cwd=neutral, env=env, check=True)
            cmd = ['git', '-C', str(path)]
            sha = subprocess.check_output(cmd + ['rev-parse', 'HEAD'], env=env).decode().strip()
            ref = subprocess.run(cmd + ['symbolic-ref', '-q', 'HEAD'], env=env, capture_output=True)
            expected_content = record.get('content')
            if expected_content is None:
                expected_content = subprocess.check_output(cmd + ['show', record['commit'] + ':app.py'], env=env).decode()
        assert sha == record['commit'], (relative, sha, record)
        assert ref.returncode == 1, (relative, ref.stdout)
        content = (path / 'app.py').read_text()
        assert content == expected_content, (relative, content, record)
        if relative != '.':
            supplied = next(item for item in context['submodules'] if item['path'] == relative)
            assert supplied['expected_commit'] == sha
            assert supplied['actual'] == {'commit': sha, 'ref': None, 'clean': True}
        observed[relative] = {'commit': sha, 'content': content}
    call['observed'] = observed


def action(stage):
    spec = json.loads(os.environ.get('AUDIT_TEST_ACTION', '{}'))
    if spec.get('stage') != stage:
        return
    path = Path(spec.get('path', '.'))
    kind = spec['kind']
    if kind == 'file':
        (path / 'app.py').write_text('external modification\n')
    elif kind == 'attach':
        subprocess.run(['git', '-C', str(path), 'switch', '-c', 'external-branch'], check=True, capture_output=True)
    elif kind == 'metadata':
        gitdir = Path(subprocess.check_output(['git', '-C', str(path), 'rev-parse', '--absolute-git-dir']).decode().strip())
        (gitdir / 'config').write_text((gitdir / 'config').read_text() + '\n[external]\n changed = true\n')
    elif kind == 'error':
        sys.exit(17)
    elif kind == 'interrupt':
        os.kill(os.getppid(), signal.SIGTERM)
        # Keep the invocation alive until the parent stops its process group.
        time.sleep(3)
    elif kind in ('wait', 'active', 'closed-pipes'):
        if kind == 'active':
            print('private CLI activity', file=sys.stderr, flush=True)
        if kind == 'closed-pipes':
            os.close(1)
            os.close(2)
        time.sleep(spec.get('seconds', 0.5))
    elif kind == 'invalid':
        print('{"completion_status": "' + spec['value'] + '"}', flush=True)
        sys.exit(0)

args = sys.argv[1:]
profile = json.loads((Path(os.environ['HOME']) / 'audit-profile.json').read_text())
assert profile['model'] == 'configured-model'
assert not any(key.endswith('_API_KEY') for key in os.environ)
call = {'args': args, 'cwd': str(Path.cwd()), 'home': os.environ['HOME'],
        'euid': os.geteuid(),
        'git_config_env': {k: v for k, v in os.environ.items() if k.startswith('GIT_CONFIG')}}
if '--version' in args:
    action('check')
    print('Fixture startup warning', file=sys.stderr)
    print('fixture-cli 1.0')
elif '--help' in args:
    print('--ephemeral --output-schema --sandbox --skip-git-repo-check --no-session-persistence --json-schema '
          '--tools --allowedTools --disallowedTools --permission-mode --format --model --agent')
else:
    prompt = sys.stdin.read()
    raw = prompt.split('# Authoritative orchestration context (data)\n', 1)[1]
    context_text, schema_text = raw.split('\n\n# Required final JSON Schema\n', 1)
    context = json.loads(context_text)
    call['context'] = context
    call['schema'] = json.loads(schema_text)
    if '--output-schema' in args:
        assert json.loads(Path(args[args.index('--output-schema') + 1]).read_text()) == call['schema']
    elif '--json-schema' in args:
        assert json.loads(args[args.index('--json-schema') + 1]) == call['schema']
    stage = 'compare' if 'baseline_branch' in context else 'review' if 'architecture_document' in context else 'document'
    if stage != 'compare' and context.get('source_mode') == 'git':
        inspect_sources(context)
    action(stage)
    data = {'completion_status': 'COMPLETE',
            'report_markdown': '# Report: configured-model\nC-001\n', 'limitations': []}
    if os.environ.get('AUDIT_TEST_PARTIAL') and stage == 'document':
        data.update(completion_status='PARTIAL', limitations=['Fixture coverage is incomplete.'])
    if 'baseline_branch' in context:
        data.update(task='architecture_comparison', baseline_branch=context['baseline_branch'],
                    baseline_commit=context['baseline_commit'], unresolved_branches=[], differences=[],
                    compared_branches=[b for b in context['requested_branches'] if b != context['baseline_branch']])
    else:
        if context.get('source_mode') == 'folder':
            data.update(source_directory=context['source_directory'],
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
