# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Deterministic English tables, separate from canonical narrative bytes."""
import html
import unicodedata
from src.contracts.contracts import is_recovered_material, validate_schema
from src.reports.document_rendering import validate_materialized
from src.contracts.saved_contracts import SAVED_SCHEMAS, SAVED_FOLDER_SCHEMAS

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


def render_citation_warnings(data, language='English'):
    warnings = data.get('program_checks', {}).get('citation_warnings', [])
    if not warnings:
        return ''
    ru = russian(language)
    out = ['## ' + ('Предупреждения о цитатах — проверка программы' if ru else 'Citation warnings — program checks'), '',
           ('Указанные утверждения требуют проверки цитат. Исходные метки FACT сохранены; '
            'они не означают, что эти цитаты подтверждают утверждения.' if ru else
            'The listed claims require citation checks. Original FACT labels remain unchanged; '
            'they do not mean that these citations establish the claims.'), '',
           '| ' + ('Доказательство | Источник | Путь : строки | Исходные строки | Причина | Действие | Затронутые утверждения' if ru else
                    'Evidence | Source | Path : lines | Original lines | Reason | Action | Affected claims') + ' |',
           '| --- | --- | --- | --- | --- | --- | --- |']
    for warning in sorted(warnings, key=lambda w: (w.get('branch', ''), w['evidence_id'], w['source_id'], w['path'],
                                                   w['start_line'], w['end_line'], w['reason'], w['action'],
                                                   tuple(sorted(w['claim_ids'])), w.get('original_start_line', 0),
                                                   w.get('original_end_line', 0))):
        original = (f"{warning['original_start_line']}-{warning['original_end_line']}"
                    if 'original_start_line' in warning and 'original_end_line' in warning else '—')
        out.append('| ' + ' | '.join(cell(value) for value in (
            warning['evidence_id'], (warning['branch'] + ' / ' if warning.get('branch') else '') + warning['source_id'],
            f"{warning['path']}:{warning['start_line']}-{warning['end_line']}", original,
            warning['reason'], warning['action'], ', '.join(sorted(warning['claim_ids'])) or '—')) + ' |')
    out += ['', ('UNRESOLVED: цитата не подтверждена. CORRECTED: диапазон или общий отступ исправлен; '
                 'исходная ошибка сохранена в предупреждении. Продолжение обработки не означает строгую приёмку.' if ru else
                 'UNRESOLVED: the citation was not verified. CORRECTED: the range or common indentation was repaired; '
                 'the original error remains recorded. Continued processing does not imply strict acceptance.')]
    return '\n'.join(out) + '\n'


def render_architecture(data, language='English'):
    """Append checks without changing authored bytes or existing line coordinates."""
    appendix = render_citation_warnings(data, language)
    return data['report_markdown'] + ('\n\n' + appendix if appendix else '')


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
            'INSPECTED requires resolved source evidence or verified metadata for an empty-file scope; '
            'it does not establish exhaustive investigation.', '',
            '| Area | Reported status | Evidence | Limitation |', '| --- | --- | --- | --- |']
    by_id = {r['area_id']: r for r in reports}
    for area in plan['areas']:
        record = by_id.get(area['id'], {})
        limitation = record.get('limitation', '')
        if area['id'] in (checks or {}).get('unsupported_ids', []):
            limitation += ' No resolved evidence within this area.'
        empty_paths = set((checks or {}).get('metadata_verified_empty_paths', [])) & set(area['file_paths'])
        if empty_paths:
            limitation += ' Empty files verified from pinned metadata: ' + ', '.join(sorted(empty_paths)) + '.'
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
    if stage == 'catalog':
        out = ['# Subsystem catalog', '',
               'Catalog completion reported by the agent: ' + cell(data['program_checks']['completion_self_assessment']), '',
               'Catalog: ' + cell(data['coverage_plan']['origin']) + ' / ' + data['coverage_plan']['catalog_status'], '',
               '## Subsystems', '', '| ID | Subsystem | Purpose | Paths |', '| --- | --- | --- | --- |']
        for subsystem in data['subsystems']:
            out.append('| ' + ' | '.join(cell(v) for v in (subsystem['id'], subsystem['name'],
                subsystem['purpose'], ', '.join(subsystem['paths']))) + ' |')
        out += ['', '## Exclusions', '', '| Path | Reason |', '| --- | --- |']
        for exclusion in data['exclusions']:
            out.append('| ' + cell(exclusion['path']) + ' | ' + cell(exclusion['reason']) + ' |')
        for title, values in (('Limitations', data['coverage_plan']['limitations']),
                              ('Unclassified paths', data['coverage_plan']['unclassified_paths'])):
            out += ['', '## ' + title, '', *(['- ' + cell(v) for v in values] or ['None.'])]
        return '\n'.join(out) + '\n'
    checks = data['program_checks']
    out = ['# Program checks and agent assessments', '',
        ('Study processing conditions satisfied; final acceptance depends on review.' if stage == 'study' else
         'Policy checks satisfied; factual correctness is not established.') if checks['policy_satisfied'] else
        'Policy checks not satisfied. Review may be incomplete or have limitations/issues.', '',
        'Semantic review quality is not measured. Resolving a locator does not validate the conclusion.', '']
    out += ['- ' + cell(x) for x in data.get('limitations', [])]
    if checks.get('citation_warnings'):
        out += ['Continued processing allowed: ' + str(checks.get('continuation_allowed', checks['policy_satisfied'])) +
                '; strict policy checks satisfied: ' + str(checks['policy_satisfied']) + '.', '']
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
            warned = [w['evidence_id'] + ' (' + w['action'] + ')' for w in checks.get('citation_warnings', [])
                      if claim['id'] in w['claim_ids']]
            if warned:
                issues += ' Citation checks required: ' + ', '.join(sorted(warned)) + '.'
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
    warnings = render_citation_warnings(data, language)
    if warnings:
        out += ['', warnings]
    return '\n'.join(out).rstrip() + '\n'
