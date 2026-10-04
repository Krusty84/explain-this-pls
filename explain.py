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
from dataclasses import asdict
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
from urllib.parse import urlsplit
from contracts import MODEL_FOLDER_SCHEMAS, MODEL_SCHEMAS, ContractError, accepted, jsonc, strict_json, validate_result, schema_diagnostics, result_diagnostics
from reporting import Diagnostic, NullReporter, Reporter, diagnostic, existing_file, output_mode
from execution import Budget, execution_settings
import codex
import claude_code
import opencode
import xxx
from structured_output import blocked_comparison, repair_prompt, required_unresolved, retry_policy, validate_repair
from final_report import recoverable_material, stage_document, usable_study, report_entries, comparison_possible, render_final_report
from ledger import prepare_result, review_context
from study_normalization import normalize_evidence, normalization_provenance
from document_rendering import materialize_study, validate_materialized
from evidence import source_catalog, SourceChanged, open_source_directory, read_confined
from contracts import CONTRACT_ID, ARTIFACT_FORMAT, has_ledger_structure, validate_schema
from saved_contracts import SAVED_SCHEMAS, SAVED_FOLDER_SCHEMAS
from presentation import render_stage
from model_context import CONTEXT_FORMAT
from model_boundary import BindingRegistry
from source_decoding import normalize_source_decoding
from coverage_plan import build_coverage_plan, inventory_summary, verify_coverage_plan
from revisions import revision_inputs, choose_revision, completed_pair

ROOT = Path(__file__).resolve().parent
STAGES = ('catalog', 'study', 'review', 'compare')
SOURCE_STAGES = ('catalog', 'study', 'review')
ARTIFACTS = {'study': 'ARCHITECTURE.md', 'review': 'ARCHITECTURE_REVIEW.md',
             'compare': 'BRANCH_COMPARISON.md'}
BACKENDS = {'codex': 'codex', 'claude-code': 'claude', 'opencode': 'opencode', 'xxx': 'xxx'}
CLI_ADAPTERS = {'codex': codex, 'claude-code': claude_code}
MIN_GIT_VERSION = (2, 34, 1)

class AuditError(RuntimeError):
    def __init__(self, message: str = '', *, code=None, snapshot=None, node_path=None,
                 required_commit=None, hint=None, failure_kind=None, failure_layer=None, details=None):
        super().__init__(message)
        self.message = message
        self.code = code or ('UNSAFE_REPOSITORY' if isinstance(self, UnsafeRepository) else 'AUDIT_ERROR')
        self.snapshot, self.node_path = snapshot, node_path
        self.required_commit, self.hint = required_commit, hint
        self.failure_kind, self.failure_layer, self.details = failure_kind, failure_layer, details

    def diagnostic(self):
        return Diagnostic(self.code, self.message, self.snapshot, self.node_path,
                          self.required_commit, self.hint, self.failure_kind, self.failure_layer, self.details)

    def with_context(self, **context):
        fields = vars(self.diagnostic()).copy()
        for key, value in context.items():
            if fields.get(key) is None:
                fields[key] = value
        return type(self)(**fields)

    def __str__(self):
        details = []
        if self.snapshot is not None:
            details.append(f'Snapshot {self.snapshot!r}')
        if self.node_path is not None:
            details.append(f'path {self.node_path!r}')
        if self.required_commit is not None:
            details.append(f'required SHA {self.required_commit}')
        return (', '.join(details) + ': ' if details else '') + self.message

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

def private_directory(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700, exist_ok=True)

def neutral_temporary_base(source: Path) -> Path:
    for candidate in (Path('/tmp'), Path('/var/tmp')):
        candidate = candidate.resolve()
        if candidate.is_dir() and not inside(candidate, source.resolve()):
            return candidate
    raise AuditError('No temporary directory outside the source tree is available.')

def atomic(path: Path, data: str | bytes) -> None:
    private_directory(path.parent)
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

