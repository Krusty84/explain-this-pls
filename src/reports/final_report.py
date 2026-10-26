# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Deterministic report assembly. No model calls or promotion of recovered text."""
from __future__ import annotations

from src.contracts.contracts import (SCHEMAS, accepted, workflow_satisfied, processing_satisfied,
                                     has_program_checks, schema_diagnostics)


def recoverable_material(stage, data, context, mode, diagnostics):
    """Minimum document contract; transport and source guards are the caller's job."""
    if stage not in ('study', 'review') or type(data) is not dict:
        return None
    identity = ('source_directory', 'source_fingerprint') if mode == 'folder' else ('branch', 'source_commit')
    if stage == 'study':
        from src.reports.document_rendering import recover_sections
        narrative = recover_sections(data.get('report_sections'))
        narrative_origin = 'PROGRAM_ASSEMBLED_AUTHOR_BLOCKS'
    else:
        narrative = data.get('report_markdown')
        narrative_origin = 'MODEL_MARKDOWN'
    if (data.get('task') != SCHEMAS[stage]['properties']['task']['enum'][0]
            or any(type(data.get(k)) is not str or data[k] != context.get(k) for k in identity)
            or type(narrative) is not str or not narrative.strip()):
        return None
    if stage == 'review' and 'target' in data and data['target'] != context.get('review_target'):
        return None
    material = {k: data[k] for k in ('task', *identity)}
    material.update(report_markdown=narrative, narrative_origin=narrative_origin)
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
    from src.reports.presentation import validate_report
    document = item.get(stage) or item.get(stage + '_material')
    if document is not None:
        validate_report(stage, document)
    return document


def usable_study(item):
    doc = stage_document(item, 'study')
    return bool(doc and doc.get('completion_status') != 'BLOCKED')


def report_entries(manifest, source, mode):
    if mode == 'folder':
        return [manifest]
    entries = {b['branch']: b for b in manifest.get('branches', [])}
    labels = source['branches'] + [b for b in entries if b not in source['branches']]
    return [entries.get(branch, {'branch': branch, 'source_commit': manifest.get('pins', {}).get(branch),
                                'errors': []}) for branch in labels]


def comparison_possible(entries, baseline):
    return (any(b['branch'] == baseline and usable_study(b) for b in entries)
            and any(b['branch'] != baseline and usable_study(b) for b in entries))


