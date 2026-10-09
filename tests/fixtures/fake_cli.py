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
    root = Path(context['repository'])
    assert root == Path.cwd()
    call['source_files'] = {}
    for relative, record in expected.items():
        path = root / relative
        commit = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD']).decode().strip()
        assert commit == record['commit'], (relative, commit, record)
        assert subprocess.run(['git', '-C', str(path), 'symbolic-ref', '-q', 'HEAD'],
                              capture_output=True).returncode == 1
        content = (path / 'app.py').read_text()
        if 'content' in record:
            assert content == record['content'], (relative, content, record)
        observed[relative] = {'commit': commit, 'content': content}
        names = subprocess.check_output(['git', '-C', str(path), 'ls-files', '-z']).split(b'\0')
        for name in names:
            if name:
                source = path / os.fsdecode(name)
                if source.is_file() and not source.is_symlink():
                    call['source_files'][str(source.relative_to(root))] = source.read_text(errors='replace')
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
        if spec.get('process_marker'):
            Path(spec['process_marker']).write_text(json.dumps({'pid': os.getpid(), 'cwd': os.getcwd()}))
        if kind == 'active':
            print('private CLI activity', file=sys.stderr, flush=True)
        if kind == 'closed-pipes':
            os.close(1)
            os.close(2)
        time.sleep(spec.get('seconds', 0.5))
    elif kind == 'invalid':
        from cli_response import cli_result
        sys.stdout.buffer.write(cli_result(sys.argv[1:], {'completion_status': spec['value']})['stdout'])
        sys.stdout.buffer.flush()
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
    print(os.environ.get('AUDIT_TEST_CLI_VERSION', 'fixture-cli 1.0'))
elif '--help' in args:
    print('--ephemeral --output-schema --sandbox --json --output-last-message --skip-git-repo-check --no-session-persistence --json-schema '
          '--tools --allowedTools --disallowedTools --permission-mode --format --model --agent --standalone')
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
    stage = context.get('stage') or ('compare' if 'baseline_branch' in context else
                                    'review' if 'architecture_document' in context else 'study')
    if stage != 'compare' and context.get('source_mode') == 'git':
        inspect_sources(context)
    action(stage)
    sys.path.insert(0, str(Path(__file__).parent))
    sys.dont_write_bytecode = True
    from ledger_response import response
    data = response(context)
    if stage == 'study' and (os.environ.get('AUDIT_TEST_PARTIAL') or
            context.get('branch') in json.loads(os.environ.get('AUDIT_TEST_PARTIAL_BRANCHES', '[]'))):
        data.update(completion_status='PARTIAL', limitations=['Fixture coverage is incomplete.'])
    if context.get('source_mode') == 'folder' and os.environ.get('AUDIT_TEST_MUTATE_SOURCE'):
        (Path(context['source_directory']) / 'modified.txt').write_text('agent modification')
    if '--format' in args:
        config = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
        name = args[args.index('--agent') + 1]
        call['permissions'] = {p['action']: p['effect'] for p in config['agents'][name]['permissions']}
        from cli_response import cli_result
        sys.stdout.buffer.write(cli_result(args, data)['stdout'])
    elif '--output-format' in args:
        print(json.dumps({'is_error': False, 'structured_output': data, 'total_cost_usd': 0.01,
                          'modelUsage': {'fixture-model': {'inputTokens': 60, 'outputTokens': 20,
                              'cacheReadInputTokens': 30, 'cacheCreationInputTokens': 10, 'costUSD': 0.01}}}))
    else:
        from cli_response import cli_result
        sys.stdout.buffer.write(cli_result(args, data)['stdout'])

with open(os.environ['AUDIT_TEST_CALL_LOG'], 'a') as stream:
    stream.write(json.dumps(call) + '\n')
