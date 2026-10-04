# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Output schemas and deterministic semantic gates (stdlib only)."""
from __future__ import annotations
import copy
import json
import math
import re
from typing import Any

class ContractError(ValueError):
    def __init__(self, message, *, failure_kind='SEMANTIC_ERROR', failure_layer='semantic',
                 details=None, safe=False):
        super().__init__(message)
        self.failure_kind = failure_kind
        self.failure_layer = failure_layer
        self.details = details or {}
        self.safe_message = message if safe else 'The agent response failed contract validation.'


def response_error(kind, layer, message, **details):
    return ContractError(message, failure_kind=kind, failure_layer=layer,
                         details=details, safe=True)


def contract_violation(code, path, *, kind='SEMANTIC_ERROR', layer='semantic', **details):
    """Callers supply closed codes and schema-owned paths, never response text/IDs."""
    suffix = (f"; expected {details['expected_type']}, got {details['actual_type']}"
              if code == 'CLAIMS_TYPE_MISMATCH' else '')
    return response_error(kind, layer, f'Response contract rejected: {code} at {path}{suffix}.',
                          code=code, path=path, **details)


def json_type(value):
    return {dict: 'object', list: 'array', str: 'string', bool: 'boolean',
            int: 'integer', float: 'number', type(None): 'null'}.get(type(value), 'non_json')


def json_error_details(exc):
    # JSONDecodeError.msg can be supplied by callers, so use a closed vocabulary.
    messages = {'Expecting value', 'Expecting property name enclosed in double quotes',
                "Expecting ':' delimiter", "Expecting ',' delimiter", 'Extra data',
                'Unterminated string starting at', 'Invalid \\escape',
                'Invalid \\uXXXX escape', 'Invalid control character at',
                'Unterminated block comment'}
    return {'json_error': exc.msg if exc.msg in messages else 'Invalid JSON syntax',
            'line': exc.lineno, 'column': exc.colno, 'position': exc.pos}

def string(*values: str) -> dict:
    spec = {'type': 'string'}
    if values:
        spec['enum'] = list(values)
    return spec

def array(items: dict) -> dict:
    return {'type': 'array', 'items': items}

def obj(**properties: dict) -> dict:
    return {'type': 'object', 'properties': properties,
            'required': list(properties), 'additionalProperties': False}

# Wire schemas deliberately use only type/enum/properties/required/items and
# additionalProperties. Local semantic checks enforce nonblank strings, bounds
# and graph constraints regardless of a backend's JSON Schema dialect.
CONTRACT_VERSION = 'evidence-ledger-v4'
ARTIFACT_VERSION = 'evidence-ledger-artifacts-v4'
STRINGS = array(string())
STATUS = string('COMPLETE', 'PARTIAL', 'BLOCKED')
BASE = dict(completion_status=STATUS, report_markdown=string(), limitations=STRINGS)
LOCATOR = obj(start_line={'type': 'integer'}, end_line={'type': 'integer'}, quote=string())
EVIDENCE = array(obj(id=string(), source_id=string(), path=string(),
                     start_line={'type': 'integer'}, end_line={'type': 'integer'}, quote=string()))
CLAIM = obj(id=string(), statement=string(), scope=string(),
    epistemic_kind=string('FACT', 'HYPOTHESIS', 'UNKNOWN'), evidence_ids=STRINGS,
    uncertainty=string())
MATERIALIZED_CLAIM = obj(**CLAIM['properties'], document_locators=array(LOCATOR))
SECTION_KEYS = ('scope', 'context', 'components', 'startup_and_flows', 'data_and_state',
                'cross_cutting', 'constraints', 'change_navigation', 'unknowns', 'evidence_basis')
SECTIONS = array(obj(key=string(*SECTION_KEYS), title=string(),
                     blocks=array(obj(markdown=string(), claim_ids=STRINGS))))
