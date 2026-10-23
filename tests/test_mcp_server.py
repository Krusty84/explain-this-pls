# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import asyncio
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

from src.analysis.evidence import canonical, sha, stamp
from src.analysis.source_context import SourceContextBuilder, parse_symbols
from src.mcp.service import SourceService, prepare_manifest
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
                self.assertCountEqual(exact, [symbol for symbol in expected if symbol['name'] == name])
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
        self.assertEqual(service.search_symbols('original')['error']['code'], 'SOURCE_CHANGED')
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
        self.assertEqual((self.root / 'a.py').read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.root / 'a.py'])

    def test_invalid_calls_are_structured_and_receipted(self):
        service = self.service(self.context({'a.txt': 'one\n'}))
        for name, arguments, code in [('unknown', {}, 'UNKNOWN_TOOL'),
                                      ('read_source', {}, 'INVALID_ARGUMENT'),
                                      ('search_symbols', {'query': 'x', 'limit': True}, 'INVALID_ARGUMENT'),
                                      ('search_symbols', {'query': 'x', 'cursor': 'bad'}, 'INVALID_CURSOR'),
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
                    self.assertEqual({tool.name for tool in tools.tools}, {'search_symbols', 'read_source'})
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
                    invalid = await session.call_tool('read_source', dict(source_id='source-001', path='../outside', start_line=1, end_line=1))
                    self.assertTrue(invalid.isError)
                    self.assertEqual(json.loads(invalid.content[0].text)['error']['code'], 'INVALID_ARGUMENT')
        asyncio.run(exercise())
        self.assertEqual(len(service.receipts_path.read_text().splitlines()), 3)

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