def investigation_boundaries(entry, study, review, language):
    """Show retained agent statements beside program observations; infer no coverage."""
    from src.reports.presentation import cell, render_coverage, russian
    from src.reports.review import review_searches
    ru = russian(language)
    def t(r, e): return r if ru else e
    out = ['### ' + t('Границы исследования — оценки агента', 'Investigation boundaries — agent reports'), '']
    scope = [b for b in (study or {}).get('block_map', []) if b['section_key'] == 'scope']
    if scope:
        document = study['report_markdown'].split('\n')
        for block in scope:
            loc = block['locator']
            out += ['> ' + cell(line) for line in document[loc['start_line'] - 1:loc['end_line']]]
            out += ['']
    else:
        out += [t('Описание исследованных сценариев недоступно; полнота не установлена.',
                  'Investigated scenario descriptions are unavailable; completeness is not established.'), '']
    unknowns = [c for c in (study or {}).get('claims', []) if c['epistemic_kind'] in ('HYPOTHESIS', 'UNKNOWN')]
    if unknowns:
        out += ['**' + t('Гипотезы и неизвестное — оценки автора', 'Hypotheses and unknowns — author assessments') + '**', '',
                '| ' + t('ID / тип | Утверждение / область | Неопределённость и следующая проверка',
                          'ID / kind | Statement / scope | Uncertainty and next check') + ' |', '| --- | --- | --- |']
        out += ['| ' + ' | '.join(cell(x) for x in (c['id'] + ' / ' + c['epistemic_kind'],
                  c['statement'] + ' / ' + c['scope'], c['uncertainty'])) + ' |' for c in unknowns]
        out += ['']
    if review and 'review_plan' in review and 'omission_search' in review:
        out += review_searches(review, language)
    out += ['### ' + t('Фильтр исходников — данные программы', 'Source filter — program records'), '']
    settings = entry.get('source_filter')
    if settings is None:
        out += [t('Данные фильтра недоступны; отсутствие исключений не установлено.',
                  'Filter records are unavailable; absence of exclusions is not established.'), '']
    else:
        out += ['follow_gitignore: ' + str(settings['follow_gitignore']).lower(), '',
                t('Исключения пользователя: ', 'User exclusions: ') +
                (cell(', '.join(settings['exclude_paths'])) or t('не заданы', 'none configured')) + '.', '',
                t('Git mode также применяет правила Git. Служебные .git и цели символических ссылок исключены.',
                  'Git mode also applies Git rules. Administrative .git entries and symlink targets are excluded.'), '']
        if settings.get('exclusions'):
            out += ['| ' + t('Исключённый путь / корень | Основание', 'Excluded path / root | Origin') + ' |', '| --- | --- |']
            out += ['| ' + cell(e['path']) + ' | ' + cell(e['origin']) + ' |' for e in settings['exclusions']]
            out += ['']
    plan = (study or {}).get('review_plan', {}).get('coverage_plan') or entry.get('coverage_plan')
    coverage = render_coverage(plan, (study or {}).get('program_checks', {}).get('coverage'), language)
    out += ['\n'.join('#' + line if line.startswith('## ') else line for line in coverage.split('\n')), '',
            '### ' + t('Доставка исходного текста — наблюдения программы', 'Source text delivery — program observations'), '']
    for stage in ('study', 'review'):
        meta = entry.get(stage + '_invocation') or {}
        observations = meta.get('phase_invocations', []) + [meta]
        for observation in observations:
            if not observation or observation.get('phase') == 'write':
                continue
            observed = observation.get('tool_observation', {})
            delivered = bool(observed.get('delivery', {}).get('unique_lines') or
                             observed.get('mcp', {}).get('text_utf8_bytes'))
            out += ['- ' + stage + (' / ' + observation['phase'] if observation.get('phase') else '') + ': ' +
                    (t('доставка текста наблюдалась; INSPECTED_UNVERIFIED.',
                       'source text delivery observed; INSPECTED_UNVERIFIED.') if delivered else
                     t('доставка текста не установлена по сохранённым наблюдениям; исследование UNVERIFIED.',
                       'source text delivery is not established by retained observations; inspection UNVERIFIED.'))]
    out += ['', t('NOT_CITED не означает NOT_INSPECTED. INSPECTED_UNVERIFIED не означает содержательную проверку. '
                  'Доставка текста не доказывает понимания. FACT по исходникам не означает runtime verification. '
                  'Отсутствие контрпримера не доказывает отсутствие альтернативного пути.',
                  'NOT_CITED does not mean NOT_INSPECTED. INSPECTED_UNVERIFIED does not mean semantic verification. '
                  'Text delivery does not prove understanding. A source-based FACT does not mean runtime verification. '
                  'No counterexample does not prove that no alternative path exists.'), '']
    return out


