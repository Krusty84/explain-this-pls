# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import asyncio
import base64
import codecs
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.analysis.evidence import SourceChanged, canonical, sha, stamp
from src.analysis.source_context import SourceContextBuilder, parse_symbols
from src.mcp.service import SourceService, prepare_manifest
from src.mcp import SOURCE_TOOLS
from src.model.model_context import on_demand_source_index
from src.runtime.artifacts import save_json

SDK_AVAILABLE = importlib.util.find_spec('mcp') is not None
SERVER = Path(__file__).resolve().parents[1] / 'src/mcp/server.py'
FIXTURES = Path(__file__).parent / 'fixtures/source_context'


class SourceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.root = self.directory / 'source'
        self.root.mkdir()
        self.attempt = self.directory / 'attempt'

    def context(self, files):
        entries = []
        for path, content in files.items():
            blob = content.encode('utf-8') if isinstance(content, str) else content
            destination = self.root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(blob)
            entries.append(dict(path=path, type='file', size=len(blob), sha256=sha(blob)))
        return dict(source_mode='folder', source_directory=str(self.root), source_fingerprint='pinned',
                    _inventory={'entries': entries, 'source_fingerprint': 'pinned'})

    def service(self, context, *, max_response_bytes=65536, metadata=None):
        index, _ = SourceContextBuilder().prepare('study', context, expected_metadata=metadata)
        packed, maps = on_demand_source_index(index, self.attempt / 'source-maps')
        descriptors = {}
        for name, value in maps.items():
            path = self.attempt / 'source-maps' / name
            save_json(path, value)
            blob = path.read_bytes()
            descriptors[name] = dict(path=str(path), bytes=len(blob), sha256=sha(blob))
        manifest = prepare_manifest(context, index, packed, descriptors, attempt_id='attempt-test',
                                    expected_metadata=metadata)
        self.manifest_path = self.attempt / 'mcp/manifest.json'
        save_json(self.manifest_path, manifest)
        self.manifest_sha256 = sha(self.manifest_path.read_bytes())
        return SourceService(self.manifest_path, self.manifest_sha256, max_response_bytes=max_response_bytes)

    def test_language_maps_match_existing_parser_without_reparse(self):
        files = {f'sample.{extension}': (FIXTURES / f'sample.{extension}').read_bytes()
                 for extension in ('py', 'js', 'ts', 'c', 'cpp', 'swift')}
        context = self.context(files)
        expected = []
        for path, blob in files.items():
            parsed = parse_symbols(path, blob.decode())
            expected.extend(dict(source_id='source-001', path=path, **symbol, map_status=parsed['map_status'])
                            for symbol in parsed['symbols'])
        service = self.service(context)
        with patch('src.analysis.source_context.parse_symbols', side_effect=AssertionError('Reparsed source')):
            for name in {symbol['name'] for symbol in expected}:
                actual = service.search_symbols(name, limit=1000)['results']
                exact = [result for result in actual if result['name'] == name]
                self.assertTrue(all(symbol['symbol_id'] for symbol in exact))
                self.assertCountEqual([{k: v for k, v in symbol.items() if k != 'symbol_id'} for symbol in exact],
                                      [symbol for symbol in expected if symbol['name'] == name])
            service.read_source('source-001', 'sample.py', 1, 2)

    def test_exact_matches_first_partial_filters_and_deterministic_pages(self):
        context = self.context({'a.py': 'def runner(): pass\ndef run(): pass\n',
                                'nested/b.py': 'class Run:\n    def run(self): pass\n'})
        service = self.service(context)
        first = service.search_symbols('run', limit=1)
        self.assertEqual(first['results'][0]['name'], 'run')
        self.assertTrue(first['has_more'])
        second = service.search_symbols('run', limit=1, cursor=first['cursor'])
        third = service.search_symbols('run', limit=1, cursor=second['cursor'])
        self.assertEqual(third['results'][0]['name'], 'runner')
        self.assertFalse(third['has_more'])
        self.assertIsNone(third['cursor'])
        filtered = service.search_symbols('run', path='nested', kind='method', source_id='source-001')
        self.assertEqual([symbol['path'] for symbol in filtered['results']], ['nested/b.py'])
        self.assertEqual(service.search_symbols('run', limit=1)['cursor'], first['cursor'])
        self.assertEqual(service.search_symbols('different', cursor=first['cursor'])['error']['code'], 'INVALID_CURSOR')

    def test_cursor_is_bound_to_current_attempt(self):
        context = self.context({'a.py': 'def one(): pass\ndef other(): pass\n'})
        first = self.service(context).search_symbols('o', limit=1)
        manifest = json.loads(self.manifest_path.read_bytes())
        manifest['attempt_id'] = 'another-attempt'
        save_json(self.manifest_path, manifest)
        service = SourceService(self.manifest_path, sha(self.manifest_path.read_bytes()))
        self.assertEqual(service.search_symbols('o', cursor=first['cursor'])['error']['code'], 'INVALID_CURSOR')

    def test_exact_original_encoding_coordinates_and_no_final_lf(self):
        files = {'utf8.txt': codecs.BOM_UTF8 + 'один\r\nдва\r\nтри'.encode(),
                 'legacy.txt': 'Привет\r\nмир'.encode('cp1251'),
                 'wide.txt': codecs.BOM_UTF16_LE + 'Hello\r\nworld'.encode('utf-16-le'),
                 'empty.txt': b'', 'lone-cr.txt': 'left\rright'}
        context = self.context(files)
        context['source_decoding'] = {'rules': [{'path': 'legacy.txt', 'encoding': 'cp1251'}]}
        service = self.service(context)
        for path, expected, codec, start, end in [('utf8.txt', 'два\nтри', 'utf-8', 2, 3),
                                                 ('legacy.txt', 'Привет\nмир', 'cp1251', 1, 2),
                                                 ('wide.txt', 'Hello\nworld', 'utf-16-le', 1, 2),
                                                 ('lone-cr.txt', 'left\rright', 'utf-8', 1, 1)]:
            with self.subTest(path=path):
                result = service.read_source('source-001', path, start, end)
                self.assertEqual(result['text'], expected)
                self.assertEqual(result['encoding'], codec)
                self.assertEqual((result['start_line'], result['end_line']), (start, end))
                self.assertEqual(result['sha256'], sha((self.root / path).read_bytes()))
                self.assertTrue(result['complete'])
                self.assertFalse(result['has_more'])
                self.assertIsNone(result['next_start_line'])
        empty = service.read_source('source-001', 'empty.txt', 1, 1)
        self.assertEqual((empty['text'], empty['total_lines'], empty['start_line'], empty['end_line']), ('', 0, 1, 0))
        self.assertTrue(empty['complete'])
        self.assertEqual(service.read_source('source-001', 'utf8.txt', 1, 4)['error']['code'], 'OUT_OF_RANGE')

    def test_large_ranges_page_at_whole_lines_and_long_line_is_explicit_error(self):
        content = ''.join(f'{number}: ' + 'ю"\\' * 80 + '\n' for number in range(100))
        service = self.service(self.context({'large.txt': content, 'long.txt': 'x' * 5000}), max_response_bytes=1600)
        start, pieces = 1, []
        while True:
            result = service.read_source('source-001', 'large.txt', start, 100)
            self.assertNotIn('error', result)
            self.assertLessEqual(len(canonical(result)), 1600)
            self.assertEqual(result['start_line'], start)
            pieces.append(result['text'])
            if not result['has_more']:
                self.assertTrue(result['complete'])
                break
            self.assertFalse(result['complete'])
            self.assertEqual(result['next_start_line'], result['end_line'] + 1)
            start = result['next_start_line']
        self.assertEqual(''.join(pieces), content)
        error = service.read_source('source-001', 'long.txt', 1, 1)
        self.assertEqual(error['error']['code'], 'LINE_TOO_LARGE')
        self.assertNotIn('text', error)

    def test_large_symbol_results_page_without_hidden_truncation(self):
        context = self.context({'a.py': ''.join(f'def symbol{number:03d}(): pass\n' for number in range(100))})
        service = self.service(context, max_response_bytes=1400)
        cursor, results = None, []
        while True:
            page = service.search_symbols('symbol', limit=1000, cursor=cursor)
            self.assertLessEqual(len(canonical(page)), 1400)
            results.extend(page['results'])
            if not page['has_more']:
                break
            self.assertTrue(page['results'])
            cursor = page['cursor']
        self.assertEqual([item['name'] for item in results], [f'symbol{number:03d}' for number in range(100)])

    def test_parse_errors_unknown_formats_binary_and_decode_errors_keep_files(self):
        context = self.context({'bad.py': 'def invalid(:\n', 'unknown.custom': 'plain\n',
                                'binary.bin': b'abc\x00def', 'invalid.txt': b'\xffinvalid'})
        service = self.service(context)
        self.assertEqual(len(service.files), 4)
        self.assertEqual(service.files['source-001', 'bad.py']['map_reason'], 'parse_error')
        self.assertEqual(service.files['source-001', 'unknown.custom']['map_reason'], 'unknown_language')
        self.assertEqual(service.search_symbols('invalid')['results'], [])
        self.assertEqual(service.read_source('source-001', 'bad.py', 1, 1)['text'], 'def invalid(:\n')
        self.assertEqual(service.read_source('source-001', 'unknown.custom', 1, 1)['text'], 'plain\n')
        self.assertEqual(service.read_source('source-001', 'binary.bin', 1, 1)['error']['code'], 'BINARY_CONTENT')
        self.assertEqual(service.read_source('source-001', 'invalid.txt', 1, 1)['error']['code'], 'DECODE_ERROR')

    def test_invalid_paths_exclusions_and_inventory_membership(self):
        service = self.service(self.context({'allowed.txt': 'allowed\n'}))
        (self.root / 'excluded.txt').write_text('private\n')
        for path in ('../excluded.txt', '/etc/passwd', str(self.root / 'allowed.txt'),
                     'sub/../allowed.txt', 'sub\\allowed.txt', '.git/config'):
            with self.subTest(path=path):
                result = service.read_source('source-001', path, 1, 1)
                self.assertEqual(result['error']['code'], 'INVALID_ARGUMENT')
                self.assertNotIn('text', result)
        self.assertEqual(service.read_source('source-001', 'excluded.txt', 1, 1)['error']['code'], 'NOT_IN_INVENTORY')
        self.assertEqual(service.read_source('source-999', 'allowed.txt', 1, 1)['error']['code'], 'UNKNOWN_SOURCE')

    def test_symlink_file_and_directory_do_not_follow(self):
        service = self.service(self.context({'allowed.txt': 'allowed\n', 'sub/inside.txt': 'inside\n'}))
        outside = self.directory / 'outside.txt'
        outside.write_text('private\n')
        (self.root / 'allowed.txt').unlink()
        (self.root / 'allowed.txt').symlink_to(outside)
        self.assertEqual(service.read_source('source-001', 'allowed.txt', 1, 1)['error']['code'], 'UNSAFE_PATH')
        (self.root / 'sub/inside.txt').unlink()
        (self.root / 'sub').rmdir()
        (self.root / 'sub').symlink_to(self.directory, target_is_directory=True)
        self.assertEqual(service.read_source('source-001', 'sub/inside.txt', 1, 1)['error']['code'], 'UNSAFE_PATH')

    def test_nested_submodules_with_identical_paths_are_isolated(self):
        context = self.context({'same.txt': 'root\n', 'sub/same.txt': 'sub\n', 'sub/nested/same.txt': 'nested\n'})
        context.pop('source_mode')
        context.pop('source_directory')
        context.update(repository=str(self.root), branch='main', source_commit='a',
                       submodules=[{'path': 'sub', 'expected_commit': 'b'}, {'path': 'sub/nested', 'expected_commit': 'c'}])
        service = self.service(context)
        for source_id, text in [('source-001', 'root\n'), ('source-002', 'sub\n'), ('source-003', 'nested\n')]:
            self.assertEqual(service.read_source(source_id, 'same.txt', 1, 1)['text'], text)
        for source_id, path in [('source-001', 'sub/same.txt'), ('source-002', 'nested/same.txt')]:
            self.assertEqual(service.read_source(source_id, path, 1, 1)['error']['code'], 'NOT_IN_INVENTORY')

    def test_source_hash_is_checked_even_when_metadata_checker_accepts(self):
        context = self.context({'a.txt': 'original\n'})
        metadata = {'.': stamp(self.root.stat())[:3], 'a.txt': stamp((self.root / 'a.txt').stat())}
        service = self.service(context, metadata=metadata)
        from src.mcp import service as module
        reader = module._read_confined

        def false_metadata(root, relative, *args, **kwargs):
            kwargs.pop('pinned', None)
            return reader(root, relative, *args, **kwargs)

        (self.root / 'a.txt').write_text('modified\n')
        with patch('src.mcp.service._read_confined', side_effect=false_metadata):
            result = service.read_source('source-001', 'a.txt', 1, 1)
        self.assertEqual(result['error']['code'], 'SOURCE_CHANGED')
        self.assertNotIn('text', result)

    def test_pinned_metadata_change_is_rejected_even_with_same_content(self):
        context = self.context({'a.txt': 'original\n'})
        metadata = {'.': stamp(self.root.stat())[:3], 'a.txt': stamp((self.root / 'a.txt').stat())}
        service = self.service(context, metadata=metadata)
        path = self.root / 'a.txt'
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1000000))
        self.assertEqual(service.read_source('source-001', 'a.txt', 1, 1)['error']['code'], 'SOURCE_CHANGED')

    def test_manifest_map_tampering_and_wrong_snapshot_fail_closed(self):
        service = self.service(self.context({'a.py': 'def original(): pass\n'}))
        with self.assertRaises(ValueError):
            SourceService(self.manifest_path, '0' * 64)
        map_path = next((self.attempt / 'source-maps').iterdir())
        saved = json.loads(map_path.read_bytes())
        saved['symbols'][0][0] = 'tampered'
        save_json(map_path, saved)
        for name, arguments in [('search_symbols', {'query': 'original'}), ('search_text', {'query': 'original'}),
                                ('list_files', {}), ('read_symbol', {'symbol_id': 'unknown'})]:
            self.assertEqual(service.call_tool(name, arguments)['error']['code'], 'SOURCE_CHANGED')
        with self.assertRaises(ValueError):
            SourceService(self.manifest_path, self.manifest_sha256)

    def test_private_permissions_are_required_for_manifest_and_maps(self):
        self.service(self.context({'a.py': 'def original(): pass\n'}))
        self.manifest_path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'must be private'):
            SourceService(self.manifest_path, self.manifest_sha256)
        self.manifest_path.chmod(0o600)
        (self.attempt / 'source-maps').chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'must be private'):
            SourceService(self.manifest_path, self.manifest_sha256)

    def test_receipt_symlink_and_hardlink_cannot_write_source(self):
        service = self.service(self.context({'a.txt': 'original\n'}))
        source = self.root / 'a.txt'
        service.receipts_path.symlink_to(source)
        with self.assertRaises(OSError):
            service.read_source('source-001', 'a.txt', 1, 1)
        self.assertEqual(source.read_text(), 'original\n')
        service.receipts_path.unlink()
        os.link(source, service.receipts_path)
        with self.assertRaisesRegex(ValueError, 'must not be linked'):
            service.read_source('source-001', 'a.txt', 1, 1)
        self.assertEqual(source.read_text(), 'original\n')

    def test_single_oversized_symbol_is_explicit_error(self):
        service = self.service(self.context({'a.py': 'def symbol' + 'x' * 2000 + '(): pass\n'}),
                               max_response_bytes=1400)
        result = service.search_symbols('symbol')
        self.assertEqual(result['error']['code'], 'RESULT_TOO_LARGE')
        self.assertNotIn('results', result)

    def test_receipts_bind_exact_response_and_do_not_claim_inspection(self):
        service = self.service(self.context({'a.txt': 'one\ntwo\n'}))
        first = service.read_source('source-001', 'a.txt', 1, 2)
        second = service.read_source('source-001', 'a.txt', 2, 2)
        rows = [json.loads(line) for line in service.receipts_path.read_text().splitlines()]
        for result, row in zip((first, second), rows, strict=True):
            self.assertEqual(row['state'], 'SERVED')
            self.assertEqual(row['inspection'], 'INSPECTED_UNVERIFIED')
            self.assertEqual(row['result_sha256'], sha(canonical(result)))
            self.assertEqual(row['request_id'], result['_meta']['request_id'])
            self.assertEqual(row['text_bytes'], len(result['text'].encode()))
        self.assertEqual(rows[0]['arguments'], dict(source_id='source-001', path='a.txt', start_line=1, end_line=2))
        self.assertNotEqual(first['_meta']['request_id'], second['_meta']['request_id'])

    def test_no_source_writes_or_source_execution(self):
        context = self.context({'a.py': 'raise RuntimeError("must not execute")\n'})
        before = (self.root / 'a.py').read_bytes()
        service = self.service(context)
        with patch('subprocess.run', side_effect=AssertionError('Executed a command')):
            self.assertEqual(service.read_source('source-001', 'a.py', 1, 1)['text'], before.decode())
            self.assertEqual(service.search_symbols('RuntimeError')['results'], [])
            self.assertEqual(service.search_text('RuntimeError')['results'][0]['path'], 'a.py')
            self.assertEqual(service.list_files()['results'][0]['path'], 'a.py')
            self.assertEqual(service.read_symbol('unknown')['error']['code'], 'SYMBOL_NOT_FOUND')
        self.assertEqual((self.root / 'a.py').read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.root / 'a.py'])

    def test_invalid_calls_are_structured_and_receipted(self):
        service = self.service(self.context({'a.txt': 'one\n'}))
        for name, arguments, code in [('unknown', {}, 'UNKNOWN_TOOL'),
                                      ('read_source', {}, 'INVALID_ARGUMENT'),
                                      ('search_symbols', {'query': 'x', 'limit': True}, 'INVALID_ARGUMENT'),
                                      ('search_symbols', {'query': 'x', 'cursor': 'bad'}, 'INVALID_CURSOR'),
                                      ('search_text', {'query': ''}, 'INVALID_ARGUMENT'),
                                      ('search_text', {'query': 'x', 'case_sensitive': 1}, 'INVALID_ARGUMENT'),
                                      ('list_files', {'path': '../outside'}, 'INVALID_ARGUMENT'),
                                      ('list_files', {'limit': False}, 'INVALID_ARGUMENT'),
                                      ('read_symbol', {'symbol_id': []}, 'INVALID_ARGUMENT'),
                                      ('read_source', dict(source_id='source-001', path='a.txt', start_line=0, end_line=1), 'INVALID_ARGUMENT')]:
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(service.call_tool(name, arguments)['error']['code'], code)

    @unittest.skipUnless(SDK_AVAILABLE, 'Official MCP SDK is not installed; real stdio integration is unverified.')
    def test_real_sdk_stdio_client_receives_full_text_with_isolated_python(self):
        service = self.service(self.context({'a.py': 'def visible():\n    return "actual source body"\n'}))

        async def exercise():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            parameters = StdioServerParameters(command=sys.executable,
                args=['-I', '-B', str(SERVER), '--manifest', str(self.manifest_path), '--sha256', self.manifest_sha256])
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({tool.name for tool in tools.tools}, set(SOURCE_TOOLS))
                    for tool in tools.tools:
                        self.assertTrue(tool.annotations.readOnlyHint)
                        self.assertFalse(tool.annotations.destructiveHint)
                        self.assertTrue(tool.annotations.idempotentHint)
                        self.assertFalse(tool.annotations.openWorldHint)
                    search = await session.call_tool('search_symbols', {'query': 'visible'})
                    search_text = json.loads(search.content[0].text)
                    self.assertEqual(search_text['results'][0]['name'], 'visible')
                    result = await session.call_tool('read_source', dict(source_id='source-001', path='a.py', start_line=1, end_line=2))
                    text = json.loads(result.content[0].text)
                    self.assertEqual(text['text'], 'def visible():\n    return "actual source body"\n')
                    self.assertEqual(text, result.structuredContent)
                    self.assertFalse(result.isError)
                    symbol = await session.call_tool('read_symbol', {'symbol_id': search_text['results'][0]['symbol_id']})
                    self.assertEqual(json.loads(symbol.content[0].text)['text'], text['text'])
                    self.assertEqual(json.loads(symbol.content[0].text), symbol.structuredContent)
                    found = await session.call_tool('search_text', {'query': 'actual source body'})
                    self.assertEqual(json.loads(found.content[0].text)['results'][0]['line'], 2)
                    files = await session.call_tool('list_files', {})
                    self.assertEqual(json.loads(files.content[0].text)['results'][0]['path'], 'a.py')
                    invalid = await session.call_tool('read_source', dict(source_id='source-001', path='../outside', start_line=1, end_line=1))
                    self.assertTrue(invalid.isError)
                    self.assertEqual(json.loads(invalid.content[0].text)['error']['code'], 'INVALID_ARGUMENT')
        asyncio.run(exercise())
        self.assertEqual(len(service.receipts_path.read_text().splitlines()), 6)

    def test_text_search_covers_unmapped_sql_config_parse_errors_and_encodings(self):
        service = self.service(self.context({'query.sql': 'SELECT Привет FROM records;\r\n',
            'settings.ini': '[database]\r\nname=Привет\r\n', 'broken.py': 'def broken(: # Привет\n',
            'legacy.txt': 'Привет\r\nмир'.encode('cp1251'), 'empty.txt': '', 'unknown.custom': 'Привет'}))
        # Reprepare with the existing explicit decoding rule.
        context = dict(source_mode='folder', source_directory=str(self.root), source_fingerprint='pinned',
                       _inventory=service.manifest['inventory'],
                       source_decoding={'rules': [{'path': 'legacy.txt', 'encoding': 'cp1251'}]})
        service = self.service(context)
        with patch('src.analysis.source_context.parse_symbols', side_effect=AssertionError('Reparsed source')):
            result = service.search_text('Привет')
        self.assertEqual([row['path'] for row in result['results']],
                         ['broken.py', 'legacy.txt', 'query.sql', 'settings.ini', 'unknown.custom'])
        self.assertEqual(result['errors'], [])
        self.assertTrue(result['complete'])
        legacy = next(row for row in result['results'] if row['path'] == 'legacy.txt')
        self.assertEqual((legacy['line'], legacy['column'], legacy['end_column'], legacy['encoding']), (1, 1, 7, 'cp1251'))
        self.assertEqual(service.search_text('Привет\nмир')['results'][0]['end_line'], 2)
        self.assertEqual(service.search_text('Привет', path='query.sql')['results'][0]['path'], 'query.sql')
        self.assertEqual(service.search_text('SELECT.*')['results'], [])

    def test_text_casefold_coordinates_and_overlapping_matches(self):
        service = self.service(self.context({'a.txt': 'ёж STRAßE\r\naaaa\nпривет\n'}))
        result = service.search_text('strasse', case_sensitive=False)['results'][0]
        self.assertEqual((result['line'], result['column'], result['end_column']), (1, 4, 10))
        self.assertEqual(service.search_text('strasse')['results'], [])
        self.assertEqual(service.search_text('ПРИВЕТ', case_sensitive=False)['results'][0]['line'], 3)
        self.assertEqual([row['column'] for row in service.search_text('aa')['results']], [1, 2, 3])

    def test_large_text_results_keep_every_match_with_bounded_pages(self):
        count = 800
        service = self.service(self.context({'large.txt': ('x' * 200 + ' needle needle\n') * count}))
        cursor, rows, pages = None, [], 0
        while True:
            page = service.search_text('needle', limit=1000, cursor=cursor)
            self.assertNotIn('error', page)
            self.assertLessEqual(len(canonical(page)), 65536)
            self.assertTrue(page['results'])
            rows.extend(page['results'])
            pages += 1
            if not page['has_more']:
                self.assertTrue(page['complete'])
                self.assertIsNone(page['cursor'])
                break
            cursor = page['cursor']
        self.assertGreater(pages, 1)
        self.assertEqual([(row['line'], row['column']) for row in rows],
                         [(number, column) for number in range(1, count + 1) for column in (202, 209)])
        self.assertTrue(all(row['snippet_truncated'] for row in rows))

    def test_search_file_errors_are_paged_and_never_mean_absence(self):
        service = self.service(self.context({'a.invalid': b'\xff', 'b.bin': b'x\x00x',
                                             'c.txt': 'unreadable', 'd.txt': 'needle'}))
        reader = service._source_text
        def read(file):
            if file['path'] == 'c.txt':
                raise PermissionError('denied')
            return reader(file)
        with patch.object(service, '_source_text', side_effect=read):
            first = service.search_text('needle', limit=2)
            self.assertEqual([row['code'] for row in first['errors']], ['DECODE_ERROR', 'BINARY_CONTENT'])
            self.assertTrue(first['has_more'])
            second = service.search_text('needle', limit=2, cursor=first['cursor'])
            self.assertEqual(second['errors'][0]['code'], 'READ_ERROR')
            self.assertEqual(second['results'][0]['path'], 'd.txt')
            self.assertFalse(second['has_more'])
            self.assertFalse(second['complete'])
            self.assertTrue(second['incomplete'])
        (self.root / 'c.txt').unlink()
        result = service.search_text('needle', path='c.txt')
        self.assertEqual(result['errors'][0]['code'], 'NOT_FOUND')

    def test_read_failure_on_continuation_does_not_skip_next_file_matches(self):
        service = self.service(self.context({'a.txt': 'needle needle', 'b.txt': 'needle'}))
        first = service.search_text('needle', limit=1)
        reader = service._source_text
        def read(file):
            if file['path'] == 'a.txt':
                raise PermissionError('denied')
            return reader(file)
        with patch.object(service, '_source_text', side_effect=read):
            failed = service.search_text('needle', limit=1, cursor=first['cursor'])
            self.assertEqual(failed['errors'][0]['path'], 'a.txt')
            self.assertTrue(failed['has_more'])
            last = service.search_text('needle', limit=1, cursor=failed['cursor'])
        self.assertEqual(last['results'][0]['path'], 'b.txt')
        self.assertTrue(last['incomplete'])
        self.assertFalse(last['has_more'])

    def test_list_files_pages_metadata_and_component_prefix(self):
        service = self.service(self.context({'src/a.py': 'def same(): pass\n', 'src/b.sql': 'select 1;\n',
                                             'src/empty.txt': '', 'src2/private.ini': b'\xff'}))
        cursor, rows = None, []
        while True:
            result = service.list_files(limit=1, cursor=cursor)
            rows.extend(result['results'])
            if not result['has_more']:
                self.assertIsNone(result['cursor'])
                break
            cursor = result['cursor']
        self.assertEqual([row['path'] for row in rows], sorted(file['path'] for file in service.file_list))
        self.assertEqual(rows[0]['symbol_count'], 1)
        self.assertEqual(rows[2]['total_lines'], 0)
        self.assertEqual(rows[3]['map_reason'], 'decoding_error')
        self.assertEqual(len(service.list_files(path='src')['results']), 3)
        self.assertEqual(service.list_files(source_id='missing')['error']['code'], 'UNKNOWN_SOURCE')

    def test_cursors_reject_tampering_changed_parameters_and_other_tools(self):
        service = self.service(self.context({'a.py': 'def one(): pass\ndef other(): pass\n', 'b.txt': 'one one'}))
        for tool, arguments in [('list_files', {}), ('search_text', {'query': 'o'}),
                                ('search_symbols', {'query': 'o'})]:
            with self.subTest(tool=tool):
                arguments['limit'] = 1
                first = service.call_tool(tool, arguments)
                self.assertTrue(first['has_more'])
                token = json.loads(base64.urlsafe_b64decode(first['cursor']))
                token['position'] = 999
                changed = base64.urlsafe_b64encode(canonical(token)).decode()
                for cursor in ('bad', changed, first['cursor'][:-1]):
                    self.assertEqual(service.call_tool(tool, dict(arguments, cursor=cursor))['error']['code'], 'INVALID_CURSOR')
                self.assertEqual(service.call_tool(tool, dict(arguments, limit=2, cursor=first['cursor']))['error']['code'], 'INVALID_CURSOR')
                self.assertEqual(service.call_tool(tool, dict(arguments, path='a.py', cursor=first['cursor']))['error']['code'], 'INVALID_CURSOR')
                self.assertEqual(service.list_files(cursor=first['cursor'])['error']['code'], 'INVALID_CURSOR')
        cursor = service.search_text('o', limit=1)['cursor']
        self.assertEqual(service.search_text('o', limit=1, case_sensitive=False, cursor=cursor)['error']['code'], 'INVALID_CURSOR')
        other = SourceService(self.manifest_path, self.manifest_sha256)
        self.assertEqual(other.search_text('o', limit=1, cursor=cursor)['error']['code'], 'INVALID_CURSOR')

    def test_symbol_ids_distinguish_duplicates_and_overloads_without_reparse(self):
        service = self.service(self.context({'a.py': 'def same(): pass\ndef same(): return 2\nclass C:\n    def same(self): pass\n',
                                             'b.cpp': 'int same(int x) { return x; }\nint same() { return 0; }\n'}))
        symbols = service.search_symbols('same')['results']
        identifiers = {symbol['symbol_id'] for symbol in symbols}
        self.assertEqual(len(identifiers), 5)
        with patch('src.analysis.source_context.parse_symbols', side_effect=AssertionError('Reparsed source')):
            for symbol in symbols:
                result = service.read_symbol(symbol['symbol_id'])
                expected = service.read_source(symbol['source_id'], symbol['path'], symbol['start_line'], symbol['end_line'])
                self.assertEqual(result['text'], expected['text'])
                self.assertTrue(result['complete'])
        other = SourceService(self.manifest_path, self.manifest_sha256)
        self.assertEqual({s['symbol_id'] for s in other.search_symbols('same')['results']}, identifiers)

    def test_long_symbol_reads_continue_without_losing_lines(self):
        content = '@decorator\r\ndef large():\r\n' + ('    # Привет ' + 'x' * 100 + '\r\n') * 1000 + '    return 1\r\n'
        context = self.context({'a.py': content.encode('cp1251'), 'b.py': 'def small(): pass\n'})
        context['source_decoding'] = {'rules': [{'path': 'a.py', 'encoding': 'cp1251'}]}
        service = self.service(context)
        symbol = service.search_symbols('large')['results'][0]
        cursor, pieces = None, []
        while True:
            result = service.read_symbol(symbol['symbol_id'], cursor=cursor)
            self.assertNotIn('error', result)
            self.assertLessEqual(len(canonical(result)), 65536)
            self.assertEqual(result['encoding'], 'cp1251')
            self.assertEqual(result['sha256'], sha(content.encode('cp1251')))
            pieces.append(result['text'])
            if not result['has_more']:
                self.assertTrue(result['complete'])
                self.assertIsNone(result['cursor'])
                break
            self.assertFalse(result['complete'])
            cursor = result['cursor']
            small = service.search_symbols('small')['results'][0]
            self.assertEqual(service.read_symbol(small['symbol_id'], cursor=cursor)['error']['code'], 'INVALID_CURSOR')
        self.assertGreater(len(pieces), 1)
        self.assertEqual(''.join(pieces), content.replace('\r\n', '\n'))

    def test_symbol_errors_and_snapshot_change_are_explicit(self):
        context = self.context({'a.py': 'def one(): pass\ndef other(): pass\n'})
        service = self.service(context, max_response_bytes=1500)
        first, second = service.search_symbols('o')['results']
        self.assertEqual(service.read_symbol('unknown')['error']['code'], 'SYMBOL_NOT_FOUND')
        first['start_line'] = None
        service.symbols_by_id[first['symbol_id']]['start_line'] = None
        self.assertEqual(service.read_symbol(first['symbol_id'])['error']['code'], 'SYMBOL_RANGE_UNAVAILABLE')
        self.assertEqual(service.read_symbol(second['symbol_id'], cursor='bad')['error']['code'], 'INVALID_CURSOR')
        (self.root / 'a.py').write_text('def changed(): pass\n')
        self.assertEqual(service.read_symbol(second['symbol_id'])['error']['code'], 'SOURCE_CHANGED')
        self.assertEqual(service.search_text('changed')['error']['code'], 'SOURCE_CHANGED')

    def test_new_tools_respect_inventory_symlinks_and_nested_sources(self):
        context = self.context({'a.py': 'def root(): pass\n', 'sub/a.py': 'def child(): pass\n',
                                'sub/nested/a.py': 'def leaf(): pass\n'})
        context.pop('source_mode')
        context.pop('source_directory')
        context.update(repository=str(self.root), branch='main', source_commit='a',
                       submodules=[{'path': 'sub', 'expected_commit': 'b'}, {'path': 'sub/nested', 'expected_commit': 'c'}])
        service = self.service(context)
        outside = self.directory / 'excluded.py'
        outside.write_text('def secret(): pass\n')
        (self.root / 'excluded.py').symlink_to(outside)
        self.assertEqual(len(service.list_files()['results']), 3)
        self.assertEqual(service.search_text('secret')['results'], [])
        self.assertEqual(service.search_text('leaf', source_id='source-001')['results'], [])
        self.assertEqual(service.search_text('leaf', source_id='source-003')['results'][0]['path'], 'a.py')
        self.assertEqual(service.search_text('leaf', path='../excluded.py')['error']['code'], 'INVALID_ARGUMENT')
        leaf = service.search_symbols('leaf')['results'][0]
        (self.root / 'sub/nested/a.py').unlink()
        (self.root / 'sub/nested/a.py').symlink_to(outside)
        self.assertEqual(service.read_symbol(leaf['symbol_id'])['error']['code'], 'UNSAFE_PATH')
        found = service.search_text('secret')
        self.assertEqual(found['results'], [])
        self.assertEqual(found['errors'][0]['code'], 'UNSAFE_PATH')

    def test_source_changed_during_search_blocks_instead_of_becoming_a_file_gap(self):
        service = self.service(self.context({'a.txt': 'needle'}))
        with patch.object(service, '_source_text', side_effect=SourceChanged('changed')):
            result = service.search_text('needle')
        self.assertEqual(result['error']['code'], 'SOURCE_CHANGED')
        self.assertNotIn('results', result)

    def test_new_tools_apply_real_folder_exclusions_and_gitignore(self):
        from src.analysis.folder import Folder
        self.context({'allowed.py': 'def kept(): pass\n', 'excluded.py': 'def secret(): pass\n',
                      'ignored.sql': 'select secret;\n', '.gitignore': 'ignored.sql\n', '.git/config': 'secret'})
        outside = self.directory / 'outside.py'
        outside.write_text('def secret(): pass\n')
        (self.root / 'link.py').symlink_to(outside)
        folder = Folder(self.root, source_filter={'exclude_paths': ['excluded.py'], 'follow_gitignore': True})
        inventory = folder.snapshot()
        context = dict(source_mode='folder', source_directory=str(self.root), _inventory=inventory,
                       source_fingerprint=inventory['source_fingerprint'])
        service = self.service(context, metadata=folder.metadata)
        self.assertEqual({row['path'] for row in service.list_files()['results']}, {'.gitignore', 'allowed.py'})
        self.assertEqual(service.search_text('secret')['results'], [])
        symbol = service.search_symbols('kept')['results'][0]
        self.assertEqual(service.read_symbol(symbol['symbol_id'])['text'], 'def kept(): pass\n')
        folder.assert_snapshot(inventory['source_fingerprint'])

    def test_symbol_ids_change_with_snapshot_and_oversized_lines_are_explicit(self):
        context = self.context({'a.py': 'def same(): return 1\n'})
        original = self.service(context).search_symbols('same')['results'][0]['symbol_id']
        context = self.context({'a.py': 'def same(): return 2\n'})
        service = self.service(context)
        changed = service.search_symbols('same')['results'][0]['symbol_id']
        self.assertNotEqual(original, changed)
        self.assertEqual(service.read_symbol(original)['error']['code'], 'SYMBOL_NOT_FOUND')
        context = self.context({'a.py': 'def huge(): return "' + 'x' * 70000 + '"\n'})
        service = self.service(context)
        symbol = service.search_symbols('huge')['results'][0]
        self.assertEqual(service.read_symbol(symbol['symbol_id'])['error']['code'], 'LINE_TOO_LARGE')

    def test_prepared_map_with_unusable_coordinates_has_explicit_symbol_error(self):
        context = self.context({'a.py': 'def broken(): pass\ndef valid(): pass\n'})
        parsed = parse_symbols('a.py', 'def broken(): pass\ndef valid(): pass\n')
        parsed['symbols'][0]['start_line'] = None
        with patch('src.analysis.source_context.parse_symbols', return_value=parsed):
            service = self.service(context)
        symbol = service.search_symbols('broken')['results'][0]
        self.assertEqual(service.read_symbol(symbol['symbol_id'])['error']['code'], 'SYMBOL_RANGE_UNAVAILABLE')
        self.assertEqual(service.read_source('source-001', 'a.py', 1, 1)['text'], 'def broken(): pass\n')

    @unittest.skipUnless(SDK_AVAILABLE, 'Official MCP SDK is not installed; server startup is unverified.')
    def test_startup_failure_reports_stderr_and_keeps_stdout_protocol_clean(self):
        self.service(self.context({'a.txt': 'text\n'}))
        result = subprocess.run([sys.executable, '-I', '-B', str(SERVER), '--manifest', str(self.manifest_path),
                                 '--sha256', '0' * 64], capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b'integrity check failed', result.stderr)


if __name__ == '__main__':
    unittest.main()
