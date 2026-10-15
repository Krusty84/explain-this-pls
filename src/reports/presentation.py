# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Deterministic presentation, separate from canonical narrative bytes."""
import html
import json
import unicodedata
from src.contracts.contracts import SECTION_KEYS, contract_violation, is_recovered_material, validate_schema
from src.reports.architecture_format import section_headings
from src.reports.document_rendering import validate_materialized, inspect_materialized_diagrams
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
    return isinstance(language, str) and language.strip().lower() in ('ru', 'russian', 'русский')


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


def _code(value):
    """Keep untrusted paths literal in Markdown, inline HTML and table cells."""
    text = ''.join('\\u%04x' % ord(c) if ord(c) < 32 or ord(c) == 127 else c for c in str(value))
    return '<code>' + ''.join(f'&#{ord(c)};' if c in '\\`*_[]()!#~|' else html.escape(c) for c in text) + '</code>'


def _lookup(records, identifier, path):
    if identifier not in records:
        raise contract_violation('UNKNOWN_REFERENCE', path)
    return records[identifier]


def _source_path(evidence, resolution, sources):
    source = sources.get(evidence['source_id'])
    if source is None:
        if resolution['status'] != 'UNKNOWN_SOURCE':
            raise contract_violation('UNKNOWN_REFERENCE', '$.review_plan.sources')
        return evidence['path']
    root = source['root']
    return evidence['path'] if root == '.' else root + '/' + evidence['path']


def _status(evidence, resolution, language):
    status = resolution['status']
    ru = russian(language)
    if status == 'RESOLVED':
        detail = ('локатор разрешён; цитата совпала' if ru else 'locator resolved; quote matched') if evidence['quote'] else (
            'локатор разрешён; цитата не задана' if ru else 'locator resolved; no quote supplied')
    elif status == 'QUOTE_MISMATCH':
        detail = 'цитата не подтверждена' if ru else 'quote not verified'
    elif status == 'NOT_FOUND':
        detail = 'исходный файл не найден' if ru else 'source file not found'
    else:
        detail = 'локатор не разрешён' if ru else 'locator not resolved'
    return status + '; ' + detail


def render_source_references(block, claims, evidence, resolutions, sources, language='English'):
    """Follow canonical IDs in first-occurrence order; never inspect authored prose."""
    refs, limitations, seen = [], [], set()
    for claim_id in block['claim_ids']:
        claim = _lookup(claims, claim_id, '$.block_map.claim_ids')
        if not claim['evidence_ids']:
            limitations.append(_code(claim_id) + ' (' + claim['epistemic_kind'] + ')')
        for identifier in claim['evidence_ids']:
            if identifier in seen:
                continue
            seen.add(identifier)
            entry = _lookup(evidence, identifier, '$.claims.evidence_ids')
            resolution = _lookup(resolutions, identifier, '$.program_checks.evidence')
            path = _source_path(entry, resolution, sources)
            reference = (_code(f"{path}:{entry['start_line']}–{entry['end_line']}") +
                         f" [{entry['id']}](#source-{entry['id'].lower()})")
            if resolution['status'] != 'RESOLVED':
                reference += ' (' + _status(entry, resolution, language) + ')'
            refs.append(reference)
    out = []
    if refs:
        out.append('*' + ('Источники: ' if russian(language) else 'Sources: ') + ', '.join(refs) + '*')
    if limitations:
        out.append('*' + ('Ограничение: нет исходных доказательств для ' if russian(language) else
                           'Limitation: no source evidence for ') + ', '.join(limitations) + '.*')
    return '\n\n'.join(out)


def render_evidence_index(data, language='English'):
    """Render locator outcomes and pinned identities without exposing source quotes."""
    ru = russian(language)
    resolutions = {entry['id']: entry for entry in data['program_checks']['evidence']}
    sources = {entry['id']: entry for entry in data['review_plan']['sources']}
    out = ['### ' + ('Реестр источников' if ru else 'Source registry'), '',
           '| ' + ('Доказательство | Источник | Файл | Начальная строка | Конечная строка | Статус локатора | Идентичность источника' if ru else
                    'Evidence | Source | File | Start line | End line | Locator status | Source identity') + ' |',
           '| --- | --- | --- | --- | --- | --- | --- |']
    for entry in data['evidence']:
        resolution = _lookup(resolutions, 'study:' + entry['id'], '$.program_checks.evidence')
        path = _source_path(entry, resolution, sources)
        source = sources.get(entry['source_id'])
        identity = json.dumps(source['identity'], ensure_ascii=False, sort_keys=True, separators=(',', ':')) if source else (
            'Источник отсутствует' if ru else 'Source missing')
        values = [f'<a id="source-{entry["id"].lower()}"></a>' + _code(entry['id']), _code(entry['source_id']),
                  _code(path), str(entry['start_line']), str(entry['end_line']), cell(_status(entry, resolution, language)),
                  _code(identity)]
        out.append('| ' + ' | '.join(values) + ' |')
    return '\n'.join(out) + '\n'


