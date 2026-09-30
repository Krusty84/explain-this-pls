# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Output schemas and deterministic semantic gates (stdlib only)."""
from __future__ import annotations
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

STRINGS = array(string())
STATUS = string('COMPLETE', 'PARTIAL', 'BLOCKED')
BASE = dict(completion_status=STATUS,
            report_markdown=string(), limitations=STRINGS)

SCHEMAS = {
    'study': obj(**BASE, task=string('architecture_documentation'),
                    branch=string(), source_commit=string()),
    'review': obj(**BASE, task=string('architecture_review'),
        branch=string(), source_commit=string(),
        verdict=string('PASS', 'CHANGES_REQUIRED', 'INCONCLUSIVE'),
        claim_inventory_complete={'type': 'boolean'},
        claims=array(obj(id=string(), location=string(), statement=string(),
            outcome=string('SUPPORTED', 'CONTRADICTED', 'UNVERIFIABLE', 'NOT_CHECKED'),
            evidence=STRINGS, limitation={**string(), 'description':
                'Required. Use an empty string when no limitation applies; unchecked/unverifiable claims require a reason.'},
            finding_ids={**STRINGS, 'description':
                'Required links to findings (F-...). Use [] when none. Never put claim_ids in a claim.'})),
        findings=array(obj(id=string(), severity=string('HIGH', 'MEDIUM', 'LOW'),
            type=string('FACTUAL_ERROR', 'UNSUPPORTED_ASSERTION', 'MATERIAL_OMISSION',
                        'SCOPE_MISMATCH', 'CONTRACT_VIOLATION'),
            claim_ids={**STRINGS, 'description':
                'Required links to claims (C-...). Use [] when no existing claim applies. Only findings have claim_ids.'}, location=string(), evidence=STRINGS,
            impact=string(), proposed_correction=string()))),
    'compare': obj(**BASE, task=string('architecture_comparison'),
        baseline_branch=string(), baseline_commit=string(),
        compared_branches=STRINGS, unresolved_branches=STRINGS,
        differences=array(obj(id=string(), branch=string(), category=string(),
            classification=string('CONFIRMED_DIFFERENCE', 'REPORTED_UNVERIFIED',
                                  'INSUFFICIENT_EVIDENCE'),
            baseline_statement=string(), branch_statement=string(),
            evidence_refs=STRINGS, explanation=string())))
}

FOLDER_SCHEMAS = {
    stage: obj(**({key: spec for key, spec in SCHEMAS[stage]['properties'].items()
                  if key not in ('branch', 'source_commit')} |
                 {'source_directory': string(),
                  'source_fingerprint': string()}))
    for stage in ('study', 'review')
}

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
    types = {'object': dict, 'array': list, 'string': str, 'boolean': bool}
    violations, total = [], 0
    def add(path, kind, **detail):
        nonlocal total
        total += 1
        if len(violations) < limit:
            violations.append({'path': path, 'violation': kind,
                               'missing_keys': [], 'extra_key_count': 0, **detail})
    def visit(data, spec, path):
        typ = spec['type']
        if type(data) is not types[typ]:
            add(path, 'type', expected_type=typ)
            return
        if 'enum' in spec and data not in spec['enum']:
            add(path, 'enum')
        if typ == 'object':
            missing = [key for key in spec['required'] if key not in data]
            extra = [key for key in data if key not in spec['properties']]
            if missing or extra:
                add(path, 'required/additionalProperties', missing_keys=missing,
                    extra_key_count=len(extra), **({'extra_keys': extra} if private else {}))
            for key, child in spec['properties'].items():
                if key in data:
                    visit(data[key], child, f'{path}.{key}')
        elif typ == 'array':
            for i, item in enumerate(data):
                visit(item, spec['items'], f'{path}[{i}]')
    visit(value, schema, where)
    return {'violations': violations, 'total_violations': total, 'truncated': total > len(violations)}


def validate_schema(value: Any, schema: dict, where: str = '$') -> None:
    details = schema_diagnostics(value, schema, where)
    if details['total_violations']:
        first = details['violations'][0]
        raise response_error('SCHEMA_ERROR', 'schema',
            f"Structured result has {details['total_violations']} schema violation(s); first at {first['path']}.",
            **first, **details)

def review_verdict(value: dict) -> str:
    if any(f['severity'] in ('HIGH', 'MEDIUM') for f in value['findings']):
        return 'CHANGES_REQUIRED'
    return 'PASS' if value['completion_status'] == 'COMPLETE' else 'INCONCLUSIVE'

def unique_ids(records: list[dict], pattern: str) -> set[str]:
    ids = [r['id'] for r in records]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(pattern, i) for i in ids):
        raise ContractError('Invalid or duplicate record IDs')
    return set(ids)

def accepted(item: dict) -> bool:
    doc, rev = item.get('study'), item.get('review')
    return bool(doc and rev and doc['completion_status'] == 'COMPLETE'
                and rev['completion_status'] == 'COMPLETE' and rev['verdict'] == 'PASS')

