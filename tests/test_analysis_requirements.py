# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Static-analysis instructions and frozen uncertainty use the existing contracts."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from fixtures.cli_response import cli_result
from fixtures.ledger_response import prompt_context, response
from fixtures.study_runner import run_case
from src.reports.final_report import investigation_boundaries, render_final_report
from src.reports.presentation import render_stage
from src.runtime.runner import Runner
from src.runtime.tool_observer import ToolObserver
from test_folder import FolderFixture
import test_explain as git_fixtures
import test_mcp_observer as observer_fixtures


class AnalysisPromptTests(unittest.TestCase):
    def test_all_stages_preserve_boundaries_and_evidence_limits(self):
        root = Path(__file__).resolve().parents[1]
        for stage in ('collect', 'study', 'review', 'revise'):
            prompt = ' '.join((root / 'prompts' / (stage + '.md')).read_text().split())
            with self.subTest(stage=stage):
                for text in ('Investigated', 'Partially investigated', 'Unverified',
                             'Out of scope', 'trigger', 'alternative entry points',
                             'unfinished paths', 'potential impact', 'next check',
                             'NOT_CITED', 'NOT_INSPECTED', 'INSPECTED_UNVERIFIED',
                             'does not prove understanding', 'semantic verification',
                             'runtime verification', 'counterexample', 'orchestrator observations'):
                    self.assertIn(text, prompt)

    def test_behavior_checks_do_not_require_fabricated_guarantees(self):
        root = Path(__file__).resolve().parents[1]
        for stage in ('collect', 'study', 'review', 'revise'):
            prompt = ' '.join((root / 'prompts' / (stage + '.md')).read_text().split())
            with self.subTest(stage=stage):
                for text in ('effects', 'shared state', 'writers', 'feature flags',
                             'environment variables', 'launch arguments', 'configuration files',
                             'dynamic dispatch', 'exceptions', 'timeout', 'retry',
                             'idempotency', 'rollback', 'cleanup', 'partial completion',
                             'suppressed errors', 'not established'):
                    self.assertIn(text, prompt)
        collector = (root / 'prompts/collect.md').read_text()
        self.assertIn('priority_scenarios', collector)
        self.assertIn('no fixed count', collector)
        self.assertNotIn('3–5', collector)
        writer = (root / 'prompts/study.md').read_text()
        self.assertIn('Do not inspect source', writer)
        self.assertIn('Do not add claims', writer)


