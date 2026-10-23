# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Pin bundled model metadata without model-selected source tools."""
from copy import deepcopy
from pathlib import Path

from src.contracts.contracts import ContractError, strict_json
from src.runtime.errors import AuditError
from src.runtime.execution import Budget


def _compatibility(message):
    return AuditError(message, code='MCP_COMPATIBILITY', failure_kind='MCP_COMPATIBILITY',
                      failure_layer='compatibility', details={'native_fallback': False})


def sanitize_catalog(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get('models'), list) or not value['models']:
        raise _compatibility('Codex must provide a nonempty bundled model catalog for MCP.')
    names = set()
    for model in value['models']:
        name = model.get('slug') if isinstance(model, dict) else None
        if not isinstance(name, str) or not name.strip() or name in names:
            raise _compatibility('Codex returned invalid or duplicate bundled model identities.')
        names.add(name)
    catalog = deepcopy(value)
    for model in catalog['models']:
        model.update(tool_mode='direct', node_repl_disabled=True, experimental_supported_tools=[],
                     apply_patch_tool_type=None, supports_search_tool=False)
    return catalog


def load_catalog(agent: dict, state: Path, process_fn, env: dict) -> dict:
    home = state / 'catalog-home'
    home.mkdir(mode=0o700)
    isolated_env = dict(env, CODEX_HOME=str(home), PWD=str(home))
    try:
        result = process_fn([agent['executable'], 'debug', 'models', '--bundled'], home,
                            isolated_env, budget=Budget(30))
    except OSError as error:
        raise _compatibility('Codex cannot load its bundled model catalog for MCP.') from error
    if result['returncode']:
        raise _compatibility('Codex failed to report its bundled model catalog for MCP.')
    try:
        value = strict_json(result['stdout'].decode('utf-8'))
    except (ContractError, UnicodeDecodeError) as error:
        raise _compatibility('Codex returned invalid bundled model catalog JSON.') from error
    return sanitize_catalog(value)
