# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import copy
import tempfile
import unittest
from pathlib import Path

from src.contracts.contracts import ContractError, validate_result, validate_schema
from src.analysis.coverage_plan import build_coverage_plan, coverage_checks, inventory_summary, verify_coverage_plan, recover_catalog_paths
from src.analysis.ledger import prepare_result, review_context
from src.reports.presentation import render_stage
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
        self.catalog = {'task': 'architecture_catalog', 'branch': 'main', 'source_commit': 'abc',
            'completion_status': 'COMPLETE', 'limitations': [],
            'subsystems': [{'id': 'S-001', 'name': 'Source', 'purpose': 'Application code.', 'paths': ['src']}],
            'exclusions': [{'path': 'README.md', 'reason': 'User documentation.'},
                           {'path': 'linked', 'reason': 'Symbolic link; target not inspected.'}]}

    def test_overlaps_count_unique_files_and_keep_exclusions(self):
        self.catalog['subsystems'].append({'id': 'S-002', 'name': 'Library', 'purpose': 'Shared code.', 'paths': ['src/lib.py']})
        plan = build_coverage_plan(self.catalog, self.inventory, self.context)
        self.assertEqual(plan['counts'], {'files': 3, 'symlinks': 1, 'assigned_files': 2,
            'excluded_files': 1, 'unclassified_files': 0, 'overlapping_files': 1,
            'directories': 2, 'empty_directories': 0})
        self.assertTrue(plan['policy_satisfied'])
        self.assertEqual(plan['exclusions'][1]['entry_paths'], ['linked'])
        self.assertEqual(verify_coverage_plan(plan), plan)

    def test_catalog_markdown_preserves_content_and_escapes_table_values(self):
        self.catalog.update(completion_status='PARTIAL', limitations=['Не проверено | <linked>\nещё.'])
        self.catalog['subsystems'][0].update(name='Исходники | <core>', purpose='Код\nприложения\x1b.')
        self.catalog['exclusions'][0]['reason'] = 'Документация | <users>.'
        self.inventory['entries'].append({'path': 'данные|new.txt', 'type': 'file', 'sha256': '4'})
        catalog = prepare_result('catalog', self.catalog, self.context | {'_inventory': self.inventory})
        original = copy.deepcopy(catalog)
        markdown = render_stage('catalog', catalog, 'Russian')
        self.assertIn('# Subsystem catalog\n', markdown)
        self.assertIn('Catalog completion reported by the agent: PARTIAL', markdown)
        self.assertIn('| S-001 | Исходники &#124; &lt;core&gt; | Код приложения\\u001b. | src |', markdown)
        self.assertIn('| README.md | Документация &#124; &lt;users&gt;. |', markdown)
        self.assertIn('- Не проверено &#124; &lt;linked&gt; ещё.', markdown)
        self.assertIn('## Unclassified paths\n\n- данные&#124;new.txt', markdown)
        for key in ('inventory_sha256', 'plan_sha256'):
            self.assertNotIn(catalog['coverage_plan'][key], markdown)
        self.assertEqual(catalog, original)

    def test_catalog_markdown_supports_empty_lists(self):
        self.catalog.update(subsystems=[], exclusions=[])
        self.inventory['entries'] = [{'path': '.', 'type': 'directory'}]
        catalog = prepare_result('catalog', self.catalog, self.context | {'_inventory': self.inventory})
        markdown = render_stage('catalog', catalog)
        self.assertIn('Catalog completion reported by the agent: COMPLETE', markdown)
        self.assertIn('| ID | Subsystem | Purpose | Paths |', markdown)
        self.assertIn('| Path | Reason |', markdown)
        self.assertIn('## Limitations\n\nNone.', markdown)
        self.assertIn('## Unclassified paths\n\nNone.', markdown)

    def test_unclassified_partial_and_fallback_block_acceptance(self):
        self.catalog['exclusions'] = []
        plan = build_coverage_plan(self.catalog, self.inventory, self.context)
        self.assertEqual(plan['unclassified_paths'], ['README.md', 'linked'])
        self.assertEqual(plan['areas'][-1]['id'], 'UNCLASSIFIED')
        self.assertFalse(plan['policy_satisfied'])
        fallback = build_coverage_plan(None, self.inventory, self.context, fallback=True)
        self.assertEqual(fallback['origin'], 'DIRECTORY_FALLBACK')
        self.assertEqual([a['paths'] for a in fallback['areas']], [['README.md'], ['linked'], ['src']])
        self.assertFalse(fallback['policy_satisfied'])
        self.catalog['subsystems'][0]['paths'] = ['.']
        self.catalog.update(completion_status='PARTIAL', limitations=['Incomplete agent classification.'])
        self.assertFalse(build_coverage_plan(self.catalog, self.inventory, self.context)['policy_satisfied'])

    def test_path_boundaries_invalid_paths_and_frozen_hash(self):
        for path in ('src2', '../src', '/src', 'src//app.py', 'src/../src', 'src\\app.py'):
            with self.subTest(path=path):
                catalog = copy.deepcopy(self.catalog)
                catalog['subsystems'][0]['paths'] = [path]
                with self.assertRaises(ContractError):
                    build_coverage_plan(catalog, self.inventory, self.context)
        plan = build_coverage_plan(self.catalog, self.inventory, self.context)
        plan['areas'][0]['name'] = 'Changed'
        with self.assertRaises(ContractError):
            verify_coverage_plan(plan)

    def recover(self, catalog):
        context = self.context | {'_inventory': self.inventory}
        recovered, provenance = recover_catalog_paths(catalog, self.inventory, context)
        validate_result('catalog', recovered, context)
        saved = prepare_result('catalog', recovered, context, catalog_recovery=provenance)
        validate_schema(saved, SAVED_SCHEMAS['catalog'])
        verify_coverage_plan(saved['coverage_plan'])
        return saved, provenance

    def test_recovery_keeps_assignments_exclusions_and_private_selector_locations(self):
        self.catalog['subsystems'][0]['paths'] += ['missing-private']
        self.catalog['subsystems'] += [dict(id='S-002', name='Absent', purpose='Unknown.', paths=['absent'])]
        self.catalog['exclusions'] += [dict(path='src/app.p', reason='Invalid exclusion.')]
        original = copy.deepcopy(self.catalog)
        saved, provenance = self.recover(self.catalog)
        plan = saved['coverage_plan']
        self.assertEqual(plan['origin'], 'AGENT_SALVAGED')
        self.assertEqual(saved['completion_status'], 'PARTIAL')
        self.assertEqual(plan['catalog_status'], 'PARTIAL')
        self.assertFalse(plan['policy_satisfied'])
        self.assertFalse(saved['program_checks']['policy_satisfied'])
        self.assertEqual(saved['program_checks']['completion_self_assessment'], 'COMPLETE')
        self.assertEqual(saved['subsystems'], [original['subsystems'][0] | {'paths': ['src']}])
        self.assertEqual(plan['areas'][0]['file_paths'], ['src/app.py', 'src/lib.py'])
        self.assertEqual(saved['exclusions'], original['exclusions'][:2])
        self.assertEqual(provenance['rejected_selectors'], [
            {'code': 'UNKNOWN_CATALOG_PATH', 'path': '$.subsystems[0].paths[1]', 'selector': 'missing-private'},
            {'code': 'UNKNOWN_CATALOG_PATH', 'path': '$.subsystems[1].paths[0]', 'selector': 'absent'},
            {'code': 'UNKNOWN_CATALOG_PATH', 'path': '$.exclusions[2].path', 'selector': 'src/app.p'}])
        self.assertEqual(provenance['input_sha256'], sha(canonical(original)))
        self.assertEqual(self.catalog, original)
        self.assertNotIn('missing-private', str(saved))
        self.assertIn('AGENT_SALVAGED / PARTIAL', render_stage('catalog', saved))

    def test_recovery_leaves_unallocated_entries_unclassified(self):
        self.catalog['subsystems'][0]['paths'] = ['src/app.py', 'absent']
        saved, _ = self.recover(self.catalog)
        plan = saved['coverage_plan']
        self.assertEqual(plan['unclassified_paths'], ['src/lib.py'])
        self.assertEqual(plan['areas'][-1]['id'], 'UNCLASSIFIED')
        self.assertEqual(plan['counts']['assigned_files'], 1)

    def test_recovery_without_subsystems_uses_deterministic_fallback_and_valid_exclusions(self):
        self.catalog['subsystems'][0]['paths'] = ['absent']
        self.catalog['limitations'] = ['Original catalog limitation.']
        saved, provenance = self.recover(self.catalog)
        plan = saved['coverage_plan']
        self.assertEqual(provenance['origin'], 'DIRECTORY_FALLBACK')
        self.assertEqual(plan['origin'], 'DIRECTORY_FALLBACK')
        self.assertEqual(saved['subsystems'], [])
        self.assertEqual([a['paths'] for a in plan['areas']], [['README.md'], ['linked'], ['src']])
        self.assertEqual(plan['exclusions'][0]['file_paths'], ['README.md'])
        self.assertIn('Original catalog limitation.', plan['limitations'])
        self.assertFalse(plan['policy_satisfied'])
        self.assertEqual(plan, self.recover(self.catalog)[0]['coverage_plan'])

    def test_recovery_does_not_change_valid_catalogs(self):
        recovered, provenance = recover_catalog_paths(self.catalog, self.inventory, self.context)
        self.assertEqual(recovered, self.catalog)
        self.assertIsNone(provenance)
        self.assertEqual(self.recover(self.catalog)[0]['coverage_plan']['origin'], 'AGENT')

    def test_fallback_cannot_apply_exclusions_from_a_different_source_identity(self):
        with self.assertRaises(ContractError):
            build_coverage_plan(self.catalog | {'source_commit': 'another'}, self.inventory, self.context, fallback=True)

    def test_recovery_never_repairs_other_contract_or_source_boundary_failures(self):
        for kind in ('identity', 'schema', 'duplicate_paths', 'duplicate_ids', 'duplicate_exclusions',
                     'empty_paths', 'blank_reason', '../src', '/src', 'src//app.py', 'src/../src', 'src\\app.py'):
            catalog = copy.deepcopy(self.catalog)
            catalog['subsystems'][0]['paths'].append('unknown')
            if kind == 'identity': catalog['source_commit'] = 'another'
            elif kind == 'schema': catalog.pop('limitations')
            elif kind == 'duplicate_paths': catalog['subsystems'][0]['paths'].append('unknown')
            elif kind == 'duplicate_ids': catalog['subsystems'].append(copy.deepcopy(catalog['subsystems'][0]))
            elif kind == 'duplicate_exclusions': catalog['exclusions'] += [dict(path='unknown', reason='Absent.')] * 2
            elif kind == 'empty_paths': catalog['subsystems'][0]['paths'] = []
            elif kind == 'blank_reason': catalog['exclusions'][0]['reason'] = ' '
            else: catalog['exclusions'].append(dict(path=kind, reason='Unsafe.'))
            with self.subTest(kind=kind), self.assertRaises(ContractError):
                recover_catalog_paths(catalog, self.inventory, self.context)

    def test_recovery_does_not_follow_symlink_selectors(self):
        self.catalog['subsystems'][0]['paths'] = ['linked', 'linked/outside.py']
        self.catalog['exclusions'] = []
        saved, provenance = self.recover(self.catalog)
        area = saved['coverage_plan']['areas'][0]
        self.assertEqual(area['paths'], ['linked'])
        self.assertEqual(area['entry_paths'], ['linked'])
        self.assertEqual(area['file_paths'], [])
        self.assertEqual(provenance['rejected_selectors'][0]['selector'], 'linked/outside.py')

    def test_recovered_plan_cannot_claim_complete_or_positive_acceptance_even_if_rehashed(self):
        self.catalog['subsystems'][0]['paths'].append('unknown')
        saved, _ = self.recover(self.catalog)
        for field, value in (('catalog_status', 'COMPLETE'), ('policy_satisfied', True), ('limitations', [])):
            plan = copy.deepcopy(saved['coverage_plan'])
            plan[field] = value
            plan['plan_sha256'] = sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))
            with self.subTest(field=field), self.assertRaises(ContractError):
                verify_coverage_plan(plan)

    def test_unsupported_contract_is_rejected_even_with_matching_hash(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                plan = build_coverage_plan(self.catalog, self.inventory, self.context)
                if missing:
                    plan.pop('contract_id')
                else:
                    plan['contract_id'] = 'unsupported-format'
                plan['plan_sha256'] = sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))
                with self.assertRaises(ContractError):
                    verify_coverage_plan(plan)

    def test_catalog_identity_reserved_ids_and_plan_policy_cannot_be_forged(self):
        for catalog in (self.catalog | {'source_commit': 'another'}, self.catalog | {
                'subsystems': [self.catalog['subsystems'][0] | {'id': 'A-001'}]}):
            with self.assertRaises(ContractError):
                build_coverage_plan(catalog, self.inventory, self.context)
        plan = build_coverage_plan(self.catalog, self.inventory, self.context)
        plan['counts']['assigned_files'] = 999
        plan['plan_sha256'] = sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))
        with self.assertRaises(ContractError):
            verify_coverage_plan(plan)
        plan = build_coverage_plan(None, self.inventory, self.context, fallback=True)
        plan['policy_satisfied'] = True
        plan['plan_sha256'] = sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))
        with self.assertRaises(ContractError):
            verify_coverage_plan(plan)

    def test_empty_area_report_is_required_but_inspection_is_not(self):
        self.catalog['exclusions'].append({'path': 'src', 'reason': 'Explicitly excluded synthetic fixture.'})
        plan = build_coverage_plan(self.catalog, self.inventory, self.context)
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
        plan = build_coverage_plan(self.catalog, self.inventory, self.context)
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
        plan = build_coverage_plan(self.catalog, self.inventory, self.context)
        context = self.context | {'coverage_plan': plan}
        study = {'coverage': [{'area_id': 'S-001', 'status': 'INSPECTED', 'evidence_ids': ['study:E-001'], 'limitation': ''}]}
        resolution = {'id': 'study:E-001', 'source_id': 'source-002', 'path': 'app.py', 'status': 'RESOLVED'}
        self.assertTrue(coverage_checks(study, context, [resolution])['policy_satisfied'])

    def test_saved_catalog_and_study_plan_bind_decoding(self):
        context = self.context | {'_inventory': self.inventory}
        validate_result('catalog', self.catalog, context)
        catalog = prepare_result('catalog', self.catalog, context)
        validate_schema(catalog, SAVED_SCHEMAS['catalog'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'app.py').write_text('print(1)\n')
            inventory = {'entries': [{'path': '.', 'type': 'directory'}, {'path': 'app.py', 'type': 'file'}]}
            wire = self.catalog | {'subsystems': [self.catalog['subsystems'][0] | {'paths': ['.']}], 'exclusions': []}
            plan = build_coverage_plan(wire, inventory, self.context)
            context = self.context | {'repository': str(root.resolve()), 'coverage_plan': plan, 'revision_id': '001'}
            study = prepare_result('study', response(context), context)
            validate_schema(study, SAVED_SCHEMAS['study'])
            self.assertTrue(study['program_checks']['policy_satisfied'])
            review = review_context(study, context)
            self.assertIn('S-001', [a['id'] for a in review['review_plan']['omission_areas']])
            from src.analysis.ledger import freeze_plan
            changed = freeze_plan(study, context | {'source_decoding': {'rules': [{'path': '.', 'encoding': 'cp1251'}]}})
            self.assertNotEqual(changed['plan_sha256'], study['review_plan']['plan_sha256'])
            self.assertEqual(changed['document_sha256'], study['review_plan']['document_sha256'])


if __name__ == '__main__':
    unittest.main()
