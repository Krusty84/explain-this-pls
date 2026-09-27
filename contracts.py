# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Output schemas and deterministic semantic gates (stdlib only)."""
from __future__ import annotations
import json
import re
from typing import Any

class ContractError(ValueError):
    pass

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
BASE = dict(schema_version=string('2.0'), completion_status=STATUS,
            report_markdown=string(), limitations=STRINGS)

SCHEMAS = {
    'document': obj(**BASE, task=string('architecture_documentation'),
                    branch=string(), source_commit=string()),
    'review': obj(**BASE, task=string('architecture_review'),
        branch=string(), source_commit=string(),
        verdict=string('PASS', 'CHANGES_REQUIRED', 'INCONCLUSIVE'),
        claim_inventory_complete={'type': 'boolean'},
        claims=array(obj(id=string(), location=string(), statement=string(),
            outcome=string('SUPPORTED', 'CONTRADICTED', 'UNVERIFIABLE', 'NOT_CHECKED'),
            evidence=STRINGS, limitation=string(), finding_ids=STRINGS)),
        findings=array(obj(id=string(), severity=string('HIGH', 'MEDIUM', 'LOW'),
            type=string('FACTUAL_ERROR', 'UNSUPPORTED_ASSERTION', 'MATERIAL_OMISSION',
                        'SCOPE_MISMATCH', 'CONTRACT_VIOLATION'),
            claim_ids=STRINGS, location=string(), evidence=STRINGS,
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

def strict_json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    def reject(value):
        raise ContractError(f'Non-finite JSON value: {value}')
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=reject)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ContractError(f'Invalid JSON: {exc}') from exc

def validate_schema(value: Any, schema: dict, where: str = '$') -> None:
    types = {'object': dict, 'array': list, 'string': str, 'boolean': bool}
    typ = schema['type']
    if type(value) is not types[typ]:
        raise ContractError(f'{where}: expected {typ}')
    if 'enum' in schema and value not in schema['enum']:
        raise ContractError(f'{where}: unexpected enum value {value!r}')
    if typ == 'object':
        if set(value) != set(schema['properties']):
            raise ContractError(f'{where}: missing/extra keys: '
                                f'{set(value) ^ set(schema["properties"])}')
        for key, subschema in schema['properties'].items():
            validate_schema(value[key], subschema, f'{where}.{key}')
    elif typ == 'array':
        for i, item in enumerate(value):
            validate_schema(item, schema['items'], f'{where}[{i}]')

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
    doc, rev = item.get('document'), item.get('review')
    return bool(doc and rev and doc['completion_status'] == 'COMPLETE'
                and rev['completion_status'] == 'COMPLETE' and rev['verdict'] == 'PASS')

def validate_result(stage: str, value: dict, context: dict) -> None:
    validate_schema(value, SCHEMAS[stage])
    if not value['report_markdown'].strip():
        raise ContractError('Empty Markdown report')
    if value['completion_status'] != 'COMPLETE' and not value['limitations']:
        raise ContractError('PARTIAL/BLOCKED requires explicit limitations')
    if stage in ('document', 'review'):
        for key in ('branch', 'source_commit'):
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
        missing = {b['branch'] for b in context['branches'] if not accepted(b)}
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
            if diff['id'] not in value['report_markdown']:
                raise ContractError('Markdown omits a structured difference ID')

def parse_backend(backend: str, output: str) -> tuple[dict, dict]:
    """Normalize only documented transports. Never extract JSON with a greedy regex."""
    if backend == 'codex':
        return strict_json(output.strip()), {}
    if backend == 'claude-code':
        transport = strict_json(output.strip())
        if type(transport) is not dict or transport.get('is_error'):
            raise ContractError('Claude Code returned an error result')
        if type(transport.get('structured_output')) is not dict:
            raise ContractError('Claude Code did not return structured_output')
        metadata = {k: transport[k] for k in ('session_id', 'total_cost_usd', 'usage', 'modelUsage') if k in transport}
        return transport['structured_output'], metadata
    if backend == 'opencode':
        messages: dict[str, dict[str, str]] = {}
        stops: list[str] = []
        session_ids = set()
        for line in output.splitlines():
            if not line.strip():
                continue
            event = strict_json(line)
            if type(event) is not dict:
                raise ContractError('OpenCode emitted a non-object event')
            if event.get('type') == 'error':
                raise ContractError('OpenCode emitted an error event')
            if event.get('sessionID'):
                session_ids.add(event['sessionID'])
            part = event.get('part', {})
            mid = part.get('messageID')
            if event.get('type') == 'text':
                if not mid or not part.get('id') or not isinstance(part.get('text'), str):
                    raise ContractError('Unrecognized OpenCode text event layout')
                messages.setdefault(mid, {})[part['id']] = part['text']
            if event.get('type') == 'step_finish' and part.get('reason') == 'stop':
                if not mid:
                    raise ContractError('OpenCode stop event has no messageID')
                stops.append(mid)
        if not stops or stops[-1] not in messages:
            raise ContractError('No completed final OpenCode assistant message; output may be truncated')
        text = '\n'.join(messages[stops[-1]].values())
        return strict_json(text.strip()), {'session_ids': sorted(session_ids)}
    raise ContractError(f'Unknown backend: {backend}')
