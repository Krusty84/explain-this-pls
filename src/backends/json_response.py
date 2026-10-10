# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Strict object parsing with optional, unambiguous response wrappers."""
import re

from src.contracts.contracts import ContractError, response_error, strict_json


def json_object_response(text, *, allow_intro=False):
    metadata = {}
    try:
        try:
            data = strict_json(text)
        except ContractError:
            match = re.fullmatch(
                (r'(?P<intro>.*?)' if allow_intro else r'[ \t\r\n]*')
                + r'(?<!`)```(?i:json)[ \t]*(?:\r\n|\n|\r)'
                r'(?P<payload>.*?)(?:\r\n|\n|\r)[ \t]*```[ \t\r\n]*', text, re.DOTALL)
            if match is not None:
                prefix = match.group('intro') if allow_intro else ''
                start, end = match.start('payload'), match.end('payload')
                kind = 'markdown_json_fence'
            elif allow_intro and text.find('{') > 0:
                start, end = text.find('{'), len(text.rstrip())
                prefix = text[:start]
                kind = 'leading_prose_json_object'
            else:
                raise
            # Never skip an earlier possible JSON container or code block.
            # A malformed/ambiguous answer must not become valid by selecting its tail.
            if any(marker in prefix for marker in ('{', '[', '```')):
                raise
            # Character offsets refer to the unmodified, joined final response.
            metadata['response_normalization'] = {
                'kind': kind, 'payload_start': start, 'payload_end': end}
            data = strict_json(text[start:end])
        if type(data) is not dict:
            raise response_error('INVALID_JSON', 'result', 'Final answer must be a JSON object.')
    except ContractError as exc:
        exc.details.update(metadata)
        raise
    return data, metadata
