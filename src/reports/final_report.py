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


def render_final_report(manifest, source, mode, language='Russian', *, available_files=None):
    from pathlib import Path
    from src.reports.document_rendering import summary_paragraph
    from src.reports.navigation import anchor, link
    from src.reports.presentation import cell, label, russian
    from src.reports.review import ordered_findings, review_outcome

    ru = russian(language)
    def t(r, e): return r if ru else e

    entries = report_entries(manifest, source, mode)
    useful = any(usable_study(entry) for entry in entries)
    review_enabled = manifest.get('review_enabled', True)
    critical = manifest.get('critical_failure')
    compare_branches = mode == 'git' and len(source['branches']) > 1
    available = {Path(path).as_posix() for path in available_files} if available_files is not None else {'manifest.json'}
    if available_files is None:
        for entry in entries:
            for state in [entry] + entry.get('revisions', []):
                for stage in ('study', 'review'):
                    meta = state.get(stage + '_invocation') or {}
                    names = set(meta.get('artifact_hashes', {})) | set(meta.get('material_hashes', {}))
                    available.update((Path(state.get('directory', '.')) / name).as_posix() for name in names)
        meta = manifest.get('comparison_invocation') or {}
        available.update('comparison/' + name for name in meta.get('artifact_hashes', {}))

    def reference(title, directory, name, fragment=''):
        path = (Path(directory) / name).as_posix()
        return link(title, path, fragment) if path in available else ''

    def report_links(directory, review_directory, fragment=''):
        architecture = (reference(t('Архитектура', 'Architecture'), directory, 'ARCHITECTURE.md', fragment)
                        or reference(t('Восстановленное исследование', 'Recovered study'), directory,
                                     'study.material.json'))
        review = (reference(t('Ревью', 'Review'), review_directory, 'ARCHITECTURE_REVIEW.md')
                  or reference(t('Восстановленное ревью', 'Recovered review'),
                               review_directory, 'review.material.json')) if review_directory is not None else ''
        return ' · '.join(filter(None, (architecture, review)))

    def status(document):
        value = (document or {}).get('completion_status')
        if not value:
            return t('отсутствует', 'Missing')
        if value == 'PARTIAL':
            return t('частичное', 'Partial')
        translated = label(value, language)
        return translated.lower() if ru else translated

    out = ['# ' + t('Сводка исследования', 'Study summary'), '',
           t('Метод — статический анализ исходников.', 'Method — static analysis of source code.'), '']
    if not useful:
        out += [t('Диагностическая сводка — пригодное исследование отсутствует.',
                  'Diagnostic summary — no usable study available.'), '']
    if critical:
        out += [t('Критическая ошибка запуска.', 'Critical run failure.'), '']
    elif compare_branches or manifest['status'] == 'FAILED':
        out += [t('Статус запуска — ', 'Run status — ') + label(manifest['status'], language) + '.', '']

    history = []
    selected_ids = {}
    for entry in entries:
        directory = entry.get('directory', '.')
        selected_id = entry.get('selected_revision')
        selected = next((r for r in entry.get('revisions', []) if r['revision_id'] == selected_id), entry)
        artifact_directory = (directory if entry.get('selection_publication_complete') and not critical
                              else selected.get('directory', directory))
        study = stage_document(selected, 'study') or stage_document(entry, 'study')
        review = (stage_document(entry, 'review') or stage_document(selected, 'review')) if review_enabled else None
        review_directory = artifact_directory if entry.get('review') else selected.get('directory', directory)
        name = cell(entry.get('branch', manifest.get('source_directory', source.get('path', ''))))
        selected_ids[entry.get('branch', 'folder')] = selected_id
        out += ['## ' + name, '']

        fragment = ''
        if study and study.get('block_map'):
            fragment = anchor('section', 'context', study)
            block = next((b for b in study['block_map'] if b['section_key'] == 'context'), None)
            if (block and block['claim_ids'] and study.get('completion_status') != 'BLOCKED'
                    and (Path(artifact_directory) / 'ARCHITECTURE.md').as_posix() in available):
                intro = summary_paragraph(block['locator']['quote'])
                if intro:
                    out += [intro, '']
                    fragment = anchor('block', block['id'], study)
        links = report_links(artifact_directory, review_directory if review_enabled else None, fragment)
        if links:
            out += [links, '']

        if critical:
            acceptance = t('не пройдена', 'Not passed')
        elif review_enabled and accepted(entry):
            acceptance = t('пройдена', 'Passed')
        elif workflow_satisfied(entry, review_enabled=review_enabled):
            acceptance = t('проверки исследования пройдены', 'Study checks passed')
        elif processing_satisfied(entry, review_enabled=review_enabled):
            acceptance = t('не пройдена (обработка продолжена)', 'Not passed (processing continued)')
        else:
            acceptance = t('не пройдена', 'Not passed')
        if not review_enabled:
            review_status = t('отключено', 'Disabled')
        elif (review or {}).get('completion_status') == 'COMPLETE' and review_outcome(entry) == 'INCOMPLETE':
            review_status = t('не завершено', 'Incomplete')
        else:
            review_status = status(review)
        out += [' · '.join((t('Исследование — ', 'Study — ') + status(study),
                            t('Ревью — ', 'Review — ') + review_status,
                            t('Приёмка — ', 'Acceptance — ') + acceptance)) + '.', '']
        identity = entry.get('source_commit') or entry.get('source_fingerprint')
        metadata = []
        if selected_id:
            metadata.append(t('Ревизия ', 'Revision ') + cell(selected_id))
        if identity:
            metadata.append(cell(identity))
        if metadata:
            out += [' · '.join(metadata), '']

        findings = ordered_findings(review or {})
        if not findings and review:
            findings = ordered_findings(review.get('review_observations', {}))
        if findings:
            for finding in findings[:5]:
                title = label(finding['severity'], language) + ' — ' + cell(finding['impact'])
                destination = reference(title, review_directory, 'ARCHITECTURE_REVIEW.md', anchor('finding', finding['id']))
                out += ['- ' + (destination or title)]
            if len(findings) > 5:
                extra = len(findings) - 5
                out += ['- ' + t(f'Ещё замечаний — {extra}.', f'More findings — {extra}.')]
            out += ['']

        for title, document in ((t('Исследование', 'Study'), study), (t('Ревью', 'Review'), review)):
            if not document:
                continue
            if not has_program_checks(document):
                out += [title + t(' — результат восстановлен после ошибки.', ' — result recovered after an error.'), '']
            warnings = document.get('program_checks', {}).get('citation_warnings', [])
            if warnings:
                out += [title + t(f' — предупреждения о цитатах ({len(warnings)}).',
                                  f' — citation warnings ({len(warnings)}).'), '']

        registries = [
            reference(t('Реестр исследования', 'Study registry'), artifact_directory, 'study.annotated.md')
            or reference(t('Авторские блоки', 'Authored blocks'), artifact_directory, 'study.original.md'),
        ]
        if review_enabled:
            registries.append(reference(t('Реестр ревью', 'Review registry'), review_directory, 'review.annotated.md')
                              or reference(t('Исходный текст ревью', 'Original review text'), review_directory,
                                           'review.original.md'))
        registry_links = ' · '.join(filter(None, registries))
        source_history = [registry_links] if registry_links else []
        for revision in entry.get('revisions', []):
            if revision['revision_id'] != selected_id:
                previous = revision.get('directory', directory + '/revisions/' + revision['revision_id'])
                links = report_links(previous, previous)
                if links:
                    source_history.append(t('Ревизия ', 'Revision ') + cell(revision['revision_id']) + ' · ' + links)
        if source_history:
            if len(entries) > 1:
                history += ['- ' + name] + ['  - ' + item for item in source_history]
            else:
                history += ['- ' + item for item in source_history]

    if compare_branches:
        out += ['## ' + t('Сравнение', 'Comparison'), '']
        comparison = manifest.get('comparison')
        if comparison:
            comparison_link = reference(t('Сравнение веток', 'Branch comparison'),
                                        'comparison', 'BRANCH_COMPARISON.md')
            comparison_status = status(comparison)
            out += [' · '.join(filter(None, (comparison_status, comparison_link))), '']
            if comparison.get('program_checks', {}).get('citation_warnings'):
                out += [t('Сравнение — предупреждения о цитатах.', 'Comparison — citation warnings.'), '']
        else:
            out += [t('Сравнение недоступно.', 'Comparison unavailable.'), '']

    details = reference(t('Манифест запуска', 'Run manifest'), '.', 'manifest.json')
    diagnostics = []
    seen = set()
    for diagnostic in manifest.get('diagnostics', []):
        branch, revision = diagnostic.get('branch'), diagnostic.get('revision_id')
        selected_id = selected_ids.get(branch)
        if not critical and selected_id and revision and selected_id != revision:
            continue
        codes = ' / '.join(dict.fromkeys(diagnostic[key] for key in ('failure_kind', 'code') if diagnostic.get(key)))
        message = diagnostic.get('message', '')
        context = ' / '.join(filter(None, (branch if len(entries) > 1 else None, diagnostic.get('stage'), revision)))
        line = ' — '.join(cell(part) for part in (context, codes, message) if part)
        if line and line not in seen:
            seen.add(line)
            diagnostics.append(line)
    if diagnostics or history or details:
        out += ['## ' + t('Диагностика и история', 'Diagnostics and history'), '']
        if diagnostics:
            out += ['- ' + diagnostic for diagnostic in diagnostics] + ['']
        if history:
            out += history + ['']
        if details:
            out += [details, '']
    return '\n'.join(out).rstrip() + '\n', useful
