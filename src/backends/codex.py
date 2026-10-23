# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Codex CLI commands and structured-output parsing (stdlib only)."""
import json
from pathlib import Path
import sys

from src.contracts.contracts import ContractError, response_error, strict_json
from src.runtime.metrics import codex_usage
from src.runtime.errors import AuditError

MCP_SERVER_NAME = 'explain_this_pls'


def help_command(executable: str, *, mcp_mode=False) -> list[str]:
    return [executable, 'app-server' if mcp_mode else 'exec', '--help']


def required_flags(mode: str, *, writer_only=False, mcp_mode=False) -> list[str]:
    if mcp_mode:
        return ['--stdio', '--config', '--strict-config']
    flags = ['--ephemeral', '--output-schema', '--sandbox', '--json', '--output-last-message']
    flags.append('--skip-git-repo-check')
    if writer_only:
        flags += ['--ignore-user-config', '--ignore-rules']
    return flags


def check_mcp_compatibility(version: str, features: str) -> None:
    """Check prerequisites; only an actual request trace proves exclusivity."""
    shell_supported = any(line.split()[:1] == ['shell_tool'] and 'removed' not in line.split()
                          for line in features.splitlines())
    if shell_supported:
        return
    raise AuditError('Codex has no independent shell_tool control.',
                     code='MCP_COMPATIBILITY', failure_kind='MCP_COMPATIBILITY',
                     failure_layer='compatibility',
                     details={'cli_version': version, 'shell_tool_supported': shell_supported,
                              'exclusive_source_access_verified': False, 'native_fallback': False})


def _mcp_overrides(manifest: Path, sha256: str) -> list[str]:
    server = Path(__file__).resolve().parents[1] / 'mcp' / 'server.py'
    settings = {'command': sys.executable,
                'args': ['-I', '-B', str(server), '--manifest', str(manifest), '--sha256', sha256],
                'cwd': str(manifest.parent), 'required': True,
                'enabled_tools': ['search_symbols', 'read_source'],
                'startup_timeout_sec': 30, 'tool_timeout_sec': 30,
                'tools.search_symbols.approval_mode': 'approve',
                'tools.read_source.approval_mode': 'approve',
                'tools.search_symbols.output_token_limit': 1000000,
                'tools.read_source.output_token_limit': 1000000}
    result = []
    for key, value in settings.items():
        result += ['-c', 'mcp_servers.' + MCP_SERVER_NAME + '.' + key + '=' + json.dumps(value, ensure_ascii=False)]
    return result


def mcp_failure(stdout: bytes, stderr: bytes) -> AuditError | None:
    """Classify a required server's explicit startup diagnostic on CLI failure."""
    messages = [stderr.decode('utf-8', errors='replace')]
    for line in stdout.decode('utf-8', errors='replace').splitlines():
        try:
            event = strict_json(line)
        except ContractError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'error' and event.get('code') in ('MCP_STARTUP', 'MCP_COMPATIBILITY'):
            code = event['code']
            return AuditError(event.get('message', 'Codex MCP transport failed.'),
                              code=code, failure_kind=code,
                              failure_layer='compatibility' if code == 'MCP_COMPATIBILITY' else 'backend',
                              details={'native_fallback': False})
        if (event.get('type') == 'mcp_startup_update' and event.get('server') == MCP_SERVER_NAME
                and event.get('status') == 'failed'):
            messages.append('MCP startup failed: ' + MCP_SERVER_NAME)
        elif event.get('type') in ('error', 'turn.failed'):
            messages.append(json.dumps(event))
    for message in messages:
        normalized = message.casefold()
        if (MCP_SERVER_NAME in normalized and any(marker in normalized for marker in
                ('required mcp', 'mcp startup', 'failed to start', 'failed to initialize', 'mcp client for'))):
            return AuditError('The required source MCP server failed to start; inspect private attempt logs.',
                              code='MCP_STARTUP', failure_kind='MCP_STARTUP', failure_layer='backend',
                              details={'server': MCP_SERVER_NAME, 'native_fallback': False})
    return None


def build_command(agent: dict, stage: str, mode: str, schema: dict,
                  schema_path: Path, *, writer_only=False, mcp_mode=False,
                  mcp_manifest: Path | None = None, mcp_manifest_sha256: str | None = None,
                  mcp_catalog: Path | None = None) -> list[str]:
    compare = stage == 'compare'
    mcp_mode = mcp_mode or mcp_manifest is not None
    if mcp_manifest is not None:
        if compare or writer_only:
            raise AuditError('MCP is unavailable to the report writer and comparison stages.',
                             code='MCP_COMPATIBILITY', failure_layer='compatibility')
        if (not mcp_manifest.is_absolute() or not isinstance(mcp_manifest_sha256, str)
                or len(mcp_manifest_sha256) != 64
                or any(char not in '0123456789abcdef' for char in mcp_manifest_sha256)):
            raise AuditError('MCP requires an absolute private manifest path and its SHA-256.',
                             code='MCP_STARTUP', failure_layer='integrity')
    elif mcp_mode and not (compare or writer_only):
        raise AuditError('The source-reading stage has no private MCP manifest.',
                         code='MCP_STARTUP', failure_layer='execution')
    if mcp_mode:
        if mcp_catalog is None or not mcp_catalog.is_absolute():
            raise AuditError('MCP requires a pinned Codex model catalog.',
                             code='MCP_COMPATIBILITY', failure_layer='compatibility')
        adapter = Path(__file__).with_name('codex_mcp.py').resolve()
        cmd = [sys.executable, '-I', '-B', str(adapter), '--executable', agent['executable'],
               '--schema', str(schema_path), '--catalog', str(mcp_catalog),
               '--output-last-message', str(schema_path.with_name('final.response.json'))]
        if mcp_manifest is not None:
            cmd += ['--manifest', str(mcp_manifest), '--sha256', mcp_manifest_sha256]
        if agent.get('model'):
            cmd += ['--model', agent['model']]
        if agent.get('reasoning_effort') is not None:
            cmd += ['--reasoning-effort', agent['reasoning_effort']]
        return cmd
    cmd = [agent['executable'], 'exec', '--ephemeral', '--color', 'never', '--sandbox', 'read-only',
        '--output-schema', str(schema_path), '--json',
        '--output-last-message', str(schema_path.with_name('final.response.json')), '-c', 'approval_policy="never"',
        '-c', 'web_search="disabled"']
    cmd += ['--skip-git-repo-check']
    if writer_only:
        cmd += ['--ignore-user-config', '--ignore-rules']
    if compare or writer_only:
        cmd += ['-c', 'features.shell_tool=false']
    if agent.get('model'):
        cmd += ['--model', agent['model']]
    if agent.get('reasoning_effort') is not None:
        cmd += ['-c', 'model_reasoning_effort=' + json.dumps(agent['reasoning_effort'])]
    return cmd + ['-']


def parse_output(output: str) -> tuple[dict, dict]:
    return strict_json(output.strip()), {}


def read_response(schema_path: Path) -> str:
    try:
        return schema_path.with_name('final.response.json').read_text(encoding='utf-8')
    except FileNotFoundError:
        raise response_error('INCOMPLETE_OUTPUT', 'result', 'Codex did not write a final response.') from None


def collect_metrics(output: str, requested=None) -> dict:
    events, malformed = [], False
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            events.append(strict_json(line))
        except ContractError:
            malformed = True
    return codex_usage(events, requested, malformed=malformed)
