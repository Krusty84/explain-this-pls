# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import codecs
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.analysis import evidence
from src.analysis.citation_repair import repair_citations
from src.analysis.evidence import SourceChanged, canonical, resolve_evidence, sha, stamp


class CitationRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'sample.txt'
        self.context = dict(source_mode='folder', source_directory=str(self.root), source_fingerprint='pinned')

    def source(self, text):
        blob = text.encode() if isinstance(text, str) else text
        self.path.write_bytes(blob)
        self.pins = {'sample.txt': sha(blob)}
        self.metadata = {'.': stamp(self.root.stat()), 'sample.txt': stamp(self.path.stat())}

    def pointer(self, start, end, quote):
        return dict(id='unchanged-id', source_id='source-001', path='sample.txt',
                    start_line=start, end_line=end, quote=quote)

    def repair(self, pointer, *, extra=None, context=None):
        value = {'evidence': [pointer], 'claims': [{'id': 'original-claim', 'statement': 'Unchanged.'}]}
        value.update(extra or {})
        original, original_source = copy.deepcopy(value), self.path.read_bytes()
        candidate, journal = repair_citations('study', value, context or self.context, self.pins,
                                              expected_metadata=self.metadata)
        self.assertEqual(value, original)
        self.assertEqual(self.path.read_bytes(), original_source)
        self.assertIsNot(candidate, value)
        self.assertEqual(candidate['claims'], value['claims'])
        self.assertEqual(journal['input_sha256'], sha(canonical(value)))
        self.assertEqual(journal['output_sha256'], sha(canonical(candidate)))
        if journal['changes']:
            self.assertEqual(resolve_evidence('study', candidate['evidence'], context or self.context,
                                             self.pins, expected_metadata=self.metadata)[0]['status'], 'RESOLVED')
            self.assertEqual(journal['changes'][0]['evidence_index'], 0)
            self.assertEqual(journal['changes'][0]['id'], pointer['id'])
            self.assertEqual(journal['changes'][0]['original'], pointer)
            self.assertEqual(journal['changes'][0]['repaired'], candidate['evidence'][0])
        return candidate, journal

    def test_logged_block_quote_missing_final_line(self):
        # Anonymized regression: an eleven-line block cited as ten source lines.
        block = '    {\n      "name": "Example",\n      "type": "debug",\n      "request": "run",\n' \
                '      "program": "entry",\n      "console": "terminal",\n      "enabled": false,\n' \
                '      "env": {\n        "PATH": "local"\n      }\n    },'
        self.source('header\nitems\nstart\n' + block + '\nfooter\n')
        original = self.pointer(4, 13, block)
        candidate, journal = self.repair(original)
        self.assertEqual(candidate['evidence'][0], original | {'end_line': 14})
        self.assertEqual(journal['changes'][0]['reason'], 'NEARBY_EXACT_QUOTE')
        diagnostic = journal['diagnostics'][0]
        self.assertEqual({key: diagnostic[key] for key in ('start_line', 'end_line', 'reason', 'action')},
                         dict(start_line=4, end_line=13, reason='QUOTE_MISMATCH', action='CORRECTED'))
        self.assertIn('+++ source-001/sample.txt:4-13\n', diagnostic['diff'])
        self.assertIn('-    },\n\\ No newline at end of file\n', diagnostic['diff'])
        self.assertNotIn('+    },', diagnostic['diff'])

    def test_logged_single_line_extra_indent(self):
        line = '    with raises(Error, match="iteration limit"):'
        self.source('first\n' + line + '\n        await operation()\nlast\n')
        original = self.pointer(2, 3, '    ' + line)
        candidate, journal = self.repair(original)
        self.assertEqual(candidate['evidence'][0], original | {'quote': line})
        self.assertEqual(journal['changes'][0]['reason'], 'UNIFORM_LEADING_INDENT')

    def test_exact_resolution_is_untouched_even_if_elsewhere_is_ambiguous(self):
        self.source('target\ntarget\n')
        candidate, journal = self.repair(self.pointer(1, 1, 'target'))
        self.assertEqual(journal['changes'], [])
        self.assertEqual(journal['diagnostics'], [])
        self.assertEqual(journal['input_sha256'], journal['output_sha256'])
        self.assertEqual(candidate['evidence'][0]['start_line'], 1)

    def test_boundaries_expand_at_most_three_lines(self):
        self.source('outside\ntarget\nthree\nfour\nfive\nsix\nseven\n')
        candidate, journal = self.repair(self.pointer(5, 5, 'target'))
        self.assertEqual(candidate['evidence'][0]['start_line'], 2)
        self.assertTrue(journal['changes'])
        candidate, journal = self.repair(self.pointer(6, 6, 'target'))
        self.assertEqual(candidate['evidence'][0]['start_line'], 6)
        self.assertEqual(journal['changes'], [])

    def test_ambiguous_exact_and_indent_matches_are_unchanged(self):
        for text, quote in [('target\nother\nmiddle\nother\ntarget\n', 'target'),
                            ('    target\nother\nmiddle\nother\n    target\n', '        target')]:
            with self.subTest(quote=quote):
                self.source(text)
                candidate, journal = self.repair(self.pointer(3, 3, quote))
                self.assertEqual(journal['changes'], [])
                self.assertEqual(candidate['evidence'][0]['quote'], quote)

    def test_exact_match_precedes_uniform_indent_match(self):
        self.source('    target\nmiddle\n        target\n')
        candidate, journal = self.repair(self.pointer(2, 2, '        target'))
        self.assertEqual(candidate['evidence'][0]['end_line'], 3)
        self.assertEqual(candidate['evidence'][0]['quote'], '        target')
        self.assertEqual(journal['changes'][0]['reason'], 'NEARBY_EXACT_QUOTE')

    def test_multiline_uniform_indent_preserves_internal_text_and_blank_lines(self):
        actual = '  if enabled:\n\n      call("two  spaces")\n'
        self.source(actual)
        quote = '      if enabled:\n\n          call("two  spaces")\n'
        candidate, journal = self.repair(self.pointer(1, 3, quote))
        self.assertTrue(journal['changes'])
        self.assertEqual(candidate['evidence'][0]['quote'], actual)

    def test_tokens_internal_spaces_and_relative_indent_cannot_change(self):
        self.source('    if enabled:\n        call("two  spaces")\n')
        quotes = ['        if disabled:\n            call("two  spaces")\n',
                  '        if enabled:\n            call("two spaces")\n',
                  '        if enabled:\n          call("two  spaces")\n']
        for quote in quotes:
            with self.subTest(quote=quote):
                candidate, journal = self.repair(self.pointer(1, 2, quote))
                self.assertEqual(journal['changes'], [])
                self.assertEqual(candidate['evidence'][0]['quote'], quote)

    def test_uniform_tabs_preserve_relative_indentation(self):
        self.source('  if enabled:\n  \tcall()\n')
        quote = '\tif enabled:\n\t\tcall()\n'
        candidate, journal = self.repair(self.pointer(1, 2, quote))
        self.assertTrue(journal['changes'])
        self.assertEqual(candidate['evidence'][0]['quote'], '  if enabled:\n  \tcall()\n')

    def test_crlf_unicode_bom_and_explicit_decoding(self):
        text = '    if готово:\r\n        вывести("Привет")\r\n'
        quote = '        if готово:\r\n            вывести("Привет")\r\n'
        cases = [(codecs.BOM_UTF16_LE + text.encode('utf-16-le'), self.context),
                 (text.encode('cp1251'), self.context | {'source_decoding': {
                     'rules': [{'path': 'sample.txt', 'encoding': 'cp1251'}]}})]
        for blob, context in cases:
            with self.subTest(encoding=context.get('source_decoding')):
                self.source(blob)
                candidate, journal = self.repair(self.pointer(1, 2, quote), context=context)
                self.assertTrue(journal['changes'])
                self.assertEqual(candidate['evidence'][0]['quote'], text)

    def test_missing_final_newline_is_preserved(self):
        self.source('    target')
        candidate, _ = self.repair(self.pointer(1, 1, '        target'))
        self.assertEqual(candidate['evidence'][0]['quote'], '    target')
        self.source('    target\n')
        candidate, _ = self.repair(self.pointer(1, 1, '        target'))
        self.assertEqual(candidate['evidence'][0]['quote'], '    target')

    def test_out_of_range_decode_and_read_limits_remain_failures(self):
        self.source('target\n')
        _, journal = self.repair(self.pointer(1, 2, 'target'))
        self.assertEqual(journal['changes'], [])
        self.assertEqual(journal['diagnostics'], [])
        with patch('src.analysis.evidence.MAX_FILE_BYTES', 1):
            _, journal = self.repair(self.pointer(1, 1, 'other'))
        self.assertEqual(journal['changes'], [])
        self.source(b'\xff')
        _, journal = self.repair(self.pointer(1, 1, 'target'))
        self.assertEqual(journal['changes'], [])

    def test_repaired_range_must_pass_ordinary_line_limit(self):
        self.source('target\n' + 'body\n' * 200)
        _, journal = self.repair(self.pointer(2, 201, 'target'))
        self.assertEqual(journal['changes'], [])

    def test_only_mismatched_index_is_journaled_and_ids_stay_unchanged(self):
        self.source('first\n    second\n')
        pointers = [self.pointer(1, 1, 'first'), self.pointer(2, 2, '        second') | {'id': 'raw-2'}]
        candidate, journal = repair_citations('study', {'evidence': pointers}, self.context, self.pins,
                                              expected_metadata=self.metadata)
        self.assertEqual(len(journal['changes']), 1)
        self.assertEqual(journal['changes'][0]['evidence_index'], 1)
        self.assertEqual(journal['changes'][0]['id'], 'raw-2')
        self.assertEqual(candidate['evidence'][0], pointers[0])

    def test_diagnostics_cover_corrected_and_unresolved_mismatches(self):
        self.source('first\n    second\nthird\n')
        pointers = [self.pointer(1, 1, 'first'),
                    self.pointer(2, 2, '        second') | {'id': 'corrected'},
                    self.pointer(3, 3, 'different') | {'id': 'unresolved'}]
        value = {'evidence': pointers}
        candidate, journal = repair_citations('study', value, self.context, self.pins,
                                              expected_metadata=self.metadata)
        self.assertEqual(set(candidate), set(value))
        self.assertEqual(candidate['evidence'][2], pointers[2])
        diagnostics = journal['diagnostics']
        self.assertEqual([(item['evidence_index'], item['id'], item['action']) for item in diagnostics],
                         [(1, 'corrected', 'CORRECTED'), (2, 'unresolved', 'UNRESOLVED')])
        for item in diagnostics:
            self.assertEqual(item['reason'], 'QUOTE_MISMATCH')
            self.assertEqual((item['source_id'], item['path']), ('source-001', 'sample.txt'))
        self.assertIn('-        second\n', diagnostics[0]['diff'])
        self.assertIn('+    second\n', diagnostics[0]['diff'])
        self.assertIn('-different\n', diagnostics[1]['diff'])
        self.assertIn('+third\n', diagnostics[1]['diff'])

    def test_unresolved_diff_includes_full_context_without_truncation(self):
        source = ''.join(f'line {index}: точный текст\n' for index in range(1, 21))
        self.source(source)
        quote = source.replace('line 10:', 'changed 10:')
        candidate, journal = self.repair(self.pointer(1, 20, quote))
        self.assertEqual(candidate['evidence'][0]['quote'], quote)
        self.assertEqual(journal['changes'], [])
        diagnostic = journal['diagnostics'][0]
        self.assertEqual(diagnostic['action'], 'UNRESOLVED')
        diff = diagnostic['diff']
        self.assertIn('@@ -1,20 +1,20 @@\n', diff)
        for index in range(1, 21):
            prefix = '+' if index == 10 else ' '
            self.assertIn(f'{prefix}line {index}: точный текст\n', diff)
        self.assertIn('-changed 10: точный текст\n', diff)

    def test_diff_marks_each_unterminated_final_line(self):
        self.source('actual')
        _, journal = self.repair(self.pointer(1, 1, 'different'))
        self.assertEqual(journal['diagnostics'][0]['diff'],
                         '--- model_quote\n+++ source-001/sample.txt:1-1\n@@ -1 +1 @@\n'
                         '-different\n\\ No newline at end of file\n'
                         '+actual\n\\ No newline at end of file\n')

    def test_pinned_sources_are_not_rehashed(self):
        self.source('    target\n')
        with patch('src.analysis.evidence.sha', side_effect=AssertionError('Source was rehashed')):
            _, journal = self.repair(self.pointer(1, 1, '        target'))
        self.assertTrue(journal['changes'])

    def test_unsafe_path_and_excluded_file_are_not_repaired(self):
        self.source('    target\n')
        pointer = self.pointer(1, 1, '        target') | {'path': '../sample.txt'}
        value = {'evidence': [pointer]}
        candidate, journal = repair_citations('study', value, self.context, self.pins, expected_metadata=self.metadata)
        self.assertEqual(candidate, value)
        self.assertFalse(journal['changes'])
        context = self.context | {'_inventory': {'entries': []}}
        candidate, journal = repair_citations('study', {'evidence': [self.pointer(1, 1, '        target')]}, context)
        self.assertFalse(journal['changes'])

    def test_symlink_and_snapshot_mutation_are_fatal_with_pins(self):
        for mutation in ('write', 'symlink'):
            with self.subTest(mutation=mutation):
                self.source('    target\n')
                if mutation == 'write':
                    self.path.write_text('    altered\n')
                else:
                    self.path.unlink()
                    self.path.symlink_to(self.root / 'outside')
                with self.assertRaises(SourceChanged):
                    repair_citations('study', {'evidence': [self.pointer(1, 1, '        target')]},
                                     self.context, self.pins, expected_metadata=self.metadata)
                if self.path.is_symlink():
                    self.path.unlink()

    def test_mutation_before_final_validation_is_fatal(self):
        self.source('    target\n')
        original = evidence.resolve_evidence

        def mutate(*args, **kwargs):
            self.path.write_text('    altered\n')
            return original(*args, **kwargs)

        with patch('src.analysis.citation_repair.resolve_evidence', side_effect=mutate), self.assertRaises(SourceChanged):
            repair_citations('study', {'evidence': [self.pointer(1, 1, '        target')]},
                             self.context, self.pins, expected_metadata=self.metadata)

    def test_standalone_source_mutation_is_fatal_too(self):
        self.source('    target\n')
        original = evidence.resolve_evidence

        def mutate(*args, **kwargs):
            self.path.write_text('    altered\n')
            return original(*args, **kwargs)

        with patch('src.analysis.citation_repair.resolve_evidence', side_effect=mutate), self.assertRaises(SourceChanged):
            repair_citations('study', {'evidence': [self.pointer(1, 1, '        target')]}, self.context)

    def test_compare_does_not_read_sources(self):
        value = {'evidence': [self.pointer(1, 1, 'quote')]}
        with patch('src.analysis.evidence._read_confined', side_effect=AssertionError('Unexpected source read')):
            candidate, journal = repair_citations('compare', value, {})
            self.assertEqual(candidate, value)
            self.assertFalse(journal['changes'])


if __name__ == '__main__':
    unittest.main()
