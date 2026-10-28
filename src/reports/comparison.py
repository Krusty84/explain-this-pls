# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Compare existing statements and evidence without inferring new conclusions."""
from src.contracts.contracts import contract_violation
from src.reports.navigation import link
from src.reports.presentation import cell, label, russian, render_citation_warnings, source_reference, validate_report
from src.reports.review import evidence_context, claim_places
from src.runtime.artifacts import slug


def render_comparison(data, language='English', context=None, *, source_root=None, editor='vscode'):
    validate_report('compare', data)
    ru = russian(language)
    def t(r, e): return r if ru else e
    entries = {b['branch']: b for b in (context or {}).get('branches', [])}
    out = ['# ' + t('Сравнение веток', 'Branch comparison'), '',
           t('Сравнение выполнено только по предоставленным отчётам. Исходники на этом этапе не исследовались.',
             'Comparison uses supplied reports only. Sources were not inspected in this stage.'), '',
           t('Состояние: ', 'State: ') + label(data['completion_status'], language), '',
           t('Проверки политики не устанавливают фактическую правильность сравнения.',
             'Policy checks do not establish factual correctness of the comparison.'), '']
    out += ['- ' + cell(x) for x in data['limitations']]
    if data['unresolved_branches']:
        out += ['', t('Неразрешённые входные данные: ', 'Unresolved inputs: ') +
                ', '.join(cell(b) for b in data['unresolved_branches']), '']
    if not data['differences']:
        out += [t('Агент не указал различий. Это не доказывает их отсутствие.',
                  'The agent reported no differences. This does not establish their absence.'), '']
    for index, diff in enumerate(data['differences'], 1):
        out += [f'## {index}. ' + cell(diff['category']), '',
                '**' + t('Категория:', 'Category:') + '** ' + cell(diff['category']), '',
                '**' + t('Подтверждение:', 'Confirmation:') + '** ' + label(diff['classification'], language), '']
        for branch, statement, baseline in ((data['baseline_branch'], diff['baseline_statement'], True),
                                            (diff['branch'], diff['branch_statement'], False)):
            entry = entries.get(branch, {})
            study = entry.get('study')
            revision = (study or {}).get('revision_id') or entry.get('selected_revision')
            commit = (study or {}).get('source_commit') or (data['baseline_commit'] if baseline else entry.get('source_commit'))
            heading = t('Базовая ветка: ', 'Baseline: ') if baseline else t('Ветка: ', 'Branch: ')
            out += ['### ' + heading + cell(branch), '',
                    t('Ревизия отчёта: ', 'Report revision: ') + cell(revision or t('Недоступна', 'Unavailable')) +
                    '; ' + t('коммит: ', 'commit: ') + cell(commit or t('Недоступен', 'Unavailable')), '',
                    cell(statement), '', '**' + t('Основания:', 'Evidence:') + '**', '']
            refs = [ref for ref in diff['evidence_refs'] if ref['branch'] == branch]
            if not refs:
                out += [t('Ограничение: основания этой стороны не указаны.', 'Limitation: no evidence supplied for this side.'), '']
            for ref in refs:
                material = entry.get(ref['artifact'])
                if not study or not material:
                    out += [t('Ограничение: связанный отчёт недоступен.', 'Limitation: referenced report unavailable.'), '']
                    continue
                plan = study['review_plan']
                if (ref['revision_id'] != study['revision_id'] or ref['document_sha256'] != plan['document_sha256']
                        or ref['registry_sha256'] != plan['registry_sha256']):
                    raise contract_violation('REVIEW_TARGET_CHANGED', '$.differences.evidence_refs')
                claim = next(c for c in study['claims'] if c['id'] == ref['claim_id'])
                root = '../branches/' + slug(branch) + '/revisions/' + ref['revision_id'] + '/'
                refs_for_claim = list(claim['evidence_ids'])
                if ref['artifact'] == 'review':
                    reply = next(c for c in material['claims'] if c['id'] == ref['claim_id'])
                    refs_for_claim += reply['evidence_ids']
                records = evidence_context(material, study if ref['artifact'] == 'review' else None)
                out += ['- ' + cell(claim['statement']) + ' — ' + ', '.join(claim_places([claim['id']], study, language,
                                                                                       root + 'ARCHITECTURE.md'))]
                for identifier in dict.fromkeys(refs_for_claim):
                    record = records.get(identifier)
                    if record is None:
                        out += ['- ' + t('Контекст источника недоступен: ', 'Source context unavailable: ') + cell(identifier)]
                        continue
                    target = root + ('ARCHITECTURE.md' if identifier.startswith('study:') else 'review.annotated.md')
                    document = study if identifier.startswith('study:') else None
                    out += ['- ' + source_reference(identifier, record, path=target, document=document,
                                                    source_root=source_root, editor=editor) + ' — ' +
                            label(record[1]['status'], language)]
                if not refs_for_claim:
                    out += ['- ' + t('Ограничение: у утверждения нет источников.', 'Limitation: this claim has no source evidence.')]
                out += ['']
        out += ['**' + t('Объяснение:', 'Explanation:') + '** ' + cell(diff['explanation']), '']
    out += [render_citation_warnings(data, language), '',
            link(t('Структурированные данные сравнения', 'Structured comparison data'), 'compare.json') + ' · ' +
            link(t('Исходный текст', 'Original text'), 'compare.original.md'), '']
    return '\n'.join(out).rstrip() + '\n'
