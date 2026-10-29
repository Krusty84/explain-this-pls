# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""MCP attempt scope and unchanged native orchestration, without a model call."""
import json
from pathlib import Path
from unittest.mock import patch

from src.analysis.evidence import SourceChanged, sha
from src.analysis.source_context import parse_symbols
from src.mcp.service import SourceService
from src.model.model_context import MCP_SOURCE_INDEX_INSTRUCTION, SOURCE_INDEX_INSTRUCTION
from src.backends.codex_capabilities import verify_mcp_text_delivery
from src.mcp import SOURCE_TOOLS
from src.runtime.errors import AuditError
from src.runtime.runner import Runner
from test_folder import FolderFixture
from fixtures.cli_response import cli_result
from fixtures.ledger_response import prompt_context, response
from fixtures.study_runner import run_case
import test_revision_pipeline as revision_fixtures


class SourceAccessConfigTests(FolderFixture):
    def test_default_and_explicit_native_keep_context_settings(self):
        self.assertEqual(self.config()['source_access'], {'mode': 'native'})
        self.value.update(source_access={'mode': 'native'}, source_context={'enabled': False})
        self.assertFalse(self.config()['source_context']['enabled'])

    def test_mcp_requires_complete_on_demand_preparation(self):
        self.value['source_access'] = {'mode': 'mcp'}
        self.assertEqual(self.config()['source_context'], {'enabled': True, 'mode': 'on-demand'})
        for settings in ({'enabled': False}, {'mode': 'full'}):
            self.value['source_context'] = settings
            with self.subTest(settings=settings), patch('src.runtime.config.shutil.which') as which:
                with self.assertRaisesRegex(AuditError, 'requires source_context') as caught:
                    self.config()
                self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
                which.assert_not_called()

    def test_invalid_access_settings_fail_before_cli_lookup(self):
        for value in (None, [], False, '', {'mode': []}, {'mode': None}, {'mode': 'http'}, {'other': 1}):
            self.value['source_access'] = value
            with self.subTest(value=value), patch('src.runtime.config.shutil.which') as which:
                with self.assertRaises(AuditError) as caught:
                    self.config()
                self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
                which.assert_not_called()


