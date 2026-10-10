# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import copy
import tempfile
import unittest
from pathlib import Path

from src.analysis.analysis_plan import build_analysis_plan
from src.analysis.coverage_plan import build_coverage_plan, coverage_checks
from src.analysis.evidence import sha
from src.analysis.ledger import prepare_result
from src.analysis.study_shards import OBSERVATION_FIELDS, require_shard_policy, validate_shard
from src.contracts.contracts import ContractError, validate_schema
from src.contracts.saved_contracts import SAVED_SCHEMAS


class EmptyShardCoverageTests(unittest.TestCase):
    def context(self, entries=None, primary=None, selectors=None):
        entries = entries if entries is not None else [dict(path='.codex', type='file', size=0, sha256=sha(b''))]
        context = dict(branch='main', source_commit='abc', source_mode='git')
        context['_inventory'] = dict(entries=[dict(path='.', type='directory'), *entries])
        catalog = dict(task='architecture_catalog', branch='main', source_commit='abc',
            completion_status='COMPLETE', limitations=[], exclusions=[],
            subsystems=[dict(id='S-001', name='Source', purpose='Source files.', paths=selectors or ['.'])])
        context['coverage_plan'] = build_coverage_plan(catalog, context['_inventory'], context)
        context['analysis_shard'] = dict(id='R-001', subsystem_ids=['S-001'],
            primary_file_paths=primary if primary is not None else [e['path'] for e in entries])
        return context

    def response(self, context):
        return dict(task='architecture_study_shard', branch='main', source_commit='abc',
            shard_id='R-001', assigned_subsystem_ids=['S-001'], completion_status='COMPLETE',
            coverage=[dict(area_id='S-001', status='INSPECTED', evidence_ids=[], limitation='')],
            **{field: [] for field in (*OBSERVATION_FIELDS, 'evidence', 'claims', 'limitations')})

    def checks(self, context, data=None, *, shard=True):
        return coverage_checks(data or self.response(context), context, [],
                               area_ids=['S-001'] if shard else None)

    def test_empty_file_is_covered_and_counted_without_line_evidence_or_claims(self):
        context = self.context()
        data = self.response(context)
        validate_shard(data, context)
        saved = prepare_result('study-shard', data, context)
        require_shard_policy(saved, context)
        self.assertTrue(saved['program_checks']['policy_satisfied'])
        self.assertEqual(saved['program_checks']['coverage']['metadata_verified_empty_paths'], ['.codex'])
        self.assertEqual(saved['program_checks']['evidence'], [])
        plan = build_analysis_plan(context['_inventory'], context['coverage_plan'])
        self.assertEqual(plan['totals']['source_files'], 1)
        self.assertEqual(plan['totals']['source_bytes'], 0)
        self.assertEqual(plan['shards'][0]['primary_file_paths'], ['.codex'])

    def test_saved_optional_metadata_is_backwards_compatible(self):
        context = self.context()
        saved = prepare_result('study-shard', self.response(context), context)
        validate_schema(saved, SAVED_SCHEMAS['study-shard'])
        saved['program_checks']['coverage'].pop('metadata_verified_empty_paths')
        validate_schema(saved, SAVED_SCHEMAS['study-shard'])

    def test_empty_area_is_also_covered_for_full_study(self):
        checks = self.checks(self.context(), shard=False)
        self.assertTrue(checks['policy_satisfied'])
        self.assertEqual(checks['metadata_verified_empty_paths'], ['.codex'])

    def test_missing_or_unpinned_inventory_does_not_grant_coverage(self):
        context = self.context()
        context.pop('_inventory')
        self.assertEqual(self.checks(context)['unsupported_ids'], ['S-001'])
        for changes in ({'size': None}, {'size': False}, {'size': 1}, {'sha256': 'wrong'}, {'sha256': None}):
            with self.subTest(changes=changes):
                entry = dict(path='.codex', type='file', size=0, sha256=sha(b'')) | changes
                checks = self.checks(self.context([entry]))
                self.assertEqual(checks['unsupported_ids'], ['S-001'])
                self.assertEqual(checks['metadata_verified_empty_paths'], [])

    def test_changed_inventory_seal_is_rejected(self):
        context = self.context()
        context['_inventory']['entries'][1]['size'] = 1
        with self.assertRaisesRegex(ContractError, 'inventories differ'):
            self.checks(context)

    def test_zero_length_symlinks_are_never_empty_files(self):
        context = self.context([dict(path='link', type='symlink', size=0, sha256=sha(b''), target='elsewhere')])
        for shard in (True, False):
            with self.subTest(shard=shard):
                checks = self.checks(context, shard=shard)
                self.assertEqual(checks['unsupported_ids'], ['S-001'])
                self.assertEqual(checks['metadata_verified_empty_paths'], [])

    def test_empty_intersection_and_empty_directory_do_not_grant_coverage(self):
        contexts = (self.context(primary=[]), self.context(primary=['another-file']),
                    self.context([dict(path='empty', type='directory')], primary=[], selectors=['empty']))
        for context in contexts:
            with self.subTest(primary=context['analysis_shard']['primary_file_paths']):
                self.assertEqual(self.checks(context)['unsupported_ids'], ['S-001'])

    def test_mixed_scope_still_requires_line_evidence(self):
        entries = [dict(path='.codex', type='file', size=0, sha256=sha(b'')),
                   dict(path='app.py', type='file', size=5, sha256=sha(b'code\n'))]
        context = self.context(entries)
        saved = prepare_result('study-shard', self.response(context), context)
        self.assertFalse(saved['program_checks']['policy_satisfied'])
        self.assertEqual(saved['program_checks']['coverage']['metadata_verified_empty_paths'], [])
        with self.assertRaisesRegex(ContractError, 'claims=0'):
            require_shard_policy(saved, context)
        self.assertEqual(self.checks(context, shard=False)['unsupported_ids'], ['S-001'])

    def test_empty_primary_batch_can_cover_part_of_mixed_global_area(self):
        context = self.context([dict(path='.codex', type='file', size=0, sha256=sha(b'')),
                                dict(path='app.py', type='file', size=5, sha256=sha(b'code\n'))],
                               primary=['.codex'])
        saved = prepare_result('study-shard', self.response(context), context)
        self.assertTrue(saved['program_checks']['policy_satisfied'])
        self.assertEqual(saved['program_checks']['coverage']['metadata_verified_empty_paths'], ['.codex'])
        self.assertFalse(self.checks(context, shard=False)['policy_satisfied'])

    def test_partial_or_missing_coverage_is_not_promoted_by_metadata(self):
        context = self.context()
        for status in ('PARTIALLY_INSPECTED', 'NOT_INSPECTED', None):
            with self.subTest(status=status):
                data = self.response(context)
                if status is None:
                    data['coverage'] = []
                else:
                    data['coverage'][0].update(status=status, limitation='Not yet inspected.')
                checks = self.checks(context, data)
                self.assertFalse(checks['policy_satisfied'])
                self.assertEqual(checks['metadata_verified_empty_paths'], [])

    def test_fake_line_one_on_empty_file_still_fails_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / '.codex').write_bytes(b'')
            context = self.context() | {'repository': str(root)}
            data = self.response(context)
            data['evidence'] = [dict(id='E-001', source_id='source-001', path='.codex',
                                     start_line=1, end_line=1, quote='')]
            data['coverage'][0]['evidence_ids'] = ['study:E-001']
            saved = prepare_result('study-shard', data, context)
            self.assertEqual(saved['program_checks']['evidence'][0]['status'], 'OUT_OF_RANGE')
            self.assertFalse(saved['program_checks']['policy_satisfied'])
            with self.assertRaisesRegex(ContractError, 'OUT_OF_RANGE=1'):
                require_shard_policy(saved, context)

    def test_direct_library_coverage_records_no_metadata_without_plan(self):
        checks = coverage_checks({'coverage': []}, {}, [])
        self.assertEqual(checks['metadata_verified_empty_paths'], [])

    def test_relationship_errors_identify_field_and_index_without_echoing_values(self):
        context = self.context()
        original = self.response(context)
        original['claims'] = [dict(id='C-001', statement='Unknown dependency.', scope='Source',
            epistemic_kind='UNKNOWN', evidence_ids=[], uncertainty='Dependency needs inspection.')]
        original['relationships'] = [dict(subsystem_id='S-001', related_path='.codex',
                                          description='Dependency.', claim_ids=['C-001'])] * 2
        for field, code in (('subsystem_id', 'UNASSIGNED_RELATIONSHIP_SUBSYSTEM'),
                            ('related_path', 'UNKNOWN_RELATIONSHIP_PATH')):
            data = copy.deepcopy(original)
            data['relationships'][1] = data['relationships'][1] | {field: 'private-untrusted-value'}
            with self.subTest(field=field), self.assertRaises(ContractError) as failure:
                validate_shard(data, context)
            self.assertEqual(failure.exception.details, dict(code=code, path=f'$.relationships[1].{field}'))
            self.assertNotIn('private-untrusted-value', failure.exception.safe_message)


if __name__ == '__main__':
    unittest.main()
