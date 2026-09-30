# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Deterministic report assembly. No model calls or promotion of recovered text."""
from __future__ import annotations

import html
from contracts import SCHEMAS, accepted, schema_diagnostics


def recoverable_material(stage, data, context, mode, diagnostics):
    """Minimum document contract; transport and source guards are the caller's job."""
    if stage not in ('study', 'review') or type(data) is not dict:
        return None
    identity = ('source_directory', 'source_fingerprint') if mode == 'folder' else ('branch', 'source_commit')
    if (data.get('task') != SCHEMAS[stage]['properties']['task']['enum'][0]
            or any(type(data.get(k)) is not str or data[k] != context.get(k) for k in identity)
            or type(data.get('report_markdown')) is not str or not data['report_markdown'].strip()):
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


def cell(value):
    """Escape metadata, not the original report bodies."""
    return html.escape(str(value)).replace('|', '&#124;').replace('\r', ' ').replace('\n', ' ')


def render_final_report(manifest, source, mode, language='Russian'):
    ru = isinstance(language, str) and language.lower() in ('russian', 'ru', 'русский')
    def t(russian, english):
        return russian if ru else english
    entries = report_entries(manifest, source, mode)
    useful = any(usable_study(b) for b in entries)
    lines = ['# ' + t('Итоговый отчёт об архитектуре', 'Final architecture report'), '',
             t('Статус запуска: ', 'Run status: ') + manifest['status'], '',
             t('Сводка собрана программой из доступных результатов. Исходные тексты сохранены без исправлений.',
               'Assembled by the orchestrator from available results. Original texts are preserved unchanged.'), '']
    if not useful:
        lines += ['> ' + t('Диагностическая сводка: пригодное исследование отсутствует.',
                           'Diagnostic summary: no usable architecture study is available.'), '']
    elif manifest['status'] != 'COMPLETE':
        lines += ['> ' + t('Документ содержит ограничения. Наличие текста не означает прохождение строгой приёмки.',
                           'This document has limitations. Available text does not imply strict acceptance.'), '']
    if manifest.get('critical_failure'):
        lines += ['> ' + t('Критическая ошибка запуска. Ниже могут быть только результаты ранее завершённых этапов; '
                           'целостность и восстановление проверяйте по диагностике.',
                           'Critical run failure. Only previously completed stages may be available below; '
                           'consult diagnostics for source integrity and restoration.'), '']
    lines += ['## ' + t('Покрытие', 'Coverage'), '',
              t('| Источник | Снимок | Исследование | Ревью | Приёмка |',
                '| Source | Snapshot | Study | Review | Acceptance |'), '| --- | --- | --- | --- | --- |']
    def state(item, stage):
        data = item.get(stage)
        if data:
            return data['completion_status'] + (' / ' + data['verdict'] if stage == 'review' else '')
        return t('Текст с нарушениями контракта', 'Text with contract violations') if stage_document(item, stage) else t('Нет результата', 'Unavailable')
    for b in entries:
        lines.append('| ' + ' | '.join(cell(x) for x in (
            b.get('branch', manifest.get('source_directory', source.get('path', ''))),
            b.get('source_commit', b.get('source_fingerprint')) or t('Не определён', 'Unknown'),
            state(b, 'study'), state(b, 'review'), 'PASS' if accepted(b) else t('Не пройдена', 'Not accepted'))) + ' |')
    lines += ['', '## ' + t('Сравнение', 'Comparison'), '']
    comparison = manifest.get('comparison')
    if comparison and comparison['completion_status'] != 'BLOCKED':
        if comparison['completion_status'] != 'COMPLETE':
            lines += ['> ' + t('Сравнение неполное; учитывайте неподтверждённые различия и ограничения.',
                               'Comparison is incomplete; observe unverified differences and limitations.'), '']
        lines += [comparison['report_markdown'], '']
    else:
        lines += [t('Содержательное сравнение отсутствует. Для каталога или одной ветки оно не требуется; '
                    'для нескольких веток причины приведены в покрытии и диагностике.',
                    'No substantive comparison is available. Folder and single-branch runs do not require one; '
                    'for multiple branches, see coverage and diagnostics.'), '']
        if comparison:
            lines += [comparison['report_markdown'], '']
    followups = []
    for b in entries:
        name = b.get('branch', manifest.get('source_directory', source.get('path', '')))
        lines += ['## ' + cell(name), '', '### ' + t('Оговорки и расхождения', 'Caveats and discrepancies'), '']
        if not b.get('review') or b['review']['completion_status'] != 'COMPLETE':
            lines += ['> ' + t('Независимая проверка не завершена. Утверждения исследования не подтверждены полным ревью.',
                               'Independent verification is incomplete. Study claims are not confirmed by a complete review.'), '']
        elif b['review']['verdict'] != 'PASS':
            lines += ['> ' + t('Ревью не приняло документ: ', 'Review did not accept the document: ') + b['review']['verdict'], '']
        for stage in ('study', 'review'):
            material = b.get(stage + '_material')
            if material:
                lines += ['- ' + stage + ': ' + t('текст не прошёл строгую проверку контракта.',
                                                   'text did not pass strict contract validation.')]
                diagnostics = material['validation_issues']
                for group in ('schema_diagnostics', 'semantic_diagnostics'):
                    for issue in diagnostics[group]['violations']:
                        detail = issue.get('message', issue.get('violation', ''))
                        if issue.get('missing_keys'):
                            detail += ': ' + ', '.join(issue['missing_keys'])
                        if issue.get('extra_key_count'):
                            detail += f"; extra fields: {issue['extra_key_count']}"
                        lines += ['- ' + stage + ' ' + cell(issue['path']) + ': ' + cell(detail)]
                    if diagnostics[group]['truncated']:
                        lines += ['- ' + t('Полный счёт нарушений: ', 'Total violation count: ') + str(diagnostics[group]['total_violations'])]
            data = stage_document(b, stage)
            if data:
                for limitation in data.get('limitations', []):
                    lines += ['- ' + stage + ': ' + cell(limitation)]
        ledger = b.get('review') or (b.get('review_material') or {}).get('review_ledger')
        if ledger:
            # IDs and links below are quoted observations, not reconstructed relations.
            for c in ledger['claims']:
                if c['outcome'] != 'SUPPORTED':
                    lines += ['- ' + cell(c['id']) + ' — ' + cell(c['outcome']) + ': ' + cell(c['statement'])
                              + ' (' + cell(c['location']) + '). ' + cell(c['limitation'])
                              + t(' Указанные замечания: ', ' Reported finding links: ') + cell(', '.join(c['finding_ids']) or '—')]
            for f in ledger['findings']:
                lines += ['- ' + cell(f['id']) + ' / ' + f['severity'] + ': ' + cell(f['impact']) + ' '
                          + t('Предложенная правка: ', 'Proposed correction: ') + cell(f['proposed_correction'])]
        if not accepted(b):
            followups.append(t('Завершить независимую проверку и устранить перечисленные расхождения: ',
                               'Complete independent verification and resolve listed discrepancies: ') + cell(name))
        for stage, title in (('study', t('Исходное исследование', 'Original study')),
                             ('review', t('Исходное ревью', 'Original review'))):
            lines += ['', '### ' + title, '']
            doc = stage_document(b, stage)
            lines += [doc['report_markdown'] if doc else t('Завершённый пригодный текст отсутствует.', 'No completed usable text is available.'), '']
    lines += ['## ' + t('Непроверенные области и дальнейшие проверки', 'Unverified areas and follow-up checks'), '']
    lines += ['- ' + text for text in followups] or [t('Дополнительных ограничений приёмки не выявлено.', 'No additional acceptance limitations identified.')]
    lines += ['', '## ' + t('Диагностика запуска', 'Run diagnostics'), '']
    # Diagnostics are already sanitized. Never copy raw provider errors or prompts.
    for d in manifest.get('diagnostics', []):
        lines += ['- ' + cell(d.get('branch', d.get('phase', ''))) + ' / ' + cell(d.get('stage', ''))
                  + ': ' + cell(d.get('failure_kind') or d.get('code')) + ' — ' + cell(d.get('message', ''))]
    if not manifest.get('diagnostics'):
        lines += [t('Ошибок выполнения не зарегистрировано.', 'No execution errors recorded.')]
    return '\n'.join(lines).rstrip() + '\n', useful
