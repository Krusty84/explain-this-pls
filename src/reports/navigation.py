# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Portable local links and deterministic anchors, outside authored blocks."""
from urllib.parse import quote
from html import unescape


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


def block_marker(block, document):
    return '<a id="' + anchor('block', block['id'], document) + '"></a>\n\n'
