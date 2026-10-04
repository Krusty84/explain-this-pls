# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import copy
import tempfile
import unittest
from pathlib import Path

from src.contracts.contracts import CONTRACT_ID, ARTIFACT_FORMAT, ContractError, validate_result, accepted, validate_schema
from src.analysis.ledger import prepare_result, review_context
from src.analysis.revisions import registry_diff, revision_inputs, choose_revision, completed_pair
from src.contracts.saved_contracts import SAVED_SCHEMAS
from fixtures.ledger_response import response


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'app.py').write_text('print(1)\n')
        self.context = {'branch': 'main', 'source_commit': 'abc', 'source_mode': 'git',
                        'repository': str(self.root), 'revision_id': '001'}
        self.meta = {'publication_complete': True, 'contract_id': CONTRACT_ID, 'artifact_format': ARTIFACT_FORMAT}

    def tearDown(self):
        self.tmp.cleanup()

    def pair(self, context=None, change_study=None, change_review=None):
        context = context or self.context
        wire = response(context)
        if change_study:
            change_study(wire)
        validate_result('study', wire, context)
        study = prepare_result('study', wire, context)
        rc = review_context(study, context)
        review = response(rc | {'stage': 'review'})
        if change_review:
            change_review(review)
        validate_result('review', review, rc)
        review = prepare_result('review', review, rc)
        validate_schema(study, SAVED_SCHEMAS['study'])
        validate_schema(review, SAVED_SCHEMAS['review'])
        return {'revision_id': context['revision_id'], 'study': study, 'review': review,
                'study_invocation': dict(self.meta), 'review_invocation': dict(self.meta)}

    @staticmethod
    def issue(review):
        review['findings'] = [{'id': 'F-001', 'severity': 'HIGH', 'type': 'FACTUAL_ERROR',
            'claim_ids': ['C-001'], 'location': 'Original sentence.', 'evidence_ids': ['review:E-001'],
            'impact': 'Misleading description.', 'proposed_correction': 'Correct this statement.'}]

    def test_registry_diff_ignores_coordinates_and_rejects_renumbering(self):
        before = self.pair()['study']
        after = copy.deepcopy(before)
        after['claims'][0]['document_locators'][0]['start_line'] += 10
        self.assertEqual(registry_diff(before, after)['unchanged_ids'], ['C-001'])
        after['claims'][0]['statement'] = 'Changed claim.'
        self.assertEqual(registry_diff(before, after)['changed_ids'], ['C-001'])
        after['claims'][0]['id'] = 'C-002'
        diff = registry_diff(before, after)
        self.assertEqual((diff['removed_ids'], diff['added_ids']), (['C-001'], ['C-002']))
        after['claims'][0]['statement'] = before['claims'][0]['statement']
        with self.assertRaises(ContractError):
            registry_diff(before, after)

    def test_pair_requires_supported_invocation_formats(self):
        pair = self.pair()
        self.assertTrue(accepted(pair))
        self.assertTrue(completed_pair(pair))
        for stage in ('study', 'review'):
            for field in ('contract_id', 'artifact_format'):
                for missing in (False, True):
                    with self.subTest(stage=stage, field=field, missing=missing):
                        unsupported = copy.deepcopy(pair)
                        metadata = unsupported[stage + '_invocation']
                        if missing:
                            metadata.pop(field)
                        else:
                            metadata[field] = 'unsupported-format'
                        self.assertFalse(accepted(unsupported))
                        self.assertFalse(completed_pair(unsupported))

    def test_prior_findings_missing_unresolved_and_resolved(self):
        previous = self.pair(change_review=self.issue)
        self.assertFalse(accepted(previous))
        context = self.context | revision_inputs(previous) | {'revision_id': '002'}
        for status in ('MISSING', 'UNRESOLVED', 'NOT_CHECKED', 'RESOLVED'):
            with self.subTest(status=status):
                def change(review):
                    if status == 'MISSING':
                        review['prior_findings'] = []
                    else:
                        review['prior_findings'][0]['status'] = status
                current = self.pair(context, change_review=change)
                self.assertEqual(accepted(current), status == 'RESOLVED')
                self.assertEqual(current['review']['review_plan']['prior_findings'], [{'revision_id': '001', 'finding_id': 'F-001'}])

    def test_old_claim_deletion_is_visible_and_requires_reviewer_resolution(self):
        previous = self.pair(change_review=self.issue)
        context = self.context | revision_inputs(previous) | {'revision_id': '002'}
        def replace(study):
            study['claims'][0].update(id='C-002', statement='Replacement description.')
            study['report_sections'][0]['blocks'][0].update(markdown='Replacement description.\n', claim_ids=['C-002'])
        current = self.pair(context, change_study=replace)
        diff = current['study']['registry_diff']
        self.assertEqual((diff['removed_ids'], diff['added_ids']), (['C-001'], ['C-002']))
        self.assertTrue(accepted(current))
        self.assertEqual(current['review']['prior_findings'][0]['revision_id'], '001')

    def test_selection_complete_review_then_usable_study_never_mixes(self):
        previous = self.pair(change_review=self.issue)
        context = self.context | revision_inputs(previous) | {'revision_id': '002'}
        current = self.pair(context, change_review=lambda r: r.update(completion_status='PARTIAL', limitations=['Incomplete review.']))
        self.assertFalse(completed_pair(current))
        self.assertIs(choose_revision([previous, current]), previous)
        current['review_invocation']['publication_complete'] = False
        self.assertIs(choose_revision([previous, current]), previous)
        previous['review'] = None
        self.assertIs(choose_revision([previous, current]), current)

    def test_accepted_pair_precedes_newer_unaccepted_pair(self):
        first = self.pair()
        later = self.pair(self.context | {'revision_id': '002'}, change_review=self.issue)
        self.assertIs(choose_revision([first, later]), first)
        later['review'] = first['review']
        self.assertFalse(completed_pair(later))
        self.assertFalse(accepted(later))
        forged = first | {'revision_id': '002'}
        self.assertFalse(completed_pair(forged))
        self.assertFalse(accepted(forged))

    def test_prior_version_namespace_and_initial_empty_obligations(self):
        first = self.pair()
        self.assertEqual(first['review']['prior_findings'], [])
        previous = self.pair(change_review=self.issue)
        context = self.context | revision_inputs(previous) | {'revision_id': '002'}
        with self.assertRaises(ContractError):
            self.pair(context, change_review=lambda r: r['prior_findings'][0].update(revision_id='002'))
        with self.assertRaises(ContractError):
            self.pair(context | {'prior_findings': []})

    def test_completed_selection_rejects_other_source_or_invalid_recovered_review(self):
        first = self.pair(change_review=self.issue)
        altered = copy.deepcopy(first)
        altered['review']['source_commit'] = 'other'
        self.assertFalse(completed_pair(altered))
        altered = copy.deepcopy(first)
        altered['review']['strict_valid'] = False
        self.assertFalse(completed_pair(altered))
        altered = copy.deepcopy(first)
        altered['review']['target']['document_sha256'] = 'other'
        self.assertFalse(completed_pair(altered))
        context = self.context | revision_inputs(first) | {'revision_id': '002'}
        self.assertFalse(accepted(self.pair(context, change_review=self.issue)))


if __name__ == '__main__':
    unittest.main()