def validate_result(stage: str, value: dict, context: dict, mode: str = 'git') -> None:
    validate_schema(value, (FOLDER_SCHEMAS if mode == 'folder' else SCHEMAS)[stage])
    if not value['report_markdown'].strip():
        raise ContractError('Empty Markdown report')
    if value['completion_status'] != 'COMPLETE' and not value['limitations']:
        raise ContractError('PARTIAL/BLOCKED requires explicit limitations')
    if stage in ('study', 'review'):
        identity = ('source_directory', 'source_fingerprint') if mode == 'folder' else ('branch', 'source_commit')
        for key in identity:
            if value[key] != context[key]:
                raise ContractError(f'{key} does not match the pinned input')
    if stage == 'review':
        cids = unique_ids(value['claims'], r'C-[0-9]{3,}')
        fids = unique_ids(value['findings'], r'F-[0-9]{3,}')
        fs = {f['id']: f for f in value['findings']}
        if value['completion_status'] == 'COMPLETE':
            if not value['claim_inventory_complete'] or not value['claims']:
                raise ContractError('COMPLETE review requires a nonempty complete claim inventory')
            if any(c['outcome'] == 'NOT_CHECKED' for c in value['claims']):
                raise ContractError('COMPLETE review contains NOT_CHECKED claims')
        for c in value['claims']:
            if not set(c['finding_ids']) <= fids:
                raise ContractError('Unknown finding reference in claim ledger')
            if c['outcome'] in ('SUPPORTED', 'CONTRADICTED') and not c['evidence']:
                raise ContractError('Supported/contradicted claims require evidence')
            if c['outcome'] in ('UNVERIFIABLE', 'NOT_CHECKED') and not c['limitation'].strip():
                raise ContractError('Unverified/unchecked claim lacks a reason')
            if c['outcome'] in ('CONTRADICTED', 'UNVERIFIABLE'):
                if not any(fs[f]['severity'] in ('HIGH', 'MEDIUM') for f in c['finding_ids']):
                    raise ContractError('Material contradicted/unverifiable assertion lacks a material finding')
        for f in value['findings']:
            if not set(f['claim_ids']) <= cids:
                raise ContractError('Unknown claim ID in finding')
            if not f['evidence'] or not f['proposed_correction'].strip():
                raise ContractError('Finding requires evidence and a concrete correction')
        for record in value['claims'] + value['findings']:
            if record['id'] not in value['report_markdown']:
                raise ContractError('Markdown report omits a structured ledger/finding ID')
        if value['verdict'] != review_verdict(value):
            raise ContractError('Review verdict violates deterministic verdict rules')
    if stage == 'compare':
        expected = [b for b in context['requested_branches'] if b != context['baseline_branch']]
        if value['baseline_branch'] != context['baseline_branch'] or value['baseline_commit'] != context['baseline_commit']:
            raise ContractError('Comparison baseline mismatch')
        if sorted(value['compared_branches']) != sorted(expected):
            raise ContractError('Comparison must cover every non-baseline branch exactly once')
        if len(value['unresolved_branches']) != len(set(value['unresolved_branches'])) or not set(value['unresolved_branches']) <= set(context['requested_branches']):
            raise ContractError('Invalid unresolved branches')
        entries = {b['branch']: b for b in context['branches']}
        missing = {b for b in context['requested_branches'] if not accepted(entries.get(b, {}))}
        if not missing <= set(value['unresolved_branches']):
            raise ContractError('Comparison conceals unaccepted/missing branch inputs')
        if value['completion_status'] == 'COMPLETE' and value['unresolved_branches']:
            raise ContractError('COMPLETE comparison contains unresolved branches')
        unique_ids(value['differences'], r'D-[0-9]{3,}')
        for diff in value['differences']:
            if diff['branch'] not in expected:
                raise ContractError('Difference references an unexpected branch')
            if diff['classification'] == 'CONFIRMED_DIFFERENCE' and len(diff['evidence_refs']) < 2:
                raise ContractError('Confirmed contrast needs references for both sides')
            if (context.get('result_policy') == 'compromise'
                    and diff['classification'] == 'CONFIRMED_DIFFERENCE'
                    and (context['baseline_branch'] in missing or diff['branch'] in missing)):
                raise ContractError('Confirmed contrast requires accepted inputs on both sides')
            if diff['id'] not in value['report_markdown']:
                raise ContractError('Markdown omits a structured difference ID')


