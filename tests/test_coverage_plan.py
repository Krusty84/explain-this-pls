# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.contracts.contracts import ContractError, validate_result, validate_schema
from src.analysis.coverage_plan import (advisory_diagnostics, build_coverage_plan, coverage_checks,
    inventory_summary, verify_coverage_plan, study_coverage_plan)
from src.analysis.ledger import prepare_result, review_context, verify_review_context
from src.reports.presentation import render_coverage
from src.contracts.saved_contracts import SAVED_SCHEMAS
from src.analysis.evidence import canonical, sha
from fixtures.ledger_response import response


class CoveragePlanTests(unittest.TestCase):
    def setUp(self):
        self.context = {'branch': 'main', 'source_commit': 'abc', 'source_mode': 'git'}
        self.inventory = {'entries': [
            {'path': '.', 'type': 'directory'}, {'path': 'src', 'type': 'directory'},
            {'path': 'src/app.py', 'type': 'file', 'sha256': '1'},
            {'path': 'src/lib.py', 'type': 'file', 'sha256': '2'},
            {'path': 'README.md', 'type': 'file', 'sha256': '3'},
            {'path': 'linked', 'type': 'symlink', 'target': '../outside'}]}

    def plan(self):
        return build_coverage_plan(self.inventory, self.context)

    def rehash(self, plan):
        plan['plan_sha256'] = sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))

    def test_inventory_defines_all_area_memberships(self):
        plan = self.plan()
        self.assertEqual(plan['origin'], 'SOURCE_INVENTORY')
        self.assertEqual([a['paths'] for a in plan['areas']], [['.'], ['src']])
        self.assertEqual(plan['areas'][0]['entry_paths'], ['README.md', 'linked'])
        self.assertEqual(plan['areas'][1]['entry_paths'], ['src/app.py', 'src/lib.py'])
        self.assertEqual(plan['counts'], {'files': 3, 'symlinks': 1, 'directories': 2, 'empty_directories': 0})
        self.assertNotIn('policy_satisfied', plan)
        self.assertNotIn('exclusions', plan)
        self.assertNotIn('unclassified_paths', plan)
        self.assertEqual(verify_coverage_plan(plan), plan)

    def test_model_metadata_never_changes_frozen_scope(self):
        context = self.context | {'_inventory': self.inventory, 'coverage_plan': self.plan()}
        original = copy.deepcopy(context)
        for observation in ({}, {'subsystems': None}, {'coverage': 'invalid'},
                {'subsystems': [{'paths': ['missing/']}]},
                {'exclusions': [{'path': 'src', 'reason': 'Not relevant.'}]}):
            with self.subTest(observation=observation):
                self.assertEqual(study_coverage_plan(observation, context), context['coverage_plan'])
        self.assertEqual(context, original)

    def test_changed_inventory_or_source_identity_is_rejected(self):
        context = self.context | {'_inventory': self.inventory, 'coverage_plan': self.plan()}
        changed = copy.deepcopy(context)
        changed['_inventory']['entries'][2]['sha256'] = 'changed'
        for invalid in (changed, context | {'source_commit': 'other'}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ContractError):
                    study_coverage_plan({}, invalid)

    def test_frozen_hash_counts_and_memberships_cannot_be_forged(self):
        for change in (lambda p: p['counts'].update(files=999),
                lambda p: p['areas'][0].update(id='S-999'),
                lambda p: p['areas'][0].update(name='Changed'),
                lambda p: p['areas'][0].update(required=False),
                lambda p: p['areas'][0].update(entry_paths=['../outside']),
                lambda p: p['areas'][1].update(entry_paths=['README.md'])):
            with self.subTest(change=change):
                plan = self.plan()
                change(plan)
                self.rehash(plan)
                with self.assertRaises(ContractError):
                    verify_coverage_plan(plan)
        plan = self.plan()
        plan['areas'][0]['name'] = 'Changed'
        with self.assertRaises(ContractError):
            verify_coverage_plan(plan)

    def test_old_plan_fields_and_format_are_rejected(self):
        for key, value in (('exclusions', []), ('unclassified_paths', []), ('policy_satisfied', True),
                           ('artifact_format', 'evidence-ledger-study-coverage-artifacts')):
            plan = self.plan() | {key: value}
            self.rehash(plan)
            with self.assertRaises(ContractError):
                verify_coverage_plan(plan)

    def test_invalid_advisory_paths_are_preserved_and_only_warn(self):
        observations = {'subsystems': [{'id': 'S-001', 'name': 'App', 'purpose': 'App code.',
            'paths': ['src/', 'missing', '../private']}],
            'exclusions': [{'path': 'src', 'reason': 'Not relevant.'}]}
        original = copy.deepcopy(observations)
        context = self.context | {'_inventory': self.inventory}
        diagnostics = advisory_diagnostics(observations, context)
        self.assertEqual(observations, original)
        self.assertEqual([d['code'] for d in diagnostics], ['UNSAFE_COVERAGE_PATH',
            'UNKNOWN_COVERAGE_PATH', 'UNSAFE_COVERAGE_PATH', 'MODEL_EXCLUSION_IGNORED'])
        self.assertNotIn('../private', json.dumps(diagnostics))
        self.assertEqual(study_coverage_plan(observations, context), self.plan())

    def test_arbitrary_advisory_types_cannot_reject_scope(self):
        context = self.context | {'_inventory': self.inventory}
        for observations in (None, [], 1, '', {'subsystems': None, 'coverage': 'wrong', 'exclusions': 9},
                {'subsystems': [None, {'paths': [None, [], {}]}], 'exclusions': [None, []]},
                {'advisory': {'arbitrary': [1]}, 'relevance_notes': None}):
            with self.subTest(observations=observations):
                self.assertIsInstance(advisory_diagnostics(observations, context), list)
                self.assertEqual(study_coverage_plan(observations, context), self.plan())

    def test_distribution_escapes_directory_names_and_distinguishes_citations(self):
        for entry in self.inventory['entries']:
            entry['path'] = entry['path'].replace('src', 'Исходники | <core>', 1)
        markdown = render_coverage(self.plan())
        self.assertIn('Исходники &#124; &lt;core&gt;', markdown)
        self.assertIn('| S-001 | Not cited | Unverified |', markdown)
        self.assertNotIn('| NOT_INSPECTED |', markdown)
        self.assertNotIn('plan_sha256', markdown)

    def test_first_study_and_review_share_inventory_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'app.py').write_text('print(1)\n')
            inventory = {'entries': [{'path': '.', 'type': 'directory'},
                {'path': 'app.py', 'type': 'file', 'sha256': sha(b'print(1)\n')}]}
            context = self.context | {'repository': str(root), '_inventory': inventory, 'revision_id': '001'}
            original = copy.deepcopy(context)
            wire = response(context)
            validate_result('study', wire, context)
            study = prepare_result('study', wire, context)
            validate_schema(study, SAVED_SCHEMAS['study'])
            self.assertTrue(study['program_checks']['policy_satisfied'])
            self.assertEqual(context, original)
            review = review_context(study, context)
            self.assertEqual(review['coverage_plan'], build_coverage_plan(inventory, context))
            self.assertIn('S-001', [a['id'] for a in review['review_plan']['omission_areas']])
            from src.analysis.ledger import freeze_plan
            changed = freeze_plan(study, context | {'source_decoding': {'rules': [{'path': '.', 'encoding': 'cp1251'}]}})
            self.assertNotEqual(changed['plan_sha256'], study['review_plan']['plan_sha256'])
            self.assertEqual(changed['document_sha256'], study['review_plan']['document_sha256'])

            changed_context = copy.deepcopy(context)
            changed_context['_inventory']['entries'][1]['sha256'] = 'changed'
            with self.assertRaisesRegex(ContractError, 'inventories differ'):
                review_context(study, changed_context)
            changed_review = copy.deepcopy(review)
            changed_review['_inventory']['entries'][1]['sha256'] = 'changed'
            with self.assertRaisesRegex(ContractError, 'inventories differ'):
                verify_review_context(changed_review)

    def test_review_retains_topics_priority_scenarios_and_source_areas(self):
        context = self.context | {'_inventory': self.inventory, 'coverage_plan': self.plan(),
                                 'priority_scenarios': ['Start the application.']}
        from src.reports.document_rendering import materialize_study
        review = review_context(materialize_study(response(context)), context)
        areas = review['review_plan']['omission_areas']
        self.assertEqual([area['scope'] for area in areas[:5]],
                         ['context', 'components', 'startup_and_flows', 'data_and_state', 'scope'])
        self.assertEqual([area['id'] for area in areas],
                         ['A-001', 'A-002', 'A-003', 'A-004', 'A-005', 'P-001', 'S-001', 'S-002'])
        self.assertEqual(areas[5]['scope'], 'Start the application.')
        self.assertEqual(review['review_plan']['required_claim_ids'], ['C-001'])
        verify_review_context(review)

    def test_empty_directory_group_has_no_source_members(self):
        self.inventory['entries'].append({'path': 'empty', 'type': 'directory'})
        plan = self.plan()
        empty = next(a for a in plan['areas'] if a['paths'] == ['empty'])
        self.assertEqual(empty['entry_paths'], [])
        self.assertFalse(empty['required'])
        self.assertEqual(verify_coverage_plan(plan), plan)

    def test_citations_do_not_measure_inspection(self):
        context = self.context | {'coverage_plan': self.plan()}
        resolution = {'id': 'study:E-001', 'source_id': 'source-001', 'path': 'src/app.py', 'status': 'RESOLVED'}
        checks = coverage_checks({'coverage': None}, context, [resolution])
        self.assertEqual(checks['cited_ids'], ['S-002'])
        self.assertEqual(checks['not_cited_ids'], ['S-001'])
        self.assertEqual(checks['unverified_ids'], ['S-001', 'S-002'])
        self.assertFalse(checks['completeness_measured'])
        self.assertNotIn('policy_satisfied', checks)
        checks = coverage_checks({}, context, [resolution | {'status': 'DECODE_ERROR'}])
        self.assertEqual(checks['cited_ids'], [])
        self.assertEqual(checks['not_cited_ids'], ['S-001', 'S-002'])

    def test_submodule_evidence_uses_common_source_root(self):
        self.context['submodules'] = [{'path': 'src', 'expected_commit': 'def'}]
        context = self.context | {'coverage_plan': self.plan()}
        resolution = {'id': 'study:E-001', 'source_id': 'source-002', 'path': 'app.py', 'status': 'RESOLVED'}
        self.assertEqual(coverage_checks({}, context, [resolution])['cited_ids'], ['S-002'])

    def test_summary_does_not_include_file_contents_or_full_inventory(self):
        summary = inventory_summary(self.inventory)
        self.assertEqual(summary, {'files': 3, 'symlinks': 1, 'directory_count': 2, 'empty_directories': 0,
            'directories': [{'path': '.', 'files': 1, 'symlinks': 1, 'directories': 1, 'empty_directories': 0},
                            {'path': 'src', 'files': 2, 'symlinks': 0, 'directories': 1, 'empty_directories': 0}]})
        inventory = copy.deepcopy(self.inventory)
        inventory['entries'] += [{'path': 'src/nested', 'type': 'directory'},
                                  {'path': 'src/nested/empty', 'type': 'directory'},
                                  {'path': 'src/nested/code.py', 'type': 'file'}]
        summary = inventory_summary(inventory)
        self.assertEqual([g['path'] for g in summary['directories']], ['.', 'src'])
        self.assertEqual(summary['directories'][1], {'path': 'src', 'files': 3, 'symlinks': 0,
                                                    'directories': 3, 'empty_directories': 1})


if __name__ == '__main__':
    unittest.main()
