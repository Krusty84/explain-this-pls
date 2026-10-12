# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Edit a model value without serializing the rest of a JSON/JSONC config."""
import json
import re

from explain import STAGES, atomic
from src.contracts.contracts import jsonc, jsonc_text, strict_json


def replace_model(text, field, model):
    if field not in ('agent.model', *('stage_agents.' + stage + '.model' for stage in STAGES)):
        raise ValueError('Choose an agent model field.')
    clean = jsonc_text(text)
    jsonc(text)
    decoder = json.JSONDecoder()

    def skip(index):
        while clean[index].isspace():
            index += 1
        return index

    def edit(start, parts):
        index, members = skip(start + 1), []
        while clean[index] != '}':
            key_start = index
            key, end = decoder.raw_decode(clean, index)
            value_start = skip(skip(end) + 1)
            value, value_end = decoder.raw_decode(clean, value_start)
            members.append((key, key_start, value_start, value_end))
            index = skip(value_end)
            if clean[index] == ',':
                index = skip(index + 1)
        for key, _, value_start, value_end in members:
            if key == parts[0]:
                if len(parts) == 1:
                    return text[:value_start] + json.dumps(model, ensure_ascii=False) + text[value_end:]
                if clean[value_start] != '{':
                    raise ValueError('The selected model section must be an object.')
                return edit(value_start, parts[1:])
        value = model
        for key in reversed(parts[1:]):
            value = {key: value}
        line_start = text.rfind('\n', 0, index) + 1
        closing_indent = text[line_start:index]
        newline = '\r\n' if '\r\n' in text else '\n'
        multiline = '\n' in text[start:index] and not closing_indent.strip()
        if multiline:
            indent = closing_indent + '  '
            if members:
                first = members[0][1]
                prefix = text[text.rfind('\n', 0, first) + 1:first]
                if not prefix.strip():
                    indent = prefix
            unit = indent[len(closing_indent):] if indent.startswith(closing_indent) else '  '
            rendered = json.dumps(value, ensure_ascii=False, indent=unit or '  ')
            member = indent + json.dumps(parts[0]) + ': ' + rendered.replace('\n', newline + indent) + newline
            insertion = line_start
        else:
            member = ('' if not members or text[index - 1].isspace() else ' ')
            member += json.dumps(parts[0]) + ': ' + json.dumps(value, ensure_ascii=False)
            member += ' ' if text[index - 1].isspace() else ''
            insertion = index
        updated = text[:insertion] + member + text[insertion:]
        if members:
            last_end = members[-1][3]
            tail = re.sub(r'//[^\r\n]*|/\*.*?\*/', '', text[last_end:index], flags=re.S)
            if ',' not in tail:
                updated = updated[:last_end] + ',' + updated[last_end:]
        return updated

    updated = edit(skip(0), field.split('.'))
    jsonc(updated)
    return updated


def save_model(path, field, model, original):
    path = path.resolve()
    if path.read_bytes() != original:
        raise ValueError('The configuration changed. Use Refresh before applying a model.')
    updated = replace_model(original.decode('utf-8'), field, model)
    (jsonc if path.suffix.lower() == '.jsonc' else strict_json)(updated)
    if path.read_bytes() != original:
        raise ValueError('The configuration changed. Use Refresh before applying a model.')
    atomic(path, updated)
