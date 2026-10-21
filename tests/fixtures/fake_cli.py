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
        # Private global scope also works with Git builds that ignore command-scope trust.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir='/tmp') as trusted:
            value = str(path.resolve()).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t').replace('\b', '\\b')
            assert not str(path.resolve()).endswith('/*')
            trusted.write('[safe]\n\tdirectory =\n\tdirectory = "' + value + '"\n')
            trusted.flush()
            env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_CONFIG')}
            env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=trusted.name)
            commit = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], env=env).decode().strip()
            assert commit == record['commit'], (relative, commit, record)
            assert subprocess.run(['git', '-C', str(path), 'symbolic-ref', '-q', 'HEAD'],
                                  env=env, capture_output=True).returncode == 1
            names = subprocess.check_output(['git', '-C', str(path), 'ls-files', '-z'], env=env).split(b'\0')
        content = (path / 'app.py').read_text()
        if 'content' in record:
            assert content == record['content'], (relative, content, record)
        observed[relative] = {'commit': commit, 'content': content}
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
        'pid': os.getpid(),
        'euid': os.geteuid(),
        'git_config_env': {k: v for k, v in os.environ.items() if k.startswith('GIT_CONFIG')}}
if '--version' in args:
    action('check')
    print('Fixture startup warning', file=sys.stderr)
    print(os.environ.get('AUDIT_TEST_CLI_VERSION', 'fixture-cli 1.0'))
elif '--help' in args:
    print('--ephemeral --output-schema --sandbox --json --output-last-message --skip-git-repo-check --model '
          '--ignore-user-config --ignore-rules')
else:
    prompt = sys.stdin.read()
    raw = prompt.split('# Authoritative orchestration context (data)\n', 1)[1]
    context_text, schema_text = raw.split('\n\n# Required final JSON Schema\n', 1)
    context = json.loads(context_text)
    call['context'] = context
    call['prompt'] = prompt
    call['schema'] = json.loads(schema_text)
    if '--output-schema' in args:
        assert json.loads(Path(args[args.index('--output-schema') + 1]).read_text()) == call['schema']
    stage = context.get('stage') or ('compare' if 'baseline_branch' in context else
                                    'review' if 'architecture_document' in context else 'study')
    if stage != 'compare' and context.get('phase') != 'write' and context.get('source_mode') == 'git':
        inspect_sources(context)
    action(stage)
    sys.path.insert(0, str(Path(__file__).parent))
    sys.dont_write_bytecode = True
    from ledger_response import response
    data = response(context)
    scenario = os.environ.get('AUDIT_FAKE_CASE', '')
    if scenario == 'schema-extra':
        data['extra_private_key'] = ['private value']
    if scenario == 'missing-claims' and stage == 'study' and context.get('phase') != 'write':
        del data['claims']
    if scenario == 'claims-string':
        data['claims'] = '[{"private":"' + 'x' * 21295
    if scenario == 'claims-44' and stage == 'review':
        data['claims'] = [dict(data['claims'][0], id=f'C-{i + 1:03d}', claim_ids=['C-PRIVATE']) for i in range(44)]
        data['report_markdown'] = '\n'.join(claim['id'] for claim in data['claims'])
    if scenario == 'partial-review' and stage == 'review':
        data.update(completion_status='PARTIAL', limitations=['Synthetic incomplete review'])
    if scenario == 'material-review' and stage == 'review':
        data['claims'][0].update(outcome='UNVERIFIABLE', limitation='Insufficient static evidence')
    if scenario == 'schema-error' or (scenario == 'fail-main-study' and context.get('branch') == 'main' and stage == 'study'):
        data['completion_status'] = 'INVALID'
    if scenario == 'wrong-identity':
        data['source_snapshot_id' if 'source_snapshot_id' in data else 'source_commit'] = 'wrong'
    if os.environ.get('AUDIT_FAKE_LOCAL_REFS') and 'claims' in data:
        data['claims'][0]['evidence_ids'] = ['E-001']
    if 'AUDIT_FAKE_CASE' in os.environ:
        with open(os.environ['AUDIT_TEST_CALL_LOG'], 'a') as stream:
            stream.write(json.dumps(call) + '\n')
    if scenario == 'progress-barrier':
        gate = Path(os.environ['AUDIT_FAKE_PROGRESS_GATE'])
        (gate / (stage + '.ready')).touch()
        deadline = time.monotonic() + 12
        while not (gate / (stage + '.release')).exists():
            if time.monotonic() > deadline:
                raise RuntimeError('Progress fixture was not released')
            time.sleep(.01)
    if scenario == 'slow':
        time.sleep(60)
    if stage == 'study' and (os.environ.get('AUDIT_TEST_PARTIAL') or
            context.get('branch') in json.loads(os.environ.get('AUDIT_TEST_PARTIAL_BRANCHES', '[]'))):
        data.update(completion_status='PARTIAL', limitations=['Fixture coverage is incomplete.'])
    if context.get('source_mode') == 'folder' and os.environ.get('AUDIT_TEST_MUTATE_SOURCE'):
        (Path(context['source_directory']) / 'modified.txt').write_text('agent modification')
    from cli_response import cli_result
    sys.stdout.buffer.write(cli_result(args, data)['stdout'])

if 'AUDIT_FAKE_CASE' not in os.environ or '--help' in args or '--version' in args:
    with open(os.environ['AUDIT_TEST_CALL_LOG'], 'a') as stream:
        stream.write(json.dumps(call) + '\n')
