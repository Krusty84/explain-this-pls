# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Artifact-only schemas. These are never sent to a backend as wire schemas."""
from contracts import SCHEMAS, FOLDER_SCHEMAS, CLAIM, STRINGS, obj, array, string

INTEGER = {'type': 'integer', 'minimum': 0}
NULL_STRING = {'type': ['string', 'null']}
NORMALIZATION_PROVENANCE = obj(rule=string('STUDY_LOCAL_EVIDENCE_REF_V1'),
    hash_format=string('canonical-json-utf8-v1'), extracted_sha256=string(),
    normalized_sha256=string(), replacement_count=INTEGER)


def mapping(items):
    return {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': items}


IDENTITY = obj(mode=string('git', 'folder'), branch=string(), commit=string(), directory=string(),
               fingerprint=string(), parent_commit=string(), submodule_path=string())
IDENTITY['required'] = ['mode']
NULL_IDENTITY = dict(IDENTITY, type=['object', 'null'])
SOURCE = obj(id=string(), root=string(), identity=IDENTITY)
PLAN = obj(sources=array(SOURCE),
    source_sha256=string(), document_sha256=string(), registry_sha256=string(), plan_sha256=string(),
    required_claim_ids=STRINGS, omission_areas=array(obj(id=string(), scope=string())), eligible_study={'type': 'boolean'})
RESOLUTION = obj(id=string(), source_id=string(), path=string(), start_line={'type': 'integer'},
    end_line={'type': 'integer'}, source_identity=NULL_IDENTITY, file_sha256=NULL_STRING,
    fragment_sha256=NULL_STRING, fragment_bytes=INTEGER, status=string('RESOLVED', 'INVALID_POINTER',
        'UNKNOWN_SOURCE', 'SOURCE_SCOPE_MISMATCH', 'UNSAFE_PATH', 'NOT_FOUND', 'ACCESS_DENIED', 'READ_ERROR',
        'OUT_OF_RANGE', 'DECODE_ERROR', 'QUOTE_MISMATCH', 'LIMIT_EXCEEDED'))
RESOLUTION['required'].remove('fragment_bytes')
DOCUMENT_LINKS = obj(registered=INTEGER, matched=INTEGER, scope=string('REGISTERED_CLAIMS_ONLY'))
BASE_CHECKS = dict(execution=string('COMPLETED'), contract=string('VALID'), source=string('MATCHED_AT_BOUNDARIES',
    'NOT_INSPECTED_IN_COMPARISON'), evidence=array(RESOLUTION), policy_satisfied={'type': 'boolean'},
    semantic_quality=string('NOT_MEASURED'), completion_self_assessment={
        'type': ['string', 'null'], 'enum': ['COMPLETE', 'PARTIAL', 'BLOCKED', None]}, meaning=string())
CHECKS = {
    'study': obj(**BASE_CHECKS, evidence_counts=mapping(INTEGER), document_links=DOCUMENT_LINKS),
    'review': obj(**BASE_CHECKS, evidence_counts=mapping(INTEGER), document_links=DOCUMENT_LINKS,
        outcome_counts=mapping(INTEGER), finding_ids_by_claim=mapping(STRINGS),
        registry_coverage=obj(expected_ids=STRINGS, received_ids=STRINGS, assessed_ids=STRINGS,
            not_checked_ids=STRINGS, missing_ids=STRINGS, expected_count=INTEGER, received_count=INTEGER,
            assessed_count=INTEGER, fraction={'type': ['number', 'null']}, scope=string('FROZEN_REGISTRY_ONLY')),
        omission_coverage=obj(expected_ids=STRINGS, missing_ids=STRINGS, unfinished_ids=STRINGS,
            reported_by=string('AGENT'), completeness_measured={'type': 'boolean'})),
    'compare': obj(**BASE_CHECKS, evidence_scope=string('SUPPLIED_REPORTS_ONLY')),
}


def artifact_schemas(wire):
    result = {}
    for stage, spec in wire.items():
        properties = dict(spec['properties'], program_checks=CHECKS[stage])
        if stage in ('study', 'review'):
            properties['review_plan'] = PLAN
        if stage == 'review':
            properties.update(claim_registry=array(CLAIM), verdict=string('PASS', 'CHANGES_REQUIRED', 'INCONCLUSIVE'))
        result[stage] = obj(**properties)
        if stage == 'study':
            # Optional for older saved artifacts and direct prepare_result callers;
            # every new Runner publication writes this orchestrator-only record.
            result[stage]['properties']['normalization_provenance'] = NORMALIZATION_PROVENANCE
    return result


SAVED_SCHEMAS = artifact_schemas(SCHEMAS)
SAVED_FOLDER_SCHEMAS = artifact_schemas(FOLDER_SCHEMAS)
