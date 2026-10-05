# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Read-only Git inputs and independent, disposable source copies.

Git selects paths; descriptor-relative reads select bytes. No checkout, index
write, filter, archive export attribute, or symlink traversal is involved.
"""
from __future__ import annotations
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import uuid

from src.analysis.evidence import canonical, sha, open_source_directory, read_confined, SourceChanged, stamp


def ignore_path(node):
    """Import only core.excludesFile, never the user's execution configuration.

    `config` is a read-only parser (including conditional includes). Other Git
    commands keep the private runtime configuration and disabled hooks/filters.
    """
    env = node.env.copy()
    for key in ('HOME', 'XDG_CONFIG_HOME'):
        if key in os.environ:
            env[key] = os.environ[key]
    env['GIT_CONFIG_GLOBAL'] = os.environ.get('GIT_CONFIG_GLOBAL', str(Path.home() / '.gitconfig'))
    # Without an explicit override, Git reads both ~/.gitconfig and XDG config.
    if 'GIT_CONFIG_GLOBAL' not in os.environ:
        env.pop('GIT_CONFIG_GLOBAL', None)
    command = node.prefix + ['config', '--global', '--includes', '--path', '-z', '--get', 'core.excludesFile']
    result = subprocess.run(command, env=env, capture_output=True, timeout=30)
    if result.returncode not in (0, 1):
        raise ValueError('Cannot read user Git ignore configuration.')
    # Local configuration has precedence over global configuration.
    local_env = node.env | {k: env[k] for k in ('HOME', 'XDG_CONFIG_HOME') if k in env}
    local = subprocess.run(node.prefix + ['config', '--path', '-z', '--get', 'core.excludesFile'],
                           env=local_env, capture_output=True, timeout=30)
    if local.returncode not in (0, 1):
        raise ValueError('Cannot read repository ignore configuration.')
    value = (local.stdout if local.returncode == 0 else result.stdout).removesuffix(b'\0')
    if value:
        path = Path(os.fsdecode(value)).expanduser()
        return path if path.is_absolute() else node.path / path
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'git/ignore'


class GitSources:
    def __init__(self, repo, folder_type, error_type, temporary_base):
        self.repo, self.folder_type, self.error = repo, folder_type, error_type
        base = Path(temporary_base).resolve()
        if base == repo.path or repo.path in base.parents:
            raise self.error('Source snapshots must be outside the original repository.', node_path=str(base))
        self.temporary = tempfile.TemporaryDirectory(prefix='archaudit-sources-', dir=temporary_base)
        self.root = Path(self.temporary.name).resolve()
        self.snapshots = {}

    def close(self):
        self.temporary.cleanup()

    def fail(self, message, path=None):
        raise self.error(message, code='SOURCE_CHANGED', failure_layer='integrity', node_path=path)

    def entries(self, node, commit):
        result = {}
        for record in node.git('ls-tree', '-r', '-z', commit).split(b'\0'):
            if record:
                header, raw = record.split(b'\t', 1)
                path = os.fsdecode(raw)
                node.safe_relative(path)
                result[path] = header.decode('ascii').split()
        return result

    def index(self, node):
        result = {}
        for record in node.git('ls-files', '--stage', '-z').split(b'\0'):
            if record:
                header, raw = record.split(b'\t', 1)
                mode, oid, stage = header.decode('ascii').split()
                path = os.fsdecode(raw)
                node.safe_relative(path)
                if stage != '0':
                    raise self.error('Conflicted Git index.', node_path=str(node.path / path))
                result[path] = (mode, oid)
        return result

    def check_modules(self, node, index, expected):
        """Only path/name changes are structural; URL/other edits are source data."""
        wanted = {path: value['name'] for path, value in expected.items()}
        def check(raw):
            paths = {}
            for record in raw.split(b'\0'):
                if record:
                    key, path = os.fsdecode(record).split('\n', 1)
                    name = key[len('submodule.'):-len('.path')]
                    node.safe_relative(path)
                    node.safe_relative(name)
                    if path in paths or name in paths.values():
                        raise self.error('Duplicate submodule path/name.', node_path=str(node.path / '.gitmodules'))
                    paths[path] = name
            if paths != wanted:
                raise self.error('Unsupported submodule structure change in .gitmodules.',
                                 node_path=str(node.path / '.gitmodules'))
        arguments = ['config', '--no-includes', '--null']
        query = ['--get-regexp', r'^submodule\..*\.path$']
        record = index.get('.gitmodules')
        if record:
            if record[0] not in ('100644', '100755'):
                raise self.error('.gitmodules must be a regular file.', node_path=str(node.path / '.gitmodules'))
            check(node.git(*arguments, '--blob', record[1], *query, allowed=(0, 1)))
        else:
            check(b'')
        entry, data = self.read_working(node, '.gitmodules')
        if entry is not None and data is None:
            raise self.error('.gitmodules must be a regular file.', node_path=str(node.path / '.gitmodules'))
        if data is None:
            check(b'')
        else:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix='modules-') as stream:
                stream.write(data)
                stream.flush()
                check(node.git(*arguments, '--file', stream.name, *query, allowed=(0, 1)))

    def read_working(self, node, path, *, replaced_file=False):
        """Missing files are deletions. Symlinks are metadata, never copied links."""
        fd = open_source_directory(node.path)
        fds = [fd]
        try:
            parts = path.split('/')
            for name in parts[:-1]:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                fds.append(fd)
            info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            if replaced_file and stat.S_ISDIR(info.st_mode):
                # A former file can become a directory. Git enumerates its new
                # children separately; the old file is a deletion.
                return None, None
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(parts[-1], dir_fd=fd)
                if stamp(info) != stamp(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)):
                    self.fail('Symbolic link changed during source preparation.', path)
                return {'type': 'symlink', 'target': target, 'mode': '120000'}, None
            if not stat.S_ISREG(info.st_mode):
                raise self.error('Unsupported source file type.', node_path=path)
            data = read_confined(node.path, path, info.st_size)
            if stamp(info) != stamp(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)):
                self.fail('Source changed during source preparation.', path)
            return {'type': 'file', 'mode': '100755' if info.st_mode & 0o111 else '100644',
                    'size': len(data), 'sha256': sha(data)}, data
        except FileNotFoundError:
            return None, None
        except (OSError, ValueError, SourceChanged) as exc:
            raise self.error('Cannot safely read source during preparation.', node_path=path,
                             code='SOURCE_CHANGED', failure_layer='integrity') from exc
        finally:
            for opened in reversed(fds):
                os.close(opened)

    def ignore_token(self, node, paths, excludes):
        # Read rules only, never the contents of ignored files. Rules can change
        # even when they happen to select the same set in both scans.
        rules = {}
        candidates = {'.gitignore'}
        for path in paths:
            candidates.update(str(parent / '.gitignore') for parent in Path(path).parents if str(parent) != '.')
        for path in sorted(candidates):
            entry, _ = self.read_working(node, path)
            rules[path] = entry
        for path in (node.git_dir / 'info/exclude', excludes):
            # Git itself resolves user exclusion paths; hash the same rule bytes.
            try:
                rules[str(path)] = sha(path.read_bytes())
            except FileNotFoundError:
                rules[str(path)] = None
        return rules

    def working_scan(self, destination=None):
        entries, states, submodules = {}, {}, []
        modified = False
        untracked_count = 0
        self.repo.assert_expected()
        for prefix, node in self.repo.nodes.items():
            base = self.repo.original[prefix]['commit']
            index = self.index(node)
            tree = self.entries(node, base)
            child_links = {p: oid for p, (mode, oid) in index.items() if mode == '160000'}
            expected_links = node.tree(base)
            if set(child_links) != set(expected_links):
                raise self.error('Unsupported staged submodule structure change.', node_path=prefix)
            if expected_links or '.gitmodules' in index:
                self.check_modules(node, index, expected_links)
            try:
                excludes = ignore_path(node)
            except ValueError as exc:
                raise self.error('Cannot resolve Git ignore configuration.', node_path=str(node.path)) from exc
            ignore_arg = 'core.excludesFile=' + str(excludes)
            status = node.git('-c', ignore_arg, 'status', '--porcelain=v1', '-z',
                              '--untracked-files=all', '--ignore-submodules=all')
            modified |= bool(status)
            paths = {os.fsdecode(p) for p in node.git('ls-files', '--cached', '-z').split(b'\0') if p}
            untracked = {os.fsdecode(p) for p in node.git('-c', ignore_arg, 'ls-files', '--others',
                         '--exclude-standard', '-z').split(b'\0') if p}
            paths |= untracked
            untracked_count += len(untracked)
            # Index distinctions survive even when disk bytes equal HEAD.
            modified |= {p: tuple(v[:1] + v[2:]) for p, v in tree.items()} != index
            state = {'head': node.head(), 'branch': node.symbolic(), 'index': index,
                     'status': [os.fsdecode(p) for p in status.split(b'\0') if p],
                     'rules': self.ignore_token(node, paths, excludes)}
            states[prefix] = state
            local = {}
            for path in sorted(paths - set(child_links)):
                node.safe_relative(path)
                entry, data = self.read_working(node, path, replaced_file=path in index)
                if entry is None:
                    modified |= path in tree
                    continue
                full = path if prefix == '.' else prefix + '/' + path
                entries[full] = entry
                local[path] = entry
                if destination is not None and data is not None:
                    target = destination / full
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with target.open('xb') as stream:
                        stream.write(data)
                    target.chmod(0o700 if entry['mode'] == '100755' else 0o600)
            modified |= bool(set(tree) - set(child_links) - set(local))
            if prefix != '.':
                parent_path = self.repo.descriptions[prefix]['parent'] or '.'
                parent = self.repo.nodes[parent_path]
                relative = str(node.path.relative_to(parent.path))
                baseline_link = self.entries(parent, self.repo.base_plan[parent_path])[relative][2]
                index_link = self.index(parent)[relative][1]
                modified |= base != baseline_link
                submodules.append(dict(self.repo.descriptions[prefix], base_gitlink=baseline_link,
                    index_gitlink=index_link, actual_head=base, expected_commit=base,
                    actual={'commit': base, 'ref': node.symbolic_ref()}, snapshot_verified=True))
        self.repo.assert_expected()
        return {'entries': entries, 'git_state': states, 'submodules': submodules,
                'has_local_changes': bool(modified), 'untracked_files': untracked_count}

    def finish(self, label, kind, commit, path, state, branch):
        inventory = self.folder_type(path, canonical=True).snapshot()
        provenance = {'repository': str(self.repo.path), 'branch': branch, 'base_commit': commit,
                      'source_type': kind, 'snapshot_id': path.name,
                      'fingerprint': sha(canonical(state))}
        result = {'path': path, 'inventory': inventory, 'source_snapshot': provenance, **state}
        self.snapshots[label] = result
        return result

    def working(self, label):
        path = self.root / uuid.uuid4().hex
        path.mkdir(mode=0o700)
        try:
            before = self.working_scan()
            copied = self.working_scan(path)
            after = self.working_scan()
            if before != copied or copied != after:
                left, right = (before, copied) if before != copied else (copied, after)
                changed = next((p for p in sorted(left['entries'].keys() | right['entries'].keys())
                                if left['entries'].get(p) != right['entries'].get(p)), str(self.repo.path))
                self.fail('Source, index or ignore rules changed during source preparation; retry the run.', changed)
            return self.finish(label, 'working_tree', self.repo.original['.']['commit'], path, copied,
                               self.repo.symbolic())
        except (OSError, ValueError, SourceChanged) as exc:
            self.fail('Source changed or became unreadable during source preparation; retry the run.')

    def commit(self, label, commit):
        path = self.root / uuid.uuid4().hex
        path.mkdir(mode=0o700)
        entries = {}
        for prefix, pinned in self.repo.plans[commit].items():
            node = self.repo.nodes[prefix]
            for relative, (mode, kind, oid) in self.entries(node, pinned).items():
                if mode == '160000':
                    continue
                full = relative if prefix == '.' else prefix + '/' + relative
                data = node.git('cat-file', 'blob', oid)
                if mode == '120000':
                    entries[full] = {'type': 'symlink', 'target': os.fsdecode(data), 'mode': mode}
                elif mode in ('100644', '100755'):
                    target = path / full
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.write_bytes(data)
                    target.chmod(0o700 if mode == '100755' else 0o600)
                    entries[full] = {'type': 'file', 'mode': mode, 'size': len(data), 'sha256': sha(data)}
                else:
                    raise self.error('Unsupported committed source type.', node_path=full)
        subs = [dict(self.repo.descriptions[p], expected_commit=c, actual_head=c, snapshot_verified=True)
                for p, c in self.repo.plans[commit].items() if p != '.']
        return self.finish(label, 'commit', commit, path, {'entries': entries, 'submodules': subs}, label)

    def assert_intact(self, snapshot=None):
        for item in ([snapshot] if snapshot else self.snapshots.values()):
            try:
                actual = self.folder_type(item['path'], canonical=True).snapshot()
                if actual['source_fingerprint'] != item['inventory']['source_fingerprint']:
                    before = {e['path']: e for e in item['inventory']['entries']}
                    after = {e['path']: e for e in actual['entries']}
                    changed = next(p for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p))
                    self.fail('Prepared source snapshot changed during analysis.', changed)
            except OSError as exc:
                self.fail('Prepared source snapshot became unreadable.', str(item['path']))

    def delta(self, baseline, other):
        a, b = self.snapshots[baseline], self.snapshots[other]
        changes = []
        for path in sorted(a['entries'].keys() | b['entries'].keys()):
            old, new = a['entries'].get(path), b['entries'].get(path)
            if old != new:
                changes.append({'path': path, 'status': 'A' if old is None else 'D' if new is None else 'M',
                    'old_mode': old['mode'] if old else '000000', 'new_mode': new['mode'] if new else '000000',
                    'old_sha256': sha(canonical(old)) if old else None, 'new_sha256': sha(canonical(new)) if new else None})
        old_subs = {s['path']: s for s in a['submodules']}
        subs = [dict(path=s['path'], baseline_commit=old_subs[s['path']]['expected_commit'],
                     branch_commit=s['expected_commit']) for s in b['submodules']
                if old_subs[s['path']]['expected_commit'] != s['expected_commit']]
        return {'orientation': 'baseline_snapshot_to_branch_snapshot',
                'baseline_source': a['source_snapshot'], 'branch_source': b['source_snapshot'],
                'changes': changes, 'submodule_changes': subs, 'identical_trees': not changes and not subs,
                'limitations': ['Snapshot path/content/mode changes only; renames appear as deletion plus addition.']}
