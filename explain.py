#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Sequential architecture auditing with configured coding-agent CLIs.

macOS or Linux, Python 3.11+, and one or more authenticated coding-agent CLIs.
Git >= 2.34.1 is required only for git mode; folder mode needs no Git.
Textual provides the interactive interface. Git inputs use source copies without worktree switches.
The parent publishes reports. The agent receives stdin and returns structured JSON.
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
from src.contracts.contracts import MODEL_FOLDER_SCHEMAS, MODEL_SCHEMAS, ContractError, accepted, workflow_satisfied, jsonc, strict_json, validate_result, schema_diagnostics, result_diagnostics
from src.runtime.reporting import Diagnostic, NullReporter, Reporter, diagnostic, existing_file, output_mode
from src.runtime.execution import Budget, execution_settings
from src.runtime.metrics import RunMetrics, measurement, usage, FIELDS
from src.backends import codex
from src.backends import claude_code
from src.backends import opencode_cli
from src.backends import xxx
from src.model.structured_output import blocked_comparison, required_unresolved, retry_policy
from src.reports.final_report import recoverable_material, stage_document, usable_study, report_entries, comparison_possible, render_final_report
from src.analysis.ledger import prepare_result, review_context
from src.analysis.study_normalization import normalize_evidence, normalization_provenance
from src.reports.document_rendering import materialize_study, validate_materialized
from src.analysis.evidence import source_catalog, SourceChanged, open_source_directory, read_confined, stamp
from src.contracts.contracts import CONTRACT_ID, ARTIFACT_FORMAT, validate_schema, response_error
from src.contracts.saved_contracts import SAVED_SCHEMAS, SAVED_FOLDER_SCHEMAS
from src.reports.presentation import render_stage
from src.model.model_context import CONTEXT_FORMAT
from src.model.model_boundary import BindingRegistry
from src.analysis.source_decoding import normalize_source_decoding
from src.analysis.source_filter import normalize_source_filter, excluded_root, gitignore_spec, gitignore_match
from src.analysis.coverage_plan import build_coverage_plan, inventory_summary, verify_coverage_plan, recover_catalog_paths
from src.analysis.revisions import revision_inputs, choose_revision, completed_pair
from src.analysis.git_sources import GitSources
from src.analysis.source_metrics import PhysicalLines
from src.analysis.analysis_plan import DEFAULT_MAX_SOURCE_BYTES_PER_SESSION, build_analysis_plan, verify_analysis_plan
from src.analysis.study_shards import empty_shard_result, require_shard_policy, synthesis_inputs

ROOT = Path(__file__).resolve().parent
STAGES = ('catalog', 'study', 'review', 'compare')
SOURCE_STAGES = ('catalog', 'study', 'review')
ARTIFACTS = {'catalog': 'SUBSYSTEM_CATALOG.md', 'study': 'ARCHITECTURE.md', 'review': 'ARCHITECTURE_REVIEW.md',
             'compare': 'BRANCH_COMPARISON.md'}
