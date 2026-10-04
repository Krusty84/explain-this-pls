# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Versioned claim changes and deterministic choice of a published revision."""
from __future__ import annotations
import copy

from contracts import accepted, contract_violation, has_program_checks, CONTRACT_VERSION, ARTIFACT_VERSION, ContractError
from evidence import canonical, sha


def registry_diff(previous, current):
    """Compare registry content, never infer semantic equivalence from prose."""
    old = {c['id']: c for c in (previous['claims'] if isinstance(previous, dict) else previous)}
    new = {c['id']: c for c in (current['claims'] if isinstance(current, dict) else current)}
    def payload(claim):
        return canonical({k: v for k, v in claim.items() if k not in ('id', 'document_locators')})
    old_values = {identifier: payload(c) for identifier, c in old.items()}
    new_values = {identifier: payload(c) for identifier, c in new.items()}
    for identifier in new.keys() - old.keys():
        if any(new_values[identifier] == content for old_id, content in old_values.items()
               if old_id not in new or new_values[old_id] != content):
            raise contract_violation('UNCHANGED_CLAIM_RENUMBERED', '$.claims')
    return {'added_ids': sorted(new.keys() - old.keys()), 'removed_ids': sorted(old.keys() - new.keys()),
            'changed_ids': sorted(i for i in old.keys() & new.keys() if old_values[i] != new_values[i]),
            'unchanged_ids': sorted(i for i in old.keys() & new.keys() if old_values[i] == new_values[i])}


def revision_inputs(previous_revision):
    revision_id = previous_revision['revision_id']
    study, review = previous_revision['study'], previous_revision['review']
    return {'previous_revision': {'revision_id': revision_id, 'study': copy.deepcopy(study), 'review': copy.deepcopy(review)},
            'prior_findings': [{'revision_id': revision_id, 'finding_id': f['id']}
                               for f in review['findings'] if f['severity'] in ('HIGH', 'MEDIUM')]}


def completed_pair(revision):
    study, review = revision.get('study'), revision.get('review')
    if not (study and review and has_program_checks(study) and has_program_checks(review)
            and review.get('completion_status') == 'COMPLETE'
            and all((revision.get(stage + '_invocation') or {}).get('publication_complete') for stage in ('study', 'review'))):
        return False
    for stage in ('study', 'review'):
        metadata = revision.get(stage + '_invocation') or {}
        if metadata.get('contract_version') != CONTRACT_VERSION or metadata.get('artifact_version') != ARTIFACT_VERSION:
            return False
    from ledger import target
    from document_rendering import validate_materialized
    from contracts import validate_schema
    from saved_contracts import SAVED_SCHEMAS, SAVED_FOLDER_SCHEMAS
    try:
        validate_materialized(study)
        schemas = SAVED_FOLDER_SCHEMAS if 'source_directory' in study else SAVED_SCHEMAS
        validate_schema(study, schemas['study'])
        validate_schema(review, schemas['review'])
    except (ContractError, KeyError, TypeError):
        return False
    plan = study.get('review_plan')
    try:
        return bool(plan and review.get('target') == target(plan) and review.get('review_plan') == plan
                    and review.get('claim_registry') == study.get('claims')
                    and review.get('revision_id') == study.get('revision_id') == revision.get('revision_id')
                    and plan.get('revision_id') == revision.get('revision_id')
                    and all(study.get(k) == review.get(k) and (k not in revision or revision[k] == study.get(k))
                            for k in ('branch', 'source_commit', 'source_directory', 'source_fingerprint'))
                    and plan['document_sha256'] == sha(study['report_markdown'].encode('utf-8'))
                    and plan['registry_sha256'] == sha(canonical(study['claims']))
                    and plan['plan_sha256'] == sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'})))
    except (KeyError, TypeError):
        return False


def choose_revision(revisions):
    ordered = list(reversed(revisions))
    for revision in ordered:
        if accepted(revision):
            return revision
    for revision in ordered:
        if completed_pair(revision):
            return revision
    from final_report import usable_study
    return next((revision for revision in ordered if usable_study(revision)), None)