STUDY_BASE = dict(completion_status=STATUS, report_sections=SECTIONS, limitations=STRINGS)
INSPECTION_STATUS = string('INSPECTED', 'PARTIALLY_INSPECTED', 'NOT_INSPECTED')
COVERAGE = array(obj(area_id=string(), status=INSPECTION_STATUS, evidence_ids=STRINGS, limitation=string()))
PRIOR_FINDING = obj(revision_id=string(), finding_id=string())
TARGET = obj(source_sha256=string(), document_sha256=string(), registry_sha256=string(), plan_sha256=string())
SCHEMAS = {
    'catalog': obj(task=string('architecture_catalog'), branch=string(), source_commit=string(),
        completion_status=STATUS, limitations=STRINGS,
        subsystems=array(obj(id=string(), name=string(), purpose=string(), paths=STRINGS)),
        exclusions=array(obj(path=string(), reason=string()))),
    'study': obj(**STUDY_BASE, task=string('architecture_documentation'), branch=string(), source_commit=string(),
                 evidence=EVIDENCE, claims=array(CLAIM), coverage=COVERAGE),
    'review': obj(**BASE, task=string('architecture_review'), branch=string(), source_commit=string(),
        target=TARGET, evidence=EVIDENCE,
        claims=array(obj(id=string(), outcome=string('SUPPORTED', 'CONTRADICTED', 'UNVERIFIABLE',
            'NOT_CHECKED', 'CAVEAT_ACCEPTABLE', 'CAVEAT_INADEQUATE'), evidence_ids=STRINGS, limitation=string())),
        findings=array(obj(id=string(), severity=string('HIGH', 'MEDIUM', 'LOW'),
            type=string('FACTUAL_ERROR', 'UNSUPPORTED_ASSERTION', 'MATERIAL_OMISSION',
                        'SCOPE_MISMATCH', 'CONTRACT_VIOLATION'),
            claim_ids=STRINGS, location=string(), evidence_ids=STRINGS, impact=string(), proposed_correction=string())),
        omission_search=array(obj(area_id=string(), status=string('INSPECTED', 'PARTIALLY_INSPECTED', 'NOT_INSPECTED'),
                                  limitation=string(), finding_ids=STRINGS)),
        prior_findings=array(obj(**PRIOR_FINDING['properties'],
            status=string('RESOLVED', 'UNRESOLVED', 'NOT_CHECKED'), explanation=string()))),
    'compare': obj(**BASE, task=string('architecture_comparison'), baseline_branch=string(), baseline_commit=string(),
        compared_branches=STRINGS, unresolved_branches=STRINGS,
        differences=array(obj(id=string(), branch=string(), category=string(),
            classification=string('CONFIRMED_DIFFERENCE', 'REPORTED_UNVERIFIED', 'INSUFFICIENT_EVIDENCE'),
            baseline_statement=string(), branch_statement=string(),
            evidence_refs=array(obj(branch=string(), artifact=string('study', 'review'), claim_id=string(),
                                    revision_id=string(), document_sha256=string(), registry_sha256=string())), explanation=string())))
}
FOLDER_SCHEMAS = {
    stage: obj(**({key: spec for key, spec in SCHEMAS[stage]['properties'].items()
                  if key not in ('branch', 'source_commit')} |
                 {'source_directory': string(), 'source_fingerprint': string()}))
    for stage in ('catalog', 'study', 'review')
}

def model_schemas(internal):
    """Backend contracts contain opaque bindings; internal schemas retain hashes."""
    schemas = copy.deepcopy(internal)
    for stage, schema in schemas.items():
        properties = schema['properties']
        if 'source_fingerprint' in properties:
            properties.pop('source_fingerprint')
            properties['source_snapshot_id'] = string()
        if stage == 'review':
            properties.pop('target')
            properties['review_target_id'] = string()
        if stage == 'compare':
            ref = properties['differences']['items']['properties']['evidence_refs']['items']
            ref['properties'].pop('document_sha256')
            ref['properties'].pop('registry_sha256')
            ref['properties']['review_target_id'] = string()
            ref['required'] = list(ref['properties'])
        schema['required'] = list(properties)
    return schemas


MODEL_SCHEMAS = model_schemas(SCHEMAS)
MODEL_FOLDER_SCHEMAS = model_schemas(FOLDER_SCHEMAS)

