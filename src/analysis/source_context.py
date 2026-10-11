# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Run-local structural navigation and exact, bounded source delivery.

Only pinned inventory entries are read. Symbol maps are navigation, never evidence.
No source imports, parser configuration, plugins or project commands are executed.
"""
from __future__ import annotations

import ast
from bisect import bisect_left
from collections import Counter
from functools import lru_cache
import copy
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import time

from src.analysis.evidence import (MAX_FILE_BYTES, PointerError, SourceChanged, _read_confined,
                                   canonical, lines, sha, source_catalog, valid_path)
from src.analysis.source_decoding import decode_source, normalize_source_decoding, SourceDecodeError, BOMS

AST_GREP_VERSION = '0.50.0'
MAX_FRAGMENT_LINES = 120
PARSER_REVISION = 1
LANGUAGES = {'.py': 'python', '.pyi': 'python', '.js': 'javascript', '.mjs': 'javascript',
             '.cjs': 'javascript', '.jsx': 'jsx', '.ts': 'typescript', '.mts': 'typescript',
             '.cts': 'typescript', '.tsx': 'tsx', '.go': 'go', '.c': 'c', '.h': 'header',
             '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp', '.hh': 'cpp',
             '.hxx': 'cpp', '.java': 'java', '.rs': 'rust', '.swift': 'swift'}


def source_context_settings(value=None):
    if value is None:
        value = {}
    if type(value) is not dict or set(value) - {'enabled', 'max_bytes'}:
        raise ValueError('source_context accepts enabled and max_bytes only.')
    enabled, maximum = value.get('enabled', True), value.get('max_bytes', 65536)
    if type(enabled) is not bool:
        raise ValueError('source_context.enabled must be a JSON boolean.')
    if type(maximum) is not int or maximum < 4096:
        raise ValueError('source_context.max_bytes must be an integer of at least 4096.')
    return {'enabled': enabled, 'max_bytes': maximum}


def serialize_source_context(value):
    """Match the JSON serialization used inside the actual model prompt."""
    return json.dumps(value, ensure_ascii=False)


def _size(value):
    return len(serialize_source_context(value).encode('utf-8'))


def _symbol(name, kind, parent, start, end):
    return dict(name=name, kind=kind, parent=parent, start_line=start, end_line=end)


def _python_symbols(text):
    root = ast.parse(text)
    # Python counts lone CR as a newline; evidence coordinates count LF only.
    physical_line, ast_lines = 1, [1]
    for newline in re.finditer(r'\r\n|\r|\n', text):
        physical_line += '\n' in newline.group()
        ast_lines.append(physical_line)
    result = []

    def visit(node, parent=None, parent_kind=None):
        name, kind = None, None
        if isinstance(node, ast.ClassDef):
            name, kind = node.name, 'class'
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            kind = ('async_' if isinstance(node, ast.AsyncFunctionDef) else '') + (
                'method' if parent_kind == 'class' else 'function')
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ', '.join(a.name + (' as ' + a.asname if a.asname else '') for a in node.names)
            name = ('.' * node.level + (node.module or '') + ': ' + names
                    if isinstance(node, ast.ImportFrom) else names)
            kind = 'import'
        if kind:
            start = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', [])])
            result.append(_symbol(name, kind, parent, ast_lines[start - 1], ast_lines[node.end_lineno - 1]))
            if kind != 'import':
                parent, parent_kind = (parent + '.' if parent else '') + name, kind
        for child in ast.iter_child_nodes(node):
            visit(child, parent, parent_kind)
    visit(root)
    return sorted(result, key=lambda s: (s['start_line'], -s['end_line']))


KINDS = {
    'import_statement': 'import', 'import_declaration': 'import', 'use_declaration': 'import',
    'preproc_include': 'import', 'export_statement': 'export',
    'function_declaration': 'function', 'function_definition': 'function', 'function_item': 'function',
    'function_signature_item': 'function', 'function_expression': 'function',
    'generator_function': 'function', 'generator_function_declaration': 'function',
    'method_definition': 'method', 'method_declaration': 'method',
    'method_signature': 'method', 'abstract_method_signature': 'method',
    'class_declaration': 'class', 'class': 'class', 'class_specifier': 'class',
    'interface_declaration': 'interface', 'type_alias_declaration': 'type', 'type_spec': 'type',
    'type_alias': 'type', 'type_definition': 'type', 'alias_declaration': 'type', 'type_item': 'type',
    'struct_specifier': 'struct', 'union_specifier': 'union', 'enum_specifier': 'enum',
    'namespace_definition': 'namespace', 'preproc_def': 'macro', 'preproc_function_def': 'macro',
    'record_declaration': 'record', 'enum_declaration': 'enum', 'constructor_declaration': 'constructor',
    'compact_constructor_declaration': 'constructor', 'annotation_type_declaration': 'interface',
    'mod_item': 'module', 'struct_item': 'struct', 'enum_item': 'enum', 'trait_item': 'trait',
    'impl_item': 'impl', 'protocol_declaration': 'protocol', 'protocol_function_declaration': 'function',
    'init_declaration': 'init', 'deinit_declaration': 'deinit', 'property_declaration': 'property',
    'protocol_property_declaration': 'property', 'typealias_declaration': 'type',
}


def _field_text(node, name):
    child = node.field(name)
    return child.text() if child else None


def _declarator(node):
    child = node.field('declarator')
    while child and child.field('declarator'):
        child = child.field('declarator')
    return child.text() if child else None



def _c_function_name(node):
    """The constructor nearest the name distinguishes functions from pointers."""
    nearest = None
    while node is not None:
        if node.kind() in ('function_declarator', 'pointer_declarator', 'reference_declarator', 'array_declarator'):
            nearest = node.kind()
        child = node.field('declarator')
        if child is None and node.kind() == 'parenthesized_declarator':
            child = next((c for c in node.children() if c.is_named()), None)
        if child is None:
            return node.text() if nearest == 'function_declarator' else None
        node = child
    return None


def _native_symbols(text, language):
    from ast_grep_py import SgRoot
    root = SgRoot(text, language).root()
    all_nodes, stack = [], [root]
    while stack:
        node = stack.pop()
        all_nodes.append(node)
        stack.extend(reversed(node.children()))
    errors = [n.range() for n in all_nodes if n.kind() == 'ERROR' or
              n is not root and n.range().start.index == n.range().end.index]
    error_starts, error_ends = [], []
    for error in sorted(errors, key=lambda r: r.start.index):
        error_starts.append(error.start.index)
        error_ends.append(max(error.end.index, error.start.index + 1, error_ends[-1] if error_ends else 0))
    class_names = {_field_text(n, 'name') for n in all_nodes if n.kind() in ('class_specifier', 'struct_specifier', 'union_specifier')}
    result = []

    def visit(node, parent=None, parent_kind=None):
        native, region = node.kind(), node.range()
        # Keep declarations outside erroneous spans; never advertise malformed ones.
        error_index = bisect_left(error_starts, region.end.index) - 1
        valid = error_index < 0 or error_ends[error_index] <= region.start.index
        kind = KINDS.get(native) if valid else None
        name = _field_text(node, 'name')
        additional_names = []
        if native == 'variable_declarator' and valid:
            value = node.field('value')
            if value and value.kind() in ('arrow_function', 'function_expression', 'generator_function'):
                kind = 'function'
        if language in ('c', 'cpp') and valid:
            if native in ('declaration', 'field_declaration'):
                names = [name for child in node.children() if child.is_named()
                         if (name := _c_function_name(child))]
                if names:
                    name, additional_names = names[0], names[1:]
                    kind = 'method' if parent_kind in ('class', 'struct', 'union') else 'prototype'
            elif native == 'function_definition':
                name = _c_function_name(node.field('declarator'))
        if language == 'swift' and native == 'property_declaration' and valid:
            names = [child.text() for child in node.children() if child.kind() == 'pattern']
            if names:
                name, additional_names = names[0], names[1:]
        if language == 'swift' and native == 'class_declaration' and kind:
            keywords = [c.kind() for c in node.children()]
            kind = next((k for k in ('actor', 'extension', 'struct', 'enum', 'class') if k in keywords), 'class')
        if kind == 'impl':
            name = (_field_text(node, 'trait') + ' for ' if node.field('trait') else '') + (
                _field_text(node, 'type') or '')
        if kind in ('import', 'export'):
            # Full syntax remains in snippets; this label only identifies navigation.
            name = (_field_text(node, 'source') or _field_text(node, 'path') or
                    ' '.join(c.text() for c in node.children() if c.is_named() and
                             c.kind() not in ('class_declaration', 'function_declaration', 'lexical_declaration',
                                              'interface_declaration', 'type_alias_declaration'))) or kind
            if kind == 'export' and name == 'export':
                declaration = next((c for c in node.children() if _field_text(c, 'name')), None)
                variable = node.find(kind='variable_declarator')
                name = (_field_text(declaration, 'name') if declaration else
                        _field_text(variable, 'name') if variable else 'default')
        if not name and kind:
            name = _declarator(node)
        if kind and name:
            symbol_parent = parent
            if language == 'cpp' and kind in ('function', 'prototype') and '::' in name:
                owner, member = name.rsplit('::', 1)
                if owner.split('::')[-1] in class_names:
                    kind, name = 'method', member
                    symbol_parent = (parent + '.' if parent else '') + owner.replace('::', '.')
            if language == 'go' and native == 'method_declaration':
                receiver = node.field('receiver')
                receiver_type = receiver.find(kind='type_identifier') if receiver else None
                symbol_parent = receiver_type.text() if receiver_type else parent
            if kind == 'function' and parent_kind in ('class', 'struct', 'actor', 'extension', 'trait', 'impl', 'protocol'):
                kind = 'method'
            end = region.end.line + (1 if region.end.column else 0)
            for symbol_name in [name, *additional_names]:
                result.append(_symbol(symbol_name, kind, symbol_parent, region.start.line + 1,
                                      max(region.start.line + 1, end)))
            if kind not in ('import', 'export', 'macro'):
                parent, parent_kind = (parent + '.' if parent else '') + name, kind
        for child in node.children():
            if child.is_named():
                visit(child, parent, parent_kind)
    visit(root)
    return result, len(errors)


@lru_cache(maxsize=1)
def _parser_version():
    try:
        return importlib.metadata.version('ast-grep-py')
    except importlib.metadata.PackageNotFoundError:
        return 'unavailable'


def parse_symbols(path, text):
    """Parse already decoded source; return records, parser and map completeness."""
    language = LANGUAGES.get(Path(path).suffix.lower())
    if language is None:
        return {'symbols': [], 'parser': None, 'map_status': 'unavailable', 'map_reason': 'unknown_language'}
    parser = 'python-ast:' + '.'.join(map(str, sys.version_info[:3])) if language == 'python' else 'ast-grep-py:' + _parser_version()
    try:
        if language == 'python':
            symbols, errors = _python_symbols(text), 0
        elif language == 'header':
            c, cpp = _native_symbols(text, 'c'), _native_symbols(text, 'cpp')
            language, (symbols, errors) = ('c', c) if c[1] <= cpp[1] else ('cpp', cpp)
        else:
            symbols, errors = _native_symbols(text, language)
    except (SyntaxError, ValueError, RecursionError):
        return dict(symbols=[], parser=parser, language=language, map_status='unavailable', map_reason='parse_error')
    except ImportError:
        return dict(symbols=[], parser=parser, language=language, map_status='unavailable', map_reason='parser_unavailable')
    return dict(symbols=symbols, parser=parser, language=language, map_status='partial' if errors else 'complete',
                map_reason='parse_error' if errors else None)


def _subtract(start, end, delivered):
    remaining = [(start, end)]
    for left, right in delivered:
        remaining = [(a, b) for lo, hi in remaining for a, b in
                     ((lo, min(hi, left - 1)), (max(lo, right + 1), hi)) if a <= b]
    return remaining


def _merge(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1 and end - merged[-1][0] + 1 <= MAX_FRAGMENT_LINES:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _pointers(value):
    if isinstance(value, dict):
        if all(k in value for k in ('source_id', 'path', 'start_line', 'end_line')):
            yield value
        for child in value.values():
            yield from _pointers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _pointers(child)


class SourceContextBuilder:
    def __init__(self, settings=None):
        self.settings = source_context_settings(settings)
        self.cache = {}

    def _read(self, entry, context, expected_files, metadata, snapshot, decoding):
        relative = entry['path']
        key = (snapshot, relative, expected_files[relative], canonical(decoding), PARSER_REVISION,
               AST_GREP_VERSION, _parser_version(), sys.version_info[:3])
        root = context.get('source_directory', context.get('repository'))
        cached = self.cache.get(key)
        if cached:
            _read_confined(root, relative, MAX_FILE_BYTES, expected=cached['identity'], pinned=metadata, read=False)
            return copy.deepcopy(cached)
        record = dict(status='unavailable', parser=None, map_status='unavailable', symbols=[], source_lines=[],
                      text_reason=None, map_reason=None, prepared_bytes=0, encoding=None, identity=None)
        try:
            blob, identity = _read_confined(root, relative, MAX_FILE_BYTES, pinned=metadata)
            if metadata is None and sha(blob) != expected_files[relative]:
                raise SourceChanged('Prepared source bytes differ from the pinned source inventory.')
            record['identity'] = identity
            text, record['encoding'] = decode_source(blob, relative, decoding)
            if '\x00' in text:
                record['text_reason'] = record['map_reason'] = 'binary_content'
            else:
                record.update(parse_symbols(relative, text))
                record.update(status='processed', source_lines=lines(text), prepared_bytes=len(text.encode('utf-8')))
        except SourceDecodeError as exc:
            binary = (b'\x00' in blob and not any(blob.startswith(bom) for bom, _ in BOMS) and
                      not (exc.encoding or '').startswith(('utf-16', 'utf-32')))
            record['text_reason'] = record['map_reason'] = 'binary_content' if binary else 'decoding_error'
        except PointerError as exc:
            record['text_reason'] = record['map_reason'] = ('file_size_limit' if str(exc) == 'LIMIT_EXCEEDED' else 'read_error')
        except OSError:
            record['text_reason'] = record['map_reason'] = 'read_error'
        if record['identity']:
            self.cache[key] = copy.deepcopy(record)
        return record

    def prepare(self, stage, context, expected_files=None, *, expected_metadata=None):
        started = time.monotonic()
        manifest = dict(stage=stage, enabled=self.settings['enabled'], files=[], prepared_bytes=0,
                        transmitted_bytes=0, duration_seconds=0)
        if (not self.settings['enabled'] or stage not in ('catalog', 'study', 'review', 'revise') or
                context.get('correction_prompt')):
            manifest['reason'] = 'disabled' if not self.settings['enabled'] else 'stage_without_sources'
            return None, manifest
        inventory = context.get('_inventory')
        if inventory is None:
            raise ValueError('Source context requires the pinned inventory.')
        entries = {e['path']: e for e in inventory['entries'] if e['type'] == 'file'}
        expected_files = expected_files if expected_files is not None else {p: e['sha256'] for p, e in entries.items()}
        if any(path not in expected_files or entries[path]['sha256'] != expected_files[path] for path in entries):
            raise SourceChanged('Source context inventory differs from the pinned file hashes.')
        catalog = source_catalog(context)
        snapshot = canonical([s['identity'] for s in catalog])
        decoding = normalize_source_decoding(context.get('source_decoding'))
        records = []
        for relative, entry in entries.items():
            if not valid_path(relative) or '.git' in relative.split('/'):
                raise SourceChanged('Unsafe file in pinned source context inventory.')
            source = max((s for s in catalog if s['root'] == '.' or relative.startswith(s['root'] + '/')),
                         key=lambda s: len(s['root']))
            path = relative if source['root'] == '.' else relative[len(source['root']) + 1:]
            records.append(dict(source_id=source['id'], path=path, relative=relative, entry=entry))
        records.sort(key=lambda r: (r['source_id'], r['path']))
        for record in records:
            record.update(self._read(record.pop('entry'), context, expected_files, expected_metadata, snapshot, decoding))
            record['delivered'] = []
            record['map_sent'] = 0
        payload = {'files': [], 'fragments': [], 'omissions': {}}
        maximum = self.settings['max_bytes']
        map_limit = maximum // 4
        # One entry per file per cycle; symbols retain source declaration order.
        active, map_paths = [], set()
        for record in records:
            item = {k: record[k] for k in ('source_id', 'path', 'parser', 'map_status')}
            item['symbols'] = record['symbols'][:1]
            if _size({'files': payload['files'] + [item]}) > map_limit:
                item['symbols'] = []
                if _size({'files': payload['files'] + [item]}) > map_limit:
                    continue
            payload['files'].append(item)
            record['map_sent'] = len(item['symbols'])
            active.append((record, item))
            map_paths.add((record['source_id'], record['path']))
        positions = [len(item['symbols']) for _, item in active]
        while any(pos < len(r['symbols']) for pos, (r, _) in zip(positions, active)):
            for index, (record, item) in enumerate(active):
                if positions[index] >= len(record['symbols']):
                    continue
                symbol = record['symbols'][positions[index]]
                positions[index] += 1
                item['symbols'].append(symbol)
                if _size({'files': payload['files']}) > map_limit:
                    item['symbols'].pop()
                else:
                    record['map_sent'] += 1

        for record, item in active:
            if record['map_sent'] < len(record['symbols']) and item['map_status'] == 'complete':
                item['map_status'] = 'partial'
        map_omissions = sum(r['map_sent'] < len(r['symbols']) or (r['source_id'], r['path']) not in map_paths
                            for r in records)
        reason_counts = Counter()
        omitted_files = omitted_ranges = fragment_bytes = 0
        envelope_bytes = _size(payload) - _size(payload['fragments']) - _size(payload['omissions'])

        def measure(record):
            nonlocal omitted_files, omitted_ranges, fragment_bytes
            old = record.get('_delivery_stats', (0, 0, 0, set()))
            missing = _subtract(1, len(record['source_lines']), record['delivered']) if record['source_lines'] else []
            fragments = [dict(source_id=record['source_id'], path=record['path'], start_line=start, end_line=end,
                              text=''.join(record['source_lines'][start - 1:end]))
                         for start, end in record['delivered']]
            reasons = {reason for reason in (record['map_reason'], record['text_reason']) if reason}
            if missing:
                reasons.add('catalog_map_only' if stage == 'catalog' else 'budget')
            if record['map_sent'] < len(record['symbols']) or (record['source_id'], record['path']) not in map_paths:
                reasons.add('budget')
            new = (int(bool(missing or record['text_reason'])), len(missing),
                   sum(_size(f) + 2 for f in fragments), reasons)
            omitted_files += new[0] - old[0]
            omitted_ranges += new[1] - old[1]
            fragment_bytes += new[2] - old[2]
            reason_counts.subtract(old[3])
            reason_counts.update(new[3])
            record['_delivery_stats'], record['_fragments'] = new, fragments
            payload['omissions'] = dict(files=omitted_files, ranges=omitted_ranges, maps=map_omissions,
                                        reasons={k: v for k, v in sorted(reason_counts.items()) if v > 0},
                                        incomplete=bool(omitted_files or map_omissions or +reason_counts))

        def payload_size():
            # A nonempty JSON list costs sum(item bytes + comma-space); the
            # opening and closing brackets replace the final comma-space.
            return envelope_bytes + max(2, fragment_bytes) + _size(payload['omissions'])

        for record in records:
            measure(record)
        if not records:
            payload['omissions'] = dict(files=0, ranges=0, maps=0, reasons={}, incomplete=False)
        turn_bytes = max(128, (maximum - payload_size()) // max(1, len(records)))

        def add(record, start, end, *, fair=False):
            limit = min(maximum, payload_size() + turn_bytes) if fair else maximum
            for left, right in _subtract(start, min(end, len(record['source_lines'])), record['delivered']):
                right = min(right, left + MAX_FRAGMENT_LINES - 1)
                old = record['delivered']
                low, high, best = left, right, None
                while low <= high:
                    middle = (low + high) // 2
                    record['delivered'] = _merge(old + [[left, middle]])
                    measure(record)
                    if payload_size() <= limit:
                        best, low = record['delivered'], middle + 1
                    else:
                        high = middle - 1
                record['delivered'] = best if best is not None else old
                measure(record)

        if stage != 'catalog':
            if stage in ('review', 'revise') or context.get('prompt_variant') == 'revise':
                by_path = {(r['source_id'], r['path']): r for r in records}
                for pointer in _pointers({k: v for k, v in context.items() if k not in ('_inventory', 'source_context')}):
                    record = by_path.get((pointer['source_id'], pointer['path']))
                    start, end = pointer['start_line'], pointer['end_line']
                    if record and type(start) is int and type(end) is int and 1 <= start <= end:
                        for left in range(start, min(end, len(record['source_lines'])) + 1, MAX_FRAGMENT_LINES):
                            add(record, left, min(end, left + MAX_FRAGMENT_LINES - 1))
            candidates = []
            for record in records:
                symbols = sorted(record['symbols'], key=lambda s: (s['kind'] != 'import', s['start_line']))
                windows = [(s['start_line'], min(s['end_line'], s['start_line'] + MAX_FRAGMENT_LINES - 1)) for s in symbols]
                windows.extend((start, min(start + MAX_FRAGMENT_LINES - 1, len(record['source_lines'])))
                               for start in range(1, len(record['source_lines']) + 1, MAX_FRAGMENT_LINES))
                candidates.append(iter(windows))
            def cycle(queues, fair):
                active_indices = list(range(len(records)))
                while active_indices:
                    next_indices = []
                    for index in active_indices:
                        candidate = next(queues[index], None)
                        if candidate is not None:
                            add(records[index], *candidate, fair=fair and len(active_indices) > 1)
                            next_indices.append(index)
                    active_indices = next_indices
            cycle(candidates, True)
            # Reconsider residual windows without the per-turn fair share once
            # every file has had its turn; small peers can leave capacity free.
            remaining = []
            for record in records:
                windows = [(start, min(end, start + MAX_FRAGMENT_LINES - 1))
                           for left, end in _subtract(1, len(record['source_lines']), record['delivered'])
                           for start in range(left, end + 1, MAX_FRAGMENT_LINES)]
                remaining.append(iter(windows))
            cycle(remaining, False)
        payload['fragments'] = [fragment for record in records for fragment in record['_fragments']]
        for record in records:
            item = {k: record[k] for k in ('source_id', 'path', 'relative', 'status', 'parser', 'map_status',
                                           'map_reason', 'text_reason', 'encoding', 'prepared_bytes')}
            item.update(total_lines=len(record['source_lines']) if record['status'] == 'processed' else None,
                        map_omission_reason=('budget' if record['map_sent'] < len(record['symbols']) or
                                             (record['source_id'], record['path']) not in map_paths else record['map_reason']),
                        symbols=len(record['symbols']),
                        symbols_transmitted=record['map_sent'], transmitted_ranges=record['delivered'],
                        omitted_ranges=_subtract(1, len(record['source_lines']), record['delivered']) if record['source_lines'] else [],
                        transmitted_text_bytes=sum(len(''.join(record['source_lines'][a - 1:b]).encode('utf-8'))
                                                   for a, b in record['delivered']))
            if item['omitted_ranges'] and not item['text_reason']:
                item['text_reason'] = 'catalog_map_only' if stage == 'catalog' else 'budget'
            manifest['files'].append(item)
        manifest.update(prepared_bytes=sum(r['prepared_bytes'] for r in records), transmitted_bytes=_size(payload),
                        map_bytes=_size({'files': payload['files']}), duration_seconds=time.monotonic() - started)
        assert manifest['transmitted_bytes'] <= maximum
        return payload, manifest