class AnalysisUncertaintyPipelineTests(FolderFixture):
    run_case = run_case

    def test_unknown_runtime_behavior_survives_frozen_write_review_and_revision(self):
        for language in ('English', 'Russian'):
            self.value['output_language'] = language
            unknown = dict(id='C-002', statement='Active runtime retry policy is not established.',
                scope='Selected request scenario.', epistemic_kind='UNKNOWN', evidence_ids=[],
                uncertainty='Runtime configuration is unavailable; duplicate writes remain possible. '
                            'Next check: inspect deployed retry settings and the retry handler.')
            scope = ('### Investigated\nEntry point from static source.\n\n'
                     '### Partially investigated\nRequest failure chain stops at runtime dispatch.\n\n'
                     '### Unverified\nUNKNOWN: active retry policy; runtime settings are unavailable. '
                     'Impact: duplicate writes remain possible. Next check: inspect deployed retry '
                     'settings and the retry handler.\n\n'
                     '### Out of scope\nOther business scenarios were not selected.')

            def change(runner, context, data):
                if context['stage'] == 'study':
                    if context.get('phase') != 'write':
                        data['claims'].append(copy.deepcopy(unknown))
                    if context.get('phase') != 'collect':
                        data['report_sections'][0]['blocks'] = [dict(markdown=scope,
                                                                   claim_ids=['C-001', 'C-002'])]
                elif context['revision_id'] == '001':
                    data['findings'] = [dict(id='F-001', severity='MEDIUM', type='MATERIAL_OMISSION',
                        claim_ids=[], location='Selected request scenario', evidence_ids=['review:E-001'],
                        impact='The scenario description needs a source check.',
                        proposed_correction='Check the request handler and retain unresolved runtime behavior.')]

            with self.subTest(language=language):
                manifest, code = self.run_case(large=False, review=True, change=change)
                self.assertEqual((code, manifest['selected_revision']), (0, '002'), manifest.get('diagnostics'))
                self.assertEqual(len(self.calls), 5)
                package = self.calls[1]['context']['evidence_package']
                self.assertEqual(package['study']['claims'][1], unknown)
                for revision in manifest['revisions']:
                    study = revision['study']
                    retained = {key: value for key, value in study['claims'][1].items()
                                if key != 'document_locators'}
                    self.assertEqual(retained, unknown)
                    self.assertEqual(revision['review']['claims'][1]['outcome'], 'CAVEAT_ACCEPTABLE')
                    self.assertIn(scope, study['report_markdown'])
                    self.assertEqual(study['evidence'], manifest['revisions'][0]['study']['evidence'])
                    before = copy.deepcopy(study)
                    self.assertEqual(render_stage('study', study, language=language),
                                     render_stage('study', study, language=language))
                    self.assertEqual(study, before)
                self.assertEqual(manifest['study']['registry_diff']['unchanged_ids'], ['C-001', 'C-002'])
                final = (self.runner.session.run_dir / 'FINAL_REPORT.md').read_text()
                for text in ('Investigated', 'Partially investigated', 'Unverified', 'Out of scope',
                             'UNKNOWN', unknown['uncertainty'], 'runtime verification'):
                    self.assertIn(text, final)
                for text in (('Границы исследования — оценки агента', 'Фильтр исходников — данные программы',
                              'Область исходников — зафиксированный список', 'Доставка исходного текста — наблюдения программы')
                             if language == 'Russian' else
                             ('Investigation boundaries — agent reports', 'Source filter — program records',
                              'Source scope — pinned inventory', 'Source text delivery — program observations')):
                    self.assertIn(text, final)
                source = self.runner.session.cfg['folder_mode']
                before = copy.deepcopy(manifest)
                rendered = render_final_report(manifest, source, 'folder', language)
                self.assertEqual(rendered, render_final_report(manifest, source, 'folder', language))
                self.assertEqual(manifest, before)

    def test_delivery_observations_do_not_promote_uncited_paths_to_verified(self):
        (self.source / 'uncited').mkdir()
        (self.source / 'uncited/worker.py').write_text('def worker(): return 0\n')
        manifest, code = self.run_case(large=False)
        self.assertEqual(code, 0)
        study = manifest['study']
        uncited = study['program_checks']['coverage']['not_cited_ids']
        self.assertTrue(uncited)
        for observation, observed in (({}, False), ({'delivery': {'unique_lines': 1}}, True),
                                      ({'mcp': {'delivered_observed': 1}}, False),
                                      ({'mcp': {'text_utf8_bytes': 1}}, True)):
            entry = copy.deepcopy(manifest)
            entry['study_invocation']['phase_invocations'] = [dict(phase='collect', tool_observation=observation)]
            before = copy.deepcopy(entry)
            with self.subTest(observation=observation):
                final = '\n'.join(investigation_boundaries(entry, entry['study'], None, 'English'))
                self.assertIn('study / collect: ' + ('source text delivery observed; INSPECTED_UNVERIFIED.'
                              if observed else 'source text delivery is not established by retained observations; inspection UNVERIFIED.'), final)
                self.assertIn('NOT_CITED does not mean NOT_INSPECTED.', final)
                self.assertIn('Text delivery does not prove understanding.', final)
                for area in uncited:
                    self.assertIn('| ' + area + ' | Not cited | Unverified |', final)
                self.assertEqual(entry, before)

    def test_folder_filter_and_exclusions_are_visible_in_publication(self):
        self.value.update(output_language='English',
                          source_filter={'follow_gitignore': True, 'exclude_paths': ['excluded']})
        for path in ('excluded/private.py', 'ignored/cache.py', 'kept/worker.py'):
            target = self.source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('def worker(): return 0\n')
        (self.source / '.gitignore').write_text('ignored/\n')
        manifest, code = self.run_case(large=False)
        self.assertEqual(code, 0)
        final = (self.runner.session.run_dir / 'FINAL_REPORT.md').read_text()
        self.assertIn('follow_gitignore: true', final)
        self.assertIn('User exclusions: excluded.', final)
        self.assertIn('| excluded | CONFIG |', final)
        self.assertIn('| ignored | GITIGNORE |', final)
        self.assertIn('kept', final)
        plan = manifest['study']['review_plan']['coverage_plan']
        included = {path for area in plan['areas'] for path in area['file_paths']}
        self.assertIn('kept/worker.py', included)
        self.assertFalse({'excluded/private.py', 'ignored/cache.py'} & included)

    def test_collection_delivery_observation_reaches_selected_publication(self):
        self.value['output_language'] = 'English'
        original = ToolObserver.snapshot

        def snapshot(observer):
            value = original(observer)
            if observer.context.get('phase') == 'collect':
                value['delivery']['unique_lines'] = 1
            return value

        with patch.object(ToolObserver, 'snapshot', snapshot):
            manifest, code = self.run_case(large=False)
        self.assertEqual(code, 0)
        phases = manifest['study_invocation']['phase_invocations']
        self.assertEqual([(p['phase'], p['tool_observation']['delivery']['unique_lines']) for p in phases],
                         [('collect', 1)])
        final = (self.runner.session.run_dir / 'FINAL_REPORT.md').read_text()
        self.assertIn('study / collect: source text delivery observed; INSPECTED_UNVERIFIED.', final)


