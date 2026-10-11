# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Pinned empty-file metadata never implies an agent inspection."""
import unittest

from src.analysis.coverage_plan import build_coverage_plan, coverage_checks
from src.analysis.evidence import sha
from src.contracts.contracts import ContractError


class EmptyCoverageTests(unittest.TestCase):
    def context(self, entries=None):
        entries = entries if entries is not None else [dict(path='.codex', type='file', size=0, sha256=sha(b''))]
        context = dict(branch='main', source_commit='abc', source_mode='git')
        context['_inventory'] = dict(entries=[dict(path='.', type='directory'), *entries])
        context['coverage_plan'] = build_coverage_plan(context['_inventory'], context)
        return context

    def test_empty_file_metadata_does_not_measure_inspection(self):
        context = self.context()
        checks = coverage_checks({}, context, [])
        self.assertEqual(checks['metadata_verified_empty_paths'], ['.codex'])
        self.assertEqual(checks['not_cited_ids'], ['S-001'])
        self.assertEqual(checks['unverified_ids'], ['S-001'])
        self.assertFalse(checks['completeness_measured'])
        self.assertEqual(context['coverage_plan']['counts']['files'], 1)

    def test_missing_or_unpinned_inventory_does_not_verify_empty_files(self):
        context = self.context()
        context.pop('_inventory')
        self.assertEqual(coverage_checks({}, context, [])['metadata_verified_empty_paths'], [])
        for changes in ({'size': None}, {'size': False}, {'size': 1}, {'sha256': 'wrong'}, {'sha256': None}):
            with self.subTest(changes=changes):
                entry = dict(path='.codex', type='file', size=0, sha256=sha(b'')) | changes
                checks = coverage_checks({}, self.context([entry]), [])
                self.assertEqual(checks['metadata_verified_empty_paths'], [])
                self.assertEqual(checks['unverified_ids'], ['S-001'])

    def test_changed_inventory_seal_is_rejected(self):
        context = self.context()
        context['_inventory']['entries'][1]['size'] = 1
        with self.assertRaisesRegex(ContractError, 'inventories differ'):
            coverage_checks({}, context, [])

    def test_empty_metadata_is_per_file_and_never_counts_symlinks(self):
        entries = [dict(path='.codex', type='file', size=0, sha256=sha(b'')),
            dict(path='link', type='symlink', size=0, sha256=sha(b''), target='elsewhere'),
            dict(path='empty', type='directory'),
            dict(path='app.py', type='file', size=5, sha256=sha(b'code\n'))]
        checks = coverage_checks({}, self.context(entries), [])
        self.assertEqual(checks['metadata_verified_empty_paths'], ['.codex'])
        self.assertEqual(checks['not_cited_ids'], checks['expected_ids'])
        self.assertEqual(checks['unverified_ids'], checks['expected_ids'])

    def test_agent_coverage_metadata_cannot_change_diagnostics(self):
        context = self.context()
        expected = coverage_checks({}, context, [])
        for coverage in (None, [], 'invalid', [{'area_id': 'S-001', 'status': 'INSPECTED'}],
                [{'area_id': 'S-001', 'status': 'PARTIALLY_INSPECTED'}],
                [{'area_id': 'S-001', 'status': 'NOT_INSPECTED'}]):
            with self.subTest(coverage=coverage):
                self.assertEqual(coverage_checks({'coverage': coverage}, context, []), expected)

    def test_direct_library_coverage_records_no_metadata_without_plan(self):
        checks = coverage_checks({}, {}, [])
        self.assertEqual(checks['metadata_verified_empty_paths'], [])
        self.assertEqual(checks['expected_ids'], [])
