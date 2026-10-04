# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline catalog/revision pipeline with the shared synthetic CLI response."""
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from explain import Runner
from src.runtime.reporting import Reporter
from fixtures.ledger_response import response
from test_folder import FolderFixture
import test_explain as git_fixtures


def finding(severity='HIGH', claim_id='C-001'):
    return dict(id='F-001', severity=severity, type='FACTUAL_ERROR', claim_ids=[claim_id],
        location='Architecture overview', evidence_ids=['review:E-001'],
        impact='The stated execution behavior is misleading.', proposed_correction='Correct the execution description.')


def initial_finding(stage, context, data, severity='HIGH'):
    if stage == 'review' and context['revision_id'] == '001':
        data['findings'] = [finding(severity)]
        if severity != 'LOW':
            data['claims'][0].update(outcome='CONTRADICTED')
    return data


class RevisionPipelineTests(FolderFixture):
    def run_case(self, change=None, *, rounds=1, policy='strict', continue_on_error=True, verify_immutable=True):
        self.value.update(result_policy=policy, continue_on_error=continue_on_error,
                          execution={'max_revision_rounds': rounds}, output_language='English')
        config = self.config()
        run_dir = self.base / ('revision-run-' + str(len(list(self.base.glob('revision-run-*')))))
        runner = Runner(config, run_dir)
        calls, originals, frozen_first = [], {}, {}

        def process(command, cwd, env, payload, **kwargs):
            context = json.loads(payload.decode().split('# Authoritative orchestration context (data)\n', 1)[1]
                                 .split('\n\n# Required final JSON Schema', 1)[0])
            stage = context['stage']
            calls.append(copy.deepcopy(context))
            if context.get('revision_id') == '002' and not frozen_first:
                first = run_dir / 'revisions' / '001'
                frozen_first.update({str(p.relative_to(first)): p.read_bytes() for p in first.rglob('*') if p.is_file()})
            data = response(context)
            outcome = change(stage, context, data) if change else data
            if outcome == 'transport_failure':
                return {'returncode': 17, 'stdout': b'', 'stderr': b'fixture transport failed'}
            if outcome == 'invalid_json':
                return {'returncode': 0, 'stdout': b'{broken', 'stderr': b''}
            originals[(stage, context.get('revision_id'))] = copy.deepcopy(outcome)
            return {'returncode': 0, 'stdout': json.dumps(outcome).encode(), 'stderr': b''}

        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process):
            manifest, code = runner.run()
        self.runner, self.run_dir = runner, run_dir
        self.calls, self.originals = calls, originals
        self.manifest = manifest
        if frozen_first and verify_immutable:
            first = run_dir / 'revisions' / '001'
            self.assertEqual(frozen_first, {str(p.relative_to(first)): p.read_bytes()
                                          for p in first.rglob('*') if p.is_file()})
        return manifest, code

    def stages(self):
        return ['revise' if c.get('prompt_variant') == 'revise' else c['stage'] for c in self.calls]

    def assert_selected_aliases(self, revision_id, *, review=True):
        self.assertEqual(self.manifest['selected_revision'], revision_id)
        selected = self.run_dir / 'revisions' / revision_id
        names = ['ARCHITECTURE.md', 'study.json', 'claim.registry.json', 'review.plan.json']
        if review:
            names += ['review.json', 'ARCHITECTURE_REVIEW.md']
        for name in names:
            with self.subTest(alias=name):
                self.assertEqual((self.run_dir / name).read_bytes(), (selected / name).read_bytes())
        for stage in ('study', 'review') if review else ('study',):
            self.assertEqual(self.manifest[stage]['revision_id'], revision_id)
        if review:
            self.assertEqual(self.manifest['review']['target']['document_sha256'],
                             self.manifest['study']['review_plan']['document_sha256'])
            self.assertEqual(self.manifest['review']['claim_registry'], self.manifest['study']['claims'])
        self.assertEqual(json.loads((self.run_dir / 'manifest.json').read_text())['selected_revision'], revision_id)

    def test_initial_pass_and_low_findings_do_not_revise(self):
        for severity in (None, 'LOW'):
            with self.subTest(severity=severity):
                change = None if severity is None else lambda s, c, d: initial_finding(s, c, d, severity)
                manifest, code = self.run_case(change)
                self.assertEqual((code, manifest['status'], manifest['accepted']), (0, 'COMPLETE', True))
                self.assertEqual(self.stages(), ['catalog', 'study', 'review'])
                self.assertEqual(len(manifest['revisions']), 1)
                self.assert_selected_aliases('001')

    def test_material_findings_trigger_one_full_revision_and_frozen_coverage(self):
        for severity in ('HIGH', 'MEDIUM'):
            with self.subTest(severity=severity):
                manifest, code = self.run_case(lambda s, c, d: initial_finding(s, c, d, severity))
                self.assertEqual((code, manifest['status'], manifest['accepted']), (0, 'COMPLETE', True))
                self.assertEqual(self.stages(), ['catalog', 'study', 'review', 'revise', 'review'])
                self.assertEqual(len(manifest['revisions']), 2)
                self.assert_selected_aliases('002')
                coverage = json.loads((self.run_dir / 'coverage.plan.json').read_text())
                for revision in manifest['revisions']:
                    self.assertEqual(revision['study']['review_plan']['coverage_plan'], coverage)
                    self.assertEqual(revision['review']['review_plan']['coverage_plan'], coverage)
                repeated = manifest['revisions'][1]['review']
                self.assertEqual(repeated['prior_findings'], [{'revision_id': '001', 'finding_id': 'F-001',
                    'status': 'RESOLVED', 'explanation': 'The revised fixture addresses this finding.'}])
                second = self.run_dir / 'revisions' / '002'
                self.assertTrue((second / 'registry.diff.json').is_file())
                self.assertEqual(manifest['study_invocation']['prompt_variant'], 'revise')
                self.assertEqual(self.calls[-1]['review_plan']['required_claim_ids'],
                                 [c['id'] for c in manifest['study']['claims']])

    def test_disabled_revision_stops_after_initial_material_review(self):
        manifest, code = self.run_case(initial_finding, rounds=0)
        self.assertEqual((code, manifest['status'], manifest['accepted']), (2, 'PARTIAL', False))
        self.assertEqual(self.stages(), ['catalog', 'study', 'review'])
        self.assert_selected_aliases('001')

    def test_revision_report_paths_are_visible_before_the_next_stage(self):
        err = io.StringIO()
        reporter = Reporter(stdout=io.StringIO(), stderr=err, progress=False)
        self.addCleanup(reporter.close)
        observed = []
        def change(stage, context, data):
            observed.append(err.getvalue())
            return initial_finding(stage, context, data)
        with patch('explain.NullReporter', return_value=reporter):
            manifest, code = self.run_case(change)
        self.assertEqual(code, 0)
        paths = [self.run_dir / 'SUBSYSTEM_CATALOG.md',
                 self.run_dir / 'revisions/001/ARCHITECTURE.md',
                 self.run_dir / 'revisions/001/ARCHITECTURE_REVIEW.md',
                 self.run_dir / 'revisions/002/ARCHITECTURE.md',
                 self.run_dir / 'revisions/002/ARCHITECTURE_REVIEW.md']
        for index, snapshot in enumerate(observed):
            self.assertEqual([line for line in snapshot.splitlines() if line.startswith('      Report: ')],
                             ['      Report: ' + str(path) for path in paths[:index]])
        self.assertEqual([line for line in err.getvalue().splitlines() if line.startswith('      Report: ')],
                         ['      Report: ' + str(path) for path in paths])
        self.assertIn('[WARN] source / Report review may be incomplete.', err.getvalue())
        self.assertIn('[OK] source / Revised architecture report created.', err.getvalue())

    def test_partial_initial_review_and_recovered_study_never_trigger_revision(self):
        for defect in ('partial_review', 'recovered_study'):
            def change(stage, context, data):
                initial_finding(stage, context, data)
                if defect == 'partial_review' and stage == 'review':
                    data.update(completion_status='PARTIAL', limitations=['Reviewer did not finish.'])
                if defect == 'recovered_study' and stage == 'study':
                    data['unexpected'] = True
                return data
            with self.subTest(defect=defect):
                manifest, code = self.run_case(change, policy='compromise')
                self.assertFalse(manifest['accepted'])
                self.assertNotEqual(code, 0)
                self.assertEqual(self.stages(), ['catalog', 'study', 'review'])
                self.assertEqual(len(manifest['revisions']), 1)

    def test_failed_and_recovered_studies_do_not_announce_unpublished_reports(self):
        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                err = io.StringIO()
                reporter = Reporter(stdout=io.StringIO(), stderr=err, progress=False)
                self.addCleanup(reporter.close)
                def change(stage, context, data):
                    return (data | {'unexpected': True} if recovered else 'invalid_json') if stage == 'study' else data
                with patch('explain.NullReporter', return_value=reporter):
                    manifest, code = self.run_case(change, policy='compromise')
                self.assertNotEqual(code, 0)
                expected = [self.run_dir / 'SUBSYSTEM_CATALOG.md']
                if recovered:
                    self.assertTrue(manifest['revisions'][0]['study_invocation']['material_retained'])
                    expected.append(self.run_dir / 'revisions/001/ARCHITECTURE_REVIEW.md')
                else:
                    self.assertIn('[SKIP] source / Review skipped:', err.getvalue())
                self.assertEqual([line for line in err.getvalue().splitlines() if line.startswith('      Report: ')],
                                 ['      Report: ' + str(path) for path in expected])

    def test_failed_revised_study_or_review_keeps_prior_pair_and_immutable_artifacts(self):
        for failed_stage in ('study', 'review'):
            def change(stage, context, data):
                initial_finding(stage, context, data)
                if context.get('revision_id') == '002' and stage == failed_stage:
                    return 'transport_failure'
                return data
            with self.subTest(failed_stage=failed_stage):
                manifest, code = self.run_case(change)
                self.assertNotEqual(code, 0)
                self.assertFalse(manifest['accepted'])
                self.assert_selected_aliases('001')
                revised = manifest['revisions'][1]
                self.assertIsNone(revised['review'])
                if failed_stage == 'study':
                    self.assertIsNone(revised['study'])
                else:
                    self.assertIsNotNone(revised['study'])
                expected = ['catalog', 'study', 'review', 'revise'] + ([] if failed_stage == 'study' else ['review'])
                self.assertEqual(self.stages(), expected)
                self.assertEqual(manifest['review']['revision_id'], '001')
                self.assertTrue(manifest['revision_history'][0]['review_complete'])
                self.assertFalse(manifest['revision_history'][1]['review_complete'])

    def test_partial_second_review_is_preserved_separately_but_not_selected(self):
        def change(stage, context, data):
            initial_finding(stage, context, data)
            if stage == 'review' and context['revision_id'] == '002':
                data.update(completion_status='PARTIAL', limitations=['Second review was not finished.'])
            return data
        manifest, code = self.run_case(change)
        self.assertEqual(code, 2)
        self.assert_selected_aliases('001')
        self.assertEqual(self.stages(), ['catalog', 'study', 'review', 'revise', 'review'])
        revised = manifest['revisions'][1]
        self.assertEqual(revised['study']['revision_id'], '002')
        self.assertEqual(revised['review']['revision_id'], '002')
        self.assertEqual(revised['review']['completion_status'], 'PARTIAL')
        self.assertTrue((self.run_dir / 'revisions' / '002' / 'review.json').is_file())
        self.assertFalse(manifest['revision_history'][1]['review_complete'])
        final = Path(manifest['final_report']).read_text()
        self.assertIn('Additional material for revision 002', final)
        self.assertIn('This revision has no complete strictly valid review.', final)
        self.assertIn('Second review was not finished.', final)

    def test_recovered_revised_study_keeps_the_previous_verified_pair(self):
        def change(stage, context, data):
            initial_finding(stage, context, data)
            if stage == 'study' and context['revision_id'] == '002':
                data['unexpected'] = True
            return data
        manifest, code = self.run_case(change, policy='compromise')
        self.assertEqual((code, manifest['status'], manifest['critical_failure']), (2, 'PARTIAL', False))
        self.assert_selected_aliases('001')
        self.assertIsNone(manifest['revisions'][1]['study'])
        self.assertIsNotNone(manifest['revisions'][1]['study_material'])
        self.assertFalse(manifest['revision_history'][1]['review_complete'])

    def test_latest_complete_pair_selected_even_when_material_findings_remain(self):
        for failure in ('unresolved', 'not_checked', 'missing', 'new_finding'):
            def change(stage, context, data):
                initial_finding(stage, context, data)
                if stage == 'review' and context['revision_id'] == '002':
                    if failure == 'missing':
                        data['prior_findings'] = []
                    elif failure == 'new_finding':
                        data['findings'] = [finding() | {'id': 'F-002'}]
                    else:
                        data['prior_findings'][0]['status'] = 'UNRESOLVED' if failure == 'unresolved' else 'NOT_CHECKED'
                return data
            with self.subTest(failure=failure):
                manifest, code = self.run_case(change)
                self.assertEqual((code, manifest['accepted']), (2, False))
                self.assert_selected_aliases('002')
                self.assertEqual(self.stages(), ['catalog', 'study', 'review', 'revise', 'review'])
                self.assertEqual(len(manifest['revisions']), 2)

    def test_no_complete_review_selects_last_usable_study_without_old_review_alias(self):
        def change(stage, context, data):
            if stage == 'review':
                data.update(completion_status='PARTIAL', limitations=['Initial review was not finished.'])
            return data
        manifest, code = self.run_case(change)
        self.assertEqual(code, 2)
        self.assert_selected_aliases('001', review=False)
        self.assertIsNone(manifest['review'])
        self.assertFalse((self.run_dir / 'review.json').exists())
        self.assertIsNotNone(manifest['revisions'][0]['review'])
        self.assertFalse(manifest['revision_history'][0]['review_complete'])
        self.assertIn('This revision has no complete strictly valid review.', Path(manifest['final_report']).read_text())

    def test_deleted_claim_remains_visible_to_reviewer_and_requires_explicit_resolution(self):
        for resolved in (True, False):
            def change(stage, context, data):
                if stage == 'study':
                    retained = data['claims'][0] | {'id': 'C-002', 'statement': 'Secondary static behavior'}
                    block = {'markdown': 'C-002: Secondary static behavior\n', 'claim_ids': ['C-002']}
                    if context['revision_id'] == '001':
                        data['claims'].append(retained)
                        data['report_sections'][0]['blocks'].append(block)
                    else:
                        data['claims'] = [retained]
                        data['report_sections'][0]['blocks'] = [block]
                initial_finding(stage, context, data)
                if not resolved and stage == 'review' and context['revision_id'] == '002':
                    data['prior_findings'][0].update(status='UNRESOLVED',
                        explanation='Removing the required description leaves a material omission.')
                return data
            with self.subTest(resolved=resolved):
                manifest, code = self.run_case(change)
                self.assertEqual((code, manifest['accepted']), (0, True) if resolved else (2, False))
                self.assert_selected_aliases('002')
                repeated_context = self.calls[-1]
                self.assertEqual(repeated_context['registry_diff']['removed_ids'], ['C-001'])
                self.assertEqual(repeated_context['registry_diff']['unchanged_ids'], ['C-002'])
                self.assertEqual(repeated_context['prior_findings'], [{'revision_id': '001', 'finding_id': 'F-001'}])
                previous = repeated_context['previous_revision']
                self.assertEqual(previous['revision_id'], '001')
                self.assertIn('C-001', [c['id'] for c in previous['study']['claims']])
                self.assertEqual(previous['review']['findings'][0]['claim_ids'], ['C-001'])
                self.assertEqual([c['id'] for c in repeated_context['claim_registry']], ['C-002'])

    def test_decoding_rules_enter_prompts_and_frozen_plan_identity(self):
        plans = []
        original_bytes = (self.source / 'app.py').read_bytes()
        for encoding in ('cp1251', 'utf-8'):
            with self.subTest(encoding=encoding):
                self.value['source_decoding'] = {'rules': [{'path': '.', 'encoding': encoding}]}
                manifest, code = self.run_case()
                self.assertEqual(code, 0)
                self.assertTrue(all(context['source_decoding'] == self.value['source_decoding'] for context in self.calls))
                plans.append(manifest['study']['review_plan'])
                self.assertEqual(plans[-1]['source_decoding'], self.value['source_decoding'])
                self.assertEqual(manifest['study']['program_checks']['evidence'][0]['encoding'], encoding)
        self.assertNotEqual(plans[0]['plan_sha256'], plans[1]['plan_sha256'])
        self.assertEqual(plans[0]['document_sha256'], plans[1]['document_sha256'])
        self.assertEqual((self.source / 'app.py').read_bytes(), original_bytes)

    def test_previous_revision_tampering_is_fatal_before_alias_publication(self):
        for name in ('ARCHITECTURE.md', 'study.json'):
            def change(stage, context, data):
                initial_finding(stage, context, data)
                if stage == 'study' and context['revision_id'] == '002':
                    active = max(self.base.glob('revision-run-*'), key=lambda path: path.stat().st_mtime_ns)
                    with (active / 'revisions' / '001' / name).open('ab') as stream:
                        stream.write(b'\nTAMPERED\n')
                    return 'transport_failure'
                return data
            with self.subTest(name=name):
                manifest, code = self.run_case(change, verify_immutable=False)
                self.assertEqual(code, 1)
                self.assertFalse(manifest['publication_complete'])
                self.assertFalse((self.run_dir / 'ARCHITECTURE.md').exists())
                self.assertTrue(any(d.get('failure_layer') == 'integrity' for d in manifest['diagnostics']), manifest['diagnostics'])

    def test_recovered_study_material_is_immutable_during_review(self):
        def change(stage, context, data):
            if stage == 'study':
                data['unexpected'] = True
            elif stage == 'review':
                active = max(self.base.glob('revision-run-*'), key=lambda path: path.stat().st_mtime_ns)
                with (active / 'revisions' / '001' / 'study.material.json').open('ab') as stream:
                    stream.write(b'\nTAMPERED\n')
            return data
        manifest, code = self.run_case(change, policy='compromise')
        self.assertEqual((code, manifest['status'], manifest['critical_failure']), (1, 'FAILED', True))
        self.assertFalse(manifest['publication_complete'])
        self.assertFalse((self.run_dir / 'study.material.json').exists())

    def test_invalid_catalog_fallback_and_continue_on_error_are_explicit(self):
        def invalid_catalog(stage, context, data):
            if stage == 'catalog':
                data['subsystems'][0]['paths'] = ['missing-source-file']
            return data
        for policy, keep_going in (('strict', True), ('compromise', True), ('compromise', False)):
            with self.subTest(policy=policy, keep_going=keep_going):
                manifest, code = self.run_case(invalid_catalog, policy=policy, continue_on_error=keep_going)
                self.assertNotEqual(code, 0)
                self.assertFalse(manifest['accepted'])
                if policy == 'compromise' and keep_going:
                    self.assertEqual(self.stages(), ['catalog', 'study', 'review'])
                    self.assertEqual(manifest['coverage_plan']['origin'], 'DIRECTORY_FALLBACK')
                    self.assertFalse(manifest['coverage_plan']['policy_satisfied'])
                    self.assert_selected_aliases('001')
                else:
                    self.assertEqual(self.stages(), ['catalog'])
                    self.assertEqual(manifest['revisions'], [])


