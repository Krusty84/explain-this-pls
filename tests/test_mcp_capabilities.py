# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Fail-closed verification of effective Codex request tool schemas."""
import json
from pathlib import Path
import tempfile
import unittest

from src.backends.codex_capabilities import probe_command, verify_mcp_text_delivery, verify_trace
from src.runtime.errors import AuditError


class CodexCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def request(self, request, *, reference='payloads/request.json', trace='trace-one', call_id=None):
        directory = self.root / trace
        directory.mkdir(exist_ok=True)
        (directory / 'payloads').mkdir(exist_ok=True)
        number = len(list((directory / 'payloads').glob('*.json'))) + 1
        filename = 'request.json' if number == 1 else 'request-' + str(number) + '.json'
        (directory / 'payloads' / filename).write_text(json.dumps(request), encoding='utf-8')
        if reference == 'payloads/request.json':
            reference = 'payloads/' + filename
        call_id = call_id or trace + '-call-' + str(number)
        event = {'payload': {'type': 'inference_started', 'inference_call_id': call_id,
                             'request_payload': {'path': reference}}}
        with (directory / 'trace.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event) + '\n')
        return call_id

    def complete(self, call_id, response_id, trace='trace-one'):
        event = {'payload': {'type': 'inference_completed', 'inference_call_id': call_id,
                             'response_id': response_id}}
        with (self.root / trace / 'trace.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event) + '\n')

    def function(self, name):
        return {'type': 'function', 'name': name, 'parameters': {'type': 'object'}}

    def test_flat_and_namespace_schemas_match_the_same_mcp_names(self):
        names = {'mcp__explain_this_pls__read_source', 'mcp__explain_this_pls__search_symbols'}
        self.request({'model': 'selected', 'input': [], 'tools': [self.function(name) for name in names]})
        flat = verify_trace(self.root, names)
        self.assertEqual(flat['tools'], sorted(names))
        self.assertTrue(flat['exclusive_source_access_verified'])
        self.assertFalse(flat['native_fallback'])
        self.request({'model': 'selected', 'input': [{'type': 'additional_tools', 'tools': [
            {'type': 'namespace', 'name': 'mcp__explain_this_pls',
             'tools': [self.function('read_source'), self.function('search_symbols')]}]}]}, trace='trace-two')
        self.assertEqual(verify_trace(self.root, names)['inference_requests'], 2)

    def test_additional_tools_cannot_hide_native_readers(self):
        self.request({'model': 'selected', 'tools': [], 'input': [{'type': 'additional_tools', 'tools': [
            {'type': 'namespace', 'name': 'functions', 'tools': [self.function('exec_command')]}]}]})
        with self.assertRaises(AuditError) as caught:
            verify_trace(self.root, set())
        self.assertEqual(caught.exception.code, 'MCP_COMPATIBILITY')
        self.assertEqual(caught.exception.details['unexpected_tools'], ['exec_command'])

    def test_missing_tools_unknown_schema_and_deferred_discovery_fail_closed(self):
        cases = [[], [{'type': 'tool_search', 'execution': 'client'}],
                 [{'type': 'namespace', 'name': 'tools', 'tools': None}], [None]]
        for tools in cases:
            with self.subTest(tools=tools):
                with tempfile.TemporaryDirectory() as raw:
                    prior, self.root = self.root, Path(raw)
                    self.request({'model': 'selected', 'input': [], 'tools': tools})
                    with self.assertRaises(AuditError):
                        verify_trace(self.root, {'mcp__explain_this_pls__read_source'})
                    self.root = prior

    def test_every_request_is_checked_and_model_changes_fail(self):
        self.request({'model': 'first', 'input': [], 'tools': []})
        with self.assertRaises(AuditError):
            verify_trace(self.root, set(), expected_model='different')
        self.request({'model': 'second', 'input': [], 'tools': []}, trace='trace-two')
        with self.assertRaises(AuditError):
            verify_trace(self.root, set())

    def test_later_requests_cannot_add_native_tools(self):
        self.request({'model': 'selected', 'input': [], 'tools': []})
        self.request({'model': 'selected', 'input': [], 'tools': [self.function('view_image')]},
                     trace='trace-two')
        with self.assertRaises(AuditError) as caught:
            verify_trace(self.root, set())
        self.assertEqual(caught.exception.details['unexpected_tools'], ['view_image'])

    def test_linked_responses_inherit_the_verified_tool_schemas(self):
        names = {'mcp__explain_this_pls__read_source', 'mcp__explain_this_pls__search_symbols'}
        first = self.request({'model': 'selected', 'input': [],
                              'tools': [self.function(name) for name in names]})
        baseline = verify_trace(self.root, names)
        self.complete(first, 'response-one')
        second = self.request({'model': 'selected', 'input': [], 'previous_response_id': 'response-one'})
        self.complete(second, 'response-two')
        third = self.request({'model': 'selected', 'input': [], 'previous_response_id': 'response-two'})
        self.complete(third, 'response-three')
        proof = verify_trace(self.root, names, expected_model='selected')
        self.assertEqual(proof['inference_requests'], 3)
        self.assertEqual(proof['tools'], sorted(names))
        self.assertEqual(proof['toolset_sha256'], baseline['toolset_sha256'])

    def test_unlinked_unknown_or_forward_tool_omission_fails_closed(self):
        for previous in (None, 'unknown-response', 'future-response'):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as raw:
                prior, self.root = self.root, Path(raw)
                first = self.request({'model': 'selected', 'input': [], 'tools': []})
                self.complete(first, 'response-one')
                second = self.request({'model': 'selected', 'input': [], 'previous_response_id': previous})
                self.complete(second, 'future-response')
                with self.assertRaises(AuditError):
                    verify_trace(self.root, set())
                self.root = prior

    def test_linked_responses_cannot_cross_traces_or_change_models(self):
        for other_model, other_trace in (('other-model', 'trace-one'), ('selected', 'trace-two')):
            with self.subTest(model=other_model, trace=other_trace), tempfile.TemporaryDirectory() as raw:
                prior, self.root = self.root, Path(raw)
                first = self.request({'model': 'selected', 'input': [], 'tools': []})
                self.complete(first, 'response-one')
                self.request({'model': other_model, 'input': [], 'previous_response_id': 'response-one'},
                             trace=other_trace)
                with self.assertRaises(AuditError):
                    verify_trace(self.root, set())
                self.root = prior

    def test_linked_incremental_tool_updates_cannot_add_native_readers(self):
        name = 'mcp__explain_this_pls__read_source'
        first = self.request({'model': 'selected', 'input': [], 'tools': [self.function(name)]})
        self.complete(first, 'response-one')
        self.request({'model': 'selected', 'previous_response_id': 'response-one', 'input': [
            {'type': 'additional_tools', 'tools': [self.function('exec_command')]}]})
        with self.assertRaises(AuditError) as caught:
            verify_trace(self.root, {name})
        self.assertEqual(caught.exception.details['unexpected_tools'], ['exec_command'])

    def test_completed_responses_must_match_a_known_unique_request(self):
        first = self.request({'model': 'selected', 'input': [], 'tools': []})
        self.complete('unknown-inference', 'response-one')
        with self.assertRaises(AuditError):
            verify_trace(self.root, set())

    def test_incomplete_or_unsupported_request_traces_fail_closed(self):
        requests = [{'model': 'selected', 'input': [], 'tools': None},
                    {'model': 'selected', 'input': 'source text', 'tools': []},
                    {'model': 'selected', 'input': [None], 'tools': []},
                    {'model': 'selected', 'input': [{'type': 'additional_tools'}]},
                    {'model': None, 'input': [], 'tools': []}]
        for request in requests:
            with self.subTest(request=request), tempfile.TemporaryDirectory() as raw:
                prior, self.root = self.root, Path(raw)
                self.request(request)
                with self.assertRaises(AuditError):
                    verify_trace(self.root, set())
                self.root = prior

    def test_unsafe_or_missing_trace_is_rejected(self):
        with self.assertRaises(AuditError):
            verify_trace(self.root, set())
        for reference in ('../request.json', '/request.json', 'C:/request.json', 'payloads\\request.json',
                          'payloads/./request.json', 'payloads//request.json'):
            with self.subTest(reference=reference), tempfile.TemporaryDirectory() as raw:
                prior, self.root = self.root, Path(raw)
                self.request({'model': 'selected', 'input': [], 'tools': []}, reference=reference)
                with self.assertRaises(AuditError):
                    verify_trace(self.root, set())
                self.root = prior

    def test_symlink_payload_is_rejected(self):
        self.request({'model': 'selected', 'input': [], 'tools': []})
        payload = self.root / 'trace-one' / 'payloads' / 'request.json'
        target = self.root / 'outside.json'
        payload.rename(target)
        try:
            payload.symlink_to(target)
        except OSError:
            self.skipTest('This runtime cannot create symbolic links.')
        with self.assertRaises(AuditError):
            verify_trace(self.root, set())

    def test_probe_selects_offline_adapter_and_accepts_expected_transport_failure(self):
        env = {'CODEX_HOME': 'existing-auth-home', 'CODEX_ROLLOUT_TRACE_ROOT': 'untrusted-old-trace'}
        seen = []

        def process(command, state, probe_env, prompt, **kwargs):
            seen.append((command, state, probe_env, prompt, kwargs))
            prior, self.root = self.root, Path(probe_env['CODEX_ROLLOUT_TRACE_ROOT'])
            self.request({'model': 'selected', 'input': [], 'tools': []})
            self.root = prior
            return {'returncode': 1, 'stdout': b'', 'stderr': b'builder error'}

        proof = probe_command(['python', 'codex_mcp.py', '--model', 'selected'], self.root, env,
                              expected_tools=set(), process_fn=process, reporter='reporter', budget='budget')
        command, state, probe_env, prompt, kwargs = seen[0]
        self.assertEqual(proof['model'], 'selected')
        self.assertEqual(command, ['python', 'codex_mcp.py', '--model', 'selected', '--probe'])
        self.assertEqual(kwargs, {'reporter': 'reporter', 'budget': 'budget'})
        self.assertEqual(probe_env['CODEX_HOME'], env['CODEX_HOME'])
        self.assertNotEqual(probe_env['CODEX_ROLLOUT_TRACE_ROOT'], env['CODEX_ROLLOUT_TRACE_ROOT'])
        self.assertEqual(prompt, b'MCP capability probe.')
        self.assertEqual(proof['bootstrap_instruction_sources'], [])

    def test_probe_records_only_well_formed_bootstrap_instruction_metadata(self):
        for sources in (['/private/codex/AGENTS.md'], None, '/private/codex/AGENTS.md', [7]):
            with self.subTest(sources=sources):
                def process(command, state, env, prompt, **kwargs):
                    prior, self.root = self.root, Path(env['CODEX_ROLLOUT_TRACE_ROOT'])
                    self.request({'model': 'selected', 'input': [], 'tools': []})
                    self.root = prior
                    event = {'type': 'thread.started', 'bootstrap_instruction_sources': sources}
                    return {'returncode': 1, 'stdout': json.dumps(event).encode(), 'stderr': b''}
                if isinstance(sources, list) and all(isinstance(source, str) for source in sources):
                    proof = probe_command(['python', 'codex_mcp.py'], self.root, {},
                                          expected_tools=set(), process_fn=process)
                    self.assertEqual(proof['bootstrap_instruction_sources'], sources)
                else:
                    with self.assertRaises(AuditError) as caught:
                        probe_command(['python', 'codex_mcp.py'], self.root, {},
                                      expected_tools=set(), process_fn=process)
                    self.assertEqual(caught.exception.code, 'MCP_COMPATIBILITY')

    def test_failed_launch_or_missing_trace_is_a_compatibility_error(self):
        def missing(*args, **kwargs):
            return {'returncode': 0}
        with self.assertRaises(AuditError):
            probe_command(['python', 'codex_mcp.py'], self.root, {}, expected_tools=set(), process_fn=missing)
        def unavailable(*args, **kwargs):
            raise FileNotFoundError('codex')
        with self.assertRaises(AuditError):
            probe_command(['python', 'codex_mcp.py'], self.root, {}, expected_tools=set(), process_fn=unavailable)

    def test_required_mcp_startup_failure_keeps_its_diagnostic_without_a_trace(self):
        def startup(*args, **kwargs):
            return {'returncode': 1, 'stdout': b'',
                    'stderr': b'MCP client for explain_this_pls failed to initialize'}
        with self.assertRaises(AuditError) as caught:
            probe_command(['python', 'codex_mcp.py'], self.root, {}, expected_tools=set(), process_fn=startup)
        self.assertEqual(caught.exception.code, 'MCP_STARTUP')
        self.assertFalse(caught.exception.details['native_fallback'])

    def test_adapter_diagnostics_are_preserved_without_a_trace(self):
        for code, nested in (('MCP_COMPATIBILITY', False), ('MCP_STARTUP', True)):
            with self.subTest(code=code):
                diagnostic = {'code': code, 'message': 'Explicit adapter failure.',
                              'failure_layer': 'specific', 'hint': 'Inspect the private log.',
                              'details': {'reason': 'unsupported environments', 'native_fallback': True}}
                event = {'type': 'turn.failed', 'error': diagnostic} if nested else {'type': 'error', **diagnostic}
                def adapter(*args, **kwargs):
                    return {'returncode': 1, 'stdout': json.dumps(event).encode(), 'stderr': b''}
                with self.assertRaises(AuditError) as caught:
                    probe_command(['python', 'codex_mcp.py'], self.root, {},
                                  expected_tools=set(), process_fn=adapter)
                error = caught.exception
                self.assertEqual(error.code, code)
                self.assertEqual(error.message, diagnostic['message'])
                self.assertEqual(error.failure_layer, diagnostic['failure_layer'])
                self.assertEqual(error.hint, diagnostic['hint'])
                self.assertEqual(error.details['reason'], diagnostic['details']['reason'])
                self.assertFalse(error.details['native_fallback'])

    def source_result(self, identifier='source-read'):
        payload = {'source_id': 'source-001', 'path': 'probe.py', 'start_line': 1, 'end_line': 2,
                   'total_lines': 2, 'encoding': 'utf-8', 'sha256': 'a' * 64,
                   'text': 'def probe():\n    return "случайная строка"\n',
                   '_meta': {'request_id': identifier, 'state': 'SERVED', 'attempt_id': 'attempt'}}
        text = json.dumps(payload, ensure_ascii=False)
        event = {'type': 'item.completed', 'item': {
            'id': identifier, 'type': 'mcp_tool_call', 'server': 'explain_this_pls', 'tool': 'read_source',
            'status': 'completed', 'result': {'content': [{'type': 'text', 'text': text}]}}}
        return payload, text, event

    def test_full_source_result_is_verified_in_actual_model_tool_input(self):
        payload, text, event = self.source_result()
        outputs = [text, {'content': [{'type': 'text', 'text': text}]},
                   json.dumps({'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}),
                   json.dumps(text),
                   'Wall time: 0.0044 seconds\nOutput:\n' + json.dumps(payload, sort_keys=True)]
        for output in outputs:
            with self.subTest(output=output), tempfile.TemporaryDirectory() as raw:
                prior, self.root = self.root, Path(raw)
                self.request({'model': 'selected', 'tools': [], 'input': [
                    {'type': 'function_call_output', 'call_id': 'source-read', 'output': output}]})
                result = verify_mcp_text_delivery(self.root, (json.dumps(event) + '\n').encode())
                self.assertEqual(result['read_results'], 1)
                self.assertEqual(result['delivered_read_results'], 1)
                self.assertEqual(result['text_utf8_bytes'], len(payload['text'].encode('utf-8')))
                self.assertTrue(result['source_text_delivery_verified'])
                self.root = prior

    def test_metadata_only_or_truncated_model_outputs_fail_delivery(self):
        payload, text, event = self.source_result()
        outputs = [dict(payload, text=''), {'_meta': payload['_meta']},
                   dict(payload, _meta={'request_id': 'another-read'})]
        for output in outputs:
            with self.subTest(output=output), tempfile.TemporaryDirectory() as raw:
                prior, self.root = self.root, Path(raw)
                self.request({'model': 'selected', 'tools': [], 'input': [
                    {'type': 'custom_tool_call_output', 'output': json.dumps(output)}]})
                with self.assertRaises(AuditError) as caught:
                    verify_mcp_text_delivery(self.root, json.dumps(event).encode())
                self.assertEqual(caught.exception.code, 'MCP_DELIVERY')
                self.assertFalse(caught.exception.details['native_fallback'])
                self.root = prior

    def test_codex_wall_time_envelope_preserves_full_source_and_receipt_checks(self):
        payload, text, event = self.source_result()
        for changed in (dict(payload, text=payload['text'][:-1]),
                        dict(payload, start_line=2), dict(payload, sha256='changed'),
                        dict(payload, _meta={'request_id': 'another-read'})):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as raw:
                prior, self.root = self.root, Path(raw)
                output = 'Wall time: 0.0044 seconds\nOutput:\n' + json.dumps(changed, sort_keys=True)
                self.request({'model': 'selected', 'tools': [], 'input': [
                    {'type': 'function_call_output', 'output': output}]})
                with self.assertRaises(AuditError) as caught:
                    verify_mcp_text_delivery(self.root, json.dumps(event).encode())
                self.assertEqual(caught.exception.code, 'MCP_DELIVERY')
                self.root = prior

    def test_source_text_in_a_message_does_not_prove_tool_delivery(self):
        payload, text, event = self.source_result()
        self.request({'model': 'selected', 'tools': [], 'input': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}]})
        with self.assertRaises(AuditError) as caught:
            verify_mcp_text_delivery(self.root, json.dumps(event).encode())
        self.assertEqual(caught.exception.code, 'MCP_DELIVERY')

    def test_completed_metadata_only_event_fails_delivery(self):
        payload, text, event = self.source_result()
        event['item']['result'] = {'content': [], 'structuredContent': payload}
        with self.assertRaises(AuditError) as caught:
            verify_mcp_text_delivery(self.root, json.dumps(event).encode())
        self.assertEqual(caught.exception.code, 'MCP_DELIVERY')

    def test_explicit_read_errors_are_not_counted_as_source_text(self):
        _, _, event = self.source_result()
        event['item']['result']['content'][0]['text'] = json.dumps({
            'error': {'code': 'SOURCE_CHANGED'}, '_meta': {'request_id': 'changed-read'}})
        result = verify_mcp_text_delivery(self.root, json.dumps(event).encode())
        self.assertEqual(result['read_results'], 0)
        self.assertEqual(result['text_utf8_bytes'], 0)

    def test_unexplained_protocol_error_does_not_hide_a_source_result(self):
        _, _, event = self.source_result()
        event['item']['result']['isError'] = True
        with self.assertRaises(AuditError) as caught:
            verify_mcp_text_delivery(self.root, json.dumps(event).encode())
        self.assertEqual(caught.exception.code, 'MCP_DELIVERY')

    def test_every_read_result_must_reach_the_model_and_duplicate_events_do_not_add_bytes(self):
        payload, text, event = self.source_result()
        self.request({'model': 'selected', 'tools': [], 'input': [
            {'type': 'custom_tool_call_output', 'output': text}]})
        stdout = (json.dumps(event) + '\n') * 2
        result = verify_mcp_text_delivery(self.root, stdout.encode())
        self.assertEqual(result['read_results'], 1)
        self.assertEqual(result['text_utf8_bytes'], len(payload['text'].encode('utf-8')))
        _, _, missing = self.source_result('second-read')
        with self.assertRaises(AuditError) as caught:
            verify_mcp_text_delivery(self.root, (stdout + json.dumps(missing)).encode())
        self.assertEqual(caught.exception.details['missing_read_results'], 1)


if __name__ == '__main__':
    unittest.main()
