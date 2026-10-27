# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Codex stdio app-server transport with no native source environment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.dont_write_bytecode = True

from src.messages import message as ui_message
from src.backends.codex import MCP_SERVER_NAME, _mcp_overrides
from src.backends.codex_catalog import sanitize_catalog
from src.analysis.evidence import read_confined
from src.contracts.contracts import ContractError, strict_json
from src.runtime.errors import AuditError


DISABLED_FEATURES = ('shell_tool', 'view_image', 'multi_agent', 'multi_agent_v2', 'apps', 'plugins',
                     'code_mode', 'code_mode_host', 'computer_use', 'browser_use', 'browser_use_external',
                     'in_app_browser', 'image_generation', 'hooks', 'memories', 'workspace_dependencies',
                     'shell_snapshot', 'goals', 'skill_search', 'tool_suggest', 'stable_environment_tools',
                     'unified_exec', 'skill_mcp_dependency_install', 'api_key_model_discovery',
                     'remote_plugin', 'auth_elicitation')


def failure(message, code='MCP_COMPATIBILITY'):
    return AuditError(message, code=code, failure_kind=code,
                      failure_layer='backend' if code != 'MCP_COMPATIBILITY' else 'compatibility',
                      details={'native_fallback': False})


def emit(event):
    print(json.dumps(event, ensure_ascii=False, separators=(',', ':')), flush=True)


def empty_settings(value):
    if isinstance(value, dict):
        return all(empty_settings(item) for item in value.values())
    if isinstance(value, list):
        return all(empty_settings(item) for item in value)
    return value is None


def app_command(args, disabled_servers=(), disabled_plugins=(), disabled_apps=()):
    settings = [('mcp_servers', '{}'), ('model_providers', '{}'), ('hooks', '{}'), ('notify', '[]'),
                ('plugins', '{}'), ('apps', '{}'), ('agents.enabled', 'false'),
                ('approval_policy', '"never"'), ('sandbox_mode', '"read-only"'),
                ('web_search', '"disabled"'), ('project_doc_max_bytes', '0'),
                ('skills.bundled.enabled', 'false'), ('skills.include_instructions', 'false'),
                ('developer_instructions', '""'), ('instructions', '""'),
                ('model_catalog_json', json.dumps(str(args.catalog))),
                ('model_provider', '"explain_probe"' if args.probe else '"openai"')]
    settings += [('features.' + feature, 'false') for feature in DISABLED_FEATURES]
    settings += [('features.skip_host_skill_discovery', 'true')]
    if args.model:
        settings.append(('model', json.dumps(args.model)))
    if args.reasoning_effort:
        settings.append(('model_reasoning_effort', json.dumps(args.reasoning_effort)))
    if args.probe:
        settings += [('model_providers.explain_probe.' + key, value) for key, value in (
            ('name', '"Offline capability probe"'), ('base_url', '"file:///unavailable"'),
            ('wire_api', '"responses"'), ('requires_openai_auth', 'false'),
            ('request_max_retries', '0'), ('stream_max_retries', '0'))]
    command = [args.executable, 'app-server', '--stdio', '--strict-config']
    for key, value in settings:
        command += ['-c', key + '=' + value]
    if args.manifest:
        command += _mcp_overrides(args.manifest, args.sha256)
        for setting in ('enabled=true', 'env_vars=[]', 'env={}'):
            command += ['-c', 'mcp_servers.' + MCP_SERVER_NAME + '.' + setting]
    for key, names, extra in (('mcp_servers', disabled_servers, ''),
                              ('plugins', disabled_plugins, '@'), ('apps', disabled_apps, '@')):
        for name in names:
            if not name or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' + extra for char in name):
                raise failure(ui_message('diagnostic.codex_mcp.codex_cannot_safely_override_an_inherited_name', value=key))
            command += ['-c', key + '.' + name + '.enabled=false']
    return command


