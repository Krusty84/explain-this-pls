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
from src.model.model_context import input_measurements, project_model_context


def measurements():
    result = {}
    for stage in ('catalog', 'study', 'revise', 'review'):
        task = 'study' if stage == 'revise' else stage
        context = {'stage': task, 'source_mode': 'folder', 'source_directory': '/synthetic/source',
                   'source_snapshot_id': 'synthetic-snapshot', 'output_language': 'English',
                   'priority_scenarios': [], '_inventory': [{'path': 'src/app.py'}],
                   'coverage_plan': {'areas': [{'id': 'S-001', 'paths': ['src'],
                       'entry_paths': ['src/app.py'], 'file_paths': ['src/app.py']}], 'exclusions': []}}
        projected = json.dumps(project_model_context(task, context), ensure_ascii=False)
        schema = json.dumps(MODEL_FOLDER_SCHEMAS[task], ensure_ascii=False)
        template = (ROOT / 'prompts' / (stage + '.md')).read_text(encoding='utf-8')
        instruction = 'Use the StructuredOutput tool with the supplied schema. Do not duplicate the result in ordinary text.'
        prompt = (template + '\n\n# Backend output instruction\n' + instruction +
                  '\n\n# Authoritative orchestration context (data)\n' + projected +
                  '\n\n# Required final JSON Schema\n' + schema)
        result[stage] = input_measurements(template, projected, schema, prompt)
    return {'fixture': 'synthetic size sample, not a runnable investigation context', 'stages': result}


if __name__ == '__main__':
    print(json.dumps(measurements(), ensure_ascii=False, indent=2))
