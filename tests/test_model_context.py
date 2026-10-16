#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import copy
import json
import unittest

from src.reports.document_rendering import materialize_study
from fixtures.ledger_response import response
from src.analysis.ledger import review_context, verify_review_context
from src.model.model_context import (build_model_prompt, input_measurements,
                                     project_model_context, serialize_model_input)


def decode_source_context(value):
    result = copy.deepcopy(value)
    file_columns = result.pop('file_columns')
    symbol_columns = result.pop('symbol_columns')
    result['files'] = [dict(zip(file_columns, row, strict=True)) for row in result['files']]
    for record in result['files']:
        record['symbols'] = [dict(zip(symbol_columns, row, strict=True)) for row in record['symbols']]
    return result


class ModelContextTests(unittest.TestCase):
    def test_sizes_are_exact_bytes_and_characters_without_token_claims(self):
        measured = input_measurements('АБ', '{"x":1}', '{}', 'АБ test')
        self.assertEqual(measured['template'], {'characters': 2, 'utf8_bytes': 4})
        self.assertEqual(measured['projected_context']['utf8_bytes'], 7)
        self.assertEqual(measured['schema']['utf8_bytes'], 2)
        self.assertIsNone(measured['tokens'])
        self.assertTrue(measured['text_schema_copy'])
        correction = input_measurements('unused template', '{}', '{}', 'correction', correction=True)
        self.assertEqual(correction['template']['utf8_bytes'], 0)
        self.assertFalse(correction['template_applied'])
        self.assertNotIn('source_index', measured)

    def test_compact_serialization_preserves_unicode_and_string_whitespace(self):
        value = {'text': '  ёж🐍, : \n\t', 'items': [None, 0, False, 'a, b: c']}
        encoded = serialize_model_input(value)
        self.assertEqual(encoded, '{"text":"  ёж🐍, : \\n\\t","items":[null,0,false,"a, b: c"]}')
        self.assertEqual(json.loads(encoded), value)

    def test_twenty_claim_table_is_sent_once_without_changing_frozen_context(self):
        context = {'branch': 'main', 'source_commit': 'abc'}
        wire = response(context)
        wire.setdefault('coverage', [])
        wire['claims'] = [wire['claims'][0] | {'id': f'C-{n:03}', 'statement': f'Assertion {n}'}
                          for n in range(1, 21)]
        table = '| Key | Value |\n| --- | --- |\n' + ''.join(f'| {n} | unique row {n} |\n' for n in range(1, 21))
        wire['report_sections'][0]['blocks'] = [{'markdown': table, 'claim_ids': [c['id'] for c in wire['claims']]}]
        context = review_context(materialize_study(wire), context)
        original = copy.deepcopy(context)
        projected = project_model_context('review', context)
        payload = json.dumps(projected, ensure_ascii=False)
        self.assertEqual(payload.count('unique row 20'), 1)
        self.assertEqual(payload.count('Assertion 20'), 1)
        self.assertEqual(len(projected['claim_registry']), 20)
        self.assertNotIn('claims', projected['architecture_document'])
        for claim in projected['claim_registry']:
            self.assertNotIn('quote', claim['document_locators'][0])
        for key in ('block_map', 'materialization_provenance', 'normalization_provenance'):
            self.assertNotIn(key, projected['architecture_document'])
        self.assertEqual(context, original)
        verify_review_context(context)
        projected['claim_registry'][0]['statement'] = 'edited'
        self.assertEqual(context, original)

    def test_compare_and_revision_retain_content_once(self):
        pair = {'study': {'report_markdown': 'document', 'claims': [{'id': 'C-001',
            'statement': 'statement', 'document_locators': [{'quote': 'document', 'start_line': 1, 'end_line': 1}]}]},
            'review': {'report_markdown': 'review', 'claim_registry': [{'statement': 'statement'}],
                       'findings': [{'impact': 'issue'}], 'limitations': ['limit']}}
        context = {'branches': [pair], 'previous_revision': pair}
        original = copy.deepcopy(context)
        projected = project_model_context('compare', context)
        for item in (projected['branches'][0], projected['previous_revision']):
            payload = json.dumps(item)
            self.assertEqual(payload.count('"document"'), 1)
            self.assertNotIn('claim_registry', item['review'])
            self.assertEqual(item['review']['findings'], pair['review']['findings'])
        self.assertEqual(context, original)


    def test_coverage_projection_uses_selectors_without_expanded_inventory(self):
        plan = {'areas': [{'id': 'S-001', 'paths': ['src'], 'entry_paths': ['src/app.py', 'src/link'],
                           'file_paths': ['src/app.py']}], 'exclusions': [], 'plan_sha256': 'frozen'}
        context = {'coverage_plan': plan, 'review_plan': {'coverage_plan': plan, 'plan_sha256': 'review'}}
        before = copy.deepcopy(context)
        projected = project_model_context('review', context)
        self.assertNotIn('coverage_plan', projected['review_plan'])
        self.assertEqual(projected['coverage_plan']['areas'][0],
                         {'id': 'S-001', 'paths': ['src'], 'entry_count': 2, 'file_count': 1})
        self.assertEqual(project_model_context('review', projected), projected)
        self.assertEqual(context, before)

    def test_source_context_rows_preserve_values_order_and_independence(self):
        source = {'mode': 'index-only', 'fragments': [],
            'omissions': {'maps': 1, 'reasons': {'parse_error': 1}, 'incomplete': True},
            'files': [
                {'source_id': 'source-002', 'path': 'модуль/ёж🐍.py', 'parser': 'python:3.12',
                 'language': 'python', 'encoding': 'utf-8', 'total_lines': 12,
                 'map_status': 'complete', 'map_reason': None, 'symbols': [
                     {'name': 'внутри', 'kind': 'function', 'parent': 'Класс.метод', 'start_line': 9, 'end_line': 10},
                     {'name': 'Класс', 'kind': 'class', 'parent': None, 'start_line': 1, 'end_line': 12},
                     {'name': 'метод', 'kind': 'method', 'parent': 'Класс', 'start_line': 7, 'end_line': 11}]},
                {'source_id': 'source-001', 'path': 'a.rs', 'parser': 'ast-grep-py:0.1',
                 'language': 'rust', 'encoding': 'utf-8', 'total_lines': 3,
                 'map_status': 'partial', 'map_reason': 'parse_error', 'symbols': []}]}
        context = {'source_context': source, 'priority_scenarios': ['Сценарий 🐍'],
                   'source_decoding': {'rules': []}}
        before = copy.deepcopy(context)
        for stage, variant in (('study', 'study'), ('study', 'revise'), ('review', 'review')):
            with self.subTest(stage=stage, variant=variant):
                current = context | {'prompt_variant': variant}
                projected = project_model_context(stage, current)
                wire = json.loads(serialize_model_input(projected))
                rows = wire['source_context']
                self.assertEqual(rows['file_columns'], ['source_id', 'path', 'parser', 'language', 'encoding',
                    'total_lines', 'map_status', 'map_reason', 'symbols'])
                self.assertEqual(rows['symbol_columns'], ['name', 'kind', 'parent', 'start_line', 'end_line'])
                self.assertEqual(rows['files'][0], ['source-002', 'модуль/ёж🐍.py', 'python:3.12',
                    'python', 'utf-8', 12, 'complete', None, [
                        ['внутри', 'function', 'Класс.метод', 9, 10],
                        ['Класс', 'class', None, 1, 12], ['метод', 'method', 'Класс', 7, 11]]])
                self.assertEqual(decode_source_context(rows), source)
                self.assertEqual(wire['priority_scenarios'], context['priority_scenarios'])
                self.assertEqual(wire['source_decoding'], context['source_decoding'])
                self.assertEqual(project_model_context(stage, projected), projected)
                rows['files'][0][-1][0][0] = 'changed'
                self.assertEqual(context, before)

    def test_source_context_rows_preserve_empty_and_unavailable_files(self):
        files = [
            {'source_id': 'source-001', 'path': 'empty.py', 'parser': 'python:3.12',
             'language': 'python', 'encoding': 'utf-8', 'total_lines': 0,
             'map_status': 'complete', 'map_reason': None, 'symbols': []},
            {'source_id': 'source-001', 'path': 'binary.dat', 'parser': None,
             'language': None, 'encoding': None, 'total_lines': None,
             'map_status': 'unavailable', 'map_reason': 'binary_content', 'symbols': []},
            {'source_id': 'source-001', 'path': 'unreadable.py', 'parser': None,
             'language': 'python', 'encoding': None, 'total_lines': None,
             'map_status': 'unavailable', 'map_reason': 'read_error', 'symbols': []},
            {'source_id': 'source-001', 'path': 'encoding.py', 'parser': None,
             'language': 'python', 'encoding': 'utf-16', 'total_lines': None,
             'map_status': 'unavailable', 'map_reason': 'decoding_error', 'symbols': []}]
        for selected in ([], files):
            with self.subTest(files=len(selected)):
                source = {'mode': 'index-only', 'files': selected, 'fragments': [],
                    'omissions': {'maps': max(0, len(selected) - 1), 'reasons': {
                        record['map_reason']: 1 for record in selected if record['map_reason']},
                        'incomplete': bool(selected)}}
                projected = project_model_context('study', {'source_context': source})['source_context']
                self.assertEqual(decode_source_context(projected), source)
                self.assertEqual(set(projected), set(source) | {'file_columns', 'symbol_columns'})

    def test_large_source_context_rows_retain_every_file_and_symbol(self):
        for count in (1000, 5000):
            with self.subTest(files=count):
                source = {'mode': 'index-only', 'files': [
                    {'source_id': 'source-001', 'path': f'пакет/file{i:05d}.py', 'parser': 'python:3.12',
                     'language': 'python', 'encoding': 'utf-8', 'total_lines': 2,
                     'map_status': 'complete', 'map_reason': None, 'symbols': [
                         {'name': f'operation{i}', 'kind': 'function', 'parent': None,
                          'start_line': 1, 'end_line': 2}]} for i in range(count)],
                    'fragments': [], 'omissions': {'maps': 0, 'reasons': {}, 'incomplete': False}}
                before = copy.deepcopy(source)
                projected = project_model_context('study', {'source_context': source})['source_context']
                encoded = serialize_model_input(projected).encode('utf-8')
                original = json.dumps(source, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                decoded = decode_source_context(json.loads(encoded))
                self.assertEqual(decoded, before)
                self.assertEqual(len(decoded['files']), count)
                self.assertEqual(sum(len(record['symbols']) for record in decoded['files']), count)
                self.assertLess(len(encoded), len(original))
                self.assertEqual(source, before)
                context_json = serialize_model_input({'source_context': projected})
                prompt = build_model_prompt('', context_json, '{}', 'codex', source_index=True)
                measured = input_measurements('', context_json, '{}', prompt,
                    source_context=source, transmitted_source_context=projected)
                source_bytes = len(json.dumps(source, ensure_ascii=False).encode('utf-8'))
                self.assertEqual(measured['source_index']['original_utf8_bytes'], source_bytes)
                self.assertEqual(measured['source_index']['transmitted_utf8_bytes'], len(encoded))
                self.assertEqual(measured['source_index']['saved_utf8_bytes'], source_bytes - len(encoded))
                self.assertAlmostEqual(measured['source_index']['reduction_percent'],
                    (source_bytes - len(encoded)) * 100 / source_bytes, places=2)