class RevisionGitPipelineTests(unittest.TestCase):
    setUp = git_fixtures.RepoFixture.setUp
    tearDown = git_fixtures.RepoFixture.tearDown
    git = git_fixtures.RepoFixture.git

    def test_comparison_cannot_change_recovered_study_sidecars(self):
        for name in ('claim.registry.json', 'review.plan.json'):
            for location in ('revision', 'alias'):
                with self.subTest(name=name, location=location):
                    config = git_fixtures.RepoFixture.config(self)
                    config['git_mode']['branches'] = ['master', 'test01']
                    config.update(output_language='English', result_policy='compromise')
                    runner = Runner(config, self.base / ('revision-recovered-sidecars-' + name + '-' + location))
                    calls = []
                    def process(command, cwd, env, payload, **kwargs):
                        context = json.loads(payload.decode().split('# Authoritative orchestration context (data)\n', 1)[1]
                                             .split('\n\n# Required final JSON Schema', 1)[0])
                        calls.append(context['stage'])
                        data = response(context)
                        if context['stage'] == 'study' and context['branch'] == 'master':
                            data['unexpected'] = True
                        elif context['stage'] == 'compare':
                            first = runner.manifest['branches'][0]
                            directory = runner.run_dir / first['directory']
                            if location == 'revision':
                                directory = directory / 'revisions' / '001'
                            with (directory / name).open('ab') as stream:
                                stream.write(b'\nTAMPERED\n')
                        return {'returncode': 0, 'stdout': json.dumps(data).encode(), 'stderr': b''}
                    with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process):
                        manifest, code = runner.run()
                    self.assertIn('compare', calls)
                    self.assertIsNone(manifest['branches'][0]['study'])
                    self.assertIsNotNone(manifest['branches'][0]['study_material'])
                    self.assertIsNotNone(manifest['branches'][1]['study'])
                    self.assertEqual((code, manifest['status'], manifest['critical_failure']), (1, 'FAILED', True))
                    self.assertFalse(manifest['publication_complete'])
                    self.assertTrue(any(d.get('failure_layer') == 'integrity' for d in manifest['diagnostics']))
                    self.assertEqual(self.repo.symbolic(), 'master')
                    self.assertEqual(self.repo.head(), self.master)

    def test_comparison_cannot_change_the_frozen_coverage_plan(self):
        config = git_fixtures.RepoFixture.config(self)
        config['git_mode']['branches'] = ['master', 'test01']
        config.update(output_language='English')
        runner = Runner(config, self.base / 'revision-coverage-guard')
        def process(command, cwd, env, payload, **kwargs):
            context = json.loads(payload.decode().split('# Authoritative orchestration context (data)\n', 1)[1]
                                 .split('\n\n# Required final JSON Schema', 1)[0])
            if context['stage'] == 'compare':
                first = runner.manifest['branches'][0]
                with (runner.run_dir / first['directory'] / 'coverage.plan.json').open('ab') as stream:
                    stream.write(b'\nTAMPERED\n')
            return {'returncode': 0, 'stdout': json.dumps(response(context)).encode(), 'stderr': b''}
        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process):
            manifest, code = runner.run()
        self.assertEqual((code, manifest['status'], manifest['critical_failure']), (1, 'FAILED', True))
        self.assertFalse(manifest['publication_complete'])
        self.assertTrue(any(d.get('failure_layer') == 'integrity' for d in manifest['diagnostics']))

    def test_comparison_receives_only_selected_revision_and_resolves_its_claims(self):
        config = git_fixtures.RepoFixture.config(self)
        config['git_mode']['branches'] = ['master', 'test01']
        config.update(output_language='English')
        err = io.StringIO()
        reporter = Reporter(stdout=io.StringIO(), stderr=err, progress=False)
        self.addCleanup(reporter.close)
        runner = Runner(config, self.base / 'revision-git', reporter=reporter)
        calls = []
        def process(command, cwd, env, payload, **kwargs):
            context = json.loads(payload.decode().split('# Authoritative orchestration context (data)\n', 1)[1]
                                 .split('\n\n# Required final JSON Schema', 1)[0])
            calls.append(copy.deepcopy(context))
            stage = context['stage']
            data = response(context)
            if context.get('branch') == 'master':
                initial_finding(stage, context, data)
                if stage == 'study' and context['revision_id'] == '002':
                    data['claims'][0]['statement'] = 'Revised master behavior'
                    data['report_sections'][0]['blocks'][0]['markdown'] = 'Revised master behavior\n'
            if stage == 'compare':
                prompt = payload.decode()
                for published in runner.manifest['branches']:
                    self.assertIn(published['source_commit'], prompt)
                    for artifact in ('study', 'review'):
                        document = published[artifact]
                        for key, value in document['review_plan'].items():
                            if key.endswith('_sha256') and value:
                                self.assertNotIn(value, prompt)
                        for evidence in document['program_checks']['evidence']:
                            for key in ('file_sha256', 'fragment_sha256'):
                                self.assertNotIn(evidence[key], prompt)
                refs = []
                for branch in context['branches']:
                    plan = branch['study']['review_plan']
                    refs.append(dict(branch=branch['branch'], revision_id=branch['selected_revision'],
                        artifact='study', claim_id='C-001',
                        review_target_id=plan['review_target_id']))
                data['differences'] = [dict(id='D-001', branch='test01', category='execution',
                    classification='CONFIRMED_DIFFERENCE', baseline_statement='Revised master behavior',
                    branch_statement='Test branch behavior', evidence_refs=refs,
                    explanation='The fixture contrasts the two supplied accepted reports.')]
            return {'returncode': 0, 'stdout': json.dumps(data).encode(), 'stderr': b''}
        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process):
            manifest, code = runner.run()
        self.assertEqual((code, manifest['status']), (0, 'COMPLETE'))
        self.assertEqual(len(calls), 9)
        comparison = calls[-1]
        self.assertEqual(comparison['stage'], 'compare')
        master, topic = comparison['branches']
        self.assertEqual(master['selected_revision'], '002')
        self.assertEqual(topic['selected_revision'], '001')
        self.assertEqual(master['study']['revision_id'], '002')
        self.assertEqual(master['review']['revision_id'], '002')
        self.assertNotIn('revisions', master)
        self.assertNotIn('previous_revision', master)
        first_plan = manifest['branches'][0]['revisions'][0]['study']['review_plan']
        selected_plan = manifest['branches'][0]['study']['review_plan']
        self.assertNotIn('registry_sha256', master['study']['review_plan'])
        self.assertNotIn('document_sha256', master['study']['review_plan'])
        self.assertEqual(master['study']['review_plan']['review_target_id'], master['review']['review_target_id'])
        self.assertNotEqual(first_plan['registry_sha256'], selected_plan['registry_sha256'])
        self.assertNotEqual(first_plan['document_sha256'], selected_plan['document_sha256'])
        self.assertEqual(manifest['comparison']['differences'][0]['evidence_refs'][0]['registry_sha256'],
                         selected_plan['registry_sha256'])
        self.assertTrue(manifest['comparison']['program_checks']['policy_satisfied'])
        paths = []
        for branch in manifest['branches']:
            paths.append(runner.run_dir / branch['directory'] / 'SUBSYSTEM_CATALOG.md')
            for revision in branch['revisions']:
                paths.extend(runner.run_dir / revision['directory'] / name
                             for name in ('ARCHITECTURE.md', 'ARCHITECTURE_REVIEW.md'))
        paths.append(runner.run_dir / 'comparison/BRANCH_COMPARISON.md')
        self.assertEqual([line for line in err.getvalue().splitlines() if line.startswith('      Report: ')],
                         ['      Report: ' + str(path) for path in paths])
        self.assertEqual(self.repo.symbolic(), 'master')
        self.assertEqual(self.repo.head(), self.master)
        self.repo.clean()


if __name__ == '__main__':
    unittest.main()
