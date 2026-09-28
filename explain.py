#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Sequential architecture auditing with configured coding-agent CLIs.

macOS or Linux, Python 3.11+, and one or more authenticated coding-agent CLIs.
Git >= 2.34.1 is required only for git mode; folder mode needs no Git.
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
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any
from contracts import FOLDER_SCHEMAS, SCHEMAS, ContractError, accepted, jsonc, parse_backend, strict_json, validate_result

ROOT = Path(__file__).resolve().parent
STAGES = ('document', 'review', 'compare')
ARTIFACTS = {'document': 'ARCHITECTURE.md', 'review': 'ARCHITECTURE_REVIEW.md',
             'compare': 'BRANCH_COMPARISON.md'}
BACKENDS = {'codex': 'codex', 'claude-code': 'claude', 'opencode': 'opencode'}
MIN_GIT_VERSION = (2, 34, 1)

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

class GitRuntime:
    """One PATH resolution and version check for the entire repository hierarchy."""
    def __init__(self):
        executable = shutil.which('git')
        if not executable:
            raise AuditError('Git >= 2.34.1 is required for git mode; git was not found in PATH.')
        self.executable = str(Path(executable).resolve())
        self.env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL') if k in os.environ}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                        GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_PAGER='cat',
                        GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='', GIT_NO_REPLACE_OBJECTS='1')
        # Do not discover a repository or use an inherited TMPDIR in source files.
        with tempfile.TemporaryDirectory(prefix='archaudit-git-version-', dir='/tmp') as neutral:
            result = process([self.executable, '--version'], Path(neutral), self.env,
                             timeout=30, max_output=4096)
        self.version_string = result['stdout'].decode('utf-8', errors='replace').strip()
        match = re.fullmatch(r'git version (\d+)\.(\d+)\.(\d+)(?:[-+. ][^\r\n]*)?', self.version_string)
        if result['returncode'] or result['error'] or not match:
            raise AuditError(f'Cannot determine Git version at {self.executable}: '
                             f'{self.version_string!r}; Git >= 2.34.1 is required.')
        self.version = tuple(map(int, match.groups()))
        if self.version < MIN_GIT_VERSION:
            raise AuditError(f'Found {self.version_string} at {self.executable}; Git >= 2.34.1 is required.')
        self.temporary = None

    def trust_config(self, path: Path) -> str:
        if str(path).endswith('/*'):
            raise UnsafeRepository(f'Cannot express exact safe.directory trust for a checkout named "*": {path}')
        if self.temporary is None:
            self.temporary = tempfile.TemporaryDirectory(prefix='archaudit-git-trust-', dir='/tmp')
        config = Path(self.temporary.name) / (uuid.uuid4().hex + '.gitconfig')
        value = str(path).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t').replace('\b', '\\b')
        atomic(config, '[safe]\n\tdirectory =\n\tdirectory = "' + value + '"\n')
        return str(config)

    def close(self) -> None:
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None

    def manifest(self) -> dict:
        return {'executable': self.executable, 'version_string': self.version_string,
                'version': list(self.version), 'minimum_version': list(MIN_GIT_VERSION),
                'compatibility': {'head': 'validated-direct-file', 'fsmonitor': 'empty-config-value',
                    'ownership': 'runner-euid', 'trust': 'private-global-config-per-checkout',
                    'local_objects': 'reject-promisor-config; deny-all-transports; no-lazy-fetch-when-supported'}}


