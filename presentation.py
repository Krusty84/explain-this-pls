# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Deterministic English tables, separate from canonical narrative bytes."""
import html
import unicodedata
from contracts import is_recovered_material, validate_schema
from document_rendering import validate_materialized
from saved_contracts import SAVED_SCHEMAS, SAVED_FOLDER_SCHEMAS

LABELS = {
    'SUPPORTED': 'Supported according to the reviewing agent',
    'CONTRADICTED': 'Agent reported a contradiction',
    'UNVERIFIABLE': 'Insufficient evidence according to the agent',
    'NOT_CHECKED': 'Not assessed by the agent',
    'MISSING': 'Agent response missing',
    'CAVEAT_ACCEPTABLE': 'Caveat acceptable according to the agent',
    'CAVEAT_INADEQUATE': 'Agent considers the caveat inadequate',
    'PASS': 'No material issues reported for the required claim registry; policy checks satisfied',
    'CHANGES_REQUIRED': 'Material issues reported by the agent',
    'INCONCLUSIVE': 'No positive policy result; see check results',
    'CONFIRMED_DIFFERENCE': 'Difference supported by supplied reports according to the agent',
    'REPORTED_UNVERIFIED': 'Reported by the agent; unverified',
    'INSUFFICIENT_EVIDENCE': 'Insufficient comparison evidence',
}


def russian(language):
    return isinstance(language, str) and language.lower() in ('ru', 'russian', 'русский')


def cell(value):
    text = ''.join('\\u%04x' % ord(c) if unicodedata.category(c).startswith('C') and c not in '\n\r\t' else c for c in str(value))
    return html.escape(text).replace('|', '&#124;').replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')


def label(value, language='English'):
    return LABELS.get(value, value)


def render_coverage(plan, reports=(), checks=None):
    if not plan:
        return ''
    counts = plan['counts']
    out = ['## Source distribution — program reconciliation', '',
           'Catalog: ' + cell(plan['origin']) + ' / ' + cell(plan['catalog_status']), '',
           '; '.join(f'{key}: {counts[key]}' for key in
                     ('files', 'assigned_files', 'excluded_files', 'unclassified_files', 'overlapping_files', 'symlinks',
                      'directories', 'empty_directories')), '',
           'Totals count unique files. Areas may overlap. This does not measure understanding.', '',
           '| Area | Purpose | Paths | Files | Symbolic links |', '| --- | --- | --- | --- | --- |']
    links = set()
    for area in plan['areas']:
        symlinks = set(area['entry_paths']) - set(area['file_paths'])
        links.update(symlinks)
        out += ['| ' + ' | '.join(cell(x) for x in (area['id'] + ': ' + area['name'], area['purpose'],
                  ', '.join(area['paths']), len(area['file_paths']), len(symlinks))) + ' |']
    if plan['exclusions']:
        out += ['', 'Explicit exclusions:', '']
        for exclusion in plan['exclusions']:
            links.update(set(exclusion['entry_paths']) - set(exclusion['file_paths']))
            out += ['- ' + cell(exclusion['path']) + ': ' + cell(exclusion['reason'])]
    if links:
        out += ['', 'Symbolic links (targets were not followed): ' + cell(', '.join(sorted(links)))]
    out += ['', *('- ' + cell(x) for x in plan['limitations']), '',
            '## Area inspection — agent reports', '',
            'INSPECTED requires a resolved source pointer in the area; it does not establish exhaustive investigation.', '',
            '| Area | Reported status | Evidence | Limitation |', '| --- | --- | --- | --- |']
    by_id = {r['area_id']: r for r in reports}
    for area in plan['areas']:
        record = by_id.get(area['id'], {})
        limitation = record.get('limitation', '')
        if area['id'] in (checks or {}).get('unsupported_ids', []):
            limitation += ' No resolved evidence within this area.'
        out += ['| ' + ' | '.join(cell(x) for x in (area['id'], record.get('status', 'MISSING'),
                  ', '.join(record.get('evidence_ids', [])), limitation)) + ' |']
    return '\n'.join(out) + '\n'


def validate_report(stage, data):
    if is_recovered_material(stage, data):
        return
    schemas = SAVED_FOLDER_SCHEMAS if type(data) is dict and 'source_directory' in data else SAVED_SCHEMAS
    validate_schema(data, schemas[stage])
    if stage == 'study':
        validate_materialized(data)


