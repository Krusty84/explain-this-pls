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
from src.analysis.source_context import (MAX_FRAGMENT_LINES, SourceContextBuilder, parse_symbols,
                                         serialize_source_context, source_context_settings)

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

    def assert_complete_accounting(self, payload, manifest):
        for record in manifest['files']:
            if record['total_lines'] is None:
                self.assertTrue(record['text_reason'])
                continue
            delivered = set()
            for fragment in payload['fragments']:
                if (fragment['source_id'], fragment['path']) != (record['source_id'], record['path']):
                    continue
                numbers = set(range(fragment['start_line'], fragment['end_line'] + 1))
                self.assertFalse(delivered & numbers)
                self.assertLessEqual(len(numbers), MAX_FRAGMENT_LINES)
                delivered |= numbers
            omitted = {n for start, end in record['omitted_ranges'] for n in range(start, end + 1)}
            self.assertFalse(delivered & omitted)
            self.assertEqual(delivered | omitted, set(range(1, record['total_lines'] + 1)))

    def test_exact_budget_fairness_and_deterministic_inventory_order(self):
        context = self.context({'a.py': ''.join(f'def f{i}():\n    return "{i}"\n' for i in range(800)),
                                'b.py': 'import os\ndef useful():\n    return 1\n', 'c.unknown': 'plain text\n' * 500})
        builder = SourceContextBuilder({'max_bytes': 4096})
        payload, manifest = builder.prepare('study', context)
        reordered = copy.deepcopy(context)
        reordered['_inventory']['entries'].reverse()
        self.assertEqual(payload, builder.prepare('study', reordered)[0])
        self.assertLessEqual(len(serialize_source_context(payload).encode()), 4096)
        self.assertLessEqual(manifest['map_bytes'], 1024)
        self.assertEqual(manifest['transmitted_bytes'], len(json.dumps(payload, ensure_ascii=False).encode()))
        self.assertEqual({f['path'] for f in payload['fragments']}, {'a.py', 'b.py', 'c.unknown'})
        self.assert_complete_accounting(payload, manifest)

    def test_exact_decoding_coordinates_and_final_line(self):
        context = self.context({'unicode.py': codecs.BOM_UTF8 + 'def привет():\r\n    return "🍒"'.encode(),
                                'old.txt': 'Привет\r\nмир'.encode('cp1251'),
                                'utf16.txt': codecs.BOM_UTF16_LE + 'Hello\r\nworld'.encode('utf-16-le')})
        context['source_decoding'] = {'rules': [{'path': 'old.txt', 'encoding': 'cp1251'}]}
        payload, manifest = SourceContextBuilder().prepare('study', context)
        actual = {f['path']: f['text'] for f in payload['fragments']}
        self.assertEqual(actual, {'unicode.py': 'def привет():\n    return "🍒"', 'old.txt': 'Привет\nмир',
                                  'utf16.txt': 'Hello\nworld'})
        self.assert_complete_accounting(payload, manifest)

    def test_text_fallback_empty_binary_decode_limit_and_long_line(self):
        context = self.context({'empty.py': '', 'bad.py': 'def broken(:\n', 'binary.bin': b'a\x00b',
                                'decode.txt': b'\xffinvalid', 'image.png': b'\x89PNG\x00', 'long.txt': 'x' * 9000 + '\nsecond\n',
                                'later.txt': 'kept\n', 'large.txt': 'y' * 200})
        with patch('src.analysis.source_context.MAX_FILE_BYTES', 100):
            _, capped = SourceContextBuilder().prepare('study', context)
        self.assertEqual(next(r for r in capped['files'] if r['path'] == 'large.txt')['text_reason'], 'file_size_limit')
        payload, manifest = SourceContextBuilder({'max_bytes': 4096}).prepare('study', context)
        records = {r['path']: r for r in manifest['files']}
        self.assertEqual(records['binary.bin']['text_reason'], 'binary_content')
        self.assertEqual(records['image.png']['text_reason'], 'binary_content')
        self.assertEqual(records['decode.txt']['text_reason'], 'decoding_error')
        self.assertEqual(records['bad.py']['map_reason'], 'parse_error')
        self.assertTrue(any(f['path'] == 'bad.py' for f in payload['fragments']))
        self.assertTrue(any(f['path'] == 'later.txt' for f in payload['fragments']))
        self.assertEqual(records['empty.py']['total_lines'], 0)
        self.assert_complete_accounting(payload, manifest)

    def test_snapshot_mutation_is_not_omission_and_cache_validates_identity(self):
        context = self.context({'x.py': 'def original(): pass\n'})
        builder = SourceContextBuilder()
        with patch('src.analysis.source_context.parse_symbols', wraps=parse_symbols) as parser:
            builder.prepare('catalog', context)
            builder.prepare('study', context)
            self.assertEqual(parser.call_count, 1)
        (self.root / 'x.py').write_text('def changed(): pass\n')
        with self.assertRaises(SourceChanged):
            builder.prepare('study', context)
        with self.assertRaises(SourceChanged):
            SourceContextBuilder().prepare('study', context)

    def test_pinned_read_reuses_hash_and_rejects_mid_read_mutation(self):
        context = self.context({'x.py': 'def original(): pass\n'})
        path = self.root / 'x.py'
        metadata = {'.': stamp(self.root.stat()), 'x.py': stamp(path.stat())}
        with patch('src.analysis.source_context.sha', side_effect=AssertionError('Source rehashed')):
            payload, _ = SourceContextBuilder().prepare('study', context, expected_metadata=metadata)
        self.assertTrue(payload['fragments'])
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
        self.assertEqual({(f['source_id'], f['path']) for f in payload['fragments']},
                         {('source-001', 'file.py'), ('source-002', 'file.py'), ('source-003', 'file.py')})

    def test_catalog_disabled_compare_and_repair(self):
        context = self.context({'a.py': 'def a(): pass\n', 'b.py': 'def b(): pass\n'})
        self.assertEqual(SourceContextBuilder().prepare('catalog', context)[0]['fragments'], [])
        self.assertIsNone(SourceContextBuilder({'enabled': False}).prepare('study', context)[0])
        for stage, extra in [('compare', {}), ('repair', {})]:
            self.assertIsNone(SourceContextBuilder().prepare(stage, context | extra)[0])

    def test_review_prioritizes_citations_and_never_executes_source(self):
        context = self.context({'a.py': 'raise RuntimeError("must not execute")\n' * 400})
        context['study'] = {'evidence': [dict(source_id='source-001', path='a.py', start_line=390, end_line=392)]}
        payload, manifest = SourceContextBuilder({'max_bytes': 4096}).prepare('review', context)
        actual = {n for f in payload['fragments'] for n in range(f['start_line'], f['end_line'] + 1)}
        self.assertTrue({390, 391, 392} <= actual)
        self.assert_complete_accounting(payload, manifest)

    def test_plain_text_round_robin_and_many_file_map(self):
        context = self.context({'a.txt': ('a' * 90 + '\n') * 120,
                                'b.txt': ('b' * 90 + '\n') * 120, 'c.txt': 'last file\n'})
        payload, manifest = SourceContextBuilder({'max_bytes': 4096}).prepare('study', context)
        self.assertEqual({f['path'] for f in payload['fragments']}, {'a.txt', 'b.txt', 'c.txt'})
        self.assert_complete_accounting(payload, manifest)
        context = self.context({f'file{i:03d}.py': f'def f{i}(): pass\n' for i in range(100)})
        payload, _ = SourceContextBuilder({'max_bytes': 4096}).prepare('catalog', context)
        self.assertTrue(payload['files'])
        self.assertTrue(any(item['symbols'] for item in payload['files']))
        self.assertGreater(payload['omissions']['maps'], 0)
        self.assertIn('budget', payload['omissions']['reasons'])

    def test_large_symbol_does_not_hide_small_later_symbol(self):
        context = self.context({'a.py': 'def ' + 'a' * 1500 + '(): pass\ndef small(): pass\n'})
        payload, _ = SourceContextBuilder({'max_bytes': 4096}).prepare('catalog', context)
        self.assertEqual([s['name'] for f in payload['files'] for s in f['symbols']], ['small'])

    def test_remaining_text_uses_capacity_after_small_peer_finishes(self):
        context = self.context({'a.txt': ('line ' * 10 + '\n') * 100, 'b.txt': 'tiny\n'})
        payload, manifest = SourceContextBuilder({'max_bytes': 8192}).prepare('study', context)
        self.assertEqual(manifest['files'][0]['omitted_ranges'], [])
        self.assertEqual(manifest['files'][1]['omitted_ranges'], [])
        self.assert_complete_accounting(payload, manifest)

    def test_revise_variant_prioritizes_citations(self):
        context = self.context({'a.txt': ('material ' * 20 + '\n') * 300})
        context.update(prompt_variant='revise', study={'evidence': [
            dict(source_id='source-001', path='a.txt', start_line=299, end_line=300)]})
        payload, _ = SourceContextBuilder({'max_bytes': 4096}).prepare('study', context)
        self.assertTrue(any(f['start_line'] <= 299 <= f['end_line'] for f in payload['fragments']))

    def test_every_delivered_line_is_literal_source(self):
        source = ''.join(f'# quote \\" and slash \\ number {i} 🍒\n' for i in range(350)) + 'last'
        context = self.context({'notes.md': source, 'module.py': 'def f():\n    return 1\n'})
        payload, manifest = SourceContextBuilder({'max_bytes': 4096}).prepare('study', context)
        source_lines = {'notes.md': lines(source), 'module.py': ['def f():\n', '    return 1\n']}
        for fragment in payload['fragments']:
            self.assertEqual(fragment['text'], ''.join(source_lines[fragment['path']][fragment['start_line'] - 1:fragment['end_line']]))
        self.assert_complete_accounting(payload, manifest)
        self.assertLessEqual(manifest['transmitted_bytes'], 4096)

    def test_configuration_is_strict(self):
        self.assertEqual(source_context_settings(), {'enabled': True, 'max_bytes': 65536})
        for value in [{'enabled': 1}, {'enabled': 'false'}, {'max_bytes': True}, {'max_bytes': 4095},
                      {'max_bytes': 4096.0}, {'unknown': True}, []]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                source_context_settings(value)


if __name__ == '__main__':
    unittest.main()
