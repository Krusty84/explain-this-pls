# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Deterministic bilingual labels and tables, separate from unchanged agent prose."""
import html
import unicodedata
from contracts import has_program_checks

LABELS = {
    'SUPPORTED': ('Поддержано по оценке агента', 'Supported according to the reviewing agent'),
    'CONTRADICTED': ('Агент указал противоречие', 'Agent reported a contradiction'),
    'UNVERIFIABLE': ('По оценке агента данных недостаточно', 'Insufficient evidence according to the agent'),
    'NOT_CHECKED': ('Агент не оценил пункт', 'Not assessed by the agent'),
    'MISSING': ('Ответ агента отсутствует', 'Agent response missing'),
    'CAVEAT_ACCEPTABLE': ('Оговорка приемлема по оценке агента', 'Caveat acceptable according to the agent'),
    'CAVEAT_INADEQUATE': ('Агент считает оговорку недостаточной', 'Agent considers the caveat inadequate'),
    'PASS': ('По обязательным пунктам агент не сообщил существенных замечаний; условия политики выполнены',
             'No material issues reported for the required claim registry; policy checks satisfied'),
    'CHANGES_REQUIRED': ('Есть существенные замечания агента', 'Material issues reported by the agent'),
    'INCONCLUSIVE': ('Проверка не завершена или данных недостаточно', 'Review incomplete or evidence insufficient'),
    'CONFIRMED_DIFFERENCE': ('Различие поддержано отчётами по оценке агента', 'Difference supported by supplied reports according to the agent'),
    'REPORTED_UNVERIFIED': ('Сообщено агентом; не проверено', 'Reported by the agent; unverified'),
    'INSUFFICIENT_EVIDENCE': ('Недостаточно данных для сравнения', 'Insufficient comparison evidence'),
}


def russian(language):
    return isinstance(language, str) and language.lower() in ('ru', 'russian', 'русский')


def cell(value):
    text = ''.join('\\u%04x' % ord(c) if unicodedata.category(c).startswith('C') and c not in '\n\r\t' else c for c in str(value))
    return html.escape(text).replace('|', '&#124;').replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')


def label(value, language='English'):
    return LABELS.get(value, (value, value))[0 if russian(language) else 1]