def render_stage(stage, data, language='English'):
    validate_report(stage, data)
    if is_recovered_material(stage, data):
        return '> Text retained after contract rejection; full policy checks are not complete.\n\n'
    checks = data['program_checks']
    out = ['# Program checks and agent assessments', '',
        ('Study processing conditions satisfied; final acceptance depends on review.' if stage == 'study' else
         'Policy checks satisfied; factual correctness is not established.') if checks['policy_satisfied'] else
        'Policy checks not satisfied. Review is incomplete or has limitations/issues.', '',
        'Semantic review quality is not measured. Resolving a locator does not validate the conclusion.', '']
    out += ['- ' + cell(x) for x in data.get('limitations', [])]
    if stage == 'study':
        out += ['Study completion reported by the agent: ' + data['completion_status'], '']
        out += [render_coverage(data.get('review_plan', {}).get('coverage_plan'), data.get('coverage', []),
                               checks.get('coverage')), '']
    elif stage == 'review':
        out += [label(data['verdict'], language), '']
        c = checks['registry_coverage']
        out += ['Frozen registry coverage (not all facts in the document): ' +
                f"{c['assessed_count']}/{c['expected_count']}; " +
                'responses: ' + str(c['received_count']) + '; ' +
                'missing: ' + cell(', '.join(c['missing_ids']) or '—'), '']
        out += ['Document links: ' +
                str(checks['document_links']['matched']) + '/' + str(checks['document_links']['registered']) +
                ' registered claims.', '']
        out += ['; '.join(cell(label(outcome, language)) + ': ' + str(count)
                         for outcome, count in checks['outcome_counts'].items()), '']
    else:
        out += ['Comparison uses supplied reports only. Sources were not inspected in this stage.', '',
                '| ID | Branch | Agent assessment | Rationale |',
                '| --- | --- | --- | --- |']
        for d in data['differences']:
            out.append('| ' + ' | '.join(cell(v) for v in (d['id'], d['branch'], label(d['classification'], language), d['explanation'])) + ' |')
    if stage in ('study', 'review'):
        registry = data['claims'] if stage == 'study' else data['claim_registry']
        replies = {c['id']: c for c in data['claims']} if stage == 'review' else {}
        out += ['', '| ID | Document location | Statement and scope | Author classification | Agent assessment | Cited evidence | Findings and limitations |',
                '| --- | --- | --- | --- | --- | --- | --- |']
        findings = {f['id']: f for f in data.get('findings', [])}
        for claim in registry:
            reply = replies.get(claim['id'], {})
            linked = checks.get('finding_ids_by_claim', {}).get(claim['id'], [])
            issues = '; '.join(fid + ': ' + findings[fid]['impact'] for fid in linked)
            locations = claim['document_locators']
            evidence_refs = claim['evidence_ids'] + reply.get('evidence_ids', [])
            out.append('| ' + ' | '.join(cell(v) for v in (claim['id'],
                ', '.join(f"L{location['start_line']}-L{location['end_line']}" for location in locations),
                claim['statement'] + ' / ' + claim['scope'],
                claim['epistemic_kind'], label(reply.get('outcome', 'MISSING'), language) if stage == 'review' else
                'Author assessment; review assessments are shown separately',
                ', '.join(evidence_refs), issues + ' ' + reply.get('limitation', '') + ' ' + claim['uncertainty'])) + ' |')
        out += ['', 'Source state matched at checked boundaries. This is not continuous immutability.', '',
                'Locators were resolved by the orchestrator; this is not a record of agent file reads.', '',
                '| ID | Source | Path : lines | Locator status | Encoding |',
                '| --- | --- | --- | --- | --- |']
        for e in checks['evidence']:
            out.append('| ' + ' | '.join(cell(v) for v in (e['id'], e['source_id'],
                f"{e['path']}:{e['start_line']}-{e['end_line']}", e['status'], e.get('encoding'))) + ' |')
        if stage == 'review':
            out += ['', '## Omission search — agent reports', '',
                    '| Area | Status | Limitation |', '| --- | --- | --- |']
            searches = {a['area_id']: a for a in data['omission_search']}
            for area in data['review_plan']['omission_areas']:
                a = searches.get(area['id'], {})
                out.append('| ' + ' | '.join(cell(v) for v in (area['id'] + ': ' + area['scope'],
                    a.get('status', label('MISSING', language)), a.get('limitation', ''))) + ' |')
            out += ['', '## Agent findings', '']
            for f in data['findings']:
                out += ['- ' + cell(f['id'] + ' / ' + f['severity'] + ': ' + f['impact'] + ' ' + f['proposed_correction'])]
            expected = data.get('review_plan', {}).get('prior_findings', [])
            if expected:
                replies = {(f['revision_id'], f['finding_id']): f for f in data.get('prior_findings', [])}
                out += ['', '## Previous material findings — reviewer assessment', '',
                        '| Revision / finding | Status | Explanation |', '| --- | --- | --- |']
                for finding in expected:
                    key = (finding['revision_id'], finding['finding_id'])
                    reply = replies.get(key, {})
                    out += ['| ' + ' | '.join(cell(x) for x in (' / '.join(key), reply.get('status', 'MISSING'),
                              reply.get('explanation', 'Required response missing.'))) + ' |']
    return '\n'.join(out).rstrip() + '\n'
