#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline input sizes on explicit synthetic contexts. No token/quality claims."""
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.contracts.contracts import MODEL_FOLDER_SCHEMAS
from src.analysis.source_context import parse_symbols
from src.model.model_context import (build_model_prompt, input_measurements,
                                     on_demand_source_index, project_model_context, serialize_model_input)


def synthetic_index(count, symbols_per_file=2, name_length=0):
    files, source_bytes = [], 0
    for number in range(count):
        path = f'src/file{number:05d}.py'
        source = (f'def entry{number:05d}():\n    return helper{number:05d}()\n\n'
                  f'def helper{number:05d}():\n    return {number}\n')
        if symbols_per_file != 2 or name_length:
            source = ''.join(f'def symbol{number:05d}_{symbol:05d}' + 'x' * name_length +
                             '():\n    return 1\n' for symbol in range(symbols_per_file))
        source_bytes += len(source.encode('utf-8'))
        files.append(dict(source_id='source-001', path=path,
                          **parse_symbols(path, source), encoding='utf-8',
                          total_lines=source.count('\n')))
    return {'mode': 'index-only', 'files': files, 'fragments': [],
            'omissions': {'maps': 0, 'reasons': {}, 'incomplete': False}}, source_bytes


def legacy_index(index):
    result = copy.deepcopy(index)
    result['file_columns'] = ['source_id', 'path', 'parser', 'language', 'encoding', 'total_lines',
                              'map_status', 'map_reason', 'symbols']
    result['symbol_columns'] = ['name', 'kind', 'parent', 'start_line', 'end_line']
    result['files'] = [[file[column] if column != 'symbols' else
                       [[symbol[key] for key in result['symbol_columns']] for symbol in file['symbols']]
                       for column in result['file_columns']] for file in result['files']]
    return result


def measurements():
    samples = []
    cases = [('many-files', count, 2, 0) for count in (1, 1000, 5000)]
    cases += [('many-symbols', 1, 10000, 0), ('long-symbol-names', 1, 100, 1500)]
    for case, count, symbols_per_file, name_length in cases:
        index, source_bytes = synthetic_index(count, symbols_per_file, name_length)
        paths = [file['path'] for file in index['files']]
        stages = {}
        for stage in ('study', 'revise', 'review'):
            task = 'study' if stage == 'revise' else stage
            context = {'stage': task, 'source_mode': 'folder', 'source_directory': '/synthetic/source',
                       'source_snapshot_id': 'synthetic-snapshot', 'output_language': 'English',
                       'priority_scenarios': [], '_inventory': [{'path': path} for path in paths],
                       'source_context': index,
                       'coverage_plan': {'areas': [{'id': 'S-001', 'paths': ['src'],
                           'entry_paths': paths, 'file_paths': paths}], 'exclusions': []}}
            if stage == 'revise':
                context['prompt_variant'] = 'revise'
            if stage == 'study':
                context['inventory_summary'] = {'files': count, 'symlinks': 0, 'directory_count': 2,
                    'empty_directories': 0, 'directories': [
                        {'path': '.', 'files': 0, 'symlinks': 0, 'directories': 1, 'empty_directories': 0},
                        {'path': 'src', 'files': count, 'symlinks': 0, 'directories': 0, 'empty_directories': 0}]}
            projected = project_model_context(task, context)
            schema_json = serialize_model_input(MODEL_FOLDER_SCHEMAS[task])
            template = (ROOT / 'prompts' / (stage + '.md')).read_text(encoding='utf-8')
            legacy = legacy_index(index)
            baseline_prompt = build_model_prompt(template,
                serialize_model_input(dict(projected, source_context=legacy)), schema_json, 'codex', source_index=True)
            baseline_bytes = len(baseline_prompt.encode('utf-8'))
            stages[stage] = {'legacy_rows_index_utf8_bytes': len(serialize_model_input(legacy).encode('utf-8')),
                             'legacy_rows_prompt_utf8_bytes': baseline_bytes}
            demand, maps = on_demand_source_index(projected['source_context'], (ROOT / 'synthetic-maps').resolve())
            map_bytes = sum(len((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
                            for value in maps.values())
            for delivery, wire, stored in (('full', projected['source_context'], 0), ('on-demand', demand, map_bytes)):
                context_json = serialize_model_input(dict(projected, source_context=wire))
                prompt = build_model_prompt(template, context_json, schema_json, 'codex', source_index=True)
                measured = input_measurements(template, context_json, schema_json, prompt,
                    source_context=index, transmitted_source_context=wire, stored_map_utf8_bytes=stored)
                transmitted_bytes = len(prompt.encode('utf-8'))
                saved_bytes = baseline_bytes - transmitted_bytes
                measured['legacy_rows_comparison'] = {'saved_utf8_bytes': saved_bytes,
                    'reduction_percent': round(saved_bytes * 100 / baseline_bytes, 2)}
                stages[stage][delivery] = measured
        samples.append({'case': case, 'files': count, 'symbols': sum(len(file['symbols']) for file in index['files']),
                        'source_utf8_bytes': source_bytes, 'stages': stages})
    return {'fixture': 'synthetic size samples, not runnable investigation contexts or live usage measurements',
            'backend': 'codex', 'samples': samples}


if __name__ == '__main__':
    print(json.dumps(measurements(), ensure_ascii=False, indent=2))
