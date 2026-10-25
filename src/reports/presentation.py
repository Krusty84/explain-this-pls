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
from src.reports.navigation import anchor, block_marker, link

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

RU_LABELS = {
    'SUPPORTED': 'Подтверждено по оценке ревьюера', 'CONTRADICTED': 'Ревьюер указал противоречие',
    'UNVERIFIABLE': 'Недостаточно доказательств по оценке ревьюера', 'NOT_CHECKED': 'Не проверено агентом',
    'MISSING': 'Ответ агента отсутствует', 'CAVEAT_ACCEPTABLE': 'Оговорка достаточна по оценке ревьюера',
    'CAVEAT_INADEQUATE': 'Оговорка недостаточна по оценке ревьюера',
    'PASS': 'Существенных проблем в обязательном реестре не указано; проверки политики пройдены',
    'CHANGES_REQUIRED': 'Агент указал существенные проблемы', 'INCONCLUSIVE': 'Приёмка не пройдена; см. проверки',
    'CONFIRMED_DIFFERENCE': 'Различие подтверждено отчётами по оценке агента',
    'REPORTED_UNVERIFIED': 'Указано агентом; не подтверждено', 'INSUFFICIENT_EVIDENCE': 'Недостаточно оснований для сравнения',
    'COMPLETE': 'Завершено', 'COMPLETE_WITH_WARNINGS': 'Завершено с предупреждениями',
    'PARTIAL': 'Не завершено', 'BLOCKED': 'Заблокировано', 'FAILED': 'Ошибка', 'RUNNING': 'Выполняется',
    'SKIPPED': 'Пропущено', 'HIGH': 'Высокая', 'MEDIUM': 'Средняя', 'LOW': 'Низкая',
    'FACTUAL_ERROR': 'Фактическая ошибка', 'UNSUPPORTED_ASSERTION': 'Неподтверждённое утверждение',
    'MATERIAL_OMISSION': 'Существенный пропуск', 'SCOPE_MISMATCH': 'Несоответствие области исследования',
    'CONTRACT_VIOLATION': 'Нарушение контракта', 'FACT': 'Факт', 'HYPOTHESIS': 'Гипотеза', 'UNKNOWN': 'Неизвестно',
    'RESOLVED': 'Проверено', 'UNRESOLVED': 'Не подтверждено', 'CORRECTED': 'Исправлено',
    'INSPECTED': 'Проверено агентом', 'PARTIALLY_INSPECTED': 'Частично проверено агентом',
    'NOT_INSPECTED': 'Не проверено агентом', 'CITED': 'Есть цитаты', 'NOT_CITED': 'Нет цитат',
    'UNVERIFIED': 'Проверка не подтверждена', 'QUOTE_MISMATCH': 'Цитата не совпала',
    'INVALID_POINTER': 'Некорректный локатор', 'UNKNOWN_SOURCE': 'Неизвестный источник',
    'SOURCE_SCOPE_MISMATCH': 'Источник вне области исследования', 'UNSAFE_PATH': 'Недопустимый путь',
    'NOT_FOUND': 'Файл не найден', 'ACCESS_DENIED': 'Нет доступа', 'READ_ERROR': 'Ошибка чтения',
    'OUT_OF_RANGE': 'Строки вне файла', 'DECODE_ERROR': 'Ошибка декодирования',
    'ENCODING_MISMATCH': 'Кодировка не совпала', 'LIMIT_EXCEEDED': 'Превышен лимит',
    'NEARBY_EXACT_QUOTE': 'Точная цитата найдена рядом', 'UNIFORM_LEADING_INDENT': 'Общий отступ исправлен',
}
LABELS.update({
    'COMPLETE': 'Complete', 'COMPLETE_WITH_WARNINGS': 'Complete with warnings', 'PARTIAL': 'Incomplete',
    'BLOCKED': 'Blocked', 'FAILED': 'Failed', 'RUNNING': 'Running', 'SKIPPED': 'Skipped',
    'FACTUAL_ERROR': 'Factual error', 'UNSUPPORTED_ASSERTION': 'Unsupported assertion',
    'MATERIAL_OMISSION': 'Material omission', 'SCOPE_MISMATCH': 'Scope mismatch',
    'CONTRACT_VIOLATION': 'Contract violation', 'FACT': 'Fact', 'HYPOTHESIS': 'Hypothesis', 'UNKNOWN': 'Unknown',
    'INSPECTED': 'Inspected according to the agent', 'PARTIALLY_INSPECTED': 'Partly inspected according to the agent',
    'NOT_INSPECTED': 'Not inspected according to the agent', 'CITED': 'Cited', 'NOT_CITED': 'Not cited',
    'UNVERIFIED': 'Unverified', 'UNRESOLVED': 'Unresolved', 'CORRECTED': 'Corrected', 'RESOLVED': 'Resolved',
    'QUOTE_MISMATCH': 'Quote mismatch', 'INVALID_POINTER': 'Invalid locator', 'UNKNOWN_SOURCE': 'Unknown source',
    'SOURCE_SCOPE_MISMATCH': 'Source outside scope', 'UNSAFE_PATH': 'Unsafe path', 'NOT_FOUND': 'File not found',
    'ACCESS_DENIED': 'Access denied', 'READ_ERROR': 'Read error', 'OUT_OF_RANGE': 'Lines outside file',
    'DECODE_ERROR': 'Decode error', 'ENCODING_MISMATCH': 'Encoding mismatch', 'LIMIT_EXCEEDED': 'Limit exceeded',
    'NEARBY_EXACT_QUOTE': 'Exact quote found nearby', 'UNIFORM_LEADING_INDENT': 'Common indentation repaired',
})


