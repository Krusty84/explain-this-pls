# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Codex CLI commands and structured-output parsing (stdlib only)."""
from pathlib import Path

from src.contracts.contracts import ContractError, response_error, strict_json
from src.runtime.metrics import codex_usage


def help_command(executable: str) -> list[str]:
    return [executable, 'exec', '--help']


def required_flags(mode: str) -> list[str]:
    flags = ['--ephemeral', '--output-schema', '--sandbox', '--json', '--output-last-message']
    flags.append('--skip-git-repo-check')
    return flags


def build_command(agent: dict, stage: str, mode: str, schema: dict,
                  schema_path: Path) -> list[str]:
    compare = stage == 'compare'
    cmd = [agent['executable'], 'exec', '--ephemeral', '--color', 'never', '--sandbox', 'read-only',
        '--output-schema', str(schema_path), '--json',
        '--output-last-message', str(schema_path.with_name('final.response.json')), '-c', 'approval_policy="never"',
        '-c', 'web_search="disabled"']
    cmd += ['--skip-git-repo-check']
    if compare:
        cmd += ['-c', 'features.shell_tool=false']
    if agent.get('model'):
        cmd += ['--model', agent['model']]
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
