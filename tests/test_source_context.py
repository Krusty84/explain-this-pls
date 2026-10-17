# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import codecs
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.analysis.evidence import SourceChanged, lines, sha, stamp
from src.analysis.source_context import (SourceContextBuilder, parse_symbols,
                                         serialize_source_context, source_context_settings)
from src.runtime.errors import AuditError

FIXTURES = Path(__file__).parent / 'fixtures' / 'source_context'
# Independent expected names, kinds, parents and inclusive physical line ranges.
EXPECTED = {
    'py': [('os', 'import', None, 1, 1), ('Box', 'class', None, 3, 7),
           ('run', 'async_method', 'Box', 4, 7), ('inner', 'function', 'Box.run', 5, 6), ('top', 'function', None, 8, 9)],
    'js': [("'other'", 'import', None, 1, 1), ('arrow', 'function', None, 2, 2),
           ('nested', 'function', 'run', 4, 4), ('method', 'method', 'Box', 7, 7)],
    'ts': [('Runner', 'interface', None, 2, 4), ('run', 'method', 'Runner', 3, 3),
           ('Name', 'type', None, 5, 5), ('arrow', 'function', None, 6, 6)],
    'jsx': [("'react'", 'import', None, 1, 1), ('Card', 'function', None, 2, 2), ('render', 'method', 'Box', 4, 4)],
    'tsx': [('Props', 'interface', None, 2, 2), ('Card', 'function', None, 3, 3), ('App', 'function', None, 4, 6)],
    'go': [('"fmt"', 'import', None, 2, 2), ('Box', 'type', None, 3, 3),
           ('run', 'function', None, 4, 6), ('Method', 'method', 'Box', 7, 7)],
    'c': [('<stdio.h>', 'import', None, 1, 1), ('PLUS', 'macro', None, 2, 3),
          ('Count', 'type', None, 4, 4), ('Box', 'struct', None, 5, 5),
          ('run', 'prototype', None, 6, 8), ('run', 'function', None, 9, 9)],
    'cpp': [('<string>', 'import', None, 1, 1), ('VALUE', 'macro', None, 2, 2),
            ('sample', 'namespace', None, 3, 12), ('Box', 'class', 'sample', 4, 10),
            ('Box', 'method', 'sample.Box', 6, 6), ('method', 'method', 'sample.Box', 7, 9), ('method', 'method', 'sample.Box', 11, 11)],
    'java': [('java.util.List', 'import', None, 1, 1), ('Runner', 'interface', None, 2, 4),
             ('run', 'method', 'Runner', 3, 3), ('Item', 'record', None, 5, 5),
             ('Color', 'enum', None, 6, 6), ('Box', 'constructor', 'Box', 8, 8), ('method', 'method', 'Box', 9, 9)],
    'rs': [('std::fmt', 'import', None, 1, 1), ('child', 'module', None, 2, 2), ('Name', 'type', None, 3, 3),
           ('Item', 'struct', None, 4, 4), ('Color', 'enum', None, 5, 5), ('Runner', 'trait', None, 6, 8),
           ('run', 'method', 'Runner', 7, 7), ('Runner for Item', 'impl', None, 9, 11),
           ('run', 'method', 'Runner for Item', 10, 10)],
    'swift': [('SwiftUI', 'import', None, 1, 1), ('Runner', 'protocol', None, 2, 4),
              ('Worker', 'actor', None, 5, 5), ('Card', 'struct', None, 6, 13),
              ('count', 'property', 'Card', 7, 7), ('body', 'property', 'Card', 8, 10),
              ('init', 'init', 'Card', 11, 11), ('Card', 'extension', None, 14, 16),
              ('extra', 'method', 'Card', 15, 15)],
}