def render_final_report(manifest, source, mode, language='Russian', *, available_files=None):
    from pathlib import Path
    from src.reports.navigation import anchor, link
    from src.reports.presentation import cell, label, russian
    from src.reports.review import ordered_findings, review_summary
    ru = russian(language)
    def t(r, e): return r if ru else e
    entries = report_entries(manifest, source, mode)
    useful = any(usable_study(b) for b in entries)
    review_enabled = manifest.get('review_enabled', True)
    available = {Path(path).as_posix() for path in available_files} if available_files is not None else {'manifest.json'}
    if available_files is None:
        for entry in entries:
            states = [entry] + entry.get('revisions', [])
            for state in states:
                for stage in ('study', 'review'):
                    meta = state.get(stage + '_invocation') or {}
                    names = set(meta.get('artifact_hashes', {})) | set(meta.get('material_hashes', {}))
                    available.update((Path(state.get('directory', '.')) / name).as_posix() for name in names)
        meta = manifest.get('comparison_invocation') or {}
        available.update('comparison/' + name for name in meta.get('artifact_hashes', {}))
    def target(directory, name): return (Path(directory) / name).as_posix()
    def reference(title, directory, name, fragment=''):
        path = target(directory, name)
        return link(title, path, fragment) if path in available else ''
    def materials(directory):
        names = [('ARCHITECTURE.md', t('Архитектура', 'Architecture')),
                 ('ARCHITECTURE_REVIEW.md', t('Ревью', 'Review')),
                 ('study.annotated.md', t('Реестр исследования', 'Study registry')),
                 ('review.annotated.md', t('Реестр ревью', 'Review registry')),
                 ('study.original.md', t('Авторские блоки', 'Authored blocks')),
                 ('review.original.md', t('Исходный текст ревью', 'Original review text')),
                 ('study.material.json', t('Восстановленное исследование', 'Recovered study')),
                 ('review.material.json', t('Восстановленное ревью', 'Recovered review'))]
        return ' · '.join(value for name, title in names if (value := reference(title, directory, name)))
    out = ['# ' + t('Сводка исследования', 'Study summary'), '',
           t('Статус запуска: ', 'Run status: ') + label(manifest['status'], language) + '.', '',
           t('Пригодный результат: ', 'Usable result: ') + (t('есть', 'available') if useful else t('отсутствует', 'unavailable')) + '.', '',
           t('Проверки политики не доказывают фактическую правильность отчёта. Качество содержательного ревью не измерено.',
             'Policy checks do not establish factual correctness. Semantic review quality is not measured.'), '',
           t('Успешное ревью не устанавливает безопасность изменения кода.',
             'Successful review does not establish that code changes are safe.'), '']
    if not useful:
        out += ['> ' + t('Диагностическая сводка: пригодное исследование отсутствует.',
                          'Diagnostic summary: no usable study is available.'), '']
    if manifest.get('critical_failure'):
        out += ['> ' + t('Критическая ошибка запуска; проверьте диагностику целостности, восстановления и очистки.',
                          'Critical run failure; consult integrity, restoration, and cleanup diagnostics.'), '']
    if not review_enabled:
        out += ['> ' + t('Ревью отключено конфигурацией; отдельная проверка не выполнена.',
                          'Review is disabled by configuration; no separate review was performed.'), '']
    documents = [stage_document(state, stage) for entry in entries
                 for state in [entry] + entry.get('revisions', []) for stage in ('study', 'review')]
    if any(doc and (doc.get('strict_valid') is False or not has_program_checks(doc)) for doc in documents):
        out += ['> ' + t('Есть восстановленные результаты; их проверки политики не завершены.',
                          'Recovered results are present; their policy checks are not complete.'), '']
    if any(doc and doc.get('program_checks', {}).get('citation_warnings') for doc in documents):
        out += ['> ' + t('Есть предупреждения о цитатах. Проверьте ограничения перед использованием отчёта.',
                          'Citation warnings are present. Read the limitations before using the report.'), '']
    if review_enabled and any(not entry.get('review') or entry['review'].get('completion_status') != 'COMPLETE'
                              for entry in entries):
        out += ['> ' + t('Есть результаты без завершённого ревью.', 'Some results have no completed review.'), '']
    for entry in entries:
        directory = entry.get('directory', '.')
        selected_id = entry.get('selected_revision')
        selected = next((r for r in entry.get('revisions', []) if r['revision_id'] == selected_id), entry)
        artifact_directory = (directory if entry.get('selection_publication_complete') and not manifest.get('critical_failure')
                              else selected.get('directory', directory))
        study = stage_document(selected, 'study') or stage_document(entry, 'study')
        review = stage_document(entry, 'review') or stage_document(selected, 'review')
        review_directory = artifact_directory if entry.get('review') else selected.get('directory', directory)
        out += ['## ' + cell(entry.get('branch', manifest.get('source_directory', source.get('path', '')))), '',
                t('Выбранная ревизия: ', 'Selected revision: ') + cell(selected_id or t('не выбрана', 'none')) + '.', '']
        identity = entry.get('source_commit') or entry.get('source_fingerprint')
        if identity:
            out += [t('Идентичность исходников: ', 'Source identity: ') + cell(identity), '']
        out += [t('Исследование: ', 'Study: ') + label((study or {}).get('completion_status') or 'MISSING', language) + '.', '',
                t('Ревью: ', 'Review: ') + (t('отключено', 'disabled') if not review_enabled else
                    label((review or {}).get('completion_status') or 'MISSING', language)) + '.', '']
        out += ['> ' + review_summary(entry, language, review_enabled=review_enabled), '']
        if manifest.get('critical_failure'):
            out += [t('Приёмка запуска не пройдена из-за критической ошибки.',
                      'Run acceptance failed due to a critical error.'), '']
        elif accepted(entry):
            out += [t('Проверки политики обработки и ревью пройдены.', 'Processing and review policy checks satisfied.'), '']
        elif workflow_satisfied(entry, review_enabled=review_enabled):
            out += [t('Проверки включённых этапов пройдены; отдельного ревью не было.',
                      'Enabled-stage checks satisfied; no separate review was performed.'), '']
        elif processing_satisfied(entry, review_enabled=review_enabled):
            out += ['> ' + t('Обработка продолжена с предупреждениями о цитатах; строгая приёмка не пройдена.',
                              'Processing continued with citation warnings; strict acceptance has not passed.'), '']
        else:
            out += [t('Условия политики не выполнены.', 'Policy checks not satisfied.'), '']
        findings = ordered_findings(review or {})
        if not findings and review:
            findings = ordered_findings(review.get('review_observations', {}))
        out += [t('Замечания по важности: ', 'Findings by severity: ') + '; '.join(
            label(severity, language) + ': ' + str(sum(f['severity'] == severity for f in findings))
            for severity in ('HIGH', 'MEDIUM', 'LOW')) + '.', '']
        for finding in findings[:5]:
            title = label(finding['severity'], language) + ': ' + cell(finding['impact'])
            destination = reference(title, review_directory, 'ARCHITECTURE_REVIEW.md', anchor('finding', finding['id']))
            out += ['- ' + (destination or title)]
        full = reference(t('Полный список замечаний', 'Full findings'), review_directory, 'ARCHITECTURE_REVIEW.md')
        out += ['', t('Остальные замечания: ', 'Remaining findings: ') + str(max(0, len(findings) - 5)) +
                ('. ' + full if full else '.'), '']
        out += investigation_boundaries(selected | {'source_filter': entry.get('source_filter')}, study, review, language)
        # Include limitations from every retained attempt, without copying its narrative or registry.
        seen = set()
        for state in [entry] + entry.get('revisions', []):
            for stage in ('study', 'review'):
                doc = stage_document(state, stage)
                if not doc:
                    continue
                revision_id = doc.get('revision_id') or state.get('revision_id') or selected_id
                key = (stage, revision_id, doc.get('report_markdown'))
                if key in seen:
                    continue
                seen.add(key)
                warnings = doc.get('program_checks', {}).get('citation_warnings', [])
                if doc.get('limitations') or warnings or doc.get('strict_valid') is False:
                    out += ['**' + t('Ограничения ревизии ', 'Limitations for revision ') + cell(revision_id or '—') +
                            ' — ' + (t('исследование', 'study') if stage == 'study' else t('ревью', 'review')) + ':**', '']
                if not has_program_checks(doc) or doc.get('strict_valid') is False:
                    out += ['> ' + t('Результат восстановлен после ошибки; проверки политики не завершены.',
                                      'Result recovered after an error; policy checks are not complete.'), '']
                    if doc.get('contract_failure'):
                        out += ['- ' + cell(doc['contract_failure']['message'])]
                if warnings:
                    out += ['> ' + t('Предупреждения о цитатах: ', 'Citation warnings: ') + str(len(warnings)) +
                            t('. Проверьте исправления и неподтверждённые цитаты в подробных материалах.',
                              '. Check corrected and unresolved citations in the detailed material.'), '']
                out += ['- ' + cell(issue) for issue in doc.get('limitations', [])]
        links = materials(artifact_directory)
        if review_directory != artifact_directory:
            review_links = ' · '.join(value for name, title in (
                ('ARCHITECTURE_REVIEW.md', t('Ревью выбранной ревизии', 'Selected revision review')),
                ('review.annotated.md', t('Реестр ревью', 'Review registry')),
                ('review.material.json', t('Восстановленное ревью', 'Recovered review')))
                if (value := reference(title, review_directory, name)))
            links += (' · ' if links and review_links else '') + review_links
        if links:
            out += ['', links, '']
        for revision in entry.get('revisions', []):
            if revision['revision_id'] == selected_id:
                continue
            details = materials(revision.get('directory', directory + '/revisions/' + revision['revision_id']))
            state = (revision.get('review') or {}).get('completion_status')
            out += [t('Другая ревизия ', 'Other revision ') + cell(revision['revision_id']) + ': ' +
                    label(state or 'MISSING', language) + ('. ' + details if details else '.'), '']
    comparison = manifest.get('comparison')
    out += ['## ' + t('Сравнение', 'Comparison'), '']
    comp_link = reference(t('Сравнение веток и основания различий', 'Branch comparison and supporting evidence'),
                          'comparison', 'BRANCH_COMPARISON.md')
    if comparison:
        out += [t('Состояние: ', 'State: ') + label(comparison['completion_status'], language) + '. ' + comp_link, '']
        out += ['- ' + cell(x) for x in comparison['limitations']]
        if comparison.get('program_checks', {}).get('citation_warnings'):
            out += ['> ' + t('В сравнении есть предупреждения о цитатах.', 'Comparison has citation warnings.'), '']
    else:
        out += [t('Сравнение отсутствует; это не означает отсутствия различий. Для каталога и одной ветки оно не требуется.',
                  'Comparison unavailable; this does not imply no differences. Folder and single-branch runs do not require it.'), '']
    out += ['## ' + t('Диагностика и история', 'Diagnostics and history'), '']
    for diagnostic in manifest.get('diagnostics', []):
        codes = dict.fromkeys(diagnostic[key] for key in ('failure_kind', 'code') if diagnostic.get(key))
        out += ['- ' + cell(' / '.join(codes)) + ': ' + cell(diagnostic.get('message', ''))]
    if not manifest.get('diagnostics'):
        out += [t('Ошибок выполнения не зарегистрировано.', 'No execution errors recorded.'), '']
    details = reference(t('Полная диагностика, история ревизий и результаты проверок', 'Full diagnostics, revision history and check results'), '.', 'manifest.json')
    if details:
        out += [details, '']
    return '\n'.join(out).rstrip() + '\n', useful