class AnalysisMCPDeliveryTests(unittest.TestCase):
    setUp = observer_fixtures.MCPObserverTests.setUp
    arguments = observer_fixtures.MCPObserverTests.arguments
    payload = observer_fixtures.MCPObserverTests.payload
    event = observer_fixtures.MCPObserverTests.event

    def test_search_error_and_empty_response_do_not_establish_source_text_delivery(self):
        events = [
            self.event(1, tool='search_symbols', arguments={'query': 'one'},
                       payload={'results': [], 'has_more': False, 'next_cursor': None}),
            self.event(2, payload={'error': {'code': 'DECODE_ERROR', 'message': 'unavailable'}}),
            self.event(3, arguments=self.arguments(1, 1),
                       payload=self.payload(1, 0, total_lines=0, text='')),
            self.event(4),
        ]
        for event, delivered in zip(events, (False, False, False, True)):
            observer = ToolObserver('codex', context=self.context)
            observer.event(event)
            observation = observer.snapshot()
            entry = {'study_invocation': {'phase_invocations': [dict(phase='collect',
                                                                    tool_observation=observation)]}}
            with self.subTest(tool=event['item']['tool'], request=event['item']['id']):
                self.assertEqual(observation['mcp']['delivered_observed'], 1)
                self.assertEqual(observation['mcp']['text_utf8_bytes'] > 0, delivered)
                final = '\n'.join(investigation_boundaries(entry, None, None, 'English'))
                self.assertIn('study / collect: ' + ('source text delivery observed; INSPECTED_UNVERIFIED.'
                              if delivered else 'source text delivery is not established by retained observations; inspection UNVERIFIED.'), final)


class AnalysisGitBoundaryTests(unittest.TestCase):
    setUp = git_fixtures.RepoFixture.setUp
    tearDown = git_fixtures.RepoFixture.tearDown
    git = git_fixtures.RepoFixture.git

    def test_git_pinned_scope_and_user_exclusion_are_visible_in_publication(self):
        (self.repo_path / 'excluded.py').write_text('def excluded(): return 1\n')
        self.git('add', 'excluded.py')
        self.git('commit', '-m', 'Excluded source fixture')
        pinned = self.git('rev-parse', 'HEAD').strip()
        cfg = git_fixtures.RepoFixture.config(self)
        cfg.update(output_language='English', source_filter={'follow_gitignore': False,
                                                            'exclude_paths': ['excluded.py']})
        cfg['git_mode']['branches'] = ['master']
        run_dir = self.base / 'boundary-run'
        runner = Runner(cfg, run_dir)

        def process(command, cwd, env, payload, **kwargs):
            return cli_result(command, response(prompt_context(payload)))

        with patch.object(runner, 'check_cli', return_value={}), \
                patch('src.runtime.process.process', side_effect=process):
            manifest, code = runner.run()
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        branch = manifest['branches'][0]
        self.assertEqual(branch['source_commit'], pinned)
        final = (run_dir / 'FINAL_REPORT.md').read_text()
        for text in (pinned, 'Source scope — pinned inventory', 'User exclusions: excluded.py.',
                     '| excluded.py | CONFIG |', 'Git mode also applies Git rules.'):
            self.assertIn(text, final)
        plan = branch['study']['review_plan']['coverage_plan']
        self.assertEqual({path for area in plan['areas'] for path in area['file_paths']}, {'app.py'})
        before = copy.deepcopy(manifest)
        self.assertEqual(render_final_report(manifest, cfg['git_mode'], 'git', 'English'),
                         render_final_report(manifest, cfg['git_mode'], 'git', 'English'))
        self.assertEqual(manifest, before)


if __name__ == '__main__':
    unittest.main()