def cli_env(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(PWD=str(cwd.resolve()), NO_COLOR='1', GIT_TERMINAL_PROMPT='0')
    return env

def process(command: list[str], cwd: Path, env: dict[str, str], input_data: bytes = b'',
            *, reporter=None, context: dict | None = None, log_dir: Path | None = None,
            clock=time.monotonic, progress_interval: float = 30.0, budget=None) -> dict:
    """Nonblocking pipe I/O; do not pass artifact FDs into the CLI child."""
    reporter = reporter or NullReporter()
    context = context or {}
    # ExitStack also closes the files if opening the second file or Popen fails.
    with contextlib.ExitStack() as logs:
        streams = {}
        if log_dir is not None:
            for name in ('stdout', 'stderr'):
                fd = os.open(log_dir / (name + '.log'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                stream = logs.enter_context(os.fdopen(fd, 'wb', buffering=0))
                os.fchmod(stream.fileno(), 0o600)
                streams[name] = stream
        return _process(command, cwd, env, input_data, reporter,
                        context, clock, progress_interval, streams, budget or Budget(3600, clock=clock))


def _process(command, cwd, env, input_data, reporter, context,
             clock, progress_interval, logs, budget):
    budget.check()
    start = clock()
    last_output = None
    next_progress = start + progress_interval
    # A stage-owned Reporter also covers silent HTTP waits and retries.
    # Standalone process callers retain their own waiting events.
    managed_progress = reporter.cli_started()
    out, err = bytearray(), bytearray()
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
        while selector.get_map() or p.poll() is None:
            budget.check()
            current = clock()
            if reporter.progress and not managed_progress and current >= next_progress:
                reporter.emit('process_waiting', **context, elapsed_seconds=current - start,
                              last_output_seconds=None if last_output is None else current - last_output)
                next_progress = current + progress_interval
            # select() also bounds the wait after both output pipes have closed.
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
                        last_output = clock()
                        if managed_progress:
                            reporter.cli_output()
                        budget.activity()
                        target = out if name == 'stdout' else err
                        target.extend(block)
                        if name in logs:
                            remaining = memoryview(block)
                            while remaining:
                                remaining = remaining[logs[name].write(remaining):]
                            logs[name].flush()
    except KeyboardInterrupt:
        reporter.stop_requested()
        raise
    finally:
        failure = sys.exc_info()[1]
        failed = failure is not None
        cleanup_error = None
        selector.close()
        # Kill ordinary descendants remaining in this invocation's process group,
        # including on errors/interrupts; detached hostile daemons are not supported.
        if p.poll() is None:
            reporter.emit('process_stopping', **context)
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            cleanup_error = exc
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            cleanup_error = exc
        for stream in (p.stdin, p.stdout, p.stderr):
            with contextlib.suppress(Exception):
                stream.close()
        if cleanup_error is not None and not failed:
            raise AuditError('Could not finish cleanup of the owned CLI process group.',
                             code='CLI_FAILED', failure_kind='BACKEND_ERROR', failure_layer='cleanup') from cleanup_error
        if cleanup_error is not None and failure is not None:
            failure.cleanup_failed = True
    return {'stdout': bytes(out), 'stderr': bytes(err), 'returncode': p.returncode,
            'duration_seconds': round(clock() - start, 3)}

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
            result = process([self.executable, '--version'], Path(neutral), self.env, budget=Budget(30))
        self.version_string = result['stdout'].decode('utf-8', errors='replace').strip()
        match = re.fullmatch(r'git version (\d+)\.(\d+)\.(\d+)(?:[-+. ][^\r\n]*)?', self.version_string)
        if result['returncode'] or not match:
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
    def __init__(self, path: Path, trust_repository: bool = False, runtime: GitRuntime | None = None,
                 reporter=None):
        self.reporter = reporter or NullReporter()
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
        r = subprocess.run(self.prefix + list(args), env=self.env,
            input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
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

    def display_name(self) -> str:
        fallback = self.path.name or str(self.path)
        try:
            origin = self.text('config', '--get', 'remote.origin.url', allowed=(0, 1))
            path = urlsplit(origin).path if '://' in origin else re.sub(r'^[^/]+:', '', origin)
            return path.rstrip('/').rsplit('/', 1)[-1].removesuffix('.git') or fallback
        except (AuditError, OSError, UnicodeError, ValueError):
            # Naming is optional; preflight still diagnoses invalid Git metadata.
            return fallback

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
                    raise UnsafeRepository(**vars(exc.with_context(node_path=path).diagnostic())) from exc
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
            if parent:
                raise AuditError('The submodule is not initialized locally.', code='SUBMODULE_NOT_INITIALIZED')
            raise AuditError('A normal standalone checkout is required; no bare/linked worktree.')
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
        self.reporter.emit('preflight_started', check='submodules')
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
                            raise AuditError('The submodule is not initialized locally.',
                                code='SUBMODULE_NOT_INITIALIZED', node_path=full, required_commit=detail['commit']) from exc
                    child_node = Repository(child_path, self.trust_repository, self.runtime,
                                            self.reporter) if discover else self.nodes[full]
                    visit(child_node, full, node, detail['name'], detail['commit'], label, plan, discover)
                self.reporter.emit('snapshot_node_checked', snapshot=label, node_path=path, required_commit=required)
            except (AuditError, OSError) as exc:
                error = exc if isinstance(exc, AuditError) else AuditError(str(exc), code='IO_ERROR')
                raise error.with_context(snapshot=label, node_path=path, required_commit=required or 'HEAD') from exc

        initial = {}
        self.reporter.emit('snapshot_started', snapshot='original')
        visit(self, '.', None, None, None, 'original', initial, True)
        self.plans[initial['.']] = initial
        self.reporter.emit('snapshot_completed', snapshot='original', commit=initial['.'])
        result = {}
        for name in branches:
            self.git('check-ref-format', 'refs/heads/' + name)
            commit = self.text('rev-parse', '--verify', 'refs/heads/' + name + '^{commit}')
            plan = {}
            self.reporter.emit('snapshot_started', snapshot=name, commit=commit)
            visit(self, '.', None, None, commit, name, plan)
            self.plans[commit] = plan
            result[name] = commit
            self.reporter.emit('snapshot_completed', snapshot=name, commit=commit)
        self.reporter.emit('preflight_completed', check='git', **self.runtime.manifest(), branches=result)
        self.reporter.emit('preflight_completed', check='submodules')
        self.reporter.emit('preflight_started', check='integrity')
        self.assert_expected()
        self.reporter.emit('preflight_completed', check='integrity')
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
                detail = exc if isinstance(exc, AuditError) else AuditError(str(exc), code='IO_ERROR')
                error = UnsafeRepository(**vars(detail.with_context(node_path=path).diagnostic()))
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
        if commit not in getattr(self, 'plans', {}):
            raise AuditError('Checkout requires a prepared preflight plan.')
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
    def __init__(self, path: Path, *, canonical=False):
        self.path = path if canonical else path.resolve()

    def snapshot(self, *, exclude_git=False) -> dict:
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
                if exclude_git and name == '.git':
                    continue
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
            fd = open_source_directory(self.path)
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
    allowed = {'reports_dir', 'agent', 'stage_agents',
        'output_language', 'priority_scenarios', 'continue_on_error', 'project_description', 'prompts',
        'mode', 'git_mode', 'folder_mode', 'execution', 'result_policy', 'source_decoding'}
    if set(value) - allowed:
        raise AuditError(f'Unknown configuration keys: {set(value) - allowed}')
    try:
        if 'execution' in value and value['execution'] is None:
            raise ValueError('execution must be a JSON object.')
        value['execution'] = execution_settings(value.get('execution'))
        value['source_decoding'] = normalize_source_decoding(value.get('source_decoding'))
    except ValueError as exc:
        raise AuditError(str(exc), code='INVALID_CONFIG') from exc
    for key in ('mode', 'reports_dir', 'agent'):
        if key not in value:
            raise AuditError(f'Missing configuration key: {key}')
    mode = value['mode']
    if mode not in ('git', 'folder'):
        raise AuditError('Configuration requires mode: "git" or "folder".')
    for section in ('git_mode', 'folder_mode'):
        if section in value and type(value[section]) is not dict:
            raise AuditError(f'{section} must be a JSON object, even when inactive.')
    section = mode + '_mode'
    if section not in value:
        raise AuditError(f'Missing configuration section: {section}')
    source = value[section]
    required = ('repository', 'branches', 'baseline_branch') if mode == 'git' else ('path',)
    if set(source) - set(required):
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
        if not isinstance(branches, list) or not branches or any(not isinstance(b, str) or not b for b in branches):
            raise AuditError('At least one nonempty local branch name is required.')
        if len(set(branches)) != len(branches) or source['baseline_branch'] not in branches:
            raise AuditError('Branches must be unique and include baseline_branch.')
    defaults = {'output_language': 'Russian', 'project_description': '', 'priority_scenarios': [],
        'continue_on_error': True, 'stage_agents': {}, 'prompts': {}, 'result_policy': 'compromise'}
    for key, default in defaults.items():
        value.setdefault(key, default)
    if not isinstance(value['project_description'], str):
        raise AuditError('project_description must be a string.')
    value['project_description'] = value['project_description'].strip()
    for key in ('agent', 'stage_agents', 'prompts'):
        if type(value[key]) is not dict:
            raise AuditError(f'{key} must be a JSON object.')
    if type(value['continue_on_error']) is not bool:
        raise AuditError('continue_on_error must be boolean.')
    if value['result_policy'] not in ('compromise', 'strict'):
        raise AuditError('result_policy must be compromise or strict.')
    if not isinstance(value['priority_scenarios'], list) or any(not isinstance(s, str) for s in value['priority_scenarios']):
        raise AuditError('priority_scenarios must be an array of strings.')
    stages = STAGES if mode == 'git' and len(source['branches']) > 1 else SOURCE_STAGES
    for stage in value['stage_agents']:
        if stage not in STAGES:
            raise AuditError(f'Unknown stage override: {stage}')
        if stage in stages and type(value['stage_agents'][stage]) is not dict:
            raise AuditError(f'stage_agents.{stage} must be a JSON object.')
    value['_agents'] = {}
    for stage in ('study', *(s for s in stages if s != 'study')):
        agent = dict(value['_agents']['study'] if stage == 'catalog' else value['agent'])
        agent.update(value['stage_agents'].get(stage, {}))
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
    if set(value['prompts']) - (set(STAGES) | {'revise'}):
        raise AuditError('Unknown prompt stage.')
    for stage in (*stages, *(('revise',) if value['execution']['max_revision_rounds'] else ())):
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
    def __init__(self, config: dict, run_dir: Path, trust_repository: bool = False, reporter=None):
        self.reporter = reporter or NullReporter()
        self.reported_errors = []
        self.analysis_started = False
        self.active_stage = {}
        self.cfg, self.run_dir = config, run_dir.resolve()
        existing_manifest = self.run_dir / 'manifest.json'
        if existing_manifest.exists():
            previous = strict_json(existing_manifest.read_text(encoding='utf-8'))
            if (previous.get('contract_id') != CONTRACT_ID or
                    previous.get('artifact_format') != ARTIFACT_FORMAT):
                raise AuditError('Unsupported artifact format; select a new run directory.',
                                 code='UNSUPPORTED_ARTIFACT_FORMAT', failure_layer='publication')
        self.compromise = config.get('result_policy', 'compromise') == 'compromise'
        self.critical_failure = False
        self.execution = execution_settings(config.get('execution'))
        self.mode = config['mode']
        if trust_repository and self.mode != 'git':
            raise AuditError('--trust-repository requires git mode; it cannot be used in folder mode.')
        self.source = config[self.mode + '_mode']
        self.source_path = Path(self.source['path' if self.mode == 'folder' else 'repository'])
        self.folder = Folder(self.source_path) if self.mode == 'folder' else None
        self.repo = Repository(self.source_path, trust_repository=trust_repository,
                               reporter=self.reporter) if self.mode == 'git' else None
        self.schemas = MODEL_FOLDER_SCHEMAS if self.mode == 'folder' else MODEL_SCHEMAS
        self.bindings = BindingRegistry()
        self.versions: dict[str, str] = {}

    def record_error(self, manifest, exc, *, phase='run', **context):
        self.reporter.stop_progress()
        recoverable = isinstance(exc, ContractError) or (isinstance(exc, AuditError)
            and exc.code == 'CLI_FAILED' and exc.failure_layer == 'backend')
        if (not recoverable or phase in ('preflight', 'restoration')
                or getattr(exc, 'failure_layer', None) == 'cleanup' or getattr(exc, 'cleanup_failed', False)):
            self.critical_failure = True
        if isinstance(exc, KeyboardInterrupt):
            self.reporter.stop_requested()
        if not any(exc is seen for seen in self.reported_errors):
            self.reported_errors.append(exc)
            if phase in ('run', 'stage') and self.active_stage:
                phase = 'stage'
                context = self.active_stage | context
            detail = self.reporter.error(exc, phase=phase, analysis_started=self.analysis_started,
                switches_performed=bool(getattr(self.repo, 'journal', [])), **context)
            manifest.setdefault('diagnostics', []).append(detail | {'phase': phase} | context)

    def stage_context(self, stage, context):
        return {'branch': context.get('branch', 'all branches' if stage == 'compare' else 'folder'),
                **({'source_name': self.source_path.name or str(self.source_path)} if self.mode == 'folder' else {}),
                'commit': context.get('source_commit'),
                'stage': 'revise' if stage == 'study' and context.get('prompt_variant') == 'revise' else stage,
                'revision_id': context.get('revision_id'),
                'backend': self.cfg['_agents'][stage]['backend']}

    def stage_started(self, stage, context):
        started = self.reporter.clock()
        self.active_stage = self.stage_context(stage, context)
        self.reporter.emit('stage_started', _started=started, **self.active_stage)
        return started

    def stage_finished(self, stage, context, data, started):
        status = data['completion_status']
        if stage == 'review' and data['verdict'] != 'PASS':
            status = 'PARTIAL'
        if stage == 'compare' and status == 'COMPLETE' and not data.get('program_checks', {}).get('policy_satisfied'):
            status = 'PARTIAL'
        self.reporter.emit('stage_completed', **self.stage_context(stage, context), status=status,
                           elapsed_seconds=self.reporter.clock() - started)
        self.active_stage = {}

    def check_cli(self) -> dict:
        result = {}
        for agent in self.cfg['_agents'].values():
            key = agent['backend'] + ':' + agent['executable']
            if key in result:
                if agent.get('expected_version') and agent['expected_version'] != result[key]['version']:
                    raise AuditError('Conflicting expected_version for the same executable.')
                continue
            backend = agent['backend']
            self.reporter.emit('preflight_started', check='cli', backend=backend)
            with tempfile.TemporaryDirectory(prefix='archaudit-cli-check-', dir=neutral_temporary_base(self.source_path)) as raw:
                state = Path(raw).resolve()
                env = cli_env(state)
                adapter = CLI_ADAPTERS.get(backend)
                cmds = [[agent['executable'], '--version'],
                        adapter.help_command(agent['executable']) if adapter else
                        [agent['executable'], 'serve', '--help']]
                texts = []
                for cmd in cmds:
                    r = process(cmd, state, env, reporter=self.reporter,
                                context={'stage': 'preflight', 'backend': backend}, budget=Budget(30))
                    if r['returncode']:
                        raise AuditError(f'{backend} failed its CLI check: exit code {r["returncode"]}.',
                                         code='CLI_CHECK_FAILED',
                                         hint='Check the configured executable and its --version / --help locally.')
                    # Startup warnings are not part of the version identity.
                    output = r['stdout']
                    if cmd[-1] == '--help':
                        output += r['stderr']
                    elif not output.strip():
                        output = r['stderr']
                    texts.append(output.decode(errors='replace'))
                required = adapter.required_flags(self.mode) if adapter else ['--port', '--hostname', '--mdns']
                if any(flag not in texts[1] for flag in required):
                    raise AuditError(f'{backend} lacks required CLI options: {required}',
                                     code='BACKEND_INCOMPATIBLE', failure_kind='BACKEND_INCOMPATIBLE',
                                     failure_layer='compatibility')
                version = texts[0].strip()
                if agent.get('expected_version') and agent['expected_version'] != version:
                    raise AuditError(f'{backend} version differs from expected_version.', code='CLI_VERSION_MISMATCH')
                if backend == 'opencode':
                    opencode.verify_version(version)
                    opencode.verify_native_retries()
                result[key] = {'version': version, 'required_flags': required}
                if backend == 'xxx':
                    result[key]['http'] = self.check_xxx(agent, state, env, version)
                self.reporter.emit('preflight_completed', check='cli', backend=backend, required_flags=required)
        self.versions = {k: v['version'] for k, v in result.items()}
        return result

    def check_xxx(self, agent, cwd, env, version):
        """Local readiness only: no session or model prompt is created."""
        parent = self.run_dir / 'cli-checks' / slug(agent['executable'])
        private_directory(parent)
        artifacts = Path(tempfile.mkdtemp(prefix='attempt-', dir=parent))
        meta = {'backend': 'xxx', 'cli_version': version, 'artifact_directory': str(artifacts),
                'compatibility_profile': xxx.PROFILE,
                'retry_policy': xxx.retry_policy(self.execution['structured_output_repair_attempts']), 'status': 'RUNNING'}
        opencode.prepare_environment(env, 'compare')
        server = xxx.Server(agent['executable'], cwd, env, artifacts,
            Budget(xxx.Server.preflight_seconds(self.execution), label='http_preflight'),
            atomic, meta, self.execution)
        error = None
        try:
            server.start()
            server.verify_api()
        except BaseException as exc:
            error = exc
            meta.update(status='FAILED', error=asdict(diagnostic(exc)))
            raise
        finally:
            try:
                server.close()
            except BaseException as exc:
                if error is None:
                    error = exc
                    meta.update(status='FAILED', error=asdict(diagnostic(exc)))
                    raise
            finally:
                if error is None:
                    meta['status'] = 'SUCCEEDED'
                    save_json(artifacts / 'invocation.json', meta)
                else:
                    with contextlib.suppress(OSError):
                        save_json(artifacts / 'invocation.json', meta)
        return meta

    def command(self, stage: str, state: Path, agent: dict, schema_path: Path, env: dict) -> list[str]:
        adapter = CLI_ADAPTERS.get(agent['backend'])
        if adapter:
            return adapter.build_command(agent, stage, self.mode, self.schemas[stage], schema_path)
        raise AuditError('OpenCode and XXX use the managed HTTP adapter, not the CLI event stream.',
                         code='BACKEND_INCOMPATIBLE')

    def invoke(self, stage: str, context: dict, destination: Path) -> tuple[dict | None, dict]:
        context = dict(context)
        context['stage'] = stage
        self.assert_coverage_file(context)
        if stage != 'compare':
            context['sources'] = source_catalog(context)
        budget = Budget(self.execution['stage_timeout_seconds'], self.execution['idle_timeout_seconds'])
        evidence_pins = None
        if stage != 'compare':
            # Pin actual checkout bytes (including Git's legitimate EOL transforms),
            # not blob bytes. No Git control/history files enter this inventory.
            if self.repo:
                self.repo.assert_expected()
            inventory = (self.folder or Folder(self.source_path, canonical=True)).snapshot(exclude_git=self.repo is not None)
            if context.get('_inventory') and inventory['source_fingerprint'] != context['_inventory']['source_fingerprint']:
                raise UnsafeRepository('Source changed from the inventory used for the coverage plan.', failure_layer='integrity')
            if stage == 'catalog':
                context['_inventory'] = inventory
                context['inventory_summary'] = inventory_summary(inventory)
            if self.folder and inventory['source_fingerprint'] != context['source_fingerprint']:
                raise UnsafeRepository('Source folder changed from the pinned snapshot before invocation.')
            if self.repo:
                self.repo.assert_expected()
            evidence_pins = {e['path']: e['sha256'] for e in inventory['entries'] if e['type'] == 'file'}
        binding = self.bindings.bind(stage, context, self.mode)
        binding_hashes = {}
        attempt_hashes = {}
        native = self.cfg['_agents'][stage]['backend'] in ('xxx', 'opencode')
        limit = self.execution['structured_output_repair_attempts'] if native else 0
        correction = None
        repair_model = None
        repair_source = None
        candidate = None
        candidate_attempt = None
        for repair_index in range(limit + 1):
            try:
                return self._invoke_once(stage, context, destination, budget=budget,
                                         repair_index=repair_index, correction=correction,
                                         repair_model=repair_model, repair_source=repair_source, evidence_pins=evidence_pins,
                                         binding=binding, binding_hashes=binding_hashes, attempt_hashes=attempt_hashes)
            except ContractError as exc:
                self.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
                if not (destination / 'invocation.json').is_file():
                    raise
                meta = strict_json((destination / 'invocation.json').read_text())
                if meta.get('cleanup_errors'):
                    self.critical_failure = True
                    raise
                attempt = Path(meta['artifact_directory'])
                # Recover only the original response, never facts rewritten by a format repair.
                if (self.compromise and repair_index == 0 and meta.get('source_integrity_verified')
                        and meta.get('validation_failed') and meta.get('backend_result_valid')
                        and meta.get('model_identity_verified')
                        and exc.failure_kind in ('SCHEMA_ERROR', 'SEMANTIC_ERROR')):
                    invalid = self.read_attempt_value(attempt, 'expanded.json', attempt_hashes)
                    validation = self.read_attempt_value(attempt, 'validation.json', attempt_hashes)
                    checked = (self.read_attempt_value(attempt, 'normalized.json', attempt_hashes)
                               if validation.get('validated_object') == 'normalized.json' else invalid)
                    candidate = recoverable_material(stage, invalid, context, self.mode,
                        result_diagnostics(stage, checked, context, self.mode))
                    if candidate is not None:
                        candidate['contract_failure'] = asdict(diagnostic(exc))
                        if meta.get('normalization_provenance'):
                            candidate['normalization_provenance'] = meta['normalization_provenance']
                    candidate_attempt = str(attempt)
                can_repair = (exc.failure_kind == 'SCHEMA_ERROR' and repair_index < limit
                              and meta.get('native_envelope_valid') and meta.get('model_identity_verified'))
                if can_repair:
                    expanded = self.read_attempt_value(attempt, 'expanded.json', attempt_hashes)
                    can_repair = (expanded.get('task') == self.schemas[stage]['properties']['task']['enum'][0]
                                  and has_ledger_structure(stage, expanded, representation='wire'))
                if not can_repair:
                    if candidate is None:
                        raise
                    # A failed repair cannot erase the original completed document.
                    # Recheck the source even if the later attempt expired before starting.
                    if self.folder:
                        self.folder.assert_snapshot(context['source_fingerprint'])
                    else:
                        self.repo.assert_expected()
                    candidate['artifact_directory'] = candidate_attempt
                    save_json(destination.parent / (stage + '.material.json'), candidate)
                    material_bytes = (json.dumps(candidate, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
                    meta = meta | {'status': 'PARTIAL', 'usable_material': candidate,
                                   'local_validation': False, 'recovery_source_attempt': candidate_attempt,
                                   'material_retained': True, 'material_hashes': {
                                       stage + '.material.json': {'sha256': digest(material_bytes), 'bytes': len(material_bytes)}}}
                    # Per-attempt files remain the original failed validation record.
                    save_json(destination / 'invocation.json', meta)
                    return None, meta
                # A neutral repair cwd must not silently select another profile model.
                if repair_model is None:
                    repair_model = meta['model_actual']
                invalid = self.read_attempt_value(attempt, 'extracted.json', attempt_hashes)
                if repair_source is None:
                    repair_source = invalid
                details = self.read_attempt_value(attempt, 'validation.json', attempt_hashes)['schema_diagnostics']
                correction = repair_prompt(binding.project(context), self.schemas[stage], invalid, details, original=repair_source)

    def validate_attempt(self, stage, data, context, attempt, meta, binding, repair_source=None):
        candidate = data
        validation = {'valid': False, 'validated_object': 'extracted.json'}
        try:
            self.pin_attempt_value(attempt, 'extracted.json', data, meta,
                                   compact=meta['backend'] in ('opencode', 'xxx'))
            self.save_binding(binding, data, None, attempt, meta)
            binding.validate_identity(data)
            meta['model_identity_verified'] = True
            candidate = binding.expand(data, allow_invalid=True)
            self.save_attempt_value(attempt, 'expanded.json', candidate, meta)
            self.save_binding(binding, data, candidate, attempt, meta)
            validation['validated_object'] = 'expanded.json'
            validate_schema(data, self.schemas[stage])
            expanded = candidate
            if stage in ('study', 'review'):
                candidate, changes = normalize_evidence(stage, expanded, context, self.mode)
                provenance = normalization_provenance(expanded, candidate, changes)
                # Required artifacts precede validation/publication; extracted.json
                # remains the adapter's original object, including on failure.
                self.save_attempt_value(attempt, 'normalized.json', candidate, meta)
                self.save_attempt_value(attempt, 'normalization.json', provenance | {'changes': changes}, meta)
                meta['normalization_provenance'] = provenance
                validation.update(validated_object='normalized.json', normalization_provenance=provenance)
                if changes:
                    self.reporter.emit(stage + '_normalized', **self.stage_context(stage, context),
                                       rule=provenance['rule'], replacement_count=len(changes))
            validate_result(stage, candidate, context, self.mode)
            if repair_source is not None:
                # Compare model outputs, not edits made by our deterministic rule.
                validate_repair(repair_source, data, self.schemas[stage])
            validation['valid'] = True
        except BaseException as exc:
            validation.update(result_diagnostics(stage, candidate, context, self.mode))
            validation['schema_diagnostics'] = schema_diagnostics(data, self.schemas[stage], private=True)
            validation['error'] = asdict(diagnostic(exc))
            meta['local_validation'] = False
            meta['validation_failed'] = True
            with contextlib.suppress(OSError):
                self.save_attempt_value(attempt, 'validation.json', validation, meta)
            raise
        validation.update(result_diagnostics(stage, candidate, context, self.mode))
        meta['local_validation'] = True
        self.save_attempt_value(attempt, 'validation.json', validation, meta)
        if stage == 'study':
            try:
                candidate = materialize_study(candidate)
            except ContractError as exc:
                raise AuditError('Program document assembly failed its invariant checks.',
                                 code='MATERIALIZATION_ERROR', failure_layer='materialization') from exc
            self.save_attempt_value(attempt, 'materialized.json', candidate, meta)
            self.save_attempt_value(attempt, 'provenance.json', {
                'binding': binding.record(data, expanded),
                'normalization': meta['normalization_provenance'],
                'materialization': candidate['materialization_provenance']}, meta)
        return candidate

    def pin_attempt_value(self, attempt, name, value, meta, *, compact=False):
        content = (json.dumps(value) if compact else json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        relative = str((attempt / name).relative_to(attempt.parent.parent))
        meta['attempt_hashes'][relative] = {'sha256': digest(content), 'bytes': len(content)}

    def save_attempt_value(self, attempt, name, value, meta):
        # Pin the program's value, not a reread of bytes writable by a subprocess.
        save_json(attempt / name, value)
        self.pin_attempt_value(attempt, name, value, meta)

    def read_attempt_value(self, attempt, name, hashes):
        relative = str((attempt / name).relative_to(attempt.parent.parent))
        expected = hashes[relative]
        try:
            content = read_confined(self.run_dir, str((attempt / name).relative_to(self.run_dir)), expected['bytes'])
            if len(content) != expected['bytes'] or digest(content) != expected['sha256']:
                raise ValueError('changed')
        except (OSError, ValueError, SourceChanged) as exc:
            self.critical_failure = True
            raise AuditError('Private attempt artifact changed.', code='MODEL_BINDING_CHANGED',
                             failure_layer='integrity') from exc
        return strict_json(content.decode('utf-8'))

    def save_binding(self, binding, raw, expanded, attempt, meta):
        record = binding.record(raw, expanded)
        content = (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        path = attempt / 'binding.json'
        atomic(path, content)
        relative = str(path.relative_to(attempt.parent.parent))
        meta['binding_hashes'][relative] = {'sha256': digest(content), 'bytes': len(content)}

    def assert_binding_files(self, binding, context, directory, hashes):
        try:
            binding.assert_unchanged(context)
            for name, expected in hashes.items():
                relative = str((directory / name).relative_to(self.run_dir))
                content = read_confined(self.run_dir, relative, expected['bytes'])
                if len(content) != expected['bytes'] or digest(content) != expected['sha256']:
                    raise ValueError('changed')
        except (ContractError, OSError, ValueError, SourceChanged) as exc:
            self.critical_failure = True
            raise AuditError('Model binding changed during invocation.', code='MODEL_BINDING_CHANGED',
                             failure_layer='integrity') from exc

    def store_stage(self, item, stage, data, meta, context):
        item[stage], item[stage + '_invocation'] = data, meta
        material = meta.get('usable_material')
        if material is not None:
            item[stage + '_material'] = material
            detail = meta['error'] | self.stage_context(stage, context) | {'phase': 'stage'}
            self.manifest.setdefault('diagnostics', []).append(detail)
            self.reporter.emit('stage_recovered', level=30, **self.stage_context(stage, context),
                               message=material['contract_failure']['message'] +
                               ' Text retained; policy checks not completed. Agent self-assessment: ' +
                               (material['completion_status'] or 'UNAVAILABLE') + '.')
        item[stage + '_usable'] = usable_study(item) if stage == 'study' else bool(stage_document(item, stage))
        if material is not None and not self.cfg['continue_on_error']:
            raise ContractError('Stopped after retaining unvalidated material (continue_on_error=false).')

    def publish_coverage_plan(self, plan, directory):
        verify_coverage_plan(plan)
        content = (json.dumps(plan, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        path = directory / 'coverage.plan.json'
        if path.exists() or path.is_symlink():
            self.assert_coverage_file({'coverage_plan': plan, '_coverage_plan_path': str(path)})
            return
        atomic(path, content)

    def assert_coverage_file(self, context):
        if not context.get('_coverage_plan_path'):
            return
        try:
            verify_coverage_plan(context['coverage_plan'])
            expected = (json.dumps(context['coverage_plan'], ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            relative = str(Path(context['_coverage_plan_path']).relative_to(self.run_dir))
            if read_confined(self.run_dir, relative, len(expected)) != expected:
                raise ValueError('changed')
        except (OSError, ValueError, SourceChanged) as exc:
            raise AuditError('Frozen coverage plan changed.', code='COVERAGE_PLAN_CHANGED', failure_layer='integrity') from exc

    def select_source_revision(self, item, directory, *, publish):
        self.assert_revision_files(item)
        selected = choose_revision(item.get('revisions', []))
        item['selected_revision'] = selected['revision_id'] if selected else None
        item['revision_history'] = [{
            'revision_id': revision['revision_id'], 'directory': revision['directory'],
            'study_status': (stage_document(revision, 'study') or {}).get('completion_status'),
            'review_status': (revision.get('review') or {}).get('completion_status'),
            'review_complete': completed_pair(revision), 'accepted': accepted(revision),
            'errors': list(revision['errors'])} for revision in item.get('revisions', [])]
        for stage in ('study', 'review'):
            for suffix in ('', '_material', '_invocation', '_usable'):
                item.pop(stage + suffix, None)
            item[stage] = None
        if selected:
            for stage in ('study', 'review'):
                if stage == 'review' and not completed_pair(selected):
                    continue
                for suffix in ('', '_material', '_invocation', '_usable'):
                    if stage + suffix in selected:
                        item[stage + suffix] = selected[stage + suffix]
            if publish:
                source = directory / 'revisions' / selected['revision_id']
                names = ['study.json', 'study.material.json', 'study.original.md', 'study.annotated.md',
                         'ARCHITECTURE.md', 'claim.registry.json', 'review.plan.json', 'registry.diff.json']
                if completed_pair(selected):
                    names += ['review.json', 'review.original.md', 'ARCHITECTURE_REVIEW.md']
                expected_files = {}
                for stage in ('study', 'review'):
                    meta = selected.get(stage + '_invocation') or {}
                    expected_files.update(meta.get('artifact_hashes', {}))
                    expected_files.update(meta.get('material_hashes', {}))
                for name in names:
                    path = source / name
                    if name in expected_files or path.exists():
                        expected = expected_files.get(name)
                        blob = read_confined(self.run_dir, str(path.relative_to(self.run_dir)),
                                             expected['bytes'] if expected else path.stat().st_size)
                        if expected and (len(blob) != expected['bytes'] or digest(blob) != expected['sha256']):
                            raise AuditError('Selected revision artifact changed during publication.',
                                             code='REVIEW_TARGET_CHANGED', failure_layer='integrity')
                        atomic(directory / name, blob)
                item['selection_publication_complete'] = True
        item['accepted'] = accepted(item)

    def assert_revision_files(self, item):
        states = list(item.get('revisions', []))
        directory = item.get('directory', '.')
        if item.get('coverage_plan'):
            self.assert_coverage_file({'coverage_plan': item['coverage_plan'],
                                       '_coverage_plan_path': str(self.run_dir / directory / 'coverage.plan.json')})
        states.append({'directory': directory, 'catalog_invocation': item.get('catalog_invocation')})
        if item.get('selection_publication_complete'):
            states.append(item | {'directory': directory, 'selected_aliases': True})
        for revision in states:
            for stage in ('catalog', 'study', 'review'):
                meta = revision.get(stage + '_invocation') or {}
                hashes = dict(meta.get('artifact_hashes', {})) if meta.get('publication_complete') else {}
                if meta.get('material_retained'):
                    hashes.update(meta.get('material_hashes', {}))
                if not revision.get('selected_aliases'):
                    hashes.update(meta.get('binding_hashes', {}))
                    hashes.update(meta.get('attempt_hashes', {}))
                for name, expected in hashes.items():
                    relative = str(Path(revision['directory']) / name)
                    try:
                        blob = read_confined(self.run_dir, relative, expected['bytes'])
                        if len(blob) != expected['bytes'] or digest(blob) != expected['sha256']:
                            raise ValueError('changed')
                    except (OSError, ValueError, SourceChanged) as exc:
                        self.critical_failure = True
                        raise AuditError('Published revision artifact changed.', code='REVIEW_TARGET_CHANGED',
                                         failure_layer='integrity') from exc

    def run_source(self, item, context, directory, persist):
        """One pinned source, one catalog, and at most two independent document pairs."""
        context = dict(context, source_decoding=self.cfg.get('source_decoding', {'rules': []}),
                       source_decoding_access='Decoding rules apply to program evidence checks only; agent reading depends on its CLI.')
        item['revisions'] = []

        def guard(stage_context):
            if self.folder:
                self.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.repo.assert_snapshot(context['source_commit'])
            self.assert_coverage_file(stage_context)
            self.assert_revision_files(item)

        def run_stage(stage, state, stage_context, stage_dir):
            started = self.stage_started(stage, stage_context)
            destination = stage_dir / (stage + '.logs')
            try:
                guard(stage_context)
                data, meta = self.invoke(stage, stage_context, destination)
                self.store_stage(state, stage, data, meta, stage_context)
                if data is not None:
                    self.stage_finished(stage, stage_context, data, started)
                else:
                    self.reporter.emit('stage_completed', **self.stage_context(stage, stage_context),
                                       status='PARTIAL', elapsed_seconds=self.reporter.clock() - started)
                    self.active_stage = {}
                return data
            except (AuditError, ContractError, OSError, UnicodeError) as exc:
                message = f'{stage}: {exc}'
                state.setdefault('errors', []).append(message)
                if state is not item:
                    item['errors'].append(message)
                self.record_error(self.manifest, exc, phase='stage', **self.stage_context(stage, stage_context))
                invocation = destination / 'invocation.json'
                if invocation.is_file():
                    with contextlib.suppress(OSError, ContractError):
                        state[stage + '_invocation'] = strict_json(invocation.read_text())
                if self.critical_failure or not self.cfg['continue_on_error']:
                    raise
                return None
            finally:
                try:
                    guard(stage_context)
                except BaseException:
                    self.critical_failure = True
                    state[stage] = None
                    state.pop(stage + '_material', None)
                    state[stage + '_usable'] = False
                    raise
                finally:
                    persist()

        try:
            guard(context)
            inventory = (self.folder or Folder(self.source_path, canonical=True)).snapshot(exclude_git=self.repo is not None)
            guard(context)
            save_json(directory / 'source.inventory.json', inventory)
            context['_inventory'] = inventory
            context['inventory_summary'] = inventory_summary(inventory)
            catalog = run_stage('catalog', item, context, directory)
            if catalog is None:
                if not self.compromise:
                    return
                plan = build_coverage_plan(None, inventory, context, fallback=True)
                self.publish_coverage_plan(plan, directory)
            else:
                plan = catalog['coverage_plan']
            item['coverage_plan'] = plan
            context.update(coverage_plan=plan, _coverage_plan_path=str(directory / 'coverage.plan.json'))
            context.pop('inventory_summary', None)
            previous = None
            for number in range(1, self.execution['max_revision_rounds'] + 2):
                revision_id = f'{number:03d}'
                revision_dir = directory / 'revisions' / revision_id
                revision = {key: item[key] for key in ('branch', 'source_commit', 'source_directory', 'source_fingerprint') if key in item}
                revision.update(revision_id=revision_id, directory=str(revision_dir.relative_to(self.run_dir)),
                                study=None, review=None, errors=[])
                item['revisions'].append(revision)
                current = context | {'revision_id': revision_id}
                if previous:
                    current.update(revision_inputs(previous))
                    current['prompt_variant'] = 'revise'
                run_stage('study', revision, current, revision_dir)
                if usable_study(revision) and (previous is None or revision.get('study') is not None):
                    current.pop('prompt_variant', None)
                    if previous and revision.get('study'):
                        current['registry_diff'] = revision['study']['registry_diff']
                    review_input = self.freeze_review(stage_document(revision, 'study'), current, revision_dir)
                    if revision.get('study_material') is not None:
                        meta = revision['study_invocation']
                        for name, value in (('claim.registry.json', review_input['claim_registry']),
                                            ('review.plan.json', review_input['review_plan'])):
                            content = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
                            meta['material_hashes'][name] = {'sha256': digest(content), 'bytes': len(content)}
                        save_json(revision_dir / 'study.logs' / 'invocation.json', meta)
                    run_stage('review', revision, review_input, revision_dir)
                else:
                    revision['review_skipped'] = 'No usable strictly valid revised study.' if previous else 'No usable architecture document.'
                    self.reporter.emit('stage_skipped', **self.stage_context('review', current))
                    if self.repo and not usable_study(revision):
                        item['errors'].append('Review skipped: no usable architecture document.')
                revision['accepted'] = accepted(revision)
                persist()
                if not (completed_pair(revision) and revision.get('study') and
                        any(f['severity'] in ('HIGH', 'MEDIUM') for f in revision['review']['findings'])):
                    break
                previous = revision
        except BaseException as exc:
            self.record_error(self.manifest, exc, phase='stage' if self.active_stage else 'run')
            raise
        finally:
            # Even a failed second stage leaves the first published pair intact.
            self.select_source_revision(item, directory, publish=not self.critical_failure)
            persist()

    def publish_result(self, stage, data, destination):
        try:
            if stage == 'study':
                validate_materialized(data)
            validate_schema(data, (SAVED_FOLDER_SCHEMAS if self.mode == 'folder' else SAVED_SCHEMAS)[stage])
        except ContractError as exc:
            raise AuditError('Computed artifact failed local validation.', code='ARTIFACT_CONTRACT_ERROR',
                             failure_layer='publication') from exc
        contents = self.artifact_contents(stage, data)
        # Published revision files are immutable, including JSON and annotations.
        for name, content in contents.items():
            path = destination.parent / name
            if path.exists() or path.is_symlink():
                try:
                    actual = read_confined(self.run_dir, str(path.relative_to(self.run_dir)), len(content))
                    if actual != content:
                        raise ValueError('changed')
                except (OSError, ValueError, SourceChanged) as exc:
                    raise AuditError('Frozen stage artifacts cannot be replaced; select a new run directory.',
                                     code='REVIEW_TARGET_CHANGED', failure_layer='publication') from exc
        for name, content in contents.items():
            path = destination.parent / name
            if not path.exists():
                atomic(path, content)
        return digest(contents['catalog.json' if stage == 'catalog' else ARTIFACTS[stage]])

    def artifact_contents(self, stage, data):
        def encoded(value):
            return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        if stage == 'catalog':
            verify_coverage_plan(data['coverage_plan'])
            return {'coverage.plan.json': encoded(data['coverage_plan']), 'catalog.json': encoded(data)}
        report = data['report_markdown'] if stage == 'study' else render_stage(stage, data, self.cfg.get('output_language'))
        contents = {ARTIFACTS[stage]: report.encode('utf-8'),
                    stage + '.original.md': data['report_markdown'].encode('utf-8'),
                    stage + '.json': encoded(data)}
        if stage == 'study':
            contents.update({'study.annotated.md': render_stage(stage, data, self.cfg.get('output_language')).encode('utf-8'),
                             'claim.registry.json': encoded(data['claims']), 'review.plan.json': encoded(data['review_plan'])})
            if data.get('registry_diff') is not None:
                contents['registry.diff.json'] = encoded(data['registry_diff'])
        return contents

    def assert_review_files(self, context, directory):
        expected = {'claim.registry.json': (json.dumps(context['claim_registry'], ensure_ascii=False, indent=2) + '\n').encode(),
            'review.plan.json': (json.dumps(context['review_plan'], ensure_ascii=False, indent=2) + '\n').encode()}
        if context.get('document_strictly_valid'):
            expected['ARCHITECTURE.md'] = context['architecture_document']['report_markdown'].encode('utf-8')
        for name, content in expected.items():
            try:
                actual = read_confined(self.run_dir, str((directory / name).relative_to(self.run_dir)), len(content))
                if actual != content:
                    raise ValueError('changed')
            except (OSError, ValueError, SourceChanged) as exc:
                raise AuditError('Published document, registry or review plan changed.',
                                 code='REVIEW_TARGET_CHANGED', failure_layer='integrity') from exc

    def freeze_review(self, doc, context, directory):
        result = review_context(doc, context)
        if result['document_strictly_valid']:
            self.assert_review_files(result, directory)
        else:
            pending = {'claim.registry.json': result['claim_registry'], 'review.plan.json': result['review_plan']}
            for name, value in pending.items():
                path = directory / name
                expected = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
                if path.exists() or path.is_symlink():
                    try:
                        if read_confined(self.run_dir, str(path.relative_to(self.run_dir)), len(expected)) != expected:
                            raise ValueError('changed')
                    except (OSError, ValueError, SourceChanged) as exc:
                        raise AuditError('Recovered material cannot replace an existing frozen registry or plan.',
                                         code='REVIEW_TARGET_CHANGED', failure_layer='publication') from exc
            for name, value in pending.items():
                if not (directory / name).exists():
                    save_json(directory / name, value)
        return result

    def publish_blocked_comparison(self, context, destination):
        data = blocked_comparison(context)
        meta = {'stage': 'compare', 'generated_by': 'orchestrator',
                'reason': 'no_comparable_studies' if self.compromise else 'no_accepted_branches',
                'started_at': now(), 'status': 'RUNNING'}
        private_directory(destination)
        try:
            self.repo.assert_expected()
            validate_result('compare', data, context)
            meta['local_validation'] = True
            self.repo.assert_expected()
            data = prepare_result('compare', data, context | {'generated_by': 'orchestrator'})
            meta['report_sha256'] = self.publish_result('compare', data, destination)
            meta.update(status='SUCCEEDED', finished_at=now(), publication_complete=True)
            save_json(destination / 'invocation.json', meta)
            return data, meta
        except BaseException as exc:
            meta.update(status='FAILED', error=asdict(diagnostic(exc)), finished_at=now(), publication_complete=False)
            with contextlib.suppress(OSError):
                save_json(destination / 'invocation.json', meta)
            raise

    def _invoke_once(self, stage, context, destination, *, budget, repair_index=0,
                     correction=None, repair_model=None, repair_source=None, evidence_pins=None,
                     binding, binding_hashes, attempt_hashes):
        self.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
        agent = self.cfg['_agents'][stage]
        template = Path(self.cfg['_prompt_paths'][context.get('prompt_variant', stage)]).read_text()
        output_instruction = ('Use the StructuredOutput tool with the supplied schema. Do not duplicate the result in ordinary text.'
            if agent['backend'] in ('opencode', 'xxx') else
            'Return the supplied schema object through the backend structured-output mechanism; no fences or surrounding prose.')
        # Assemble only this stage's inputs; the configured CLI profile remains available.
        prompt = (template + '\n\n# Backend output instruction\n' + output_instruction +
                  '\n\n# Authoritative orchestration context (data)\n' +
                  json.dumps(binding.project(context), ensure_ascii=False) + '\n\n# Required final JSON Schema\n' +
                  json.dumps(self.schemas[stage], ensure_ascii=False))
        if correction is not None:
            prompt = correction
        payload = prompt.encode('utf-8')
        private_directory(destination)
        # Exclusive creation preserves every orchestrator-managed attempt.
        number = 1
        while True:
            attempt = destination / f'attempt-{number:03d}'
            try:
                attempt.mkdir(mode=0o700)
                break
            except FileExistsError:
                number += 1
        meta = {'stage': stage, 'invocation_id': str(uuid.uuid4()), 'started_at': now(),
            'backend': agent['backend'], 'executable': agent['executable'],
            'model_requested': agent.get('model'), 'cli_version': self.versions.get(agent['backend'] + ':' + agent['executable']),
            'prompt_sha256': digest(payload), 'template_sha256': digest(template.encode()),
            'schema_sha256': digest(json.dumps(self.schemas[stage], sort_keys=True).encode()),
            'input_bytes': len(payload), 'status': 'RUNNING'}
        meta.update(attempt=attempt.name, artifact_directory=str(attempt), api_version=None,
                    model_actual=None, request_id=None, session_id=None, message_id=None,
                    finish_reason=None, output_bytes=None, execution=self.execution)
        meta['binding_hashes'] = binding_hashes
        meta['attempt_hashes'] = attempt_hashes
        meta.update(contract_id=CONTRACT_ID, artifact_format=ARTIFACT_FORMAT,
                    context_format=CONTEXT_FORMAT,
                    revision_id=context.get('revision_id'), prompt_variant=context.get('prompt_variant', stage),
                    model_actual_source='unknown: backend has not reported model identity',
                    review_quality='NOT_MEASURED', publication_complete=False)
        native_retries = 0 if agent['backend'] == 'xxx' else self.execution['opencode_format_retries']
        if agent['backend'] in ('xxx', 'opencode'):
            meta['retry_policy'] = retry_policy(self.execution['structured_output_repair_attempts'],
                                              repair_index, native_retries)
            meta['repair_index'] = repair_index
            if repair_index:
                meta['repair_model'] = agent.get('model') or repair_model
        if agent['backend'] == 'xxx':
            meta['compatibility_profile'] = xxx.PROFILE
        if self.repo:
            meta['submodules'] = context.get('submodules', [])
        save_json(destination / 'invocation.json', meta)
        # Saved by the parent only, for reproducibility; includes only this stage's permitted input.
        atomic(attempt / 'input.prompt.txt', payload)
        save_json(attempt / 'schema.json', self.schemas[stage])
        save_json(attempt / 'validation.json', {'valid': False, 'status': 'not_run'})
        try:
            self.save_binding(binding, None, None, attempt, meta)
            budget.check()
            self.assert_coverage_file(context)
            if self.folder:
                self.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.repo.assert_expected()
            with tempfile.TemporaryDirectory(prefix='archaudit-invocation-', dir=neutral_temporary_base(self.source_path)) as raw:
                state = Path(raw).resolve()
                cwd = state if stage == 'compare' or repair_index else self.source_path
                env = cli_env(cwd)
                schema_path = state / 'output.schema.json'
                save_json(schema_path, self.schemas[stage])
                try:
                    if agent['backend'] in ('opencode', 'xxx'):
                        # This gate is repeated for callers using Runner.invoke directly.
                        if agent['backend'] == 'opencode':
                            opencode.verify_version(meta['cli_version'])
                            opencode.verify_native_retries()
                        name = opencode.prepare_environment(env, 'repair' if repair_index else stage)
                        server_class = xxx.Server if agent['backend'] == 'xxx' else opencode.Server
                        server = server_class(agent['executable'], cwd, env, attempt, budget, atomic, meta, self.execution)
                        error = None
                        try:
                            server.start()
                            server.verify_api()
                            data = server.invoke(prompt, self.schemas[stage], name, agent.get('model') or repair_model,
                                                 native_retries)
                            meta['native_envelope_valid'] = True
                            meta['backend_result_valid'] = True
                            self.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
                            data = self.validate_attempt(stage, data, context, attempt, meta, binding, repair_source)
                        except BaseException as exc:
                            error = exc
                            raise
                        finally:
                            try:
                                server.close()
                            except BaseException:
                                self.critical_failure = True
                                meta.setdefault('cleanup_errors', []).append('cleanup_failed')
                                if error is None:
                                    raise
                    else:
                        cmd = self.command(stage, state, agent, schema_path, env)
                        r = process(cmd, cwd, env, payload, reporter=self.reporter,
                                    context=self.stage_context(stage, context), log_dir=attempt,
                                    clock=self.reporter.clock, progress_interval=self.reporter.progress_interval,
                                    budget=budget)
                        meta.update(returncode=r['returncode'], output_bytes=len(r['stdout']))
                        if r['returncode']:
                            raise AuditError(f'{agent["backend"]} exited with {r["returncode"]}; inspect private attempt logs.',
                                code='CLI_FAILED', failure_kind='BACKEND_ERROR', failure_layer='backend')
                        data, provider_meta = CLI_ADAPTERS[agent['backend']].parse_output(r['stdout'].decode('utf-8'))
                        meta['backend_result_valid'] = True
                        meta['provider_metadata'] = provider_meta
                        model_usage = provider_meta.get('modelUsage')
                        if type(model_usage) is dict and model_usage:
                            meta['models_reported'] = list(model_usage)
                            if len(model_usage) == 1:
                                meta['model_actual'] = next(iter(model_usage))
                            meta['model_actual_source'] = 'backend modelUsage; may include multiple models'
                        save_json(attempt / 'extracted.json', data)
                        self.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
                        data = self.validate_attempt(stage, data, context, attempt, meta, binding)
                finally:
                    self.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
                    self.assert_coverage_file(context)
                    if self.folder:
                        self.folder.assert_snapshot(context['source_fingerprint'])
                    else:
                        self.repo.assert_expected()
                    meta['source_integrity_verified'] = True
                budget.check()
            # Publish after the CLI exits and temporary invocation files are removed.
            try:
                data = prepare_result(stage, data, context, evidence_pins)
                if stage in ('study', 'review'):
                    data['normalization_provenance'] = meta['normalization_provenance']
            except SourceChanged as exc:
                meta['source_integrity_verified'] = False
                meta['source_check_status'] = 'CHANGED_DURING_EVIDENCE_RESOLUTION'
                raise UnsafeRepository('Source changed during evidence resolution.', code='SOURCE_CHANGED',
                                       failure_kind='SOURCE_CHANGED', failure_layer='integrity') from exc
            if self.folder:
                self.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.repo.assert_expected()
            meta['source_check_status'] = 'MATCHED_AT_BOUNDARIES'
            budget.check()
            if agent['backend'] in ('xxx', 'opencode') and meta.get('model_actual'):
                meta['model_actual_source'] = 'backend assistant-message metadata'
            if stage == 'review':
                self.assert_review_files(context, destination.parent)
            self.assert_coverage_file(context)
            self.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
            report_hash = self.publish_result(stage, data, destination)
            artifact_hashes = {name: {'sha256': digest(blob), 'bytes': len(blob)}
                               for name, blob in self.artifact_contents(stage, data).items()}
            meta.update(status='SUCCEEDED', finished_at=now(), report_sha256=report_hash, publication_complete=True)
            meta['artifact_hashes'] = artifact_hashes
            meta['duration_seconds'] = round(budget.clock() - budget.started, 3)
            save_json(attempt / 'invocation.json', meta)
            save_json(destination / 'invocation.json', meta)
            return data, meta
        except BaseException as exc:
            if getattr(exc, 'cleanup_failed', False):
                self.critical_failure = True
                meta.setdefault('cleanup_errors', []).append('process_cleanup_failed')
            if repair_index and not meta.get('prompt_sent'):
                meta['retry_policy'] = retry_policy(self.execution['structured_output_repair_attempts'],
                                                  repair_index - 1, native_retries)
            meta.update(status='FAILED', finished_at=now(), error=asdict(diagnostic(exc)),
                        publication_complete=False, duration_seconds=round(budget.clock() - budget.started, 3))
            # A failing diagnostic write must not mask the original exception.
            with contextlib.suppress(OSError):
                save_json(attempt / 'invocation.json', meta)
                save_json(destination / 'invocation.json', meta)
            raise

    def run(self, check_only: bool = False) -> tuple[dict, int]:
        try:
            manifest, code = self.run_folder(check_only) if self.folder else self.run_git(check_only)
        finally:
            self.reporter.stop_progress()
            if self.repo:
                try:
                    self.repo.close()
                except OSError as exc:
                    self.record_error(self.manifest, exc, phase='restoration')
        manifest['result_policy'] = 'compromise' if self.compromise else 'strict'
        manifest.update(contract_id=CONTRACT_ID, artifact_format=ARTIFACT_FORMAT,
            acceptance_meaning='accepted=true means policy checks satisfied; factual correctness is not established.',
            review_quality='NOT_MEASURED', source_check_meaning='MATCHED_AT_BOUNDARIES means source state matched at performed checks only.')
        manifest['critical_failure'] = self.critical_failure
        if not check_only:
            entries = report_entries(manifest, self.source, self.mode)
            if self.compromise and code != 130:
                if self.critical_failure or not any(usable_study(b) for b in entries):
                    manifest['status'], code = 'FAILED', 1
                elif (all(accepted(b) for b in entries) and not manifest.get('diagnostics')
                      and (self.mode == 'folder' or len(entries) == 1
                           or (manifest.get('comparison', {}).get('program_checks', {}).get('policy_satisfied')
                               and manifest.get('comparison_invocation', {}).get('publication_complete')))):
                    manifest['status'], code = 'COMPLETE', 0
                else:
                    manifest['status'], code = 'PARTIAL', 2
            elif self.critical_failure and code != 130:
                manifest['status'], code = 'FAILED', 1
            try:
                report, useful = render_final_report(manifest, self.source, self.mode,
                                                     self.cfg.get('output_language', 'Russian'))
                path = self.run_dir / 'FINAL_REPORT.md'
                atomic(path, report)
                manifest.update(final_report=str(path), has_usable_material=useful)
            except OSError as exc:
                self.record_error(manifest, exc, phase='publication')
                manifest.update(status='FAILED', critical_failure=True)
                code = 130 if code == 130 else 1
        manifest['exit_code'] = code
        # This last manifest write commits the run's publication record. Individual
        # file replacements above are atomic; the group is not a transaction.
        manifest['publication_complete'] = code in (0, 2)
        if self.critical_failure and 'accepted' in manifest:
            manifest['accepted'] = False
        save_json(self.run_dir / 'manifest.json', manifest)
        return manifest, code

    def run_git(self, check_only: bool = False) -> tuple[dict, int]:
        manifest = {'run_id': self.run_dir.name,
            'started_at': now(), 'repository': str(self.repo.path),
            'git': self.repo.runtime.manifest(),
            'baseline_branch': self.source['baseline_branch'], 'status': 'RUNNING',
            'isolation': 'cli-native-permissions', 'platform': sys.platform,
            'branches': [], 'errors': []}
        manifest['publication_complete'] = False
        self.manifest = manifest
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
            self.active_stage = {}
            self.reporter.emit('restoration_started')
            try:
                manifest['restoration'] = self.repo.restore(original_branch, original_commit)
                restored = True
                self.reporter.emit('restoration_completed')
            except BaseException as exc:
                manifest['restoration'] = {'restored': False, 'node': getattr(exc, 'node', None),
                                           'error': str(exc) or type(exc).__name__}
                self.record_error(manifest, exc, phase='restoration')
                raise
            finally:
                persist()

        try:
            if not self.cfg['project_description']:
                self.reporter.emit('description_missing')
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
                    self.analysis_started = True
                    self.active_stage = {}
                    self.reporter.emit('branch_started', branch=branch, commit=commit)
                    branch_dir = self.run_dir / 'branches' / slug(branch)
                    item: dict[str, Any] = {'branch': branch, 'source_commit': commit,
                        'directory': str(branch_dir.relative_to(self.run_dir)),
                        'study': None, 'review': None, 'errors': []}
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
                    self.run_source(item, context, branch_dir, persist)
                    item['accepted'] = accepted(item)
                    persist()
            except BaseException as exc:
                analysis_error = exc
                self.record_error(manifest, exc)
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
            entries = [existing.get(b, {'branch': b, 'source_commit': pins[b], 'study': None,
                       'review': None, 'errors': ['Branch was not analyzed.']}) for b in self.source['branches']]
            quality_ok = all(accepted(b) for b in entries)
            if len(self.source['branches']) > 1:
                baseline = self.source['baseline_branch']
                bundle = {'baseline_branch': baseline, 'baseline_commit': pins[baseline],
                    'result_policy': 'compromise' if self.compromise else 'strict',
                    'requested_branches': self.source['branches'], 'output_language': self.cfg['output_language'],
                    'project_description': self.cfg['project_description'],
                    'scope': 'reports-only comparison; source inspection is outside task scope',
                    'branches': [{k: b.get(k) for k in ('branch', 'source_commit', 'submodules', 'study', 'review',
                                                       'study_material', 'review_material', 'errors', 'selected_revision', 'coverage_plan')} |
                                 {'accepted': accepted(b)} |
                                 {stage + '_invocation': {k: (b.get(stage + '_invocation') or {}).get(k) for k in
                                    ('publication_complete', 'contract_id', 'artifact_format', 'status', 'model_requested', 'model_actual',
                                     'model_actual_source', 'source_check_status', 'review_quality')}
                                  for stage in ('study', 'review')} for b in entries],
                    'git_deltas': {b: self.repo.delta(pins[baseline], pins[b]) for b in self.source['branches'] if b != baseline}}
                bundle['required_unresolved_branches'] = required_unresolved(bundle)
                comp_dir = self.run_dir / 'comparison'
                save_json(comp_dir / 'inputs.json', bundle)
                started = self.stage_started('compare', bundle)
                try:
                    self.repo.assert_expected()
                    for entry in entries:
                        self.assert_revision_files(entry)
                    can_compare = (comparison_possible(entries, baseline) if self.compromise
                                   else any(accepted(b) for b in entries))
                    if can_compare:
                        comparison, meta = self.invoke('compare', bundle, comp_dir / 'compare.logs')
                    else:
                        comparison, meta = self.publish_blocked_comparison(bundle, comp_dir / 'compare.logs')
                finally:
                    # Compare also uses the user's profile, so verify the restored
                    # checkout even though this stage runs outside the repository.
                    try:
                        self.repo.assert_expected()
                        for entry in entries:
                            self.assert_revision_files(entry)
                    except AuditError as exc:
                        manifest['restoration'] = {'restored': False, 'node': getattr(exc, 'node', None), 'error': str(exc)}
                        self.record_error(manifest, exc, phase='restoration')
                        raise
                manifest['comparison'] = comparison
                manifest['comparison_invocation'] = meta
                self.stage_finished('compare', bundle, comparison, started)
                quality_ok = quality_ok and comparison['program_checks']['policy_satisfied'] and meta['publication_complete']
            failed = any(b['errors'] for b in entries)
            manifest['status'] = 'FAILED' if failed else 'COMPLETE' if quality_ok else 'PARTIAL'
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
            self.record_error(manifest, exc, phase='run' if self.analysis_started else 'preflight')
        manifest['finished_at'] = now()
        manifest['exit_code'] = code
        persist()
        return manifest, code

    def run_folder(self, check_only: bool = False) -> tuple[dict, int]:
        manifest = {'mode': 'folder', 'run_id': self.run_dir.name,
            'started_at': now(), 'source_directory': str(self.source_path), 'status': 'RUNNING',
            'isolation': 'cli-native-permissions', 'platform': sys.platform,
            'integrity': 'Fingerprints at stage boundaries; not a backup or continuous immutability guarantee.',
            'study': None, 'review': None, 'accepted': False, 'errors': []}
        manifest['publication_complete'] = False
        self.manifest = manifest
        def persist():
            save_json(self.run_dir / 'manifest.json', manifest)
        persist()
        try:
            if not self.cfg['project_description']:
                self.reporter.emit('description_missing')
            self.reporter.emit('preflight_started', check='inventory')
            inventory = self.folder.snapshot()
            save_json(self.run_dir / 'source.inventory.json', inventory)
            fingerprint = inventory['source_fingerprint']
            manifest.update(source_fingerprint=fingerprint, inventory='source.inventory.json')
            self.reporter.emit('preflight_completed', check='inventory')
            manifest['cli_checks'] = self.check_cli()
            self.folder.assert_snapshot(fingerprint)
            self.reporter.emit('preflight_completed', check='integrity')
            persist()
            if check_only:
                manifest['status'], code = 'PREFLIGHT_OK', 0
            else:
                self.analysis_started = True
                context = {'source_mode': 'folder', 'source_directory': str(self.source_path),
                    'source_fingerprint': fingerprint, 'output_language': self.cfg['output_language'],
                    'project_description': self.cfg['project_description'],
                    'priority_scenarios': self.cfg['priority_scenarios'], 'execution_mode': 'static-only',
                    'source_access': 'Current directory tree, including hidden files; do not follow symlinks or use Git. '
                                     'Native CLI permissions; fingerprints verify stage boundaries only.'}
                self.run_source(manifest, context, self.run_dir, persist)
                manifest['accepted'] = accepted(manifest)
                manifest['status'] = 'FAILED' if manifest['errors'] else 'COMPLETE' if manifest['accepted'] else 'PARTIAL'
                code = 1 if manifest['errors'] else 0 if manifest['accepted'] else 2
        except BaseException as exc:
            manifest['errors'].append(str(exc) or type(exc).__name__)
            manifest['status'] = 'FAILED'
            code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            self.record_error(manifest, exc, phase='run' if self.analysis_started else 'preflight')
        manifest.update(finished_at=now(), exit_code=code)
        persist()
        return manifest, code

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=
        'After argument parsing, stdout contains one result. --help and argument syntax errors '
        'use standard argparse output without a run or manifest. Console messages are in English; '
        'output_language only controls generated reports. Labels and statuses are colored automatically '
        'when their output stream is a terminal, unless NO_COLOR is nonempty or TERM=dumb. '
        'JSON is never colored. Disable color with NO_COLOR=1.')
    parser.add_argument('--config', required=True, type=Path, help='Path to a JSON or JSONC configuration.')
    parser.add_argument('--check', action='store_true', help='Check configuration, source state, and CLI capabilities; no branch switch or model calls.')
    parser.add_argument('--trust-repository', action='store_true', help='Trust only the configured Git checkout and verified submodules despite an ownership mismatch (Git >= 2.34.1).')
    parser.add_argument('--output', choices=('auto', 'text', 'json'), default='auto',
                        help='Result format on stdout: text is human-readable; auto selects text for a TTY, JSON otherwise (default: auto). Diagnostics use stderr.')
    parser.add_argument('--verbose', action='store_true', help='Add technical event details to stderr; never print prompts, credentials or raw model output.')
    parser.add_argument('--no-progress', action='store_true', help='Disable the spinner and periodic waiting messages; keep stage boundaries, warnings and errors.')
    args = parser.parse_args()
    reporter = Reporter(mode=output_mode(args.output, sys.stdout), verbose=args.verbose, progress=not args.no_progress)
    run_id = run_dir = runner = None
    run_created = False
    manifest, code = {}, 1
    header_shown = False
    config = None
    phase = 'configuration'
    old_umask = os.umask(0o077)
    old_handler = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'Received signal {signum}')

    def header():
        nonlocal header_shown
        if header_shown:
            return
        header_shown = True
        mode = config['mode'] if config else None
        source = config[mode + '_mode'] if config else {}
        reporter.emit('run_started', check_only=args.check, output=reporter.mode, mode=mode,
                      source=source.get('path' if mode == 'folder' else 'repository'))
        if os.geteuid() == 0:
            reporter.emit('root_warning')

    try:
        signal.signal(signal.SIGTERM, interrupted)
        if sys.platform not in ('darwin', 'linux') or sys.version_info < (3, 11):
            raise AuditError('This implementation requires macOS or Linux and Python 3.11+.')
        try:
            config = load_config(args.config.resolve())
        except ContractError as exc:
            raise AuditError('The configuration is not valid JSON/JSONC.', code='CONFIGURATION_ERROR',
                             hint='Check the configuration syntax and run --check again.') from exc
        reports = Path(config['reports_dir'])
        run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:10]
        run_dir = reports / run_id
        reporter.run_id = run_id
        header()
        mode = config['mode']
        source = config[mode + '_mode']
        source_path = Path(source['path' if mode == 'folder' else 'repository'])
        repository_name = source_path.name or str(source_path)
        phase = 'preflight'
        try:
            runner = Runner(config, run_dir, trust_repository=args.trust_repository, reporter=reporter)
            if runner.repo:
                repository_name = runner.repo.display_name()
        finally:
            reporter.emit('configuration_loaded', repository_name=repository_name, branches=source.get('branches', []),
                          agents={stage: agent['backend'] for stage, agent in config['_agents'].items()})
            reporter.emit('preflight_completed', check='configuration')
        reports.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_dir.mkdir(mode=0o700)
        run_created = True
        reporter.attach_log(run_dir)
        snapshot = {k: v for k, v in config.items() if not k.startswith('_')}
        save_json(run_dir / 'config.snapshot.json', snapshot)
        with repository_lock(runner.source_path):
            manifest, code = runner.run(check_only=args.check)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            reporter.stop_requested()
        if isinstance(exc, AuditError) and phase == 'configuration' and exc.code == 'AUDIT_ERROR':
            exc.code = 'CONFIGURATION_ERROR'
        header()
        manifest = getattr(runner, 'manifest', manifest)
        code = 130 if isinstance(exc, KeyboardInterrupt) or manifest.get('exit_code') == 130 else 1
        manifest.update(run_id=run_id, status='FAILED', exit_code=code, finished_at=now(), publication_complete=False)
        manifest.setdefault('errors', []).append(str(exc) or type(exc).__name__)
        if runner:
            runner.record_error(manifest, exc, phase='run' if runner.analysis_started else 'preflight')
        else:
            manifest.setdefault('diagnostics', []).append(reporter.error(exc, phase='preflight',
                analysis_started=False, switches_performed=False))
        if run_created:
            try:
                save_json(run_dir / 'manifest.json', manifest)
            except OSError as save_error:
                reporter.error(save_error)
    finally:
        # Presentation failures never bypass source restoration or resource cleanup.
        if runner and runner.repo:
            try:
                runner.repo.close()
            except OSError as close_error:
                reporter.error(close_error)
                manifest['status'] = 'FAILED'
                code = 130 if code == 130 else 1
        signal.signal(signal.SIGTERM, old_handler)
        os.umask(old_umask)
        manifest_path = run_dir / 'manifest.json' if run_dir else None
        result = {'run_id': run_id, 'status': manifest.get('status', 'FAILED'),
                  'manifest': str(manifest_path) if existing_file(manifest_path) else None,
                  'final_report': manifest.get('final_report'),
                  'has_usable_material': manifest.get('has_usable_material', False),
                  'exit_code': code}
        result['status_meaning'] = ('Local launch prerequisites checked. Source analysis was not performed; '
            'model availability and provider authorization were not tested.' if result['status'] == 'PREFLIGHT_OK' else
            'Policy checks satisfied; factual correctness is not established.' if result['status'] == 'COMPLETE' else
            'Processing may be incomplete or evidence may be insufficient; consult limitations and diagnostics.')
        result['review_quality'] = 'NOT_MEASURED'
        try:
            reporter.finish(result, manifest, check_only=args.check, config_path=args.config,
                            run_dir=run_dir, trust=args.trust_repository)
        finally:
            reporter.close()
    return code


if __name__ == '__main__':
    code = main()
    # Broken redirected streams must not change the exit code during interpreter shutdown.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            with contextlib.suppress(OSError, ValueError), open(os.devnull, 'w') as sink:
                os.dup2(sink.fileno(), stream.fileno())
    raise SystemExit(code)
