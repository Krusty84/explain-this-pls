# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Explicit source decoding for evidence; never guess or replace bad bytes."""
import codecs
import re

ENCODINGS = {'utf-8', 'utf-16', 'utf-16-le', 'utf-16-be', 'utf-32',
             'utf-32-le', 'utf-32-be', 'cp1251', 'cp1252', 'cp866', 'iso8859-1'}
BOMS = ((codecs.BOM_UTF32_LE, 'utf-32-le'), (codecs.BOM_UTF32_BE, 'utf-32-be'),
        (codecs.BOM_UTF8, 'utf-8'), (codecs.BOM_UTF16_LE, 'utf-16-le'),
        (codecs.BOM_UTF16_BE, 'utf-16-be'))


class SourceDecodeError(ValueError):
    def __init__(self, status, encoding=None):
        super().__init__(status)
        self.encoding = encoding


def normalize_source_decoding(value=None):
    """Return stable rules or reject configuration before any model invocation."""
    if value is None:
        return {'rules': []}
    if type(value) is not dict or set(value) - {'rules'} or type(value.get('rules', [])) is not list:
        raise ValueError('source_decoding must contain only a rules array.')
    rules, paths = [], set()
    for rule in value.get('rules', []):
        if type(rule) is not dict or set(rule) != {'path', 'encoding'}:
            raise ValueError('Each source_decoding rule requires path and encoding only.')
        path = rule['path']
        if (type(path) is not str or not path.strip() or len(path) > 4096 or
                re.search(r'[\x00-\x1f\x7f\\:]', path) or
                (path != '.' and any(part in ('', '.', '..') for part in path.split('/')))):
            raise ValueError('source_decoding rule paths must be safe root-relative paths or ".".')
        if path in paths:
            raise ValueError('Duplicate source_decoding rule path.')
        encoding = rule['encoding']
        try:
            encoding = codecs.lookup(encoding).name if type(encoding) is str else None
        except LookupError:
            encoding = None
        if encoding not in ENCODINGS:
            raise ValueError('Unsupported source_decoding encoding.')
        rules.append({'path': path, 'encoding': 'latin-1' if encoding == 'iso8859-1' else encoding})
        paths.add(path)
    return {'rules': sorted(rules, key=lambda rule: rule['path'])}


def decode_source(blob, relative, settings):
    """Return text and actual codec; rules use paths from the common source root."""
    matching = [rule for rule in settings['rules'] if rule['path'] == '.' or
                relative == rule['path'] or relative.startswith(rule['path'] + '/')]
    configured = max(matching, key=lambda rule: 0 if rule['path'] == '.' else len(rule['path']))['encoding'] if matching else None
    bom, detected = next(((bom, encoding) for bom, encoding in BOMS if blob.startswith(bom)), (b'', None))
    compatible = (configured is None or configured == detected or
                  configured in ('utf-16', 'utf-32') and detected is not None and
                  detected.startswith(configured + '-'))
    if detected and not compatible:
        raise SourceDecodeError('ENCODING_MISMATCH')
    encoding = detected or configured or 'utf-8'
    if encoding in ('utf-16', 'utf-32'):
        raise SourceDecodeError('DECODE_ERROR', encoding)
    try:
        return blob[len(bom):].decode(encoding, errors='strict'), encoding
    except UnicodeError:
        raise SourceDecodeError('DECODE_ERROR', encoding) from None