class Repository:
    def __init__(self, path: Path, trust_repository: bool = False, runtime: GitRuntime | None = None):
        self.runtime = runtime or GitRuntime()
        self.path = path.resolve()
        self.trust_repository = trust_repository
        self.env = self.runtime.env.copy()
        self.prefix = [self.runtime.executable, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=',
            '-c', 'core.untrackedCache=false', '-c', 'submodule.recurse=false',
            '-c', 'core.pager=cat', '-C', str(self.path)]

    def close(self) -> None:
        self.runtime.close()

    def check_ownership(self) -> None:
        for path in dict.fromkeys((self.path, self.path / '.git', self.git_dir)):
            owner = path.lstat().st_uid
            if owner != os.geteuid() and not self.trust_repository:
                raise UnsafeRepository(f'Repository ownership mismatch at {path}: owner UID {owner}, '
                    f'runner UID {os.geteuid()}. Use --trust-repository only if you trust this checkout.')

    def git(self, *args: str, allowed: tuple[int, ...] = (0,), input_data: bytes | None = None) -> bytes:
        if not hasattr(self, 'git_dir'):
            self.setup_paths(self)
        self.check_ownership()
        try:
            r = subprocess.run(self.prefix + list(args), env=self.env,
                input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise AuditError(f'git {args[0]} timed out after 120 seconds') from exc
        if r.returncode not in allowed:
            error = r.stderr.decode(errors='replace').strip()
            if any(line.startswith(('fatal: detected dubious ownership in repository', 'fatal: unsafe repository'))
                   for line in error.splitlines()):
                error += ('\nGit rejected the repository because of an ownership mismatch.\n'
                          'Run with the checkout owner\'s UID, or use --trust-repository\n'
                          'only if you trust this specific checkout.')
            raise AuditError(f'git {args[0]} failed: {error}')
        return r.stdout

    def text(self, *args: str, **kwargs) -> str:
        return self.git(*args, **kwargs).decode('utf-8').strip()

    def head(self) -> str:
        return self.text('rev-parse', 'HEAD')

    def symbolic(self) -> str | None:
        ref = self.symbolic_ref()
        return ref[len('refs/heads/'):] if ref and ref.startswith('refs/heads/') else ref

    def symbolic_ref(self) -> str | None:
        if not hasattr(self, 'git_dir'):
            self.setup_paths(self)
        raw, _ = self.read_head()
        value = raw.removesuffix(b'\n')
        if value.startswith(b'ref: '):
            try:
                ref = value[5:].decode('utf-8')
            except UnicodeError as exc:
                raise UnsafeRepository(f'Invalid symbolic HEAD at {self.git_dir / "HEAD"}') from exc
            if not ref.startswith('refs/') or '\0' in ref:
                raise UnsafeRepository(f'Invalid symbolic HEAD at {self.git_dir / "HEAD"}: {ref!r}')
            try:
                self.git('check-ref-format', ref)
            except AuditError as exc:
                raise UnsafeRepository(f'Invalid symbolic HEAD at {self.git_dir / "HEAD"}: {ref!r}: {exc}') from exc
            return ref
        if not re.fullmatch(b'[0-9a-fA-F]+', value):
            raise UnsafeRepository(f'Invalid detached HEAD at {self.git_dir / "HEAD"}')
        # Both SHA-1 and SHA-256 repositories exist in Git 2.34.1.
        try:
            object_format = self.text('rev-parse', '--show-object-format')
            if object_format not in ('sha1', 'sha256') or len(value) != {'sha1': 40, 'sha256': 64}[object_format]:
                raise AuditError(f'Invalid object ID for format {object_format!r}')
            if self.text('cat-file', '-t', value.decode('ascii')) != 'commit':
                raise AuditError('Object is not a commit')
        except AuditError as exc:
            raise UnsafeRepository(f'Invalid detached HEAD at {self.git_dir / "HEAD"}: {exc}') from exc
        return None

    def read_head(self) -> tuple[bytes, list]:
        path = self.git_dir / 'HEAD'
        def stamp(info):
            return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise UnsafeRepository(f'HEAD must be a regular file, not a symlink: {path}')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or stamp(before) != stamp(opened):
                    raise UnsafeRepository(f'HEAD changed while opening: {path}')
                data = stream.read(4097)
                if stamp(opened) != stamp(os.fstat(stream.fileno())) or stamp(opened) != stamp(path.lstat()):
                    raise UnsafeRepository(f'HEAD changed while reading: {path}')
            if not data or len(data) > 4096:
                raise UnsafeRepository(f'Empty or oversized HEAD: {path}')
            return data, stamp(opened) + [digest(data)]
        except OSError as exc:
            raise UnsafeRepository(f'Cannot read HEAD at {path}: {exc}') from exc

    def clean(self, *, local: bool = False) -> None:
        if not local and hasattr(self, 'nodes'):
            for path, node in self.nodes.items():
                try:
                    node.clean(local=True)
                except AuditError as exc:
                    raise UnsafeRepository(f'{path}: {exc}') from exc
            return
        # Each child is checked directly. During a planned switch its HEAD can
        # temporarily differ from its parent's gitlink. Still check staged gitlinks.
        status = self.git('status', '--porcelain=v1', '-z', '--untracked-files=all',
                          '--ignore-submodules=all')
        staged = self.git('diff-index', '--cached', '--raw', '-z', '--ignore-submodules=none',
                          '--no-ext-diff', 'HEAD', '--')
        ignored = self.git('ls-files', '--others', '--ignored', '--exclude-standard', '-z')
        if status or staged or ignored:
            raise UnsafeRepository('Working tree must have no modifications, untracked files, '
                                   'or ignored files. No automatic stash/reset/clean is performed.')
        flags = self.git('ls-files', '-v', '-z').split(b'\0')
        if any(line and (line[:1].islower() or line[:1] == b'S') for line in flags):
            raise UnsafeRepository('assume-unchanged/skip-worktree entries are unsupported.')

    @staticmethod
    def safe_relative(value: str) -> str:
        if (not value or value.startswith('/') or '\\' in value or
                any(part in ('', '.', '..') or part.lower() == '.git' for part in value.split('/'))):
            raise UnsafeRepository(f'Unsafe submodule path or name: {value!r}')
        return value

    @staticmethod
    def directory_chain(path: Path, boundary: Path) -> dict:
        if boundary.resolve() != boundary or not inside(path, boundary):
            raise UnsafeRepository(f'Path escapes checkout boundary: {path}')
        result = {}
        items = [boundary]
        for part in path.relative_to(boundary).parts:
            items.append(items[-1] / part)
        for item in items:
            info = item.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise UnsafeRepository(f'Directory replaced or symbolic link encountered: {item}')
            result[str(item)] = [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid]
        return result

    @staticmethod
    def file_stamp(path: Path) -> list | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeRepository(f'Git metadata must be a regular file: {path}')
        return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, digest(path.read_bytes())]

    def setup_paths(self, root: Repository, parent: Repository | None = None, name: str | None = None) -> None:
        entry = self.path / '.git'
        self.root_path = root.path
        self.directory_chain(self.path, root.path)
        if entry.is_symlink():
            raise UnsafeRepository(f'Symbolic .git entry: {entry}')
        if entry.is_dir():
            expected = entry
        elif parent and entry.is_file():
            raw = entry.read_text().rstrip('\n')
            if not raw.startswith('gitdir: '):
                raise UnsafeRepository(f'Invalid .git file: {entry}')
            expected = parent.git_dir / 'modules' / self.safe_relative(name)
            target = Path(os.path.abspath(self.path / raw[8:]))
            if target != expected:
                raise UnsafeRepository(f'Unexpected external Git directory for {self.path}')
        else:
            raise AuditError('Submodule is not initialized locally.' if parent else
                             'A normal standalone checkout is required; no bare/linked worktree.')
        boundary = root.path
        self.directory_chain(expected, boundary)
        self.git_dir = expected
        self.git_boundary = boundary
        self.check_ownership()
        self._fixed = self.fixed_metadata()
        self.control_metadata()
        if self._fixed['files']['commondir'] is not None:
            raise UnsafeRepository('Linked/common Git directories are unsupported.')
        if self.trust_repository:
            self.env['GIT_CONFIG_GLOBAL'] = self.runtime.trust_config(self.path)

    def setup_node(self, root: Repository, parent: Repository | None = None, name: str | None = None) -> None:
        self.setup_paths(root, parent, name)
        # 2.34.1 ignores GIT_NO_LAZY_FETCH. A failed lazy fetch can even write
        # remote.*.partialclonefilter before GIT_ALLOW_PROTOCOL denies transport.
        # Reject these configurations before resolving HEAD or reading objects.
        if self.git('config', '--get-regexp',
                    r'^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$', allowed=(0, 1)):
            raise AuditError('Partial-clone/promisor configuration is unsupported: Git may attempt lazy fetching '
                             'and change config before transport denial. Prepare a full local checkout.')
        self.symbolic_ref()
        # Validate the paths before any command receives a safe.directory exception.
        actual = Path(self.git('rev-parse', '--absolute-git-dir').decode().rstrip('\n'))
        common = Path(os.path.abspath(self.path / self.git('rev-parse', '--git-common-dir').decode().rstrip('\n')))
        top = Path(self.git('rev-parse', '--show-toplevel').decode().rstrip('\n'))
        if actual != self.git_dir or common != self.git_dir or top != self.path:
            raise UnsafeRepository(f'Git data does not belong to expected checkout: {self.path}')
        self.policy()
        self.clean(local=True)
        self._trees = {}

    def fixed_metadata(self) -> dict:
        directories = self.directory_chain(self.path, self.root_path)
        directories.update(self.directory_chain(self.git_dir, self.git_boundary))
        for name in ('objects', 'refs'):
            directories.update(self.directory_chain(self.git_dir / name, self.git_dir))
        if (self.git_dir / 'objects/info/alternates').exists():
            raise UnsafeRepository('External object alternates are unsupported; make objects local first.')
        files = {name: self.file_stamp(self.git_dir / name)
                 for name in ('config', 'config.worktree', 'commondir')}
        if self.path / '.git' != self.git_dir:
            files['gitfile'] = self.file_stamp(self.path / '.git')
        return {'directories': directories, 'files': files}

    def control_metadata(self) -> dict:
        return {'HEAD': self.read_head()[1], 'index': self.file_stamp(self.git_dir / 'index')}

    def reference_metadata(self, ref: str) -> dict:
        path = self.git_dir / ref
        directories = {}
        for parent in reversed(path.relative_to(self.git_dir).parents):
            candidate = self.git_dir / parent
            if candidate.exists() or candidate.is_symlink():
                directories.update(self.directory_chain(candidate, self.git_dir))
        return {'directories': directories, 'loose': self.file_stamp(path),
                'packed': self.file_stamp(self.git_dir / 'packed-refs')}

    def guard_metadata(self) -> None:
        if self.fixed_metadata() != self._fixed:
            raise UnsafeRepository('Working directory or Git metadata changed outside the orchestrator.')

    def policy(self) -> None:
        if self.git('config', '--get-regexp', r'^filter\.', allowed=(0, 1)):
            raise AuditError('Git filters (including Git LFS filters) require a separate supported policy.')
        for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-apply', 'rebase-merge', 'BISECT_START'):
            if (self.git_dir / marker).exists():
                raise UnsafeRepository(f'Unfinished Git operation: {marker}')
        if self.text('config', '--bool', '--get', 'core.sparseCheckout', allowed=(0, 1)) == 'true':
            raise AuditError('Sparse checkouts are unsupported.')

    def tree(self, commit: str) -> dict:
        if commit in self._trees:
            return self._trees[commit]
        if self.text('cat-file', '-t', commit) != 'commit':
            raise AuditError('Required object is not a commit.')
        entries = {}
        for entry in self.git('ls-tree', '-r', '-z', commit).split(b'\0'):
            if entry:
                header, path = entry.split(b'\t', 1)
                mode, kind, oid = header.decode('ascii').split(' ')
                entries[path.decode('utf-8')] = (mode, kind, oid)
        # Do not allow lazy fetching, including on Git versions predating
        # GIT_NO_LAZY_FETCH. GIT_ALLOW_PROTOCOL='' also blocks every transport.
        blobs = [oid for mode, kind, oid in entries.values() if kind == 'blob']
        if blobs:
            checked = self.git('cat-file', '--batch-check', input_data=('\n'.join(blobs) + '\n').encode())
            if len(checked.splitlines()) != len(blobs) or any(
                    len(line.split()) != 3 or line.split()[1] != b'blob' for line in checked.splitlines()):
                raise AuditError('Required tree/blob objects are unavailable locally.')
        links = {p: oid for p, (mode, kind, oid) in entries.items() if mode == '160000'}
        modules = {}
        if '.gitmodules' in entries:
            if entries['.gitmodules'][0] not in ('100644', '100755'):
                raise AuditError('.gitmodules must be a regular tracked file.')
            try:
                raw = self.git('config', '--no-includes', '--null', '--blob', commit + ':.gitmodules',
                               '--get-regexp', r'^submodule\..*\.path$', allowed=(0, 1))
            except AuditError as exc:
                raise AuditError('Cannot parse committed .gitmodules.') from exc
            names = set()
            for record in raw.split(b'\0'):
                if not record:
                    continue
                key, path = record.decode('utf-8').split('\n', 1)
                name = self.safe_relative(key[len('submodule.'):-len('.path')])
                self.safe_relative(path)
                if path in modules or name in names:
                    raise AuditError(f'Duplicate submodule path/name: {path!r}, {name!r}')
                modules[path] = name
                names.add(name)
        if set(links) != set(modules):
            raise AuditError('gitlinks and committed .gitmodules paths differ: ' +
                             repr(sorted(set(links) ^ set(modules))))
        self._trees[commit] = {path: {'name': modules[path], 'commit': sha} for path, sha in links.items()}
        return self._trees[commit]

    def preflight(self, branches: list[str]) -> dict[str, str]:
        self.nodes, self.original, self.plans, self.expected, self.journal = {}, {}, {}, {}, []
        self.descriptions = {}
        used = set()

        def visit(node, path, parent, name, required, label, plan, discover=False):
            try:
                if discover:
                    node.setup_node(self, parent, name)
                    identity = tuple(node._fixed['directories'][str(node.git_dir)][:2])
                    if identity in used:
                        raise UnsafeRepository('Git directory reused by multiple submodules.')
                    used.add(identity)
                    self.nodes[path] = node
                    self.descriptions[path] = {'path': path, 'parent': None if parent is None else
                        str(parent.path.relative_to(self.path)), 'name': name}
                    actual = node.head()
                    if required and actual != required:
                        raise UnsafeRepository(f'Initial HEAD {actual} does not match gitlink.')
                    required = required or actual
                    ref = node.symbolic_ref()
                    if ref and not ref.startswith('refs/heads/'):
                        raise UnsafeRepository(f'Unsupported symbolic HEAD: {ref}')
                    self.original[path] = {'commit': required, 'ref': ref,
                        'worktree': str(node.path), 'git_dir': str(node.git_dir), 'identity': node._fixed,
                        'ref_identity': node.reference_metadata(ref) if ref else None}
                    self.expected[path] = {'commit': required, 'ref': ref, 'control': node.control_metadata()}
                    if parent is None:
                        label = f'original ({ref or required})'
                plan[path] = required
                children = node.tree(required)
                if not discover:
                    original_children = node.tree(self.original[path]['commit'])
                    for child in sorted(set(children) | set(original_children)):
                        before, after = original_children.get(child), children.get(child)
                        if before is None or after is None or before['name'] != after['name']:
                            detail = after or before
                            raise AuditError(f'Unsupported submodule structure change at {path}/{child}: '
                                             f'required SHA {detail["commit"]}; paths and logical names must match original checkout.')
                for child, detail in children.items():
                    full = child if path == '.' else path + '/' + child
                    child_path = node.path / child
                    # Check lexical paths before Repository resolves them.
                    if discover:
                        try:
                            self.directory_chain(child_path, self.path)
                        except OSError as exc:
                            raise AuditError(f'Submodule {full!r}, required SHA {detail["commit"]}: not initialized locally.') from exc
                    child_node = Repository(child_path, self.trust_repository, self.runtime) if discover else self.nodes[full]
                    visit(child_node, full, node, detail['name'], detail['commit'], label, plan, discover)
            except (AuditError, OSError) as exc:
                kind = UnsafeRepository if isinstance(exc, UnsafeRepository) else AuditError
                raise kind(f'Snapshot {label!r}, path {path!r}, required SHA {required or "HEAD"}: {exc}') from exc

        initial = {}
        visit(self, '.', None, None, None, 'original', initial, True)
        self.plans[initial['.']] = initial
        result = {}
        for name in branches:
            self.git('check-ref-format', 'refs/heads/' + name)
            commit = self.text('rev-parse', '--verify', 'refs/heads/' + name + '^{commit}')
            plan = {}
            visit(self, '.', None, None, commit, name, plan)
            self.plans[commit] = plan
            result[name] = commit
        self.assert_expected()
        return result

    def assert_expected(self) -> None:
        for path, node in self.nodes.items():
            try:
                node.guard_metadata()
                state = self.expected[path]
                if node.control_metadata() != state['control'] or node.head() != state['commit'] or node.symbolic_ref() != state['ref']:
                    raise UnsafeRepository('HEAD/index changed outside the orchestrator.')
                original = self.original[path]
                if original['ref'] and (node.reference_metadata(original['ref']) != original['ref_identity'] or
                        node.text('rev-parse', '--verify', original['ref']) != original['commit']):
                    raise UnsafeRepository('Original branch moved concurrently; refusing automatic restoration.')
                node.policy()
                node.clean(local=True)
            except (AuditError, OSError) as exc:
                error = UnsafeRepository(f'{path}: {exc}')
                error.node = path
                raise error from exc

    def switch_node(self, path: str, commit: str, ref: str | None = None) -> None:
        self.assert_expected()
        node = self.nodes[path]
        before = self.expected[path]
        target = {'commit': commit, 'ref': ref}
        entry = {'path': path, 'before': {k: before[k] for k in target}, 'target': target, 'status': 'INTENDED'}
        self.journal.append(entry)
        switch_error = None
        try:
            if ref:
                node.git('switch', '--no-guess', '--no-recurse-submodules', '--', ref[len('refs/heads/'):])
            else:
                node.git('switch', '--detach', '--no-recurse-submodules', commit)
        except BaseException as exc:
            switch_error = exc
            exc.node = path
            raise
        finally:
            # Git may have completed just before a handled signal or error. Only
            # accept a clean old or intended state; never infer arbitrary progress.
            try:
                node.guard_metadata()
                actual = {'commit': node.head(), 'ref': node.symbolic_ref()}
                if actual != target and actual != entry['before']:
                    raise UnsafeRepository('Unexpected HEAD during interrupted switch.')
                node.clean(local=True)
                self.expected[path] = dict(actual, control=node.control_metadata())
                entry['status'] = 'COMPLETED' if actual == target else 'UNCHANGED'
            except (AuditError, OSError) as exc:
                entry.update(status='UNSAFE', error=str(exc))
                exc.node = path
                # Keep the last expected state so restoration refuses evidence.
                if switch_error is None:
                    raise
        self.assert_expected()

    def checkout(self, commit: str) -> None:
        if not hasattr(self, 'plans'):
            self.preflight([])
            # Compatibility for callers that previously checked out directly.
            if commit not in self.plans:
                if self.tree(commit):
                    raise AuditError('Submodule checkout requires a prepared preflight plan.')
                self.plans[commit] = {'.': commit}
        for path, sha in self.plans[commit].items():
            self.switch_node(path, sha)
        self.assert_snapshot(commit)

    def assert_snapshot(self, commit: str) -> None:
        self.assert_expected()
        for path, sha in self.plans[commit].items():
            if self.expected[path]['commit'] != sha or self.expected[path]['ref'] is not None:
                raise UnsafeRepository(f'{path}: HEAD differs from the detached snapshot plan.')

    def restore(self, branch: str | None, commit: str) -> dict:
        self.assert_expected()
        for path, state in self.original.items():
            if any(self.expected[path][k] != state[k] for k in ('commit', 'ref')):
                self.switch_node(path, state['commit'], state['ref'])
        self.assert_expected()
        return {'restored': True, 'branch': branch, 'commit': commit,
                'nodes': {p: {'commit': s['commit'], 'ref': s['ref'], 'restored': True}
                          for p, s in self.original.items()}}

    def submodules(self, commit: str, verified: bool = False) -> list[dict]:
        if verified:
            self.assert_snapshot(commit)
        return [dict(self.descriptions[path], expected_commit=sha, available_locally=True,
                     actual={'commit': self.expected[path]['commit'], 'ref': self.expected[path]['ref'],
                             'clean': True}, snapshot_verified=verified)
                for path, sha in self.plans[commit].items() if path != '.']

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
        submodules = [dict(self.descriptions[path], baseline_commit=sha, branch_commit=self.plans[other][path])
                      for path, sha in getattr(self, 'plans', {}).get(baseline, {}).items()
                      if path != '.' and sha != self.plans[other][path]]
        return {'orientation': 'baseline_tree_to_branch_tree', 'baseline_commit': baseline,
            'branch_commit': other, 'merge_bases': bases, 'changes': changes,
            'submodule_changes': submodules,
            'identical_trees': self.text('rev-parse', baseline + '^{tree}') ==
                               self.text('rev-parse', other + '^{tree}'),
            'limitations': ['Path/mode/blob changes only; no patch, runtime evidence, or historical rationale.',
                            'Rename detection disabled: renames appear as deletion plus addition.',
                            'Gitlink/submodule SHA changes are not file diffs of the nested repositories.']}