class MCPAttemptTests(FolderFixture):
    run_case = run_case

    def test_plantuml_repair_has_no_source_manifest_or_source_tools(self):
        def change(runner, context, data):
            if context.get('phase') == 'write':
                data['report_sections'][2]['blocks'][-1]['markdown'] = (
                    '```plantuml\n@startuml\ncomponent "App" aass App\n@enduml\n```')

        manifest, code = self.run_case(large=False, change=change)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        repair = self.calls[-1]
        self.assertEqual(repair['context']['phase'], 'plantuml_repair')
        self.assertNotIn('source_context', repair['context'])
        self.assertNotIn('--manifest', repair['command'])
        self.assertIn('--catalog', repair['command'])
        private = next(path for path in self.runner.session.run_dir.rglob('attempt-*/invocation.json')
                       if json.loads(path.read_text()).get('phase') == 'plantuml_repair')
        metadata = json.loads(private.read_text())
        self.assertFalse(metadata['source_tools_enabled'])
        self.assertFalse(metadata['source_access']['enabled'])
        self.assertEqual(metadata['tool_observation']['calls'], 0)

    def setUp(self):
        super().setUp()
        self.value['source_access'] = {'mode': 'mcp'}
        def profile(session, agent):
            model = agent.get('model') or 'fixture/default-model'
            return {'catalog': {'models': [{'slug': model}]}, 'proof': {'model': model}}
        def proof(command, *args, expected_tools, **kwargs):
            return {'model': command[command.index('--model') + 1], 'tools': sorted(expected_tools),
                    'toolset_sha256': 'a' * 64, 'exclusive_source_access_verified': True,
                    'native_fallback': False, 'inference_requests': 1}
        def trace(root, expected_tools, *, expected_model=None):
            return {'model': expected_model, 'tools': sorted(expected_tools), 'toolset_sha256': 'a' * 64,
                    'exclusive_source_access_verified': True, 'native_fallback': False, 'inference_requests': 1}
        for name, options in (('mcp_profile', {'side_effect': profile}),
                              ('probe_command', {'side_effect': proof}),
                              ('verify_trace', {'side_effect': trace}),
                              ('verify_mcp_text_delivery', {'return_value': {'read_results': 0,
                                  'delivered_read_results': 0, 'text_utf8_bytes': 0,
                                  'source_text_delivery_verified': True}})):
            patched = patch('src.model.invocation.' + name, create=True, **options)
            setattr(self, name, patched.start())
            self.addCleanup(patched.stop)

    def test_collect_review_and_revise_have_private_independent_manifests(self):
        self.value['source_filter'] = {'exclude_paths': ['excluded.py']}
        (self.source / 'excluded.py').write_text('def excluded(): pass\n')
        (self.source / 'notes.txt').write_text('No parser.\n')
        (self.source / 'broken.py').write_text('def broken(:\n')
        with patch('src.analysis.source_context.parse_symbols', wraps=parse_symbols) as parse:
            manifest, code = revision_fixtures.RevisionPipelineTests.run_case(
                self, revision_fixtures.initial_finding)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual(parse.call_count, 3)
        paths = sorted(self.run_dir.rglob('mcp/manifest.json'))
        self.assertEqual(len(paths), 4)
        attempts = set()
        for path in paths:
            metadata = json.loads((path.parent.parent / 'invocation.json').read_text())
            private = json.loads(path.read_text())
            attempts.add(private['attempt_id'])
            self.assertEqual(private['attempt_id'], metadata['invocation_id'])
            self.assertEqual(metadata['source_access']['manifest'], str(path))
            service = SourceService(path, metadata['source_access']['manifest_sha256'])
            found = service.search_symbols('main')
            self.assertEqual(found['results'][0]['path'], 'app.py')
            self.assertEqual(service.read_source('source-001', 'app.py', 1, 1)['text'],
                             'def main(): return 42\n')
            self.assertIn('error', service.read_source('source-001', 'excluded.py', 1, 1))
            self.assertEqual({file['path'] for file in private['files']}, {'app.py', 'notes.txt', 'broken.py'})
        self.assertEqual(len(attempts), 4)
        self.assertEqual([call.get('phase', call['stage']) for call in self.calls],
                         ['collect', 'write', 'review', 'study', 'review'])
        for call, cmd in zip(self.calls, self.commands):
            if call.get('phase') == 'write':
                self.assertNotIn('source_context', call)
                self.assertNotIn('--manifest', cmd)
                continue
            index = call['source_context']
            self.assertEqual(index['delivery'], 'mcp')
            self.assertNotIn('maps_root', index)
            self.assertNotIn('file_columns', index)
            self.assertNotIn('symbol_columns', index)
            self.assertNotIn('files', index)
            self.assertEqual(index['summary']['files'], 3)
            self.assertEqual(index['summary']['indexing_errors'], 1)
            self.assertTrue(any(Path(arg).name == 'codex_mcp.py' for arg in cmd))
            self.assertIn('--manifest', cmd)
            self.assertIn('--sha256', cmd)
            self.assertIn('--catalog', cmd)
            self.assertIn('--model', cmd)
        writer = next(path for path in self.run_dir.rglob('attempt-*/invocation.json')
                      if json.loads(path.read_text()).get('phase') == 'write')
        self.assertFalse((writer.parent / 'mcp').exists())
        for path in self.run_dir.rglob('input.prompt.txt'):
            prompt = path.read_text()
            if path.parent == writer.parent:
                self.assertNotIn(MCP_SOURCE_INDEX_INSTRUCTION, prompt)
            else:
                self.assertIn(MCP_SOURCE_INDEX_INSTRUCTION, prompt)
                self.assertNotIn(SOURCE_INDEX_INSTRUCTION, prompt)
                self.assertNotIn('source-maps', prompt)
                self.assertNotIn('M-000001.json', prompt)
        self.assertEqual((self.source / 'app.py').read_text(), 'def main(): return 42\n')

    def test_manifest_corruption_blocks_publication(self):
        def corrupt(runner, context, data):
            if context.get('phase') == 'collect':
                path = next(runner.session.run_dir.rglob('mcp/manifest.json'))
                path.write_text('{}\n')

        manifest, code = self.run_case(large=False, change=corrupt)
        self.assertNotEqual(code, 0)
        self.assertTrue(manifest['critical_failure'])
        self.assertFalse((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())
        meta = json.loads(next(self.runner.session.run_dir.rglob('study.logs/invocation.json')).read_text())
        self.assertEqual(meta['error']['code'], 'MODEL_BINDING_CHANGED')

    def test_served_source_change_blocks_publication_without_a_delivered_event(self):
        def changed_read(runner, context, data):
            if context.get('phase') != 'collect':
                return
            path = next(runner.session.run_dir.rglob('mcp/manifest.json'))
            service = SourceService(path, sha(path.read_bytes()))
            with patch.object(service, '_read_source', side_effect=SourceChanged('Transient changed bytes')):
                result = service.read_source('source-001', 'app.py', 1, 1)
            self.assertEqual(result['error']['code'], 'SOURCE_CHANGED')

        manifest, code = self.run_case(large=False, change=changed_read)
        self.assertNotEqual(code, 0)
        self.assertTrue(manifest['critical_failure'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.source / 'app.py').read_text(), 'def main(): return 42\n')
        meta = json.loads(next(self.runner.session.run_dir.rglob('study.logs/invocation.json')).read_text())
        self.assertEqual(meta['error']['code'], 'SOURCE_CHANGED')
        self.assertEqual(meta['source_access']['served_calls'], 1)
        self.assertEqual(meta['tool_observation']['calls'], 0)
        self.assertFalse((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())

    def test_mcp_startup_failure_stops_without_native_retry(self):
        runner = Runner(self.config(), self.base / 'startup-failure')
        failed = {'returncode': 1, 'stdout': b'',
                  'stderr': b'MCP client for explain_this_pls failed to initialize'}
        with patch.object(runner, 'check_cli', return_value={}), \
                patch('src.runtime.process.process', return_value=failed) as process:
            manifest, code = runner.run()
        self.assertNotEqual(code, 0)
        self.assertEqual(process.call_count, 1)
        self.assertIn('--manifest', process.call_args.args[0])
        meta = json.loads(next(runner.session.run_dir.rglob('study.logs/invocation.json')).read_text())
        self.assertEqual(meta['error']['code'], 'MCP_STARTUP')
        self.assertFalse((runner.session.run_dir / 'ARCHITECTURE.md').exists())

    def test_failed_capability_probe_stops_before_a_model_call(self):
        runner = Runner(self.config(), self.base / 'capability-failure')
        self.probe_command.side_effect = AuditError('Unexpected source reader.', code='MCP_COMPATIBILITY',
                                                   details={'native_fallback': False})
        with patch.object(runner, 'check_cli', return_value={}), \
                patch('src.runtime.process.process') as process:
            manifest, code = runner.run()
        self.assertNotEqual(code, 0)
        process.assert_not_called()
        meta = json.loads(next(runner.session.run_dir.rglob('study.logs/invocation.json')).read_text())
        self.assertEqual(meta['error']['code'], 'MCP_COMPATIBILITY')
        self.assertFalse((runner.session.run_dir / 'ARCHITECTURE.md').exists())

    def test_post_request_tool_mismatch_blocks_publication(self):
        self.verify_trace.side_effect = AuditError('A native reader appeared.', code='MCP_COMPATIBILITY',
                                                  details={'native_fallback': False})
        manifest, code = self.run_case(large=False)
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.calls), 1)
        meta = json.loads(next(self.runner.session.run_dir.rglob('study.logs/invocation.json')).read_text())
        self.assertEqual(meta['error']['code'], 'MCP_COMPATIBILITY')
        self.assertFalse((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())
        self.assertEqual((self.source / 'app.py').read_text(), 'def main(): return 42\n')

    def test_missing_model_source_text_blocks_publication(self):
        self.verify_mcp_text_delivery.side_effect = AuditError('Source text was truncated.', code='MCP_DELIVERY',
                                                             details={'native_fallback': False})
        manifest, code = self.run_case(large=False)
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.calls), 1)
        meta = json.loads(next(self.runner.session.run_dir.rglob('study.logs/invocation.json')).read_text())
        self.assertEqual(meta['error']['code'], 'MCP_DELIVERY')
        self.assertFalse((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())

    def test_collect_write_review_use_new_tools_and_verify_symbol_delivery(self):
        for truncated in (False, True):
            runner = Runner(self.config(), self.base / ('symbol-delivery-' + str(truncated)))
            self.verify_mcp_text_delivery.side_effect = verify_mcp_text_delivery
            def process(command, cwd, env, payload, *, observer, **kwargs):
                context = prompt_context(payload)
                result = cli_result(command, response(context))
                if '--manifest' not in command:
                    return result
                path = Path(command[command.index('--manifest') + 1])
                service = SourceService(path, sha(path.read_bytes()))
                events, inputs = [], []
                arguments = [('list_files', {}), ('search_text', {'query': 'return 42'}),
                             ('search_symbols', {'query': 'main'})]
                for tool, values in arguments:
                    served = service.call_tool(tool, values)
                    if tool == 'search_symbols':
                        identifier = served['results'][0]['symbol_id']
                    event = {'type': 'item.completed', 'item': {'id': tool, 'type': 'mcp_tool_call',
                        'server': 'explain_this_pls', 'tool': tool, 'arguments': values, 'status': 'completed',
                        'result': {'content': [{'type': 'text', 'text': json.dumps(served)}]}}}
                    events.append(event)
                served = service.read_symbol(identifier)
                events.append({'type': 'item.completed', 'item': {'id': 'read_symbol', 'type': 'mcp_tool_call',
                    'server': 'explain_this_pls', 'tool': 'read_symbol', 'arguments': {'symbol_id': identifier, 'cursor': None},
                    'status': 'completed', 'result': {'content': [{'type': 'text', 'text': json.dumps(served)}]}}})
                for event in events:
                    observer.event(event)
                    text = event['item']['result']['content'][0]['text']
                    if truncated and event['item']['tool'] == 'read_symbol':
                        text = json.dumps(dict(served, text=''))
                    inputs.append({'type': 'function_call_output', 'output': text})
                trace = Path(env['CODEX_ROLLOUT_TRACE_ROOT'])
                (trace / 'request.json').write_text(json.dumps({'model': 'fixture', 'input': inputs}))
                (trace / 'trace.jsonl').write_text(json.dumps({'payload': {'type': 'inference_started',
                    'request_payload': {'path': 'request.json'}}}) + '\n')
                result['stdout'] = ('\n'.join(json.dumps(event) for event in events) + '\n').encode()
                return result
            with self.subTest(truncated=truncated), patch.object(runner, 'check_cli', return_value={}), \
                    patch('src.runtime.process.process', side_effect=process):
                manifest, code = runner.run()
            if truncated:
                self.assertNotEqual(code, 0)
                meta = json.loads(next(runner.session.run_dir.rglob('study.logs/invocation.json')).read_text())
                self.assertEqual(meta['error']['code'], 'MCP_DELIVERY')
            else:
                self.assertEqual(code, 0, manifest.get('diagnostics'))
                for path in runner.session.run_dir.rglob('attempt-*/invocation.json'):
                    meta = json.loads(path.read_text())
                    if meta['source_access']['enabled']:
                        self.assertEqual(meta['source_access']['delivered_read_results'], 1)
                        self.assertEqual(meta['tool_observation']['mcp']['read_symbol_calls'], 1)
                        self.assertEqual(meta['tool_observation']['unique_lines'], 1)
                source_calls = [call for call in self.probe_command.call_args_list if '--manifest' in call.args[0]]
                for call in source_calls:
                    self.assertTrue({'mcp__explain_this_pls__' + tool for tool in SOURCE_TOOLS} <= call.kwargs['expected_tools'])

    def test_completed_codex_event_counts_full_text_only(self):
        for full_text in (True, False):
            runner = Runner(self.config(), self.base / ('delivered-' + str(full_text)))

            def process(command, cwd, env, payload, *, observer, **kwargs):
                context = prompt_context(payload)
                self.assertNotEqual(cwd, self.source)
                if context.get('phase') == 'collect':
                    path = next(runner.session.run_dir.rglob('mcp/manifest.json'))
                    service = SourceService(path, sha(path.read_bytes()))
                    result = service.read_source('source-001', 'app.py', 1, 1)
                    event = {'type': 'item.completed', 'item': {
                        'type': 'mcp_tool_call', 'id': 'source-read', 'status': 'completed',
                        'server': 'explain_this_pls', 'tool': 'read_source',
                        'arguments': {'source_id': 'source-001', 'path': 'app.py', 'start_line': 1, 'end_line': 1},
                        'result': {'content': [{'type': 'text', 'text': json.dumps(result)}] if full_text else [],
                                   'structured_content': result, 'is_error': False}}}
                    observer.feed((json.dumps(event) + '\n').encode())
                return cli_result(command, response(context))

            with self.subTest(full_text=full_text), patch.object(runner, 'check_cli', return_value={}), \
                    patch('src.runtime.process.process', side_effect=process):
                manifest, code = runner.run()
            self.assertEqual(code, 0, manifest.get('diagnostics'))
            meta = next(json.loads(path.read_text()) for path in runner.session.run_dir.rglob('attempt-*/invocation.json')
                        if json.loads(path.read_text()).get('phase') == 'collect')
            observed = meta['tool_observation']
            self.assertEqual(observed['mcp']['delivered_observed'], int(full_text))
            self.assertEqual(observed['unique_lines'], 1 if full_text else None)
            self.assertEqual(observed['mcp']['inspection_state'], 'INSPECTED_UNVERIFIED')
            self.assertEqual(meta['source_access']['served_calls'], 1)

    def test_missing_or_replaced_observed_receipt_blocks_publication(self):
        for remove in (True, False):
            runner = Runner(self.config(), self.base / ('receipt-changed-' + str(remove)))

            def process(command, cwd, env, payload, *, observer, **kwargs):
                context = prompt_context(payload)
                path = next(runner.session.run_dir.rglob('mcp/manifest.json'))
                service = SourceService(path, sha(path.read_bytes()))
                result = service.read_source('source-001', 'app.py', 1, 1)
                observer.event({'type': 'item.completed', 'item': {
                    'type': 'mcp_tool_call', 'id': 'source-read', 'status': 'completed',
                    'server': 'explain_this_pls', 'tool': 'read_source',
                    'arguments': {'source_id': 'source-001', 'path': 'app.py', 'start_line': 1, 'end_line': 1},
                    'result': {'content': [{'type': 'text', 'text': json.dumps(result)}]}}})
                if remove:
                    service.receipts_path.unlink()
                else:
                    service.receipts_path.write_text('')
                    service.search_symbols('main')
                return cli_result(command, response(context))

            with self.subTest(remove=remove), patch.object(runner, 'check_cli', return_value={}), \
                    patch('src.runtime.process.process', side_effect=process):
                manifest, code = runner.run()
            self.assertNotEqual(code, 0)
            self.assertTrue(manifest['critical_failure'])
            meta = json.loads(next(runner.session.run_dir.rglob('study.logs/invocation.json')).read_text())
            self.assertEqual(meta['error']['code'], 'MCP_RECEIPT_CHANGED')
            self.assertFalse((runner.session.run_dir / 'ARCHITECTURE.md').exists())

    def test_native_keeps_native_map_prompt_and_source_working_directory(self):
        self.value['source_access'] = {'mode': 'native'}
        manifest, code = self.run_case(large=False)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        collect = self.calls[0]
        self.assertEqual(collect['cwd'], self.source)
        self.assertIn(SOURCE_INDEX_INSTRUCTION, collect['prompt'])
        self.assertIn('maps_root', collect['context']['source_context'])
        self.assertFalse(list(self.runner.session.run_dir.rglob('mcp/manifest.json')))
        self.mcp_profile.assert_not_called()
        self.probe_command.assert_not_called()
        self.verify_trace.assert_not_called()
        self.verify_mcp_text_delivery.assert_not_called()


if __name__ == '__main__':
    import unittest
    unittest.main()
