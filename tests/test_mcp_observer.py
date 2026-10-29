# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.replay_tool_observation import replay
from src.analysis.source_context import SourceContextBuilder
from src.mcp.service import SourceService, prepare_manifest
from src.model.model_context import on_demand_source_index
from src.runtime.artifacts import save_json
from src.runtime.tool_observer import ToolObserver, canonical, digest


class MCPObserverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.receipts = Path(self.directory.name) / 'served.jsonl'
        self.receipts.write_text('', encoding='utf-8')
        self.text = 'one\nдва\nthree'
        self.sha256 = hashlib.sha256(self.text.encode()).hexdigest()
        self.context = {
            'source_mode': 'folder', 'source_directory': '/source', 'source_fingerprint': 'snapshot',
            '_inventory': {'entries': [{'type': 'file', 'path': 'unit.py', 'sha256': self.sha256}]},
            '_mcp_source_access': {'server': 'explain_this_pls', 'attempt_id': 'attempt',
                                   'manifest_sha256': 'manifest', 'receipts_path': str(self.receipts)}}
        self.saved = []
        self.observer = ToolObserver('codex', context=self.context, attempt_id='attempt', clock=lambda: 0,
                                     event_sink=lambda value: self.saved.append(copy.deepcopy(value)))

    def arguments(self, start=1, end=3, **values):
        return {'source_id': 'source-001', 'path': 'unit.py', 'start_line': start, 'end_line': end, **values}

    def payload(self, start=1, end=3, *, complete=True, **values):
        return {'source_id': 'source-001', 'path': 'unit.py', 'start_line': start, 'end_line': end,
                'total_lines': 3, 'encoding': 'utf-8', 'sha256': self.sha256,
                'text': ''.join(self.text.splitlines(keepends=True)[start - 1:end]),
                'complete': complete, 'has_more': not complete, 'next_start_line': None if complete else end + 1,
                **values}

    def event(self, number=1, *, tool='read_source', arguments=None, payload=None, receipt=True):
        arguments = arguments if arguments is not None else self.arguments()
        payload = copy.deepcopy(payload if payload is not None else self.payload())
        payload.setdefault('_meta', {'manifest_sha256': 'manifest', 'attempt_id': 'attempt',
                                    'request_id': str(number), 'state': 'SERVED', 'inspection': 'INSPECTED_UNVERIFIED'})
        if receipt:
            record = {'state': 'SERVED', 'inspection': 'INSPECTED_UNVERIFIED', 'attempt_id': 'attempt',
                      'request_id': str(number), 'tool': tool, 'arguments': arguments,
                      'result_sha256': digest(payload), 'text_bytes': len(payload.get('text', '').encode())}
            with self.receipts.open('a', encoding='utf-8') as stream:
                stream.write(canonical(record) + '\n')
        return {'type': 'item.completed', 'item': {'id': 'call_' + str(number), 'type': 'mcp_tool_call',
                'server': 'explain_this_pls', 'tool': tool, 'arguments': arguments, 'status': 'completed',
                'result': {'content': [{'type': 'text', 'text': canonical(payload)}], 'structuredContent': payload}}}

    def test_complete_text_has_verified_coordinates_and_unverified_inspection(self):
        self.observer.event(self.event())
        metrics = self.observer.snapshot()
        self.assertEqual(metrics['unique_lines'], 3)
        self.assertEqual(metrics['delivery']['unique_lines'], 3)
        self.assertEqual(metrics['mcp']['text_utf8_bytes'], len(self.text.encode()))
        self.assertEqual(metrics['mcp']['delivered_observed'], 1)
        self.assertEqual(metrics['mcp']['inspection_state'], 'INSPECTED_UNVERIFIED')
        recorded = self.saved[0]['call']['mcp']
        self.assertEqual(recorded['request'], self.arguments())
        self.assertEqual(recorded['returned']['start_line'], 1)
        self.assertEqual(recorded['returned']['end_line'], 3)
        self.assertEqual(recorded['new_ranges'], [{'source_id': 'source-001', 'path': 'unit.py',
                                                  'start_line': 1, 'end_line': 3}])

    def test_overlapping_reads_record_new_and_repeated_ranges(self):
        self.observer.event(self.event(1, arguments=self.arguments(1, 2), payload=self.payload(1, 2)))
        self.observer.event(self.event(2, arguments=self.arguments(2, 3), payload=self.payload(2, 3)))
        metrics = self.observer.snapshot()
        self.assertEqual((metrics['unique_lines'], metrics['repeated_lines']), (3, 1))
        self.assertEqual((metrics['mcp']['unique_lines'], metrics['mcp']['repeated_lines']), (3, 1))
        recorded = self.saved[1]['call']['mcp']
        self.assertEqual(recorded['new_ranges'][0]['start_line'], 3)
        self.assertEqual(recorded['repeated_ranges'][0]['start_line'], 2)

    def test_page_counts_actual_lines_and_resets_repeat_guard(self):
        event = self.event(1, payload=self.payload(1, 2, complete=False))
        self.observer.event(event)
        self.observer.event(self.event(2, payload=self.payload(1, 2, complete=False)))
        metrics = self.observer.snapshot()
        self.assertEqual(metrics['mcp']['delivered_observed'], 2)
        self.assertEqual(metrics['mcp']['incomplete_results'], 2)
        self.assertEqual(metrics['unique_lines'], 2)
        self.assertEqual(metrics['repeated_lines'], 2)
        self.assertEqual(metrics['consecutive_confirmed_repeats'], 0)
        self.assertEqual(self.saved[0]['call']['mcp']['returned']['next_start_line'], 3)

    def test_search_does_not_count_source_text(self):
        self.observer.event(self.event(tool='search_symbols', arguments={'query': 'one'},
                                      payload={'results': [], 'has_more': False, 'next_cursor': None}))
        metrics = self.observer.snapshot()
        self.assertEqual(metrics['mcp']['search_symbols_calls'], 1)
        self.assertEqual(metrics['mcp']['text_utf8_bytes'], 0)
        self.assertIsNone(metrics['unique_lines'])
        self.assertIsNone(metrics['delivery']['unique_lines'])

    def test_text_snippets_and_file_metadata_do_not_count_source_reading(self):
        for number, tool in enumerate(('search_text', 'list_files')):
            self.observer.event(self.event(number, tool=tool, arguments={'query': 'one'} if number == 0 else {},
                payload={'results': [{'source_id': 'source-001', 'path': 'unit.py', 'line': 1, 'snippet': 'one'}],
                         'has_more': False, 'cursor': None}))
        metrics = self.observer.snapshot()
        self.assertEqual(metrics['mcp']['delivered_observed'], 2)
        self.assertEqual(metrics['mcp']['search_text_calls'], 1)
        self.assertEqual(metrics['mcp']['list_files_calls'], 1)
        self.assertEqual(metrics['mcp']['text_utf8_bytes'], 0)
        self.assertIsNone(metrics['unique_lines'])

    def test_structured_content_without_text_does_not_establish_delivery(self):
        for content in ([], [{'type': 'text', 'text': '{"source_id":"source-001"}'}],
                        [{'type': 'text', 'text': '{"text":"one'}]):
            with self.subTest(content=content):
                event = self.event(len(self.saved))
                event['item']['result']['content'] = content
                self.observer.event(event)
        metrics = self.observer.snapshot()
        self.assertEqual(metrics['mcp']['delivered_observed'], 0)
        self.assertEqual(metrics['mcp']['unverified_results'], 3)
        self.assertIsNone(metrics['unique_lines'])

    def test_truncated_event_and_structured_mismatch_do_not_count_lines(self):
        event = self.event(1)
        event['item']['truncated'] = True
        self.observer.event(event)
        event = self.event(2)
        event['item']['result']['structuredContent']['text'] = 'different'
        self.observer.event(event)
        event = self.event(3)
        event['item']['id'] = None
        self.observer.event(event)
        self.assertEqual(self.observer.snapshot()['mcp']['delivered_observed'], 0)
        self.assertEqual(self.observer.tool_lines, {})

    def test_requires_matching_private_receipt_and_attempt_identity(self):
        for number, values in enumerate(({'receipt': False},
                                        {'payload': self.payload(_meta={'request_id': '2', 'attempt_id': 'another',
                                            'manifest_sha256': 'manifest', 'state': 'SERVED', 'inspection': 'INSPECTED_UNVERIFIED'})},
                                        {'payload': self.payload(_meta={'request_id': '3', 'attempt_id': 'attempt',
                                            'manifest_sha256': 'other', 'state': 'SERVED', 'inspection': 'INSPECTED_UNVERIFIED'})}), 1):
            self.observer.event(self.event(number, **values))
        self.assertEqual(self.observer.snapshot()['mcp']['delivered_observed'], 0)
        self.assertEqual(self.observer.tool_lines, {})

    def test_forged_result_and_arguments_do_not_match_receipt(self):
        event = self.event(1)
        payload = event['item']['result']['structuredContent']
        payload['text'] = 'xxx\nyyy\nzzzzz'
        event['item']['result']['content'][0]['text'] = canonical(payload)
        self.observer.event(event)
        event = self.event(2)
        event['item']['arguments']['start_line'] = 2
        self.observer.event(event)
        self.assertEqual(self.observer.snapshot()['mcp']['delivered_observed'], 0)

    def test_file_hash_locator_and_text_coordinates_must_match_inventory(self):
        for number, payload in enumerate((self.payload(sha256='changed'), self.payload(path='../unit.py'),
                                         self.payload(text='one\n'), self.payload(end_line=2),
                                         self.payload(next_start_line=4), self.payload(text='one\r\nдва\nthree')), 1):
            self.observer.event(self.event(number, payload=payload))
        self.assertEqual(self.observer.snapshot()['mcp']['delivered_observed'], 0)
        self.assertEqual(self.observer.tool_lines, {})

    def test_errors_keep_codes_and_request_without_source_delivery(self):
        for number, code in enumerate(('SOURCE_CHANGED', 'DECODE_ERROR', 'ENCODING_MISMATCH', 'BINARY_CONTENT'), 1):
            self.observer.event(self.event(number, payload={'error': {'code': code, 'message': 'unavailable'}}))
        metrics = self.observer.snapshot()
        self.assertEqual(metrics['mcp']['read_errors'], 4)
        self.assertEqual(metrics['mcp']['decoding_errors'], 3)
        self.assertEqual(metrics['mcp']['errors']['SOURCE_CHANGED'], 1)
        self.assertEqual(metrics['mcp']['delivered_observed'], 4)
        self.assertEqual(metrics['error_results'], 4)
        self.assertIsNone(metrics['unique_lines'])

    def test_call_failure_is_explicit_without_receipt(self):
        event = self.event(receipt=False)
        event['item'].update(status='failed', result=None, error={'message': 'MCP disconnected'})
        self.observer.event(event)
        metrics = self.observer.snapshot()
        self.assertEqual(metrics['mcp']['errors'], {'TOOL_CALL_FAILED': 1})
        self.assertEqual(metrics['mcp']['delivered_observed'], 0)
        self.assertEqual(metrics['error_results'], 1)

    def test_empty_file_is_observed_without_invented_line(self):
        self.observer.event(self.event(arguments=self.arguments(1, 1), payload=self.payload(1, 0, total_lines=0, text='')))
        metrics = self.observer.snapshot()
        self.assertEqual(metrics['mcp']['delivered_observed'], 1)
        self.assertEqual(metrics['mcp']['unique_lines'], 0)
        self.assertEqual(metrics['incomplete_results'], 0)

    def test_other_server_or_absent_access_metadata_has_no_source_coordinates(self):
        event = self.event()
        event['item']['server'] = 'project_plugin'
        self.observer.event(event)
        self.assertNotIn('mcp', self.observer.snapshot())
        native = ToolObserver('codex')
        event['item']['server'] = 'explain_this_pls'
        native.event(event)
        self.assertNotIn('mcp', native.snapshot())
        self.assertEqual(native.tool_lines, {})

    def test_duplicate_events_and_sessions_do_not_double_count(self):
        event = self.event()
        self.observer.event(event)
        self.observer.event(event)
        self.assertEqual(self.observer.snapshot()['mcp']['delivered_observed'], 1)
        self.assertEqual(self.observer.snapshot()['duplicate_events'], 1)
        self.observer.session_id = 'another'
        self.observer.event(event)
        self.assertEqual(self.observer.snapshot()['mcp']['delivered_observed'], 2)
        self.assertEqual(self.observer.snapshot()['mcp']['repeated_lines'], 3)

    def test_submodule_source_ids_are_isolated(self):
        context = {**self.context, 'source_mode': 'git', 'branch': 'main', 'source_commit': 'commit',
                   'submodules': [{'path': 'lib', 'expected_commit': 'subcommit'}],
                   '_inventory': {'entries': [{'type': 'file', 'path': 'unit.py', 'sha256': self.sha256},
                                             {'type': 'file', 'path': 'lib/unit.py', 'sha256': self.sha256}]}}
        context.pop('source_directory')
        observer = ToolObserver('codex', context=context)
        observer.event(self.event(1))
        observer.event(self.event(2, arguments=self.arguments(source_id='source-002'),
                                  payload=self.payload(source_id='source-002')))
        observer.event(self.event(3, arguments=self.arguments(path='lib/unit.py'), payload=self.payload(path='lib/unit.py')))
        self.assertEqual(observer.snapshot()['mcp']['delivered_observed'], 2)
        self.assertEqual(observer.snapshot()['unique_lines'], 6)

    def test_normalized_replay_preserves_delivery_without_rereading_source_or_receipts(self):
        self.observer.event(self.event(1))
        self.observer.event(self.event(2, arguments=self.arguments(2, 3), payload=self.payload(2, 3)))
        self.observer.event({'type': 'turn.completed'})
        path = Path(self.directory.name) / 'tool-events.jsonl'
        path.write_text('\n'.join(canonical(row) for row in self.saved), encoding='utf-8')
        self.receipts.unlink()
        self.assertEqual(replay(path, 'codex')['metrics'], self.observer.finish())

    def test_real_service_receipt_and_result_establish_text_delivery(self):
        directory = Path(self.directory.name).resolve()
        root, attempt = directory / 'source', directory / 'attempt'
        root.mkdir()
        (root / 'unit.py').write_text(self.text, encoding='utf-8')
        context = {**self.context, 'source_directory': str(root)}
        index, _ = SourceContextBuilder().prepare('study', context)
        packed, maps = on_demand_source_index(index, attempt / 'source-maps')
        descriptors = {}
        for name, value in maps.items():
            path = attempt / 'source-maps' / name
            save_json(path, value)
            blob = path.read_bytes()
            descriptors[name] = {'path': str(path), 'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
        manifest = prepare_manifest(context, index, packed, descriptors, attempt_id='attempt')
        manifest_path = attempt / 'mcp/manifest.json'
        save_json(manifest_path, manifest)
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        service = SourceService(manifest_path, manifest_sha256)
        context['_mcp_source_access'] = {**context['_mcp_source_access'], 'manifest_sha256': manifest_sha256,
                                         'receipts_path': str(service.receipts_path)}
        observer = ToolObserver('codex', context=context)
        with patch('src.analysis.source_context.parse_symbols', side_effect=AssertionError('Reparsed source')):
            payload = service.call_tool('read_source', self.arguments())
            observer.event({'type': 'item.completed', 'item': {
                'id': 'actual', 'type': 'mcp_tool_call', 'server': 'explain_this_pls', 'tool': 'read_source',
                'arguments': self.arguments(), 'status': 'completed',
                'result': {'content': [{'type': 'text', 'text': canonical(payload)}], 'structured_content': payload}}})
        self.assertEqual(observer.snapshot()['mcp']['delivered_observed'], 1)
        self.assertEqual(observer.snapshot()['mcp']['text_utf8_bytes'], len(self.text.encode()))
        self.assertEqual(observer.snapshot()['unique_lines'], 3)

    def test_symbol_pages_share_source_delivery_and_repeated_line_accounting(self):
        from test_mcp_server import SourceServiceTests
        fixture = SourceServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        text = 'def large():\n' + ('    # large body\n' * 100) + '    return 1\n'
        context = fixture.context({'unit.py': text})
        service = fixture.service(context, max_response_bytes=1500)
        context['source_context'], _ = SourceContextBuilder().prepare('study', context)
        context['_mcp_source_access'] = {'server': 'explain_this_pls', 'attempt_id': 'attempt-test',
            'manifest_sha256': fixture.manifest_sha256, 'receipts_path': str(service.receipts_path)}
        saved = []
        observer = ToolObserver('codex', context=context, event_sink=saved.append)
        identifier = service.search_symbols('large')['results'][0]['symbol_id']
        cursor, pages = None, 0
        with patch('src.analysis.source_context.parse_symbols', side_effect=AssertionError('Reparsed source')):
            while True:
                arguments = {'symbol_id': identifier, 'cursor': cursor}
                payload = service.call_tool('read_symbol', arguments)
                observer.event({'type': 'item.completed', 'item': {
                    'id': 'symbol-' + str(pages), 'type': 'mcp_tool_call', 'server': 'explain_this_pls',
                    'tool': 'read_symbol', 'arguments': arguments, 'status': 'completed',
                    'result': {'content': [{'type': 'text', 'text': canonical(payload)}]}}})
                pages += 1
                if not payload['has_more']:
                    break
                cursor = payload['cursor']
        self.assertGreater(pages, 1)
        metrics = observer.snapshot()
        self.assertEqual(metrics['mcp']['delivered_observed'], pages)
        self.assertEqual(metrics['mcp']['read_symbol_calls'], pages)
        self.assertEqual(metrics['mcp']['text_utf8_bytes'], len(text.encode()))
        self.assertEqual(metrics['unique_lines'], 102)
        self.assertEqual(metrics['repeated_lines'], 0)
        observer.event({'type': 'turn.completed'})
        path = fixture.directory / 'events.jsonl'
        path.write_text('\n'.join(canonical(row) for row in saved))
        self.assertEqual(replay(path, 'codex')['metrics'], observer.finish())


if __name__ == '__main__':
    unittest.main()
