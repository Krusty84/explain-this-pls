# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Finding context from the frozen review and its bound study only."""
import json
import re

from src.contracts.contracts import contract_violation, is_recovered_material
from src.analysis.evidence import canonical, sha
from src.reports.navigation import anchor, link
from src.reports.presentation import (cell, label, russian, _code, _source_path, _status,
                                      block_link, validate_report, render_citation_warnings)


def bound_study(data, study):
    if study is None or is_recovered_material('study', study):
        return None
    validate_report('study', study)
    plan = study['review_plan']
    intact = (plan['document_sha256'] == sha(study['report_markdown'].encode('utf-8'))
              and plan['registry_sha256'] == sha(canonical(study['claims']))
              and plan['source_sha256'] == sha(canonical(plan['sources']))
              and plan['plan_sha256'] == sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'})))
    if (not intact or data['target'] != {k: plan[k] for k in data['target']} or data['review_plan'] != plan
            or data['claim_registry'] != study['claims'] or data['revision_id'] != study['revision_id']
            or any(data.get(k) != study.get(k) for k in
                   ('branch', 'source_commit', 'source_directory', 'source_fingerprint'))):
        raise contract_violation('REVIEW_TARGET_CHANGED', '$.target')
    return study


def evidence_context(data, study=None):
    records = {}
    documents = [('study', study)] if study is not None else []
    documents.append(('review' if 'claim_registry' in data else 'study', data))
    for stage, document in documents:
        resolutions = {e['id']: e for e in document['program_checks']['evidence']}
        sources = {s['id']: s for s in document['review_plan']['sources']}
        for entry in document['evidence']:
            identifier = stage + ':' + entry['id']
            resolution = resolutions[identifier]
            records[identifier] = (entry, resolution, sources)
    return records


def source_title(record):
    entry, resolution, sources = record
    return _code(f"{_source_path(entry, resolution, sources)}:{entry['start_line']}–{entry['end_line']}")


def claim_places(claim_ids, study, language, path='ARCHITECTURE.md'):
    if study is None:
        return []
    return [block_link(block, study, language, path) for block in study['block_map']
            if set(claim_ids).intersection(block['claim_ids'])]


def ordered_findings(data):
    return sorted(data.get('findings', []), key=lambda f: ('HIGH', 'MEDIUM', 'LOW').index(f['severity']))


def finding_location(value):
    if re.fullmatch(r'L\d+(?:[–-]L?\d+)?', value.strip()):
        value = 'study.original.md:' + value.strip()
    return cell(value)


def render_review(data, language='English', study=None):
    validate_report('review', data)
    study = bound_study(data, study)
    records = evidence_context(data, study)
    ru = russian(language)
    def t(r, e): return r if ru else e
    registry = {c['id']: c for c in data['claim_registry']}
    replies = {c['id']: c for c in data['claims']}
    used = {}

    def basis(refs, usage, title):
        result = []
        for ref in dict.fromkeys(refs):
            record = records.get(ref)
            if record is None:
                result.append(t('Контекст источника недоступен: ', 'Source context unavailable: ') + _code(ref))
                continue
            used.setdefault(ref, {})[usage] = title
            result.append(link(source_title(record), fragment=anchor('source', ref)) + ' — ' +
                          cell(_status(record[0], record[1], language)))
        return '; '.join(result) or t('Основания не указаны.', 'No evidence supplied.')

    checks = data['program_checks']
    coverage = checks['registry_coverage']
    out = ['# ' + t('Ревью архитектуры', 'Architecture review'), '', label(data['verdict'], language), '',
           t('Состояние ревью: ', 'Review state: ') + label(data['completion_status'], language) + '.', '',
           t('Проверки политики пройдены; фактическая правильность не установлена.',
             'Policy checks satisfied; factual correctness is not established.') if checks['policy_satisfied'] else
           t('Приёмка не пройдена. Ревью содержит проблемы или не завершено.',
             'Policy checks not satisfied. Review has issues or is incomplete.'), '',
           t('Качество содержательного ревью не измерено. Разрешение локатора не подтверждает вывод.',
             'Semantic review quality is not measured. Resolving a locator does not validate the conclusion.'), '',
           t('Проверен обязательный реестр: ', 'Required registry assessed: ') +
           f"{coverage['assessed_count']}/{coverage['expected_count']}.", '']
    out += ['- ' + cell(x) for x in data['limitations']]
    if checks.get('citation_warnings'):
        out += ['', '> ' + link(t('Есть предупреждения о цитатах; проверьте ограничения.',
                                   'Citation warnings require attention; see limitations.'),
                               fragment=anchor('section', 'citation-warnings')), '']
    out += ['', '## ' + t('Замечания', 'Findings'), '']
    if not data['findings']:
        out += [t('Агент не указал замечаний. Это не доказывает отсутствие проблем.',
                  'The agent reported no findings. This does not establish the absence of issues.'), '']
    for index, finding in enumerate(ordered_findings(data), 1):
        usage = anchor('finding', finding['id'])
        title = label(finding['severity'], language) + ' — ' + label(finding['type'], language)
        out += [f'<a id="{usage}"></a>', '', f'### {index}. {title}', '']
        for cid in finding['claim_ids']:
            out += ['**' + t('Утверждение:', 'Statement:') + '** ' + cell(registry[cid]['statement']), '']
            reply = replies.get(cid, {})
            out += [label(reply.get('outcome', 'MISSING'), language) + '. ' + cell(reply.get('limitation', '')), '']
            if registry[cid]['uncertainty']:
                out += [cell(registry[cid]['uncertainty']), '']
        if not finding['claim_ids']:
            out += [t('Связанное утверждение не указано.', 'No linked claim supplied.'), '']
        refs = list(finding['evidence_ids'])
        for cid in finding['claim_ids']:
            refs += registry[cid]['evidence_ids'] + replies.get(cid, {}).get('evidence_ids', [])
        fields = [(t('Проблема:', 'Problem:'), label(finding['type'], language) + ' — ' + finding_location(finding['location'])),
                  (t('Последствие:', 'Impact:'), cell(finding['impact'])),
                  (t('Предлагаемое исправление:', 'Proposed correction:'), cell(finding['proposed_correction'])),
                  (t('Основание:', 'Basis:'), basis(refs, usage, title)),
                  (t('Место в отчёте:', 'Report location:'), ', '.join(claim_places(finding['claim_ids'], study, language))
                   or t('Связанный блок архитектуры не указан.', 'No linked architecture block.'))]
        for name, value in fields:
            out += ['**' + name + '** ' + value, '']
    out += ['## ' + t('Непроверенные утверждения и ограничения', 'Unchecked claims and limitations'), '']
    linked = {cid for finding in data['findings'] for cid in finding['claim_ids']}
    unchecked = [c for c in registry.values() if replies.get(c['id'], {}).get('outcome') not in ('SUPPORTED', 'CAVEAT_ACCEPTABLE')
                 and (c['id'] not in linked or replies.get(c['id'], {}).get('outcome') not in ('CONTRADICTED', 'CAVEAT_INADEQUATE'))]
    if not unchecked:
        out += [t('Других непроверенных или спорных утверждений в реестре не указано.',
                  'No further unchecked or disputed registry claims reported.'), '']
    for claim in unchecked:
        reply = replies.get(claim['id'], {})
        usage = anchor('claim', claim['id'])
        out += [f'<a id="{usage}"></a>', '', '**' + cell(claim['statement']) + '**', '',
                label(reply.get('outcome', 'MISSING'), language) + '. ' + cell(reply.get('limitation', '')), '',
                cell(claim['uncertainty']), '', basis(claim['evidence_ids'] + reply.get('evidence_ids', []),
                                                     usage, cell(claim['statement'])), '',
                ', '.join(claim_places([claim['id']], study, language)), '']
    out += review_searches(data, language)
    if checks.get('citation_warnings'):
        out += [f'<a id="{anchor("section", "citation-warnings")}"></a>', '',
                render_citation_warnings(data, language), '']
    if used:
        out += ['## ' + t('Источники замечаний', 'Finding sources'), '']
        for ref, uses in used.items():
            entry, resolution, sources = records[ref]
            source = sources.get(entry['source_id'])
            identity = json.dumps(source['identity'], ensure_ascii=False, sort_keys=True) if source else t('Источник отсутствует', 'Source missing')
            out += [f'<a id="{anchor("source", ref)}"></a>', '', '### ' + source_title(records[ref]), '',
                    _code(ref) + ' — ' + cell(_status(entry, resolution, language)), '',
                    t('Идентичность источника: ', 'Source identity: ') + _code(identity), '',
                    t('Вернуться: ', 'Return to: ') + ', '.join(link(title, fragment=usage) for usage, title in uses.items()), '']
    out += ['## ' + t('Подробные материалы', 'Detailed material'), '',
            '- ' + link(t('Полный технический реестр', 'Full technical registry'), 'review.annotated.md'),
            '- ' + link(t('Исходный текст агента', 'Original agent text'), 'review.original.md'),
            '- ' + link(t('Структурированные данные ревью', 'Structured review data'), 'review.json'), '']
    return '\n'.join(out).rstrip() + '\n'


def review_searches(data, language):
    ru = russian(language)
    def t(r, e): return r if ru else e
    from src.reports.architecture_format import section_headings
    from src.contracts.contracts import SECTION_KEYS
    titles = dict(zip(SECTION_KEYS, section_headings(language)))
    searches = {a['area_id']: a for a in data['omission_search']}
    out = ['### ' + t('Поиск пропусков — оценки агента', 'Omission search — agent reports'), '',
           '| ' + t('Область | Состояние | Ограничение', 'Area | State | Limitation') + ' |', '| --- | --- | --- |']
    for area in data['review_plan']['omission_areas']:
        reply = searches.get(area['id'], {})
        scope = titles.get(area['scope'], area['scope']) if area['id'].startswith('A-') else area['scope']
        out += ['| ' + ' | '.join(cell(x) for x in (scope,
                  label(reply.get('status', 'MISSING'), language), reply.get('limitation', ''))) + ' |']
    expected = data['review_plan']['prior_findings']
    if expected:
        replies = {(f['revision_id'], f['finding_id']): f for f in data['prior_findings']}
        out += ['', '### ' + t('Предыдущие существенные замечания — оценки ревьюера', 'Previous material findings — reviewer assessment'), '',
                '| ' + t('Ревизия / замечание | Состояние | Объяснение', 'Revision / finding | State | Explanation') + ' |',
                '| --- | --- | --- |']
        for finding in expected:
            key = (finding['revision_id'], finding['finding_id'])
            reply = replies.get(key, {})
            status = reply.get('status', 'MISSING')
            status_text = {'RESOLVED': 'Устранено', 'UNRESOLVED': 'Не устранено'}.get(status, label(status, language)) if ru else label(status, language)
            out += ['| ' + ' | '.join(cell(x) for x in (' / '.join(key), status_text,
                      reply.get('explanation', t('Обязательный ответ отсутствует.', 'Required response missing.')))) + ' |']
    return out + ['']
