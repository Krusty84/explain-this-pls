# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline checks for deterministic reporting of verified study shards."""
import copy
import unittest

from src.reports.final_report import (comparison_possible, has_usable_report_material,
    partial_shard_coverage, render_final_report, usable_study, verified_shards)
from test_folder import FolderFixture
import test_multi_session as multi_session


class PartialShardReportTests(FolderFixture):
    run_case = multi_session.MultiSessionTests.run_case

    def failed_middle(self, *, keep_going=True):
        def change(runner, context, data):
            if context.get('analysis_shard', {}).get('id') == 'R-002':
                data['components'][0]['description'] = 'UNVERIFIED PRIVATE OBSERVATION'
                data['claims'][0]['statement'] = 'UNVERIFIED PRIVATE CLAIM'
                data['claims'][0]['evidence_ids'] = ['study:E-999']
            elif context['stage'] == 'study-shard':
                data['limitations'] = ['Runtime behavior was not checked.']
        return self.run_case(change=change, policy='compromise', keep_going=keep_going)

    def test_only_verified_parts_are_rendered_with_distinct_ids(self):
        manifest, _ = self.failed_middle()
        report = (self.runner.run_dir / 'FINAL_REPORT.md').read_text()
        self.assertTrue(has_usable_report_material(manifest))
        self.assertFalse(usable_study(manifest))
        self.assertIsNone(manifest.get('study'))
        self.assertEqual([s['id'] for s in verified_shards(manifest)], ['R-001', 'R-003'])
        for text in ('R-001/C-001', 'R-003/C-001', 'R-001/study:E-001', 'R-003/study:E-001',
                     'Runtime behavior was not checked.', '| R-002 | FAILED |'):
            self.assertIn(text, report)
        self.assertNotIn('UNVERIFIED PRIVATE', report)
        self.assertEqual(len(self.calls), 4)
        self.assertNotIn('synthesis_invocation', manifest)

    def test_unstarted_shards_and_their_paths_are_explicit_gaps(self):
        manifest, _ = self.failed_middle(keep_going=False)
        report = (self.runner.run_dir / 'FINAL_REPORT.md').read_text()
        self.assertIn('| R-002 | FAILED |', report)
        self.assertIn('| R-003 | PLANNED |', report)
        for path in manifest['analysis_plan']['shards'][2]['primary_file_paths']:
            self.assertIn(path, report)
        records = partial_shard_coverage(manifest, verified_shards(manifest))
        unstarted = set(manifest['analysis_plan']['shards'][2]['subsystem_ids'])
        self.assertTrue(all(r['status'] == 'NOT_INSPECTED' and 'R-003' in r['limitation']
                            for r in records if r['area_id'] in unstarted))

    def test_verification_requires_each_publication_and_identity_guard(self):
        manifest, _ = self.failed_middle()
        changes = {
            'state': lambda item, state, data, meta: state.update(status='FAILED'),
            'invocation': lambda item, state, data, meta: meta.update(status='FAILED'),
            'backend': lambda item, state, data, meta: meta.update(backend_result_valid=False),
            'local': lambda item, state, data, meta: meta.update(local_validation=False),
            'publication': lambda item, state, data, meta: meta.update(publication_complete=False),
            'source_guard': lambda item, state, data, meta: meta.update(source_integrity_verified=False),
            'source_status': lambda item, state, data, meta: meta.update(source_check_status='CHANGED'),
            'source_checks': lambda item, state, data, meta: data['program_checks'].update(source='NOT_INSPECTED_IN_COMPARISON'),
            'identity': lambda item, state, data, meta: data.update(source_fingerprint='different'),
            'assignment': lambda item, state, data, meta: data.update(assigned_subsystem_ids=[]),
            'shard_id': lambda item, state, data, meta: data.update(shard_id='R-999'),
            'completion': lambda item, state, data, meta: data.update(completion_status='PARTIAL'),
            'policy': lambda item, state, data, meta: data['program_checks'].update(policy_satisfied=False),
            'coverage': lambda item, state, data, meta: data['program_checks']['coverage'].update(policy_satisfied=False),
            'evidence': lambda item, state, data, meta: data['program_checks']['evidence'][0].update(status='QUOTE_MISMATCH'),
            'schema': lambda item, state, data, meta: data.update(unexpected=True),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                item = copy.deepcopy(manifest)
                state = item['study_shards'][0]
                change(item, state, state['study-shard'], state['study-shard_invocation'])
                self.assertEqual([s['id'] for s in verified_shards(item)], ['R-003'])

    def test_source_identity_is_checked_in_git_mode(self):
        manifest, _ = self.failed_middle()
        item = copy.deepcopy(manifest)
        item.pop('source_directory')
        item.pop('source_fingerprint')
        item.update(branch='main', source_commit='pinned-commit')
        for state in item['study_shards']:
            if 'study-shard' not in state:
                continue
            data = state['study-shard']
            data.pop('source_directory')
            data.pop('source_fingerprint')
            data.update(branch='main', source_commit='pinned-commit')
        self.assertEqual(len(verified_shards(item)), 2)
        item['study_shards'][0]['study-shard']['source_commit'] = 'other-commit'
        self.assertEqual([s['id'] for s in verified_shards(item)], ['R-003'])
        self.assertFalse(comparison_possible([item, dict(branch='other')], 'main'))

    def test_shared_area_requires_all_planned_contributors(self):
        item = {'coverage_plan': {'areas': [{'id': 'S-001'}]}, 'analysis_plan': {'shards': [
            {'id': 'R-001', 'subsystem_ids': ['S-001']}, {'id': 'R-002', 'subsystem_ids': ['S-001']}]}}
        shards = [dict(id=sid, **{'study-shard': {'coverage': [dict(area_id='S-001', status='INSPECTED',
            evidence_ids=['study:E-001'], limitation='Source evidence only.')]}}) for sid in ('R-001', 'R-002')]
        self.assertEqual(partial_shard_coverage(item, [])[0]['status'], 'NOT_INSPECTED')
        partial = partial_shard_coverage(item, shards[:1])[0]
        self.assertEqual(partial['status'], 'PARTIALLY_INSPECTED')
        self.assertEqual(partial['evidence_ids'], ['R-001/study:E-001'])
        self.assertIn('R-002', partial['limitation'])
        complete = partial_shard_coverage(item, shards)[0]
        self.assertEqual(complete['status'], 'INSPECTED')
        self.assertEqual(complete['evidence_ids'], ['R-001/study:E-001', 'R-002/study:E-001'])
        shards[1]['study-shard']['coverage'][0].update(status='NOT_INSPECTED', limitation='Optional area.')
        self.assertEqual(partial_shard_coverage(item, shards)[0]['status'], 'PARTIALLY_INSPECTED')
        shards[0]['study-shard']['coverage'][0]['status'] = 'NOT_INSPECTED'
        self.assertEqual(partial_shard_coverage(item, shards)[0]['status'], 'NOT_INSPECTED')

    def test_critical_failure_suppresses_partial_material(self):
        manifest, _ = self.failed_middle()
        manifest['critical_failure'] = True
        self.assertEqual(verified_shards(manifest), [])
        self.assertFalse(has_usable_report_material(manifest))
        report, useful = render_final_report(manifest, self.runner.source, 'folder', 'English')
        self.assertFalse(useful)
        self.assertIn('Critical run failure', report)
        self.assertNotIn('R-001/C-001', report)

    def test_empty_results_do_not_count_as_useful_material(self):
        manifest, _ = self.failed_middle()
        empty = copy.deepcopy(manifest)
        for planned in empty['analysis_plan']['shards']:
            planned['source_bytes'] = 0
        self.assertFalse(has_usable_report_material(empty))
        for state in manifest['study_shards']:
            if 'study-shard' in state:
                state['study-shard']['claims'] = []
        self.assertFalse(has_usable_report_material(manifest))

    def test_partial_coverage_displays_verified_empty_file_metadata(self):
        manifest, _ = self.failed_middle()
        data = manifest['study_shards'][0]['study-shard']
        path = manifest['analysis_plan']['shards'][0]['primary_file_paths'][0]
        data['program_checks']['coverage']['metadata_verified_empty_paths'] = [path]
        report, useful = render_final_report(manifest, self.runner.source, 'folder', 'English')
        self.assertTrue(useful)
        self.assertIn('Empty files verified from pinned metadata: ' + path + '.', report)

    def test_successful_full_study_keeps_existing_report_behavior(self):
        manifest, code = self.run_case()
        self.assertEqual(code, 0)
        report = (self.runner.run_dir / 'FINAL_REPORT.md').read_text()
        self.assertTrue(usable_study(manifest))
        self.assertTrue(has_usable_report_material(manifest))
        self.assertNotIn('R-001/C-001', report)
        self.assertNotIn('Partial report from verified shards', report)
        self.assertEqual(len(self.calls), 5)


if __name__ == '__main__':
    unittest.main()