class Folder:
    def __init__(self, path: Path):
        self.path = path.resolve()

    def snapshot(self) -> dict:
        """Inventory without following links; stream file contents through SHA-256."""
        entries = []

        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns)

        def unchanged(before, after, name):
            if identity(before) != identity(after):
                raise AuditError(f'Source changed while fingerprinting: {name}')

        def directory(fd, relative):
            before = os.fstat(fd)
            entries.append({'path': relative, 'type': 'directory',
                            'mode': format(stat.S_IMODE(before.st_mode), '04o')})
            for name in sorted(os.listdir(fd)):
                child = name if relative == '.' else relative + '/' + name
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                entry = {'path': child, 'mode': format(stat.S_IMODE(info.st_mode), '04o')}
                if stat.S_ISLNK(info.st_mode):
                    entry.update(type='symlink', target=os.readlink(name, dir_fd=fd))
                    entries.append(entry)
                elif stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    if stat.S_ISDIR(info.st_mode):
                        flags |= os.O_DIRECTORY
                    child_fd = os.open(name, flags, dir_fd=fd)
                    try:
                        unchanged(info, os.fstat(child_fd), child)
                        if stat.S_ISDIR(info.st_mode):
                            directory(child_fd, child)
                        else:
                            sha = hashlib.sha256()
                            while block := os.read(child_fd, 1024 * 1024):
                                sha.update(block)
                            entry.update(type='file', size=info.st_size, sha256=sha.hexdigest())
                            entries.append(entry)
                        unchanged(info, os.fstat(child_fd), child)
                    finally:
                        os.close(child_fd)
                else:
                    raise AuditError(f'Unsupported special file in source folder: {child}')
                unchanged(info, os.stat(name, dir_fd=fd, follow_symlinks=False), child)
            unchanged(before, os.fstat(fd), relative)

        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                before = os.fstat(fd)
                directory(fd, '.')
                unchanged(before, self.path.lstat(), str(self.path))
            finally:
                os.close(fd)
        except OSError as exc:
            raise AuditError(f'Cannot read source folder {self.path}: {exc}') from exc
        fingerprint = digest(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode())
        return {'source_directory': str(self.path), 'source_fingerprint': fingerprint,
                'algorithm': 'sha256', 'entries': entries}

    def assert_snapshot(self, fingerprint: str) -> None:
        if self.snapshot()['source_fingerprint'] != fingerprint:
            raise AuditError('Source folder changed during the run; files will not be restored.')

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
    value = (jsonc if path.suffix.lower() == '.jsonc' else strict_json)(path.read_text())
    if type(value) is not dict:
        raise AuditError('Configuration must be a JSON object.')
    allowed = {'repository', 'reports_dir', 'branches', 'baseline_branch', 'agent', 'stage_agents',
        'output_language', 'priority_scenarios', 'timeout_seconds', 'max_input_bytes',
        'max_output_bytes', 'continue_on_error', 'project_description', 'prompts',
        'mode', 'git_mode', 'folder_mode'}
    if 'additional_runtime_read_paths' in value:
        raise AuditError('Remove additional_runtime_read_paths from configuration: the runner now uses native CLI permissions.')
    if set(value) - allowed:
        raise AuditError(f'Unknown configuration keys: {set(value) - allowed}')
    for key in ('reports_dir', 'agent'):
        if key not in value:
            raise AuditError(f'Missing configuration key: {key}')
    grouped = bool(set(value) & {'mode', 'git_mode', 'folder_mode'})
    if grouped:
        if set(value) & {'repository', 'branches', 'baseline_branch'}:
            raise AuditError('Do not mix legacy repository/branches/baseline_branch with mode sections.')
        if value.get('mode') not in ('git', 'folder'):
            raise AuditError('Grouped configuration requires mode: "git" or "folder".')
        for section in ('git_mode', 'folder_mode'):
            if section in value and type(value[section]) is not dict:
                raise AuditError(f'{section} must be a JSON object, even when inactive.')
        section = value['mode'] + '_mode'
        if section not in value:
            raise AuditError(f'Missing configuration section: {section}')
        source = value[section]
    else:
        source = value
    mode = value.get('mode', 'git')
    required = ('repository', 'branches', 'baseline_branch') if mode == 'git' else ('path',)
    if grouped and set(source) - set(required):
        raise AuditError(f'Unknown {mode}_mode keys: {set(source) - set(required)}')
    for key in required:
        if key not in source:
            raise AuditError(f'Missing configuration key: {key}')
    source_key = 'repository' if mode == 'git' else 'path'
    for obj, key in ((source, source_key), (value, 'reports_dir')):
        if not isinstance(obj[key], str) or not obj[key].strip():
            raise AuditError(f'{key} must be a nonempty path string.')
        p = Path(obj[key]).expanduser()
        obj[key] = str((path.parent / p if not p.is_absolute() else p).resolve())
    repo, reports = Path(source[source_key]), Path(value['reports_dir'])
    if overlap(repo, reports):
        raise AuditError('Source directory and reports_dir must be disjoint, not ancestors of each other.')
    if mode == 'git':
        branches = source['branches']
        if not isinstance(branches, list) or len(branches) < 2 or any(not isinstance(b, str) or not b for b in branches):
            raise AuditError('At least two nonempty local branch names are required.')
        if len(set(branches)) != len(branches) or source['baseline_branch'] not in branches:
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
    stages = STAGES if mode == 'git' else STAGES[:2]
    for stage in value['stage_agents']:
        if stage not in STAGES:
            raise AuditError(f'Unknown stage override: {stage}')
        if stage in stages and type(value['stage_agents'][stage]) is not dict:
            raise AuditError(f'stage_agents.{stage} must be a JSON object.')
    value['_agents'] = {}
    for stage in stages:
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
    if set(value['prompts']) - set(STAGES):
        raise AuditError('Unknown prompt stage.')
    for stage in stages:
        prompt_path = value['prompts'].get(stage, str(ROOT / 'prompts' / (stage + '.md')))
        if not isinstance(prompt_path, str) or not prompt_path.strip():
            raise AuditError(f'prompts.{stage} must be a nonempty path string.')
        p = Path(prompt_path).expanduser()
        p = (path.parent / p if not p.is_absolute() else p).resolve()
        if inside(p, repo):
            raise AuditError('Prompt templates must be outside the inspected repository.')
        if not p.is_file():
            raise AuditError(f'Missing prompt template: {p}')
        value['_prompt_paths'][stage] = str(p)
    return value

