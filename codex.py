# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Codex CLI commands and structured-output parsing (stdlib only)."""
from pathlib import Path

from contracts import strict_json


def help_command(executable: str) -> list[str]:
    return [executable, 'exec', '--help']


def required_flags(mode: str) -> list[str]:
    flags = ['--ephemeral', '--output-schema', '--sandbox']
    if mode == 'folder':
        flags.append('--skip-git-repo-check')
    return flags


def build_command(agent: dict, stage: str, mode: str, schema: dict,
                  schema_path: Path) -> list[str]:
    compare = stage == 'compare'
    cmd = [agent['executable'], 'exec', '--ephemeral', '--color', 'never', '--sandbox', 'read-only',
        '--output-schema', str(schema_path), '-c', 'approval_policy="never"',
        '-c', 'web_search="disabled"']
    if compare or mode == 'folder':
        cmd += ['--skip-git-repo-check']
    if compare:
        cmd += ['-c', 'features.shell_tool=false']
    if agent.get('model'):
        cmd += ['--model', agent['model']]
    return cmd + ['-']


def parse_output(output: str) -> tuple[dict, dict]:
    return strict_json(output.strip()), {}