def render_architecture(data, language='English'):
    """Render fixed headings and evidence beside immutable canonical blocks."""
    headings = section_headings(language)
    validate_materialized(data)
    missing_diagrams = inspect_materialized_diagrams(data)['missing_sections']
    blocks = data['block_map']
    claims = {entry['id']: entry for entry in data['claims']}
    evidence = {'study:' + entry['id']: entry for entry in data['evidence']}
    resolutions = {entry['id']: entry for entry in data['program_checks']['evidence']}
    sources = {entry['id']: entry for entry in data['review_plan']['sources']}
    # Split on LF only, preserving actual canonical bytes, including bare CR.
    parts = data['report_markdown'].split('\n')
    document = [part + '\n' for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    chunks = []
    for index, key in enumerate(SECTION_KEYS):
        chunks.append('## ' + headings[index] + '\n\n')
        for block in (block for block in blocks if block['section_key'] == key):
            locator = block['locator']
            chunks.append(''.join(document[locator['start_line'] - 1:locator['end_line']]) + '\n')
            references = render_source_references(block, claims, evidence, resolutions, sources, language)
            if references:
                chunks.append(references + '\n\n')
        if key in missing_diagrams:
            chunks.append(('### Обязательная диаграмма отсутствует\n\n'
                           'Исследование не завершено. Причины и необходимые проверки указаны в разделе и ограничениях.\n\n'
                           if russian(language) else
                           '### Required diagram missing\n\n'
                           'The study is incomplete. See this section and the limitations for the missing checks.\n\n'))
        if key == 'scope':
            chunks.append(render_evidence_index(data, language) + '\n')
    return ''.join(chunks)


def render_coverage(plan, checks=None):
    if not plan:
        return ''
    checks = checks or {}
    counts = plan['counts']
    out = ['## Source scope — pinned inventory', '',
           '; '.join(f'{key}: {counts[key]}' for key in
                     ('files', 'symlinks', 'directories', 'empty_directories')), '',
           'Python groups the filtered inventory for navigation. This does not measure understanding.', '',
           '| Area | Purpose | Paths | Files | Symbolic links |', '| --- | --- | --- | --- | --- |']
    links = set()
    for area in plan['areas']:
        symlinks = set(area['entry_paths']) - set(area['file_paths'])
        links.update(symlinks)
        out += ['| ' + ' | '.join(cell(x) for x in (area['id'] + ': ' + area['name'], area['purpose'],
                  ', '.join(area['paths']), len(area['file_paths']), len(symlinks))) + ' |']
    if links:
        out += ['', 'Symbolic links (targets were not followed): ' + cell(', '.join(sorted(links)))]
    out += ['',
            '## Citation coverage — program diagnostics', '',
            'CITED means an evidence locator resolved within the area. NOT_CITED does not mean NOT_INSPECTED. '
            'Inspection remains UNVERIFIED; citations and inventory metadata do not record agent file reads. '
            'These diagnostics do not block document publication.', '',
            '| Area | Citations | Inspection | Note |', '| --- | --- | --- | --- |']
    for area in plan['areas']:
        cited = area['id'] in checks.get('cited_ids', [])
        note = ''
        empty_paths = set(checks.get('metadata_verified_empty_paths', [])) & set(area['file_paths'])
        if empty_paths:
            note = 'Empty files verified from pinned metadata: ' + ', '.join(sorted(empty_paths)) + '.'
        out += ['| ' + ' | '.join(cell(x) for x in (area['id'], 'CITED' if cited else 'NOT_CITED',
                  'UNVERIFIED', note)) + ' |']
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
        'Policy checks not satisfied. Review may be incomplete or have limitations/issues.', '',
        'Semantic review quality is not measured. Resolving a locator does not validate the conclusion.', '']
    out += ['- ' + cell(x) for x in data.get('limitations', [])]
    if checks.get('citation_warnings'):
        out += ['Continued processing allowed: ' + str(checks.get('continuation_allowed', checks['policy_satisfied'])) +
                '; strict policy checks satisfied: ' + str(checks['policy_satisfied']) + '.', '']
    if stage == 'study':
        out += ['Study completion reported by the agent: ' + data['completion_status'], '']
        out += [render_coverage(data.get('review_plan', {}).get('coverage_plan'), checks.get('coverage')), '']
        diagnostics = data.get('advisory', {}).get('diagnostics', [])
        if diagnostics:
            out += ['## Auxiliary observations — warnings', '',
                    'These warnings do not change the document, source scope or acceptance checks.', '',
                    '| Response path | Reason | Detail |', '| --- | --- | --- |']
            for diagnostic in diagnostics:
                out += ['| ' + ' | '.join(cell(diagnostic[key]) for key in ('path', 'code', 'message')) + ' |']
            out += ['']
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
