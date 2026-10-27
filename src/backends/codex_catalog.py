# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Pin bundled model metadata without model-selected source tools."""
from copy import deepcopy
from pathlib import Path

from src.messages import message as ui_message
from src.contracts.contracts import ContractError, strict_json
from src.runtime.errors import AuditError
from src.runtime.execution import Budget


def _compatibility(message):
    return AuditError(message, code='MCP_COMPATIBILITY', failure_kind='MCP_COMPATIBILITY',
                      failure_layer='compatibility', details={'native_fallback': False})


def sanitize_catalog(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get('models'), list) or not value['models']:
        raise _compatibility(ui_message('diagnostic.codex_catalog.codex_must_provide_a_nonempty_bundled_model_catalog'))
    names = set()
    for model in value['models']:
        name = model.get('slug') if isinstance(model, dict) else None
        if not isinstance(name, str) or not name.strip() or name in names:
            raise _compatibility(ui_message(
                'diagnostic.codex_catalog.codex_returned_invalid_or_duplicate_bundled_model_identities',
            ))
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
        raise _compatibility(ui_message('diagnostic.codex_catalog.codex_cannot_load_its_bundled_model_catalog_for')) from error
    if result['returncode']:
        raise _compatibility(ui_message('diagnostic.codex_catalog.codex_failed_to_report_its_bundled_model_catalog'))
    try:
        value = strict_json(result['stdout'].decode('utf-8'))
    except (ContractError, UnicodeDecodeError) as error:
        raise _compatibility(ui_message('diagnostic.codex_catalog.codex_returned_invalid_bundled_model_catalog_json')) from error
    return sanitize_catalog(value)
