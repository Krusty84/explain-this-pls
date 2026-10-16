# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Lossless content projection for model input; never used for validation/hashing."""
import copy
import json

CONTEXT_FORMAT = 'compact-context'

_FILE_COLUMNS = ('source_id', 'path', 'parser', 'language', 'encoding', 'total_lines',
                 'map_status', 'map_reason', 'symbols')
_SYMBOL_COLUMNS = ('name', 'kind', 'parent', 'start_line', 'end_line')
SOURCE_INDEX_INSTRUCTION = (
    'source_context.files contains rows in file_columns order. Each row\'s symbols cell '
    'contains rows in symbol_columns order. Preserve null values and original inclusive '
    'source line coordinates.')


def serialize_model_input(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def build_model_prompt(template, context_json, schema_json, backend, *, source_index=False):
    instruction = ('Return exactly one JSON object matching the supplied schema as your final answer; no fences or surrounding prose.'
        if backend in ('xxx', 'opencode') else
        'Return the supplied schema object through the backend structured-output mechanism; no fences or surrounding prose.')
    index_instruction = ('\n\n# Source index reading instruction\n' + SOURCE_INDEX_INSTRUCTION
                         if source_index else '')
    return (template + '\n\n# Backend output instruction\n' + instruction + index_instruction +
            '\n\n# Authoritative orchestration context (data)\n' + context_json +
            '\n\n# Required final JSON Schema\n' + schema_json)


def input_measurements(template, context_json, schema_json, prompt, *, correction=False,
                       source_context=None, transmitted_source_context=None):
    """Exact wire text sizes, NOT tokenizer estimates or quality measurements."""
    def size(text):
        return {'characters': len(text), 'utf8_bytes': len(text.encode('utf-8'))}
    result = {'template': size('' if correction else template), 'projected_context': size(context_json),
            'schema': size(schema_json), 'prompt': size(prompt),
            'text_schema_copy': True, 'correction_prompt': correction, 'template_applied': not correction,
            'tokens': None, 'token_measurement': 'unavailable'}
    if source_context is not None and transmitted_source_context is not None:
        original = len(json.dumps(source_context, ensure_ascii=False).encode('utf-8'))
        transmitted = len(serialize_model_input(transmitted_source_context).encode('utf-8'))
        result['source_index'] = {'original_utf8_bytes': original, 'transmitted_utf8_bytes': transmitted,
            'saved_utf8_bytes': original - transmitted,
            'reduction_percent': round(100 * (original - transmitted) / original, 2)}
    return result


def _source_index(index):
    if 'file_columns' in index:
        return
    rows = []
    for file in index['files']:
        values = file | {'symbols': [[symbol[key] for key in _SYMBOL_COLUMNS] for symbol in file['symbols']]}
        rows.append([values[column] for column in _FILE_COLUMNS])
    index['file_columns'] = list(_FILE_COLUMNS)
    index['symbol_columns'] = list(_SYMBOL_COLUMNS)
    index['files'] = rows


def _registry(claims):
    for claim in claims:
        if isinstance(claim, dict):
            for locator in claim.get('document_locators', []):
                locator.pop('quote', None)
    return claims


def _document(document, *, registry=True, plan=True):
    if not isinstance(document, dict):
        return document
    for key in ('block_map', 'materialization_provenance', 'normalization_provenance'):
        document.pop(key, None)
    if registry:
        _registry(document.get('claims', []))
    else:
        document.pop('claims', None)
    if not plan:
        document.pop('review_plan', None)
    return document


def _pair(pair):
    if not isinstance(pair, dict):
        return
    _document(pair.get('study'))
    _document(pair.get('study_material'))
    for key in ('review', 'review_material'):
        review = pair.get(key)
        if isinstance(review, dict):
            review.pop('claim_registry', None)
            review.pop('review_plan', None)
            review.pop('normalization_provenance', None)


def _coverage(plan):
    if not isinstance(plan, dict):
        return
    for area in plan.get('areas', []):
        # Selectors define scope; expanded inventory membership is used only by
        # program reconciliation. No source or authored prose is summarized.
        if 'entry_paths' in area:
            area['entry_count'] = len(area.pop('entry_paths'))
        if 'file_paths' in area:
            area['file_count'] = len(area.pop('file_paths'))


def project_model_context(stage, context):
    """Return an independent projection while retaining every substantive value."""
    result = copy.deepcopy(context)
    result.pop('_inventory', None)
    result.pop('_coverage_plan_path', None)
    if stage in ('compare', 'repair') or result.get('prompt_variant') == 'repair':
        result.pop('source_context', None)
    if 'architecture_document' in result:
        _document(result['architecture_document'], registry=False, plan=False)
        _registry(result.get('claim_registry', []))
    for branch in result.get('branches', []):
        _pair(branch)
    _pair(result.get('previous_revision'))
    # Frozen plans retain the full objects locally. Shared prompt data has one
    # authoritative occurrence, without duplicated internal plan data.
    plans = [result.get('review_plan')]
    previous = result.get('previous_revision') or {}
    plans.append((previous.get('study') or {}).get('review_plan'))
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        for key in ('coverage_plan', 'source_decoding', 'sources', 'prior_findings', 'registry_diff'):
            if key in result and plan.get(key) == result[key]:
                plan.pop(key, None)
    for branch in result.get('branches', []):
        plan = (branch.get('study') or {}).get('review_plan')
        if isinstance(plan, dict) and branch.get('coverage_plan') == plan.get('coverage_plan'):
            plan.pop('coverage_plan', None)
    _coverage(result.get('coverage_plan'))
    for plan in plans:
        if isinstance(plan, dict):
            _coverage(plan.get('coverage_plan'))
    for branch in result.get('branches', []):
        _coverage(branch.get('coverage_plan'))
        _coverage(((branch.get('study') or {}).get('review_plan') or {}).get('coverage_plan'))
    result = project_model_response(result)
    if 'source_context' in result:
        _source_index(result['source_context'])
    return result


_SERVICE_KEYS = {'fingerprint', 'source_fingerprint', 'sha256', 'hash_format',
                 'artifact_hashes', 'material_hashes', 'binding_hashes', 'attempt_hashes', 'binding', 'binding_provenance',
                 'materialization_provenance', 'normalization_provenance'}


def project_model_response(value):
    """Strip structured service metadata, preserving every string verbatim.

    This also handles malformed native objects in a format-repair prompt. It
    never searches prose, commits, quotes or paths for hexadecimal substrings.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in _SERVICE_KEYS or key.endswith('_sha256'):
                continue
            # Complete internal targets are replaced by Binding.project before
            # this projection. An unbound/invalid target is private metadata.
            if (key == 'review_target' or (key == 'target' and
                    (value.get('task') == 'architecture_review' or
                     isinstance(item, dict) and any(k.endswith('_sha256') for k in item)))):
                continue
            result[key] = project_model_response(item)
        return result
    if isinstance(value, list):
        return [project_model_response(item) for item in value]
    return copy.deepcopy(value)
