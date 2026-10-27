# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Search saved declarations and read original pinned source bytes."""
from __future__ import annotations

import base64
import copy
import errno
import json
import os
from pathlib import Path
import stat
import uuid

from src.messages import message as ui_message
from src.analysis.evidence import (PointerError, SourceChanged, _read_confined, canonical,
                                   lines, open_source_directory, sha, source_catalog, valid_path)
from src.analysis.source_decoding import SourceDecodeError, decode_source, normalize_source_decoding

SERVER_NAME = 'explain_this_pls'
MAX_RESPONSE_BYTES = 65536
MAX_SEARCH_LIMIT = 1000


def _unpack_files(index):
    for row in index['files']:
        file = dict(zip(index['file_columns'], row, strict=True))
        for column, table in index['file_value_tables'].items():
            file[column] = table[file[column]]
        yield file


def prepare_manifest(context, index, packed_index, map_files, *, attempt_id, expected_metadata=None):
    """Bind existing maps to this attempt; do not parse or copy source text."""
    catalog = source_catalog(context)
    inventory = copy.deepcopy(context['_inventory'])
    entries = {entry['path']: entry for entry in inventory['entries'] if entry['type'] == 'file'}
    references = {(file['source_id'], file['path']): file['symbols_ref']
                  for file in _unpack_files(packed_index)}
    files, seen = [], set()
    for file in index['files']:
        source = next(source for source in catalog if source['id'] == file['source_id'])
        relative = file['path'] if source['root'] == '.' else source['root'] + '/' + file['path']
        owner = max((source for source in catalog if source['root'] == '.' or
                     relative.startswith(source['root'] + '/')), key=lambda source: len(source['root']))
        if (not valid_path(relative) or relative not in entries or relative in seen or
                owner['id'] != file['source_id']):
            raise ValueError(ui_message('diagnostic.service.mcp_index_does_not_match_the_pinned_inventory'))
        seen.add(relative)
        record = {key: copy.deepcopy(value) for key, value in file.items() if key != 'symbols'}
        record.update(relative=relative, sha256=entries[relative]['sha256'],
                      symbol_count=len(file['symbols']), symbols_sha256=sha(canonical(file['symbols'])))
        name = references[(file['source_id'], file['path'])]
        record['map'] = copy.deepcopy(map_files[name]) if name else None
        if bool(name) != bool(file['symbols']):
            raise ValueError(ui_message('diagnostic.service.mcp_symbol_map_reference_is_missing'))
        files.append(record)
    if seen != set(entries):
        raise ValueError(ui_message('diagnostic.service.mcp_index_is_incomplete'))
    return dict(attempt_id=attempt_id, source_root=context.get('source_directory', context.get('repository')),
                inventory=inventory, catalog=catalog, source_decoding=normalize_source_decoding(context.get('source_decoding')),
                expected_metadata=expected_metadata, files=files,
                snapshot_sha256=sha(canonical([inventory, catalog, normalize_source_decoding(context.get('source_decoding'))])))