def russian(language):
    return isinstance(language, str) and language.strip().lower() in ('ru', 'russian', 'русский')


def cell(value):
    text = ''.join('\\u%04x' % ord(c) if unicodedata.category(c).startswith('C') and c not in '\n\r\t' else c for c in str(value))
    return (html.escape(text).replace('|', '&#124;').replace('[', '&#91;').replace(']', '&#93;')
            .replace('\r', ' ').replace('\n', ' ').replace('\t', ' '))


def label(value, language='English'):
    return (RU_LABELS if russian(language) else LABELS).get(value, value)


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
            label(warning['reason'], language), label(warning['action'], language), ', '.join(sorted(warning['claim_ids'])) or '—')) + ' |')
    out += ['', ('Не подтверждено: цитата не проверена. Исправлено: диапазон или общий отступ исправлен; '
                 'исходная ошибка сохранена в предупреждении. Продолжение обработки не означает строгую приёмку.' if ru else
                 'Unresolved: the citation was not verified. Corrected: the range or common indentation was repaired; '
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
    return label(status, language) + '; ' + detail


def render_source_references(block, claims, evidence, resolutions, sources, language='English', *, document=None):
    """Follow canonical IDs in first-occurrence order; never inspect authored prose."""
    refs, limitations, seen = [], [], set()
    for claim_id in block['claim_ids']:
        claim = _lookup(claims, claim_id, '$.block_map.claim_ids')
        if not claim['evidence_ids']:
            limitations.append(cell(claim['statement']) + ' (' + label(claim['epistemic_kind'], language) + ')')
        for identifier in claim['evidence_ids']:
            if identifier in seen:
                continue
            seen.add(identifier)
            entry = _lookup(evidence, identifier, '$.claims.evidence_ids')
            resolution = _lookup(resolutions, identifier, '$.program_checks.evidence')
            path = _source_path(entry, resolution, sources)
            reference = link(_code(f"{path}:{entry['start_line']}–{entry['end_line']}"),
                             fragment=anchor('source', identifier, document))
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
           '| ' + ('ID | Файл:строки | Источник | Проверка | Места использования' if ru else
                    'ID | File:lines | Source | Check | Used in') + ' |',
           '| --- | --- | --- | --- | --- |']
    claims = {entry['id']: entry for entry in data['claims']}
    for entry in data['evidence']:
        identifier = 'study:' + entry['id']
        resolution = _lookup(resolutions, identifier, '$.program_checks.evidence')
        path = _source_path(entry, resolution, sources)
        uses = [block_link(block, data, language) for block in data['block_map']
                if any(identifier in claims[cid]['evidence_ids'] for cid in block['claim_ids'])]
        values = [f'<a id="{anchor("source", identifier, data)}"></a>' + _code(identifier),
                  _code(f"{path}:{entry['start_line']}–{entry['end_line']}"), _code(entry['source_id']),
                  cell(_status(entry, resolution, language)), ', '.join(uses) or ('Не используется' if ru else 'Unused')]
        out.append('| ' + ' | '.join(values) + ' |')
    out += ['', '### ' + ('Идентичность источников' if ru else 'Source identities'), '',
            '| ' + ('Источник | Идентичность источника' if ru else 'Source | Source identity') + ' |',
            '| --- | --- |']
    for source_id in dict.fromkeys(entry['source_id'] for entry in data['evidence']):
        source = sources.get(source_id)
        identity = json.dumps(source['identity'], ensure_ascii=False, sort_keys=True, separators=(',', ':')) if source else (
            'Источник отсутствует' if ru else 'Source missing')
        out.append('| ' + _code(source_id) + ' | ' + _code(identity) + ' |')
    return '\n'.join(out) + '\n'


def block_link(block, data, language='English', path=''):
    title = section_headings(language)[block['section_index']]
    title += (' — блок ' if russian(language) else ' — block ') + str(block['block_index'] + 1)
    return link(title, path, anchor('block', block['id'], data))


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
    chunks = [('**Оглавление**' if russian(language) else '**Contents**') + '\n\n' +
              ' · '.join(link(title, fragment=anchor('section', key, data))
                         for key, title in zip(SECTION_KEYS, headings)) + '\n\n']
    for index, key in enumerate(SECTION_KEYS):
        chunks.append('<a id="' + anchor('section', key, data) + '"></a>\n\n## ' + headings[index] + '\n\n')
        for block in (block for block in blocks if block['section_key'] == key):
            locator = block['locator']
            chunks.append(block_marker(block, data))
            chunks.append(''.join(document[locator['start_line'] - 1:locator['end_line']]) + '\n')
            references = render_source_references(block, claims, evidence, resolutions, sources, language, document=data)
            if references:
                chunks.append(references + '\n\n')
        if key in missing_diagrams:
            chunks.append(('### Обязательная диаграмма отсутствует\n\n'
                           'Исследование не завершено. Причины и необходимые проверки указаны в разделе и ограничениях.\n\n'
                           if russian(language) else
                           '### Required diagram missing\n\n'
                           'The study is incomplete. See this section and the limitations for the missing checks.\n\n'))
    chunks.append(render_evidence_index(data, language) + '\n')
    return ''.join(chunks)


def render_coverage(plan, checks=None, language='English'):
    if not plan:
        return ''
    from src.analysis.coverage_plan import AREA_PURPOSE
    ru = russian(language)
    def t(r, e): return r if ru else e
    checks = checks or {}
    counts = plan['counts']
    names = (('files', t('Файлы', 'Files')), ('symlinks', t('Символические ссылки', 'Symbolic links')),
             ('directories', t('Каталоги', 'Directories')), ('empty_directories', t('Пустые каталоги', 'Empty directories')))
    out = ['## ' + t('Область исходников — зафиксированный список', 'Source scope — pinned inventory'), '',
           '; '.join(name + ': ' + str(counts[key]) for key, name in names), '',
           t('Программа группирует список для навигации. Это не измеряет понимание системы.',
             'Python groups the filtered inventory for navigation. This does not measure understanding.'), '',
           '| ' + t('Область | Назначение | Пути | Файлы | Символические ссылки', 'Area | Purpose | Paths | Files | Symbolic links') + ' |',
           '| --- | --- | --- | --- | --- |']
    symlinks = set()
    for area in plan['areas']:
        links = set(area['entry_paths']) - set(area['file_paths'])
        symlinks.update(links)
        purpose = t('Элементы исходников в этой группе каталогов; исследование не подтверждено.', AREA_PURPOSE) if area['purpose'] == AREA_PURPOSE else area['purpose']
        name = t('Корень исходников', 'Source root') if area['paths'] == ['.'] else area['name']
        out += ['| ' + ' | '.join(cell(x) for x in (area['id'] + ': ' + name, purpose, ', '.join(area['paths']),
                                                   len(area['file_paths']), len(links))) + ' |']
    if symlinks:
        out += ['', t('Символические ссылки (цели не читались): ', 'Symbolic links (targets were not followed): ') + cell(', '.join(sorted(symlinks)))]
    out += ['', '## ' + t('Покрытие цитатами — диагностика программы', 'Citation coverage — program diagnostics'), '',
            t('Наличие цитаты означает разрешённый локатор в этой области. Отсутствие цитат не означает, что область не исследована. '
              'Список и цитаты не регистрируют чтение файлов агентом. Эта диагностика не блокирует публикацию.',
              'CITED means an evidence locator resolved within the area. NOT_CITED does not mean NOT_INSPECTED. '
              'Inspection remains UNVERIFIED; citations and inventory metadata do not record agent file reads. '
              'These diagnostics do not block document publication.'), '',
            '| ' + t('Область | Цитаты | Исследование | Примечание', 'Area | Citations | Inspection | Note') + ' |', '| --- | --- | --- | --- |']
    for area in plan['areas']:
        empty = set(checks.get('metadata_verified_empty_paths', [])) & set(area['file_paths'])
        note = t('Пустые файлы подтверждены зафиксированными метаданными: ', 'Empty files verified from pinned metadata: ') + ', '.join(sorted(empty)) + '.' if empty else ''
        out += ['| ' + ' | '.join(cell(x) for x in (area['id'], label('CITED' if area['id'] in checks.get('cited_ids', []) else 'NOT_CITED', language),
                                                   label('UNVERIFIED', language), note)) + ' |']
    return '\n'.join(out) + '\n'


def validate_report(stage, data):
    if is_recovered_material(stage, data):
        return
    schemas = SAVED_FOLDER_SCHEMAS if type(data) is dict and 'source_directory' in data else SAVED_SCHEMAS
    validate_schema(data, schemas[stage])
    if stage == 'study':
        validate_materialized(data)


def render_stage(stage, data, language='English', *, study=None, context=None):
    """Full technical annotation; primary review and comparison have separate views."""
    validate_report(stage, data)
    ru = russian(language)
    def t(r, e): return r if ru else e
    if is_recovered_material(stage, data):
        return '> ' + t('Текст восстановлен после отклонения контракта; полные проверки политики не завершены.',
                        'Text retained after contract rejection; full policy checks are not complete.') + '\n\n'
    if stage == 'compare':
        from src.reports.comparison import render_comparison
        return render_comparison(data, language, context)
    from src.reports.review import bound_study, evidence_context, source_title, review_searches, ordered_findings, finding_location
    if stage == 'review':
        study = bound_study(data, study)
    else:
        study = data
    records = evidence_context(data, study if stage == 'review' else None)
    checks = data['program_checks']
    out = ['# ' + t('Проверки программы и оценки агента', 'Program checks and agent assessments'), '',
           t('Проверки политики пройдены; фактическая правильность не установлена.',
             'Policy checks satisfied; factual correctness is not established.') if checks['policy_satisfied'] else
           t('Проверки политики не пройдены. Ревью может быть не завершено или содержать проблемы.',
             'Policy checks not satisfied. Review may be incomplete or have limitations/issues.'), '',
           t('Качество содержательного ревью не измерено. Разрешение локатора не подтверждает вывод.',
             'Semantic review quality is not measured. Resolving a locator does not validate the conclusion.'), '',
           t('Состояние по оценке агента: ', 'Agent completion assessment: ') + label(data['completion_status'], language), '']
    out += ['- ' + cell(x) for x in data['limitations']]
    if checks.get('citation_warnings'):
        out += [t('Продолжение обработки разрешено: ', 'Continued processing allowed: ') +
                (t('Да', 'True') if checks.get('continuation_allowed', checks['policy_satisfied']) else t('Нет', 'False')) +
                t('; строгие проверки политики пройдены: ', '; strict policy checks satisfied: ') +
                (t('Да', 'True') if checks['policy_satisfied'] else t('Нет', 'False')) + '.', '']
    if stage == 'study':
        out += [render_coverage(data['review_plan'].get('coverage_plan'), checks.get('coverage'), language), '']
        diagnostics = data.get('advisory', {}).get('diagnostics', [])
        if diagnostics:
            out += ['## ' + t('Дополнительные наблюдения — предупреждения', 'Auxiliary observations — warnings'), '',
                    t('Эти предупреждения не меняют документ, область исходников или проверки приёмки.',
                      'These warnings do not change the document, source scope or acceptance checks.'), '',
                    '| ' + t('Путь ответа | Причина | Подробности', 'Response path | Reason | Detail') + ' |', '| --- | --- | --- |']
            out += ['| ' + ' | '.join(cell(d[key]) for key in ('path', 'code', 'message')) + ' |' for d in diagnostics]
    else:
        coverage = checks['registry_coverage']
        out += [label(data['verdict'], language), '',
                t('Покрытие зафиксированного реестра (не всех фактов документа): ', 'Frozen registry coverage (not all facts in the document): ') +
                f"{coverage['assessed_count']}/{coverage['expected_count']}", '',
                '; '.join(label(outcome, language) + ': ' + str(count) for outcome, count in checks['outcome_counts'].items()), '']
    registry = data['claims'] if stage == 'study' else data['claim_registry']
    replies = {c['id']: c for c in data['claims']} if stage == 'review' else {}
    uses = {}
    def evidence_links(refs, usage, title):
        result = []
        for ref in dict.fromkeys(refs):
            if ref not in records:
                result.append(_code(ref) + ' — ' + t('Контекст недоступен', 'Context unavailable'))
            else:
                uses.setdefault(ref, {})[usage] = title
                result.append(link(source_title(records[ref]), fragment=anchor('source', ref)))
        return ', '.join(result) or '—'
    out += ['', '## ' + t('Полный реестр утверждений', 'Full claim registry'), '',
            '| ' + t('ID | Место в документе | Утверждение и область | Классификация автора | Оценка агента | Источники | Ограничения',
                      'ID | Document location | Statement and scope | Author classification | Agent assessment | Sources | Limitations') + ' |',
            '| --- | --- | --- | --- | --- | --- | --- |']
    for claim in registry:
        reply = replies.get(claim['id'], {})
        usage = anchor('claim', claim['id'])
        coordinates = ', '.join(f"study.original.md:{loc['start_line']}–{loc['end_line']}" for loc in claim['document_locators'])
        locations = link(_code(coordinates), 'study.original.md') if study else _code(coordinates)
        if study:
            from src.reports.review import claim_places
            locations += '<br>' + ', '.join(claim_places([claim['id']], study, language))
        warnings = [w['evidence_id'] + ' (' + label(w['action'], language) + ')' for w in checks.get('citation_warnings', []) if claim['id'] in w['claim_ids']]
        issues = (t('Требуется проверка цитат: ', 'Citation checks required: ') + ', '.join(sorted(warnings)) + '. ') if warnings else ''
        values = [f'<a id="{usage}"></a>' + _code(claim['id']), locations,
                  cell(claim['statement'] + ' / ' + claim['scope']), label(claim['epistemic_kind'], language),
                  label(reply.get('outcome', 'MISSING'), language) if stage == 'review' else t('Оценка автора; ревью показано отдельно', 'Author assessment; review assessments are shown separately'),
                  evidence_links(claim['evidence_ids'] + reply.get('evidence_ids', []), usage, cell(claim['statement'])),
                  cell(issues + reply.get('limitation', '') + ' ' + claim['uncertainty'])]
        out += ['| ' + ' | '.join(values) + ' |']
    if stage == 'review':
        out += ['', '## ' + t('Замечания агента', 'Agent findings'), '']
        for finding in ordered_findings(data):
            usage = anchor('finding', finding['id'])
            out += [f'<a id="{usage}"></a>', '', '### ' + _code(finding['id']) + ' — ' + label(finding['severity'], language), '',
                    label(finding['type'], language) + ': ' + finding_location(finding['location']), '',
                    cell(finding['impact']), '', cell(finding['proposed_correction']), '',
                    ', '.join(link(_code(cid), fragment=anchor('claim', cid)) for cid in finding['claim_ids']), '',
                    evidence_links(finding['evidence_ids'], usage, cell(finding['impact'])), '']
        out += review_searches(data, language)
    out += ['', '## ' + t('Полный реестр источников', 'Full source registry'), '',
            t('Состояние исходников совпало на границах проверок; непрерывная неизменность не установлена. '
              'Локаторы разрешены программой; чтение файлов агентом не зарегистрировано.',
              'Source state matched at checked boundaries. This is not continuous immutability. '
              'Locators were resolved by the orchestrator; this is not a record of agent file reads.'), '',
            '| ' + t('ID | Файл:строки | Проверка | Кодировка | Идентичность источника | Места использования',
                      'ID | File:lines | Check | Encoding | Source identity | Used in') + ' |', '| --- | --- | --- | --- | --- | --- |']
    for ref, record in records.items():
        entry, resolution, sources = record
        source = sources.get(entry['source_id'])
        identity = json.dumps(source['identity'], ensure_ascii=False, sort_keys=True) if source else t('Источник отсутствует', 'Source missing')
        values = [f'<a id="{anchor("source", ref)}"></a>' + _code(ref), source_title(record), cell(_status(entry, resolution, language)),
                  _code(resolution.get('encoding') or '—'), _code(identity),
                  ', '.join(link(title, fragment=usage) for usage, title in uses.get(ref, {}).items()) or '—']
        out += ['| ' + ' | '.join(values) + ' |']
    out += ['', render_citation_warnings(data, language)]
    return '\n'.join(out).rstrip() + '\n'