def render_stage(stage, data, language='English'):
    ru = russian(language)
    def t(r, e): return r if ru else e
    if not has_program_checks(data):
        return '> ' + t('Старый или восстановленный формат; новые проверки не выполнялись.',
                        'Legacy or recovered format; new checks were not performed.') + '\n\n'
    checks = data['program_checks']
    out = ['# ' + t('Результаты программных проверок и оценки агента', 'Program checks and agent assessments'), '',
        (t('Условия обработки исследования выполнены; итоговая приёмка зависит от ревью.',
           'Study processing conditions satisfied; final acceptance depends on review.') if stage == 'study' else
         t('Условия политики выполнены; достоверность текста этим не установлена.',
           'Policy checks satisfied; factual correctness is not established.')) if checks['policy_satisfied'] else
        t('Условия политики не выполнены. Проверка неполна или есть ограничения/замечания.',
          'Policy checks not satisfied. Review is incomplete or has limitations/issues.'), '',
        t('Качество содержательного ревью не измерено. Разрешение ссылки не проверяет смысл вывода.',
          'Semantic review quality is not measured. Resolving a locator does not validate the conclusion.'), '']
    out += ['- ' + cell(x) for x in data.get('limitations', [])]
    if stage == 'study':
        out += [t('Самооценка исследования агентом: ', 'Study completion reported by the agent: ') + data['completion_status'], '']
    elif stage == 'review':
        out += [label(data['verdict'], language), '']
        c = checks['registry_coverage']
        out += [t('Покрытие зафиксированного реестра (не всех фактов документа): ',
                  'Frozen registry coverage (not all facts in the document): ') +
                f"{c['assessed_count']}/{c['expected_count']}; " +
                t('ответов: ', 'responses: ') + str(c['received_count']) + '; ' +
                t('отсутствуют: ', 'missing: ') + cell(', '.join(c['missing_ids']) or '—'), '']
        out += [t('Связь с документом: ', 'Document links: ') +
                str(checks['document_links']['matched']) + '/' + str(checks['document_links']['registered']) +
                t(' зарегистрированных утверждений.', ' registered claims.'), '']
        out += ['; '.join(cell(label(outcome, language)) + ': ' + str(count)
                         for outcome, count in checks['outcome_counts'].items()), '']
    else:
        out += [t('Сравнение основано только на переданных отчётах. Исходники на этом этапе не проверялись.',
                  'Comparison uses supplied reports only. Sources were not inspected in this stage.'), '',
                '| ID | ' + t('Ветка | Оценка агента | Основание', 'Branch | Agent assessment | Rationale') + ' |',
                '| --- | --- | --- | --- |']
        for d in data['differences']:
            out.append('| ' + ' | '.join(cell(v) for v in (d['id'], d['branch'], label(d['classification'], language), d['explanation'])) + ' |')
    if stage in ('study', 'review'):
        registry = data['claims'] if stage == 'study' else data['claim_registry']
        replies = {c['id']: c for c in data['claims']} if stage == 'review' else {}
        out += ['', '| ID | ' + t('Место в документе | Утверждение и область | Вид по самооценке автора | Оценка агента | Указанные доказательства | Замечания и ограничения',
                                  'Document location | Statement and scope | Author classification | Agent assessment | Cited evidence | Findings and limitations') + ' |',
                '| --- | --- | --- | --- | --- | --- | --- |']
        findings = {f['id']: f for f in data.get('findings', [])}
        for claim in registry:
            reply = replies.get(claim['id'], {})
            linked = checks.get('finding_ids_by_claim', {}).get(claim['id'], [])
            issues = '; '.join(fid + ': ' + findings[fid]['impact'] for fid in linked)
            location = claim['document_locator']
            evidence_refs = claim['evidence_ids'] + reply.get('evidence_ids', [])
            out.append('| ' + ' | '.join(cell(v) for v in (claim['id'], f"L{location['start_line']}-L{location['end_line']}",
                claim['statement'] + ' / ' + claim['scope'],
                claim['epistemic_kind'], label(reply.get('outcome', 'MISSING'), language) if stage == 'review' else
                t('Самооценка автора; оценки ревью показаны отдельно', 'Author assessment; review assessments are shown separately'),
                ', '.join(evidence_refs), issues + ' ' + reply.get('limitation', '') + ' ' + claim['uncertainty'])) + ' |')
        out += ['', t('Состояние источника совпало на границах проверки. Это не непрерывная гарантия неизменности.',
                       'Source state matched at checked boundaries. This is not continuous immutability.'), '',
                t('Указатели разрешены оркестратором; это не запись чтения файлов агентом.',
                  'Locators were resolved by the orchestrator; this is not a record of agent file reads.'), '',
                '| ID | ' + t('Источник | Путь : строки | Статус ссылки | Файл SHA-256 | Фрагмент SHA-256',
                              'Source | Path : lines | Locator status | File SHA-256 | Fragment SHA-256') + ' |',
                '| --- | --- | --- | --- | --- | --- |']
        for e in checks['evidence']:
            out.append('| ' + ' | '.join(cell(v) for v in (e['id'], e['source_id'],
                f"{e['path']}:{e['start_line']}-{e['end_line']}", e['status'], e['file_sha256'], e['fragment_sha256'])) + ' |')
        if stage == 'review':
            out += ['', '## ' + t('Поиск упущений — сведения агента', 'Omission search — agent reports'), '',
                    '| ' + t('Область | Состояние | Ограничение', 'Area | Status | Limitation') + ' |', '| --- | --- | --- |']
            searches = {a['area_id']: a for a in data['omission_search']}
            for area in data['review_plan']['omission_areas']:
                a = searches.get(area['id'], {})
                out.append('| ' + ' | '.join(cell(v) for v in (area['id'] + ': ' + area['scope'],
                    a.get('status', label('MISSING', language)), a.get('limitation', ''))) + ' |')
            out += ['', '## ' + t('Замечания агента', 'Agent findings'), '']
            for f in data['findings']:
                out += ['- ' + cell(f['id'] + ' / ' + f['severity'] + ': ' + f['impact'] + ' ' + f['proposed_correction'])]
    return '\n'.join(out).rstrip() + '\n'