def strict_json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise response_error('INVALID_JSON', 'result', 'Duplicate JSON key.', json_error='duplicate_key')
            result[key] = value
        return result
    def reject(value):
        raise response_error('INVALID_JSON', 'result', 'Non-finite JSON value.', json_error='non_finite')
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            reject(value)
        return number
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=reject, parse_float=finite_float)
    except json.JSONDecodeError as exc:
        raise response_error('INVALID_JSON', 'result',
                             f'Invalid JSON syntax: line {exc.lineno} column {exc.colno} (char {exc.pos}).',
                             **json_error_details(exc)) from exc
    except (UnicodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise response_error('INVALID_JSON', 'result', 'Invalid JSON encoding or numeric/nesting limit.') from exc

def jsonc(text: str) -> Any:
    """Replace comments/trailing commas with spaces, preserving error positions."""
    chars = list(text)
    tokens: list[tuple[str, int]] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == '"':
            tokens.append(('string', i))
            i += 1
            while i < len(text):
                if text[i] == '\\':
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            continue
        if text.startswith('//', i) or text.startswith('/*', i):
            start = i
            if text[i + 1] == '/':
                while i < len(text) and text[i] not in '\r\n':
                    i += 1
            else:
                end = text.find('*/', i + 2)
                if end == -1:
                    error = json.JSONDecodeError('Unterminated block comment', text, start)
                    raise response_error('INVALID_JSON', 'configuration',
                                         f'Invalid JSONC comment: line {error.lineno} column {error.colno} (char {error.pos}).',
                                         **json_error_details(error)) from error
                i = end + 2
            for pos in range(start, i):
                if chars[pos] not in '\r\n':
                    chars[pos] = ' '
            continue
        if not char.isspace():
            tokens.append((char, i))
        i += 1
    for index in range(1, len(tokens) - 1):
        token, pos = tokens[index]
        if token == ',' and tokens[index + 1][0] in (']', '}'):
            # Do not accidentally accept an empty array/object containing a comma.
            if tokens[index - 1][0] not in ('[', '{', ',', ':'):
                chars[pos] = ' '
    return strict_json(''.join(chars))

def schema_diagnostics(value: Any, schema: dict, where: str = '$', *, private=False, limit=100) -> dict:
    """Collect every defect count, but bound the detail list. Paths use trusted schema keys.

    Only private artifacts/repair input may contain arbitrary additional key names.
    Values remain in the original extracted object, never in public diagnostics.
    """
    types = {'object': (dict,), 'array': (list,), 'string': (str,), 'boolean': (bool,),
             'integer': (int,), 'number': (int, float), 'null': (type(None),)}
    violations, total = [], 0
    def add(path, kind, **detail):
        nonlocal total
        total += 1
        if len(violations) < limit:
            violations.append({'path': path, 'violation': kind,
                               'missing_keys': [], 'extra_key_count': 0, **detail})
    def visit(data, spec, path):
        requested = spec['type']
        permitted = requested if isinstance(requested, list) else [requested]
        typ = next((name for name in permitted if type(data) in types[name]), None)
        if typ is None:
            add(path, 'type', expected_type=requested, actual_type=json_type(data),
                **({'code': 'CLAIMS_TYPE_MISMATCH'} if path == '$.claims' and requested == 'array' else {}))
            return
        if 'enum' in spec and data not in spec['enum']:
            add(path, 'enum')
        if typ == 'string' and len(data) < spec.get('minLength', 0):
            add(path, 'minLength')
        if typ == 'integer' and 'minimum' in spec and data < spec['minimum']:
            add(path, 'minimum')
        if typ == 'array' and len(data) < spec.get('minItems', 0):
            add(path, 'minItems')
        if typ == 'object':
            missing = [key for key in spec['required'] if key not in data]
            extra = [key for key in data if key not in spec['properties']]
            additional = spec.get('additionalProperties', False)
            if missing or (extra and additional is False):
                add(path, 'required/additionalProperties', missing_keys=missing,
                    extra_key_count=len(extra), **({'extra_keys': extra} if private else {}))
            for key, child in spec['properties'].items():
                if key in data:
                    visit(data[key], child, f'{path}.{key}')
            if isinstance(additional, dict):
                for index, key in enumerate(extra):
                    visit(data[key], additional, f'{path}.values[{index}]')
        elif typ == 'array':
            for i, item in enumerate(data):
                visit(item, spec['items'], f'{path}[{i}]')
    visit(value, schema, where)
    return {'violations': violations, 'total_violations': total, 'truncated': total > len(violations)}


def validate_schema(value: Any, schema: dict, where: str = '$') -> None:
    details = schema_diagnostics(value, schema, where)
    if details['total_violations']:
        first = details['violations'][0]
        if (where == '$' and type(value) is dict and 'claims' in value
                and schema.get('properties', {}).get('claims', {}).get('type') == 'array'
                and type(value['claims']) is not list):
            raise contract_violation('CLAIMS_TYPE_MISMATCH', '$.claims', kind='SCHEMA_ERROR', layer='schema',
                expected_type='array', actual_type=json_type(value['claims']), **details)
        if first['path'] == '$.task' and first['violation'] == 'enum':
            raise contract_violation('TASK_IDENTITY_MISMATCH', '$.task', kind='SCHEMA_ERROR', layer='schema', **details)
        raise response_error('SCHEMA_ERROR', 'schema',
            f"Structured result has {details['total_violations']} schema violation(s); first at {first['path']}.",
            **first, **details)


def has_ledger_structure(stage, value, *, representation='materialized'):
    """Recognize substantive ledger fields without a model-supplied version tag."""
    fields = {'catalog': ('subsystems', 'exclusions'), 'study': ('claims', 'evidence', 'coverage'),
              'review': ('claims', 'evidence', 'target', 'omission_search', 'prior_findings'),
              'compare': ('differences', 'unresolved_branches')}
    specs = dict(SCHEMAS[stage]['properties'])
    if stage == 'study' and representation == 'wire' and (type(value) is not dict or 'report_sections' not in value):
        return False
    if stage == 'study' and representation != 'wire':
        specs['claims'] = array(MATERIALIZED_CLAIM)
        if type(value) is not dict or type(value.get('report_markdown')) is not str:
            return False
    return type(value) is dict and all(not schema_diagnostics(
        value.get(key), specs[key], limit=0)['total_violations']
        for key in fields[stage])


def has_program_checks(value):
    """Saved processing marker, forbidden in wire responses; not a signature."""
    return (type(value) is dict and type(value.get('program_checks')) is dict
            and value['program_checks'].get('contract') == 'VALID')

def is_recovered_material(stage, value):
    """Recognize explicitly retained current-format text without a claim registry."""
    origins = {'study': 'PROGRAM_ASSEMBLED_AUTHOR_BLOCKS', 'review': 'MODEL_MARKDOWN'}
    if (stage not in origins or type(value) is not dict or value.get('strict_valid') is not False
            or value.get('narrative_origin') != origins[stage]
            or value.get('task') != SCHEMAS[stage]['properties']['task']['enum'][0]
            or type(value.get('report_markdown')) is not str or not value['report_markdown'].strip()
            or type(value.get('validation_issues')) is not dict
            or any(key in value for key in ('claims', 'program_checks', 'materialization_provenance'))):
        return False
    if 'normalization_provenance' in value:
        from saved_contracts import NORMALIZATION_PROVENANCE
        if schema_diagnostics(value['normalization_provenance'], NORMALIZATION_PROVENANCE, limit=0)['total_violations']:
            return False
    return True

def unique_ids(records, pattern, path):
    seen = set()
    for i, record in enumerate(records):
        identifier = record['id']
        if not re.fullmatch(pattern, identifier):
            raise contract_violation('INVALID_RECORD_ID', f'{path}[{i}].id')
        if identifier in seen:
            raise contract_violation('DUPLICATE_RECORD_ID', f'{path}[{i}].id')
        seen.add(identifier)
    return seen


def review_verdict(value):
    if any(f['severity'] in ('HIGH', 'MEDIUM') for f in value.get('findings', [])):
        return 'CHANGES_REQUIRED'
    return 'PASS' if value.get('program_checks', {}).get('policy_satisfied') else 'INCONCLUSIVE'


def accepted(item):
    from ledger import accepted_pair
    return accepted_pair(item)


def nonblank(value, key=''):
    if type(value) is str and not value.strip() and key not in ('quote', 'limitation', 'uncertainty'):
        raise ContractError('Required strings must not be empty or whitespace')
    if type(value) is dict:
        for name, child in value.items():
            nonblank(child, name)
    elif type(value) is list:
        for child in value:
            nonblank(child, key)


def references(refs, allowed, path, *, namespaces=None):
    seen = set()
    for i, ref in enumerate(refs):
        at = f'{path}[{i}]'
        if namespaces is not None:
            # Bare IDs (including unknown ones) are unknown references in strict
            # validation. An explicit foreign/malformed namespace is distinct.
            if ':' in ref and ref.split(':', 1)[0] not in namespaces:
                raise contract_violation('INVALID_EVIDENCE_NAMESPACE', at)
        subject = 'EVIDENCE_REFERENCE' if namespaces is not None else 'REFERENCE'
        if ref in seen:
            raise contract_violation('DUPLICATE_' + subject, at)
        if ref not in allowed:
            raise contract_violation('UNKNOWN_' + subject, at)
        seen.add(ref)


def validate_wire_identity(stage, value, context, mode='git'):
    """Structural and pinned-identity prerequisites; performs no repair."""
    validate_schema(value, (FOLDER_SCHEMAS if mode == 'folder' else SCHEMAS)[stage])
    if stage in ('catalog', 'study', 'review'):
        identity = ('source_directory', 'source_fingerprint') if mode == 'folder' else ('branch', 'source_commit')
        for key in identity:
            if value[key] != context.get(key):
                raise contract_violation('SOURCE_IDENTITY_MISMATCH', '$.' + key,
                                         kind='IDENTITY_MISMATCH', layer='identity')
    if stage == 'review':
        from ledger import verify_review_context
        verify_review_context(context)
        if value['target'] != context['review_target']:
            key = next(k for k in TARGET['properties'] if value['target'][k] != context['review_target'][k])
            raise contract_violation('TARGET_IDENTITY_MISMATCH', '$.target.' + key,
                                     kind='IDENTITY_MISMATCH', layer='identity')


def validate_result(stage, value, context, mode='git'):
    validate_wire_identity(stage, value, context, mode)
    nonblank(value)
    if value['completion_status'] != 'COMPLETE' and not value['limitations']:
        raise ContractError('PARTIAL/BLOCKED requires explicit limitations')
    if stage == 'catalog':
        from coverage_plan import checked_path, build_coverage_plan
        unique_ids(value['subsystems'], r'S-[0-9]{3,}', '$.subsystems')
        for subsystem in value['subsystems']:
            if not subsystem['paths'] or len(subsystem['paths']) != len(set(subsystem['paths'])):
                raise contract_violation('INVALID_CATALOG_PATHS', '$.subsystems')
            for path in subsystem['paths']:
                checked_path(path)
        for exclusion in value['exclusions']:
            checked_path(exclusion['path'])
        if '_inventory' in context:
            build_coverage_plan(value, context['_inventory'], context)
        return
    if stage in ('study', 'review'):
        unique_ids(value['evidence'], r'E-[0-9]{3,}', '$.evidence')
        for i, e in enumerate(value['evidence']):
            if e['start_line'] < 1 or e['end_line'] < e['start_line']:
                raise contract_violation('INVALID_EVIDENCE_LINE_RANGE', f'$.evidence[{i}]')
        # Invalid locators remain machine-readable evidence results and prevent
        # policy success. Wrong JSON types still fail schema validation.
        evidence_ids = {stage + ':' + e['id'] for e in value['evidence']}
        unique_ids(value['claims'], r'C-[0-9]{3,}', '$.claims')
    if stage == 'study':
        from document_rendering import validate_sections
        validate_sections(value['report_sections'], value['claims'])
        for i, claim in enumerate(value['claims']):
            references(claim['evidence_ids'], evidence_ids, f'$.claims[{i}].evidence_ids', namespaces={'study'})
            if claim['epistemic_kind'] != 'FACT' and not claim['uncertainty'].strip():
                raise ContractError('Hypothesis/unknown requires a concrete missing check or limitation')
        areas = {a['id'] for a in (context.get('coverage_plan') or {}).get('areas', [])}
        seen = set()
        for i, area in enumerate(value['coverage']):
            if area['area_id'] in seen or area['area_id'] not in areas:
                raise contract_violation('INVALID_COVERAGE_AREA_ID', f'$.coverage[{i}].area_id')
            seen.add(area['area_id'])
            references(area['evidence_ids'], evidence_ids, f'$.coverage[{i}].evidence_ids', namespaces={'study'})
            if area['status'] != 'INSPECTED' and not area['limitation'].strip():
                raise ContractError('Unfinished coverage requires a limitation')
        if context.get('previous_revision'):
            from revisions import registry_diff
            registry_diff(context['previous_revision']['study'], value)
    elif stage == 'review':
        registry = {c['id']: c for c in context['claim_registry']}
        for i, c in enumerate(value['claims']):
            if c['id'] not in registry:
                raise contract_violation('UNKNOWN_CLAIM_ID', f'$.claims[{i}].id')
        study = context['architecture_document']
        evidence_ids |= {'study:' + e['id'] for e in study.get('evidence', [])}
        fids = unique_ids(value['findings'], r'F-[0-9]{3,}', '$.findings')
        for i, finding in enumerate(value['findings']):
            references(finding['claim_ids'], registry.keys(), f'$.findings[{i}].claim_ids')
            references(finding['evidence_ids'], evidence_ids, f'$.findings[{i}].evidence_ids', namespaces={'study', 'review'})
            if not finding['evidence_ids']:
                raise ContractError('Finding requires source evidence')
        for i, claim in enumerate(value['claims']):
            references(claim['evidence_ids'], evidence_ids, f'$.claims[{i}].evidence_ids', namespaces={'study', 'review'})
            fact = registry[claim['id']]['epistemic_kind'] == 'FACT'
            permitted = {'SUPPORTED', 'CONTRADICTED', 'UNVERIFIABLE', 'NOT_CHECKED'} if fact else {
                'CAVEAT_ACCEPTABLE', 'CAVEAT_INADEQUATE', 'NOT_CHECKED'}
            if claim['outcome'] not in permitted:
                raise ContractError('Outcome is incompatible with the frozen epistemic kind')
            if claim['outcome'] in ('SUPPORTED', 'CONTRADICTED') and not claim['evidence_ids']:
                raise ContractError('Supported/contradicted assessments require source evidence')
            if claim['outcome'] in ('UNVERIFIABLE', 'NOT_CHECKED', 'CAVEAT_INADEQUATE') and not claim['limitation'].strip():
                raise ContractError('Unassessed or insufficient assessment requires a limitation')
            if claim['outcome'] in ('CONTRADICTED', 'UNVERIFIABLE', 'CAVEAT_INADEQUATE') and not any(
                    claim['id'] in f['claim_ids'] and f['severity'] in ('HIGH', 'MEDIUM') for f in value['findings']):
                raise ContractError('Material issue requires a linked material finding')
        areas = {a['id'] for a in context['review_plan']['omission_areas']}
        returned = set()
        for i, area in enumerate(value['omission_search']):
            if area['area_id'] in returned:
                raise contract_violation('DUPLICATE_OMISSION_AREA_ID', f'$.omission_search[{i}].area_id')
            if area['area_id'] not in areas:
                raise contract_violation('UNKNOWN_OMISSION_AREA_ID', f'$.omission_search[{i}].area_id')
            returned.add(area['area_id'])
            references(area['finding_ids'], fids, f'$.omission_search[{i}].finding_ids')
            if area['status'] != 'INSPECTED' and not area['limitation'].strip():
                raise ContractError('Unfinished omission search requires a limitation')
        obligations = {(f['revision_id'], f['finding_id']) for f in context.get('prior_findings', [])}
        replied = set()
        for i, finding in enumerate(value['prior_findings']):
            key = (finding['revision_id'], finding['finding_id'])
            if key not in obligations or key in replied:
                raise contract_violation('INVALID_PRIOR_FINDING_REFERENCE', f'$.prior_findings[{i}]')
            replied.add(key)
    else:
        expected = [b for b in context['requested_branches'] if b != context['baseline_branch']]
        if value['baseline_branch'] != context['baseline_branch'] or value['baseline_commit'] != context['baseline_commit']:
            raise response_error('IDENTITY_MISMATCH', 'identity', 'Comparison baseline mismatch.')
        if sorted(value['compared_branches']) != sorted(expected):
            raise ContractError('Comparison must cover each non-baseline branch exactly once')
        references(value['unresolved_branches'], set(context['requested_branches']), '$.unresolved_branches')
        entries = {b['branch']: b for b in context['branches']}
        missing = {b for b in context['requested_branches'] if not accepted(entries.get(b, {}))}
        if not missing <= set(value['unresolved_branches']):
            raise ContractError('Comparison conceals unaccepted inputs')
        if value['completion_status'] == 'COMPLETE' and value['unresolved_branches']:
            raise ContractError('COMPLETE comparison contains unresolved inputs')
        unique_ids(value['differences'], r'D-[0-9]{3,}', '$.differences')
        for diff in value['differences']:
            if diff['branch'] not in expected:
                raise ContractError('Unknown difference branch')
            sides = set()
            fact_sides = set()
            for ref in diff['evidence_refs']:
                if ref['branch'] not in (context['baseline_branch'], diff['branch']):
                    raise ContractError('Evidence references the wrong comparison side')
                entry = entries.get(ref['branch'], {})
                doc = entry.get('study') or {}
                plan = doc.get('review_plan', {})
                material = entry.get(ref['artifact']) or {}
                if (ref['revision_id'] != doc.get('revision_id')
                        or ('selected_revision' in entry and ref['revision_id'] != entry['selected_revision'])
                        or ref['document_sha256'] != plan.get('document_sha256') or
                        ref['registry_sha256'] != plan.get('registry_sha256') or
                        ref['claim_id'] not in {c['id'] for c in material.get('claims', [])}):
                    raise ContractError('Comparison reference does not resolve in the supplied report')
                sides.add(ref['branch'])
                registered = {c['id']: c for c in doc.get('claims', [])}
                assessed = {c['id']: c for c in (entry.get('review') or {}).get('claims', [])}
                if (registered.get(ref['claim_id'], {}).get('epistemic_kind') == 'FACT' and
                        assessed.get(ref['claim_id'], {}).get('outcome') == 'SUPPORTED'):
                    fact_sides.add(ref['branch'])
            if diff['classification'] == 'CONFIRMED_DIFFERENCE':
                if sides != {context['baseline_branch'], diff['branch']} or sides & missing or fact_sides != sides:
                    raise ContractError('Strong contrast requires supported factual references and accepted inputs on both sides')


def result_diagnostics(stage, value, context, mode='git'):
    schema = (FOLDER_SCHEMAS if mode == 'folder' else SCHEMAS)[stage]
    structural = schema_diagnostics(value, schema)
    issues = []
    if not structural['total_violations']:
        try:
            validate_result(stage, value, context, mode)
        except ContractError as exc:
            issues.append({'path': exc.details.get('path', '$'),
                           'code': exc.details.get('code', exc.failure_kind), 'message': exc.safe_message})
    if type(value) is dict and stage == 'review':
        # Inspect independently valid records even when another row is malformed.
        # Only trusted field paths and closed messages appear in diagnostics.
        records = {}
        for name in ('claims', 'findings'):
            items = value.get(name)
            records[name] = [(i, r) for i, r in enumerate(items) if not schema_diagnostics(
                r, schema['properties'][name]['items'], limit=0)['total_violations']] if type(items) is list else []
        registry = {c['id'] for c in context.get('claim_registry', [])}
        seen = set()
        for i, c in records['claims']:
            path = f'$.claims[{i}]'
            if c['id'] in seen or not re.fullmatch(r'C-[0-9]{3,}', c['id']):
                code = 'DUPLICATE_RECORD_ID' if c['id'] in seen else 'INVALID_RECORD_ID'
                issues.append({'path': path + '.id', 'code': code, 'message': 'Invalid or duplicate claim ID.'})
            seen.add(c['id'])
            if c['id'] not in registry:
                issues.append({'path': path + '.id', 'code': 'UNKNOWN_CLAIM_ID', 'message': 'Claim is absent from the frozen registry.'})
            if c['outcome'] in ('CONTRADICTED', 'UNVERIFIABLE', 'CAVEAT_INADEQUATE') and not any(
                    c['id'] in f['claim_ids'] and f['severity'] in ('HIGH', 'MEDIUM') for _, f in records['findings']):
                issues.append({'path': path, 'code': 'MISSING_MATERIAL_FINDING',
                               'message': 'Material issue lacks a linked HIGH/MEDIUM finding.'})
        for i, f in records['findings']:
            if not set(f['claim_ids']) <= registry:
                issues.append({'path': f'$.findings[{i}].claim_ids', 'code': 'UNKNOWN_CLAIM',
                               'message': 'Finding references a claim outside the frozen registry.'})
    # Exact schema diagnostics cover every independent malformed record. Semantic
    # failures use a closed message, never source/response text.
    return {'schema_diagnostics': structural, 'semantic_diagnostics': {
        'violations': issues[:100], 'total_violations': len(issues), 'truncated': len(issues) > 100}}

def transport_json(text):
    try:
        return strict_json(text)
    except ContractError as exc:
        raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid transport JSON.', **exc.details) from exc
