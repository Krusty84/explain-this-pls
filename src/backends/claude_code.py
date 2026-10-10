# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Claude Code CLI commands and structured-output parsing (stdlib only)."""
import json
from pathlib import Path

from src.contracts.contracts import ContractError, response_error, transport_json
from src.runtime.metrics import claude_usage


def help_command(executable: str) -> list[str]:
    return [executable, '--help']


def required_flags(mode: str) -> list[str]:
    return ['--no-session-persistence', '--json-schema', '--tools', '--allowedTools',
            '--disallowedTools', '--permission-mode', '--output-format', '--verbose',
            '--include-partial-messages']


def build_command(agent: dict, stage: str, mode: str, schema: dict,
                  schema_path: Path) -> list[str]:
    tools = '' if stage == 'compare' else 'Read,Glob,Grep'
    cmd = [agent['executable'], '-p', '--no-session-persistence', '--output-format', 'stream-json',
        '--verbose', '--include-partial-messages',
        '--json-schema', json.dumps(schema), '--permission-mode', 'dontAsk',
        '--tools', tools, '--disallowedTools', 'mcp__*']
    if tools:
        cmd += ['--allowedTools', tools]
    if agent.get('model'):
        cmd += ['--model', agent['model']]
    return cmd


def result_envelope(output: str) -> dict:
    """Read the final streaming result, retaining legacy JSON report compatibility."""
    try:
        value = transport_json(output.strip())
    except ContractError:
        value = None
    if type(value) is dict and 'is_error' in value:
        return value
    result = None
    for line in output.splitlines():
        if not line.strip():
            continue
        event = transport_json(line)
        if type(event) is not dict or type(event.get('type')) is not str:
            raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid Claude Code stream event.')
        if event['type'] == 'result':
            if result is not None and result != event:
                raise response_error('TRANSPORT_ERROR', 'transport', 'Conflicting Claude Code results.')
            result = event
    if result is None:
        raise response_error('INCOMPLETE_OUTPUT', 'result', 'Claude Code did not return a final result.')
    return result


def parse_output(output: str) -> tuple[dict, dict]:
    transport = result_envelope(output)
    if type(transport) is not dict or type(transport.get('is_error')) is not bool:
        raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid Claude Code result envelope.')
    if transport['is_error']:
        raise response_error('BACKEND_ERROR', 'backend', 'Claude Code returned an error result.')
    if type(transport.get('structured_output')) is not dict:
        raise response_error('INCOMPLETE_OUTPUT', 'result', 'Claude Code did not return structured_output.')
    metadata = {k: transport[k] for k in ('session_id', 'total_cost_usd', 'usage', 'modelUsage') if k in transport}
    return transport['structured_output'], metadata


def collect_metrics(output: str, requested=None) -> dict:
    try:
        envelope = result_envelope(output)
    except ContractError:
        envelope = None
    return claude_usage(envelope, requested)