class Runner:
    def __init__(self, config: dict, run_dir: Path, trust_repository: bool = False):
        self.cfg, self.run_dir = config, run_dir.resolve()
        self.mode = config.get('mode', 'git')
        if trust_repository and self.mode != 'git':
            raise AuditError('--trust-repository requires git mode; it cannot be used in folder mode.')
        self.source = config.get(self.mode + '_mode', config)
        self.source_path = Path(self.source['path' if self.mode == 'folder' else 'repository'])
        self.folder = Folder(self.source_path) if self.mode == 'folder' else None
        self.repo = Repository(self.source_path, trust_repository=trust_repository) if self.mode == 'git' else None
        self.schemas = FOLDER_SCHEMAS if self.mode == 'folder' else SCHEMAS
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
                if backend == 'codex' and self.mode == 'folder':
                    required.append('--skip-git-repo-check')
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
            if compare or self.mode == 'folder':
                cmd += ['--skip-git-repo-check']
            if compare:
                cmd += ['-c', 'features.shell_tool=false']
            if agent.get('model'):
                cmd += ['--model', agent['model']]
            return cmd + ['-']
        if backend == 'claude-code':
            tools = '' if compare else 'Read,Glob,Grep'
            cmd = [exe, '-p', '--no-session-persistence', '--output-format', 'json',
                '--json-schema', json.dumps(self.schemas[stage]), '--permission-mode', 'dontAsk',
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
                  json.dumps(self.schemas[stage], ensure_ascii=False))
        payload = prompt.encode('utf-8')
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        meta = {'stage': stage, 'invocation_id': str(uuid.uuid4()), 'started_at': now(),
            'backend': agent['backend'], 'executable': agent['executable'],
            'model_requested': agent.get('model'), 'cli_version': self.versions.get(agent['backend'] + ':' + agent['executable']),
            'prompt_sha256': digest(payload), 'template_sha256': digest(template.encode()),
            'schema_sha256': digest(json.dumps(self.schemas[stage], sort_keys=True).encode()),
            'input_bytes': len(payload), 'status': 'RUNNING'}
        if self.repo:
            meta['submodules'] = context.get('submodules', [])
        save_json(destination / 'invocation.json', meta)
        # Saved by the parent only, for reproducibility; includes only this stage's permitted input.
        atomic(destination / 'input.prompt.txt', payload)
        if len(payload) > self.cfg['max_input_bytes']:
            meta.update(status='FAILED', error='Input exceeds max_input_bytes; no truncation or model call.', finished_at=now())
            save_json(destination / 'invocation.json', meta)
            raise AuditError(meta['error'])
        try:
            if self.folder:
                self.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.repo.assert_expected()
            with tempfile.TemporaryDirectory(prefix='archaudit-invocation-') as raw:
                state = Path(raw).resolve()
                cwd = state if stage == 'compare' else self.source_path
                env = cli_env(cwd)
                schema_path = state / 'output.schema.json'
                save_json(schema_path, self.schemas[stage])
                cmd = self.command(stage, state, agent, schema_path, env)
                try:
                    r = process(cmd, cwd, env, payload, self.cfg['timeout_seconds'], self.cfg['max_output_bytes'])
                    atomic(destination / 'stdout.log', r['stdout'])
                    atomic(destination / 'stderr.log', r['stderr'])
                    meta.update(returncode=r['returncode'], duration_seconds=r['duration_seconds'])
                finally:
                    if self.folder:
                        self.folder.assert_snapshot(context['source_fingerprint'])
                    else:
                        self.repo.assert_expected()
                if r['returncode'] or r['error']:
                    raise AuditError(r['error'] or f'{agent["backend"]} exited with {r["returncode"]}; see stderr.log')
                data, provider_meta = parse_backend(agent['backend'], r['stdout'].decode('utf-8'))
                validate_result(stage, data, context, self.mode)
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
        if self.folder:
            return self.run_folder(check_only)
        try:
            return self.run_git(check_only)
        finally:
            self.repo.close()

    def run_git(self, check_only: bool = False) -> tuple[dict, int]:
        manifest = {'run_id': self.run_dir.name,
            'started_at': now(), 'repository': str(self.repo.path),
            'git': self.repo.runtime.manifest(),
            'baseline_branch': self.source['baseline_branch'], 'status': 'RUNNING',
            'isolation': 'cli-native-permissions', 'platform': sys.platform,
            'branches': [], 'errors': []}
        def persist():
            if hasattr(self.repo, 'journal'):
                manifest['switch_journal'] = self.repo.journal
            save_json(self.run_dir / 'manifest.json', manifest)
        persist()
        original_branch = original_commit = None
        restored = False
        restoration_attempted = False

        def restore():
            nonlocal restored, restoration_attempted
            restoration_attempted = True
            try:
                manifest['restoration'] = self.repo.restore(original_branch, original_commit)
                restored = True
            except BaseException as exc:
                manifest['restoration'] = {'restored': False, 'node': getattr(exc, 'node', None),
                                           'error': str(exc) or type(exc).__name__}
                raise
            finally:
                persist()

        try:
            if not self.cfg['project_description']:
                log('Описание проекта не указано. Рекомендуем заполнить project_description: '
                    'кратко расскажите о назначении и истории системы')
            pins = self.repo.preflight(self.source['branches'])
            manifest['pins'] = pins
            original_branch, original_commit = self.repo.symbolic(), self.repo.head()
            manifest['original_checkout'] = {'branch': original_branch, 'commit': original_commit}
            manifest['original_hierarchy'] = self.repo.original
            manifest['snapshot_plans'] = {name: {'commit': sha, 'submodules': self.repo.submodules(sha)}
                                          for name, sha in pins.items()}
            try:
                manifest['cli_checks'] = self.check_cli()
            finally:
                self.repo.assert_expected()
            persist()
            if check_only:
                manifest.update(status='PREFLIGHT_OK', finished_at=now())
                persist()
                return manifest, 0
            analysis_error = None
            try:
                for branch in self.source['branches']:
                    commit = pins[branch]
                    log(f'Analyzing {branch} at {commit[:12]}')
                    branch_dir = self.run_dir / 'branches' / slug(branch)
                    item: dict[str, Any] = {'branch': branch, 'source_commit': commit,
                        'directory': str(branch_dir.relative_to(self.run_dir)),
                        'document': None, 'review': None, 'errors': []}
                    manifest['branches'].append(item)
                    persist()
                    self.repo.checkout(commit)
                    item['submodules'] = self.repo.submodules(commit, verified=True)
                    context = {'source_mode': 'git', 'branch': branch, 'source_commit': commit,
                        'submodules': item['submodules'],
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
                            self.repo.assert_snapshot(commit)
                            data, meta = self.invoke(stage, stage_context, branch_dir / (stage + '.logs'))
                            item[stage] = data
                            item[stage + '_invocation'] = meta
                        except (AuditError, ContractError, OSError, UnicodeError) as exc:
                            item['errors'].append(f'{stage}: {exc}')
                            if isinstance(exc, UnsafeRepository) or not self.cfg['continue_on_error']:
                                raise
                        finally:
                            # Changed HEAD or working tree is always fatal, even with continue_on_error.
                            self.repo.assert_snapshot(commit)
                            persist()
                    item['accepted'] = accepted(item)
                    persist()
            except BaseException as exc:
                analysis_error = exc
                raise
            finally:
                try:
                    restore()
                except BaseException as exc:
                    if analysis_error is None:
                        raise
                    manifest['errors'].append('Restoration: ' + (str(exc) or type(exc).__name__))
            # Include all requested branches even if future policies skip one; missing inputs stay visible.
            existing = {b['branch']: b for b in manifest['branches']}
            entries = [existing.get(b, {'branch': b, 'source_commit': pins[b], 'document': None,
                       'review': None, 'errors': ['Branch was not analyzed.']}) for b in self.source['branches']]
            baseline = self.source['baseline_branch']
            bundle = {'baseline_branch': baseline, 'baseline_commit': pins[baseline],
                'requested_branches': self.source['branches'], 'output_language': self.cfg['output_language'],
                'project_description': self.cfg['project_description'],
                'scope': 'reports-only comparison; source inspection is outside task scope',
                'branches': [{k: b.get(k) for k in ('branch', 'source_commit', 'submodules', 'document', 'review', 'errors')} for b in entries],
                'git_deltas': {b: self.repo.delta(pins[baseline], pins[b]) for b in self.source['branches'] if b != baseline}}
            comp_dir = self.run_dir / 'comparison'
            save_json(comp_dir / 'inputs.json', bundle)
            log('Comparing all branch reports in a new session outside the repository')
            try:
                self.repo.assert_expected()
                comparison, meta = self.invoke('compare', bundle, comp_dir / 'compare.logs')
            finally:
                # Compare also uses the user's profile, so verify the restored
                # checkout even though this stage runs outside the repository.
                try:
                    self.repo.assert_expected()
                except AuditError as exc:
                    manifest['restoration'] = {'restored': False, 'node': getattr(exc, 'node', None), 'error': str(exc)}
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
            if original_commit and not restored and not restoration_attempted and not check_only:
                try:
                    restore()
                except BaseException as restore_error:
                    manifest['errors'].append('Restoration: ' + (str(restore_error) or type(restore_error).__name__))
            log(f'Run failed: {exc}')
        manifest['finished_at'] = now()
        manifest['exit_code'] = code
        persist()
        return manifest, code

    def run_folder(self, check_only: bool = False) -> tuple[dict, int]:
        manifest = {'mode': 'folder', 'run_id': self.run_dir.name,
            'started_at': now(), 'source_directory': str(self.source_path), 'status': 'RUNNING',
            'isolation': 'cli-native-permissions', 'platform': sys.platform,
            'integrity': 'Fingerprints at stage boundaries; not a backup or continuous immutability guarantee.',
            'document': None, 'review': None, 'accepted': False, 'errors': []}
        def persist():
            save_json(self.run_dir / 'manifest.json', manifest)
        persist()
        try:
            if not self.cfg['project_description']:
                log('Описание проекта не указано. Рекомендуем заполнить project_description: '
                    'кратко расскажите о назначении и истории системы')
            inventory = self.folder.snapshot()
            save_json(self.run_dir / 'source.inventory.json', inventory)
            fingerprint = inventory['source_fingerprint']
            manifest.update(source_fingerprint=fingerprint, inventory='source.inventory.json')
            manifest['cli_checks'] = self.check_cli()
            self.folder.assert_snapshot(fingerprint)
            persist()
            if check_only:
                manifest['status'], code = 'PREFLIGHT_OK', 0
            else:
                context = {'source_mode': 'folder', 'source_directory': str(self.source_path),
                    'source_fingerprint': fingerprint, 'output_language': self.cfg['output_language'],
                    'project_description': self.cfg['project_description'],
                    'priority_scenarios': self.cfg['priority_scenarios'], 'execution_mode': 'static-only',
                    'source_access': 'Current directory tree, including hidden files; do not follow symlinks or use Git. '
                                     'Native CLI permissions; fingerprints verify stage boundaries only.'}
                for stage in ('document', 'review'):
                    stage_context = dict(context)
                    if stage == 'review':
                        doc = manifest['document']
                        if doc['completion_status'] == 'BLOCKED':
                            manifest['review_skipped'] = 'No usable architecture document.'
                            break
                        stage_context.update(architecture_document=doc,
                            document_sha256=digest((doc['report_markdown'].rstrip() + '\n').encode()))
                    log(f'  {stage}: {self.cfg["_agents"][stage]["backend"]}')
                    data, meta = self.invoke(stage, stage_context, self.run_dir / (stage + '.logs'))
                    manifest[stage], manifest[stage + '_invocation'] = data, meta
                    persist()
                manifest['accepted'] = accepted(manifest)
                manifest['status'] = 'COMPLETE' if manifest['accepted'] else 'PARTIAL'
                code = 0 if manifest['accepted'] else 2
        except BaseException as exc:
            manifest['errors'].append(str(exc) or type(exc).__name__)
            manifest['status'] = 'FAILED'
            code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            log(f'Run failed: {exc}')
        manifest.update(finished_at=now(), exit_code=code)
        persist()
        return manifest, code

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path, help='Path to a JSON or JSONC configuration.')
    parser.add_argument('--check', action='store_true', help='Check configuration, source state, and CLI capabilities; no branch switch or model calls.')
    parser.add_argument('--trust-repository', action='store_true', help='Trust only the configured Git checkout and verified submodules despite an ownership mismatch (Git >= 2.34.1).')
    args = parser.parse_args()
    if sys.platform not in ('darwin', 'linux') or sys.version_info < (3, 11):
        raise AuditError('This implementation requires macOS or Linux and Python 3.11+.')
    if os.geteuid() == 0:
        print('WARNING: Running as root; child CLIs inherit root privileges.', file=sys.stderr)
    os.umask(0o077)
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'Received signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    config = load_config(args.config.resolve())
    reports = Path(config['reports_dir'])
    run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:10]
    run_dir = reports / run_id
    runner = Runner(config, run_dir, trust_repository=args.trust_repository)
    reports.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir.mkdir(mode=0o700)
    snapshot = {k: v for k, v in config.items() if not k.startswith('_')}
    save_json(run_dir / 'config.snapshot.json', snapshot)
    with repository_lock(runner.source_path):
        manifest, code = runner.run(check_only=args.check)
    print(json.dumps({'run_id': run_id, 'status': manifest['status'],
                      'manifest': str(run_dir / 'manifest.json'), 'exit_code': code}, ensure_ascii=False))
    return code

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (AuditError, ContractError, OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(1)
