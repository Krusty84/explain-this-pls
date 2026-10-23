# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Stdio protocol integration and source isolation without a model call."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from src.backends.codex_catalog import sanitize_catalog


FAKE_SERVER = r'''
import json, os, sys, tomllib
from pathlib import Path

scenario = os.environ.get('MCP_TRANSPORT_SCENARIO', 'success')
log = Path(os.environ['MCP_TRANSPORT_LOG'])
settings = [sys.argv[index + 1] for index, value in enumerate(sys.argv[:-1]) if value == '-c']
config = {}
def merge(target, value):
    for key, item in value.items():
        if isinstance(item, dict): merge(target.setdefault(key, {}), item)
        else: target[key] = item
for setting in settings:
    key, _, raw = setting.partition('=')
    value = tomllib.loads('value=' + raw)['value']
    for part in reversed(key.split('.')): value = {part: value}
    merge(config, value)
servers = config.setdefault('mcp_servers', {})
for server in servers.values():
    server.setdefault('enabled', True)
    server.setdefault('environment_id', 'local')
if scenario.startswith('foreign'):
    name = 'foreign.name' if scenario == 'foreign_name' else 'foreign_server'
    servers.setdefault(name, {'command': 'untrusted', 'enabled': True})
    servers[name].setdefault('command', 'untrusted')
if scenario == 'plugins': config['plugins'].setdefault('visualize@openai-bundled', {'enabled': True})
if scenario == 'apps': config['apps'].setdefault('app-example', {'enabled': True})
if scenario == 'shell': config['features']['shell_tool'] = True
if scenario == 'notify': config['notify'] = ['untrusted-command']
if scenario == 'hooks': config['hooks'] = {'SessionStart': [{'command': 'untrusted'}]}
if scenario == 'instructions': config['model_instructions_file'] = '/untrusted/source'
if scenario == 'server_env': servers['explain_this_pls']['env'] = {'LD_PRELOAD': '/untrusted/library'}
if scenario == 'server_cap': servers['explain_this_pls']['tools']['read_source']['output_token_limit'] = 1
if scenario == 'server_approval': servers['explain_this_pls']['tools']['read_source']['approval_mode'] = 'prompt'
if scenario == 'server_extra_approval': servers['explain_this_pls']['tools']['shell'] = {'approval_mode': 'approve'}
if scenario == 'provider': config['model_providers']['inherited'] = {'auth': {'command': 'untrusted'}}

def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)

def note(method, params):
    send({'method': method, 'params': params})

for line in sys.stdin:
    request = json.loads(line)
    with log.open('a') as stream:
        stream.write(json.dumps(request, ensure_ascii=False) + '\n')
    method, identifier = request.get('method'), request.get('id')
    if method == 'initialize':
        if scenario == 'malformed': print('invalid protocol', flush=True); continue
        send({'id': identifier, 'result': {'userAgent': 'Fixture'}})
    elif method == 'config/read':
        send({'id': identifier, 'result': {'config': config, 'origins': {}}})
    elif method == 'thread/start':
        if scenario in ('startup_rpc', 'rpc_compat'):
            message = 'Required MCP servers failed to start: explain_this_pls' if scenario == 'startup_rpc' else 'Unsupported environment selection'
            send({'id': identifier, 'error': {'code': -32600, 'message': message}})
            continue
        thread = {'id': 'test-thread', 'environments': [], 'ephemeral': True}
        result = {'thread': thread, 'model': 'selected-model', 'runtimeWorkspaceRoots': [],
                  'instructionSources': [], 'approvalPolicy': 'never'}
        if scenario == 'environments': thread['environments'] = [{'environmentId': 'local'}]
        if scenario == 'roots': result['runtimeWorkspaceRoots'] = ['/untrusted/source']
        if scenario == 'sources': result['instructionSources'] = ['/untrusted/AGENTS.md']
        if scenario.startswith('bootstrap'): result['instructionSources'] = [str(Path(os.environ['CODEX_HOME']) / 'AGENTS.md')]
        if scenario == 'model': result['model'] = 'unlisted-model'
        send({'id': identifier, 'result': result})
        if scenario == 'startup':
            note('mcpServer/startupStatus/updated', {'name': 'explain_this_pls', 'status': 'failed'})
    elif method == 'turn/start':
        send({'id': identifier, 'result': {'turn': {'id': 'turn', 'status': 'inProgress'}}})
        note('turn/started', {'turn': {'id': 'turn'}})
        if scenario == 'interactive':
            send({'id': 90, 'method': 'item/commandExecution/requestApproval', 'params': {}})
            continue
        if scenario == 'native':
            note('item/started', {'item': {'id': 'native', 'type': 'commandExecution'}})
            continue
        if 'explain_this_pls' in servers and servers['explain_this_pls'].get('enabled') is not False:
            item = {'id': 'read', 'type': 'mcpToolCall', 'server': 'explain_this_pls', 'tool': 'read_source',
                    'arguments': {'source_id': 'root', 'path': 'source.py', 'start_line': 1, 'end_line': 2},
                    'status': 'completed', 'result': {'content': [{'type': 'text', 'text': '{"text":"nonce Ж😀\\nsecond"}'}],
                                                    'structuredContent': {'text': 'nonce Ж😀\nsecond'}}, 'error': None}
            if scenario == 'tool_error': item.update(status='failed', result=None, error={'message': 'Read failed'})
            note('item/completed', {'item': item})
        note('item/completed', {'item': {'id': 'final', 'type': 'agentMessage', 'text': '{"result":"nonce Ж😀"}'}})
        note('thread/tokenUsage/updated', {'tokenUsage': {'total': {'inputTokens': 42, 'outputTokens': 8,
              'cachedInputTokens': 10, 'reasoningOutputTokens': 3}}})
        note('turn/completed', {'turn': {'id': 'turn', 'status': 'failed' if scenario == 'failed' else 'completed'}})
'''


