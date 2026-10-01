# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Deterministic report assembly. No model calls or promotion of recovered text."""
from __future__ import annotations

import html
from contracts import SCHEMAS, accepted, has_program_checks, schema_diagnostics


def recoverable_material(stage, data, context, mode, diagnostics):
    """Minimum document contract; transport and source guards are the caller's job."""
    if stage not in ('study', 'review') or type(data) is not dict:
        return None
    identity = ('source_directory', 'source_fingerprint') if mode == 'folder' else ('branch', 'source_commit')
    if (data.get('task') != SCHEMAS[stage]['properties']['task']['enum'][0]
            or any(type(data.get(k)) is not str or data[k] != context.get(k) for k in identity)
            or type(data.get('report_markdown')) is not str or not data['report_markdown'].strip()):
        return None
    if stage == 'review' and 'target' in data and data['target'] != context.get('review_target'):
        return None
    material = {k: data[k] for k in ('task', 'report_markdown', *identity)}
    material.update(strict_valid=False, validation_issues=diagnostics,
                    completion_status=data.get('completion_status') if data.get('completion_status') in
                    ('COMPLETE', 'PARTIAL', 'BLOCKED') else None)
    if not schema_diagnostics(data.get('limitations'), SCHEMAS[stage]['properties']['limitations'], limit=0)['total_violations']:
        material['limitations'] = data['limitations']
    if stage == 'review' and all(not schema_diagnostics(data.get(key), SCHEMAS['review']['properties'][key],
            limit=0)['total_violations'] for key in ('claims', 'findings')):
        # Display these as the reviewer's observations, never as independent acceptance.
        material['review_ledger'] = {key: data[key] for key in ('claims', 'findings')}
    if stage == 'review' and data.get('target') == context.get('review_target'):
        material['review_observations'] = {}
        for key in ('claims', 'findings'):
            items = data.get(key)
            material['review_observations'][key] = [record for record in items
                if not schema_diagnostics(record, SCHEMAS['review']['properties'][key]['items'], limit=0)['total_violations']
                ] if type(items) is list else []
    return material


def stage_document(item, stage):
    return item.get(stage) or item.get(stage + '_material')


def usable_study(item):
    doc = stage_document(item, 'study')
    return bool(doc and doc.get('completion_status') != 'BLOCKED')


def report_entries(manifest, source, mode):
    if mode == 'folder':
        return [manifest]
    entries = {b['branch']: b for b in manifest.get('branches', [])}
    return [entries.get(branch, {'branch': branch, 'source_commit': manifest.get('pins', {}).get(branch),
                                'errors': []}) for branch in source['branches']]


def comparison_possible(entries, baseline):
    return (any(b['branch'] == baseline and usable_study(b) for b in entries)
            and any(b['branch'] != baseline and usable_study(b) for b in entries))


