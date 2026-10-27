# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Verify the actual Codex request tool schemas without a model connection."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile

from src.messages import message as ui_message
from src.contracts.contracts import ContractError, strict_json
from src.runtime.errors import AuditError

MCP_AUXILIARY_TOOLS = frozenset({'list_mcp_resources', 'list_mcp_resource_templates',
                                'read_mcp_resource', 'request_user_input'})
MCP_NO_SOURCE_TOOLS = frozenset({'request_user_input'})


def _incompatible(message, **details):
    return AuditError(message, code='MCP_COMPATIBILITY', failure_kind='MCP_COMPATIBILITY',
                      failure_layer='compatibility',
                      details={'exclusive_source_access_verified': False, 'native_fallback': False, **details})


def _tools(value, namespace=''):
    if not isinstance(value, list):
        raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_returned_an_unsupported_tool_schema_list'))
    result = []
    for tool in value:
        if not isinstance(tool, dict) or not isinstance(tool.get('type'), str):
            raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_returned_an_unsupported_tool_schema'))
        name = tool.get('name')
        if tool['type'] == 'namespace':
            if not isinstance(name, str) or not name:
                raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_returned_an_unnamed_tool_namespace'))
            result.extend(_tools(tool.get('tools'), namespace + name + '.'))
            continue
        if tool['type'] not in ('function', 'custom') or not isinstance(name, str) or not name:
            raise _incompatible(ui_message(
                'diagnostic.codex_capabilities.codex_exposes_an_unsupported_native_tool',
            ), tool_type=tool['type'])
        qualified = namespace + name
        # Codex uses both flat MCP names and namespace schemas for the same tool.
        if qualified.startswith('functions.'):
            qualified = qualified.removeprefix('functions.')
        if qualified.startswith('mcp__'):
            qualified = qualified.replace('.', '__')
        result.append((qualified, tool))
    return result


def _read_trace_file(root, path):
    try:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_trace_contains_a_symbolic_link'))
        return strict_json(path.read_text(encoding='utf-8'))
    except AuditError:
        raise
    except Exception as error:
        raise _incompatible(ui_message('diagnostic.codex_capabilities.cannot_read_the_codex_capability_trace')) from error


