# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Bounded, descriptor-relative source locators. Resolution never implies support.

Explicit rules or a Unicode BOM select strict decoding; UTF-8 is the default.
Lines split on LF only, CRLF normalized to LF, final unterminated line retained.
File hashes cover original bytes; fragment hashes cover normalized UTF-8.
No source text is retained in the resolution result.
"""
from __future__ import annotations
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from src.analysis.source_decoding import normalize_source_decoding, decode_source, SourceDecodeError

MAX_RECORD_BYTES = 16 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RANGE_LINES = 200
MAX_FRAGMENT_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024  # unique source paths read within one resolver call
MAX_EVIDENCE = 256


class SourceChanged(RuntimeError):
    """Integrity failure, never recoverable as a model pointer error."""


class PointerError(ValueError):
    """Closed-vocabulary resolution status; never arbitrary exception text."""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def lines(text):
    parts = text.replace('\r\n', '\n').split('\n')
    return [s + '\n' for s in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def source_catalog(context):
    folder = context.get('source_mode') == 'folder' or 'source_directory' in context
    identity = ({'mode': 'folder', 'fingerprint': context['source_fingerprint'],
                 'directory': context['source_directory']} if folder else
                {'mode': 'git', 'branch': context['branch'], 'commit': context['source_commit']})
    snapshot = context.get('source_snapshot')
    if snapshot and not folder:
        identity = dict(mode='git', **snapshot)
    result = [dict(id='source-001', root='.', identity=identity)]
    if not folder:
        for index, sub in enumerate(sorted(context.get('submodules', []), key=lambda s: s['path']), 2):
            sub_identity = ({'mode': 'git', 'commit': sub['expected_commit'], 'parent_commit': context['source_commit']}
                            if not snapshot else dict(mode='git', **snapshot, submodule_head=sub['expected_commit']))
            sub_identity['submodule_path'] = sub['path']
            result.append(dict(id=f'source-{index:03d}', root=sub['path'], identity=sub_identity))
    return result


def valid_path(path):
    return (type(path) is str and bool(path.strip()) and len(path) <= 4096 and
            not re.search(r'[\x00-\x1f\x7f\\:]', path) and
            all(part not in ('', '.', '..') for part in path.split('/')))


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def open_source_directory(root):
    """Open an already canonical source root without following ancestor links."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in Path(root).parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_confined(root, relative, limit):
    return _read_confined(root, relative, limit)[0]


def _read_confined(root, relative, limit, *, expected=None, read=True):
    """Walk absolute root and relative components with openat + O_NOFOLLOW.

    Directory handles stay open until final validation. Component replacement
    cannot redirect a subsequent open to an outside tree, including during races.
    Root is the orchestrator's canonical absolute path, never a model path.
    """
    fds, chain, identity = [], [], []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(fd)
        components = Path(root).parts[1:] + tuple(relative.split('/'))
        for index, name in enumerate(components):
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            is_file = index == len(components) - 1
            component_identity = stamp(before) if is_file else stamp(before)[:3]
            if expected is not None and component_identity != expected[index]:
                raise SourceChanged('Previously read source path changed during evidence resolution.')
            identity.append(component_identity)
            if not (stat.S_ISREG(before.st_mode) if is_file else stat.S_ISDIR(before.st_mode)):
                raise PointerError('UNSAFE_PATH')
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if not is_file:
                flags |= os.O_DIRECTORY
            child = os.open(name, flags, dir_fd=fd)
            fds.append(child)
            opened = os.fstat(child)
            if (stamp(before) if is_file else stamp(before)[:3]) != (stamp(opened) if is_file else stamp(opened)[:3]):
                raise SourceChanged('Source component changed during evidence resolution.')
            chain.append((fd, name, child, before, is_file))
            fd = child
        if stamp(before) != stamp(os.fstat(fd)):
            raise SourceChanged('Source changed before evidence read.')
        if read and before.st_size > limit:
            raise PointerError('LIMIT_EXCEEDED')
        data = bytearray()
        if read:
            while chunk := os.read(fd, min(65536, limit + 1 - len(data))):
                data.extend(chunk)
                if len(data) > limit:
                    raise SourceChanged('Source grew beyond the pinned evidence read size.')
        for parent, name, child, before, is_file in chain:
            # Ancestor mtimes may change due to unrelated /tmp peers; identity
            # checks are sufficient for directories, full stamps for the file.
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError:
                raise SourceChanged('Source component disappeared during evidence resolution.') from None
            for after in (os.fstat(child), current):
                if (stamp(before) if is_file else stamp(before)[:3]) != (stamp(after) if is_file else stamp(after)[:3]):
                    raise SourceChanged('Source changed during evidence resolution.')
        return bytes(data), tuple(identity)
    except (OSError, PointerError):
        if expected is not None:
            raise SourceChanged('Previously read source path became unavailable or unsafe.') from None
        raise
    finally:
        for fd in reversed(fds):
            os.close(fd)


