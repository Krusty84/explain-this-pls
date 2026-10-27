# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Adapter capability checks and commands for configured coding agents."""
from __future__ import annotations
import tempfile
from pathlib import Path
from src.messages import message as ui_message
from src.runtime.execution import Budget
from src.backends import codex
from src.backends.codex_catalog import load_catalog
from src.backends.codex_capabilities import MCP_NO_SOURCE_TOOLS, probe_command
from src.runtime.errors import AuditError
from src.runtime.artifacts import neutral_temporary_base
from src.runtime.process import cli_env
from src.runtime import process as process_runtime
from src.runtime import artifacts

CLI_ADAPTERS = {'codex': codex}

def check_cli(session) -> dict:
    result = {}
    mcp_mode = session.cfg.get('source_access', {}).get('mode') == 'mcp'
    if mcp_mode:
        session.mcp_capabilities = {}
    catalogs = {}
    writers = {agent['backend'] + ':' + agent['executable']
               for stage, agent in session.cfg['_agents'].items() if stage == 'study'}
    for agent in session.cfg['_agents'].values():
        key = agent['backend'] + ':' + agent['executable']
        if key in result:
            if agent.get('expected_version') and agent['expected_version'] != result[key]['version']:
                raise AuditError(ui_message('diagnostic.preflight.conflicting_expected_version_for_the_same_executable'))
            if not mcp_mode or profile_key(agent) in session.mcp_capabilities:
                continue
        backend = agent['backend']
        session.reporter.emit('preflight_started', check='cli', backend=backend)
        with tempfile.TemporaryDirectory(prefix='archaudit-cli-check-', dir=neutral_temporary_base(session.source_path)) as raw:
            state = Path(raw).resolve()
            env = cli_env(state)
            adapter = CLI_ADAPTERS[backend]
            cmds = [[agent['executable'], '--version'],
                    adapter.help_command(agent['executable'], **({'mcp_mode': True} if mcp_mode else {}))]
            texts = []
            for cmd in cmds:
                r = process_runtime.process(cmd, state, env, reporter=session.reporter,
                            context={'stage': 'preflight', 'backend': backend}, budget=Budget(30))
                if r['returncode']:
                    raise AuditError(ui_message(
                        'diagnostic.preflight.failed_its_cli_check_exit_code',
                        backend=backend,
                        returncode=r["returncode"],
                    ),
                                     code='CLI_CHECK_FAILED',
                                     hint=ui_message('diagnostic.preflight.check_the_configured_executable_and_its_version_help'))
                # Startup warnings are not part of the version identity.
                output = r['stdout']
                if cmd[-1] == '--help':
                    output += r['stderr']
                elif not output.strip():
                    output = r['stderr']
                texts.append(output.decode(errors='replace'))
            required = adapter.required_flags(session.mode, writer_only=key in writers, mcp_mode=mcp_mode)
            if any(flag not in texts[1] for flag in required):
                raise AuditError(ui_message('diagnostic.preflight.lacks_required_cli_options', backend=backend, required=required),
                                 code='MCP_COMPATIBILITY' if mcp_mode else 'BACKEND_INCOMPATIBLE',
                                 failure_kind='MCP_COMPATIBILITY' if mcp_mode else 'BACKEND_INCOMPATIBLE',
                                 failure_layer='compatibility')
            version = texts[0].strip()
            if agent.get('expected_version') and agent['expected_version'] != version:
                raise AuditError(ui_message(
                    'diagnostic.preflight.version_differs_from_expected_version',
                    backend=backend,
                ), code='CLI_VERSION_MISMATCH')
            if mcp_mode:
                r = process_runtime.process([agent['executable'], 'features', 'list'], state, env,
                            reporter=session.reporter, context={'stage': 'preflight', 'backend': backend},
                            budget=Budget(30))
                if r['returncode']:
                    raise AuditError(ui_message('diagnostic.preflight.codex_cannot_report_independent_mcp_and_native_tool'),
                                     code='MCP_COMPATIBILITY', failure_kind='MCP_COMPATIBILITY',
                                     failure_layer='compatibility')
                adapter.check_mcp_compatibility(version, r['stdout'].decode(errors='replace'))
                if key not in catalogs:
                    catalogs[key] = load_catalog(agent, state, process_runtime.process, env)
                catalog = catalogs[key]
                catalog_path = state / 'model-catalog.json'
                schema_path = state / 'output.schema.json'
                artifacts.save_json(catalog_path, catalog)
                artifacts.save_json(schema_path, {'type': 'object', 'properties': {
                    'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False})
                cmd = adapter.build_command(agent, 'compare', session.mode, {}, schema_path,
                                            mcp_mode=True, mcp_catalog=catalog_path)
                proof = probe_command(cmd, state, env, expected_tools=MCP_NO_SOURCE_TOOLS,
                                      process_fn=process_runtime.process, reporter=session.reporter,
                                      budget=Budget(60))
                if proof['model'] not in {model['slug'] for model in catalog['models']}:
                    raise AuditError(ui_message('diagnostic.preflight.codex_selected_a_model_outside_its_pinned_catalog'),
                                     code='MCP_COMPATIBILITY', failure_layer='compatibility')
                session.mcp_capabilities[profile_key(agent)] = {'catalog': catalog, 'proof': proof}
            result[key] = {'version': version, 'required_flags': required}
            session.reporter.emit('preflight_completed', check='cli', backend=backend, required_flags=required)
    session.versions = {k: v['version'] for k, v in result.items()}
    return result


def profile_key(agent):
    return (agent['backend'], agent['executable'], agent.get('model'), agent.get('reasoning_effort'))

def command(session, stage: str, state: Path, agent: dict, schema_path: Path, env: dict, *, reports_only=False, writer_only=False, schema=None,
            mcp_manifest: Path | None = None, mcp_manifest_sha256: str | None = None,
            mcp_catalog: Path | None = None) -> list[str]:
    adapter = CLI_ADAPTERS.get(agent['backend'])
    if adapter:
        access_stage = 'compare' if reports_only and not writer_only else stage
        return adapter.build_command(agent, access_stage, session.mode, schema or session.schemas[stage], schema_path,
                                     writer_only=writer_only,
                                     mcp_mode=getattr(session, 'cfg', {}).get('source_access', {}).get('mode') == 'mcp',
                                     mcp_manifest=mcp_manifest, mcp_manifest_sha256=mcp_manifest_sha256,
                                     mcp_catalog=mcp_catalog)
    raise AuditError(ui_message('diagnostic.preflight.no_cli_adapter_for_this_backend'),
                     code='BACKEND_INCOMPATIBLE')