def check_config(config, args):
    if not isinstance(config, dict):
        raise failure(ui_message('diagnostic.codex_mcp.codex_did_not_return_its_effective_configuration'))
    features = config.get('features', {})
    if not isinstance(features, dict) or any(features.get(key) is not False for key in DISABLED_FEATURES):
        raise failure(ui_message('diagnostic.codex_mcp.codex_did_not_disable_every_required_native_tool'))
    if features.get('skip_host_skill_discovery') is not True:
        raise failure(ui_message('diagnostic.codex_mcp.codex_cannot_exclude_host_skill_discovery'))
    hooks = config.get('hooks')
    if isinstance(hooks, dict):
        # Persisted trust decisions are metadata, not executable hook handlers.
        state = hooks.get('state', {})
        state_types = {'enabled': bool, 'trusted_hash': str, 'trusted_content_hash': str}
        if not isinstance(state, dict) or any(not isinstance(entry, dict) or any(
                key not in state_types or type(value) is not state_types[key]
                for key, value in entry.items()) for entry in state.values()):
            raise failure(ui_message('diagnostic.codex_mcp.codex_returned_unsupported_hook_trust_state'))
        hooks = {key: value for key, value in hooks.items() if key != 'state'}
    if config.get('notify') not in (None, []) or not empty_settings(hooks):
        raise failure(ui_message('diagnostic.codex_mcp.codex_retains_notification_commands_or_hooks'))
    for key in ('apps', 'plugins'):
        entries = config.get(key)
        if entries is not None and (not isinstance(entries, dict) or any(
                entry is not None and (not isinstance(entry, dict) or entry.get('enabled') is not False)
                for entry in entries.values())):
            raise failure(ui_message('diagnostic.codex_mcp.codex_retains_enabled_app_or_plugin_configuration'))
    if not isinstance(config.get('agents'), dict) or config['agents'].get('enabled') is not False:
        raise failure(ui_message('diagnostic.codex_mcp.codex_retains_native_agent_delegation'))
    if config.get('web_search') != 'disabled' or config.get('project_doc_max_bytes') != 0:
        raise failure(ui_message('diagnostic.codex_mcp.codex_retains_web_search_or_project_instruction_discovery'))
    skills = config.get('skills', {})
    if (not isinstance(skills, dict) or not isinstance(skills.get('bundled'), dict)
            or skills['bundled'].get('enabled') is not False or skills.get('include_instructions') is not False):
        raise failure(ui_message('diagnostic.codex_mcp.codex_retains_automatic_skill_instructions'))
    if any(config.get(key) for key in ('model_instructions_file', 'experimental_instructions_file',
                                      'experimental_compact_prompt_file', 'compact_prompt',
                                      'developer_instructions', 'instructions')):
        raise failure(ui_message('diagnostic.codex_mcp.codex_retains_external_instructions_outside_the_mcp_prompt'))
    if config.get('model_provider') != ('explain_probe' if args.probe else 'openai'):
        raise failure(ui_message('diagnostic.codex_mcp.codex_did_not_select_the_required_model_provider'))
    providers = config.get('model_providers', {})
    if not isinstance(providers, dict) or set(providers) != ({'explain_probe'} if args.probe else set()):
        raise failure(ui_message('diagnostic.codex_mcp.codex_retains_inherited_model_provider_configuration'))
    if args.probe:
        provider = providers['explain_probe']
        if (provider.get('base_url') != 'file:///unavailable' or provider.get('requires_openai_auth') is not False
                or provider.get('request_max_retries') != 0 or provider.get('stream_max_retries') != 0
                or any(provider.get(key) for key in ('auth', 'env_key', 'experimental_bearer_token',
                                                     'gateway_oauth', 'aws', 'model_catalog_url',
                                                     'http_headers', 'env_http_headers', 'query_params'))):
            raise failure(ui_message('diagnostic.codex_mcp.codex_retained_authentication_or_network_settings_for_the'))
    servers = config.get('mcp_servers', {})
    expected = {MCP_SERVER_NAME} if args.manifest else set()
    if (not isinstance(servers, dict)
            or {name for name, server in servers.items()
                if not isinstance(server, dict) or server.get('enabled') is not False} != expected):
        raise failure(ui_message('diagnostic.codex_mcp.codex_retains_an_unexpected_mcp_server'))
    if args.manifest:
        server = servers[MCP_SERVER_NAME]
        overrides = _mcp_overrides(args.manifest, args.sha256)
        expected_settings = dict(overrides[index + 1].split('=', 1) for index in range(0, len(overrides), 2))
        root = 'mcp_servers.' + MCP_SERVER_NAME + '.'
        if (not isinstance(server, dict) or server.get('required') is not True or server.get('enabled') is not True
                or server.get('command') != json.loads(expected_settings[root + 'command'])
                or server.get('args') != json.loads(expected_settings[root + 'args'])
                or server.get('cwd') != str(args.manifest.parent)
                or server.get('startup_timeout_sec') != 30 or server.get('tool_timeout_sec') != 30
                or server.get('enabled_tools') != ['search_symbols', 'read_source']):
            raise failure(ui_message('diagnostic.codex_mcp.codex_changed_the_required_private_source_mcp_configuration'))
        allowed = {'command', 'args', 'cwd', 'env', 'env_vars', 'environment_id', 'enabled', 'required',
                   'startup_timeout_sec', 'tool_timeout_sec', 'enabled_tools', 'tools'}
        if (any(key not in allowed and value is not None for key, value in server.items())
                or not empty_settings(server.get('env')) or not empty_settings(server.get('env_vars'))
                or server.get('environment_id') not in (None, 'local')
                or server.get('tools') != {name: {'output_token_limit': 1000000, 'approval_mode': 'approve'}
                                           for name in ('read_source', 'search_symbols')}):
            raise failure(ui_message('diagnostic.codex_mcp.codex_retained_unsafe_source_mcp_environment_transport_or'))