class SourceService:
    def __init__(self, manifest_path, expected_sha256, *, max_response_bytes=MAX_RESPONSE_BYTES):
        self.manifest_path = Path(manifest_path)
        if not self.manifest_path.is_absolute() or '..' in self.manifest_path.parts:
            raise ValueError(ui_message('diagnostic.service.mcp_manifest_path_must_be_absolute'))
        self.manifest_sha256 = expected_sha256
        self.max_response_bytes = max_response_bytes
        self.receipts_path = self.manifest_path.parent / 'served.jsonl'
        self.artifacts = []
        self.manifest = self._artifact(self.manifest_path, expected_sha256)
        self.attempt_id = self.manifest['attempt_id']
        self.root = self.manifest['source_root']
        source_root, attempt_root = Path(self.root), self.manifest_path.parent.parent
        if not source_root.is_absolute() or '..' in source_root.parts:
            raise ValueError(ui_message('diagnostic.service.mcp_source_root_must_be_absolute'))
        if source_root == attempt_root or source_root in attempt_root.parents or attempt_root in source_root.parents:
            raise ValueError(ui_message('diagnostic.service.mcp_private_attempt_and_source_directory_must_be'))
        self.catalog = {source['id']: source for source in self.manifest['catalog']}
        self.metadata = self.manifest['expected_metadata']
        if self.metadata is not None:
            self.metadata = {path: tuple(value) for path, value in self.metadata.items()}
        self.decoding = normalize_source_decoding(self.manifest['source_decoding'])
        inventory = self.manifest['inventory']
        if sha(canonical([inventory, self.manifest['catalog'], self.decoding])) != self.manifest['snapshot_sha256']:
            raise ValueError(ui_message('diagnostic.service.mcp_source_snapshot_identity_is_invalid'))
        entries = {entry['path']: entry for entry in inventory['entries'] if entry['type'] == 'file'}
        self.files, self.symbols = {}, []
        for file in self.manifest['files']:
            relative = file['relative']
            source = self.catalog[file['source_id']]
            expected_relative = file['path'] if source['root'] == '.' else source['root'] + '/' + file['path']
            owners = [source for source in self.catalog.values() if source['root'] == '.' or
                      relative.startswith(source['root'] + '/')]
            if (not valid_path(relative) or relative != expected_relative or
                    max(owners, key=lambda source: len(source['root']))['id'] != file['source_id'] or
                    relative not in entries or file['sha256'] != entries[relative]['sha256']):
                raise ValueError(ui_message('diagnostic.service.mcp_file_identity_does_not_match_the_inventory'))
            key = file['source_id'], file['path']
            if key in self.files:
                raise ValueError(ui_message('diagnostic.service.duplicate_mcp_source_file'))
            self.files[key] = file
            symbols = []
            if file['map'] is not None:
                descriptor = file['map']
                map_path = Path(descriptor['path'])
                if self.manifest_path.parent.parent not in map_path.parents:
                    raise ValueError(ui_message('diagnostic.service.mcp_symbol_map_is_outside_the_private_attempt'))
                saved = self._artifact(map_path, descriptor['sha256'], descriptor['bytes'])
                if (saved['source_id'], saved['path']) != key:
                    raise ValueError(ui_message('diagnostic.service.mcp_symbol_map_belongs_to_a_different_source'))
                for row in saved['symbols']:
                    symbol = dict(zip(saved['symbol_columns'], row, strict=True))
                    for column, table in saved['symbol_value_tables'].items():
                        symbol[column] = table[symbol[column]]
                    symbols.append(symbol)
            if len(symbols) != file['symbol_count'] or sha(canonical(symbols)) != file['symbols_sha256']:
                raise ValueError(ui_message('diagnostic.service.mcp_symbol_map_differs_from_the_prepared_declarations'))
            self.symbols.extend(dict(source_id=key[0], path=key[1], **symbol, map_status=file['map_status'])
                                for symbol in symbols)
        if {file['relative'] for file in self.files.values()} != set(entries):
            raise ValueError(ui_message('diagnostic.service.mcp_files_do_not_match_the_pinned_inventory'))
        self.symbols.sort(key=lambda symbol: (symbol['source_id'], symbol['path'], symbol['start_line'],
                                             symbol['end_line'], symbol['name'], symbol['kind'], symbol['parent'] or ''))

    def _artifact(self, path, expected_sha256, size=None):
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError(ui_message('diagnostic.service.mcp_artifact_path_must_be_absolute'))
        blob, identity = _read_confined(str(path.parent), path.name, size)
        if any(stat.S_IMODE(component[2]) & 0o077 for component in identity[-2:]):
            raise ValueError(ui_message('diagnostic.service.mcp_artifact_and_parent_directory_must_be_private'))
        if sha(blob) != expected_sha256 or size is not None and len(blob) != size:
            raise ValueError(ui_message('diagnostic.service.mcp_private_artifact_integrity_check_failed'))
        self.artifacts.append((str(path.parent), path.name, identity))
        return json.loads(blob)

    def _check_artifacts(self):
        for root, relative, identity in self.artifacts:
            _read_confined(root, relative, expected=identity, read=False)

    def _meta(self):
        return dict(manifest_sha256=self.manifest_sha256, attempt_id=self.attempt_id,
                    request_id=uuid.uuid4().hex, state='SERVED', inspection='INSPECTED_UNVERIFIED')

    def _receipt(self, record):
        parent = open_source_directory(str(self.manifest_path.parent))
        try:
            fd = os.open('served.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, 'ab') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
                    raise ValueError(ui_message('diagnostic.service.mcp_receipt_file_must_be_private_and_must'))
                stream.write(canonical(record) + b'\n')
        finally:
            os.close(parent)

    def call_tool(self, name, arguments):
        """Return and receipt the exact JSON body exposed in MCP text content."""
        meta = self._meta()
        try:
            self._check_artifacts()
            if name == 'search_symbols':
                result = self._search_symbols(meta, **arguments)
            elif name == 'read_source':
                result = self._read_source(meta, **arguments)
            else:
                raise PointerError('UNKNOWN_TOOL')
        except SourceChanged:
            result = {'error': {'code': 'SOURCE_CHANGED', 'message': ui_message(
                'diagnostic.service.pinned_source_or_private_artifact_changed',
            )}}
        except SourceDecodeError as exc:
            result = {'error': {'code': str(exc), 'message': ui_message(
                'diagnostic.service.source_decoding_failed',
            ), 'encoding': exc.encoding}}
        except PointerError as exc:
            result = {'error': {'code': str(exc), 'message': ui_message('diagnostic.service.source_tool_request_cannot_be_served')}}
        except (TypeError, ValueError):
            result = {'error': {'code': 'INVALID_ARGUMENT', 'message': ui_message(
                'diagnostic.service.invalid_source_tool_arguments',
            )}}
        except OSError as exc:
            code = ('UNSAFE_PATH' if exc.errno in (errno.ELOOP, errno.ENOTDIR) else
                    'NOT_FOUND' if exc.errno == errno.ENOENT else 'READ_ERROR')
            result = {'error': {'code': code, 'message': ui_message('diagnostic.service.source_read_failed')}}
        result['_meta'] = meta
        if len(canonical(result)) > self.max_response_bytes:
            result = {'error': {'code': 'RESPONSE_TOO_LARGE', 'message': ui_message(
                'diagnostic.service.one_result_exceeds_the_response_limit',
            )}, '_meta': meta}
        self._receipt(dict(state='SERVED', inspection='INSPECTED_UNVERIFIED',
                      attempt_id=self.attempt_id, request_id=meta['request_id'], tool=name,
                      arguments=arguments, result_sha256=sha(canonical(result)),
                      result_summary={key: value for key, value in result.items()
                                      if key not in ('text', '_meta', 'results')},
                      text_bytes=len(result.get('text', '').encode('utf-8'))))
        return result

    def search_symbols(self, query, source_id=None, path=None, kind=None, limit=100, cursor=None):
        return self.call_tool('search_symbols', dict(query=query, source_id=source_id, path=path,
                                                   kind=kind, limit=limit, cursor=cursor))

    def read_source(self, source_id, path, start_line, end_line):
        return self.call_tool('read_source', dict(source_id=source_id, path=path,
                                                start_line=start_line, end_line=end_line))

    def _search_symbols(self, meta, query, source_id=None, path=None, kind=None, limit=100, cursor=None):
        if (type(query) is not str or not query or type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT or
                source_id is not None and type(source_id) is not str or
                kind is not None and type(kind) is not str or
                path is not None and not valid_path(path) or cursor is not None and type(cursor) is not str):
            raise PointerError('INVALID_ARGUMENT')
        signature = sha(canonical([self.manifest_sha256, query, source_id, path, kind]))
        offset = 0
        if cursor is not None:
            try:
                token = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')))
                if token['query'] != signature or type(token['offset']) is not int or token['offset'] < 0:
                    raise ValueError()
                offset = token['offset']
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise PointerError('INVALID_CURSOR') from None
        matches = [symbol for symbol in self.symbols if query in symbol['name'] and
                   (source_id is None or symbol['source_id'] == source_id) and
                   (path is None or symbol['path'] == path or symbol['path'].startswith(path + '/')) and
                   (kind is None or symbol['kind'] == kind)]
        # Exact names precede partial matches; all ties keep deterministic source order.
        matches.sort(key=lambda symbol: symbol['name'] != query)
        if offset > len(matches):
            raise PointerError('INVALID_CURSOR')

        def response(count):
            next_offset = offset + count
            more = next_offset < len(matches)
            token = base64.urlsafe_b64encode(canonical(dict(query=signature, offset=next_offset))).decode('ascii') if more else None
            return dict(results=matches[offset:next_offset], has_more=more, cursor=token, _meta=meta)
        count = min(limit, len(matches) - offset)
        result = response(count)
        while count and len(canonical(result)) > self.max_response_bytes:
            count -= 1
            result = response(count)
        if not count and offset < len(matches):
            raise PointerError('RESULT_TOO_LARGE')
        return result

    def _read_source(self, meta, source_id, path, start_line, end_line):
        if (type(source_id) is not str or not valid_path(path) or type(start_line) is not int or
                type(end_line) is not int or start_line < 1 or end_line < start_line):
            raise PointerError('INVALID_ARGUMENT')
        if source_id not in self.catalog:
            raise PointerError('UNKNOWN_SOURCE')
        file = self.files.get((source_id, path))
        if file is None:
            raise PointerError('NOT_IN_INVENTORY')
        blob, _ = _read_confined(self.root, file['relative'], pinned=self.metadata)
        if sha(blob) != file['sha256']:
            raise SourceChanged(ui_message('diagnostic.service.original_source_bytes_differ_from_the_pinned_hash'))
        text, encoding = decode_source(blob, file['relative'], self.decoding)
        if '\x00' in text:
            raise PointerError('BINARY_CONTENT')
        source_lines = lines(text)
        total = len(source_lines)
        if not total and start_line == end_line == 1:
            return dict(source_id=source_id, path=path, start_line=1, end_line=0, total_lines=0,
                        encoding=encoding, sha256=file['sha256'], text='', complete=True, has_more=False,
                        next_start_line=None, _meta=meta)
        if end_line > total:
            raise PointerError('OUT_OF_RANGE')

        def response(end, fragment):
            more = end < end_line
            return dict(source_id=source_id, path=path, start_line=start_line, end_line=end,
                        total_lines=total, encoding=encoding, sha256=file['sha256'], text=fragment,
                        complete=not more, has_more=more, next_start_line=end + 1 if more else None, _meta=meta)
        fragments, end = [], start_line - 1
        base_size = len(canonical(response(end, '')))
        content_bytes = 0
        for number in range(start_line, end_line + 1):
            line = source_lines[number - 1]
            line_bytes = len(json.dumps(line, ensure_ascii=False).encode('utf-8')) - 2
            if base_size + content_bytes + line_bytes + 32 > self.max_response_bytes:
                if not fragments:
                    raise PointerError('LINE_TOO_LARGE')
                break
            fragments.append(line)
            content_bytes += line_bytes
            end = number
        result = response(end, ''.join(fragments))
        if len(canonical(result)) > self.max_response_bytes:
            raise PointerError('RESPONSE_TOO_LARGE')
        return result