def result_diagnostics(stage: str, value: Any, context: dict, mode: str = 'git') -> dict:
    """All independently checkable defects; public paths/messages contain no model text.

    Partial or malformed ledgers are not evidence. Inspect only schema-valid records,
    retaining their original indexes, so one malformed record does not hide others.
    The strict validator remains the authority for acceptance.
    """
    schema = (FOLDER_SCHEMAS if mode == 'folder' else SCHEMAS)[stage]
    structural = schema_diagnostics(value, schema)
    issues, total = [], 0

    def add(path, code, message):
        nonlocal total
        total += 1
        if len(issues) < 100:
            issues.append({'path': path, 'code': code, 'message': message})

    if type(value) is dict:
        report = value.get('report_markdown')
        if type(report) is str and not report.strip():
            add('$.report_markdown', 'EMPTY_REPORT', 'Empty Markdown report.')
        if value.get('completion_status') in ('PARTIAL', 'BLOCKED') and value.get('limitations') == []:
            add('$.limitations', 'MISSING_LIMITATIONS', 'PARTIAL/BLOCKED requires explicit limitations.')
        identity = ('source_directory', 'source_fingerprint') if mode == 'folder' else ('branch', 'source_commit')
        if stage in ('study', 'review'):
            for key in identity:
                if value.get(key) != context.get(key):
                    add('$.' + key, 'IDENTITY_MISMATCH', 'Identity does not match the pinned input.')
        if stage == 'review':
            records = {}
            for key, prefix in (('claims', 'C'), ('findings', 'F')):
                items = value.get(key)
                records[key] = []
                if type(items) is not list:
                    continue
                seen = set()
                for i, record in enumerate(items):
                    if schema_diagnostics(record, schema['properties'][key]['items'], limit=0)['total_violations']:
                        continue
                    records[key].append((i, record))
                    if not re.fullmatch(prefix + r'-[0-9]{3,}', record['id']) or record['id'] in seen:
                        add(f'$.{key}[{i}].id', 'INVALID_ID', 'Invalid or duplicate record ID.')
                    seen.add(record['id'])
                    if type(report) is str and record['id'] not in report:
                        add(f'$.{key}[{i}].id', 'MISSING_REPORT_ID', 'Markdown omits a structured record ID.')
            claims, findings = records['claims'], records['findings']
            cids = {c['id'] for _, c in claims}
            fs = {f['id']: f for _, f in findings}
            if value.get('completion_status') == 'COMPLETE':
                if value.get('claim_inventory_complete') is not True or not value.get('claims'):
                    add('$.claims', 'INCOMPLETE_INVENTORY', 'COMPLETE review requires a nonempty complete inventory.')
            for i, c in claims:
                path = f'$.claims[{i}]'
                if not set(c['finding_ids']) <= fs.keys():
                    add(path + '.finding_ids', 'UNKNOWN_FINDING', 'Unknown or malformed finding reference.')
                if c['outcome'] in ('SUPPORTED', 'CONTRADICTED') and not c['evidence']:
                    add(path + '.evidence', 'MISSING_EVIDENCE', 'Supported/contradicted claims require evidence.')
                if c['outcome'] in ('UNVERIFIABLE', 'NOT_CHECKED') and not c['limitation'].strip():
                    add(path + '.limitation', 'MISSING_REASON', 'Unverified/unchecked claim requires a reason.')
                if c['outcome'] == 'NOT_CHECKED' and value.get('completion_status') == 'COMPLETE':
                    add(path + '.outcome', 'UNCHECKED_CLAIM', 'COMPLETE review contains an unchecked claim.')
                if c['outcome'] in ('CONTRADICTED', 'UNVERIFIABLE') and not any(
                        fs.get(fid, {}).get('severity') in ('HIGH', 'MEDIUM') for fid in c['finding_ids']):
                    add(path + '.finding_ids', 'MISSING_MATERIAL_FINDING',
                        'Contradicted/unverifiable claim lacks a linked HIGH/MEDIUM finding.')
            for i, f in findings:
                if not set(f['claim_ids']) <= cids:
                    add(f'$.findings[{i}].claim_ids', 'UNKNOWN_CLAIM', 'Unknown or malformed claim reference.')
                if not f['evidence'] or not f['proposed_correction'].strip():
                    add(f'$.findings[{i}]', 'INCOMPLETE_FINDING', 'Finding requires evidence and a concrete correction.')
            if (type(value.get('findings')) is list and len(findings) == len(value['findings'])
                    and value.get('completion_status') in ('COMPLETE', 'PARTIAL', 'BLOCKED')
                    and value.get('verdict') != review_verdict(value)):
                add('$.verdict', 'INCONSISTENT_VERDICT', 'Verdict violates deterministic verdict rules.')
    return {'schema_diagnostics': structural,
            'semantic_diagnostics': {'violations': issues, 'total_violations': total, 'truncated': total > len(issues)}}

def transport_json(text):
    try:
        return strict_json(text)
    except ContractError as exc:
        raise response_error('TRANSPORT_ERROR', 'transport', 'Invalid transport JSON.', **exc.details) from exc


def parse_backend(backend: str, output: str) -> tuple[dict, dict]:
    """Normalize only documented transports. Never extract JSON with a greedy regex."""
    if backend == 'codex':
        from codex import parse_output
        return parse_output(output)
    if backend == 'claude-code':
        from claude_code import parse_output
        return parse_output(output)
    if backend == 'opencode':
        raise response_error('BACKEND_INCOMPATIBLE', 'compatibility',
                             'OpenCode text event parsing was removed; native HTTP structured output is required.')
    raise ContractError(f'Unknown backend: {backend}')