class Connection:
    def __init__(self, child, args):
        self.child, self.args = child, args
        self.sequence, self.done = 0, False
        self.last_message, self.usage, self.turn_status = None, {}, None

    def send(self, value):
        self.child.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
        self.child.stdin.flush()

    def receive(self):
        raw = self.child.stdout.readline()
        if not raw:
            raise failure(ui_message('diagnostic.codex_mcp.codex_app_server_closed_its_stdio_connection'),
                          'MCP_STARTUP' if self.args.manifest and not self.done else 'MCP_COMPATIBILITY')
        try:
            value = strict_json(raw.decode('utf-8'))
        except (ContractError, UnicodeDecodeError) as error:
            raise failure(ui_message('diagnostic.codex_mcp.codex_app_server_returned_invalid_protocol_json')) from error
        if not isinstance(value, dict):
            raise failure(ui_message('diagnostic.codex_mcp.codex_app_server_returned_an_invalid_protocol_message'))
        if 'method' in value:
            if 'id' in value:
                self.send({'jsonrpc': '2.0', 'id': value['id'],
                           'error': {'code': -32601, 'message': ui_message(
                               'diagnostic.codex_mcp.interactive_server_requests_are_disabled',
                           )}})
                raise failure(ui_message('diagnostic.codex_mcp.codex_requested_an_interactive_operation_outside_the_mcp'))
            self.notification(value['method'], value.get('params', {}))
        return value

    def request(self, method, params):
        self.sequence += 1
        identifier = self.sequence
        self.send({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params})
        while True:
            value = self.receive()
            if value.get('id') == identifier:
                error = value.get('error')
                message = error.get('message', '') if isinstance(error, dict) else ''
                if (method == 'thread/start' and self.args.manifest and isinstance(message, str)
                        and 'required mcp' in message.casefold() and MCP_SERVER_NAME in message.casefold()):
                    raise failure(ui_message('diagnostic.codex_mcp.required_mcp_server_explain_this_pls_failed_to'), 'MCP_STARTUP')
                if value.get('error') is not None or not isinstance(value.get('result'), dict):
                    raise failure(ui_message(
                        'diagnostic.codex_mcp.codex_rejected_the_required_app_server_operation',
                        method=method,
                    ))
                return value['result']

    def notification(self, method, params):
        if not isinstance(params, dict):
            raise failure(ui_message('diagnostic.codex_mcp.codex_app_server_returned_invalid_notification_parameters'))
        if method == 'mcpServer/startupStatus/updated':
            if params.get('name') != MCP_SERVER_NAME or not self.args.manifest:
                raise failure(ui_message('diagnostic.codex_mcp.codex_started_an_unexpected_mcp_server'))
            if params.get('status') == 'failed':
                raise failure(ui_message('diagnostic.codex_mcp.required_mcp_server_explain_this_pls_failed_to'), 'MCP_STARTUP')
        elif method in ('item/started', 'item/completed'):
            self.item(method, params.get('item', {}))
        elif method == 'turn/started':
            emit({'type': 'turn.started'})
        elif method == 'thread/tokenUsage/updated':
            raw = params.get('tokenUsage', {}).get('total', {})
            self.usage = {target: raw.get(source) for target, source in (
                ('input_tokens', 'inputTokens'), ('output_tokens', 'outputTokens'),
                ('cached_input_tokens', 'cachedInputTokens'), ('reasoning_output_tokens', 'reasoningOutputTokens'))}
        elif method == 'turn/completed':
            self.turn_status = params.get('turn', {}).get('status')
            self.done = True
        elif method == 'error' and not params.get('willRetry'):
            print(ui_message(
                'diagnostic.codex_mcp.server_error',
                error=str(params.get('error', {}).get('message', ui_message('diagnostic.codex_mcp.turn_failed'))),
            ), file=sys.stderr)

    def item(self, method, item):
        if not isinstance(item, dict):
            raise failure(ui_message('diagnostic.codex_mcp.codex_returned_an_invalid_turn_item'))
        kind = item.get('type')
        if kind == 'mcpToolCall':
            if (not self.args.manifest or item.get('server') != MCP_SERVER_NAME
                    or item.get('tool') not in ('search_symbols', 'read_source')):
                raise failure(ui_message('diagnostic.codex_mcp.codex_called_a_tool_outside_the_private_source'))
            normalized = {key: item.get(key) for key in ('id', 'server', 'tool', 'arguments', 'result', 'error', 'status')}
            normalized['type'] = 'mcp_tool_call'
        elif kind == 'agentMessage':
            normalized = {'id': item.get('id'), 'type': 'agent_message', 'text': item.get('text')}
            if method == 'item/completed':
                self.last_message = item.get('text')
        elif kind in ('userMessage', 'reasoning', 'plan', 'contextCompaction'):
            return
        else:
            raise failure(ui_message('diagnostic.codex_mcp.codex_exposed_an_unexpected_native_turn_item', kind=str(kind)))
        emit({'type': method.replace('/', '.'), 'item': normalized})


