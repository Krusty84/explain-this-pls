# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline, deterministic report examples; no model or source reads."""
import copy
from pathlib import Path
from unittest.mock import patch

from fixtures.ledger_response import response
from src.analysis.ledger import prepare_result, review_context
from src.contracts.contracts import CONTRACT_ID, ARTIFACT_FORMAT
from src.analysis.revisions import completed_pair
from src.reports.publication import ReportPublisher
from src.reports.final_report import render_final_report
from src.runtime.artifacts import digest, save_json, atomic
from types import SimpleNamespace


def revision(count=1, *, branch='main', revision_id='001', folder=False, partial=False,
             source_path='src/Заказы и счета.py', prose=None):
    context = ({'source_directory': '/example/Исходный код', 'source_fingerprint': 'f' * 64} if folder else
               {'branch': branch, 'source_commit': ('a' if branch == 'main' else 'b') * 40})
    context['revision_id'] = revision_id
    wire = response(context)
    wire['evidence'][0]['path'] = source_path
    if prose is not None:
        wire['report_sections'][0]['blocks'][0]['markdown'] = prose
    wire['claims'] = [wire['claims'][0] | {'id': f'C-{i:03d}', 'statement': f'Existing statement {i}.'}
                      for i in range(1, count + 1)]
    for section in wire['report_sections']:
        for block in section['blocks']:
            if block['claim_ids']:
                block['claim_ids'] = [c['id'] for c in wire['claims']]

    def resolved(stage, evidence, *args, **kwargs):
        return [dict(id=stage + ':' + e['id'], status='RESOLVED', encoding='utf-8', source_identity=None, file_sha256='c' * 64,
                     **{k: e[k] for k in ('source_id', 'path', 'start_line', 'end_line')}) for e in evidence]

    with patch('src.analysis.ledger.resolve_evidence', side_effect=resolved):
        study = prepare_result('study', wire, context)
        review_input = review_context(study, context)
        review_wire = response(review_input)
        review_wire['evidence'] = []
        for claim in review_wire['claims']:
            claim['evidence_ids'] = ['study:E-001']
        review_wire['claims'][0]['outcome'] = 'CONTRADICTED'
        review_wire['findings'] = [dict(id='F-001', severity='HIGH', type='FACTUAL_ERROR',
            claim_ids=['C-001'], location='Entry point description', evidence_ids=['study:E-001'],
            impact='The documented entry point is wrong.', proposed_correction='Use the existing worker entry point.')]
        if partial:
            review_wire.update(completion_status='PARTIAL', limitations=['Review was interrupted.'])
            review_wire['claims'] = review_wire['claims'][:1]
        review = prepare_result('review', review_wire, review_input)
    meta = dict(publication_complete=True, contract_id=CONTRACT_ID, artifact_format=ARTIFACT_FORMAT)
    item = {k: v for k, v in context.items() if k != 'revision_id'} | dict(
        revision_id=revision_id, directory=f'revisions/{revision_id}', study=study, review=review,
        study_invocation=copy.deepcopy(meta), review_invocation=copy.deepcopy(meta), errors=[])
    return item, review_input


def example(count=1, *, partial=False, multiple=False):
    item, context = revision(count, folder=True, partial=partial)
    selected = copy.deepcopy(item)
    selected.update(directory='.', selected_revision='001', selection_publication_complete=True,
                    revisions=[item], revision_history=[])
    if multiple:
        other, _ = revision(count, folder=True, revision_id='002', partial=True)
        selected['revisions'].append(other)
    manifest = selected | dict(status='PARTIAL', review_enabled=True, diagnostics=[])
    return manifest, {'path': '/example/Исходный код'}, context


def publish_example(root, manifest, source, language='English', mode='folder', comparison_context=None, *, editor='vscode'):
    """Use production publication, hashes and selected-revision copying."""
    root = root.resolve()
    manifest = copy.deepcopy(manifest)
    source_root = source.get('path' if mode == 'folder' else 'repository', '/example')
    session = SimpleNamespace(mode=mode, run_dir=root, source_path=Path(source_root),
        cfg={'output_language': language, 'your_editor': editor}, critical_failure=False)
    publisher = ReportPublisher(session)
    entries = [manifest] if mode == 'folder' else manifest['branches']
    for entry in entries:
        for state in entry['revisions']:
            directory = root / state['directory']
            for stage in ('study', 'review'):
                context = {'architecture_document': state['study']} if stage == 'review' else None
                contents = publisher.artifact_contents(stage, state[stage], context=context)
                publisher.publish_result(stage, state[stage], directory / (stage + '.logs'), context=context)
                state[stage + '_invocation']['artifact_hashes'] = {
                    name: {'sha256': digest(blob), 'bytes': len(blob)} for name, blob in contents.items()}
        selected = next(r for r in entry['revisions'] if r['revision_id'] == entry['selected_revision'])
        for stage in ('study', 'review'):
            if stage == 'review' and not completed_pair(selected):
                entry[stage] = None
                entry.pop(stage + '_invocation', None)
            else:
                entry[stage] = selected[stage]
                entry[stage + '_invocation'] = selected[stage + '_invocation']
        publisher.publish_revision(selected, root / entry['directory'])
    if manifest.get('comparison'):
        data = manifest['comparison']
        publisher.publish_result('compare', data, root / 'comparison/compare.logs', context=comparison_context)
    save_json(root / 'manifest.json', manifest)
    report, _ = render_final_report(manifest, source, mode, language,
        available_files={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()})
    atomic(root / 'FINAL_REPORT.md', report)
    publisher.assert_revision_files(entries[0])
    return manifest, publisher