BACKENDS = {'codex': 'codex', 'claude-code': 'claude', 'opencode': 'opencode', 'xxx': 'xxx'}
CLI_ADAPTERS = {'codex': codex, 'claude-code': claude_code, 'opencode': opencode_cli, 'xxx': xxx}
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
    env.update(PWD=str(cwd.resolve()), NO_COLOR='1', GIT_TERMINAL_PROMPT='0', OPENCODE_SKIP_SAFE_CHECK='1')
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
    # A stage-owned Reporter also covers silent subprocess waits.
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
        # Historical name retained for callers; dirtiness is valid input.
        if self.git('ls-files', '--unmerged', '-z'):
            raise UnsafeRepository('Conflicted Git index.', node_path=str(self.path))
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
        for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-apply', 'rebase-merge',
                       'BISECT_START', 'sequencer', 'index.lock', 'HEAD.lock'):
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
                    # Working submodules use their actual checkout, independently
                    # of the base/index gitlink. Commit plans are validated below.
                    required = actual
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
        self.base_plan = {}
        visit(self, '.', None, None, initial['.'], 'working tree base', self.base_plan)
        self.plans[initial['.']] = self.base_plan
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
                    raise UnsafeRepository('Original branch moved during source preparation.')
                node.policy()
                node.clean(local=True)
            except (AuditError, OSError) as exc:
                detail = exc if isinstance(exc, AuditError) else AuditError(str(exc), code='IO_ERROR')
                error = UnsafeRepository(**vars(detail.with_context(node_path=path).diagnostic()))
                error.node = path
                raise error from exc

    def submodules(self, commit: str, verified: bool = False) -> list[dict]:
        if verified:
            self.assert_expected()
        return [dict(self.descriptions[path], expected_commit=sha, available_locally=True,
                     actual={'commit': self.expected[path]['commit'], 'ref': self.expected[path]['ref']}, snapshot_verified=verified)
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
    def __init__(self, path: Path, *, canonical=False, source_filter=None):
        self.path = path if canonical else path.resolve()
        self.inventory = None
        self.metadata = {}
        self.files = {}
        self.exclude_git = False
        self.source_filter = normalize_source_filter(source_filter if source_filter is not None else {})
        self.ignore_rules = {}

    def _scan(self, *, exclude_git=False, files=None, expected=None):
        entries, metadata = [], {}
        rules, exclusions = {}, []
        filtered = self.source_filter['follow_gitignore'] or self.source_filter['exclude_paths']

        def changed(name):
            raise AuditError('Source folder changed during the run; files will not be restored.',
                             code='SOURCE_CHANGED', failure_layer='integrity', node_path=name)

        def record(info, name):
            # Excluded additions/removals can change a parent's timestamps.
            metadata[name] = stamp(info)[:3] if filtered and stat.S_ISDIR(info.st_mode) else stamp(info)
            if expected is not None and metadata[name] != expected.get(name):
                changed(name)

        def unchanged(before, after, name):
            if stamp(before) != stamp(after):
                if expected is not None:
                    changed(name)
                raise AuditError(f'Source changed while fingerprinting: {name}')

        def load_rules(fd, relative, scopes):
            child = '.gitignore' if relative == '.' else relative + '/.gitignore'
            if not self.source_filter['follow_gitignore'] or excluded_root(child, self.source_filter):
                return scopes
            try:
                info = os.stat('.gitignore', dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return scopes
            if not stat.S_ISREG(info.st_mode):
                return scopes
            record(info, child)
            if expected is not None:
                rule = self.ignore_rules[child]
            else:
                rule_fd = os.open('.gitignore', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    unchanged(info, os.fstat(rule_fd), child)
                    data = bytearray()
                    while block := os.read(rule_fd, 1024 * 1024):
                        data.extend(block)
                    unchanged(info, os.fstat(rule_fd), child)
                finally:
                    os.close(rule_fd)
                lines = PhysicalLines()
                lines.update(data)
                rule = {'sha256': digest(data), 'source_lines': lines.count,
                        'spec': gitignore_spec(data)}
            unchanged(info, os.stat('.gitignore', dir_fd=fd, follow_symlinks=False), child)
            rules[child] = rule
            return (*scopes, (relative, child, rule['spec']))

        def directory(fd, relative, scopes=()):
            before = os.fstat(fd)
            record(before, relative)
            entries.append({'path': relative, 'type': 'directory',
                            'mode': format(stat.S_IMODE(before.st_mode), '04o')})
            scopes = load_rules(fd, relative, scopes)
            for name in sorted(os.listdir(fd)):
                if exclude_git and name == '.git':
                    continue
                child = name if relative == '.' else relative + '/' + name
                root = excluded_root(child, self.source_filter)
                if root:
                    exclusions.append({'path': child, 'origin': 'CONFIG', 'rule': root})
                    continue
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if child in rules and stamp(info) != metadata[child]:
                    changed(child)
                ignored = None
                for base, rule_path, spec in scopes:
                    local = child if base == '.' else child[len(base) + 1:]
                    include, line = gitignore_match(spec, local, stat.S_ISDIR(info.st_mode))
                    if include is not None:
                        ignored = {'path': child, 'origin': 'GITIGNORE', 'rule_file': rule_path,
                                   'line': line} if include else None
                if ignored:
                    exclusions.append(ignored)
                    continue
                record(info, child)
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
                            directory(child_fd, child, scopes)
                        else:
                            if child in rules:
                                file_hash = rules[child]['sha256']
                                source_lines = rules[child]['source_lines']
                            elif files is None:
                                sha = hashlib.sha256()
                                lines = PhysicalLines()
                                while block := os.read(child_fd, 1024 * 1024):
                                    sha.update(block)
                                    lines.update(block)
                                file_hash = sha.hexdigest()
                                source_lines = lines.count
                            else:
                                if child not in files or info.st_size != files[child]['size']:
                                    changed(child)
                                if '_stamp' in files[child] and stamp(info) != files[child]['_stamp']:
                                    changed(child)
                                file_hash = files[child]['sha256']
                                source_lines = files[child]['source_lines']
                            entry.update(type='file', size=info.st_size, sha256=file_hash, source_lines=source_lines)
                            entries.append(entry)
                        unchanged(info, os.fstat(child_fd), child)
                    finally:
                        os.close(child_fd)
                else:
                    raise AuditError(f'Unsupported special file in source folder: {child}')
                unchanged(info, os.stat(name, dir_fd=fd, follow_symlinks=False), child)
            rule_path = '.gitignore' if relative == '.' else relative + '/.gitignore'
            if rule_path in rules and stamp(os.stat('.gitignore', dir_fd=fd, follow_symlinks=False)) != metadata[rule_path]:
                changed(rule_path)
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
            if expected is not None:
                changed(str(self.path))
            raise AuditError(f'Cannot read source folder {self.path}: {exc}') from exc
        if expected is not None and metadata.keys() != expected.keys():
            changed(next(iter(metadata.keys() ^ expected.keys())))
        if files is not None:
            actual_files = {e['path'] for e in entries if e['type'] == 'file'}
            if actual_files != files.keys():
                changed(next(iter(actual_files ^ files.keys())))
        return entries, metadata, rules, exclusions

    def snapshot(self, *, exclude_git=False, files=None) -> dict:
        """Pin one inventory; prepared copies reuse hashes from their initial reads."""
        entries, metadata, rules, exclusions = self._scan(exclude_git=exclude_git, files=files)
        rule_provenance = [{'path': path, 'sha256': rule['sha256']} for path, rule in sorted(rules.items())]
        filtered = self.source_filter['follow_gitignore'] or self.source_filter['exclude_paths']
        basis = {'entries': entries, 'settings': self.source_filter, 'rule_files': rule_provenance} if filtered else entries
        fingerprint = digest(json.dumps(basis, sort_keys=True, separators=(',', ':')).encode())
        self.inventory = {'source_directory': str(self.path), 'source_fingerprint': fingerprint,
                          'algorithm': 'sha256', 'entries': entries,
                          'source_filter': dict(self.source_filter, exclude_paths=list(self.source_filter['exclude_paths']),
                                                rule_files=rule_provenance, exclusions=exclusions)}
        self.metadata, self.exclude_git = metadata, exclude_git
        self.ignore_rules = rules
        self.files = {e['path']: e for e in entries if e['type'] == 'file'}
        return self.inventory

    def assert_snapshot(self, fingerprint: str) -> None:
        if self.inventory is None:
            raise AuditError('Source folder has no pinned snapshot.', failure_layer='integrity')
        if self.inventory['source_fingerprint'] != fingerprint:
            raise AuditError('Source folder changed from the pinned snapshot.', failure_layer='integrity')
        if self.source_filter != {key: self.inventory['source_filter'][key] for key in self.source_filter}:
            raise AuditError('Source filter changed during the run.', code='SOURCE_CHANGED', failure_layer='integrity')
        entries, _, rules, _ = self._scan(exclude_git=self.exclude_git, files=self.files, expected=self.metadata)
        if entries != self.inventory['entries'] or rules.keys() != self.ignore_rules.keys():
            raise AuditError('Source folder changed during the run; files will not be restored.',
                             code='SOURCE_CHANGED', failure_layer='integrity')

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
        'mode', 'git_mode', 'folder_mode', 'execution', 'result_policy', 'source_decoding', 'multi_session',
        'max_source_bytes_per_session', 'source_filter'}
    if set(value) - allowed:
        raise AuditError(f'Unknown configuration keys: {set(value) - allowed}')
    try:
        if 'execution' in value and value['execution'] is None:
            raise ValueError('execution must be a JSON object.')
        value['execution'] = execution_settings(value.get('execution'))
        value['source_decoding'] = normalize_source_decoding(value.get('source_decoding'))
        value['source_filter'] = normalize_source_filter(value.get('source_filter', {}))
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
        'continue_on_error': True, 'stage_agents': {}, 'prompts': {}, 'result_policy': 'compromise', 'multi_session': True,
        'max_source_bytes_per_session': DEFAULT_MAX_SOURCE_BYTES_PER_SESSION}
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
    if type(value['multi_session']) is not bool:
        raise AuditError('multi_session must be boolean.')
    if type(value['max_source_bytes_per_session']) is not int or value['max_source_bytes_per_session'] <= 0:
        raise AuditError('max_source_bytes_per_session must be a positive integer.', code='INVALID_CONFIG')
    if value['result_policy'] not in ('compromise', 'strict'):
        raise AuditError('result_policy must be compromise or strict.')
    if not isinstance(value['priority_scenarios'], list) or any(not isinstance(s, str) for s in value['priority_scenarios']):
        raise AuditError('priority_scenarios must be an array of strings.')
    stages = STAGES if mode == 'git' and len(source['branches']) > 1 else SOURCE_STAGES
    if not value['execution']['review_enabled']:
        stages = tuple(stage for stage in stages if stage != 'review')
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
    for stage in (*stages, *(('revise',) if value['execution']['review_enabled'] and value['execution']['max_revision_rounds'] else ())):
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
        self.metrics = RunMetrics(self.reporter.clock)
        self.metrics.started = getattr(self.reporter, 'started', self.metrics.started)
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
        self.folder = Folder(self.source_path, source_filter=config.get('source_filter')) if self.mode == 'folder' else None
        self.repo = Repository(self.source_path, trust_repository=trust_repository,
                               reporter=self.reporter) if self.mode == 'git' else None
        self.schemas = MODEL_FOLDER_SCHEMAS if self.mode == 'folder' else MODEL_SCHEMAS
        self.bindings = BindingRegistry()
        self.analysis_inventories = {}
        self.versions: dict[str, str] = {}

    def assert_source(self):
        if getattr(self, 'git_sources', None) is not None:
            self.git_sources.assert_intact(getattr(self, 'active_snapshot', None))
        elif self.repo:
            self.repo.assert_expected()
            if getattr(self, '_source_folder', None) is not None:
                self._source_folder.assert_snapshot(self._source_folder.inventory['source_fingerprint'])

    def source_inventory(self):
        if getattr(self, 'active_snapshot', None) is not None:
            folder = self.git_sources.guards[self.active_snapshot['path']]
        elif self.folder:
            folder = self.folder
        else:
            if getattr(self, '_source_folder', None) is None:
                settings = dict(self.cfg.get('source_filter', {}), follow_gitignore=False)
                self._source_folder = Folder(self.source_path, canonical=True, source_filter=settings)
            folder = self._source_folder
        if folder.inventory is None:
            folder.snapshot(exclude_git=self.repo is not None)
        folder.assert_snapshot(folder.inventory['source_fingerprint'])
        return folder.inventory, folder.metadata

    def record_error(self, manifest, exc, *, phase='run', **context):
        self.reporter.stop_progress()
        recoverable = isinstance(exc, ContractError) or (isinstance(exc, AuditError)
            and exc.code == 'CLI_FAILED' and exc.failure_layer == 'backend')
        if (not recoverable or phase in ('preflight', 'cleanup')
                or getattr(exc, 'failure_layer', None) == 'cleanup' or getattr(exc, 'cleanup_failed', False)):
            self.critical_failure = True
        if isinstance(exc, KeyboardInterrupt):
            self.reporter.stop_requested()
        if not any(exc is seen for seen in self.reported_errors):
            self.reported_errors.append(exc)
            if phase in ('run', 'stage') and self.active_stage:
                phase = 'stage'
                context = self.active_stage | context
            if phase == 'stage' and context.get('stage'):
                context['metrics'] = self.metrics.finish_stage(context, 'FAILED')
            detail = self.reporter.error(exc, phase=phase, analysis_started=self.analysis_started,
                switches_performed=bool(getattr(self.repo, 'journal', [])), **context)
            manifest.setdefault('diagnostics', []).append(detail | {'phase': phase} | context)

    def stage_context(self, stage, context):
        return {'branch': context.get('branch', 'all branches' if stage == 'compare' else 'folder'),
                **({'source_name': self.source_path.name or str(self.source_path)} if self.mode == 'folder' else {}),
                'commit': context.get('source_commit'),
                'stage': context.get('prompt_variant', stage) if stage == 'study' else stage,
                'revision_id': context.get('revision_id'),
                **({'shard_id': context['analysis_shard']['id']} if 'analysis_shard' in context else {}),
                **({'generated_by': context['generated_by']} if context.get('generated_by') else {}),
                'backend': None if context.get('generated_by') == 'orchestrator' else
                    self.cfg['_agents'].get('study' if stage == 'study-shard' else stage, {}).get('backend')}

    def stage_started(self, stage, context):
        started = self.reporter.clock()
        self.active_stage = self.stage_context(stage, context)
        self.metrics.start(self.active_stage)
        self.reporter.emit('stage_started', _started=started, **self.active_stage)
        return started

    def stage_finished(self, stage, context, data, started, *, report_path=None):
        status = data['completion_status']
        if stage == 'review' and data['verdict'] != 'PASS':
            status = 'PARTIAL'
        if stage == 'compare' and status == 'COMPLETE' and not data.get('program_checks', {}).get('policy_satisfied'):
            status = 'PARTIAL'
        self.reporter.emit('stage_completed', **self.stage_context(stage, context), status=status,
                           elapsed_seconds=self.reporter.clock() - started,
                           metrics=self.metrics.finish_stage(self.stage_context(stage, context), status),
                           **({'report_path': report_path} if existing_file(report_path) else {}))
        self.active_stage = {}

    def record_attempt_metrics(self, meta, context, started):
        if 'metrics' not in meta:
            measured = usage() if meta.get('prompt_sent') else usage({key: 0 for key in FIELDS}, source='not_sent')
            meta['metrics'] = measurement(meta['backend'], meta['model_requested'], meta['model_actual'], measured)
        meta['metrics'].update(duration_seconds=round(self.reporter.clock() - started, 3), attempts=1)
        models = list(dict.fromkeys(entry['model_actual'] for entry in meta['metrics']['by_model']
                                    if entry.get('model_actual')))
        if models:
            meta['models_reported'] = models
            meta['model_actual_source'] = 'backend usage metadata; may include multiple models'
            stage_models = list(dict.fromkeys(entry['model_actual'] for entry in meta['metrics']['by_model']
                if entry.get('model_actual') and entry.get('origin') != 'compaction'))
            if len(stage_models) == 1:
                meta['model_actual'] = stage_models[0]
        self.metrics.record(self.stage_context(meta['stage'], context), meta)

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
                adapter = CLI_ADAPTERS[backend]
                cmds = [[agent['executable'], '--version'],
                        adapter.help_command(agent['executable'])]
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
                required = adapter.required_flags(self.mode)
                if any(flag not in texts[1] for flag in required):
                    raise AuditError(f'{backend} lacks required CLI options: {required}',
                                     code='BACKEND_INCOMPATIBLE', failure_kind='BACKEND_INCOMPATIBLE',
                                     failure_layer='compatibility')
                version = texts[0].strip()
                if agent.get('expected_version') and agent['expected_version'] != version:
                    raise AuditError(f'{backend} version differs from expected_version.', code='CLI_VERSION_MISMATCH')
                if backend == 'opencode':
                    opencode_cli.verify_version(version)
                result[key] = {'version': version, 'required_flags': required}
                if backend == 'xxx':
                    for arguments, marker in ((['export', '--help'], 'sessionID'),
                                              (['session', 'delete', '--help'], 'sessionID')):
                        checked = process([agent['executable'], *arguments], state, env,
                                          reporter=self.reporter, budget=Budget(30))
                        if checked['returncode'] or marker not in (checked['stdout'] + checked['stderr']).decode(errors='replace'):
                            raise response_error('BACKEND_INCOMPATIBLE', 'compatibility',
                                                 'XXX requires export and session delete commands.')
                    result[key]['compatibility_profile'] = xxx.PROFILE
                self.reporter.emit('preflight_completed', check='cli', backend=backend, required_flags=required)
        self.versions = {k: v['version'] for k, v in result.items()}
        return result

    def command(self, stage: str, state: Path, agent: dict, schema_path: Path, env: dict, *, reports_only=False, schema=None) -> list[str]:
        adapter = CLI_ADAPTERS.get(agent['backend'])
        if adapter:
            access_stage = 'compare' if reports_only else stage
            kwargs = {'excluded_root': self.repo.path} if adapter is claude_code and getattr(self, 'git_sources', None) else {}
            if adapter is opencode_cli:
                kwargs['agent_name'] = opencode_cli.prepare_environment(env, access_stage)
            elif adapter is xxx:
                kwargs['agent_name'] = xxx.prepare_environment(
                    env, access_stage, source_snapshot=bool(getattr(self, 'git_sources', None)))
            return adapter.build_command(agent, access_stage, self.mode, schema or self.schemas[stage], schema_path, **kwargs)
        raise AuditError('No CLI adapter for this backend.',
                         code='BACKEND_INCOMPATIBLE')

    def invoke(self, stage: str, context: dict, destination: Path) -> tuple[dict | None, dict]:
        context = dict(context)
        context['stage'] = stage
        self.assert_coverage_file(context)
        self.assert_analysis_file(context)
        if stage != 'compare':
            context['sources'] = source_catalog(context)
        budget = Budget(self.execution['stage_timeout_seconds'], self.execution['idle_timeout_seconds'])
        evidence_pins = None
        evidence_metadata = None
        if stage != 'compare':
            # Every stage uses the initial inventory and verifies its metadata.
            if self.repo:
                self.assert_source()
            inventory, evidence_metadata = self.source_inventory()
            if context.get('_inventory') and inventory['source_fingerprint'] != context['_inventory']['source_fingerprint']:
                raise UnsafeRepository('Source changed from the inventory used for the coverage plan.', failure_layer='integrity')
            if stage == 'catalog':
                context['_inventory'] = inventory
                context['inventory_summary'] = inventory_summary(inventory)
            if self.folder and inventory['source_fingerprint'] != context['source_fingerprint']:
                raise UnsafeRepository('Source folder changed from the pinned snapshot before invocation.')
            if self.repo:
                self.assert_source()
            evidence_pins = {e['path']: e['sha256'] for e in inventory['entries'] if e['type'] == 'file'}
        binding = self.bindings.bind(stage, context, self.mode)
        binding_hashes = {}
        attempt_hashes = {}
        candidate = None
        candidate_attempt = None
        try:
            return self._invoke_once(stage, context, destination, budget=budget,
                                     evidence_pins=evidence_pins, evidence_metadata=evidence_metadata, binding=binding,
                                     binding_hashes=binding_hashes, attempt_hashes=attempt_hashes)
        except ContractError as exc:
            self.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
            if not (destination / 'invocation.json').is_file():
                raise
            meta = strict_json((destination / 'invocation.json').read_text())
            if meta.get('cleanup_errors'):
                self.critical_failure = True
                raise
            attempt = Path(meta['artifact_directory'])
            # Retain only the verified original response under the compromise policy.
            if (self.compromise and stage != 'study-shard' and context.get('prompt_variant') != 'synthesis'
                    and meta.get('source_integrity_verified')
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

            if candidate is None:
                raise
            # Recheck the source before retaining contract-invalid material.
            if self.folder:
                self.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.assert_source()
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

    def validate_attempt(self, stage, data, context, attempt, meta, binding):
        candidate = data
        validation = {'valid': False, 'validated_object': 'extracted.json'}
        try:
            self.pin_attempt_value(attempt, 'extracted.json', data, meta)
            self.save_binding(binding, data, None, attempt, meta)
            binding.validate_identity(data)
            meta['model_identity_verified'] = True
            candidate = binding.expand(data, allow_invalid=True)
            self.save_attempt_value(attempt, 'expanded.json', candidate, meta)
            self.save_binding(binding, data, candidate, attempt, meta)
            validation['validated_object'] = 'expanded.json'
            validate_schema(data, binding.schema)
            expanded = candidate
            if stage == 'catalog' and self.compromise:
                candidate, recovery = recover_catalog_paths(expanded, context['_inventory'], context)
                if recovery:
                    self.save_attempt_value(attempt, 'recovered.json', candidate, meta)
                    self.save_attempt_value(attempt, 'recovery.json', recovery, meta)
                    meta['catalog_recovery'] = {k: v for k, v in recovery.items() if k != 'rejected_selectors'}
                    validation.update(validated_object='recovered.json', original_valid=False,
                                      catalog_recovery=meta['catalog_recovery'])
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
            validation['valid'] = True
        except BaseException as exc:
            validation.update(result_diagnostics(stage, candidate, context, self.mode))
            validation['schema_diagnostics'] = schema_diagnostics(data, binding.schema, private=True)
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
        if meta.get('catalog_recovery') and not self.cfg['continue_on_error']:
            raise ContractError('Stopped after recovering an invalid catalog (continue_on_error=false).')

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

    def assert_analysis_file(self, context):
        if not context.get('_analysis_plan_path'):
            return
        try:
            inventory = context.get('_inventory') or self.analysis_inventories[context['analysis_plan']['inventory_sha256']]
            plan = verify_analysis_plan(context['analysis_plan'], inventory, context['coverage_plan'],
                max_source_bytes_per_session=self.cfg.get('max_source_bytes_per_session', DEFAULT_MAX_SOURCE_BYTES_PER_SESSION))
            expected = (json.dumps(plan, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            relative = str(Path(context['_analysis_plan_path']).relative_to(self.run_dir))
            if read_confined(self.run_dir, relative, len(expected)) != expected:
                raise ValueError('changed')
        except (OSError, ValueError, SourceChanged) as exc:
            self.critical_failure = True
            raise AuditError('Frozen analysis plan changed.', code='ANALYSIS_PLAN_CHANGED', failure_layer='integrity') from exc
        for relative, pin in context.get('_shard_artifact_hashes', {}).items():
            try:
                content = read_confined(self.run_dir, relative, pin['bytes'])
                if len(content) != pin['bytes'] or digest(content) != pin['sha256']:
                    raise ValueError('changed')
            except (OSError, ValueError, SourceChanged) as exc:
                self.critical_failure = True
                raise AuditError('Validated shard artifact changed.', code='STUDY_SHARD_CHANGED', failure_layer='integrity') from exc

    def complete_empty_shard(self, context, destination, guard):
        meta = {'stage': 'study-shard', 'generated_by': 'orchestrator', 'reason': 'empty_subsystem_assignment',
                'started_at': now(), 'status': 'RUNNING',
                'metrics': {'attempts': 0, 'by_model': [],
                            'usage': usage({key: 0 for key in FIELDS}, source='not_sent')}}
        private_directory(destination)
        try:
            data = empty_shard_result(context['analysis_shard'], context)
            validate_result('study-shard', data, context, self.mode)
            meta['local_validation'] = True
            data = prepare_result('study-shard', data, context)
            require_shard_policy(data)
            guard(context)
            meta['source_integrity_verified'] = True
            meta['source_check_status'] = 'MATCHED_AT_BOUNDARIES'
            meta['report_sha256'] = self.publish_result('study-shard', data, destination)
            meta['artifact_hashes'] = {name: {'sha256': digest(blob), 'bytes': len(blob)}
                                      for name, blob in self.artifact_contents('study-shard', data).items()}
            meta.update(status='SUCCEEDED', finished_at=now(), publication_complete=True)
            save_json(destination / 'invocation.json', meta)
            return data, meta
        except BaseException as exc:
            meta.update(status='FAILED', error=asdict(diagnostic(exc)), finished_at=now(), publication_complete=False)
            with contextlib.suppress(OSError):
                save_json(destination / 'invocation.json', meta)
            raise

    def run_study_shards(self, item, context, directory, run_stage, run_empty_shard, persist):
        plan = item['analysis_plan']
        item['study_shards'] = [dict(id=s['id'], status='PLANNED', errors=[],
            directory=str((directory / 'study-shards' / s['id']).relative_to(self.run_dir))) for s in plan['shards']]
        persist()
        self.assert_coverage_file(context)
        self.assert_analysis_file(context)
        for shard, state in zip(plan['shards'], item['study_shards']):
            state['status'] = 'RUNNING'
            persist()
            try:
                shard_context = context | {'analysis_shard': shard}
                if not shard['subsystem_ids']:
                    data = run_empty_shard(state, shard_context, self.run_dir / state['directory'])
                else:
                    data = run_stage('study-shard', state, shard_context, self.run_dir / state['directory'])
                state['status'] = 'SUCCEEDED' if data is not None and data['program_checks']['policy_satisfied'] else 'FAILED'
            finally:
                if state['status'] == 'RUNNING':
                    state['status'] = 'FAILED'
                persist()
        if any(s['status'] != 'SUCCEEDED' for s in item['study_shards']):
            item['errors'].append('Study synthesis skipped: not all planned shards succeeded.')
            item['synthesis_status'] = 'SKIPPED'
            return None
        self.assert_revision_files(item)
        return synthesis_inputs(plan, item['study_shards'])

    def select_source_revision(self, item, directory, *, publish):
        self.assert_revision_files(item)
        selected = choose_revision(item.get('revisions', []))
        item['selected_revision'] = selected['revision_id'] if selected else None
        item['revision_history'] = [{
            'revision_id': revision['revision_id'], 'directory': revision['directory'],
            'study_status': (stage_document(revision, 'study') or {}).get('completion_status'),
            'review_status': 'SKIPPED' if revision.get('review_skipped') == 'disabled_by_config'
                             else (revision.get('review') or {}).get('completion_status'),
            'review_enabled': revision.get('review_enabled', True),
            'review_skipped': revision.get('review_skipped'),
            'workflow_satisfied': workflow_satisfied(revision),
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
        item['workflow_satisfied'] = workflow_satisfied(item)

    def assert_revision_files(self, item):
        states = list(item.get('revisions', []))
        states.extend(item.get('study_shards', []))
        directory = item.get('directory', '.')
        if item.get('coverage_plan'):
            self.assert_coverage_file({'coverage_plan': item['coverage_plan'],
                                       '_coverage_plan_path': str(self.run_dir / directory / 'coverage.plan.json')})
        if item.get('analysis_plan'):
            self.assert_analysis_file({'analysis_plan': item['analysis_plan'], 'coverage_plan': item['coverage_plan'],
                '_analysis_plan_path': str(self.run_dir / directory / 'analysis.plan.json')})
        states.append({'directory': directory, 'catalog_invocation': item.get('catalog_invocation')})
        if item.get('selection_publication_complete'):
            states.append(item | {'directory': directory, 'selected_aliases': True})
        for revision in states:
            for stage in ('catalog', 'study', 'study-shard', 'review'):
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
        review_enabled = self.execution['review_enabled']
        item['review_enabled'] = review_enabled
        item['workflow_satisfied'] = False
        if not review_enabled:
            item['review_skipped'] = 'disabled_by_config'
        context = dict(context, review_enabled=review_enabled, source_decoding=self.cfg.get('source_decoding', {'rules': []}),
                       source_decoding_access='Decoding rules apply to program evidence checks only; agent reading depends on its CLI.')
        item['revisions'] = []

        def guard(stage_context):
            if self.folder:
                self.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.assert_source()
            self.assert_coverage_file(stage_context)
            self.assert_analysis_file(stage_context)
            self.assert_revision_files(item)

        def execute_stage(stage, state, stage_context, stage_dir, *, local=False):
            started = self.stage_started(stage, stage_context)
            destination = stage_dir / (stage + '.logs')
            try:
                guard(stage_context)
                data, meta = (self.complete_empty_shard(stage_context, destination, guard) if local else
                              self.invoke(stage, stage_context, destination))
                self.store_stage(state, stage, data, meta, stage_context)
                if data is not None:
                    self.stage_finished(stage, stage_context, data, started,
                        report_path=stage_dir / ARTIFACTS[stage] if meta.get('publication_complete') and stage in ARTIFACTS else None)
                else:
                    self.reporter.emit('stage_completed', **self.stage_context(stage, stage_context),
                                       status='PARTIAL', elapsed_seconds=self.reporter.clock() - started,
                                       metrics=self.metrics.finish_stage(self.stage_context(stage, stage_context), 'PARTIAL'))
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

        def run_stage(stage, state, stage_context, stage_dir):
            return execute_stage(stage, state, stage_context, stage_dir)

        def run_empty_shard(state, stage_context, stage_dir):
            return execute_stage('study-shard', state, stage_context | {'generated_by': 'orchestrator'},
                                 stage_dir, local=True)

        try:
            guard(context)
            inventory, _ = self.source_inventory()
            guard(context)
            save_json(directory / 'source.inventory.json', inventory)
            context['_inventory'] = inventory
            context['inventory_summary'] = inventory_summary(inventory)
            catalog = run_stage('catalog', item, context, directory)
            if catalog is None:
                error = (item.get('catalog_invocation') or {}).get('error', {})
                if not self.compromise or error.get('failure_kind') == 'IDENTITY_MISMATCH':
                    return
                plan = build_coverage_plan(None, inventory, context, fallback=True)
                self.publish_coverage_plan(plan, directory)
            else:
                plan = catalog['coverage_plan']
            item['coverage_plan'] = plan
            context.update(coverage_plan=plan, _coverage_plan_path=str(directory / 'coverage.plan.json'))
            context.pop('inventory_summary', None)
            byte_limit = self.cfg.get('max_source_bytes_per_session', DEFAULT_MAX_SOURCE_BYTES_PER_SESSION)
            analysis = build_analysis_plan(inventory, plan, self.cfg.get('multi_session', True),
                                           max_source_bytes_per_session=byte_limit)
            verify_analysis_plan(analysis, inventory, plan, max_source_bytes_per_session=byte_limit)
            self.analysis_inventories[analysis['inventory_sha256']] = inventory
            context.update(analysis_plan=analysis, _analysis_plan_path=str(directory / 'analysis.plan.json'))
            path = Path(context['_analysis_plan_path'])
            if path.exists() or path.is_symlink():
                self.assert_analysis_file(context)
            else:
                save_json(path, analysis)
            item.update(analysis_plan=analysis, analysis_plan_path=str(path.relative_to(self.run_dir)),
                        multi_session=analysis['multi_session'], required_sessions=analysis['required_sessions'],
                        source_metrics=analysis['totals'], study_shards=[],
                        study_origin='multi_session_synthesis' if analysis['required_sessions'] > 1 else 'direct_single_session')
            synthesis = None
            if analysis['required_sessions'] > 1:
                synthesis = self.run_study_shards(item, context, directory, run_stage, run_empty_shard, persist)
                if synthesis is None:
                    return
            previous = None
            revision_rounds = self.execution['max_revision_rounds'] if review_enabled else 0
            for number in range(1, revision_rounds + 2):
                revision_id = f'{number:03d}'
                revision_dir = directory / 'revisions' / revision_id
                revision = {key: item[key] for key in ('branch', 'source_commit', 'source_directory', 'source_fingerprint', 'source_snapshot') if key in item}
                revision.update(revision_id=revision_id, directory=str(revision_dir.relative_to(self.run_dir)),
                                study=None, review=None, errors=[], review_enabled=review_enabled, workflow_satisfied=False)
                if not review_enabled:
                    revision['review_skipped'] = 'disabled_by_config'
                item['revisions'].append(revision)
                current = context | {'revision_id': revision_id}
                if previous:
                    current.update(revision_inputs(previous))
                    current['prompt_variant'] = 'revise'
                elif synthesis is not None:
                    current.update(synthesis, catalog=catalog, prompt_variant='synthesis',
                                   source_access='Validated structured inputs only; all source-inspection tools are disabled.')
                try:
                    if current.get('prompt_variant') == 'synthesis':
                        item['synthesis_status'] = 'RUNNING'
                        persist()
                    run_stage('study', revision, current, revision_dir)
                finally:
                    if current.get('prompt_variant') == 'synthesis':
                        item['synthesis_invocation'] = revision.get('study_invocation')
                        item['synthesis_status'] = 'SUCCEEDED' if revision.get('study') is not None else 'FAILED'
                        current = context | {'revision_id': revision_id}
                if not review_enabled:
                    skip_context = self.stage_context('review', current)
                    self.reporter.emit('stage_skipped', **skip_context, reason='disabled_by_config',
                                       metrics=self.metrics.finish_stage(skip_context, 'SKIPPED'))
                elif usable_study(revision) and (previous is None or revision.get('study') is not None):
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
                    self.metrics.finish_stage(self.stage_context('review', current), 'SKIPPED')
                    self.reporter.emit('stage_skipped', **self.stage_context('review', current))
                    if self.repo and not usable_study(revision):
                        item['errors'].append('Review skipped: no usable architecture document.')
                revision['accepted'] = accepted(revision)
                revision['workflow_satisfied'] = workflow_satisfied(revision)
                persist()
                if not (review_enabled and completed_pair(revision) and revision.get('study') and
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
        return digest(contents[stage + '.json' if stage in ('catalog', 'study-shard') else ARTIFACTS[stage]])

    def artifact_contents(self, stage, data):
        def encoded(value):
            return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        if stage == 'study-shard':
            return {'study-shard.json': encoded(data)}
        if stage == 'catalog':
            verify_coverage_plan(data['coverage_plan'])
            return {'coverage.plan.json': encoded(data['coverage_plan']), 'catalog.json': encoded(data),
                    ARTIFACTS[stage]: render_stage(stage, data, self.cfg.get('output_language')).encode('utf-8')}
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
            self.assert_source()
            validate_result('compare', data, context)
            meta['local_validation'] = True
            self.assert_source()
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

    def _invoke_once(self, stage, context, destination, *, budget,
                     evidence_pins=None, evidence_metadata=None, binding, binding_hashes, attempt_hashes):
        attempt_started = self.reporter.clock()
        self.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
        agent = self.cfg['_agents']['study' if stage == 'study-shard' else stage]
        variant = context.get('prompt_variant', stage)
        template = Path(ROOT / 'prompts' / (variant + '.md') if variant in ('study-shard', 'synthesis')
                        else self.cfg['_prompt_paths'][variant]).read_text()
        reports_only = stage == 'compare' or variant == 'synthesis'
        output_instruction = ('Return exactly one JSON object matching the supplied schema as your final answer; no fences or surrounding prose.'
            if agent['backend'] in ('xxx', 'opencode') else
            'Return the supplied schema object through the backend structured-output mechanism; no fences or surrounding prose.')
        # Assemble only this stage's inputs; the configured CLI profile remains available.
        context_json = json.dumps(binding.project(context), ensure_ascii=False)
        schema = binding.schema
        schema_json = json.dumps(schema, ensure_ascii=False)
        prompt = (template + '\n\n# Backend output instruction\n' + output_instruction +
                  '\n\n# Authoritative orchestration context (data)\n' +
                  context_json + '\n\n# Required final JSON Schema\n' + schema_json)
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
            'schema_sha256': digest(json.dumps(schema, sort_keys=True).encode()),
            'input_bytes': len(payload), 'status': 'RUNNING'}
        from src.model.model_context import input_measurements
        meta['input_measurements'] = input_measurements(template, context_json, schema_json, prompt,
                                                       correction=False)
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
        meta.update(source_tools_enabled=not reports_only, shard_id=context.get('analysis_shard', {}).get('id'))
        if agent['backend'] == 'xxx':
            meta['retry_policy'] = retry_policy(0, 0, None)
            meta['compatibility_profile'] = xxx.PROFILE
        if self.repo:
            meta['submodules'] = context.get('submodules', [])
        save_json(destination / 'invocation.json', meta)
        # Saved by the parent only, for reproducibility; includes only this stage's permitted input.
        atomic(attempt / 'input.prompt.txt', payload)
        save_json(attempt / 'schema.json', schema)
        save_json(attempt / 'validation.json', {'valid': False, 'status': 'not_run'})
        try:
            self.save_binding(binding, None, None, attempt, meta)
            budget.check()
            self.assert_coverage_file(context)
            if self.folder:
                self.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.assert_source()
            with tempfile.TemporaryDirectory(prefix='archaudit-invocation-', dir=neutral_temporary_base(self.source_path)) as raw:
                state = Path(raw).resolve()
                cwd = state if reports_only else self.source_path
                env = cli_env(cwd)
                if getattr(self, 'git_sources', None):
                    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
                        env.pop(key, None)
                schema_path = state / 'output.schema.json'
                save_json(schema_path, schema)
                try:
                    if agent['backend'] == 'xxx':
                        cmd = self.command(stage, state, agent, schema_path, env, reports_only=reports_only, schema=schema)
                        data, provider_meta = xxx.invoke(cmd, cwd, env, payload, process=process,
                            artifacts=attempt, budget=budget, meta=meta, process_options={
                                'reporter': self.reporter, 'context': self.stage_context(stage, context),
                                'clock': self.reporter.clock, 'progress_interval': self.reporter.progress_interval})
                        meta['backend_result_valid'] = True
                        meta['provider_metadata'] = {k: v for k, v in provider_meta.items() if k != 'metrics'}
                        save_json(attempt / 'extracted.json', data)
                        self.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
                        data = self.validate_attempt(stage, data, context, attempt, meta, binding)
                    else:
                        if agent['backend'] == 'opencode':
                            opencode_cli.verify_version(meta['cli_version'])
                        cmd = self.command(stage, state, agent, schema_path, env, reports_only=reports_only, schema=schema)
                        r = None
                        meta['prompt_sent'] = True
                        try:
                            r = process(cmd, cwd, env, payload, reporter=self.reporter,
                                        context=self.stage_context(stage, context), log_dir=attempt,
                                        clock=self.reporter.clock, progress_interval=self.reporter.progress_interval,
                                        budget=budget)
                        finally:
                            # The private pipe log survives timeout/interruption even without a process result.
                            with contextlib.suppress(OSError):
                                output = r['stdout'] if r is not None else (attempt / 'stdout.log').read_bytes()
                                meta['metrics'] = CLI_ADAPTERS[agent['backend']].collect_metrics(
                                    output.decode('utf-8', errors='replace'), agent.get('model'))
                        meta.update(returncode=r['returncode'], output_bytes=len(r['stdout']))
                        if r['returncode']:
                            raise AuditError(f'{agent["backend"]} exited with {r["returncode"]}; inspect private attempt logs.',
                                code='CLI_FAILED', failure_kind='BACKEND_ERROR', failure_layer='backend')
                        output = (codex.read_response(schema_path)
                                  if agent['backend'] == 'codex' else r['stdout'].decode('utf-8'))
                        try:
                            data, provider_meta = CLI_ADAPTERS[agent['backend']].parse_output(output)
                        except ContractError as exc:
                            if (agent['backend'] != 'opencode' or exc.failure_kind != 'INCOMPLETE_OUTPUT'
                                    or not exc.details.get('session_id')):
                                raise
                            # V2 can exit before emitting step_finish; export confirms the same answer.
                            export_dir = attempt / 'session-export'
                            private_directory(export_dir)
                            exported = process([agent['executable'], 'session', 'export', '--standalone',
                                                exc.details['session_id']], cwd, env, reporter=self.reporter,
                                               context=self.stage_context(stage, context), log_dir=export_dir,
                                               clock=self.reporter.clock, budget=budget)
                            if exported['returncode']:
                                raise response_error('INCOMPLETE_OUTPUT', 'result',
                                                     'OpenCode could not export the final session for verification.') from exc
                            data, provider_meta = opencode_cli.parse_output(
                                output, exported=exported['stdout'].decode('utf-8'),
                                agent_name=cmd[cmd.index('--agent') + 1])
                            meta['metrics'] = opencode_cli.collect_metrics(
                                output, agent.get('model'), completed=provider_meta['exported_finish'])
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
                    self.assert_analysis_file(context)
                    if self.folder:
                        self.folder.assert_snapshot(context['source_fingerprint'])
                    else:
                        self.assert_source()
                    meta['source_integrity_verified'] = True
                budget.check()
            # Publish after the CLI exits and temporary invocation files are removed.
            try:
                data = prepare_result(stage, data, context, evidence_pins, expected_metadata=evidence_metadata,
                                      catalog_recovery=meta.get('catalog_recovery'))
                if stage == 'study-shard':
                    self.save_attempt_value(attempt, 'prepared.json', data, meta)
                    require_shard_policy(data)
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
                self.assert_source()
            meta['source_check_status'] = 'MATCHED_AT_BOUNDARIES'
            budget.check()
            if agent['backend'] == 'xxx' and meta.get('model_actual'):
                meta['model_actual_source'] = 'backend assistant-message metadata'
            if stage == 'review':
                self.assert_review_files(context, destination.parent)
            self.assert_coverage_file(context)
            self.assert_analysis_file(context)
            self.assert_binding_files(binding, context, destination.parent, binding_hashes | attempt_hashes)
            report_hash = self.publish_result(stage, data, destination)
            artifact_hashes = {name: {'sha256': digest(blob), 'bytes': len(blob)}
                               for name, blob in self.artifact_contents(stage, data).items()}
            meta.update(status='SUCCEEDED', finished_at=now(), report_sha256=report_hash, publication_complete=True)
            meta['artifact_hashes'] = artifact_hashes
            meta['duration_seconds'] = round(budget.clock() - budget.started, 3)
            self.record_attempt_metrics(meta, context, attempt_started)
            save_json(attempt / 'invocation.json', meta)
            save_json(destination / 'invocation.json', meta)
            return data, meta
        except BaseException as exc:
            if getattr(exc, 'cleanup_failed', False):
                self.critical_failure = True
                meta.setdefault('cleanup_errors', []).append('process_cleanup_failed')
            if meta.get('cleanup_errors'):
                self.critical_failure = True
            meta.update(status='FAILED', finished_at=now(), error=asdict(diagnostic(exc)),
                        publication_complete=False, duration_seconds=round(budget.clock() - budget.started, 3))
            self.record_attempt_metrics(meta, context, attempt_started)
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
                    self.record_error(self.manifest, exc, phase='cleanup')
        manifest['result_policy'] = 'compromise' if self.compromise else 'strict'
        manifest.update(contract_id=CONTRACT_ID, artifact_format=ARTIFACT_FORMAT,
            acceptance_meaning='accepted=true means study and review policy checks satisfied; factual correctness is not established.',
            review_quality='NOT_MEASURED', source_check_meaning='MATCHED_AT_BOUNDARIES means pinned source metadata matched at performed checks only.')
        manifest['critical_failure'] = self.critical_failure
        if not check_only:
            entries = report_entries(manifest, self.source, self.mode)
            if self.compromise and code != 130:
                if self.critical_failure or not any(usable_study(b) for b in entries):
                    manifest['status'], code = 'FAILED', 1
                elif (all(workflow_satisfied(b) for b in entries) and not manifest.get('diagnostics')
                      and (self.mode == 'folder' or len(self.source['branches']) == 1
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
        manifest['workflow_satisfied'] = not check_only and manifest['status'] == 'COMPLETE' and code == 0
        manifest['exit_code'] = code
        # This last manifest write commits the run's publication record. Individual
        # file replacements above are atomic; the group is not a transaction.
        manifest['publication_complete'] = code in (0, 2)
        if self.critical_failure and 'accepted' in manifest:
            manifest['accepted'] = False
        manifest['metrics'] = self.metrics.snapshot(finish=True)
        save_json(self.run_dir / 'manifest.json', manifest)
        return manifest, code

    def run_git(self, check_only: bool = False) -> tuple[dict, int]:
        manifest = {'run_id': self.run_dir.name, 'started_at': now(), 'repository': str(self.repo.path),
            'review_enabled': self.execution['review_enabled'],
            'git': self.repo.runtime.manifest(), 'baseline_branch': self.source['baseline_branch'],
            'status': 'RUNNING', 'isolation': 'independent-source-copies; cli-native-permissions',
            'platform': sys.platform, 'branches': [], 'errors': [], 'switch_journal': [],
            'source_preservation': 'Analyzer performs no writes to the original repository.',
            'publication_complete': False}
        self.manifest = manifest
        def persist():
            manifest['metrics'] = self.metrics.snapshot()
            save_json(self.run_dir / 'manifest.json', manifest)
        persist()
        original_path = self.source_path
        self.git_sources = None
        try:
            if not self.cfg['project_description']:
                self.reporter.emit('description_missing')
            pins = self.repo.preflight(self.source['branches'])
            manifest['pins'] = pins
            original_branch, original_commit = self.repo.symbolic(), self.repo.head()
            manifest['original_checkout'] = {'branch': original_branch, 'commit': original_commit}
            manifest['original_hierarchy'] = self.repo.original
            self.git_sources = GitSources(self.repo, Folder, UnsafeRepository, neutral_temporary_base(original_path),
                                          source_filter=self.cfg.get('source_filter'))
            working_label = original_branch if original_branch in pins else 'working tree (' + (original_branch or 'detached HEAD') + ')'
            working = self.git_sources.working(working_label)
            labels = list(self.source['branches'])
            if original_branch not in pins and working['has_local_changes']:
                labels.append(working_label)
            for branch in self.source['branches']:
                if branch != original_branch:
                    self.git_sources.commit(branch, pins[branch])
            manifest['snapshot_plans'] = {name: {
                'source_snapshot': item['source_snapshot'], 'submodules': item['submodules']}
                for name, item in self.git_sources.snapshots.items() if name in labels}
            manifest['working_tree'] = {key: working[key] for key in ('has_local_changes', 'untracked_files', 'git_state')}
            manifest['working_tree']['ignored_files'] = 'excluded using Git standard rules'
            self.reporter.emit('sources_prepared', working_changes=working['has_local_changes'],
                               untracked_files=working['untracked_files'], revisions=labels,
                               ignored_files='excluded', additional_revision=working_label if working_label not in pins and working['has_local_changes'] else None)
            self.repo.assert_expected()
            manifest['cli_checks'] = self.check_cli()
            self.git_sources.assert_intact()
            if check_only:
                manifest['status'], code = 'PREFLIGHT_OK', 0
            else:
                for branch in labels:
                    prepared = self.git_sources.snapshots[branch]
                    provenance = prepared['source_snapshot']
                    commit = provenance['base_commit']
                    self.source_path = prepared['path']
                    self.active_snapshot = prepared
                    self.analysis_started = True
                    self.active_stage = {}
                    self.reporter.emit('branch_started', branch=branch, commit=commit,
                                       source_type=provenance['source_type'], snapshot_id=provenance['snapshot_id'])
                    branch_dir = self.run_dir / 'branches' / slug(branch)
                    item = {'branch': branch, 'source_commit': commit, 'source_snapshot': provenance,
                        'additional_revision': branch not in pins, 'submodules': prepared['submodules'],
                        'directory': str(branch_dir.relative_to(self.run_dir)), 'study': None, 'review': None, 'errors': []}
                    manifest['branches'].append(item)
                    save_json(branch_dir / 'source.snapshot.json', {k: v for k, v in prepared.items() if k not in ('path', 'inventory')})
                    persist()
                    context = {'source_mode': 'git', 'branch': branch, 'source_commit': commit,
                        'source_snapshot': provenance, 'submodules': item['submodules'],
                        'repository': str(self.source_path), 'output_language': self.cfg['output_language'],
                        'project_description': self.cfg['project_description'],
                        'priority_scenarios': self.cfg['priority_scenarios'], 'execution_mode': 'static-only',
                        'source_access': 'Prepared independent copy only; no .git or ignored untracked contents. '
                            'Symlinks are metadata only. Use relative project paths. For working_tree, source_commit '
                            'is a base commit, not the identity of file bytes. Native CLI permissions are not full filesystem isolation.'}
                    self.run_source(item, context, branch_dir, persist)
                    item['accepted'] = accepted(item)
                    persist()
                entries = manifest['branches']
                quality_ok = all(workflow_satisfied(b) for b in entries)
                if len(self.source['branches']) > 1:
                    baseline = self.source['baseline_branch']
                    selected_entries = [b for b in entries if b['branch'] in self.source['branches']]
                    bundle = {'baseline_branch': baseline, 'baseline_commit': pins[baseline],
                        'review_enabled': self.execution['review_enabled'],
                        'result_policy': 'compromise' if self.compromise else 'strict',
                        'requested_branches': self.source['branches'], 'output_language': self.cfg['output_language'],
                        'project_description': self.cfg['project_description'],
                        'scope': 'reports-only comparison; source inspection is outside task scope',
                        'branches': [{k: b.get(k) for k in ('branch', 'source_commit', 'source_snapshot', 'submodules', 'study', 'review',
                                                           'study_material', 'review_material', 'errors', 'selected_revision', 'coverage_plan')} |
                                      {'accepted': accepted(b), 'workflow_satisfied': workflow_satisfied(b),
                                       'review_enabled': self.execution['review_enabled']} |
                                     {stage + '_invocation': {k: (b.get(stage + '_invocation') or {}).get(k) for k in
                                        ('publication_complete', 'contract_id', 'artifact_format', 'status', 'model_requested', 'model_actual',
                                         'model_actual_source', 'source_check_status', 'review_quality')}
                                      for stage in ('study', 'review')} for b in entries if b['branch'] in self.source['branches']],
                        'git_deltas': {b: self.git_sources.delta(baseline, b) for b in self.source['branches'] if b != baseline}}
                    bundle['required_unresolved_branches'] = required_unresolved(bundle)
                    comp_dir = self.run_dir / 'comparison'
                    save_json(comp_dir / 'inputs.json', bundle)
                    started = self.stage_started('compare', bundle)
                    try:
                        self.git_sources.assert_intact()
                        for entry in entries:
                            self.assert_revision_files(entry)
                        can_compare = (comparison_possible(selected_entries, baseline) if self.compromise
                                       else any(workflow_satisfied(b) for b in selected_entries))
                        if can_compare:
                            comparison, meta = self.invoke('compare', bundle, comp_dir / 'compare.logs')
                        else:
                            comparison, meta = self.publish_blocked_comparison(bundle, comp_dir / 'compare.logs')
                    finally:
                        try:
                            self.git_sources.assert_intact()
                            for entry in entries:
                                self.assert_revision_files(entry)
                        except AuditError as exc:
                            self.record_error(manifest, exc, phase='integrity')
                            raise
                    manifest['comparison'] = comparison
                    manifest['comparison_invocation'] = meta
                    self.stage_finished('compare', bundle, comparison, started,
                        report_path=comp_dir / ARTIFACTS['compare'] if meta.get('publication_complete') else None)
                    quality_ok = quality_ok and comparison['program_checks']['policy_satisfied'] and meta['publication_complete']
                failed = any(b['errors'] for b in entries)
                manifest['status'] = 'FAILED' if failed else 'COMPLETE' if quality_ok else 'PARTIAL'
                code = 1 if failed else 0 if quality_ok else 2
        except BaseException as exc:
            manifest['errors'].append(str(exc) or type(exc).__name__)
            manifest['status'] = 'FAILED'
            code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            self.record_error(manifest, exc, phase='run' if self.analysis_started else 'preflight')
        finally:
            self.source_path = original_path
            self.active_snapshot = None
            if self.git_sources is not None:
                try:
                    self.git_sources.close()
                    manifest['temporary_sources_removed'] = True
                except OSError as exc:
                    manifest['temporary_sources_removed'] = False
                    manifest['errors'].append('Temporary source cleanup failed.')
                    manifest['status'] = 'FAILED'
                    code = 130 if code == 130 else 1
                    self.record_error(manifest, exc, phase='cleanup')
        manifest.update(finished_at=now(), exit_code=code)
        persist()
        return manifest, code

    def run_folder(self, check_only: bool = False) -> tuple[dict, int]:
        manifest = {'mode': 'folder', 'run_id': self.run_dir.name,
            'review_enabled': self.execution['review_enabled'],
            'started_at': now(), 'source_directory': str(self.source_path), 'status': 'RUNNING',
            'isolation': 'cli-native-permissions', 'platform': sys.platform,
            'integrity': 'One initial content fingerprint; metadata checked at stage boundaries. '
                         'Not a backup or continuous immutability guarantee.',
            'study': None, 'review': None, 'accepted': False, 'errors': []}
        manifest['publication_complete'] = False
        self.manifest = manifest
        def persist():
            manifest['metrics'] = self.metrics.snapshot()
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
                                     'Native CLI permissions; pinned metadata is verified at stage boundaries only.'}
                self.run_source(manifest, context, self.run_dir, persist)
                manifest['accepted'] = accepted(manifest)
                manifest['workflow_satisfied'] = workflow_satisfied(manifest)
                manifest['status'] = 'FAILED' if manifest['errors'] else 'COMPLETE' if manifest['workflow_satisfied'] else 'PARTIAL'
                code = 1 if manifest['errors'] else 0 if manifest['workflow_satisfied'] else 2
        except BaseException as exc:
            manifest['errors'].append(str(exc) or type(exc).__name__)
            manifest['status'] = 'FAILED'
            code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            self.record_error(manifest, exc, phase='run' if self.analysis_started else 'preflight')
        manifest.update(finished_at=now(), exit_code=code)
        persist()
        return manifest, code

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=
        'In command-line mode, stdout contains one result. --help and argument syntax errors '
        'use standard argparse output without a run or manifest. Console messages are in English; '
        'output_language only controls generated reports. Labels and statuses are colored automatically '
        'when their output stream is a terminal, unless NO_COLOR is nonempty or TERM=dumb. '
        'JSON is never colored. Disable color with NO_COLOR=1.')
    parser.add_argument('--config', type=Path, help='Path to a JSON or JSONC configuration; required outside the TUI.')
    parser.add_argument('--tui', action='store_true', help='Open the interactive config picker and run dashboard (also the default without arguments).')
    parser.add_argument('--check', action='store_true', help='Check configuration, source state, and CLI capabilities; no branch switch or model calls.')
    parser.add_argument('--trust-repository', action='store_true', help='Trust only the configured Git checkout and verified submodules despite an ownership mismatch (Git >= 2.34.1).')
    parser.add_argument('--output', choices=('auto', 'text', 'json'), default='auto',
                        help='Result format on stdout: text is human-readable; auto selects text for a TTY, JSON otherwise (default: auto). Diagnostics use stderr.')
    parser.add_argument('--verbose', action='store_true', help='Add technical event details to stderr; never print prompts, credentials or raw model output.')
    parser.add_argument('--no-progress', action='store_true', help='Disable the spinner and periodic waiting messages; keep stage boundaries, warnings and errors.')
    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    if not argv or args.tui:
        if args.output != 'auto':
            parser.error('--tui cannot be combined with --output text or --output json.')
        if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get('TERM') == 'dumb':
            parser.error('The TUI needs an interactive terminal. Use --config FILE for command-line output.')
        try:
            from src.tui.app import launch
        except ModuleNotFoundError as exc:
            if exc.name not in ('textual', 'rich'):
                raise
            parser.error('Install the required TUI dependency with: python3 -m pip install -r ' + str(ROOT / 'requirements.txt'))
        return launch(args)
    if args.config is None:
        parser.error('--config is required unless using --tui.')
    reporter = Reporter(mode=output_mode(args.output, sys.stdout), verbose=args.verbose, progress=not args.no_progress)
    return run_audit(args, reporter)


def run_audit(args, reporter) -> int:
    """Run one audit with the same lifecycle for CLI and interactive callers."""
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
            manifest['metrics'] = runner.metrics.snapshot(finish=True)
        else:
            manifest.setdefault('diagnostics', []).append(reporter.error(exc, phase='preflight',
                analysis_started=False, switches_performed=False))
        if run_created:
            try:
                save_json(run_dir / 'manifest.json', manifest)
            except OSError as save_error:
                reporter.error(save_error)
    finally:
        # Presentation failures never bypass temporary-source or resource cleanup.
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
            'Enabled-stage policy checks satisfied; factual correctness is not established.' if result['status'] == 'COMPLETE' else
            'Processing may be incomplete or evidence may be insufficient; consult limitations and diagnostics.')
        result['review_quality'] = 'NOT_MEASURED'
        result['review_enabled'] = manifest.get('review_enabled', (config or {}).get('execution', {}).get('review_enabled', False))
        result['workflow_satisfied'] = manifest.get('workflow_satisfied', False)
        if 'metrics' in manifest:
            result['metrics'] = manifest['metrics']
        else:
            metrics = RunMetrics(reporter.clock)
            metrics.started = reporter.started
            result['metrics'] = metrics.snapshot(finish=True)
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
