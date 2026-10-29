# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Search saved declarations and read original pinned source bytes."""
from __future__ import annotations

import base64
from bisect import bisect_right
import copy
import errno
import hashlib
import hmac
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


def symbol_identifier(snapshot, source_id, path, ordinal, symbol):
    return sha(canonical([snapshot, source_id, path, ordinal, symbol]))


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
        self.cursor_key = os.urandom(32)
        inventory = self.manifest['inventory']
        if sha(canonical([inventory, self.manifest['catalog'], self.decoding])) != self.manifest['snapshot_sha256']:
            raise ValueError(ui_message('diagnostic.service.mcp_source_snapshot_identity_is_invalid'))
        entries = {entry['path']: entry for entry in inventory['entries'] if entry['type'] == 'file'}
        self.files, self.symbols, self.symbols_by_id = {}, [], {}
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
            for ordinal, symbol in enumerate(symbols):
                identifier = symbol_identifier(self.manifest['snapshot_sha256'], *key, ordinal, symbol)
                record = dict(source_id=key[0], path=key[1], **symbol,
                              map_status=file['map_status'], symbol_id=identifier)
                self.symbols.append(record)
                self.symbols_by_id[identifier] = record
        if {file['relative'] for file in self.files.values()} != set(entries):
            raise ValueError(ui_message('diagnostic.service.mcp_files_do_not_match_the_pinned_inventory'))
        self.symbols.sort(key=lambda symbol: (symbol['source_id'], symbol['path'],
            *(symbol[key] if type(symbol[key]) is int else -1 for key in ('start_line', 'end_line')),
            symbol['name'], symbol['kind'], symbol['parent'] or ''))
        self.file_list = [self.files[key] for key in sorted(self.files)]

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
            elif name == 'search_text':
                result = self._search_text(meta, **arguments)
            elif name == 'list_files':
                result = self._list_files(meta, **arguments)
            elif name == 'read_source':
                result = self._read_source(meta, **arguments)
            elif name == 'read_symbol':
                result = self._read_symbol(meta, **arguments)
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

    def search_text(self, query, source_id=None, path=None, case_sensitive=True, limit=100, cursor=None):
        return self.call_tool('search_text', dict(query=query, source_id=source_id, path=path,
            case_sensitive=case_sensitive, limit=limit, cursor=cursor))

    def list_files(self, source_id=None, path=None, limit=100, cursor=None):
        return self.call_tool('list_files', dict(source_id=source_id, path=path, limit=limit, cursor=cursor))

    def read_symbol(self, symbol_id, cursor=None):
        return self.call_tool('read_symbol', dict(symbol_id=symbol_id, cursor=cursor))

    def _scope(self, tool, parameters):
        return sha(canonical([self.manifest_sha256, self.manifest['snapshot_sha256'], tool, parameters]))

    def _encode_cursor(self, scope, position):
        token = dict(scope=scope, position=position)
        token['mac'] = hmac.new(self.cursor_key, canonical(token), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(canonical(token)).decode('ascii')

    def _decode_cursor(self, cursor, scope, default):
        if cursor is None:
            return default
        try:
            if type(cursor) is not str or len(cursor) > 4096:
                raise ValueError()
            token = json.loads(base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True))
            if type(token) is not dict or set(token) != {'scope', 'position', 'mac'}:
                raise ValueError()
            mac = token.pop('mac')
            if (token['scope'] != scope or type(mac) is not str or
                    not hmac.compare_digest(mac, hmac.new(self.cursor_key, canonical(token), hashlib.sha256).hexdigest())):
                raise ValueError()
            return token['position']
        except (ValueError, TypeError, UnicodeError):
            raise PointerError('INVALID_CURSOR') from None

    def _filters(self, source_id, path, limit):
        if (type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT or
                source_id is not None and type(source_id) is not str or
                path is not None and not valid_path(path)):
            raise PointerError('INVALID_ARGUMENT')
        if source_id is not None and source_id not in self.catalog:
            raise PointerError('UNKNOWN_SOURCE')

    @staticmethod
    def _selected(file, source_id, path):
        return ((source_id is None or file['source_id'] == source_id) and
                (path is None or file['path'] == path or file['path'].startswith(path + '/')))

    def _page(self, meta, rows, scope, limit, cursor):
        offset = self._decode_cursor(cursor, scope, 0)
        if type(offset) is not int or not 0 <= offset <= len(rows):
            raise PointerError('INVALID_CURSOR')
        def response(count):
            following = offset + count
            more = following < len(rows)
            return dict(results=rows[offset:following], has_more=more,
                        cursor=self._encode_cursor(scope, following) if more else None, _meta=meta)
        count = min(limit, len(rows) - offset)
        result = response(count)
        if len(canonical(result)) > self.max_response_bytes:
            low, high = 0, count - 1
            while low < high:
                middle = (low + high + 1) // 2
                if len(canonical(response(middle))) <= self.max_response_bytes:
                    low = middle
                else:
                    high = middle - 1
            count = low
            result = response(count)
        if not count and offset < len(rows):
            raise PointerError('RESULT_TOO_LARGE')
        return result

    def _list_files(self, meta, source_id=None, path=None, limit=100, cursor=None):
        self._filters(source_id, path, limit)
        columns = ('source_id', 'path', 'language', 'encoding', 'total_lines',
                   'map_status', 'map_reason', 'symbol_count')
        rows = [{key: file[key] for key in columns} for file in self.file_list
                if self._selected(file, source_id, path)]
        return self._page(meta, rows, self._scope('list_files', [source_id, path, limit]), limit, cursor)

    def _search_symbols(self, meta, query, source_id=None, path=None, kind=None, limit=100, cursor=None):
        self._filters(source_id, path, limit)
        if type(query) is not str or not query or kind is not None and type(kind) is not str:
            raise PointerError('INVALID_ARGUMENT')
        matches = [symbol for symbol in self.symbols if query in symbol['name'] and
                   self._selected(symbol, source_id, path) and
                   (kind is None or symbol['kind'] == kind)]
        # Exact names precede partial matches; all ties keep deterministic source order.
        matches.sort(key=lambda symbol: symbol['name'] != query)
        return self._page(meta, matches, self._scope('search_symbols', [query, source_id, path, kind, limit]),
                          limit, cursor)

    def _source_text(self, file):
        blob, _ = _read_confined(self.root, file['relative'], pinned=self.metadata)
        if sha(blob) != file['sha256']:
            raise SourceChanged(ui_message('diagnostic.service.original_source_bytes_differ_from_the_pinned_hash'))
        text, encoding = decode_source(blob, file['relative'], self.decoding)
        if '\x00' in text:
            raise PointerError('BINARY_CONTENT')
        return text, encoding

    def _text_events(self, files, position, query, case_sensitive):
        first_file, offset = position
        for number in range(first_file, len(files)):
            file = files[number]
            locator = dict(source_id=file['source_id'], path=file['path'])
            try:
                text, encoding = self._source_text(file)
            except SourceDecodeError as exc:
                yield 'errors', dict(**locator, code=str(exc), encoding=exc.encoding), [number + 1, 0]
                continue
            except (PointerError, OSError) as exc:
                code = (str(exc) if isinstance(exc, PointerError) else
                        'UNSAFE_PATH' if exc.errno in (errno.ELOOP, errno.ENOTDIR) else
                        'NOT_FOUND' if exc.errno == errno.ENOENT else 'READ_ERROR')
                yield 'errors', dict(**locator, code=code), [number + 1, 0]
                continue
            text = text.replace('\r\n', '\n')
            # Per-character folding preserves a map to original character coordinates.
            # Unicode casefold can expand characters (e.g. ß -> ss).
            if case_sensitive:
                haystack, positions = text, None
            else:
                folded, positions = [], []
                for index, character in enumerate(text):
                    value = character.casefold()
                    folded.append(value)
                    positions.extend([index] * len(value))
                haystack = ''.join(folded)
            starts = [0] + [index + 1 for index, char in enumerate(text) if char == '\n']
            file_offset = offset if number == first_file else 0
            at = file_offset if case_sensitive else bisect_right(positions, file_offset - 1)
            while (found := haystack.find(query, at)) >= 0:
                start = found if positions is None else positions[found]
                end = found + len(query) if positions is None else positions[found + len(query) - 1] + 1
                line, last_line = bisect_right(starts, start), bisect_right(starts, end)
                snippet_start, snippet_end = max(0, start - 60), min(len(text), start + 100)
                yield 'results', dict(**locator, line=line, column=start - starts[line - 1] + 1,
                    end_line=last_line, end_column=end - starts[last_line - 1] + 1,
                    snippet=text[snippet_start:snippet_end],
                    snippet_truncated=snippet_start > 0 or snippet_end < len(text), encoding=encoding), [number, start + 1]
                at = start + 1 if positions is None else bisect_right(positions, start)

    def _search_text(self, meta, query, source_id=None, path=None, case_sensitive=True, limit=100, cursor=None):
        self._filters(source_id, path, limit)
        if type(query) is not str or not query or type(case_sensitive) is not bool:
            raise PointerError('INVALID_ARGUMENT')
        scope = self._scope('search_text', [query, source_id, path, case_sensitive, limit])
        position = self._decode_cursor(cursor, scope, [0, 0, 0])
        files = [file for file in self.file_list if self._selected(file, source_id, path)]
        if (type(position) is not list or len(position) != 3 or
                any(type(value) is not int or value < 0 for value in position) or position[0] > len(files)):
            raise PointerError('INVALID_CURSOR')
        results, errors = [], []
        following, errors_seen = position[:2], position[2]
        def response(more):
            return dict(results=results, errors=errors, incomplete=bool(errors_seen),
                        complete=not more and not errors_seen, has_more=more,
                        cursor=self._encode_cursor(scope, [*following, errors_seen]) if more else None, _meta=meta)
        needle = query.replace('\r\n', '\n')
        if not case_sensitive:
            needle = needle.casefold()
        for kind, row, next_position in self._text_events(files, following, needle, case_sensitive):
            if len(results) + len(errors) == limit:
                return response(True)
            target = errors if kind == 'errors' else results
            target.append(row)
            old_following, following = following, next_position
            errors_seen += kind == 'errors'
            if len(canonical(response(True))) > self.max_response_bytes:
                target.pop()
                following = old_following
                errors_seen -= kind == 'errors'
                if not results and not errors:
                    raise PointerError('RESULT_TOO_LARGE')
                return response(True)
        return response(False)

    def _read_symbol(self, meta, symbol_id, cursor=None):
        if type(symbol_id) is not str:
            raise PointerError('INVALID_ARGUMENT')
        symbol = self.symbols_by_id.get(symbol_id)
        if symbol is None:
            raise PointerError('SYMBOL_NOT_FOUND')
        start, end = symbol['start_line'], symbol['end_line']
        total = self.files[symbol['source_id'], symbol['path']]['total_lines']
        if (symbol['map_status'] not in ('complete', 'partial') or
                any(type(value) is not int for value in (start, end, total)) or not 1 <= start <= end <= total):
            raise PointerError('SYMBOL_RANGE_UNAVAILABLE')
        scope = self._scope('read_symbol', [symbol_id])
        following = self._decode_cursor(cursor, scope, start)
        if type(following) is not int or not start <= following <= end:
            raise PointerError('INVALID_CURSOR')
        return self._read_range(meta, symbol['source_id'], symbol['path'], following, end, symbol=symbol, scope=scope)

    def _read_source(self, meta, source_id, path, start_line, end_line):
        return self._read_range(meta, source_id, path, start_line, end_line)

    def _read_range(self, meta, source_id, path, start_line, end_line, *, symbol=None, scope=None):
        if (type(source_id) is not str or not valid_path(path) or type(start_line) is not int or
                type(end_line) is not int or start_line < 1 or end_line < start_line):
            raise PointerError('INVALID_ARGUMENT')
        if source_id not in self.catalog:
            raise PointerError('UNKNOWN_SOURCE')
        file = self.files.get((source_id, path))
        if file is None:
            raise PointerError('NOT_IN_INVENTORY')
        text, encoding = self._source_text(file)
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
            result = dict(source_id=source_id, path=path, start_line=start_line, end_line=end,
                        total_lines=total, encoding=encoding, sha256=file['sha256'], text=fragment,
                        complete=not more, has_more=more, next_start_line=end + 1 if more else None, _meta=meta)
            if symbol is not None:
                result.update(symbol_id=symbol['symbol_id'], declaration_start_line=symbol['start_line'],
                              declaration_end_line=symbol['end_line'],
                              cursor=self._encode_cursor(scope, end + 1) if more else None)
            return result
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
