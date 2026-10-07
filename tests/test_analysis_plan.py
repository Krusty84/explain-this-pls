# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from explain import AuditError, Folder, load_config
from src.analysis.analysis_plan import build_analysis_plan, verify_analysis_plan
from src.analysis.coverage_plan import build_coverage_plan
from src.analysis.evidence import canonical, sha
from src.analysis.source_metrics import PhysicalLines, physical_lines
from src.contracts.contracts import ContractError
from test_folder import FolderFixture


def synthetic_plan(subsystems=9, files=9, lines=9, *, enabled=True, overlap=False):
    """Large logical inventory without large on-disk files or provider calls."""
    context = {'branch': 'main', 'source_commit': 'abc'}
    inventory = {'entries': [{'path': '.', 'type': 'directory'}] + [
        {'path': f'f{i:04d}.py', 'type': 'file', 'source_lines': lines // files + (i < lines % files)}
        for i in range(files)]}
    catalog = dict(task='architecture_catalog', branch='main', source_commit='abc', completion_status='COMPLETE',
                   limitations=[], exclusions=[], subsystems=[dict(id=f'S-{i + 1:03d}', name=f'Subsystem {i}',
        purpose='Synthetic scope', paths=['.'] if overlap else [f'f{j:04d}.py' for j in range(i, files, subsystems)] or ['.'])
        for i in range(subsystems)])
    coverage = build_coverage_plan(catalog, inventory, context)
    return build_analysis_plan(inventory, coverage, enabled), inventory, coverage


class AnalysisPlanTests(unittest.TestCase):
    def test_each_threshold_and_maximum(self):
        for s, f, l, n in ((4, 300, 50000, 1), (5, 5, 5, 2), (2, 301, 301, 2),
                           (2, 2, 50001, 2), (9, 601, 150001, 4), (9, 901, 1, 4),
                           (13, 1, 1, 4), (0, 0, 0, 1)):
            with self.subTest(s=s, f=f, l=l):
                plan, inventory, coverage = synthetic_plan(s, f, l)
                self.assertEqual(plan['required_sessions'], n)
                self.assertEqual(len(plan['shards']), n)
                self.assertEqual(verify_analysis_plan(plan, inventory, coverage), plan)

    def test_disabled_forces_one_complete_assignment(self):
        plan, _, _ = synthetic_plan(13, 901, 150001, enabled=False)
        self.assertEqual(plan['required_sessions'], 1)
        self.assertEqual(plan['shards'][0]['subsystem_count'], 13)
        self.assertFalse(plan['multi_session'])

    def test_deterministic_bytes_ties_and_exact_assignment(self):
        plan, inventory, coverage = synthetic_plan()
        self.assertEqual(canonical(plan), canonical(build_analysis_plan(inventory, coverage)))
        self.assertEqual([s['subsystem_ids'] for s in plan['shards']], [
            ['S-001', 'S-004', 'S-007'], ['S-002', 'S-005', 'S-008'], ['S-003', 'S-006', 'S-009']])
        assigned = [sid for shard in plan['shards'] for sid in shard['subsystem_ids']]
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(sorted(assigned), [s['subsystem_id'] for s in plan['subsystems']])
        reversed_coverage = copy.deepcopy(coverage)
        reversed_coverage['areas'].reverse()
        reversed_coverage['plan_sha256'] = sha(canonical({k: v for k, v in reversed_coverage.items() if k != 'plan_sha256'}))
        other = build_analysis_plan(inventory, reversed_coverage)
        self.assertEqual(other['shards'], plan['shards'])

    def test_normalized_cost_and_load(self):
        plan, inventory, coverage = synthetic_plan(3, 3, 3)
        inventory['entries'][1]['source_lines'] = 50001
        inventory['entries'][2]['source_lines'] = 25000
        coverage['inventory_sha256'] = sha(canonical(inventory['entries']))
        coverage['plan_sha256'] = sha(canonical({k: v for k, v in coverage.items() if k != 'plan_sha256'}))
        plan = build_analysis_plan(inventory, coverage)
        self.assertEqual(plan['shards'][0]['subsystem_ids'], ['S-001'])
        self.assertEqual(plan['shards'][0]['normalized_load'], 50001 / 50000)
        self.assertEqual(plan['shards'][1]['subsystem_ids'], ['S-002', 'S-003'])

    def test_oversized_indivisible_subsystem_keeps_empty_bins_visible(self):
        plan, _, _ = synthetic_plan(1, 901, 200001)
        self.assertEqual(plan['required_sessions'], 5)
        self.assertEqual(plan['shards'][0]['subsystem_ids'], ['S-001'])
        self.assertTrue(plan['shards'][0]['over_capacity'])
        self.assertTrue(all(s['subsystem_ids'] == [] and s['normalized_load'] == 0 for s in plan['shards'][1:]))

    def test_overlaps_count_once_per_shard_and_once_globally(self):
        plan, _, _ = synthetic_plan(9, 6, 60, overlap=True)
        self.assertEqual(plan['totals'], dict(subsystems=9, source_files=6, source_lines=60))
        self.assertTrue(all(s['source_files'] == 6 and s['source_lines'] == 60 for s in plan['shards']))
        self.assertEqual(plan['shards'][0]['normalized_load'], .75)
        self.assertTrue(all(s['file_count'] == 6 for s in plan['subsystems']))

    def test_plan_validation_rejects_tampering_even_with_recomputed_seal(self):
        plan, inventory, coverage = synthetic_plan()
        changes = [lambda p: p.update(required_sessions=1), lambda p: p['totals'].update(source_files=99),
                   lambda p: p['shards'][0]['subsystem_ids'].append('S-999'),
                   lambda p: p['shards'][1].update(id='R-001'),
                   lambda p: p['shards'][0]['subsystem_ids'].pop(),
                   lambda p: p['shards'][1]['subsystem_ids'].append('S-001'),
                   lambda p: p['shards'][0].update(source_lines=100), lambda p: p.update(multi_session=False),
                   lambda p: p['totals'].update(source_files=9.0)]
        for change in changes:
            altered = copy.deepcopy(plan)
            change(altered)
            altered['plan_sha256'] = sha(canonical({k: v for k, v in altered.items() if k != 'plan_sha256'}))
            with self.assertRaises(ContractError):
                verify_analysis_plan(altered, inventory, coverage)

    def test_unclassified_files_still_count_in_global_inventory(self):
        plan, _, _ = synthetic_plan(0, 301, 60000)
        self.assertEqual(plan['totals'], dict(subsystems=0, source_files=301, source_lines=60000))
        self.assertEqual(plan['required_sessions'], 2)

    def test_catalog_exclusions_change_membership_but_not_inventory_totals(self):
        _, inventory, _ = synthetic_plan(1, 301, 60000)
        context = {'branch': 'main', 'source_commit': 'abc'}
        catalog = dict(task='architecture_catalog', **context, completion_status='COMPLETE', limitations=[],
            subsystems=[dict(id='S-001', name='Source', purpose='Fixture', paths=['.'])],
            exclusions=[dict(path='f0000.py', reason='Excluded fixture content.')])
        coverage = build_coverage_plan(catalog, inventory, context)
        plan = build_analysis_plan(inventory, coverage)
        self.assertEqual(plan['totals'], dict(subsystems=1, source_files=301, source_lines=60000))
        self.assertEqual(plan['subsystems'][0]['file_count'], 300)
        self.assertEqual(plan['subsystems'][0]['source_lines'], 59800)


class MultiSessionConfigTests(FolderFixture):
    def test_omitted_explicit_and_invalid_values(self):
        self.assertIs(self.config()['multi_session'], True)
        for value in (True, False):
            self.value['multi_session'] = value
            self.assertIs(self.config()['multi_session'], value)
        for value in (0, 1, 'true', 'false', None, [], {}):
            self.value['multi_session'] = value
            with self.subTest(value=value), self.assertRaisesRegex(AuditError, 'multi_session must be boolean'):
                self.config()

    def test_all_active_configuration_examples_show_true(self):
        for path in Path(__file__).resolve().parents[1].glob('config*.jsonc'):
            with self.subTest(path=path), patch('explain.shutil.which', return_value=sys.executable):
                self.assertIs(load_config(path)['multi_session'], True)
                self.assertIn('sequentially, not concurrently', path.read_text())

    def test_physical_line_definition_and_chunk_boundaries(self):
        for blob, count in ((b'', 0), (b'\n', 1), (b'a', 1), (b'a\n\n', 2), (b'a\r\nb', 2),
                            (b'a\rb', 1), (b'\xff\x00\n ', 2), (b'a\n\nb', 3)):
            with self.subTest(blob=blob):
                self.assertEqual(physical_lines(blob), count)
                for chunk_size in (1, 2, 10):
                    counter = PhysicalLines()
                    for offset in range(0, len(blob), chunk_size):
                        counter.update(blob[offset:offset + chunk_size])
                    counter.update(b'')
                    self.assertEqual(counter.count, count)

    def test_inventory_counts_during_hash_read_and_guards_do_not_read_again(self):
        blob = b'x' * (1024 * 1024 - 1) + b'\n\nlast'
        (self.source / 'app.py').write_bytes(blob)
        (self.source / 'empty').mkdir()
        (self.source / 'link').symlink_to('app.py')
        folder = Folder(self.source)
        with patch('os.read', wraps=os.read) as read:
            inventory = folder.snapshot()
            self.assertEqual(sum(len(c.args) == 2 for c in read.call_args_list), 3)
        file_entries = [e for e in inventory['entries'] if e['type'] == 'file']
        self.assertEqual(len(file_entries), 1)
        self.assertEqual(file_entries[0]['source_lines'], 3)
        with patch('os.read', side_effect=AssertionError('Second source-content read')):
            folder.assert_snapshot(inventory['source_fingerprint'])
