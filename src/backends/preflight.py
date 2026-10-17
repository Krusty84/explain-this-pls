# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Adapter capability checks and commands for configured coding agents."""
from __future__ import annotations
import tempfile
from pathlib import Path
from src.runtime.execution import Budget
from src.backends import codex, opencode_cli, xxx
from src.contracts.contracts import response_error
from src.runtime.errors import AuditError
from src.runtime.artifacts import neutral_temporary_base
from src.runtime.process import cli_env
from src.runtime import process as process_runtime

CLI_ADAPTERS = {'codex': codex, 'opencode': opencode_cli, 'xxx': xxx}

def check_cli(session) -> dict:
    result = {}
    variant_executables = {agent['executable'] for agent in session.cfg['_agents'].values()
        if agent['backend'] == 'xxx' and agent.get('reasoning_effort') is not None}
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
            required = adapter.required_flags(session.mode)
            if backend == 'xxx' and agent['executable'] in variant_executables:
                required.append('--variant')
            if any(flag not in texts[1] for flag in required):
                raise AuditError(f'{backend} lacks required CLI options: {required}',
                                 code='BACKEND_INCOMPATIBLE', failure_kind='BACKEND_INCOMPATIBLE',
                                 failure_layer='compatibility')
            version = texts[0].strip()
            if agent.get('expected_version') and agent['expected_version'] != version:
                raise AuditError(f'{backend} version differs from expected_version.', code='CLI_VERSION_MISMATCH')
            if backend == 'opencode':
                opencode_cli.verify_version(version)
            result[key] = {'version': version, 'required_flags': required}
            if backend == 'xxx':
                for arguments, marker in ((['export', '--help'], 'sessionID'),
                                          (['session', 'delete', '--help'], 'sessionID')):
                    checked = process_runtime.process([agent['executable'], *arguments], state, env,
                                      reporter=session.reporter, budget=Budget(30))
                    if checked['returncode'] or marker not in (checked['stdout'] + checked['stderr']).decode(errors='replace'):
                        raise response_error('BACKEND_INCOMPATIBLE', 'compatibility',
                                             'XXX requires export and session delete commands.')
                result[key]['compatibility_profile'] = xxx.PROFILE
            session.reporter.emit('preflight_completed', check='cli', backend=backend, required_flags=required)
    session.versions = {k: v['version'] for k, v in result.items()}
    return result

def command(session, stage: str, state: Path, agent: dict, schema_path: Path, env: dict, *, reports_only=False, schema=None,
            source_maps_directory: Path | None = None) -> list[str]:
    adapter = CLI_ADAPTERS.get(agent['backend'])
    if adapter:
        access_stage = 'compare' if reports_only else stage
        kwargs = {}
        if adapter is opencode_cli:
            kwargs['agent_name'] = opencode_cli.prepare_environment(env, access_stage,
                source_maps_directory=source_maps_directory)
        elif adapter is xxx:
            kwargs['agent_name'] = xxx.prepare_environment(env, access_stage,
                source_maps_directory=source_maps_directory)
        return adapter.build_command(agent, access_stage, session.mode, schema or session.schemas[stage], schema_path, **kwargs)
    raise AuditError('No CLI adapter for this backend.',
                     code='BACKEND_INCOMPATIBLE')
