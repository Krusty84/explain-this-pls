# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""The installation's config-folder preference, separate from audit configs."""
import json
from pathlib import Path

from explain import atomic


def config_directory(value):
    if not str(value).strip():
        raise ValueError('Enter a config folder path.')
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError('Choose an existing directory.')
    # Check readability now, including when a saved directory loses permissions.
    list(path.iterdir())
    return path


def read_settings(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('config_dir'), str) or not value['config_dir'].strip():
        raise ValueError('explain.config must contain a config_dir path.')
    if not Path(value['config_dir']).is_absolute():
        raise ValueError('The saved config_dir must be an absolute path.')
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