def render_final_report(manifest, source, mode, language='Russian'):
    from presentation import cell, label, render_stage, russian
    ru = russian(language)
    def t(r, e): return r if ru else e
    entries = report_entries(manifest, source, mode)
    useful = any(usable_study(b) for b in entries)
    out = ['# ' + t('Сводка исследования и автоматизированного ревью', 'Study and automated review summary'), '',
        t('Статус обработки: ', 'Processing status: ') + manifest['status'], '',
        t('Сводка собрана из материалов и замечаний; это не автоматически исправленная архитектура. '
          'Условия политики не устанавливают достоверность документа. Качество содержательного ревью не измерено.',
          'Assembled from materials and findings; this is not an automatically corrected architecture. '
          'Policy checks do not establish factual correctness. Semantic review quality is not measured.'), '']
    if not useful:
        out += ['> ' + t('Диагностическая сводка: пригодное исследование отсутствует.',
                          'Diagnostic summary: no usable study is available.'), '']
    if manifest.get('critical_failure'):
        out += ['> ' + t('Критическая ошибка запуска; учитывайте диагностику целостности и восстановления.',
                          'Critical run failure; consult integrity and restoration diagnostics.'), '']
    for b in entries:
        out += ['## ' + cell(b.get('branch', manifest.get('source_directory', source.get('path', '')))), '',
            t('Условия политики обработки и ревью выполнены.', 'Processing and review policy checks satisfied.') if accepted(b) else
            t('Условия политики не выполнены; причины указаны в диагностике.',
              'Policy checks not satisfied; see diagnostics for the reasons.'), '']
        if not b.get('review') or not accepted(b):
            out += ['> ' + t('Проверка реестра не завершена с положительным результатом политики.',
                              'Registry review has not completed with a positive policy result.'), '']
        review = b.get('review')
        if review:
            out += [render_stage('review', review, language), '']
        elif b.get('review_material', {}).get('review_observations'):
            observations = b['review_material']['review_observations']
            registry = {c['id']: c for c in (b.get('study') or {}).get('claims', [])}
            out += ['> ' + t('Ниже — сохранённые оценки агента из ответа с нарушениями контракта; приёмка не пройдена.',
                              'Retained agent assessments from a contract-invalid response follow; policy acceptance failed.'), '',
                    '| ID | ' + t('Исходное утверждение | Оценка агента | Ограничение',
                                  'Frozen statement | Agent assessment | Limitation') + ' |', '| --- | --- | --- | --- |']
            for c in observations['claims']:
                out += ['| ' + ' | '.join(cell(v) for v in (c['id'], registry.get(c['id'], {}).get('statement', '—'),
                            label(c['outcome'], language), c['limitation'])) + ' |']
            for f in observations['findings']:
                out += ['- ' + cell(f['id'] + ' / ' + f['severity'] + ': ' + f['impact'] + ' ' + f['proposed_correction'])]
            out += ['']
        for stage in ('study', 'review'):
            doc = stage_document(b, stage)
            if not doc:
                out += [stage + ': ' + t('Результат отсутствует.', 'Result unavailable.'), '']
                continue
            if not has_program_checks(doc) or doc.get('strict_valid') is False:
                failure = doc.get('contract_failure')
                if failure:
                    out += ['> ' + cell(failure['message']) + ' ' +
                            t('Текст сохранён; проверки политики не завершены. Самооценка агента: ',
                              'Text retained; policy checks not completed. Agent self-assessment: ') +
                            (doc.get('completion_status') or 'UNAVAILABLE') + '.', '']
                if not failure or not failure.get('details', {}).get('code'):
                    out += ['> ' + t('Старый формат или текст с нарушениями контракта; новые проверки не выполнялись. '
                                      'Исходный текст не получает положительную приёмку.',
                                      'Legacy format or text with contract violations; new checks were not performed. '
                                      'Original text has no positive acceptance.'), '']
            if stage == 'study':
                out += [render_stage(stage, doc, language), '']
            for issue in doc.get('limitations', []):
                out += ['- ' + cell(issue)]
            if doc.get('validation_issues'):
                for group in doc['validation_issues'].values():
                    for issue in group.get('violations', []):
                        out += ['- ' + cell(issue['path']) + ': ' + cell(issue.get('message', issue.get('violation')))]
            out += ['', '### ' + t('Исходный текст агента: ', 'Original agent text: ') + stage, '',
                    t('Ниже сохранены оценки и формулировки агента без исправлений; применяйте ограничения выше.',
                      'Agent assessments and wording below are preserved unchanged; apply the limitations above.'), '',
                    doc['report_markdown'], '']
    out += ['## ' + t('Сравнение', 'Comparison'), '']
    comparison = manifest.get('comparison')
    if comparison:
        out += [render_stage('compare', comparison, language), '',
                t('Исходный текст агента или объяснение оркестратора:', 'Original agent text or orchestrator explanation:'),
                '', comparison['report_markdown'], '']
    else:
        out += [t('Сравнение отсутствует; это не означает отсутствия различий. Для каталога и одной ветки оно не требуется.',
                  'Comparison unavailable; this does not imply no differences. Folder and single-branch runs do not require it.'), '']
    out += ['## ' + t('Диагностика запуска', 'Run diagnostics'), '']
    for d in manifest.get('diagnostics', []):
        out += ['- ' + cell(d.get('stage', '')) + ': ' + cell(d.get('failure_kind') or d.get('code')) + ' — ' + cell(d.get('message', ''))]
    if not manifest.get('diagnostics'):
        out += [t('Ошибок выполнения не зарегистрировано.', 'No execution errors recorded.')]
    return '\n'.join(out).rstrip() + '\n', useful
