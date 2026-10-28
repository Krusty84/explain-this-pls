# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Portable local links and deterministic anchors, outside authored blocks."""
from urllib.parse import quote
from html import unescape
from pathlib import Path

from src.analysis.evidence import valid_path


def prefix(document=None):
    result = 'report-'
    authored = unescape((document or {}).get('report_markdown', ''))
    while result in authored:
        result += 'x-'
    return result


def anchor(kind, identifier, document=None):
    return prefix(document) + kind + '-' + identifier.lower().replace(':', '-')


def link(title, path='', fragment=''):
    target = quote(str(path), safe='/.-') + ('#' + quote(fragment, safe='-') if fragment else '')
    return f'[{title}]({target})'


def editor_link(title, path, start_line, end_line, status, *, source_root=None, editor='vscode'):
    """Open a checked locator in the current source tree without reading it."""
    if (source_root is None or status not in ('RESOLVED', 'QUOTE_MISMATCH') or not valid_path(path)
            or type(start_line) is not int or type(end_line) is not int or not 1 <= start_line <= end_line):
        return title
    root = Path(source_root)
    if not root.is_absolute():
        return title
    target = editor + '://file' + quote((root / path).as_posix(), safe='/.-') + f':{start_line}:1'
    return f'[{title}]({target})'


def block_marker(block, document):
    return '<a id="' + anchor('block', block['id'], document) + '"></a>\n\n'
