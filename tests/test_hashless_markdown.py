# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Presentation hides service hashes without rewriting documents or saved data."""
import copy
from pathlib import Path
import tempfile
import unittest

from contracts import ContractError, SECTION_KEYS
from final_report import render_final_report
from ledger import prepare_result, review_context
from presentation import render_stage


class HashlessMarkdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'app.py').write_text('digest = "SHA-256"\n', encoding='utf-8')
        self.context = dict(source_mode='git', repository=str(self.root), branch='main',
                            source_commit='c' * 64, revision_id='001')
        self.narrative = 'The source computes SHA-256. Commit ' + 'c' * 64 + '.\n'
        evidence = [dict(id='E-001', source_id='source-001', path='app.py',
                         start_line=1, end_line=1, quote='')]
        self.study = prepare_result('study', dict(task='architecture_documentation',
            branch='main', source_commit=self.context['source_commit'], completion_status='COMPLETE',
            limitations=[], coverage=[], evidence=evidence,
            claims=[dict(id='C-001', statement='The source computes SHA-256.', scope='Static source.',
                         epistemic_kind='FACT', evidence_ids=['study:E-001'], uncertainty='')],
            report_sections=[dict(key=key, title=key, blocks=[dict(
                markdown=self.narrative if i == 0 else 'No additional fixture details.',
                claim_ids=['C-001'] if i == 0 else [])]) for i, key in enumerate(SECTION_KEYS)]),
            self.context)
        context = review_context(self.study, self.context)
        self.review = prepare_result('review', dict(task='architecture_review',
            branch='main', source_commit=self.context['source_commit'], target=context['review_target'],
            completion_status='COMPLETE', report_markdown=self.narrative, limitations=[], evidence=evidence,
            claims=[dict(id='C-001', outcome='SUPPORTED', evidence_ids=['review:E-001'], limitation='')],
            findings=[], prior_findings=[], omission_search=[dict(area_id=area['id'], status='INSPECTED',
                limitation='', finding_ids=[]) for area in context['review_plan']['omission_areas']]), context)

    def assert_no_service_hashes(self, markdown):
        self.assertNotIn('File SHA-256', markdown)
        self.assertNotIn('Fragment SHA-256', markdown)
        for document in (self.study, self.review):
            for evidence in document['program_checks']['evidence']:
                self.assertNotIn(evidence['file_sha256'], markdown)
                self.assertNotIn(evidence['fragment_sha256'], markdown)

    def test_stage_tables_hide_hashes_keep_resolution_details_and_json_unchanged(self):
        for stage, document in (('study', self.study), ('review', self.review)):
            with self.subTest(stage=stage):
                before = copy.deepcopy(document)
                markdown = render_stage(stage, document)
                self.assert_no_service_hashes(markdown)
                self.assertIn('| ID | Source | Path : lines | Locator status | Encoding |', markdown)
                self.assertIn('| source-001 | app.py:1-1 | RESOLVED | utf-8 |', markdown)
                self.assertIn('The source computes SHA-256.', markdown)
                self.assertEqual(document, before)
                self.assertEqual(len(document['program_checks']['evidence'][0]['file_sha256']), 64)

    def test_final_report_and_unselected_revision_preserve_authored_hash_semantics(self):
        original = copy.deepcopy((self.study, self.review))
        manifest = dict(status='PARTIAL', branch='main', study=self.study, review=self.review,
            selected_revision='001', revisions=[dict(revision_id='002', study=self.study, review=self.review)])
        markdown, _ = render_final_report(dict(status='PARTIAL', branches=[manifest]),
                                          {'branches': ['main']}, 'git', 'English')
        self.assert_no_service_hashes(markdown)
        self.assertIn('Additional material for revision 002', markdown)
        self.assertIn(self.narrative, markdown)
        self.assertEqual((self.study, self.review), original)

    def test_unsupported_normalization_provenance_is_rejected(self):
        for stage, current in (('study', self.study), ('review', self.review)):
            with self.subTest(stage=stage):
                historical = copy.deepcopy(current)
                historical['normalization_provenance'] = {'rule': 'EVIDENCE_IDS_V3',
                    'hash_format': 'canonical-json-utf8-v1', 'input_sha256': 'a' * 64,
                    'normalized_sha256': self.study['materialization_provenance']['normalized_sha256'],
                    'replacement_count': 0}
                before = copy.deepcopy(historical)
                with self.assertRaises(ContractError): render_stage(stage, historical)
                if stage == 'study':
                    with self.assertRaises(ContractError): review_context(historical, self.context)
                self.assertEqual(historical, before)


if __name__ == '__main__':
    unittest.main()