def stop(child):
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=2)
    child.stdin.close()
    child.stdout.close()


def bootstrap_instructions(values, args):
    if not isinstance(values, list):
        raise failure(ui_message('diagnostic.codex_mcp.codex_did_not_identify_its_bootstrap_instruction_sources'))
    configured_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    home = configured_home.resolve()
    allowed = {home / 'AGENTS.md', home / 'AGENTS.override.md'}
    paths = []
    for value in values:
        if not isinstance(value, str):
            raise failure(ui_message('diagnostic.codex_mcp.codex_returned_an_invalid_bootstrap_instruction_path'))
        path = Path(value)
        if path.is_absolute() and path.parent == configured_home:
            path = home / path.name
        if not path.is_absolute() or path not in allowed or any(part.is_symlink() for part in (path, *path.parents)):
            raise failure(ui_message('diagnostic.codex_mcp.codex_loaded_project_instructions_outside_the_permitted_home'))
        if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
            raise failure(ui_message('diagnostic.codex_mcp.codex_bootstrap_instructions_are_not_regular_home_configuration'))
        paths.append(str(path))
    if paths and args.manifest:
        content = read_confined(args.manifest.parent, args.manifest.name)
        if hashlib.sha256(content).hexdigest() != args.sha256:
            raise failure(ui_message('diagnostic.codex_mcp.the_private_mcp_manifest_changed_before_thread_creation'), 'MCP_STARTUP')
        source_root = Path(strict_json(content.decode('utf-8'))['source_root'])
        if any(path == source_root or source_root in path.parents for path in map(Path, paths)):
            raise failure(ui_message('diagnostic.codex_mcp.codex_home_instructions_overlap_the_pinned_source_directory'))
    return paths


