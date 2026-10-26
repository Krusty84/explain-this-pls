# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Validate complete Markdown link graphs, publication and canonical invariants."""
import copy
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from fixtures.report_examples import example, revision, publish_example
from fixtures.ledger_response import response
from src.analysis.ledger import prepare_result
from src.analysis.evidence import canonical, sha
from src.contracts.contracts import ContractError
from src.reports.document_rendering import validate_published_architecture
from src.reports.final_report import render_final_report, recoverable_material
from src.reports.navigation import anchor
from src.reports.presentation import render_architecture, render_stage, label
from src.reports.review import render_review
from src.reports.comparison import render_comparison
from src.runtime.artifacts import slug
from src.runtime.errors import AuditError


class Links(HTMLParser):
    def __init__(self, markdown):
        super().__init__()
        self.ids, self.hrefs = [], []
        self.feed(MarkdownIt('commonmark').enable('table').render(markdown))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if tag == 'a' and 'href' in attrs:
            self.hrefs.append(attrs['href'])


def assert_links(test, root):
    documents = {p.resolve(): Links(p.read_text()) for p in root.rglob('*.md')}
    for path, parsed in documents.items():
        test.assertEqual(len(parsed.ids), len(set(parsed.ids)), path)
        for href in parsed.hrefs:
            url = urlsplit(href)
            if url.scheme or url.netloc:
                continue
            destination = (path.parent / unquote(url.path)).resolve() if url.path else path
            test.assertTrue(destination.is_file(), (path, href))
            if url.fragment:
                test.assertIn(unquote(url.fragment), documents[destination].ids, (path, href))
                test.assertFalse(re.fullmatch(r'L\d+(?:-L?\d+)?', url.fragment), href)


class ReportNavigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='отчёты с пробелом ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_summary_size_is_independent_of_supported_registry_rows(self):
        reports = []
        for count in (1, 100):
            manifest, source, _ = example(count)
            directory = self.root / str(count)
            publish_example(directory, manifest, source)
            report = (directory / 'FINAL_REPORT.md').read_text()
            reports.append(report)
            self.assertNotIn('Full claim registry', report)
            self.assertNotIn('```plantuml', report)
            self.assertNotIn('Existing statement 100', report)
            self.assertIn('HIGH: 1', report)
            assert_links(self, directory)
        self.assertEqual(reports[0], reports[1])
        self.assertLess(len(reports[1].splitlines()), 80)

    def test_findings_are_sorted_and_keep_context_together(self):
        state, _ = revision()
        data, study = state['review'], state['study']
        base = data['findings'][0]
        data['findings'] = [base | dict(id='F-002', severity='LOW', impact='Low impact'),
                            base, base | dict(id='F-003', severity='MEDIUM', impact='Medium impact'),
                            base | dict(id='F-004', severity='LOW', claim_ids=[], impact='Unbound impact')]
        original = copy.deepcopy(data)
        rendered = render_review(data, study=study)
        self.assertLess(rendered.index('1. HIGH'), rendered.index('2. MEDIUM'))
        self.assertLess(rendered.index('2. MEDIUM'), rendered.index('3. LOW'))
        first = rendered.split('### 1.', 1)[1].split('### 2.', 1)[0]
        for value in ('Existing statement 1.', base['impact'], base['proposed_correction'],
                      'src/Заказы и счета.py:1–1', 'quote supplied', 'ARCHITECTURE.md#report-block-'):
            self.assertIn(value, first)
        self.assertNotIn('Full claim registry', rendered)
        self.assertIn('No linked claim supplied.', rendered)
        self.assertIn('review.annotated.md', rendered)
        self.assertEqual(data, original)

    def test_shared_source_returns_to_all_architecture_blocks(self):
        manifest, source, _ = example()
        publish_example(self.root, manifest, source)
        architecture = (self.root / 'ARCHITECTURE.md').read_text()
        data = manifest['study']
        row = next(line for line in architecture.splitlines() if '<a id="report-source-' in line)
        expected = [anchor('block', block['id'], data) for block in data['block_map'] if block['claim_ids']]
        self.assertEqual(set(Links(row).hrefs), {'#' + item for item in expected})
        assert_links(self, self.root)

    def test_review_uses_frozen_study_and_distinct_evidence_namespaces(self):
        state, _ = revision(folder=True, revision_id='002')
        data = copy.deepcopy(state['review'])
        data['evidence'] = [dict(id='E-001', source_id='source-001', path='review/Другой файл.py',
                                 start_line=2, end_line=4, quote='')]
        data['program_checks']['evidence'] = [dict(state['study']['program_checks']['evidence'][0],
            id='review:E-001', path='review/Другой файл.py', start_line=2, end_line=4)]
        data['findings'][0]['evidence_ids'].append('review:E-001')
        with patch('src.analysis.evidence.read_confined', side_effect=AssertionError('No source reads')):
            rendered = render_review(data, study=state['study'])
        self.assertIn('report-source-study-e-001', rendered)
        self.assertIn('report-source-review-e-001', rendered)
        self.assertIn('src/Заказы и счета.py:1–1', rendered)
        self.assertIn('review/Другой файл.py:2–4', rendered)
        state['review'] = data
        selected = copy.deepcopy(state)
        selected.update(directory='.', selected_revision='002', revisions=[state], selection_publication_complete=True)
        publish_example(self.root, selected | dict(status='PARTIAL', review_enabled=True, diagnostics=[]), {'path': 'example'})
        assert_links(self, self.root)
        wrong, _ = revision(revision_id='001')
        with self.assertRaises(ContractError): render_review(data, study=wrong['study'])
        wrong = copy.deepcopy(state['study'])
        wrong['source_commit'] = 'wrong'
        with self.assertRaises(ContractError): render_review(data, study=wrong)

    def test_publication_copies_hashes_and_guards_new_annotation(self):
        manifest, source, _ = example(multiple=True)
        saved, publisher = publish_example(self.root, manifest, source)
        name = 'review.annotated.md'
        expected = saved['review_invocation']['artifact_hashes'][name]
        self.assertEqual(expected['sha256'], sha((self.root / name).read_bytes()))
        self.assertEqual((self.root / name).read_bytes(), (self.root / 'revisions/001' / name).read_bytes())
        assert_links(self, self.root)
        for path in (self.root / name, self.root / 'revisions/002' / name):
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.assertRaises(AuditError): publisher.assert_revision_files(saved)
            path.write_bytes(original)
        state = saved['revisions'][0]
        data = copy.deepcopy(state['review'])
        data['limitations'].append('Changed after publication')
        with self.assertRaises(AuditError):
            publisher.publish_result('review', data, self.root / 'revisions/001/review.logs',
                                     context={'architecture_document': state['study']})

    def test_incomplete_disabled_and_recovered_results_are_explicit(self):
        for language, warning in (('English', 'Review is incomplete'), ('Russian', 'Ревью не завершено')):
            manifest, source, _ = example(partial=True)
            path = self.root / language
            publish_example(path, manifest, source, language)
            self.assertIn(warning, (path / 'FINAL_REPORT.md').read_text())
            self.assertFalse((path / 'ARCHITECTURE_REVIEW.md').exists())
            assert_links(self, path)
        manifest, source, _ = example()
        manifest['review_enabled'] = False
        manifest['review'] = None
        report, _ = render_final_report(manifest, source, 'folder')
        self.assertIn('Ревью отключено', report)
        material = recoverable_material('study', response(dict(source_directory='/example', source_fingerprint='f')),
                                       dict(source_directory='/example', source_fingerprint='f'), 'folder', {})
        manifest.update(study=None, study_material=material, revisions=[], status='PARTIAL')
        report, _ = render_final_report(manifest, source, 'folder')
        self.assertLess(report.index('Есть восстановленные результаты'), report.index('## '))
        self.assertIn('проверки политики не завершены', report)

    def test_canonical_data_and_hashes_survive_both_languages(self):
        state, _ = revision(100)
        original = copy.deepcopy(state)
        for language in ('English', 'Russian'):
            for _ in range(2):
                architecture = render_architecture(state['study'], language)
                validate_published_architecture(architecture, language, canonical=state['study'])
                render_review(state['review'], language, state['study'])
                render_stage('review', state['review'], language, study=state['study'])
            for block in state['study']['block_map']:
                self.assertIn(block['locator']['quote'], architecture)
        self.assertEqual(state, original)
        plan = state['study']['review_plan']
        self.assertEqual(plan['document_sha256'], sha(state['study']['report_markdown'].encode()))
        self.assertEqual(plan['registry_sha256'], sha(canonical(state['study']['claims'])))
        manifest, source, _ = example(100)
        before = copy.deepcopy(manifest)
        for language in ('English', 'Russian'):
            self.assertEqual(render_final_report(manifest, source, 'folder', language),
                             render_final_report(manifest, source, 'folder', language))
        self.assertEqual(manifest, before)

    def test_validator_rejects_changes_to_any_author_block(self):
        state, _ = revision()
        data = state['study']
        rendered = render_architecture(data)
        fragment = data['block_map'][0]['locator']['quote']
        for changed in (rendered.replace(fragment, 'Changed author block\n', 1),
                        rendered.replace(fragment, '', 1)):
            with self.assertRaises(ContractError) as caught:
                validate_published_architecture(changed, 'English', canonical=data)
            self.assertEqual(caught.exception.details['code'], 'AUTHOR_BLOCK_PUBLICATION_MISMATCH')

    def test_publication_validation_preserves_bare_cr_and_canonical_coordinates(self):
        for prose in ('bare\rCR', 'trailing\r', 'First\r\rSecond\nFinal  \n'):
            state, _ = revision(prose=prose)
            data = state['study']
            before = copy.deepcopy(data)
            for language in ('English', 'Russian'):
                rendered = render_architecture(data, language)
                validate_published_architecture(rendered, language, canonical=data)
                parts = data['report_markdown'].split('\n')
                raw_lines = [part + '\n' for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
                for block in data['block_map']:
                    locator = block['locator']
                    self.assertIn(''.join(raw_lines[locator['start_line'] - 1:locator['end_line']]), rendered)
            self.assertEqual(data, before)

    def test_no_links_are_invented_for_missing_publications(self):
        manifest, source, _ = example()
        report, _ = render_final_report(manifest, source, 'folder', available_files=set())
        self.assertFalse(Links(report).hrefs)
        self.assertNotIn('#L', report)

    def test_failed_selection_publication_links_the_frozen_revision(self):
        manifest, source, _ = example()
        saved, _ = publish_example(self.root, manifest, source)
        saved.update(selection_publication_complete=False, critical_failure=True)
        report, _ = render_final_report(saved, source, 'folder', 'English',
            available_files={str(p.relative_to(self.root)) for p in self.root.rglob('*') if p.is_file()})
        self.assertIn('Run acceptance failed due to a critical error.', report)
        self.assertIn('[Architecture](revisions/001/ARCHITECTURE.md)', report)
        self.assertIn('revisions/001/ARCHITECTURE_REVIEW.md#report-finding-f-001', report)
        self.assertNotIn('[Architecture](ARCHITECTURE.md)', report)
        (self.root / 'FINAL_REPORT.md').write_text(report)
        assert_links(self, self.root)

    def test_summary_encodes_relative_paths_with_spaces_and_unicode(self):
        manifest, source, _ = example()
        manifest['directory'] = 'Выбранный результат с пробелом'
        for state in manifest['revisions']:
            state['directory'] = manifest['directory'] + '/' + state['directory']
        publish_example(self.root, manifest, source)
        hrefs = Links((self.root / 'FINAL_REPORT.md').read_text()).hrefs
        self.assertTrue(any('%20' in href and '%D0' in href for href in hrefs))
        assert_links(self, self.root)

    def test_golden_primary_views_in_both_languages(self):
        golden = Path(__file__).parent / 'fixtures/report_navigation'
        for language in ('English', 'Russian'):
            manifest, source, _ = example()
            destination = self.root / language
            publish_example(destination, manifest, source, language)
            for name in ('FINAL_REPORT.md', 'ARCHITECTURE_REVIEW.md', 'review.annotated.md'):
                self.assertEqual((destination / name).read_bytes(), (golden / language / name).read_bytes())

    def test_comparison_both_sides_resolve_across_branches_and_revisions(self):
        entries = []
        for branch, selected_id in (('main', '001'), ('ветка / проверка', '002')):
            states = [revision(branch=branch, revision_id=identifier)[0] for identifier in ('001', '002')]
            directory = 'branches/' + slug(branch)
            for state in states:
                state['directory'] = directory + '/' + state['directory']
            selected = copy.deepcopy(states[int(selected_id) - 1])
            selected.update(directory=directory, selected_revision=selected_id, revisions=states,
                            selection_publication_complete=True)
            entries.append(selected)
        context = dict(baseline_branch='main', baseline_commit='a' * 40, branches=entries,
                       requested_branches=[b['branch'] for b in entries])
        wire = response(context)
        wire.update(completion_status='PARTIAL', unresolved_branches=context['requested_branches'])
        refs = [dict(branch=b['branch'], artifact=artifact, claim_id='C-001', revision_id=b['selected_revision'],
                     **{key: b['study']['review_plan'][key] for key in ('document_sha256', 'registry_sha256')})
                for b, artifact in zip(entries, ('study', 'review'))]
        wire['differences'] = [dict(id='D-001', branch=entries[1]['branch'], category='Execution model',
            classification='REPORTED_UNVERIFIED', baseline_statement='Uses a worker queue.',
            branch_statement='Runs the worker directly.', explanation='The supplied reports describe different dispatch paths.',
            evidence_refs=refs)]
        comparison = prepare_result('compare', wire, context)
        manifest = dict(status='PARTIAL', branches=entries, comparison=comparison, review_enabled=True, diagnostics=[])
        original = copy.deepcopy(comparison)
        for language in ('English', 'Russian'):
            path = self.root / language
            publish_example(path, manifest, {'branches': context['requested_branches']}, language, 'git', context)
            rendered = (path / 'comparison/BRANCH_COMPARISON.md').read_text()
            for value in ('Uses a worker queue.', 'Runs the worker directly.', wire['differences'][0]['explanation'],
                          '001', '002', 'a' * 40, 'b' * 40):
                self.assertIn(value, rendered)
            self.assertIn('SUPPLIED_REPORTS_ONLY', json.dumps(comparison))
            self.assertEqual(len([href for href in Links(rendered).hrefs if '#report-source-' in href]), 2)
            assert_links(self, path)
        self.assertEqual(comparison, original)
        incomplete = copy.deepcopy(comparison)
        incomplete['differences'][0]['evidence_refs'] = refs[:1]
        self.assertIn('Limitation: no evidence supplied for this side.', render_comparison(incomplete, context=context))

    def test_summary_shows_only_five_findings_and_preserves_severity_order(self):
        manifest, source, _ = example()
        findings = manifest['review']['findings']
        findings[:] = [findings[0] | dict(id=f'F-{i:03d}', severity=severity, impact=f'Impact {i}')
                       for i, severity in enumerate(('LOW', 'MEDIUM', 'HIGH', 'LOW', 'HIGH', 'MEDIUM', 'LOW'), 1)]
        report, _ = render_final_report(manifest, source, 'folder')
        self.assertLess(report.index('Impact 3'), report.index('Impact 2'))
        self.assertNotIn('Impact 4', report)
        self.assertIn('Остальные замечания: 2', report)

    def test_anchor_collisions_and_special_paths_are_safe(self):
        state, _ = revision(folder=True, source_path='src/Пример [a](b)#_`&.py',
                            prose='<a id="&#114;eport-block-b-001-001"></a>\nAuthor text.\n')
        selected = copy.deepcopy(state)
        selected.update(directory='.', selected_revision='001', revisions=[state], selection_publication_complete=True)
        manifest = selected | dict(status='PARTIAL', review_enabled=True, diagnostics=[])
        publish_example(self.root, manifest, {'path': 'example'})
        assert_links(self, self.root)
        self.assertIn('report-x-block-b-001-001', (self.root / 'ARCHITECTURE.md').read_text())

    def test_program_labels_and_empty_states_are_localized(self):
        from src.reports.presentation import LABELS, RU_LABELS, render_coverage
        from src.analysis.coverage_plan import build_coverage_plan
        self.assertFalse(set(LABELS) - set(RU_LABELS))
        manifest, source, _ = example()
        data = manifest['review']
        data['findings'] = []
        data['claims'][0]['outcome'] = 'SUPPORTED'
        review = render_review(data, 'Russian', manifest['study'])
        for text in ('Агент не указал замечаний', 'Поиск пропусков', 'Полный технический реестр'):
            self.assertIn(text, review)
        for text in ('No findings', 'Source identity', 'Full technical registry', 'INSPECTED'):
            self.assertNotIn(text, review)
        plan = build_coverage_plan({'entries': []}, dict(source_directory='/пример', source_fingerprint='f'))
        coverage = render_coverage(plan, language='Russian')
        for text in ('Source root', 'Source inventory entries', 'NOT_CITED', 'UNVERIFIED'):
            self.assertNotIn(text, coverage)

    def test_warnings_omission_search_and_prior_checks_remain_visible(self):
        state, _ = revision()
        study, data = state['study'], state['review']
        # These saved diagnostic fields have no effect on canonical author bytes.
        warning = dict(evidence_id='study:E-001', source_id='source-001', path='src/Заказы и счета.py',
                       start_line=1, end_line=1, reason='QUOTE_MISMATCH', action='UNRESOLVED', claim_ids=['C-001'])
        data['program_checks']['citation_warnings'] = [warning]
        data['findings'][0]['location'] = 'L7–L12'
        data['omission_search'][0].update(status='NOT_INSPECTED', limitation='Context search was interrupted.')
        for document in (study, data):
            document['review_plan']['prior_findings'] = [dict(revision_id='000', finding_id='F-004')]
            plan = document['review_plan']
            plan['plan_sha256'] = sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))
        data['target']['plan_sha256'] = data['review_plan']['plan_sha256']
        data['prior_findings'] = [dict(revision_id='000', finding_id='F-004', status='NOT_CHECKED',
                                      explanation='Previous finding was not checked.')]
        for language, quote_warning in (('English', 'Citation warnings'), ('Russian', 'Предупреждения о цитатах')):
            for rendered in (render_review(data, language, study), render_stage('review', data, language, study=study)):
                self.assertIn(quote_warning, rendered)
                self.assertIn('Context search was interrupted.', rendered)
                self.assertIn('Previous finding was not checked.', rendered)
                self.assertIn('study.original.md:L7–L12', rendered)
                self.assertNotIn('](#L', rendered)
        data['prior_findings'][0]['status'] = 'RESOLVED'
        self.assertIn('Устранено', render_review(data, 'Russian', study))
        for document in (study, data):
            plan = document['review_plan']
            plan['omission_areas'].append(dict(id='P-001', scope='components'))
            plan['plan_sha256'] = sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))
        data['target']['plan_sha256'] = data['review_plan']['plan_sha256']
        self.assertIn('| components |', render_review(data, 'Russian', study))
        manifest, source, _ = example()
        manifest['study']['program_checks']['citation_warnings'] = [warning]
        report, _ = render_final_report(manifest, source, 'folder')
        self.assertLess(report.index('Есть предупреждения о цитатах'), report.index('## '))


if __name__ == '__main__':
    unittest.main()
