# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""The installation's config-folder preference, separate from audit configs."""
import json
from pathlib import Path

from src.messages import message as ui_message
from src.runtime.artifacts import atomic


def config_directory(value):
    if not str(value).strip():
        raise ValueError(ui_message('tui.enter_a_config_folder_path'))
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(ui_message('tui.choose_an_existing_directory'))
    # Check readability now, including when a saved directory loses permissions.
    list(path.iterdir())
    return path


def read_settings(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('config_dir'), str) or not value['config_dir'].strip():
        raise ValueError(ui_message('tui.explain_config_must_contain_a_config_dir_path'))
    if not Path(value['config_dir']).is_absolute():
        raise ValueError(ui_message('tui.the_saved_config_dir_must_be_an_absolute'))
    return config_directory(value['config_dir'])


def save_settings(path, directory):
    directory = config_directory(directory)
    atomic(path, json.dumps({'config_dir': str(directory)}, ensure_ascii=False, indent=2) + '\n')
    return directory


def config_files(directory):
    return sorted((path for path in directory.iterdir()
                   if path.is_file() and path.suffix.lower() in ('.json', '.jsonc')
                   and not path.name.lower().endswith(('.example.json', '.example.jsonc'))),
                  key=lambda path: (path.name.casefold(), path.name))
