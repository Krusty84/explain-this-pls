# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Pinned metadata can cover empty files, but cannot imply an inspection."""
import unittest

from src.analysis.coverage_plan import build_coverage_plan, coverage_checks
from src.analysis.evidence import sha
from src.contracts.contracts import ContractError


class EmptyCoverageTests(unittest.TestCase):
    def context(self, entries=None):
        entries = entries if entries is not None else [dict(path='.codex', type='file', size=0, sha256=sha(b''))]
        context = dict(branch='main', source_commit='abc', source_mode='git')
        context['_inventory'] = dict(entries=[dict(path='.', type='directory'), *entries])
        catalog = dict(task='architecture_catalog', branch='main', source_commit='abc',
            completion_status='COMPLETE', limitations=[], exclusions=[],
            subsystems=[dict(id='S-001', name='Source', purpose='Source files.', paths=['.'])])
        context['coverage_plan'] = build_coverage_plan(catalog, context['_inventory'], context)
        return context

    def checks(self, context, status='INSPECTED'):
        data = {'coverage': [] if status is None else [dict(area_id='S-001', status=status,
                                                          evidence_ids=[], limitation='')]}
        return coverage_checks(data, context, [])

    def test_empty_file_is_covered_without_line_evidence(self):
        context = self.context()
        checks = self.checks(context)
        self.assertTrue(checks['policy_satisfied'])
        self.assertEqual(checks['metadata_verified_empty_paths'], ['.codex'])
        self.assertEqual(context['coverage_plan']['counts']['files'], 1)

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

    def test_mixed_scope_symlinks_and_directories_still_require_evidence(self):
        empty = dict(path='.codex', type='file', size=0, sha256=sha(b''))
        for entries in ([dict(path='link', type='symlink', size=0, sha256=sha(b''), target='elsewhere')],
                        [dict(path='empty', type='directory')],
                        [empty, dict(path='app.py', type='file', size=5, sha256=sha(b'code\n'))]):
            with self.subTest(entries=entries):
                checks = self.checks(self.context(entries))
                self.assertEqual(checks['unsupported_ids'], ['S-001'])
                self.assertEqual(checks['metadata_verified_empty_paths'], [])

    def test_partial_or_missing_coverage_is_not_promoted_by_metadata(self):
        for status in ('PARTIALLY_INSPECTED', 'NOT_INSPECTED', None):
            with self.subTest(status=status):
                checks = self.checks(self.context(), status)
                self.assertFalse(checks['policy_satisfied'])
                self.assertEqual(checks['metadata_verified_empty_paths'], [])

    def test_direct_library_coverage_records_no_metadata_without_plan(self):
        checks = coverage_checks({'coverage': []}, {}, [])
        self.assertEqual(checks['metadata_verified_empty_paths'], [])
