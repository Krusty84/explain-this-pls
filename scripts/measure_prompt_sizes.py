#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline input sizes on explicit synthetic contexts. No token/quality claims."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.contracts.contracts import MODEL_FOLDER_SCHEMAS
from src.analysis.source_context import parse_symbols
from src.model.model_context import (build_model_prompt, input_measurements,
                                     project_model_context, serialize_model_input)


def synthetic_index(count):
    files, source_bytes = [], 0
    for number in range(count):
        path = f'src/file{number:05d}.py'
        source = (f'def entry{number:05d}():\n    return helper{number:05d}()\n\n'
                  f'def helper{number:05d}():\n    return {number}\n')
        source_bytes += len(source.encode('utf-8'))
        files.append(dict(source_id='source-001', path=path,
                          **parse_symbols(path, source), encoding='utf-8',
                          total_lines=source.count('\n')))
    return {'mode': 'index-only', 'files': files, 'fragments': [],
            'omissions': {'maps': 0, 'reasons': {}, 'incomplete': False}}, source_bytes


def measurements():
    samples = []
    for count in (1, 1000, 5000):
        index, source_bytes = synthetic_index(count)
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
            context_json = serialize_model_input(projected)
            schema_json = serialize_model_input(MODEL_FOLDER_SCHEMAS[task])
            template = (ROOT / 'prompts' / (stage + '.md')).read_text(encoding='utf-8')
            prompt = build_model_prompt(template, context_json, schema_json, 'codex', source_index=True)
            stages[stage] = input_measurements(template, context_json, schema_json, prompt,
                source_context=index, transmitted_source_context=projected['source_context'])
            original_prompt = build_model_prompt(template,
                json.dumps(dict(projected, source_context=index), ensure_ascii=False),
                json.dumps(MODEL_FOLDER_SCHEMAS[task], ensure_ascii=False), 'codex')
            original_bytes = len(original_prompt.encode('utf-8'))
            transmitted_bytes = len(prompt.encode('utf-8'))
            saved_bytes = original_bytes - transmitted_bytes
            stages[stage]['prompt_comparison'] = {'original_utf8_bytes': original_bytes,
                'transmitted_utf8_bytes': transmitted_bytes, 'saved_utf8_bytes': saved_bytes,
                'reduction_percent': round(saved_bytes * 100 / original_bytes, 2)}
        samples.append({'files': count, 'symbols': sum(len(file['symbols']) for file in index['files']),
                        'source_utf8_bytes': source_bytes, 'stages': stages})
    return {'fixture': 'synthetic size samples, not runnable investigation contexts or live usage measurements',
            'backend': 'codex', 'samples': samples}


if __name__ == '__main__':
    print(json.dumps(measurements(), ensure_ascii=False, indent=2))