def resolve_evidence(stage, pointers, context, expected_files=None):
    catalog = {s['id']: s for s in source_catalog(context)}
    if context.get('source_snapshot') and expected_files is None:
        expected_files = {e['path']: e['sha256'] for e in context.get('_inventory', {}).get('entries', [])
                          if e['type'] == 'file'}
    root = context.get('source_directory', context.get('repository'))
    decoding = normalize_source_decoding(context.get('source_decoding'))
    results, total, cache, read_paths = [], 0, {}, {}
    for index, pointer in enumerate(pointers):
        result = {k: pointer.get(k) for k in ('source_id', 'path', 'start_line', 'end_line')}
        result.update(id=stage + ':' + str(pointer.get('id', '')), source_identity=None,
                      file_sha256=None, fragment_sha256=None, encoding=None, status='INVALID_POINTER')
        results.append(result)
        try:
            if index >= MAX_EVIDENCE or len(canonical(pointer)) > MAX_RECORD_BYTES:
                raise PointerError('LIMIT_EXCEEDED')
            source = catalog.get(pointer.get('source_id'))
            if source is None:
                raise PointerError('UNKNOWN_SOURCE')
            result['source_identity'] = source['identity']
            path = pointer.get('path')
            if not valid_path(path):
                raise PointerError('INVALID_POINTER')
            start, end = pointer.get('start_line'), pointer.get('end_line')
            if type(start) is not int or type(end) is not int or start < 1 or end < start:
                raise PointerError('INVALID_POINTER')
            if end - start + 1 > MAX_RANGE_LINES:
                raise PointerError('LIMIT_EXCEEDED')
            relative = path if source['root'] == '.' else source['root'] + '/' + path
            owners = [s for s in catalog.values() if s['root'] == '.' or relative.startswith(s['root'] + '/')]
            if max(owners, key=lambda s: len(s['root']))['id'] != source['id']:
                raise PointerError('SOURCE_SCOPE_MISMATCH')
            # Repository control files are not architectural source evidence.
            if '.git' in relative.split('/'):
                raise PointerError('INVALID_POINTER')
            if (context.get('source_snapshot') and expected_files is not None and relative not in expected_files):
                raise PointerError('NOT_FOUND')
            if relative in cache:
                cached = cache[relative]
                _read_confined(root, relative, MAX_FILE_BYTES, expected=read_paths[relative], read=False)
                source_lines, result['file_sha256'], result['encoding'] = cached
            else:
                limit = MAX_FILE_BYTES if relative in read_paths else min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total)
                blob, identity = _read_confined(root, relative, limit, expected=read_paths.get(relative))
                if relative not in read_paths:
                    total += len(blob)
                    read_paths[relative] = identity
                result['file_sha256'] = sha(blob)
                if expected_files is not None and result['file_sha256'] != expected_files.get(relative):
                    raise SourceChanged('Evidence bytes differ from the pinned source inventory.')
                try:
                    text, result['encoding'] = decode_source(blob, relative, decoding)
                except SourceDecodeError as exc:
                    result['encoding'] = exc.encoding
                    raise PointerError(str(exc)) from None
                source_lines = lines(text)
                cache[relative] = source_lines, result['file_sha256'], result['encoding']
            if expected_files is not None and result['file_sha256'] != expected_files.get(relative):
                raise SourceChanged('Evidence bytes differ from the pinned source inventory.')
            if end > len(source_lines):
                raise PointerError('OUT_OF_RANGE')
            fragment = ''.join(source_lines[start - 1:end])
            encoded = fragment.encode('utf-8')
            if len(encoded) > MAX_FRAGMENT_BYTES:
                raise PointerError('LIMIT_EXCEEDED')
            result['fragment_sha256'] = sha(encoded)
            quote = pointer.get('quote', '')
            if quote and quote != fragment:
                raise PointerError('QUOTE_MISMATCH')
            result.update(status='RESOLVED', fragment_bytes=len(encoded))
        except PointerError as exc:
            result['status'] = str(exc)
        except (ValueError, TypeError):
            result['status'] = 'INVALID_POINTER'
        except OSError as exc:
            result['status'] = ('ACCESS_DENIED' if exc.errno in (errno.EACCES, errno.EPERM) else
                                'UNSAFE_PATH' if exc.errno in (errno.ELOOP, errno.ENOTDIR) else
                                'NOT_FOUND' if exc.errno == errno.ENOENT else 'READ_ERROR')
    return results
