# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Strict object parsing with one optional Markdown JSON fence."""
import re

from src.contracts.contracts import ContractError, response_error, strict_json


def json_object_response(text):
    metadata = {}
    try:
        try:
            data = strict_json(text)
        except ContractError:
            match = re.fullmatch(
                r'[ \t\r\n]*```(?i:json)[ \t]*(?:\r\n|\n|\r)'
                r'(?P<payload>.*?)(?:\r\n|\n|\r)[ \t]*```[ \t\r\n]*', text, re.DOTALL)
            if match is None:
                raise
            # Character offsets refer to the unmodified, joined final response.
            metadata['response_normalization'] = {
                'kind': 'markdown_json_fence',
                'payload_start': match.start('payload'), 'payload_end': match.end('payload')}
            data = strict_json(match.group('payload'))
        if type(data) is not dict:
            raise response_error('INVALID_JSON', 'result', 'Final answer must be a JSON object.')
    except ContractError as exc:
        exc.details.update(metadata)
        raise
    return data, metadata
