# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Filtered directory inventories and pinned source metadata."""
from __future__ import annotations
import hashlib
import json
import os
import stat
from pathlib import Path
from src.analysis.evidence import open_source_directory, stamp, valid_path
from src.analysis.source_filter import normalize_source_filter, excluded_root, gitignore_spec, gitignore_match
from src.analysis.source_metrics import PhysicalLines
from src.runtime.errors import AuditError
from src.runtime.artifacts import digest

class Folder:
    def __init__(self, path: Path, *, canonical=False, source_filter=None, paths=None):
        self.path = path if canonical else path.resolve()
        self.inventory = None
        self.metadata = {}
        self.files = {}
        self.source_filter = normalize_source_filter(source_filter if source_filter is not None else {})
        self.ignore_rules = {}
        self.paths = None if paths is None else frozenset(paths) | {
            str(parent) for path in paths for parent in Path(path).parents}

    def _scan(self, *, files=None, expected=None):
        entries, metadata = [], {}
        rules, exclusions = {}, []

        def changed(name):
            raise AuditError('Source folder changed during the run; files will not be restored.',
                             code='SOURCE_CHANGED', failure_layer='integrity', node_path=name)

        def record(info, name):
            # Excluded additions/removals can change a parent's timestamps.
            metadata[name] = stamp(info)[:3] if stat.S_ISDIR(info.st_mode) else stamp(info)
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
                if name == '.git':
                    continue
                child = name if relative == '.' else relative + '/' + name
                if self.paths is not None and child not in self.paths:
                    continue
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
                if not valid_path(child):
                    if expected is not None:
                        changed(child)
                    raise AuditError('Unsafe path in source folder.', code='UNSAFE_SOURCE_PATH',
                                     failure_layer='integrity', node_path=child)
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

    def snapshot(self) -> dict:
        """Pin one content inventory; later checks reuse its hashes."""
        entries, metadata, rules, exclusions = self._scan()
        rule_provenance = [{'path': path, 'sha256': rule['sha256']} for path, rule in sorted(rules.items())]
        filtered = self.source_filter['follow_gitignore'] or self.source_filter['exclude_paths']
        basis = {'entries': entries, 'settings': self.source_filter, 'rule_files': rule_provenance} if filtered else entries
        fingerprint = digest(json.dumps(basis, sort_keys=True, separators=(',', ':')).encode())
        self.inventory = {'source_directory': str(self.path), 'source_fingerprint': fingerprint,
                          'algorithm': 'sha256', 'entries': entries,
                          'source_filter': dict(self.source_filter, exclude_paths=list(self.source_filter['exclude_paths']),
                                                rule_files=rule_provenance, exclusions=exclusions)}
        self.metadata = metadata
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
        entries, _, rules, _ = self._scan(files=self.files, expected=self.metadata)
        if entries != self.inventory['entries'] or rules.keys() != self.ignore_rules.keys():
            raise AuditError('Source folder changed during the run; files will not be restored.',
                             code='SOURCE_CHANGED', failure_layer='integrity')
