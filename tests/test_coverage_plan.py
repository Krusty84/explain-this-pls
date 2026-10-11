# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import copy
import tempfile
import unittest
from pathlib import Path

from src.contracts.contracts import ContractError, validate_result, validate_schema
from src.analysis.coverage_plan import build_coverage_plan, coverage_checks, inventory_summary, verify_coverage_plan, study_coverage_plan
from src.analysis.ledger import prepare_result, review_context
from src.reports.presentation import render_coverage
from src.contracts.saved_contracts import SAVED_SCHEMAS
from src.analysis.evidence import canonical, sha, valid_path
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
        self.scope = dict(subsystems=[dict(id='S-001', name='Source', purpose='Application code.', paths=['src'])],
            exclusions=[dict(path='README.md', reason='User documentation.'),
                        dict(path='linked', reason='Symbolic link; target not inspected.')])

    def plan(self, scope=None):
        scope = scope if scope is not None else self.scope
        return build_coverage_plan(scope['subsystems'], scope['exclusions'], self.inventory, self.context)

    def test_unclassified_entries_block_policy(self):
        self.scope['exclusions'] = []
        plan = self.plan()
        self.assertEqual(plan['unclassified_paths'], ['README.md', 'linked'])
        self.assertEqual(plan['areas'][-1]['id'], 'UNCLASSIFIED')
        self.assertFalse(plan['policy_satisfied'])
        self.assertEqual(verify_coverage_plan(plan), plan)

    def test_paths_require_real_inventory_entries(self):
        for path in ('src2', '../src', '/src', 'src//app.py', 'src/../src', 'src\\app.py', 'linked/outside.py',
                     'src//', './', '/', 'missing/', 'README.md/', 'linked/'):
            for location in ('subsystem', 'exclusion'):
                with self.subTest(path=path, location=location):
                    scope = copy.deepcopy(self.scope)
                    if location == 'subsystem':
                        scope['subsystems'][0]['paths'] = [path]
                    else:
                        scope['exclusions'].append(dict(path=path, reason='Invalid selector.'))
                    with self.assertRaises(ContractError):
                        self.plan(scope)

    def test_directory_suffix_preserves_assignments_without_mutating_response(self):
        expected = self.plan()
        self.scope['subsystems'][0]['paths'] = ['src/']
        original = copy.deepcopy(self.scope)
        self.assertEqual(self.plan(), expected)
        self.assertEqual(self.scope, original)
        self.assertFalse(valid_path('src/'))

    def test_alias_duplicates_empty_paths_and_reserved_ids_are_rejected(self):
        changes = [lambda s: s['subsystems'][0].update(paths=['src', 'src/']),
                   lambda s: s['subsystems'][0].update(paths=[]),
                   lambda s: s['subsystems'][0].update(id='UNCLASSIFIED'),
                   lambda s: s['subsystems'].append(copy.deepcopy(s['subsystems'][0])),
                   lambda s: s['exclusions'].append(dict(path='README.md', reason='Duplicate.')),
                   lambda s: s['exclusions'][0].update(reason=' ')]
        for change in changes:
            with self.subTest(change=change):
                scope = copy.deepcopy(self.scope)
                change(scope)
                with self.assertRaises(ContractError):
                    self.plan(scope)

    def test_plan_policy_counts_and_hash_cannot_be_forged(self):
        for change in (lambda p: p['counts'].update(assigned_files=999),
                       lambda p: p.update(policy_satisfied=False)):
            plan = self.plan()
            change(plan)
            plan['plan_sha256'] = sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))
            with self.assertRaises(ContractError):
                verify_coverage_plan(plan)
        plan = self.plan()
        plan['areas'][0]['name'] = 'Changed'
        with self.assertRaises(ContractError):
            verify_coverage_plan(plan)

    def test_scope_is_frozen_across_revisions_without_binding_mutation(self):
        context = self.context | {'_inventory': self.inventory, 'coverage_plan': self.plan()}
        original = copy.deepcopy(context)
        self.assertEqual(study_coverage_plan(self.scope, context), context['coverage_plan'])
        self.assertEqual(context, original)
        for change in (lambda s: s['subsystems'][0].update(paths=['src/app.py']),
                       lambda s: s['subsystems'][0].update(name='Changed'),
                       lambda s: s['exclusions'].pop()):
            scope = copy.deepcopy(self.scope)
            change(scope)
            with self.assertRaises(ContractError) as caught:
                study_coverage_plan(scope, context)
            self.assertEqual(caught.exception.details['code'], 'COVERAGE_SCOPE_CHANGED')

    def test_old_coverage_fields_are_rejected(self):
        for key, value in (('origin', 'AGENT'), ('catalog_status', 'COMPLETE'), ('limitations', [])):
            plan = self.plan() | {key: value}
            plan['plan_sha256'] = sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))
            with self.assertRaises(ContractError):
                verify_coverage_plan(plan)

    def test_distribution_escapes_values_without_old_stage_metadata(self):
        self.scope['subsystems'][0].update(name='Исходники | <core>', purpose='Код\nприложения\x1b.')
        markdown = render_coverage(self.plan())
        self.assertIn('Исходники &#124; &lt;core&gt;', markdown)
        self.assertNotIn('Catalog', markdown)
        self.assertNotIn('plan_sha256', markdown)

    def test_first_study_builds_plan_and_review_binds_decoding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'app.py').write_text('print(1)\n')
            inventory = {'entries': [{'path': '.', 'type': 'directory'}, {'path': 'app.py', 'type': 'file', 'sha256': sha(b'print(1)\n')}]}
            context = self.context | {'repository': str(root), '_inventory': inventory, 'revision_id': '001'}
            original = copy.deepcopy(context)
            wire = response(context)
            validate_result('study', wire, context)
            study = prepare_result('study', wire, context)
            validate_schema(study, SAVED_SCHEMAS['study'])
            self.assertTrue(study['program_checks']['policy_satisfied'])
            self.assertEqual(context, original)
            context = context | {'coverage_plan': study['review_plan']['coverage_plan']}
            review = review_context(study, context)
            self.assertIn('S-001', [a['id'] for a in review['review_plan']['omission_areas']])
            from src.analysis.ledger import freeze_plan
            changed = freeze_plan(study, context | {'source_decoding': {'rules': [{'path': '.', 'encoding': 'cp1251'}]}})
            self.assertNotEqual(changed['plan_sha256'], study['review_plan']['plan_sha256'])
            self.assertEqual(changed['document_sha256'], study['review_plan']['document_sha256'])


    def test_overlaps_count_unique_files_and_keep_exclusions(self):
        self.scope['subsystems'].append({'id': 'S-002', 'name': 'Library', 'purpose': 'Shared code.', 'paths': ['src/lib.py']})
        plan = self.plan()
        self.assertEqual(plan['counts'], {'files': 3, 'symlinks': 1, 'assigned_files': 2,
            'excluded_files': 1, 'unclassified_files': 0, 'overlapping_files': 1,
            'directories': 2, 'empty_directories': 0})
        self.assertTrue(plan['policy_satisfied'])
        self.assertEqual(plan['exclusions'][1]['entry_paths'], ['linked'])
        self.assertEqual(verify_coverage_plan(plan), plan)


    def test_unsupported_contract_is_rejected_even_with_matching_hash(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                plan = self.plan()
                if missing:
                    plan.pop('contract_id')
                else:
                    plan['contract_id'] = 'unsupported-format'
                plan['plan_sha256'] = sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))
                with self.assertRaises(ContractError):
                    verify_coverage_plan(plan)


    def test_empty_area_report_is_required_but_inspection_is_not(self):
        self.scope['exclusions'].append({'path': 'src', 'reason': 'Explicitly excluded synthetic fixture.'})
        plan = self.plan()
        self.assertFalse(plan['areas'][0]['required'])
        context = self.context | {'coverage_plan': plan}
        self.assertEqual(coverage_checks({'coverage': []}, context, [])['missing_ids'], ['S-001'])
        study = {'coverage': [{'area_id': 'S-001', 'status': 'NOT_INSPECTED',
                              'evidence_ids': [], 'limitation': 'All entries explicitly excluded.'}]}
        self.assertTrue(coverage_checks(study, context, [])['policy_satisfied'])


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


    def test_inspected_needs_resolved_evidence_inside_area(self):
        plan = self.plan()
        context = self.context | {'coverage_plan': plan}
        study = {'coverage': [{'area_id': 'S-001', 'status': 'INSPECTED',
                              'evidence_ids': ['study:E-001'], 'limitation': ''}]}
        resolution = {'id': 'study:E-001', 'source_id': 'source-001', 'path': 'src/app.py', 'status': 'RESOLVED'}
        self.assertTrue(coverage_checks(study, context, [resolution])['policy_satisfied'])
        self.assertEqual(coverage_checks(study, context, [resolution | {'path': 'README.md'}])['unsupported_ids'], ['S-001'])
        self.assertEqual(coverage_checks(study, context, [resolution | {'status': 'DECODE_ERROR'}])['unsupported_ids'], ['S-001'])
        study['coverage'] = []
        self.assertEqual(coverage_checks(study, context, [])['missing_ids'], ['S-001'])


    def test_submodule_evidence_uses_common_source_root(self):
        self.context['submodules'] = [{'path': 'src', 'expected_commit': 'def'}]
        plan = self.plan()
        context = self.context | {'coverage_plan': plan}
        study = {'coverage': [{'area_id': 'S-001', 'status': 'INSPECTED', 'evidence_ids': ['study:E-001'], 'limitation': ''}]}
        resolution = {'id': 'study:E-001', 'source_id': 'source-002', 'path': 'app.py', 'status': 'RESOLVED'}
        self.assertTrue(coverage_checks(study, context, [resolution])['policy_satisfied'])



if __name__ == "__main__":
    unittest.main()
