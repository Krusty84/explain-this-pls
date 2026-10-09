# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Explicit source-root exclusions; pattern rules belong in .gitignore."""
import re
from pathspec import GitIgnoreSpec


def normalize_source_filter(value):
    if type(value) is not dict or set(value) - {'follow_gitignore', 'exclude_paths'}:
        raise ValueError('source_filter must contain only follow_gitignore and exclude_paths.')
    follow = value.get('follow_gitignore', False)
    paths = value.get('exclude_paths', [])
    if type(follow) is not bool:
        raise ValueError('source_filter.follow_gitignore must be a JSON boolean.')
    if type(paths) is not list:
        raise ValueError('source_filter.exclude_paths must be an array of relative paths.')
    seen = set()
    for path in paths:
        if (type(path) is not str or not path.strip() or len(path) > 4096 or
                re.search(r'[\x00-\x1f\x7f\\:*?\[\]]', path) or
                any(part in ('', '.', '..') for part in path.split('/'))):
            raise ValueError('source_filter.exclude_paths requires safe root-relative paths without glob patterns.')
        if path in seen:
            raise ValueError('Duplicate source_filter.exclude_paths entry.')
        seen.add(path)
    return {'follow_gitignore': follow, 'exclude_paths': sorted(paths)}


def excluded_root(path, settings):
    return next((root for root in settings['exclude_paths']
                 if path == root or path.startswith(root + '/')), None)


def gitignore_spec(data):
    spec = GitIgnoreSpec.from_lines(data.decode('utf-8-sig', errors='surrogateescape').split('\n'))
    # pathspec 0.12.1 lets a trailing /**/ match its own parent. Require
    # one child directory, using the library to parse the equivalent pattern.
    patterns = [type(p)(p.pattern.rstrip() + '*/')
                if p.include is not None and p.pattern.rstrip().endswith('/**/') else p
                for p in spec.patterns]
    return GitIgnoreSpec(patterns)


def gitignore_match(spec, path, directory):
    """Match this entry with pathspec; traversal has already checked its parents.

    The pinned parser marks ancestor matches with the ps_d capture group.
    Ignore those here: a parent was either pruned or explicitly re-included.
    A trailing /** matches directory contents, not the directory itself.
    """
    candidate = path + '/' if directory else path
    for index in range(len(spec.patterns) - 1, -1, -1):
        pattern = spec.patterns[index]
        if pattern.include is None:
            continue
        result = pattern.match_file(candidate)
        if result is None:
            continue
        marker = result.match.groupdict().get('ps_d')
        if marker is not None:
            if result.match.start('ps_d') != len(path):
                continue
        elif directory and pattern.match_file(path) is None:
            continue
        return pattern.include, index + 1
    return None, None
