# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Adapter capability checks and commands for configured coding agents."""
from __future__ import annotations
import tempfile
from pathlib import Path
from src.runtime.execution import Budget
from src.backends import codex
from src.runtime.errors import AuditError
from src.runtime.artifacts import neutral_temporary_base
from src.runtime.process import cli_env
from src.runtime import process as process_runtime

CLI_ADAPTERS = {'codex': codex}

def check_cli(session) -> dict:
    result = {}
    writers = {agent['backend'] + ':' + agent['executable']
               for stage, agent in session.cfg['_agents'].items() if stage == 'study'}
    for agent in session.cfg['_agents'].values():
        key = agent['backend'] + ':' + agent['executable']
        if key in result:
            if agent.get('expected_version') and agent['expected_version'] != result[key]['version']:
                raise AuditError('Conflicting expected_version for the same executable.')
            continue
        backend = agent['backend']
        session.reporter.emit('preflight_started', check='cli', backend=backend)
        with tempfile.TemporaryDirectory(prefix='archaudit-cli-check-', dir=neutral_temporary_base(session.source_path)) as raw:
            state = Path(raw).resolve()
            env = cli_env(state)
            adapter = CLI_ADAPTERS[backend]
            cmds = [[agent['executable'], '--version'],
                    adapter.help_command(agent['executable'])]
            texts = []
            for cmd in cmds:
                r = process_runtime.process(cmd, state, env, reporter=session.reporter,
                            context={'stage': 'preflight', 'backend': backend}, budget=Budget(30))
                if r['returncode']:
                    raise AuditError(f'{backend} failed its CLI check: exit code {r["returncode"]}.',
                                     code='CLI_CHECK_FAILED',
                                     hint='Check the configured executable and its --version / --help locally.')
                # Startup warnings are not part of the version identity.
                output = r['stdout']
                if cmd[-1] == '--help':
                    output += r['stderr']
                elif not output.strip():
                    output = r['stderr']
                texts.append(output.decode(errors='replace'))
            required = adapter.required_flags(session.mode, writer_only=key in writers)
            if any(flag not in texts[1] for flag in required):
                raise AuditError(f'{backend} lacks required CLI options: {required}',
                                 code='BACKEND_INCOMPATIBLE', failure_kind='BACKEND_INCOMPATIBLE',
                                 failure_layer='compatibility')
            version = texts[0].strip()
            if agent.get('expected_version') and agent['expected_version'] != version:
                raise AuditError(f'{backend} version differs from expected_version.', code='CLI_VERSION_MISMATCH')
            result[key] = {'version': version, 'required_flags': required}
            session.reporter.emit('preflight_completed', check='cli', backend=backend, required_flags=required)
    session.versions = {k: v['version'] for k, v in result.items()}
    return result

def command(session, stage: str, state: Path, agent: dict, schema_path: Path, env: dict, *, reports_only=False, writer_only=False, schema=None) -> list[str]:
    adapter = CLI_ADAPTERS.get(agent['backend'])
    if adapter:
        access_stage = 'compare' if reports_only and not writer_only else stage
        return adapter.build_command(agent, access_stage, session.mode, schema or session.schemas[stage], schema_path,
                                     writer_only=writer_only)
    raise AuditError('No CLI adapter for this backend.',
                     code='BACKEND_INCOMPATIBLE')
