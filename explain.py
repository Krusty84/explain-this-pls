#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Sequential architecture auditing with configured coding-agent CLIs.

macOS or Linux, Python 3.11+, Git, and one or more authenticated coding-agent CLIs.
No third-party Python packages, worktrees, or source copies.
The parent is the only artifact writer. The agent receives stdin and returns JSON.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any
from contracts import SCHEMAS, ContractError, accepted, parse_backend, strict_json, validate_result

ROOT = Path(__file__).resolve().parent
STAGES = ('document', 'review', 'compare')
ARTIFACTS = {'document': 'ARCHITECTURE.md', 'review': 'ARCHITECTURE_REVIEW.md',
             'compare': 'BRANCH_COMPARISON.md'}
BACKENDS = {'codex': 'codex', 'claude-code': 'claude', 'opencode': 'opencode'}

class AuditError(RuntimeError):
    pass

class UnsafeRepository(AuditError):
    pass

def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents

def overlap(a: Path, b: Path) -> bool:
    return inside(a, b) or inside(b, a)

def atomic(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    blob = data.encode('utf-8') if isinstance(data, str) else data
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()

def save_json(path: Path, value: Any) -> None:
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def slug(branch: str) -> str:
    text = re.sub(r'[^A-Za-z0-9._-]+', '_', branch).strip('._-')[:55] or 'branch'
    return f'{text}--{digest(branch.encode())[:12]}'

def log(text: str) -> None:
    print(f'[{now()}] {text}', file=sys.stderr, flush=True)

def cli_env(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(PWD=str(cwd.resolve()), NO_COLOR='1', GIT_TERMINAL_PROMPT='0')
    return env

def process(command: list[str], cwd: Path, env: dict[str, str], input_data: bytes = b'',
            timeout: int = 60, max_output: int = 16_000_000) -> dict:
    """Nonblocking pipe I/O; do not pass artifact FDs into the CLI child."""
    start = time.monotonic()
    out, err = bytearray(), bytearray()
    problem = None
    p = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True, close_fds=True)
    selector = selectors.DefaultSelector()
    offset = 0
    try:
        for stream, name in ((p.stdout, 'stdout'), (p.stderr, 'stderr')):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if input_data:
            os.set_blocking(p.stdin.fileno(), False)
            selector.register(p.stdin, selectors.EVENT_WRITE, 'stdin')
        else:
            p.stdin.close()
        while selector.get_map():
            if time.monotonic() - start > timeout:
                problem = f'Timeout after {timeout} seconds'
                break
            for key, _ in selector.select(0.2):
                stream, name = key.fileobj, key.data
                if name == 'stdin':
                    try:
                        offset += os.write(stream.fileno(), input_data[offset:offset + 65536])
                    except BrokenPipeError:
                        offset = len(input_data)
                    except BlockingIOError:
                        continue
                    if offset == len(input_data):
                        selector.unregister(stream)
                        stream.close()
                else:
                    try:
                        block = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(stream)
                        stream.close()
                    else:
                        target = out if name == 'stdout' else err
                        target.extend(block)
                        if len(out) + len(err) > max_output:
                            problem = f'Combined CLI output exceeds {max_output} bytes'
                            break
            if problem:
                break
        # A child may close its pipes while still running. Bound that wait as well.
        if not problem:
            try:
                p.wait(timeout=max(0.1, timeout - (time.monotonic() - start)))
            except subprocess.TimeoutExpired:
                problem = f'Timeout after {timeout} seconds'
    finally:
        selector.close()
        # Kill ordinary descendants remaining in this invocation's process group,
        # including on errors/interrupts; detached hostile daemons are not supported.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        for stream in (p.stdin, p.stdout, p.stderr):
            with contextlib.suppress(Exception):
                stream.close()
    return {'stdout': bytes(out), 'stderr': bytes(err), 'returncode': p.returncode,
            'error': problem, 'duration_seconds': round(time.monotonic() - start, 3)}

class Repository:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL') if k in os.environ}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                        GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_PAGER='cat')
        self.prefix = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
            '-c', 'core.untrackedCache=false', '-c', 'submodule.recurse=false',
            '-c', 'core.pager=cat', '-C', str(self.path)]

    def git(self, *args: str, allowed: tuple[int, ...] = (0,)) -> bytes:
        r = subprocess.run(self.prefix + list(args), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if r.returncode not in allowed:
            raise AuditError(f'git {args[0]} failed: {r.stderr.decode(errors="replace").strip()}')
        return r.stdout

    def text(self, *args: str, **kwargs) -> str:
        return self.git(*args, **kwargs).decode('utf-8').strip()

    def head(self) -> str:
        return self.text('rev-parse', 'HEAD')

    def symbolic(self) -> str | None:
        return self.text('symbolic-ref', '--quiet', '--short', 'HEAD', allowed=(0, 1)) or None

    def clean(self) -> None:
        status = self.git('status', '--porcelain=v1', '-z', '--untracked-files=all')
        ignored = self.git('ls-files', '--others', '--ignored', '--exclude-standard', '-z')
        if status or ignored:
            raise UnsafeRepository('Working tree must have no modifications, untracked files, '
                                   'or ignored files. No automatic stash/reset/clean is performed.')
        flags = self.git('ls-files', '-v', '-z').split(b'\0')
        if any(line and (line[:1].islower() or line[:1] == b'S') for line in flags):
            raise UnsafeRepository('assume-unchanged/skip-worktree entries are unsupported.')

    def preflight(self, branches: list[str]) -> dict[str, str]:
        if not (self.path / '.git').is_dir():
            raise AuditError('A normal standalone checkout is required; no bare/linked worktree.')
        if Path(self.text('rev-parse', '--show-toplevel')).resolve() != self.path:
            raise AuditError('repository must point to the checkout root.')
        if self.git('config', '--get-regexp', r'^filter\.', allowed=(0, 1)):
            raise AuditError('Git filters (including Git LFS filters) require a separate supported policy.')
        for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-apply', 'rebase-merge', 'BISECT_START'):
            if (self.path / '.git' / marker).exists():
                raise UnsafeRepository(f'Unfinished Git operation: {marker}')
        if self.text('config', '--bool', '--get', 'core.sparseCheckout', allowed=(0, 1)) == 'true':
            raise AuditError('Sparse checkouts are unsupported.')
        self.clean()
        result = {}
        for name in branches:
            self.git('check-ref-format', 'refs/heads/' + name)
            commit = self.text('rev-parse', '--verify', 'refs/heads/' + name + '^{commit}')
            entries = self.git('ls-tree', '-r', '-z', commit).split(b'\0')
            if any(entry.startswith(b'160000 ') for entry in entries):
                raise AuditError(f'Submodules are unsupported by this version: {name}')
            result[name] = commit
        return result

    def checkout(self, commit: str) -> None:
        self.clean()
        self.git('switch', '--detach', commit)
        self.assert_snapshot(commit)

    def assert_snapshot(self, commit: str) -> None:
        if self.head() != commit or self.symbolic() is not None:
            raise UnsafeRepository('HEAD changed outside the orchestrator.')
        self.clean()

    def restore(self, branch: str | None, commit: str) -> dict:
        self.clean()
        # A concurrent actor may have moved the original branch. Never reset it.
        if branch:
            current_ref = self.text('rev-parse', 'refs/heads/' + branch + '^{commit}')
            if current_ref != commit:
                raise UnsafeRepository('Original branch moved concurrently; refusing automatic restoration.')
            self.git('switch', '--no-guess', branch)
        else:
            self.git('switch', '--detach', commit)
        self.clean()
        if self.head() != commit or self.symbolic() != branch:
            raise UnsafeRepository('Restoration verification failed.')
        return {'restored': True, 'branch': branch, 'commit': commit}

    def delta(self, baseline: str, other: str) -> dict:
        raw = self.git('diff', '--raw', '--no-abbrev', '--no-renames', '--no-ext-diff',
                       '--no-textconv', '-z', baseline, other, '--')
        fields = raw.split(b'\0')
        if fields[-1:] == [b'']:
            fields.pop()
        if len(fields) % 2:
            raise AuditError('Unexpected git diff --raw -z layout')
        changes = []
        for i in range(0, len(fields), 2):
            oldmode, newmode, oldoid, newoid, status = fields[i].decode('ascii').split()
            changes.append({'path': fields[i + 1].decode('utf-8'), 'status': status,
                'old_mode': oldmode.lstrip(':'), 'new_mode': newmode,
                'old_blob': oldoid, 'new_blob': newoid})
        bases = self.text('merge-base', '--all', baseline, other, allowed=(0, 1)).splitlines()
        return {'orientation': 'baseline_tree_to_branch_tree', 'baseline_commit': baseline,
            'branch_commit': other, 'merge_bases': bases, 'changes': changes,
            'identical_trees': self.text('rev-parse', baseline + '^{tree}') ==
                               self.text('rev-parse', other + '^{tree}'),
            'limitations': ['Path/mode/blob changes only; no patch, runtime evidence, or historical rationale.',
                            'Rename detection disabled: renames appear as deletion plus addition.']}

@contextlib.contextmanager
def repository_lock(repo: Path):
    repo = repo.resolve()
    base = Path(tempfile.gettempdir()).resolve() / f'architecture-audit-locks-{os.getuid()}'
    base.mkdir(mode=0o700, exist_ok=True)
    if base.is_symlink() or base.stat().st_uid != os.getuid() or base.stat().st_mode & 0o077:
        raise AuditError('Unsafe lock directory')
    path = base / (digest(str(repo).encode()) + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AuditError('Another architecture audit holds this repository lock.') from exc
        yield
    finally:
        os.close(fd)

def load_config(path: Path) -> dict:
    path = path.resolve()
    value = strict_json(path.read_text())
    if type(value) is not dict:
        raise AuditError('Configuration must be a JSON object.')
    allowed = {'repository', 'reports_dir', 'branches', 'baseline_branch', 'agent', 'stage_agents',
        'output_language', 'priority_scenarios', 'timeout_seconds', 'max_input_bytes',
        'max_output_bytes', 'continue_on_error', 'project_description', 'prompts'}
    if 'additional_runtime_read_paths' in value:
        raise AuditError('Remove additional_runtime_read_paths from configuration: the runner now uses native CLI permissions.')
    if set(value) - allowed:
        raise AuditError(f'Unknown configuration keys: {set(value) - allowed}')
    for key in ('repository', 'reports_dir', 'branches', 'baseline_branch', 'agent'):
        if key not in value:
            raise AuditError(f'Missing configuration key: {key}')
    for key in ('repository', 'reports_dir'):
        p = Path(value[key]).expanduser()
        value[key] = str((path.parent / p if not p.is_absolute() else p).resolve())
    repo, reports = Path(value['repository']), Path(value['reports_dir'])
    if overlap(repo, reports):
        raise AuditError('Repository and reports_dir must be disjoint, not ancestors of each other.')
    branches = value['branches']
    if not isinstance(branches, list) or len(branches) < 2 or any(not isinstance(b, str) or not b for b in branches):
        raise AuditError('At least two nonempty local branch names are required.')
    if len(set(branches)) != len(branches) or value['baseline_branch'] not in branches:
        raise AuditError('Branches must be unique and include baseline_branch.')
    defaults = {'output_language': 'Russian', 'project_description': '', 'priority_scenarios': [], 'timeout_seconds': 1800,
        'max_input_bytes': 800_000, 'max_output_bytes': 16_000_000, 'continue_on_error': True,
        'stage_agents': {}, 'prompts': {}}
    for key, default in defaults.items():
        value.setdefault(key, default)
    if not isinstance(value['project_description'], str):
        raise AuditError('project_description must be a string.')
    value['project_description'] = value['project_description'].strip()
    for key in ('agent', 'stage_agents', 'prompts'):
        if type(value[key]) is not dict:
            raise AuditError(f'{key} must be a JSON object.')
    for key in ('timeout_seconds', 'max_input_bytes', 'max_output_bytes'):
        if type(value[key]) is not int or value[key] <= 0:
            raise AuditError(f'{key} must be a positive integer.')
    if type(value['continue_on_error']) is not bool:
        raise AuditError('continue_on_error must be boolean.')
    if not isinstance(value['priority_scenarios'], list) or any(not isinstance(s, str) for s in value['priority_scenarios']):
        raise AuditError('priority_scenarios must be an array of strings.')
    for stage in value['stage_agents']:
        if stage not in STAGES:
            raise AuditError(f'Unknown stage override: {stage}')
        if type(value['stage_agents'][stage]) is not dict:
            raise AuditError(f'stage_agents.{stage} must be a JSON object.')
    value['_agents'] = {}
    for stage in STAGES:
        agent = dict(value['agent'])
        agent.update(value['stage_agents'].get(stage, {}))
        obsolete = set(agent) & {'api_key_env', 'provider_key_env'}
        if obsolete:
            raise AuditError(f'Remove {", ".join(sorted(obsolete))} from {stage} agent configuration: authenticate using the CLI itself.')
        if set(agent) - {'backend', 'executable', 'model', 'expected_version'}:
            raise AuditError('Unknown agent configuration field.')
        backend = agent.get('backend')
        if backend not in BACKENDS:
            raise AuditError(f'Unsupported backend: {backend}')
        executable_name = agent.get('executable') or BACKENDS[backend]
        if not isinstance(executable_name, str):
            raise AuditError('agent.executable must be a string.')
        if '/' in executable_name:
            executable_path = Path(executable_name).expanduser()
            executable_name = str((path.parent / executable_path).resolve())
        executable = shutil.which(executable_name)
        if not executable:
            raise AuditError(f'{backend} executable was not found. Install it separately.')
        agent['executable'] = str(Path(executable).resolve())
        for key in ('model', 'expected_version'):
            if agent.get(key) is not None and not isinstance(agent[key], str):
                raise AuditError(f'agent.{key} must be a string or null.')
        value['_agents'][stage] = agent
    value['_prompt_paths'] = {}
    for stage in STAGES:
        p = Path(value['prompts'].get(stage, str(ROOT / 'prompts' / (stage + '.md')))).expanduser()
        p = (path.parent / p if not p.is_absolute() else p).resolve()
        if inside(p, repo):
            raise AuditError('Prompt templates must be outside the inspected repository.')
        if not p.is_file():
            raise AuditError(f'Missing prompt template: {p}')
        value['_prompt_paths'][stage] = str(p)
    return value

class Runner:
    def __init__(self, config: dict, run_dir: Path):
        self.cfg, self.run_dir = config, run_dir.resolve()
        self.repo = Repository(Path(config['repository']))
        self.versions: dict[str, str] = {}

    def check_cli(self) -> dict:
        result = {}
        for agent in self.cfg['_agents'].values():
            key = agent['backend'] + ':' + agent['executable']
            if key in result:
                if agent.get('expected_version') and agent['expected_version'] != result[key]['version']:
                    raise AuditError('Conflicting expected_version for the same executable.')
                continue
            backend = agent['backend']
            with tempfile.TemporaryDirectory(prefix='archaudit-cli-check-') as raw:
                state = Path(raw).resolve()
                env = cli_env(state)
                cmds = [[agent['executable'], '--version'],
                        [agent['executable'], *(['exec'] if backend == 'codex' else ['run'] if backend == 'opencode' else []), '--help']]
                texts = []
                for cmd in cmds:
                    r = process(cmd, state, env, timeout=30, max_output=self.cfg['max_output_bytes'])
                    if r['returncode'] or r['error']:
                        raise AuditError(f'{backend} failed its CLI check: '
                                         + (r['error'] or r['stderr'].decode(errors='replace').strip()))
                    # Startup warnings are not part of the version identity.
                    output = r['stdout']
                    if cmd[-1] == '--help':
                        output += r['stderr']
                    elif not output.strip():
                        output = r['stderr']
                    texts.append(output.decode(errors='replace'))
                required = {'codex': ['--ephemeral', '--output-schema', '--sandbox'],
                    'claude-code': ['--no-session-persistence', '--json-schema', '--tools', '--allowedTools', '--disallowedTools', '--permission-mode'],
                    'opencode': ['--format', '--model', '--agent']}[backend]
                if any(flag not in texts[1] for flag in required):
                    raise AuditError(f'{backend} lacks required CLI options: {required}')
                version = texts[0].strip()
                if agent.get('expected_version') and agent['expected_version'] != version:
                    raise AuditError(f'{backend} version differs from expected_version: {version}')
                result[key] = {'version': version, 'required_flags': required}
        self.versions = {k: v['version'] for k, v in result.items()}
        return result

    def command(self, stage: str, state: Path, agent: dict, schema_path: Path, env: dict) -> list[str]:
        backend, exe = agent['backend'], agent['executable']
        compare = stage == 'compare'
        if backend == 'codex':
            cmd = [exe, 'exec', '--ephemeral', '--color', 'never', '--sandbox', 'read-only',
                '--output-schema', str(schema_path), '-c', 'approval_policy="never"',
                '-c', 'web_search="disabled"']
            if compare:
                cmd += ['--skip-git-repo-check', '-c', 'features.shell_tool=false']
            if agent.get('model'):
                cmd += ['--model', agent['model']]
            return cmd + ['-']
        if backend == 'claude-code':
            tools = '' if compare else 'Read,Glob,Grep'
            cmd = [exe, '-p', '--no-session-persistence', '--output-format', 'json',
                '--json-schema', json.dumps(SCHEMAS[stage]), '--permission-mode', 'dontAsk',
                '--tools', tools, '--disallowedTools', 'mcp__*']
            if tools:
                cmd += ['--allowedTools', tools]
            if agent.get('model'):
                cmd += ['--model', agent['model']]
            return cmd
        permissions = {'*': 'deny'}
        if not compare:
            permissions.update(read='allow', glob='allow', grep='allow', list='allow')
        # An invocation-local agent takes precedence over global permissions without
        # changing the user's providers, authentication plugins, or configuration files.
        try:
            config = strict_json(env.get('OPENCODE_CONFIG_CONTENT') or '{}')
        except ContractError as exc:
            raise AuditError('OPENCODE_CONFIG_CONTENT must contain a JSON object.') from exc
        if type(config) is not dict or type(config.get('agent', {})) is not dict:
            raise AuditError('OPENCODE_CONFIG_CONTENT and its agent field must be JSON objects.')
        name = 'architecture-audit-' + uuid.uuid4().hex
        config.setdefault('agent', {})[name] = {'mode': 'primary', 'permission': permissions}
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
        cmd = [exe, 'run', '--format', 'json', '--agent', name]
        if agent.get('model'):
            cmd += ['--model', agent['model']]
        return cmd

    def invoke(self, stage: str, context: dict, destination: Path) -> tuple[dict, dict]:
        agent = self.cfg['_agents'][stage]
        template = Path(self.cfg['_prompt_paths'][stage]).read_text()
        # Assemble only this stage's inputs; the configured CLI profile remains available.
        prompt = (template + '\n\n# Authoritative orchestration context (data)\n' +
                  json.dumps(context, ensure_ascii=False) + '\n\n# Required final JSON Schema\n' +
                  json.dumps(SCHEMAS[stage], ensure_ascii=False))
        payload = prompt.encode('utf-8')
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        meta = {'stage': stage, 'invocation_id': str(uuid.uuid4()), 'started_at': now(),
            'backend': agent['backend'], 'executable': agent['executable'],
            'model_requested': agent.get('model'), 'cli_version': self.versions.get(agent['backend'] + ':' + agent['executable']),
            'prompt_sha256': digest(payload), 'template_sha256': digest(template.encode()),
            'schema_sha256': digest(json.dumps(SCHEMAS[stage], sort_keys=True).encode()),
            'input_bytes': len(payload), 'status': 'RUNNING'}
        save_json(destination / 'invocation.json', meta)
        # Saved by the parent only, for reproducibility; includes only this stage's permitted input.
        atomic(destination / 'input.prompt.txt', payload)
        if len(payload) > self.cfg['max_input_bytes']:
            meta.update(status='FAILED', error='Input exceeds max_input_bytes; no truncation or model call.', finished_at=now())
            save_json(destination / 'invocation.json', meta)
            raise AuditError(meta['error'])
        try:
            with tempfile.TemporaryDirectory(prefix='archaudit-invocation-') as raw:
                state = Path(raw).resolve()
                cwd = state if stage == 'compare' else self.repo.path
                env = cli_env(cwd)
                schema_path = state / 'output.schema.json'
                save_json(schema_path, SCHEMAS[stage])
                cmd = self.command(stage, state, agent, schema_path, env)
                r = process(cmd, cwd, env, payload, self.cfg['timeout_seconds'], self.cfg['max_output_bytes'])
                atomic(destination / 'stdout.log', r['stdout'])
                atomic(destination / 'stderr.log', r['stderr'])
                meta.update(returncode=r['returncode'], duration_seconds=r['duration_seconds'])
                if r['returncode'] or r['error']:
                    raise AuditError(r['error'] or f'{agent["backend"]} exited with {r["returncode"]}; see stderr.log')
                data, provider_meta = parse_backend(agent['backend'], r['stdout'].decode('utf-8'))
                validate_result(stage, data, context)
                meta['provider_metadata'] = provider_meta
            # Publish after the CLI exits and temporary invocation files are removed.
            report = data['report_markdown'].rstrip() + '\n'
            atomic(destination.parent / ARTIFACTS[stage], report)
            save_json(destination.parent / (stage + '.json'), data)
            meta.update(status='SUCCEEDED', finished_at=now(), report_sha256=digest(report.encode()))
            save_json(destination / 'invocation.json', meta)
            return data, meta
        except BaseException as exc:
            meta.update(status='FAILED', finished_at=now(), error=str(exc) or type(exc).__name__)
            save_json(destination / 'invocation.json', meta)
            raise

    def run(self, check_only: bool = False) -> tuple[dict, int]:
        manifest = {'schema_version': '2.0', 'run_id': self.run_dir.name,
            'started_at': now(), 'repository': str(self.repo.path),
            'baseline_branch': self.cfg['baseline_branch'], 'status': 'RUNNING',
            'isolation': 'cli-native-permissions', 'platform': sys.platform,
            'branches': [], 'errors': []}
        def persist():
            save_json(self.run_dir / 'manifest.json', manifest)
        persist()
        original_branch = original_commit = None
        restored = False
        try:
            if not self.cfg['project_description']:
                log('Описание проекта не указано. Рекомендуем заполнить project_description: '
                    'кратко расскажите о назначении и истории системы')
            pins = self.repo.preflight(self.cfg['branches'])
            manifest['pins'] = pins
            original_branch, original_commit = self.repo.symbolic(), self.repo.head()
            manifest['original_checkout'] = {'branch': original_branch, 'commit': original_commit}
            manifest['cli_checks'] = self.check_cli()
            persist()
            if check_only:
                manifest.update(status='PREFLIGHT_OK', finished_at=now())
                persist()
                return manifest, 0
            try:
                for branch in self.cfg['branches']:
                    commit = pins[branch]
                    log(f'Analyzing {branch} at {commit[:12]}')
                    branch_dir = self.run_dir / 'branches' / slug(branch)
                    item: dict[str, Any] = {'branch': branch, 'source_commit': commit,
                        'directory': str(branch_dir.relative_to(self.run_dir)),
                        'document': None, 'review': None, 'errors': []}
                    manifest['branches'].append(item)
                    persist()
                    self.repo.checkout(commit)
                    context = {'branch': branch, 'source_commit': commit,
                        'repository': str(self.repo.path), 'output_language': self.cfg['output_language'],
                        'project_description': self.cfg['project_description'],
                        'priority_scenarios': self.cfg['priority_scenarios'],
                        'execution_mode': 'static-only', 'initial_working_tree': 'clean',
                        'source_access': 'current checkout; native CLI permissions; other branch reports are outside task scope'}
                    for stage in ('document', 'review'):
                        if stage == 'review' and (not item['document'] or item['document']['completion_status'] == 'BLOCKED'):
                            item['errors'].append('Review skipped: no usable architecture document.')
                            break
                        stage_context = dict(context)
                        if stage == 'review':
                            doc = item['document']
                            stage_context['architecture_document'] = doc
                            stage_context['document_sha256'] = digest((doc['report_markdown'].rstrip() + '\n').encode())
                        try:
                            log(f'  {stage}: {self.cfg["_agents"][stage]["backend"]}')
                            data, meta = self.invoke(stage, stage_context, branch_dir / (stage + '.logs'))
                            item[stage] = data
                            item[stage + '_invocation'] = meta
                        except (AuditError, ContractError, OSError, UnicodeError) as exc:
                            item['errors'].append(f'{stage}: {exc}')
                            if not self.cfg['continue_on_error']:
                                raise
                        finally:
                            # Changed HEAD or working tree is always fatal, even with continue_on_error.
                            self.repo.assert_snapshot(commit)
                            persist()
                    item['accepted'] = accepted(item)
                    persist()
            finally:
                manifest['restoration'] = self.repo.restore(original_branch, original_commit)
                restored = True
                persist()
            # Include all requested branches even if future policies skip one; missing inputs stay visible.
            existing = {b['branch']: b for b in manifest['branches']}
            entries = [existing.get(b, {'branch': b, 'source_commit': pins[b], 'document': None,
                       'review': None, 'errors': ['Branch was not analyzed.']}) for b in self.cfg['branches']]
            baseline = self.cfg['baseline_branch']
            bundle = {'baseline_branch': baseline, 'baseline_commit': pins[baseline],
                'requested_branches': self.cfg['branches'], 'output_language': self.cfg['output_language'],
                'project_description': self.cfg['project_description'],
                'scope': 'reports-only comparison; source inspection is outside task scope',
                'branches': [{k: b.get(k) for k in ('branch', 'source_commit', 'document', 'review', 'errors')} for b in entries],
                'git_deltas': {b: self.repo.delta(pins[baseline], pins[b]) for b in self.cfg['branches'] if b != baseline}}
            comp_dir = self.run_dir / 'comparison'
            save_json(comp_dir / 'inputs.json', bundle)
            log('Comparing all branch reports in a new session outside the repository')
            try:
                comparison, meta = self.invoke('compare', bundle, comp_dir / 'compare.logs')
            finally:
                # Compare also uses the user's profile, so verify the restored
                # checkout even though this stage runs outside the repository.
                try:
                    self.repo.clean()
                    if self.repo.head() != original_commit or self.repo.symbolic() != original_branch:
                        raise UnsafeRepository('Original checkout changed during comparison.')
                except AuditError as exc:
                    manifest['restoration'] = {'restored': False, 'error': str(exc)}
                    raise
            manifest['comparison'] = comparison
            manifest['comparison_invocation'] = meta
            failed = any(b['errors'] for b in entries)
            quality_ok = all(accepted(b) for b in entries) and comparison['completion_status'] == 'COMPLETE'
            manifest['status'] = 'COMPLETE' if quality_ok else 'PARTIAL'
            code = 1 if failed else 0 if quality_ok else 2
        except BaseException as exc:
            manifest['errors'].append(str(exc) or type(exc).__name__)
            manifest['status'] = 'FAILED'
            code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            # Only restore if preflight established an original checkout and restoration
            # has not already completed; never force away evidence of unexpected writes.
            if original_commit and not restored and not check_only:
                try:
                    manifest['restoration'] = self.repo.restore(original_branch, original_commit)
                except Exception as restore_error:
                    manifest['restoration'] = {'restored': False, 'error': str(restore_error)}
            log(f'Run failed: {exc}')
        manifest['finished_at'] = now()
        manifest['exit_code'] = code
        persist()
        return manifest, code

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--check', action='store_true', help='Check configuration, Git state, and CLI capabilities; no branch switch or model calls.')
    args = parser.parse_args()
    if sys.platform not in ('darwin', 'linux') or sys.version_info < (3, 11):
        raise AuditError('This implementation requires macOS or Linux and Python 3.11+.')
    if os.geteuid() == 0:
        raise AuditError('Run as a normal user, not root.')
    os.umask(0o077)
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'Received signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    config = load_config(args.config.resolve())
    reports = Path(config['reports_dir'])
    reports.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:10]
    run_dir = reports / run_id
    run_dir.mkdir(mode=0o700)
    snapshot = {k: v for k, v in config.items() if not k.startswith('_')}
    save_json(run_dir / 'config.snapshot.json', snapshot)
    with repository_lock(Path(config['repository'])):
        manifest, code = Runner(config, run_dir).run(check_only=args.check)
    print(json.dumps({'run_id': run_id, 'status': manifest['status'],
                      'manifest': str(run_dir / 'manifest.json'), 'exit_code': code}, ensure_ascii=False))
    return code

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (AuditError, OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(1)
