# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Lossless content projection for model input; never used for validation/hashing."""
import copy

CONTEXT_FORMAT = 'compact-context'


def input_measurements(template, context_json, schema_json, prompt, *, correction=False):
    """Exact wire text sizes, NOT tokenizer estimates or quality measurements."""
    def size(text):
        return {'characters': len(text), 'utf8_bytes': len(text.encode('utf-8'))}
    return {'template': size('' if correction else template), 'projected_context': size(context_json),
            'schema': size(schema_json), 'prompt': size(prompt),
            'text_schema_copy': True, 'correction_prompt': correction, 'template_applied': not correction,
            'tokens': None, 'token_measurement': 'unavailable'}


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
    for area in plan.get('areas', []) + plan.get('exclusions', []):
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
    result.pop('_analysis_plan_path', None)
    result.pop('_synthesis_resolutions', None)
    result.pop('_shard_artifact_hashes', None)
    if isinstance(result.get('catalog'), dict):
        result['catalog'].pop('coverage_plan', None)
        result['catalog'].pop('program_checks', None)
    if stage == 'study' and result.get('prompt_variant') == 'synthesis':
        # All references already use global IDs; the combined registries are authoritative.
        result.pop('shard_id_mappings', None)
        for shard in result.get('validated_shards', []):
            for field in ('evidence', 'claims', 'coverage'):
                shard.pop(field, None)
    if stage == 'study-shard':
        # Scope is assigned locally. Do not expose other primary assignments,
        # expanded inventory membership, or the global analysis plan to a shard.
        assigned = set(result['analysis_shard']['subsystem_ids'])
        result.pop('analysis_plan', None)
        result['coverage_plan']['areas'] = [a for a in result['coverage_plan']['areas'] if a['id'] in assigned]
        result['coverage_plan'].pop('unclassified_paths', None)
        result['coverage_plan'].pop('counts', None)
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
    return project_model_response(result)


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