class SymbolsTests(unittest.TestCase):
    def test_all_language_fixtures(self):
        for extension, expected in EXPECTED.items():
            with self.subTest(language=extension):
                path = FIXTURES / ('sample.' + extension)
                result = parse_symbols(str(path), path.read_text())
                self.assertEqual(result['map_status'], 'complete')
                actual = {(s['name'], s['kind'], s['parent'], s['start_line'], s['end_line']) for s in result['symbols']}
                self.assertTrue(set(expected) <= actual, set(expected) - actual)
                self.assertTrue(any(s['kind'] == 'import' for s in result['symbols']))
                for symbol in result['symbols']:
                    self.assertLessEqual(1, symbol['start_line'])
                    self.assertLessEqual(symbol['end_line'], len(lines(path.read_text())))

    def test_multiple_declarators_and_function_pointer_variables(self):
        expected = {
            'c': [('first', 'prototype', None, 1, 1), ('second', 'prototype', None, 1, 1),
                  ('third', 'prototype', None, 3, 3), ('factory', 'prototype', None, 4, 4)],
            'cpp': [('Box', 'struct', None, 1, 4), ('first', 'method', 'Box', 2, 2),
                    ('second', 'method', 'Box', 2, 2)],
            'swift': [('Box', 'struct', None, 1, 4), ('first', 'property', 'Box', 2, 2),
                      ('second', 'property', 'Box', 2, 2), ('left', 'property', 'Box', 3, 3),
                      ('right', 'property', 'Box', 3, 3)],
        }
        for extension, symbols in expected.items():
            with self.subTest(extension=extension):
                path = FIXTURES / ('multiple.' + extension)
                result = parse_symbols(str(path), path.read_text())
                self.assertEqual(result['map_status'], 'complete')
                self.assertEqual([(s['name'], s['kind'], s['parent'], s['start_line'], s['end_line'])
                                  for s in result['symbols']], symbols)

    def test_python_lone_cr_uses_evidence_line_coordinates(self):
        text = 'def first():\r    return 1\rdef second():\r    return 2\r'
        result = parse_symbols('x.py', text)
        self.assertEqual(len(lines(text)), 1)
        self.assertEqual([(s['name'], s['start_line'], s['end_line']) for s in result['symbols']],
                         [('first', 1, 1), ('second', 1, 1)])
        mixed = 'def first():\r    return 1\r\ndef second():\n    return 2\r'
        result = parse_symbols('x.py', mixed)
        self.assertEqual([(s['start_line'], s['end_line']) for s in result['symbols']], [(1, 1), (2, 3)])

    def test_headers_select_fewest_errors_and_c_on_tie(self):
        self.assertEqual(parse_symbols('api.h', 'int run(int value);\n')['language'], 'c')
        cpp = parse_symbols('api.h', 'template<class T> class Box { public: void method(); };\n')
        self.assertEqual(cpp['language'], 'cpp')
        self.assertIn('method', {s['name'] for s in cpp['symbols']})

    def test_syntax_errors_and_unknown_language(self):
        python = parse_symbols('x.py', 'def invalid(:\n')
        self.assertEqual(python['map_reason'], 'parse_error')
        self.assertEqual(python['symbols'], [])
        js = parse_symbols('x.js', 'const broken = ;\nfunction intact() {}\n')
        self.assertEqual(js['map_status'], 'partial')
        self.assertIn('intact', {s['name'] for s in js['symbols']})
        self.assertEqual(parse_symbols('x.custom', 'data')['map_reason'], 'unknown_language')

    def test_decorated_python_ranges(self):
        result = parse_symbols('x.py', '@decorator\nasync def run():\n    return "Л"')
        self.assertEqual(result['symbols'], [dict(name='run', kind='async_function', parent=None, start_line=1, end_line=3)])

    def test_native_nested_and_export_ranges_remain_navigation(self):
        result = parse_symbols('x.js', 'export class Box {\n  run() {\n    function inner() {}\n  }\n}\n')
        actual = {(s['name'], s['kind'], s['parent'], s['start_line'], s['end_line'])
                  for s in result['symbols']}
        self.assertEqual(actual, {('Box', 'export', None, 1, 5), ('Box', 'class', None, 1, 5),
                                  ('run', 'method', 'Box', 2, 4), ('inner', 'function', 'Box.run', 3, 3)})


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def context(self, files):
        entries = []
        for path, content in files.items():
            content = content.encode() if isinstance(content, str) else content
            destination = self.root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            entries.append(dict(path=path, type='file', size=len(content), sha256=sha(content)))
        return dict(source_mode='folder', source_directory=str(self.root), source_fingerprint='pinned',
                    _inventory={'entries': entries, 'source_fingerprint': 'pinned'})

    def assert_complete_index(self, payload, manifest):
        self.assertEqual(payload['mode'], 'index-only')
        self.assertEqual(payload['fragments'], [])
        self.assertEqual(manifest['mode'], 'index-only')
        self.assertEqual(manifest['transmitted_bytes'], len(serialize_source_context(payload).encode('utf-8')))
        indexed = {(item['source_id'], item['path']): item for item in payload['files']}
        self.assertEqual(len(indexed), len(manifest['files']))
        for record in manifest['files']:
            item = indexed[(record['source_id'], record['path'])]
            self.assertEqual(record['symbols'], len(item['symbols']))
            self.assertEqual(record['symbols_transmitted'], len(item['symbols']))
            self.assertEqual(record['transmitted_text_bytes'], 0)
            self.assertNotIn('transmitted_ranges', record)
            self.assertNotIn('omitted_ranges', record)
            self.assertNotIn('map_omission_reason', record)
            for key in ('parser', 'language', 'encoding', 'total_lines', 'map_status', 'map_reason'):
                self.assertEqual(item[key], record[key])
        self.assertEqual(payload['omissions']['maps'], sum(f['map_status'] != 'complete' for f in payload['files']))
        self.assertEqual(payload['omissions']['incomplete'], bool(payload['omissions']['maps']))
        self.assertNotIn('budget', payload['omissions']['reasons'])

    def test_complete_symbols_and_deterministic_inventory_order(self):
        context = self.context({'a.py': ''.join(f'def f{i}():\n    return "{i}"\n' for i in range(800)),
                                'b.py': 'import os\ndef useful():\n    return 1\n', 'c.unknown': 'plain text\n' * 500})
        builder = SourceContextBuilder()
        payload, manifest = builder.prepare('study', context)
        reordered = copy.deepcopy(context)
        reordered['_inventory']['entries'].reverse()
        self.assertEqual(payload, builder.prepare('study', reordered)[0])
        self.assertGreater(manifest['transmitted_bytes'], 4096)
        self.assertEqual({f['path']: len(f['symbols']) for f in payload['files']},
                         {'a.py': 800, 'b.py': 2, 'c.unknown': 0})
        self.assert_complete_index(payload, manifest)

    def test_exact_decoding_coordinates_and_final_line(self):
        context = self.context({'unicode.py': codecs.BOM_UTF8 + 'def привет():\r\n    return "🍒"'.encode(),
                                'old.txt': 'Привет\r\nмир'.encode('cp1251'),
                                'utf16.txt': codecs.BOM_UTF16_LE + 'Hello\r\nworld'.encode('utf-16-le')})
        context['source_decoding'] = {'rules': [{'path': 'old.txt', 'encoding': 'cp1251'}]}
        payload, manifest = SourceContextBuilder().prepare('study', context)
        actual = {f['path']: f for f in payload['files']}
        self.assertEqual(actual['unicode.py']['symbols'],
                         [dict(name='привет', kind='function', parent=None, start_line=1, end_line=2)])
        self.assertEqual({p: f['total_lines'] for p, f in actual.items()},
                         {'unicode.py': 2, 'old.txt': 2, 'utf16.txt': 2})
        self.assertEqual(actual['old.txt']['encoding'], 'cp1251')
        self.assertEqual(actual['utf16.txt']['encoding'], 'utf-16-le')
        self.assertNotIn('🍒', serialize_source_context(payload))
        self.assert_complete_index(payload, manifest)

    def test_empty_binary_decode_error_unknown_language_and_long_line(self):
        context = self.context({'empty.py': '', 'bad.py': 'def broken(:\n', 'binary.bin': b'a\x00b',
                                'decode.txt': b'\xffinvalid', 'image.png': b'\x89PNG\x00', 'long.txt': 'x' * 9000 + '\nsecond\n',
                                'later.txt': 'kept\n'})
        payload, manifest = SourceContextBuilder().prepare('study', context)
        records = {r['path']: r for r in payload['files']}
        self.assertEqual(set(records), {'empty.py', 'bad.py', 'binary.bin', 'decode.txt', 'image.png', 'long.txt', 'later.txt'})
        self.assertEqual(records['binary.bin']['map_reason'], 'binary_content')
        self.assertEqual(records['image.png']['map_reason'], 'binary_content')
        self.assertEqual(records['decode.txt']['map_reason'], 'decoding_error')
        self.assertEqual(records['bad.py']['map_reason'], 'parse_error')
        self.assertEqual(records['long.txt']['map_reason'], 'unknown_language')
        self.assertEqual(records['long.txt']['total_lines'], 2)
        self.assertEqual(records['empty.py']['total_lines'], 0)
        self.assertEqual(records['empty.py']['map_status'], 'complete')
        for path in ('binary.bin', 'image.png', 'decode.txt'):
            self.assertIsNone(records[path]['total_lines'])
        self.assert_complete_index(payload, manifest)

    def test_native_partial_map_and_missing_parser_keep_file_records(self):
        context = self.context({'broken.js': 'const broken = ;\nfunction intact() {}\n'})
        payload, manifest = SourceContextBuilder().prepare('study', context)
        self.assertEqual(payload['files'][0]['map_status'], 'partial')
        self.assertIn('intact', {s['name'] for s in payload['files'][0]['symbols']})
        self.assert_complete_index(payload, manifest)
        with patch('src.analysis.source_context._native_symbols', side_effect=ImportError):
            payload, manifest = SourceContextBuilder().prepare('study', context)
        self.assertEqual(payload['files'][0]['map_reason'], 'parser_unavailable')
        self.assertEqual(payload['files'][0]['total_lines'], 2)
        self.assert_complete_index(payload, manifest)

    def test_export_labels_keep_navigation_without_implementation_text(self):
        cases = {
            'arrow.js': ('export default () => "PRIVATE_ARROW_BODY";\n', 'default'),
            'class.js': ('export default class { run() { return "PRIVATE_CLASS_BODY"; } }\n', 'default'),
            'named.js': ('export function exported() { return "PRIVATE_NAMED_BODY"; }\n', 'exported'),
            'variable.js': ('export const worker = () => "PRIVATE_CONST_BODY";\n', 'worker'),
            'clause.js': ('const value = "PRIVATE_ALIAS_BODY";\nexport { value as alias };\n', '{ value as alias }'),
            'identifier.js': ('const chosen = "PRIVATE_IDENTIFIER_BODY";\nexport default chosen;\n', 'chosen'),
            'reexport.js': ('export { remote as local } from "dependency";\n', '"dependency"'),
        }
        context = self.context({path: source for path, (source, _) in cases.items()})
        builder = SourceContextBuilder()
        payload, manifest = builder.prepare('study', context)
        for item in payload['files']:
            with self.subTest(path=item['path']):
                self.assertEqual(item['map_status'], 'complete')
                self.assertEqual([s['name'] for s in item['symbols'] if s['kind'] == 'export'],
                                 [cases[item['path']][1]])
        self.assertNotIn('PRIVATE_', serialize_source_context(payload))
        self.assertNotIn('PRIVATE_', json.dumps(list(builder.cache.values()), ensure_ascii=False))
        self.assert_complete_index(payload, manifest)

    def test_large_file_has_no_index_byte_limit(self):
        source = '#' + 'x' * (8 * 1024 * 1024) + '\ndef large():\n    return "private body"\n'
        context = self.context({'large.py': source})
        payload, manifest = SourceContextBuilder().prepare('study', context)
        self.assertEqual(payload['files'][0]['symbols'],
                         [dict(name='large', kind='function', parent=None, start_line=2, end_line=3)])
        self.assertEqual(manifest['prepared_bytes'], len(source.encode()))
        self.assertNotIn('private body', serialize_source_context(payload))
        self.assert_complete_index(payload, manifest)

    def test_every_file_and_symbol_is_indexed_in_large_projects(self):
        for count in (1000, 5000):
            with self.subTest(files=count):
                context = self.context({f'area{i % 10}/file{i:05d}.js':
                    f'import dependency from "module{i}";\nfunction function{i}() {{ return {i}; }}\n'
                    for i in range(count)})
                payload, manifest = SourceContextBuilder().prepare('study', context)
                self.assertEqual(len(payload['files']), count)
                self.assertEqual({f['path'] for f in payload['files']},
                                 {e['path'] for e in context['_inventory']['entries']})
                self.assertEqual(sum(len(f['symbols']) for f in payload['files']), count * 2)
                self.assertEqual(payload['omissions'], {'maps': 0, 'reasons': {}, 'incomplete': False})
                self.assert_complete_index(payload, manifest)

    def test_snapshot_mutation_is_not_omission_and_cache_validates_identity(self):
        context = self.context({'x.py': 'def original(): pass\n'})
        builder = SourceContextBuilder()
        with patch('src.analysis.source_context.parse_symbols', wraps=parse_symbols) as parser:
            builder.prepare('study', context)
            builder.prepare('study', context)
            self.assertEqual(parser.call_count, 1)
        (self.root / 'x.py').write_text('def changed(): pass\n')
        with self.assertRaises(SourceChanged):
            builder.prepare('study', context)
        with self.assertRaises(SourceChanged):
            SourceContextBuilder().prepare('study', context)

    def test_unsafe_inventory_paths_fail_before_source_reads(self):
        for relative in ('.git', '.git/config', 'nested/.git/config', '../outside.py',
                         '/outside.py', 'a//b.py', 'a/./b.py', 'a\\b.py', 'a:b.py',
                         'a\x00b.py', 'a\nb.py', 'a\x7fb.py'):
            with self.subTest(path=relative):
                context = dict(source_mode='folder', source_directory=str(self.root),
                               source_fingerprint='pinned', _inventory={'entries': [
                                   dict(path=relative, type='file', size=0, sha256=sha(b''))]})
                with patch('src.analysis.source_context._read_confined') as read, \
                        self.assertRaises(AuditError) as caught:
                    SourceContextBuilder().prepare('study', context)
                self.assertEqual(caught.exception.code, 'UNSAFE_SOURCE_PATH')
                self.assertEqual(caught.exception.failure_layer, 'integrity')
                self.assertEqual(caught.exception.node_path, relative)
                read.assert_not_called()

    def test_cache_keeps_only_index_metadata_and_payload_is_independent(self):
        context = self.context({'x.py': 'def main():\n    return "private source body"\n'})
        builder = SourceContextBuilder()
        first, _ = builder.prepare('study', context)
        first['files'][0]['symbols'][0]['name'] = 'changed'
        cached, manifest = builder.prepare('review', context)
        self.assertEqual(cached['files'][0]['symbols'][0]['name'], 'main')
        for record in builder.cache.values():
            self.assertNotIn('source_lines', record)
            self.assertNotIn('text', record)
        self.assertNotIn('private source body', json.dumps(list(builder.cache.values()), ensure_ascii=False))
        self.assertNotIn('private source body', serialize_source_context(cached))
        self.assert_complete_index(cached, manifest)

    def test_pinned_read_reuses_hash_and_rejects_mid_read_mutation(self):
        context = self.context({'x.py': 'def original(): pass\n'})
        path = self.root / 'x.py'
        metadata = {'.': stamp(self.root.stat()), 'x.py': stamp(path.stat())}
        with patch('src.analysis.source_context.sha', side_effect=AssertionError('Source rehashed')):
            payload, _ = SourceContextBuilder().prepare('study', context, expected_metadata=metadata)
        self.assertEqual(payload['files'][0]['symbols'][0]['name'], 'original')
        original_read = os.read

        def mutate(fd, count):
            data = original_read(fd, count)
            if data:
                path.write_bytes(b'x' * len(data))
            return data

        with patch('os.read', side_effect=mutate), \
                patch('src.analysis.source_context.sha', side_effect=AssertionError('Source rehashed')), \
                self.assertRaises(SourceChanged):
            SourceContextBuilder().prepare('study', context, expected_metadata=metadata)

    def test_inventory_scope_symlinks_and_pinned_metadata(self):
        context = self.context({'allowed.py': 'print(1)\n'})
        (self.root / 'excluded.py').write_text('print(2)\n')
        (self.root / 'link.py').symlink_to(self.root / 'excluded.py')
        payload, _ = SourceContextBuilder().prepare('study', context)
        self.assertEqual({f['path'] for f in payload['files']}, {'allowed.py'})
        (self.root / 'allowed.py').unlink()
        (self.root / 'allowed.py').symlink_to(self.root / 'excluded.py')
        metadata = {'.': stamp(self.root.stat()), 'allowed.py': stamp((self.root / 'allowed.py').lstat())}
        with self.assertRaises(SourceChanged):
            SourceContextBuilder().prepare('study', context, expected_metadata=metadata)

    def test_source_ids_disambiguate_identical_relative_paths(self):
        context = self.context({'file.py': 'def root(): pass\n', 'sub/file.py': 'def sub(): pass\n',
                                'sub/nested/file.py': 'def nested(): pass\n'})
        context.pop('source_mode')
        context.pop('source_directory')
        context.update(repository=str(self.root), branch='main', source_commit='a',
                       submodules=[{'path': 'sub', 'expected_commit': 'b'}, {'path': 'sub/nested', 'expected_commit': 'c'}])
        payload, _ = SourceContextBuilder().prepare('study', context)
        self.assertEqual({(f['source_id'], f['path']) for f in payload['files']},
                         {('source-001', 'file.py'), ('source-002', 'file.py'), ('source-003', 'file.py')})

    def test_disabled_compare_and_repair(self):
        context = self.context({'a.py': 'def a(): pass\n', 'b.py': 'def b(): pass\n'})
        self.assertIsNone(SourceContextBuilder({'enabled': False}).prepare('study', context)[0])
        for stage, extra in [('compare', {}), ('repair', {})]:
            self.assertIsNone(SourceContextBuilder().prepare(stage, context | extra)[0])

    def test_review_and_revise_keep_same_index_and_never_execute_source(self):
        context = self.context({'a.py': 'raise RuntimeError("must not execute")\n' * 400})
        builder = SourceContextBuilder()
        expected, _ = builder.prepare('study', context)
        context['study'] = {'evidence': [dict(source_id='source-001', path='a.py', start_line=390, end_line=392)]}
        for stage in ('review', 'revise'):
            payload, manifest = builder.prepare(stage, context)
            self.assertEqual(payload, expected)
            self.assert_complete_index(payload, manifest)
        context['prompt_variant'] = 'revise'
        self.assertEqual(builder.prepare('study', context)[0], expected)

    def test_large_symbol_does_not_hide_small_later_symbol(self):
        context = self.context({'a.py': 'def ' + 'a' * 1500 + '(): pass\ndef small(): pass\n'})
        payload, _ = SourceContextBuilder().prepare('study', context)
        self.assertEqual([s['name'] for f in payload['files'] for s in f['symbols']], ['a' * 1500, 'small'])

    def test_source_comments_and_bodies_are_absent_from_index(self):
        source = ''.join(f'# quote \\" and slash \\ number {i} 🍒\n' for i in range(350)) + 'last'
        context = self.context({'notes.md': source, 'module.py': 'def f():\n    return 1\n'})
        payload, manifest = SourceContextBuilder().prepare('study', context)
        self.assertNotIn('🍒', serialize_source_context(payload))
        self.assertNotIn('return 1', serialize_source_context(payload))
        self.assertEqual({f['path']: f['total_lines'] for f in payload['files']},
                         {'notes.md': 351, 'module.py': 2})
        self.assert_complete_index(payload, manifest)

    def test_configuration_is_strict(self):
        self.assertEqual(source_context_settings(), {'enabled': True, 'mode': 'on-demand'})
        self.assertEqual(source_context_settings({'enabled': False}), {'enabled': False, 'mode': 'on-demand'})
        self.assertEqual(source_context_settings({'mode': 'full'}), {'enabled': True, 'mode': 'full'})
        for value in [{'enabled': 1}, {'enabled': 'false'}, {'max_bytes': 65536}, {'unknown': True}, []]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                source_context_settings(value)


if __name__ == '__main__':
    unittest.main()
