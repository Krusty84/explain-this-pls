# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline decoding and resolver cache checks, including path replacement races."""
import codecs
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.analysis import evidence
from src.analysis.evidence import SourceChanged, resolve_evidence, read_confined
from src.analysis.source_decoding import normalize_source_decoding, decode_source


class SourceDecodingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.context = dict(source_mode='folder', source_directory=str(self.root), source_fingerprint='pinned')
        self.pointer = dict(id='E-001', source_id='source-001', path='app.py', start_line=1, end_line=1, quote='')

    def resolve(self, blob, *, encoding=None, pointer=None):
        (self.root / 'app.py').write_bytes(blob)
        context = self.context | {'source_decoding': {'rules': [] if encoding is None else
            [{'path': '.', 'encoding': encoding}]}}
        return resolve_evidence('study', [pointer or self.pointer], context)[0]

    def test_normalization_is_stable_and_does_not_mutate_configuration(self):
        value = {'rules': [{'path': 'z', 'encoding': 'UTF_16LE'},
                           {'path': '.', 'encoding': 'Latin-1'}, {'path': 'a', 'encoding': 'windows-1251'}]}
        original = copy.deepcopy(value)
        result = normalize_source_decoding(value)
        self.assertEqual(result, {'rules': [{'path': '.', 'encoding': 'latin-1'},
            {'path': 'a', 'encoding': 'cp1251'}, {'path': 'z', 'encoding': 'utf-16-le'}]})
        self.assertEqual(value, original)
        self.assertEqual(normalize_source_decoding(result), result)
        self.assertEqual(normalize_source_decoding(), {'rules': []})
        self.assertEqual(normalize_source_decoding({}), {'rules': []})

    def test_invalid_rules_fail_before_resolution(self):
        invalid = [[], {'extra': []}, {'rules': None}, {'rules': [None]},
                   {'rules': [{'path': 'src'}]}, {'rules': [{'path': 'src', 'encoding': 'shift_jis'}]},
                   {'rules': [{'path': 'src', 'encoding': 'unknown'}]},
                   {'rules': [{'path': 'src', 'encoding': 1}]},
                   {'rules': [{'path': 'src', 'encoding': 'utf-8', 'extra': True}]},
                   {'rules': [{'path': 'src', 'encoding': 'cp1251'}, {'path': 'src', 'encoding': 'utf-8'}]}]
        invalid += [{'rules': [{'path': path, 'encoding': 'utf-8'}]}
                    for path in ('', ' ', '/absolute', '../src', './src', 'src/..', 'src//a',
                                 'src/', 'C:/src', 'src\\a', 'src\x00a', 'src\na', 1)]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_source_decoding(value)
        with patch('src.analysis.evidence._read_confined', side_effect=AssertionError('must not read source')):
            with self.assertRaises(ValueError):
                resolve_evidence('study', [self.pointer], self.context | {'source_decoding': invalid[5]})

    def test_supported_encoding_matrix_bom_quotes_and_raw_hashes(self):
        cases = [('utf-8', 'Привет', b'', None), ('utf-8', 'Привет', codecs.BOM_UTF8, None),
                 ('utf-8', 'Привет', codecs.BOM_UTF8, 'utf-8'),
                 ('cp1251', 'Привет', b'', 'cp1251'), ('cp866', 'Привет', b'', 'cp866'),
                 ('cp1252', 'café €', b'', 'cp1252'), ('latin-1', 'café', b'', 'latin-1')]
        for family in ('16', '32'):
            for endian in ('le', 'be'):
                codec = f'utf-{family}-{endian}'
                bom = getattr(codecs, f'BOM_UTF{family}_{endian.upper()}')
                cases.extend((codec, 'Привет', prefix, rule) for prefix, rule in
                             ((bom, None), (bom, codec), (bom, f'utf-{family}'), (b'', codec)))
        for codec, sample, bom, rule in cases:
            with self.subTest(codec=codec, bom=bom, rule=rule):
                text = sample + '\r\nlast\rpart'
                blob = bom + text.encode(codec)
                normalized = sample + '\nlast\rpart'
                result = self.resolve(blob, encoding=rule, pointer=self.pointer | {'end_line': 2, 'quote': normalized})
                self.assertEqual(result['status'], 'RESOLVED')
                self.assertEqual(result['encoding'], codec)
                self.assertEqual(result['file_sha256'], hashlib.sha256(blob).hexdigest())
                self.assertNotIn('fragment_sha256', result)
                self.assertNotIn('fragment', result)

    def test_bom_conflicts_are_distinct_from_damaged_bytes(self):
        for bom, rule in ((codecs.BOM_UTF8, 'cp1251'), (codecs.BOM_UTF16_LE, 'utf-16-be'),
                          (codecs.BOM_UTF32_LE, 'utf-16-le'), (codecs.BOM_UTF32_BE, 'utf-8'),
                          (codecs.BOM_UTF16_BE, 'utf-32')):
            with self.subTest(bom=bom, rule=rule):
                result = self.resolve(bom + b'A', encoding=rule)
                self.assertEqual(result['status'], 'ENCODING_MISMATCH')
                self.assertIsNone(result['encoding'])
                self.assertEqual(result['file_sha256'], hashlib.sha256(bom + b'A').hexdigest())
        for blob, codec in ((b'\xff', None), (b'\x81', 'cp1252'), (b'\x98', 'cp1251'),
                            (b'A', 'utf-16-le'), (b'A\0', 'utf-32-le'),
                            (b'A\0', 'utf-16'), (b'A\0\0\0', 'utf-32'),
                            (b'', 'utf-16'), (codecs.BOM_UTF16_LE + b'A', None)):
            with self.subTest(blob=blob, codec=codec):
                result = self.resolve(blob, encoding=codec)
                self.assertEqual(result['status'], 'DECODE_ERROR')
                self.assertIsNotNone(result['encoding'])
        self.assertEqual(self.resolve(codecs.BOM_UTF8 + b'A\n', pointer=self.pointer | {'quote': '\ufeffA\n'})['status'],
                         'QUOTE_MISMATCH')
        result = self.resolve(codecs.BOM_UTF8 + '\ufeffA\n'.encode(), pointer=self.pointer | {'quote': '\ufeffA\n'})
        self.assertEqual(result['status'], 'RESOLVED')

    def test_most_specific_component_path_wins_including_root_and_submodules(self):
        settings = normalize_source_decoding({'rules': [
            {'path': '.', 'encoding': 'cp1251'}, {'path': 'a', 'encoding': 'cp1252'},
            {'path': 'src', 'encoding': 'cp866'}, {'path': 'src/app.py', 'encoding': 'latin-1'}]})
        for path, encoding in (('elsewhere', 'cp1251'), ('a', 'cp1252'), ('a/b', 'cp1252'),
                               ('ab/x', 'cp1251'), ('src/old/file', 'cp866'),
                               ('src2/file', 'cp1251'), ('src/app.py', 'latin-1')):
            with self.subTest(path=path):
                self.assertEqual(decode_source(b'A', path, settings), ('A', encoding))
        vendor = self.root / 'vendor' / 'child'
        vendor.mkdir(parents=True)
        (vendor / 'app.py').write_bytes('Привет\n'.encode('cp1251'))
        context = dict(source_mode='git', repository=str(self.root), branch='main', source_commit='abc',
            submodules=[dict(path='vendor/child', expected_commit='def')],
            source_decoding={'rules': [{'path': 'vendor/child', 'encoding': 'cp1251'}]})
        result = resolve_evidence('study', [self.pointer | {'source_id': 'source-002', 'quote': 'Привет\n'}], context)[0]
        self.assertEqual(result['status'], 'RESOLVED')
        self.assertEqual(result['encoding'], 'cp1251')

    def test_same_megabyte_file_is_read_and_decoded_once_for_33_references(self):
        blob = b'x\n' * (512 * 1024)
        (self.root / 'app.py').write_bytes(blob)
        pointers = [self.pointer | {'id': f'E-{i:03d}'} for i in range(1, 34)]
        with patch('src.analysis.evidence._read_confined', wraps=evidence._read_confined) as reads, \
                patch('src.analysis.evidence.decode_source', wraps=decode_source) as decodes:
            results = resolve_evidence('study', pointers, self.context,
                {'app.py': hashlib.sha256(blob).hexdigest()})
        self.assertTrue(all(result['status'] == 'RESOLVED' for result in results))
        self.assertEqual(sum(call.kwargs.get('read', True) for call in reads.call_args_list), 1)
        self.assertEqual(reads.call_count, 33)  # Every hit still safely walks and verifies the path.
        self.assertEqual(decodes.call_count, 1)
        with patch('src.analysis.evidence.decode_source', wraps=decode_source) as decodes:
            resolve_evidence('review', pointers[:1], self.context)
            resolve_evidence('study', pointers[:1], self.context)
        self.assertEqual(decodes.call_count, 2)

    def test_33_unique_megabyte_files_exceed_total_limit(self):
        blob = b'x\n' * (512 * 1024)
        pointers = []
        for i in range(33):
            path = f'file-{i:03d}'
            (self.root / path).write_bytes(blob)
            pointers.append(self.pointer | {'id': f'E-{i:03d}', 'path': path})
        results = resolve_evidence('study', pointers, self.context)
        self.assertEqual([result['status'] for result in results], ['RESOLVED'] * 32 + ['LIMIT_EXCEEDED'])

    def test_failed_decode_is_not_cached_and_unique_read_budget_is_not_recharged(self):
        (self.root / 'app.py').write_bytes(b'\xff')
        with patch('src.analysis.evidence.MAX_TOTAL_BYTES', 1), patch('src.analysis.evidence.decode_source', wraps=decode_source) as decodes:
            results = resolve_evidence('study', [self.pointer, self.pointer | {'id': 'E-002'}], self.context)
        self.assertEqual([result['status'] for result in results], ['DECODE_ERROR', 'DECODE_ERROR'])
        self.assertEqual(decodes.call_count, 2)

    def test_bad_fragment_does_not_prevent_successful_file_cache(self):
        (self.root / 'app.py').write_bytes(b'A\n')
        pointers = [self.pointer | {'quote': 'wrong'}, self.pointer | {'id': 'E-002', 'end_line': 2},
                    self.pointer | {'id': 'E-003'}]
        with patch('src.analysis.evidence.decode_source', wraps=decode_source) as decodes:
            results = resolve_evidence('study', pointers, self.context)
        self.assertEqual([result['status'] for result in results], ['QUOTE_MISMATCH', 'OUT_OF_RANGE', 'RESOLVED'])
        self.assertEqual(decodes.call_count, 1)

    def test_cached_file_mutations_never_silently_reread(self):
        for mutation in ('contents', 'replacement', 'missing', 'symlink', 'permissions'):
            with self.subTest(mutation=mutation):
                path = self.root / mutation
                path.write_bytes(b'A\n')
                pointer = self.pointer | {'path': mutation}
                def pointers():
                    yield pointer
                    if mutation == 'contents':
                        path.write_bytes(b'B\n')
                    elif mutation == 'replacement':
                        path.rename(self.root / 'old-replacement')
                        path.write_bytes(b'A\n')
                    elif mutation == 'missing':
                        path.unlink()
                    elif mutation == 'symlink':
                        path.rename(self.root / 'old-symlink')
                        path.symlink_to(self.root / 'old-symlink')
                    else:
                        path.chmod(0o400)
                    yield pointer | {'id': 'E-002'}
                with patch('src.analysis.evidence.decode_source', wraps=decode_source) as decodes, self.assertRaises(SourceChanged):
                    resolve_evidence('study', pointers(), self.context)
                self.assertEqual(decodes.call_count, 1)

    def test_cached_directory_replacement_detected_even_with_identical_file_inode(self):
        directory = self.root / 'src'
        directory.mkdir()
        (directory / 'app.py').write_bytes(b'A\n')
        pointer = self.pointer | {'path': 'src/app.py'}
        def pointers():
            yield pointer
            directory.rename(self.root / 'old-src')
            directory.mkdir()
            os.link(self.root / 'old-src' / 'app.py', directory / 'app.py')
            yield pointer | {'id': 'E-002'}
        with self.assertRaises(SourceChanged):
            resolve_evidence('study', pointers(), self.context)

    def test_cached_path_race_during_open_is_fatal_and_original_api_remains_bytes(self):
        path = self.root / 'app.py'
        path.write_bytes(b'A\n')
        self.assertEqual(read_confined(self.root, 'app.py', 10), b'A\n')
        original = os.open
        opened = 0
        def replace_on_second_open(name, flags, *args, **kwargs):
            nonlocal opened
            if name == 'app.py':
                opened += 1
                if opened == 2:
                    path.rename(self.root / 'old-app.py')
                    path.write_bytes(b'A\n')
            return original(name, flags, *args, **kwargs)
        with patch('src.analysis.evidence.os.open', side_effect=replace_on_second_open), self.assertRaises(SourceChanged):
            resolve_evidence('study', [self.pointer, self.pointer | {'id': 'E-002'}], self.context)


if __name__ == '__main__':
    unittest.main()