def _requests(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_did_not_produce_a_private_capability_trace'))
    logs = sorted(root.glob('*/trace.jsonl'))
    if (root / 'trace.jsonl').is_file():
        logs.append(root / 'trace.jsonl')
    for log in logs:
        if log.is_symlink() or log.parent.is_symlink():
            raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_trace_contains_a_symbolic_link'))
        try:
            lines = log.read_text(encoding='utf-8').splitlines()
        except OSError as error:
            raise _incompatible(ui_message('diagnostic.codex_capabilities.cannot_read_the_codex_capability_trace')) from error
        for line in lines:
            try:
                event = strict_json(line)
                payload = event['payload']
            except Exception as error:
                raise _incompatible(ui_message(
                    'diagnostic.codex_capabilities.codex_returned_an_invalid_capability_trace',
                )) from error
            if not isinstance(payload, dict):
                raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_returned_an_invalid_capability_trace_event'))
            if payload.get('type') == 'inference_completed':
                yield log, payload, None
                continue
            if payload.get('type') != 'inference_started':
                continue
            reference = payload.get('request_payload')
            path = reference.get('path') if isinstance(reference, dict) else None
            if (not isinstance(path, str) or not path or '\\' in path
                    or ':' in path or PurePosixPath(path).is_absolute()
                    or any(part in ('', '.', '..') for part in path.split('/'))):
                raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_returned_an_unsafe_request_trace_path'))
            request = _read_trace_file(root, log.parent.joinpath(*path.split('/')))
            if not isinstance(request, dict) or not isinstance(request.get('model'), str) or not request['model']:
                raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_did_not_identify_the_effective_request_model'))
            if not isinstance(request.get('input'), list) or any(not isinstance(item, dict) for item in request['input']):
                raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_returned_an_unsupported_request_input_list'))
            yield log, payload, request


def verify_trace(root: Path, expected_tools, *, expected_model=None) -> dict:
    """Check every request, including tools carried in additional_tools input."""
    requests, models, schemas = 0, set(), {}
    expected = set(expected_tools)
    pending, responses, seen = {}, {}, set()
    for log, event, request in _requests(root):
        call_id = event.get('inference_call_id')
        if not isinstance(call_id, str) or not call_id:
            raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_omitted_the_inference_identity_needed_to_verify'))
        call_key = (log, call_id)
        if request is None:
            response_id = event.get('response_id')
            if (call_key not in pending or not isinstance(response_id, str) or not response_id
                    or (log, response_id) in responses):
                raise _incompatible(ui_message(
                    'diagnostic.codex_capabilities.codex_returned_an_unlinked_or_ambiguous_completed_inference',
                ))
            responses[(log, response_id)] = pending.pop(call_key)
            continue
        if call_key in seen:
            raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_reused_an_inference_identity_in_its_request'))
        seen.add(call_key)
        if expected_model is not None and request['model'] != expected_model:
            raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_changed_the_model_selected_for_the_mcp'),
                                expected_model=expected_model, model=request['model'])
        previous = request.get('previous_response_id')
        prior = None
        if previous is not None:
            if not isinstance(previous, str) or (log, previous) not in responses:
                raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_omitted_tool_schemas_without_a_verified_prior'))
            prior = responses[(log, previous)]
            if prior['model'] != request['model']:
                raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_changed_the_model_within_a_linked_inference'))
        declared = 'tools' in request
        tools = _tools(request.get('tools', []))
        for item in request['input']:
            if item.get('type') == 'additional_tools':
                declared = True
                tools.extend(_tools(item.get('tools')))
        if not declared and prior is None:
            raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_omitted_tool_schemas_without_a_verified_prior'))
        effective = {name: set(values) for name, values in prior['schemas'].items()} if prior is not None else {}
        for name, schema in tools:
            encoded = json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            effective.setdefault(name, set()).add(encoded)
        names = set(effective)
        if names != expected:
            raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_exposes_a_different_tool_set_than_the'),
                                unexpected_tools=sorted(names - expected), missing_tools=sorted(expected - names),
                                model=request['model'])
        models.add(request['model'])
        for name, values in effective.items():
            schemas.setdefault(name, set()).update(values)
        pending[call_key] = {'model': request['model'], 'schemas': effective}
        requests += 1
    if not requests or len(models) != 1:
        raise _incompatible(ui_message('diagnostic.codex_capabilities.codex_did_not_produce_a_complete_request_tool'))
    toolset = [{'name': name, 'schemas': sorted(values)} for name, values in sorted(schemas.items())]
    encoded = json.dumps(toolset, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {'model': models.pop(), 'tools': sorted(expected),
            'toolset_sha256': hashlib.sha256(encoded).hexdigest(), 'inference_requests': requests,
            'exclusive_source_access_verified': True, 'native_fallback': False}


def _contains_result(value, text, payload, depth=0):
    if depth > 32:
        return False
    if isinstance(value, str):
        if text in value:
            return True
        header, separator, body = value.partition('\nOutput:\n')
        if separator and header.startswith('Wall time: ') and header.endswith(' seconds'):
            seconds = header[len('Wall time: '):-len(' seconds')]
            digits = seconds.replace('.', '', 1)
            if digits and all(character in '0123456789' for character in digits):
                value = body
        try:
            value = strict_json(value)
        except ContractError:
            return False
        if isinstance(value, str):
            return _contains_result(value, text, payload, depth + 1)
    if isinstance(value, dict):
        return value == payload or any(_contains_result(item, text, payload, depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_contains_result(item, text, payload, depth + 1) for item in value)
    return False


def verify_mcp_text_delivery(root: Path, stdout: bytes) -> dict:
    """Verify completed source results in the model's actual tool-output input."""
    from src.backends.codex import MCP_SERVER_NAME
    results = {}
    for line in stdout.decode('utf-8', errors='replace').splitlines():
        try:
            event = strict_json(line)
        except ContractError:
            continue
        item = event.get('item') if isinstance(event, dict) else None
        if (not isinstance(item, dict) or event.get('type') != 'item.completed'
                or item.get('type') != 'mcp_tool_call' or item.get('server') != MCP_SERVER_NAME
                or item.get('tool') != 'read_source' or item.get('status') != 'completed' or item.get('error')):
            continue
        result = item.get('result')
        content = result.get('content') if isinstance(result, dict) else None
        try:
            if (not isinstance(item.get('id'), str) or not item['id']
                    or not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
                    or content[0].get('type') != 'text' or not isinstance(content[0].get('text'), str)):
                raise ValueError()
            text = content[0]['text']
            payload = strict_json(text)
            if not isinstance(payload, dict):
                raise ValueError()
            if isinstance(payload.get('error'), dict):
                continue
            if result.get('isError') or result.get('is_error'):
                raise ValueError()
            if not isinstance(payload.get('text'), str) or not isinstance(payload.get('_meta'), dict):
                raise ValueError()
        except (ContractError, ValueError):
            raise AuditError(ui_message(
                'diagnostic.codex_capabilities.codex_did_not_expose_the_complete_source_result',
            ), code='MCP_DELIVERY',
                             failure_kind='MCP_DELIVERY', failure_layer='backend',
                             details={'native_fallback': False}) from None
        results[(item.get('id'), text)] = payload
    remaining = dict(results)
    for _, _, request in _requests(root):
        if request is None:
            continue
        for item in request['input']:
            if item.get('type') not in ('function_call_output', 'custom_tool_call_output'):
                continue
            for key, payload in list(remaining.items()):
                if _contains_result(item.get('output'), key[1], payload):
                    del remaining[key]
    if remaining:
        raise AuditError(ui_message('diagnostic.codex_capabilities.codex_source_results_were_absent_or_truncated_in'),
                         code='MCP_DELIVERY', failure_kind='MCP_DELIVERY', failure_layer='backend',
                         details={'read_results': len(results), 'missing_read_results': len(remaining),
                                  'native_fallback': False})
    return {'read_results': len(results), 'delivered_read_results': len(results),
            'text_utf8_bytes': sum(len(payload['text'].encode('utf-8')) for payload in results.values()),
            'source_text_delivery_verified': True}


def probe_command(cmd, state: Path, env, *, expected_tools, process_fn, reporter=None, budget=None) -> dict:
    """Ask the stdio adapter to build a request with its offline file: provider."""
    root = Path(tempfile.mkdtemp(prefix='mcp-capability-trace-', dir=state))
    root.chmod(0o700)
    probe_env = dict(env, CODEX_ROLLOUT_TRACE_ROOT=str(root))
    command = list(cmd)
    if '--probe' not in command:
        command.append('--probe')
    expected_model = command[command.index('--model') + 1] if '--model' in command else None
    try:
        result = process_fn(command, state, probe_env, b'MCP capability probe.', reporter=reporter, budget=budget)
    except OSError as error:
        raise _incompatible(ui_message('diagnostic.codex_capabilities.cannot_launch_the_codex_capability_probe')) from error
    stdout, stderr = result.get('stdout', b''), result.get('stderr', b'')
    bootstrap_sources = set()
    for line in stdout.decode('utf-8', errors='replace').splitlines():
        try:
            event = strict_json(line)
        except ContractError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'thread.started':
            sources = event.get('bootstrap_instruction_sources', [])
            if not isinstance(sources, list) or any(not isinstance(source, str) for source in sources):
                raise _incompatible(ui_message(
                    'diagnostic.codex_capabilities.codex_returned_invalid_bootstrap_instruction_source_metadata',
                ))
            bootstrap_sources.update(sources)
        if event.get('type') not in ('error', 'turn.failed'):
            continue
        diagnostic = event.get('error', event)
        if not isinstance(diagnostic, dict) or diagnostic.get('code') not in ('MCP_COMPATIBILITY', 'MCP_STARTUP'):
            continue
        code = diagnostic['code']
        details = diagnostic.get('details')
        details = dict(details) if isinstance(details, dict) else {}
        details['native_fallback'] = False
        raise AuditError(diagnostic.get('message', ui_message(
            'diagnostic.codex_capabilities.the_codex_mcp_capability_probe_failed',
        )),
                         code=code, failure_kind=diagnostic.get('failure_kind', code),
                         failure_layer=diagnostic.get('failure_layer',
                                                      'compatibility' if code == 'MCP_COMPATIBILITY' else 'backend'),
                         hint=diagnostic.get('hint'), details=details)
    from src.backends.codex import mcp_failure
    failure = mcp_failure(stdout, stderr)
    if failure is not None:
        raise failure
    # The unsupported file: transport must fail. Only its recorded tool schemas
    # prove capabilities; neither exit status nor feature names prove them.
    proof = verify_trace(root, expected_tools, expected_model=expected_model)
    proof['bootstrap_instruction_sources'] = sorted(bootstrap_sources)
    return proof
