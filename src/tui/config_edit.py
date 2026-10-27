# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Edit a model value without serializing the rest of a JSON/JSONC config."""
from src.messages import message as ui_message
from src.runtime.config import STAGES
from src.runtime.config_edit import replace_value, save_text


def replace_model(text, field, model):
    if field not in ('agent.model', *('stage_agents.' + stage + '.model' for stage in STAGES)):
        raise ValueError(ui_message('tui.choose_an_agent_model_field'))
    return replace_value(text, field, model)


def save_model(path, field, model, original):
    updated = replace_model(original.decode('utf-8'), field, model)
    save_text(path, updated, original)
