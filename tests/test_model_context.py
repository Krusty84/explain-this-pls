import copy
import json
import unittest

from document_rendering import materialize_study
from fixtures.ledger_response import response
from ledger import review_context, verify_review_context
from model_context import project_model_context
from structured_output import repair_prompt


class ModelContextTests(unittest.TestCase):
    def test_twenty_claim_table_is_sent_once_without_changing_frozen_context(self):
        context = {'branch': 'main', 'source_commit': 'abc'}
        wire = response(context)
        wire.setdefault('coverage', [])
        wire['claims'] = [wire['claims'][0] | {'id': f'C-{n:03}', 'statement': f'Assertion {n}'}
                          for n in range(1, 21)]
        table = '| Key | Value |\n| --- | --- |\n' + ''.join(f'| {n} | unique row {n} |\n' for n in range(1, 21))
        wire['report_sections'][0]['blocks'] = [{'markdown': table, 'claim_ids': [c['id'] for c in wire['claims']]}]
        context = review_context(materialize_study(wire), context)
        original = copy.deepcopy(context)
        projected = project_model_context('review', context)
        payload = json.dumps(projected, ensure_ascii=False)
        self.assertEqual(payload.count('unique row 20'), 1)
        self.assertEqual(payload.count('Assertion 20'), 1)
        self.assertEqual(len(projected['claim_registry']), 20)
        self.assertNotIn('claims', projected['architecture_document'])
        for claim in projected['claim_registry']:
            self.assertNotIn('quote', claim['document_locators'][0])
        for key in ('block_map', 'materialization_provenance', 'normalization_provenance'):
            self.assertNotIn(key, projected['architecture_document'])
        self.assertEqual(context, original)
        verify_review_context(context)
        projected['claim_registry'][0]['statement'] = 'edited'
        self.assertEqual(context, original)

    def test_compare_and_revision_retain_content_once(self):
        pair = {'study': {'report_markdown': 'document', 'claims': [{'id': 'C-001',
            'statement': 'statement', 'document_locators': [{'quote': 'document', 'start_line': 1, 'end_line': 1}]}]},
            'review': {'report_markdown': 'review', 'claim_registry': [{'statement': 'statement'}],
                       'findings': [{'impact': 'issue'}], 'limitations': ['limit']}}
        context = {'branches': [pair], 'previous_revision': pair}
        original = copy.deepcopy(context)
        projected = project_model_context('compare', context)
        for item in (projected['branches'][0], projected['previous_revision']):
            payload = json.dumps(item)
            self.assertEqual(payload.count('"document"'), 1)
            self.assertNotIn('claim_registry', item['review'])
            self.assertEqual(item['review']['findings'], pair['review']['findings'])
        self.assertEqual(context, original)

    def test_format_repair_identical_native_objects_sent_once(self):
        native = {'unique_native_text': 'preserve me'}
        prompt = repair_prompt({}, {}, native, {}, original=copy.deepcopy(native))
        self.assertEqual(prompt.count('preserve me'), 1)
        self.assertIn('also the original immutable', prompt)
        prompt = repair_prompt({}, {}, native, {}, original={'original': 'other'})
        self.assertIn('Original immutable native result', prompt)
        self.assertIn('other', prompt)

    def test_coverage_projection_uses_selectors_without_expanded_inventory(self):
        plan = {'areas': [{'id': 'S-001', 'paths': ['src'], 'entry_paths': ['src/app.py', 'src/link'],
                           'file_paths': ['src/app.py']}], 'exclusions': [], 'plan_sha256': 'frozen'}
        context = {'coverage_plan': plan, 'review_plan': {'coverage_plan': plan, 'plan_sha256': 'review'}}
        before = copy.deepcopy(context)
        projected = project_model_context('review', context)
        self.assertNotIn('coverage_plan', projected['review_plan'])
        self.assertEqual(projected['coverage_plan']['areas'][0],
                         {'id': 'S-001', 'paths': ['src'], 'entry_count': 2, 'file_count': 1})
        self.assertEqual(project_model_context('review', projected), projected)
        self.assertEqual(context, before)
