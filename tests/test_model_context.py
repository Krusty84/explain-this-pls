#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.reports.document_rendering import materialize_study
from fixtures.ledger_response import response
from src.analysis.ledger import review_context, verify_review_context
from src.model.model_context import (build_model_prompt, input_measurements, mcp_source_index,
                                     on_demand_source_index, project_model_context, serialize_model_input)


def decode_source_context(value, maps=None):
    result = copy.deepcopy(value)
    file_columns = result.pop('file_columns')
    symbol_columns = result.pop('symbol_columns', [])
    file_tables = result.pop('file_value_tables', {})
    symbol_tables = result.pop('symbol_value_tables', {})
    result.pop('index_format', None)
    delivery = result.pop('delivery', 'full')
    maps_root = result.pop('maps_root', None)
    result['files'] = [dict(zip(file_columns, row, strict=True)) for row in result['files']]
    for record in result['files']:
        for column, table in file_tables.items():
            record[column] = table[record[column]]
        columns, tables = symbol_columns, symbol_tables
        if delivery == 'on-demand':
            name, count = record.pop('symbols_ref'), record.pop('symbol_count')
            shard = {'symbols': [], 'symbol_columns': [], 'symbol_value_tables': {}}
            if name:
                shard = maps[name] if maps is not None else json.loads((Path(maps_root) / name).read_text(encoding='utf-8'))
            record['symbols'] = shard['symbols']
            columns, tables = shard['symbol_columns'], shard['symbol_value_tables']
            if len(record['symbols']) != count:
                raise ValueError('Symbol count does not match the map.')
            if name and (shard['source_id'], shard['path']) != (record['source_id'], record['path']):
                raise ValueError('Symbol map identity mismatch.')
        record['symbols'] = [dict(zip(columns, row, strict=True)) for row in record['symbols']]
        for symbol in record['symbols']:
            for column, table in tables.items():
                symbol[column] = table[symbol[column]]
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
        wire['report_sections'][0]['blocks'][0] = {'markdown': table, 'claim_ids': [c['id'] for c in wire['claims']]}
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
        for stage, variant in (('study', 'study'), ('study', 'align'), ('review', 'review')):
            with self.subTest(stage=stage, variant=variant):
                current = context | {'prompt_variant': variant}
                projected = project_model_context(stage, current)
                wire = json.loads(serialize_model_input(projected))
                rows = wire['source_context']
                self.assertEqual(rows['file_columns'], ['source_id', 'path', 'parser', 'language', 'encoding',
                    'total_lines', 'map_status', 'map_reason', 'symbols'])
                self.assertEqual(rows['symbol_columns'], ['name', 'kind', 'parent', 'start_line', 'end_line'])
                self.assertEqual(rows['index_format'], 'dictionary')
                self.assertEqual(rows['delivery'], 'full')
                self.assertEqual(rows['file_value_tables']['source_id'], ['source-002', 'source-001'])
                self.assertEqual(rows['file_value_tables']['map_reason'], [None, 'parse_error'])
                self.assertEqual(rows['symbol_value_tables'],
                                 {'kind': ['function', 'class', 'method'], 'parent': ['Класс.метод', None, 'Класс']})
                self.assertEqual(rows['files'][0], [0, 'модуль/ёж🐍.py', 0, 0, 0, 12, 0, 0, [
                        ['внутри', 0, 0, 9, 10], ['Класс', 1, 1, 1, 12], ['метод', 2, 2, 7, 11]]])
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
                self.assertEqual(set(projected), set(source) | {
                    'file_columns', 'symbol_columns', 'file_value_tables', 'symbol_value_tables', 'index_format', 'delivery'})

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
                prompt = build_model_prompt('', context_json, '{}', source_index=True)
                measured = input_measurements('', context_json, '{}', prompt,
                    source_context=source, transmitted_source_context=projected)
                source_bytes = len(json.dumps(source, ensure_ascii=False).encode('utf-8'))
                self.assertEqual(measured['source_index']['original_utf8_bytes'], source_bytes)
                self.assertEqual(measured['source_index']['transmitted_utf8_bytes'], len(encoded))
                self.assertEqual(measured['source_index']['packed_full_index_utf8_bytes'], len(encoded))
                self.assertEqual(measured['source_index']['initial_index_utf8_bytes'], len(encoded))
                self.assertEqual(measured['source_index']['stored_map_utf8_bytes'], 0)
                self.assertEqual(measured['source_index']['delivery'], 'full')
                self.assertEqual(measured['source_index']['saved_utf8_bytes'], source_bytes - len(encoded))
                self.assertAlmostEqual(measured['source_index']['reduction_percent'],
                    (source_bytes - len(encoded)) * 100 / source_bytes, places=2)

    def test_on_demand_preserves_file_order_errors_and_self_contained_maps(self):
        def file(source_id, path, symbols, **extra):
            return dict(source_id=source_id, path=path, parser='python:3.12', language='python',
                        encoding='utf-8', total_lines=9, map_status='complete', map_reason=None,
                        symbols=symbols) | extra
        symbols = [dict(name='метод🐍', kind='method', parent='Класс', start_line=8, end_line=9),
                   dict(name='Класс', kind='class', parent=None, start_line=1, end_line=9)]
        source = {'mode': 'index-only', 'fragments': [], 'files': [
            file('source-002', 'same.py', symbols),
            file('source-001', 'empty.py', [], total_lines=0),
            file('source-001', 'same.py', symbols),
            file('source-001', 'bad.py', [], parser=None, encoding=None, total_lines=None,
                 map_status='unavailable', map_reason='decoding_error')],
            'omissions': {'maps': 1, 'reasons': {'decoding_error': 1}, 'incomplete': True}}
        before = copy.deepcopy(source)
        full = project_model_context('study', {'source_context': source})['source_context']
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            wire, maps = on_demand_source_index(full, root)
            self.assertEqual(on_demand_source_index(source, root), (wire, maps))
            self.assertEqual(decode_source_context(wire, maps), source)
            self.assertEqual(set(maps), {'M-000003.json', 'M-000004.json'})
            self.assertNotIn('symbol_columns', wire)
            self.assertNotIn('symbol_value_tables', wire)
            self.assertNotIn('symbols', wire['file_columns'])
            self.assertEqual(wire['files'][0][-2:], [2, 'M-000004.json'])
            self.assertEqual(wire['files'][1][-2:], [0, None])
            self.assertEqual(wire['files'][2][-2:], [2, 'M-000003.json'])
            self.assertEqual(wire['files'][3][-2:], [0, None])
            self.assertEqual(wire['maps_root'], str(root))
            self.assertEqual(project_model_context('study', {'source_context': wire})['source_context'], wire)
            stored_bytes = 0
            for name, value in maps.items():
                content = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
                stored_bytes += len(content)
                (root / name).write_bytes(content)
                self.assertEqual(value['symbol_value_tables'], {'kind': ['method', 'class'], 'parent': ['Класс', None]})
            self.assertEqual(decode_source_context(wire), source)
            context_json = serialize_model_input({'source_context': wire})
            measured = input_measurements('', context_json, '{}', context_json,
                source_context=source, transmitted_source_context=wire, stored_map_utf8_bytes=stored_bytes)['source_index']
            self.assertEqual(measured['delivery'], 'on-demand')
            self.assertEqual(measured['stored_map_utf8_bytes'], stored_bytes)
            self.assertEqual(measured['packed_full_index_utf8_bytes'], len(serialize_model_input(full).encode('utf-8')))
            self.assertEqual(measured['initial_index_utf8_bytes'], len(serialize_model_input(wire).encode('utf-8')))
            maps['M-000004.json']['symbols'][0][0] = 'changed'
            wire['files'][0][1] = 'changed'
        self.assertEqual(source, before)
        self.assertEqual(decode_source_context(full), before)

    def test_on_demand_defers_all_symbols_and_preserves_long_names(self):
        symbols = [dict(name='a' * 1500 if number == 0 else f'operation{number}', kind='function',
                        parent=None, start_line=number + 1, end_line=number + 1) for number in range(10000)]
        source = {'mode': 'index-only', 'files': [dict(source_id='source-001', path='large.py',
            parser='python:3.12', language='python', encoding='utf-8', total_lines=10000,
            map_status='partial', map_reason='parse_error', symbols=symbols)], 'fragments': [],
            'omissions': {'maps': 1, 'reasons': {'parse_error': 1}, 'incomplete': True}}
        with tempfile.TemporaryDirectory() as raw:
            wire, maps = on_demand_source_index(source, Path(raw).resolve())
        self.assertEqual(len(maps), 1)
        self.assertEqual(wire['files'][0][-2:], [10000, 'M-000001.json'])
        self.assertEqual(wire['omissions'], source['omissions'])
        self.assertNotIn('a' * 1500, serialize_model_input(wire))
        self.assertEqual(decode_source_context(wire, maps), source)
        self.assertLess(len(serialize_model_input(wire)), len(serialize_model_input(source)) / 50)

    def test_on_demand_empty_inventory_and_relative_root(self):
        source = {'mode': 'index-only', 'files': [], 'fragments': [],
                  'omissions': {'maps': 0, 'reasons': {}, 'incomplete': False}}
        with tempfile.TemporaryDirectory() as raw:
            wire, maps = on_demand_source_index(source, Path(raw).resolve())
            self.assertEqual(maps, {})
            self.assertEqual(decode_source_context(wire, maps), source)
        with self.assertRaisesRegex(ValueError, 'absolute'):
            on_demand_source_index(source, 'relative/maps')

    def test_mcp_overview_size_is_bounded_without_file_or_symbol_rows(self):
        from scripts.measure_prompt_sizes import synthetic_index
        for count in (1000, 5000):
            with self.subTest(count=count):
                index, _ = synthetic_index(count)
                before = copy.deepcopy(index)
                demand, _ = on_demand_source_index(index, '/private/maps')
                catalog = [{'id': 'source-001', 'root': '.'}, {'id': 'source-002', 'root': 'empty'}]
                overview = mcp_source_index(demand, catalog)
                encoded = serialize_model_input(overview)
                self.assertLess(len(encoded.encode()), 8192)
                self.assertLess(len(encoded), len(serialize_model_input(demand)) / 20)
                for field in ('files', 'file_columns', 'symbol_columns', 'maps_root'):
                    self.assertNotIn(field, overview)
                self.assertNotIn('file00001.py', encoded)
                self.assertNotIn('entry00001', encoded)
                self.assertEqual(overview['summary']['files'], count)
                self.assertEqual(overview['sources'], [{'source_id': 'source-001', 'root': '.', 'files': count},
                                                     {'source_id': 'source-002', 'root': 'empty', 'files': 0}])
                self.assertEqual(project_model_context('study', {'source_context': overview})['source_context'], overview)
                self.assertEqual(index, before)

    def test_mcp_overview_aggregates_labels_and_reports_errors(self):
        from scripts.measure_prompt_sizes import synthetic_index
        index, _ = synthetic_index(100)
        for number, file in enumerate(index['files']):
            file['path'] = ('directory' + str(number) + 'x' * 500) + '/file.' + str(number)
        index['files'][0].update(map_status='unavailable', map_reason='decoding_error')
        index['files'][1].update(map_status='unavailable', map_reason='read_error')
        index['files'][2].update(map_status='partial', map_reason='parse_error')
        demand, _ = on_demand_source_index(index, '/private/maps')
        overview = mcp_source_index(demand)
        summary = overview['summary']
        self.assertLessEqual(len(serialize_model_input(overview).encode()), 8192)
        self.assertEqual(sum(row['files'] for row in summary['top_directories']) + summary['other_directory_files'], 100)
        self.assertEqual(sum(row['files'] for row in summary['file_types']) + summary['other_type_files'], 100)
        self.assertEqual((summary['indexing_errors'], summary['decoding_errors'], summary['read_errors']), (1, 1, 1))

    def test_mcp_measurement_uses_stdio_and_confirms_complete_inventory(self):
        from scripts.measure_prompt_sizes import mcp_measurements
        sample = mcp_measurements((8,))['samples'][0]
        self.assertEqual(sample['mcp']['enumerated_files'], 8)
        self.assertEqual(sample['mcp']['unique_file_paths'], 8)
        self.assertEqual(sample['mcp']['matches'], 8)
        self.assertEqual(sample['mcp']['unique_source_lines'], 2)
        self.assertEqual(sample['mcp']['repeated_source_lines'], 2)
        self.assertGreater(sample['mcp']['result_json_utf8_bytes'], sample['mcp']['text_content_utf8_bytes'])
        for tool in ('list_files', 'search_text', 'search_symbols', 'read_symbol', 'read_source'):
            self.assertGreater(sample['mcp']['operations'][tool]['seconds'], 0)
