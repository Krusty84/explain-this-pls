# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Artifact-only schemas. These are never sent to a backend as wire schemas."""
from src.contracts.contracts import (SCHEMAS, FOLDER_SCHEMAS, MATERIALIZED_CLAIM, STRINGS, PRIOR_FINDING,
                       CONTRACT_ID, ARTIFACT_FORMAT, obj, array, string)
from src.reports.document_rendering import materialized_schema

INTEGER = {'type': 'integer', 'minimum': 0}
NULL_STRING = {'type': ['string', 'null']}
NORMALIZATION_PROVENANCE = obj(rule=string('EVIDENCE_IDS'),
    hash_format=string('canonical-json-utf8'), input_sha256=string(),
    normalized_sha256=string(), replacement_count=INTEGER)


def mapping(items):
    return {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': items}


IDENTITY = obj(mode=string('git', 'folder'), branch=string(), commit=string(), directory=string(),
               fingerprint=string(), parent_commit=string(), submodule_path=string())
IDENTITY['required'] = ['mode']
IDENTITY['properties'].update(repository=string(), branch=NULL_STRING, base_commit=string(),
    source_type=string('commit', 'working_tree'), snapshot_id=string(), submodule_head=string())
NULL_IDENTITY = dict(IDENTITY, type=['object', 'null'])
SOURCE = obj(id=string(), root=string(), identity=IDENTITY)
COVERAGE_PLAN = obj(contract_id=string(CONTRACT_ID), artifact_format=string(ARTIFACT_FORMAT),
    origin=string('SOURCE_INVENTORY'), sources=array(SOURCE), inventory_sha256=string(),
    areas=array(obj(id=string(), name=string(), purpose=string(), paths=STRINGS, entry_paths=STRINGS,
                    file_paths=STRINGS, required={'type': 'boolean'})),
    counts=obj(files=INTEGER, symlinks=INTEGER, directories=INTEGER, empty_directories=INTEGER),
    plan_sha256=string())
REGISTRY_DIFF = obj(added_ids=STRINGS, removed_ids=STRINGS, changed_ids=STRINGS, unchanged_ids=STRINGS)
REGISTRY_DIFF['properties'].update(previous_revision_id=string(), revision_id=string())
SOURCE_DECODING = obj(rules=array(obj(path=string(), encoding=string())))
PLAN = obj(sources=array(SOURCE),
    source_sha256=string(), document_sha256=string(), registry_sha256=string(), plan_sha256=string(),
    required_claim_ids=STRINGS, omission_areas=array(obj(id=string(), scope=string())), eligible_study={'type': 'boolean'},
    revision_id=string(), coverage_plan=dict(COVERAGE_PLAN, type=['object', 'null']), source_decoding=SOURCE_DECODING,
    prior_findings=array(PRIOR_FINDING), prior_review_sha256=NULL_STRING, previous_registry_sha256=NULL_STRING,
    registry_diff=dict(REGISTRY_DIFF, type=['object', 'null']))
RESOLUTION = obj(id=string(), source_id=string(), path=string(), start_line={'type': 'integer'},
    end_line={'type': 'integer'}, source_identity=NULL_IDENTITY, file_sha256=NULL_STRING,
    fragment_sha256=NULL_STRING, fragment_bytes=INTEGER, encoding=NULL_STRING, status=string('RESOLVED', 'INVALID_POINTER',
        'UNKNOWN_SOURCE', 'SOURCE_SCOPE_MISMATCH', 'UNSAFE_PATH', 'NOT_FOUND', 'ACCESS_DENIED', 'READ_ERROR',
        'OUT_OF_RANGE', 'DECODE_ERROR', 'ENCODING_MISMATCH', 'QUOTE_MISMATCH', 'LIMIT_EXCEEDED'))
RESOLUTION['required'].remove('fragment_bytes')
RESOLUTION['required'].remove('encoding')
# Older saved reports may carry this field; new resolutions do not hash snippets.
RESOLUTION['required'].remove('fragment_sha256')
CITATION_WARNING = obj(evidence_id=string(), source_id=string(), path=string(),
    start_line={'type': 'integer'}, end_line={'type': 'integer'}, reason=string(),
    claim_ids=STRINGS, action=string('UNRESOLVED', 'CORRECTED'))
CITATION_WARNING['properties'].update(original_start_line={'type': 'integer'},
                                      original_end_line={'type': 'integer'}, branch=string())
DOCUMENT_LINKS = obj(registered=INTEGER, matched=INTEGER, scope=string('REGISTERED_CLAIMS_ONLY'))
BASE_CHECKS = dict(execution=string('COMPLETED'), contract=string('VALID'), source=string('MATCHED_AT_BOUNDARIES',
    'NOT_INSPECTED_IN_COMPARISON'), evidence=array(RESOLUTION), policy_satisfied={'type': 'boolean'},
    semantic_quality=string('NOT_MEASURED'), completion_self_assessment={
        'type': ['string', 'null'], 'enum': ['COMPLETE', 'PARTIAL', 'BLOCKED', None]}, meaning=string())
CHECKS = {
    'study': obj(**BASE_CHECKS, evidence_counts=mapping(INTEGER), document_links=DOCUMENT_LINKS,
        coverage=obj(expected_ids=STRINGS, cited_ids=STRINGS, not_cited_ids=STRINGS, unverified_ids=STRINGS,
            metadata_verified_empty_paths=STRINGS, reported_by=string('ORCHESTRATOR'),
            completeness_measured={'type': 'boolean'})),
    'review': obj(**BASE_CHECKS, evidence_counts=mapping(INTEGER), document_links=DOCUMENT_LINKS,
        outcome_counts=mapping(INTEGER), finding_ids_by_claim=mapping(STRINGS),
        registry_coverage=obj(expected_ids=STRINGS, received_ids=STRINGS, assessed_ids=STRINGS,
            not_checked_ids=STRINGS, missing_ids=STRINGS, expected_count=INTEGER, received_count=INTEGER,
            assessed_count=INTEGER, fraction={'type': ['number', 'null']}, scope=string('FROZEN_REGISTRY_ONLY')),
        omission_coverage=obj(expected_ids=STRINGS, missing_ids=STRINGS, unfinished_ids=STRINGS,
            reported_by=string('AGENT'), completeness_measured={'type': 'boolean'}),
        prior_finding_coverage=obj(expected=array(PRIOR_FINDING), missing=array(PRIOR_FINDING), unresolved=array(PRIOR_FINDING))),
    'compare': obj(**BASE_CHECKS, evidence_scope=string('SUPPLIED_REPORTS_ONLY')),
}
for stage in ('study', 'review', 'compare'):
    # Optional saved-only fields keep earlier reports readable.
    CHECKS[stage]['properties'].update(continuation_allowed={'type': 'boolean'},
                                     citation_warnings=array(CITATION_WARNING))
ADVISORY = obj(raw_json=string(), diagnostics=array(obj(path=string(), code=string(), message=string())))


def artifact_schemas(wire):
    result = {}
    for stage, spec in wire.items():
        if stage == 'study':
            spec = materialized_schema(spec)
        properties = dict(spec['properties'], program_checks=CHECKS[stage])
        if stage in ('study', 'review'):
            properties['review_plan'] = PLAN
            properties['revision_id'] = string()
        if stage == 'study':
            properties['registry_diff'] = dict(REGISTRY_DIFF, type=['object', 'null'])
        if stage == 'review':
            properties.update(claim_registry=array(MATERIALIZED_CLAIM), verdict=string('PASS', 'CHANGES_REQUIRED', 'INCONCLUSIVE'))
        result[stage] = obj(**properties)
        if stage == 'study':
            result[stage]['properties']['advisory'] = ADVISORY
        if stage in ('study', 'review'):
            # Optional for direct prepare_result callers;
            # every new Runner publication writes this orchestrator-only record.
            result[stage]['properties']['normalization_provenance'] = NORMALIZATION_PROVENANCE
    return result


SAVED_SCHEMAS = artifact_schemas(SCHEMAS)
SAVED_FOLDER_SCHEMAS = artifact_schemas(FOLDER_SCHEMAS)
