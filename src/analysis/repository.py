# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Pinned Git hierarchies, protected checkout switches and restoration."""
from __future__ import annotations
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from src.messages import message as ui_message
from src.runtime.reporting import NullReporter
from src.runtime.execution import Budget
from src.analysis.evidence import stamp
from src.analysis.git_sources import ignore_path
from src.runtime.errors import AuditError, UnsafeRepository
from src.runtime.artifacts import digest, inside
from src.runtime import artifacts, process as process_runtime

MIN_GIT_VERSION = (2, 34, 1)

class GitRuntime:
    """One PATH resolution and version check for the entire repository hierarchy."""
    def __init__(self):
        executable = shutil.which('git')
        if not executable:
            raise AuditError(ui_message('diagnostic.repository.git_is_required_for_git_mode_git_was'))
        self.executable = str(Path(executable).resolve())
        self.env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL') if k in os.environ}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                        GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_PAGER='cat',
                        GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='', GIT_NO_REPLACE_OBJECTS='1')
        # Do not discover a repository or use an inherited TMPDIR in source files.
        with tempfile.TemporaryDirectory(prefix='archaudit-git-version-', dir='/tmp') as neutral:
            result = process_runtime.process([self.executable, '--version'], Path(neutral), self.env, budget=Budget(30))
        self.version_string = result['stdout'].decode('utf-8', errors='replace').strip()
        match = re.fullmatch(r'git version (\d+)\.(\d+)\.(\d+)(?:[-+. ][^\r\n]*)?', self.version_string)
        if result['returncode'] or not match:
            raise AuditError(ui_message(
                'diagnostic.repository.cannot_determine_git_version_at_git_is_required',
                executable=self.executable,
                version_string=self.version_string,
            ))
        self.version = tuple(map(int, match.groups()))
        if self.version < MIN_GIT_VERSION:
            raise AuditError(ui_message(
                'diagnostic.repository.found_at_git_is_required',
                version_string=self.version_string,
                executable=self.executable,
            ))
        self.temporary = None

    def trust_config(self, path: Path) -> str:
        if str(path).endswith('/*'):
            raise UnsafeRepository(ui_message('diagnostic.repository.cannot_express_exact_safe_directory_trust_for_a', path=path))
        if self.temporary is None:
            self.temporary = tempfile.TemporaryDirectory(prefix='archaudit-git-trust-', dir='/tmp')
        config = Path(self.temporary.name) / (uuid.uuid4().hex + '.gitconfig')
        value = str(path).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t').replace('\b', '\\b')
        artifacts.atomic(config, '[safe]\n\tdirectory =\n\tdirectory = "' + value + '"\n')
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
                raise UnsafeRepository(ui_message(
                    'diagnostic.repository.repository_ownership_mismatch_at_owner_uid_runner_uid',
                    path=path,
                    owner=owner,
                    value=os.geteuid(),
                ))

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
                error += (ui_message('diagnostic.repository.ownership_hint'))
            raise AuditError(ui_message('diagnostic.repository.git_failed', value=args[0], error=error))
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
                raise UnsafeRepository(ui_message(
                    'diagnostic.repository.invalid_symbolic_head_at',
                    value=self.git_dir / "HEAD",
                )) from exc
            if not ref.startswith('refs/') or '\0' in ref:
                raise UnsafeRepository(ui_message(
                    'diagnostic.repository.invalid_symbolic_head_at.symbolic_ref',
                    value=self.git_dir / "HEAD",
                    ref=ref,
                ))
            try:
                self.git('check-ref-format', ref)
            except AuditError as exc:
                raise UnsafeRepository(ui_message(
                    'diagnostic.repository.invalid_symbolic_head_at.symbolic_ref.template',
                    value=self.git_dir / "HEAD",
                    ref=ref,
                    exc=exc,
                )) from exc
            return ref
        if not re.fullmatch(b'[0-9a-fA-F]+', value):
            raise UnsafeRepository(ui_message('diagnostic.repository.invalid_detached_head_at', value=self.git_dir / "HEAD"))
        # Both SHA-1 and SHA-256 repositories exist in Git 2.34.1.
        try:
            object_format = self.text('rev-parse', '--show-object-format')
            if object_format not in ('sha1', 'sha256') or len(value) != {'sha1': 40, 'sha256': 64}[object_format]:
                raise AuditError(ui_message('diagnostic.repository.invalid_object_id_for_format', object_format=object_format))
            if self.text('cat-file', '-t', value.decode('ascii')) != 'commit':
                raise AuditError(ui_message('diagnostic.repository.object_is_not_a_commit'))
        except AuditError as exc:
            raise UnsafeRepository(ui_message(
                'diagnostic.repository.invalid_detached_head_at.symbolic_ref',
                value=self.git_dir / "HEAD",
                exc=exc,
            )) from exc
        return None

    def read_head(self) -> tuple[bytes, list]:
        path = self.git_dir / 'HEAD'
        def stamp(info):
            return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise UnsafeRepository(ui_message('diagnostic.repository.head_must_be_a_regular_file_not_a', path=path))
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or stamp(before) != stamp(opened):
                    raise UnsafeRepository(ui_message('diagnostic.repository.head_changed_while_opening', path=path))
                data = stream.read(4097)
                if stamp(opened) != stamp(os.fstat(stream.fileno())) or stamp(opened) != stamp(path.lstat()):
                    raise UnsafeRepository(ui_message('diagnostic.repository.head_changed_while_reading', path=path))
            if not data or len(data) > 4096:
                raise UnsafeRepository(ui_message('diagnostic.repository.empty_or_oversized_head', path=path))
            return data, stamp(opened) + [digest(data)]
        except OSError as exc:
            raise UnsafeRepository(ui_message('diagnostic.repository.cannot_read_head_at', path=path, exc=exc)) from exc

    def clean(self, *, local: bool = False) -> None:
        if not local and hasattr(self, 'nodes'):
            for path, node in self.nodes.items():
                try:
                    node.clean(local=True)
                except AuditError as exc:
                    raise UnsafeRepository(**vars(exc.with_context(node_path=path).diagnostic())) from exc
            return
        if self.git('ls-files', '--unmerged', '-z'):
            raise UnsafeRepository(ui_message('diagnostic.repository.conflicted_git_index'), node_path=str(self.path))
        flags = self.git('ls-files', '-v', '-z').split(b'\0')
        if any(line and (line[:1].islower() or line[:1] == b'S') for line in flags):
            raise UnsafeRepository(ui_message('diagnostic.repository.assume_unchanged_worktree_entries_are_unsupported'))
        if not hasattr(self, 'excludes_file'):
            self.excludes_file = ignore_path(self)
        # Do not inspect children before their Git directories have been verified.
        # Check staged gitlinks separately; --ignore-submodules=all hides them.
        staged = self.git('diff', '--cached', '--name-only', '-z', '--no-ext-diff',
                          '--no-textconv', '--ignore-submodules=none', 'HEAD', '--')
        if staged or self.git('-c', 'core.excludesFile=' + str(self.excludes_file), 'status',
                              '--porcelain=v1', '-z', '--untracked-files=all', '--ignore-submodules=all'):
            raise UnsafeRepository(ui_message('diagnostic.repository.a_clean_checkout_is_required_staged_tracked_or'),
                                   code='SOURCE_CHANGED', failure_layer='integrity', node_path=str(self.path))

    @staticmethod
    def safe_relative(value: str) -> str:
        if (not value or value.startswith('/') or '\\' in value or
                any(part in ('', '.', '..') or part.lower() == '.git' for part in value.split('/'))):
            raise UnsafeRepository(ui_message('diagnostic.repository.unsafe_submodule_path_or_name', value=value))
        return value

    @staticmethod
    def directory_chain(path: Path, boundary: Path) -> dict:
        if boundary.resolve() != boundary or not inside(path, boundary):
            raise UnsafeRepository(ui_message('diagnostic.repository.path_escapes_checkout_boundary', path=path))
        result = {}
        items = [boundary]
        for part in path.relative_to(boundary).parts:
            items.append(items[-1] / part)
        for item in items:
            info = item.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise UnsafeRepository(ui_message(
                    'diagnostic.repository.directory_replaced_or_symbolic_link_encountered',
                    item=item,
                ))
            result[str(item)] = [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid]
        return result

    @staticmethod
    def file_stamp(path: Path) -> list | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeRepository(ui_message('diagnostic.repository.git_metadata_must_be_a_regular_file', path=path))
        return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, digest(path.read_bytes())]

    def setup_paths(self, root: Repository, parent: Repository | None = None, name: str | None = None) -> None:
        entry = self.path / '.git'
        self.root_path = root.path
        self.directory_chain(self.path, root.path)
        if entry.is_symlink():
            raise UnsafeRepository(ui_message('diagnostic.repository.symbolic_git_entry', entry=entry))
        if entry.is_dir():
            expected = entry
        elif parent and entry.is_file():
            raw = entry.read_text().rstrip('\n')
            if not raw.startswith('gitdir: '):
                raise UnsafeRepository(ui_message('diagnostic.repository.invalid_git_file', entry=entry))
            expected = parent.git_dir / 'modules' / self.safe_relative(name)
            target = Path(os.path.abspath(self.path / raw[8:]))
            if target != expected:
                raise UnsafeRepository(ui_message('diagnostic.repository.unexpected_external_git_directory_for', path=self.path))
        else:
            if parent:
                raise AuditError(ui_message(
                    'diagnostic.repository.the_submodule_is_not_initialized_locally',
                ), code='SUBMODULE_NOT_INITIALIZED')
            raise AuditError(ui_message('diagnostic.repository.a_normal_standalone_checkout_is_required_no_bare'))
        boundary = root.path
        self.directory_chain(expected, boundary)
        self.git_dir = expected
        self.git_boundary = boundary
        self.check_ownership()
        self._fixed = self.fixed_metadata()
        self.control_metadata()
        if self._fixed['files']['commondir'] is not None:
            raise UnsafeRepository(ui_message('diagnostic.repository.linked_common_git_directories_are_unsupported'))
        if self.trust_repository:
            self.env['GIT_CONFIG_GLOBAL'] = self.runtime.trust_config(self.path)

    def setup_node(self, root: Repository, parent: Repository | None = None, name: str | None = None) -> None:
        self.setup_paths(root, parent, name)
        # 2.34.1 ignores GIT_NO_LAZY_FETCH. A failed lazy fetch can even write
        # remote.*.partialclonefilter before GIT_ALLOW_PROTOCOL denies transport.
        # Reject these configurations before resolving HEAD or reading objects.
        if self.git('config', '--get-regexp',
                    r'^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$', allowed=(0, 1)):
            raise AuditError(ui_message('diagnostic.repository.partial_clone_promisor_configuration_is_unsupported_git_may'))
        self.symbolic_ref()
        # Validate the paths before any command receives a safe.directory exception.
        actual = Path(self.git('rev-parse', '--absolute-git-dir').decode().rstrip('\n'))
        common = Path(os.path.abspath(self.path / self.git('rev-parse', '--git-common-dir').decode().rstrip('\n')))
        top = Path(self.git('rev-parse', '--show-toplevel').decode().rstrip('\n'))
        if actual != self.git_dir or common != self.git_dir or top != self.path:
            raise UnsafeRepository(ui_message(
                'diagnostic.repository.git_data_does_not_belong_to_expected_checkout',
                path=self.path,
            ))
        self.policy()
        self.clean(local=True)
        self._trees = {}

    def fixed_metadata(self) -> dict:
        directories = self.directory_chain(self.path, self.root_path)
        directories.update(self.directory_chain(self.git_dir, self.git_boundary))
        for name in ('objects', 'refs'):
            directories.update(self.directory_chain(self.git_dir / name, self.git_dir))
        if (self.git_dir / 'objects/info/alternates').exists():
            raise UnsafeRepository(ui_message(
                'diagnostic.repository.external_object_alternates_are_unsupported_make_objects_local',
            ))
        files = {name: self.file_stamp(self.git_dir / name)
                 for name in ('config', 'config.worktree', 'commondir')}
        if self.path / '.git' != self.git_dir:
            files['gitfile'] = self.file_stamp(self.path / '.git')
        return {'directories': directories, 'files': files}

    def control_metadata(self) -> dict:
        rules = {}
        for path in (self.git_dir / 'info/exclude', getattr(self, 'excludes_file', None)):
            if path is not None:
                try:
                    rules[str(path)] = [list(stamp(path.lstat())), list(stamp(path.stat()))]
                except FileNotFoundError:
                    rules[str(path)] = None
        return {'HEAD': self.read_head()[1], 'index': self.file_stamp(self.git_dir / 'index'), 'ignore_rules': rules}

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
            raise UnsafeRepository(ui_message('diagnostic.repository.working_directory_or_git_metadata_changed_outside_the'))

    def policy(self) -> None:
        if self.git('config', '--get-regexp', r'^filter\.', allowed=(0, 1)):
            raise AuditError(ui_message('diagnostic.repository.git_filters_including_git_lfs_filters_require_a'))
        for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-apply', 'rebase-merge',
                       'BISECT_START', 'sequencer', 'index.lock', 'HEAD.lock'):
            if (self.git_dir / marker).exists():
                raise UnsafeRepository(ui_message('diagnostic.repository.unfinished_git_operation', marker=marker))
        if self.text('config', '--bool', '--get', 'core.sparseCheckout', allowed=(0, 1)) == 'true':
            raise AuditError(ui_message('diagnostic.repository.sparse_checkouts_are_unsupported'))

    def tree(self, commit: str) -> dict:
        if commit in self._trees:
            return self._trees[commit]
        if self.text('cat-file', '-t', commit) != 'commit':
            raise AuditError(ui_message('diagnostic.repository.required_object_is_not_a_commit'))
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
                raise AuditError(ui_message('diagnostic.repository.required_tree_blob_objects_are_n_a_locally'))
        links = {p: oid for p, (mode, kind, oid) in entries.items() if mode == '160000'}
        modules = {}
        if '.gitmodules' in entries:
            if entries['.gitmodules'][0] not in ('100644', '100755'):
                raise AuditError(ui_message('diagnostic.repository.gitmodules_must_be_a_regular_tracked_file'))
            try:
                raw = self.git('config', '--no-includes', '--null', '--blob', commit + ':.gitmodules',
                               '--get-regexp', r'^submodule\..*\.path$', allowed=(0, 1))
            except AuditError as exc:
                raise AuditError(ui_message('diagnostic.repository.cannot_parse_committed_gitmodules')) from exc
            names = set()
            for record in raw.split(b'\0'):
                if not record:
                    continue
                key, path = record.decode('utf-8').split('\n', 1)
                name = self.safe_relative(key[len('submodule.'):-len('.path')])
                self.safe_relative(path)
                if path in modules or name in names:
                    raise AuditError(ui_message('diagnostic.repository.duplicate_submodule_path_name', path=path, name=name))
                modules[path] = name
                names.add(name)
        if set(links) != set(modules):
            raise AuditError(ui_message(
                'diagnostic.repository.gitlinks_and_committed_gitmodules_paths_differ',
                value=repr(sorted(set(links) ^ set(modules))),
            ))
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
                        raise UnsafeRepository(ui_message('diagnostic.repository.git_directory_reused_by_multiple_submodules'))
                    used.add(identity)
                    self.nodes[path] = node
                    self.descriptions[path] = {'path': path, 'parent': None if parent is None else
                        str(parent.path.relative_to(self.path)), 'name': name}
                    actual = node.head()
                    if required is not None and actual != required:
                        raise UnsafeRepository(ui_message('diagnostic.repository.submodule_head_differs_from_its_parent_gitlink'))
                    required = actual
                    ref = node.symbolic_ref()
                    if ref and not ref.startswith('refs/heads/'):
                        raise UnsafeRepository(ui_message('diagnostic.repository.unsupported_symbolic_head', ref=ref))
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
                            raise AuditError(ui_message(
                                'diagnostic.repository.unsupported_submodule_structure_change_at_required_sha_paths',
                                path=path,
                                child=child,
                                commit=detail["commit"],
                            ))
                for child, detail in children.items():
                    full = child if path == '.' else path + '/' + child
                    child_path = node.path / child
                    # Check lexical paths before Repository resolves them.
                    if discover:
                        try:
                            self.directory_chain(child_path, self.path)
                        except OSError as exc:
                            raise AuditError(ui_message('diagnostic.repository.the_submodule_is_not_initialized_locally'),
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
                head = ('ref: ' + state['ref'] if state['ref'] else state['commit']).encode()
                # The pinned HEAD bytes prove detached commit identity; attached
                # originals also verify their ref below. Do not re-resolve HEAD.
                if node.control_metadata() != state['control'] or node.read_head()[0].strip() != head:
                    raise UnsafeRepository(ui_message('diagnostic.repository.head_index_changed_outside_the_orchestrator'))
                original = self.original[path]
                if original['ref'] and (node.reference_metadata(original['ref']) != original['ref_identity'] or
                        node.text('rev-parse', '--verify', original['ref']) != original['commit']):
                    raise UnsafeRepository(ui_message('diagnostic.repository.original_branch_moved_during_the'))
                node.policy()
                node.clean(local=True)
            except (AuditError, OSError) as exc:
                detail = exc if isinstance(exc, AuditError) else AuditError(str(exc), code='IO_ERROR')
                error = UnsafeRepository(**vars(detail.with_context(node_path=path).diagnostic()))
                error.node = path
                raise error from exc

    def switch_node(self, path, commit, ref=None, *, restoring=False, persist=lambda: None):
        node = self.nodes[path]
        node.guard_metadata()
        node.policy()
        control = node.control_metadata()
        if control['ignore_rules'] != self.expected[path]['control']['ignore_rules']:
            raise UnsafeRepository(ui_message('diagnostic.repository.ignore_rules_changed_before_checkout'), node_path=path)
        if not restoring and control != self.expected[path]['control']:
            raise UnsafeRepository(ui_message(
                'diagnostic.repository.head_index_or_ignore_rules_changed_before_checkout',
            ), node_path=path)
        original = self.original[path]
        if original['ref'] and (node.reference_metadata(original['ref']) != original['ref_identity'] or
                node.text('rev-parse', '--verify', original['ref']) != original['commit']):
            raise UnsafeRepository(ui_message('diagnostic.repository.original_branch_moved_during_the'), node_path=path)
        entry = {'node_path': path, 'commit': commit, 'ref': ref,
                 'restoring': restoring, 'status': 'STARTED'}
        self.journal.append(entry)
        persist()
        try:
            arguments = ['switch', '--no-recurse-submodules', '--no-overwrite-ignore']
            arguments += ['--no-guess', ref[len('refs/heads/'):]] if ref else ['--detach', commit]
            node.git(*arguments)
            node.guard_metadata()
            if node.head() != commit or node.symbolic_ref() != ref:
                raise UnsafeRepository(ui_message('diagnostic.repository.checkout_did_not_reach_the_expected_head'), node_path=path)
            self.expected[path] = {'commit': commit, 'ref': ref, 'control': node.control_metadata()}
            entry['status'] = 'COMPLETE'
        except BaseException as exc:
            entry.update(status='FAILED', error=str(exc) or type(exc).__name__)
            raise
        finally:
            persist()

    def checkout(self, commit, persist=lambda: None):
        self.assert_expected()
        for path, required in self.plans[commit].items():
            self.switch_node(path, required, persist=persist)
        self.assert_expected()

    def restore(self, persist=lambda: None):
        result = {'restored': True, 'nodes': {}, 'journal_errors': []}
        def checkpoint():
            try:
                persist()
            except OSError as exc:
                # Report output failure without preventing a safe restoration.
                if str(exc) not in result['journal_errors']:
                    result['journal_errors'].append(str(exc))
        for path in reversed(self.nodes):
            original = self.original[path]
            state = {'commit': original['commit'], 'ref': original['ref'], 'restored': False}
            result['nodes'][path] = state
            try:
                self.switch_node(path, original['commit'], original['ref'], restoring=True, persist=checkpoint)
                self.nodes[path].clean(local=True)
                state['restored'] = True
            except BaseException as exc:
                state['error'] = str(exc) or type(exc).__name__
                result['restored'] = False
                if isinstance(exc, KeyboardInterrupt):
                    result['interrupted'] = True
        return result

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
            raise AuditError(ui_message('diagnostic.repository.unexpected_git_diff_raw_z_layout'))
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