@unittest.skipIf(os.name == 'nt', 'The project subprocess runtime requires POSIX.')
class MCPTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.schema = self.root / 'schema.json'
        self.schema.write_text(json.dumps({'type': 'object', 'properties': {'result': {'type': 'string'}},
                                          'required': ['result'], 'additionalProperties': False}), encoding='utf-8')
        self.catalog = self.root / 'catalog.json'
        self.catalog.write_text(json.dumps(sanitize_catalog({'models': [{'slug': 'selected-model'}]})), encoding='utf-8')
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'source_root': str(self.root / 'source')}), encoding='utf-8')
        self.home = self.root / 'codex-home'
        self.home.mkdir()
        (self.home / 'AGENTS.md').write_text('Fixture instructions.', encoding='utf-8')
        self.executable = self.root / 'fake-codex'
        self.executable.write_text('#!' + sys.executable + '\n' + textwrap.dedent(FAKE_SERVER), encoding='utf-8')
        self.executable.chmod(0o700)
        self.log = self.root / 'protocol.jsonl'
        self.entry = Path(__file__).resolve().parents[1] / 'src' / 'backends' / 'codex_mcp.py'

    def invoke(self, scenario='success', *, source=True, extra=()):
        command = [sys.executable, '-I', '-B', str(self.entry), '--executable', str(self.executable),
                   '--schema', str(self.schema), '--catalog', str(self.catalog), '--model', 'selected-model']
        if source:
            command += ['--manifest', str(self.manifest), '--sha256', hashlib.sha256(self.manifest.read_bytes()).hexdigest()]
        result = subprocess.run(command + list(extra), cwd=self.root, input='Analyze only the provided inventory.',
                                env=dict(os.environ, CODEX_HOME=str(self.home), MCP_TRANSPORT_SCENARIO=scenario, MCP_TRANSPORT_LOG=str(self.log)),
                                capture_output=True, text=True, timeout=10)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        requests = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, events, requests

    def assert_failure(self, scenario, code='MCP_COMPATIBILITY', *, source=True):
        result, events, requests = self.invoke(scenario, source=source)
        self.assertEqual(result.returncode, 1, result.stderr)
        diagnostic = next(event for event in events if event['type'] == 'error')
        self.assertEqual(diagnostic['code'], code, result.stderr)
        self.assertFalse(diagnostic['native_fallback'])
        self.assertFalse(self.schema.with_name('final.response.json').exists())
        return requests

    def test_source_text_schema_coordinates_and_usage_survive_stdio_translation(self):
        result, events, requests = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        thread = next(request['params'] for request in requests if request.get('method') == 'thread/start')
        turn = next(request['params'] for request in requests if request.get('method') == 'turn/start')
        self.assertEqual(thread['environments'], [])
        self.assertTrue(thread['ephemeral'])
        self.assertEqual(turn['environments'], [])
        self.assertEqual(turn['outputSchema'], json.loads(self.schema.read_text()))
        self.assertEqual(turn['input'][0]['text'], 'Analyze only the provided inventory.')
        item = next(event['item'] for event in events if event.get('item', {}).get('type') == 'mcp_tool_call')
        self.assertEqual(item['arguments'], {'source_id': 'root', 'path': 'source.py', 'start_line': 1, 'end_line': 2})
        self.assertEqual(json.loads(item['result']['content'][0]['text'])['text'], 'nonce Ж😀\nsecond')
        self.assertEqual(item['result']['structuredContent']['text'], 'nonce Ж😀\nsecond')
        self.assertEqual(self.schema.with_name('final.response.json').read_text(), '{"result":"nonce Ж😀"}')
        self.assertEqual(events[-1], {'type': 'turn.completed', 'usage': {'input_tokens': 42,
            'output_tokens': 8, 'cached_input_tokens': 10, 'reasoning_output_tokens': 3}})

    def test_reports_only_transport_has_no_source_mcp(self):
        result, events, requests = self.invoke(source=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(event.get('item', {}).get('type') == 'mcp_tool_call' for event in events))
        self.assertEqual([request.get('method') for request in requests],
                         ['initialize', 'initialized', 'config/read', 'thread/start', 'turn/start'])

    def test_inherited_servers_are_disabled_before_any_thread_is_created(self):
        result, _, requests = self.invoke('foreign')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([request.get('method') for request in requests],
                         ['initialize', 'initialized', 'config/read', 'initialize', 'initialized',
                          'config/read', 'thread/start', 'turn/start'])

    def test_unsupported_inherited_server_name_fails_before_thread_creation(self):
        requests = self.assert_failure('foreign_name')
        self.assertNotIn('thread/start', [request.get('method') for request in requests])

    def test_inherited_plugins_and_apps_are_disabled_before_thread_creation(self):
        for scenario in ('plugins', 'apps'):
            with self.subTest(scenario=scenario):
                self.log.unlink(missing_ok=True)
                result, _, requests = self.invoke(scenario)
                self.assertEqual(result.returncode, 0, result.stderr)
                methods = [request.get('method') for request in requests]
                self.assertEqual(methods.count('initialize'), 2)
                self.assertEqual(methods.index('thread/start'), 6)

    def test_effective_unsafe_configuration_fails_before_thread_creation(self):
        for scenario in ('shell', 'notify', 'hooks', 'instructions', 'server_env', 'server_cap',
                         'server_approval', 'server_extra_approval', 'provider'):
            with self.subTest(scenario=scenario):
                self.log.unlink(missing_ok=True)
                requests = self.assert_failure(scenario)
                self.assertNotIn('thread/start', [request.get('method') for request in requests])

    def test_thread_isolation_and_model_identity_are_checked_before_turn(self):
        for scenario in ('environments', 'roots', 'sources', 'model'):
            with self.subTest(scenario=scenario):
                self.log.unlink(missing_ok=True)
                requests = self.assert_failure(scenario)
                self.assertNotIn('turn/start', [request.get('method') for request in requests])

    def test_only_regular_disjoint_home_instructions_are_recorded_as_bootstrap(self):
        result, events, _ = self.invoke('bootstrap')
        self.assertEqual(result.returncode, 0, result.stderr)
        thread = next(event for event in events if event['type'] == 'thread.started')
        self.assertEqual(thread['bootstrap_instruction_sources'], [str(self.home / 'AGENTS.md')])

    def test_home_instruction_symlink_or_source_overlap_fails_before_model_turn(self):
        target = self.home / 'AGENTS.md'
        target.unlink()
        target.symlink_to(self.schema)
        requests = self.assert_failure('bootstrap_symlink')
        self.assertNotIn('turn/start', [request.get('method') for request in requests])
        target.unlink()
        target.write_text('Fixture instructions.')
        self.manifest.write_text(json.dumps({'source_root': str(self.root)}))
        requests = self.assert_failure('bootstrap_source')
        self.assertNotIn('turn/start', [request.get('method') for request in requests])

    def test_source_server_startup_failure_is_explicit(self):
        self.assert_failure('startup', 'MCP_STARTUP')

    def test_direct_startup_rpc_error_requires_the_named_required_server(self):
        self.assert_failure('startup_rpc', 'MCP_STARTUP')
        self.assert_failure('rpc_compat')

    def test_unexpected_interactive_or_native_operations_fail_closed(self):
        for scenario in ('interactive', 'native', 'malformed'):
            with self.subTest(scenario=scenario):
                self.log.unlink(missing_ok=True)
                self.assert_failure(scenario)

    def test_tool_errors_retain_the_observed_error_envelope(self):
        result, events, _ = self.invoke('tool_error')
        self.assertEqual(result.returncode, 0, result.stderr)
        item = next(event['item'] for event in events if event.get('item', {}).get('type') == 'mcp_tool_call')
        self.assertEqual(item['status'], 'failed')
        self.assertEqual(item['error'], {'message': 'Read failed'})
        self.assertIsNone(item['result'])

    def test_failed_turn_does_not_publish_a_final_response(self):
        self.assert_failure('failed', 'CLI_FAILED')

    def test_output_path_override_is_preserved(self):
        target = self.root / 'chosen-response.json'
        result, _, _ = self.invoke(extra=('--output-last-message', str(target)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(target.read_text()), {'result': 'nonce Ж😀'})
        self.assertFalse(self.schema.with_name('final.response.json').exists())

    def test_probe_uses_file_provider_without_authentication(self):
        result, _, _ = self.invoke(source=False, extra=('--probe',))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
