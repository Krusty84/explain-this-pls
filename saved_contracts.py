# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Artifact-only schemas. These are never sent to a backend as wire schemas."""
from contracts import (SCHEMAS, FOLDER_SCHEMAS, MATERIALIZED_CLAIM, STRINGS, PRIOR_FINDING,
                       CONTRACT_ID, obj, array, string)
from document_rendering import materialized_schema

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
NULL_IDENTITY = dict(IDENTITY, type=['object', 'null'])
SOURCE = obj(id=string(), root=string(), identity=IDENTITY)
COVERAGE_PLAN = obj(contract_id=string(CONTRACT_ID), origin=string('AGENT', 'DIRECTORY_FALLBACK'),
    catalog_status=string('COMPLETE', 'PARTIAL', 'BLOCKED'), limitations=STRINGS, sources=array(SOURCE), inventory_sha256=string(),
    areas=array(obj(id=string(), name=string(), purpose=string(), paths=STRINGS, entry_paths=STRINGS,
                    file_paths=STRINGS, required={'type': 'boolean'})),
    exclusions=array(obj(path=string(), reason=string(), entry_paths=STRINGS, file_paths=STRINGS)),
    unclassified_paths=STRINGS, counts=obj(files=INTEGER, symlinks=INTEGER, directories=INTEGER, empty_directories=INTEGER, assigned_files=INTEGER,
        excluded_files=INTEGER, unclassified_files=INTEGER, overlapping_files=INTEGER),
    policy_satisfied={'type': 'boolean'}, plan_sha256=string())
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
DOCUMENT_LINKS = obj(registered=INTEGER, matched=INTEGER, scope=string('REGISTERED_CLAIMS_ONLY'))
BASE_CHECKS = dict(execution=string('COMPLETED'), contract=string('VALID'), source=string('MATCHED_AT_BOUNDARIES',
    'NOT_INSPECTED_IN_COMPARISON'), evidence=array(RESOLUTION), policy_satisfied={'type': 'boolean'},
    semantic_quality=string('NOT_MEASURED'), completion_self_assessment={
        'type': ['string', 'null'], 'enum': ['COMPLETE', 'PARTIAL', 'BLOCKED', None]}, meaning=string())
CHECKS = {
    'catalog': obj(**BASE_CHECKS),
    'study': obj(**BASE_CHECKS, evidence_counts=mapping(INTEGER), document_links=DOCUMENT_LINKS,
        coverage=obj(expected_ids=STRINGS, missing_ids=STRINGS, unfinished_ids=STRINGS, unsupported_ids=STRINGS,
            reported_by=string('AGENT'), completeness_measured={'type': 'boolean'}, policy_satisfied={'type': 'boolean'})),
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


def artifact_schemas(wire):
    result = {}
    for stage, spec in wire.items():
        if stage == 'study':
            spec = materialized_schema(spec)
        properties = dict(spec['properties'], program_checks=CHECKS[stage])
        if stage == 'catalog':
            properties['coverage_plan'] = COVERAGE_PLAN
        if stage in ('study', 'review'):
            properties['review_plan'] = PLAN
            properties['revision_id'] = string()
        if stage == 'study':
            properties['registry_diff'] = dict(REGISTRY_DIFF, type=['object', 'null'])
        if stage == 'review':
            properties.update(claim_registry=array(MATERIALIZED_CLAIM), verdict=string('PASS', 'CHANGES_REQUIRED', 'INCONCLUSIVE'))
        result[stage] = obj(**properties)
        if stage in ('study', 'review'):
            # Optional for direct prepare_result callers;
            # every new Runner publication writes this orchestrator-only record.
            result[stage]['properties']['normalization_provenance'] = NORMALIZATION_PROVENANCE
    return result


SAVED_SCHEMAS = artifact_schemas(SCHEMAS)
SAVED_FOLDER_SCHEMAS = artifact_schemas(FOLDER_SCHEMAS)