def run(args, prompt):
    schema = strict_json(args.schema.read_text(encoding='utf-8'))
    catalog = strict_json(args.catalog.read_text(encoding='utf-8'))
    if sanitize_catalog(catalog) != catalog:
        raise failure(ui_message('diagnostic.codex_mcp.mcp_requires_the_pinned_catalog_with_native_model'))
    child = None
    try:
        disabled_servers, disabled_plugins, disabled_apps = [], [], []
        for attempt in range(2):
            child = subprocess.Popen(app_command(args, disabled_servers, disabled_plugins, disabled_apps), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=None, close_fds=True)
            connection = Connection(child, args)
            connection.request('initialize', {'clientInfo': {'name': 'explain-this-pls', 'version': '1'},
                                             'capabilities': {'experimentalApi': True}})
            connection.send({'jsonrpc': '2.0', 'method': 'initialized'})
            config = connection.request('config/read', {'includeLayers': False, 'cwd': str(Path.cwd())}).get('config')
            servers = config.get('mcp_servers') if isinstance(config, dict) else None
            if not isinstance(servers, dict):
                raise failure(ui_message('diagnostic.codex_mcp.codex_did_not_return_its_effective_mcp_server'))
            foreign = sorted(name for name, server in servers.items()
                             if (name != MCP_SERVER_NAME or not args.manifest)
                             and (not isinstance(server, dict) or server.get('enabled') is not False))
            extras = []
            for key in ('plugins', 'apps'):
                entries = config.get(key) or {}
                if not isinstance(entries, dict):
                    raise failure(ui_message('diagnostic.codex_mcp.codex_returned_invalid_inherited_settings', value=key))
                extras.append(sorted(name for name, entry in entries.items()
                                     if entry is not None and (not isinstance(entry, dict) or entry.get('enabled') is not False)))
            if not foreign and not any(extras):
                break
            if attempt:
                raise failure(ui_message('diagnostic.codex_mcp.codex_cannot_disable_inherited_tools_before_thread_creation'))
            disabled_servers, (disabled_plugins, disabled_apps) = foreign, extras
            stop(child)
            child = None
        check_config(config, args)
        params = {'ephemeral': True, 'cwd': str(Path.cwd()), 'approvalPolicy': 'never',
                  'sandbox': 'read-only', 'environments': []}
        if args.model:
            params['model'] = args.model
        result = connection.request('thread/start', params)
        thread = result.get('thread', {})
        if (thread.get('environments') != [] or result.get('runtimeWorkspaceRoots') != []
                or thread.get('ephemeral') is not True
                or result.get('approvalPolicy') != 'never'):
            raise failure(ui_message('diagnostic.codex_mcp.codex_did_not_create_an_isolated_thread_without'))
        bootstrap = bootstrap_instructions(result.get('instructionSources'), args)
        if result.get('model') not in {model['slug'] for model in catalog['models']}:
            raise failure(ui_message('diagnostic.codex_mcp.codex_selected_a_model_outside_the_pinned_catalog'))
        emit({'type': 'thread.started', 'thread_id': thread['id'], 'bootstrap_instruction_sources': bootstrap})
        turn = {'threadId': thread['id'], 'environments': [],
                'input': [{'type': 'text', 'text': prompt}], 'outputSchema': schema}
        if args.reasoning_effort:
            turn['effort'] = args.reasoning_effort
        connection.request('turn/start', turn)
        while not connection.done:
            connection.receive()
        if connection.turn_status != 'completed':
            raise failure(ui_message('diagnostic.codex_mcp.codex_did_not_complete_the_mcp_analysis_turn'), 'CLI_FAILED')
        if not isinstance(connection.last_message, str) or not connection.last_message.strip():
            raise failure(ui_message('diagnostic.codex_mcp.codex_completed_without_a_final_response'), 'CLI_FAILED')
        final_path = args.output_last_message or args.schema.with_name('final.response.json')
        descriptor, temporary = tempfile.mkstemp(prefix='.final-response-', dir=final_path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
                stream.write(connection.last_message)
            os.replace(temporary, final_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        emit({'type': 'turn.completed', 'usage': connection.usage})
    finally:
        if child is not None:
            stop(child)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True)
    parser.add_argument('--schema', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--output-last-message', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--reasoning-effort')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    try:
        if (not args.schema.is_absolute() or not args.catalog.is_absolute()
                or (args.manifest is not None and not args.manifest.is_absolute())
                or (args.output_last_message is not None and not args.output_last_message.is_absolute())
                or bool(args.manifest) != bool(args.sha256)):
            raise failure(ui_message('diagnostic.codex_mcp.mcp_transport_requires_absolute_private_artifact_paths_and'))
        run(args, sys.stdin.read())
    except (AuditError, OSError, ContractError, UnicodeError) as error:
        code = error.code if isinstance(error, AuditError) else 'MCP_COMPATIBILITY'
        message = str(error) if isinstance(error, AuditError) else ui_message('diagnostic.codex_mcp.transport_failed')
        print(code + ': ' + message, file=sys.stderr, flush=True)
        emit({'type': 'error', 'code': code, 'message': message, 'native_fallback': False})
        emit({'type': 'turn.failed', 'error': {'message': message}})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
