# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Git path selection and deltas over saved, in-place source inventories."""
import os
from pathlib import Path
import subprocess

from src.analysis.evidence import canonical, sha
from src.analysis.source_filter import excluded_root


def ignore_path(node):
    """Import only core.excludesFile, never the user's execution configuration.

    `config` is a read-only parser (including conditional includes). Other Git
    commands keep the private runtime configuration and disabled hooks/filters.
    """
    env = node.env.copy()
    for key in ('HOME', 'XDG_CONFIG_HOME'):
        if key in os.environ:
            env[key] = os.environ[key]
    env['GIT_CONFIG_GLOBAL'] = os.environ.get('GIT_CONFIG_GLOBAL', str(Path.home() / '.gitconfig'))
    # Without an explicit override, Git reads both ~/.gitconfig and XDG config.
    if 'GIT_CONFIG_GLOBAL' not in os.environ:
        env.pop('GIT_CONFIG_GLOBAL', None)
    command = node.prefix + ['config', '--global', '--includes', '--path', '-z', '--get', 'core.excludesFile']
    result = subprocess.run(command, env=env, capture_output=True, timeout=30)
    if result.returncode not in (0, 1):
        raise ValueError('Cannot read user Git ignore configuration.')
    # Local configuration has precedence over global configuration.
    local_env = node.env | {k: env[k] for k in ('HOME', 'XDG_CONFIG_HOME') if k in env}
    local = subprocess.run(node.prefix + ['config', '--path', '-z', '--get', 'core.excludesFile'],
                           env=local_env, capture_output=True, timeout=30)
    if local.returncode not in (0, 1):
        raise ValueError('Cannot read repository ignore configuration.')
    value = (local.stdout if local.returncode == 0 else result.stdout).removesuffix(b'\0')
    if value:
        path = Path(os.fsdecode(value)).expanduser()
        return path if path.is_absolute() else node.path / path
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'git/ignore'


def tracked_paths(repo):
    paths = set()
    for prefix, node in repo.nodes.items():
        for raw in node.git('ls-files', '--stage', '-z').split(b'\0'):
            if not raw:
                continue
            header, name = raw.split(b'\t', 1)
            if header.split()[0] == b'160000':
                continue
            relative = node.safe_relative(os.fsdecode(name))
            paths.add(relative if prefix == '.' else prefix + '/' + relative)
    return paths


def ignored_paths(repo, settings):
    exclusions = set()
    for prefix, node in repo.nodes.items():
        for raw in node.git('-c', 'core.excludesFile=' + str(node.excludes_file), 'ls-files',
                            '--others', '--ignored', '--exclude-standard', '--directory', '-z').split(b'\0'):
            if raw:
                relative = os.fsdecode(raw).rstrip('/')
                full = relative if prefix == '.' else prefix + '/' + relative
                root = excluded_root(full, settings)
                exclusions.add((root or full, 'CONFIG' if root else 'GITIGNORE'))
    return [{'path': path, 'origin': origin} for path, origin in sorted(exclusions)]


def inventory_delta(baseline, other, inventories, pins, plans):
    def entries(label):
        result = {}
        for entry in inventories[label]['entries']:
            if entry['type'] == 'directory':
                continue
            mode = ('120000' if entry['type'] == 'symlink' else
                    '100755' if int(entry['mode'], 8) & 0o111 else '100644')
            result[entry['path']] = {k: v for k, v in entry.items() if k != 'path'} | {'mode': mode}
        return result
    a, b = entries(baseline), entries(other)
    changes = []
    for path in sorted(a.keys() | b.keys()):
        old, new = a.get(path), b.get(path)
        if old != new:
            changes.append({'path': path, 'status': 'A' if old is None else 'D' if new is None else 'M',
                'old_mode': old['mode'] if old else '000000', 'new_mode': new['mode'] if new else '000000',
                'old_sha256': sha(canonical(old)) if old else None,
                'new_sha256': sha(canonical(new)) if new else None})
    subs = [dict(path=path, baseline_commit=commit, branch_commit=plans[pins[other]][path])
            for path, commit in plans[pins[baseline]].items()
            if path != '.' and commit != plans[pins[other]][path]]
    return {'orientation': 'baseline_tree_to_branch_tree', 'baseline_commit': pins[baseline],
            'branch_commit': pins[other], 'changes': changes, 'submodule_changes': subs,
            'identical_trees': not changes and not subs,
            'limitations': ['Filtered path/content/mode changes only; renames appear as deletion plus addition.']}
