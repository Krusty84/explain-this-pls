# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Publication structure does not change Study calls or canonical Review inputs."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.runtime.runner import Runner
from src.reports.document_rendering import _markdown_tokens, recover_sections
from fixtures.architecture_checks import assert_architecture
from fixtures.cli_response import cli_result
from fixtures.ledger_response import prompt_context, response
from fixtures.study_runner import run_case
from test_folder import FolderFixture
import test_explain as git_fixtures


class ArchitectureFolderPipelineTests(FolderFixture):
    run_case = run_case

    def test_publication_languages_policies_and_optional_review(self):
        for language in ('Russian', 'English'):
            for policy in ('strict', 'compromise'):
                for review in (False, True):
                    with self.subTest(language=language, policy=policy, review=review):
                        self.value['output_language'] = language
                        manifest, code = self.run_case(large=False, policy=policy, review=review)
                        self.assertEqual(code, 0, manifest.get('diagnostics'))
                        self.assertEqual([c['context']['stage'] for c in self.calls],
                                         ['study', 'review'] if review else ['study'])
                        study = manifest['study']
                        assert_architecture(self, self.runner.session.run_dir, study,
                                            manifest['study_invocation'], language)
                        if review:
                            context = self.calls[-1]['context']
                            self.assertEqual(context['architecture_document']['report_markdown'],
                                             study['report_markdown'])
                            self.assertEqual(manifest['review']['claim_registry'], study['claims'])

    def test_invalid_block_is_not_published_or_restudied(self):
        for policy in ('strict', 'compromise'):
            with self.subTest(policy=policy):
                def change(runner, context, data):
                    data['report_sections'][2]['blocks'][0]['markdown'] = '## Extra section\n'
                manifest, code = self.run_case(large=False, policy=policy, change=change)
                self.assertNotEqual(code, 0)
                self.assertEqual([c['context']['stage'] for c in self.calls], ['study'])
                self.assertFalse(list(self.runner.session.run_dir.rglob('ARCHITECTURE.md')))
                revision = manifest['revisions'][0]
                error = revision['study_invocation']['error']
                self.assertEqual(error['details']['code'], 'UNEXPECTED_TOP_LEVEL_HEADING')
                if policy == 'compromise':
                    self.assertTrue(revision['study_invocation']['material_retained'])
                    self.assertFalse(revision['study_material']['strict_valid'])

    def test_required_diagram_failures_do_not_publish_or_repeat_study(self):
        for policy in ('strict', 'compromise'):
            for defect in ('components', 'data_and_state', 'both', 'wrong_section', 'boundary', 'include'):
                with self.subTest(policy=policy, defect=defect):
                    authored = {}

                    def change(runner, context, data):
                        sections = {section['key']: section for section in data['report_sections']}
                        missing = ('components', 'data_and_state') if defect == 'both' else (defect,)
                        for key in missing:
                            if key in sections:
                                sections[key]['blocks'] = [dict(markdown='The diagram is unavailable.', claim_ids=[])]
                        if defect == 'wrong_section':
                            sections['startup_and_flows']['blocks'] += sections['components']['blocks']
                            sections['components']['blocks'] = [dict(markdown='The diagram is unavailable.', claim_ids=[])]
                        if defect in ('boundary', 'include'):
                            diagram = next(block for block in sections['components']['blocks']
                                           if '```plantuml' in block['markdown'])
                            diagram['markdown'] = diagram['markdown'].replace('@enduml', '' if defect == 'boundary'
                                                                             else '!include private.puml\n@enduml')
                        authored.update(copy.deepcopy(data))

                    manifest, code = self.run_case(large=False, policy=policy, change=change)
                    self.assertNotEqual(code, 0)
                    self.assertFalse(manifest['accepted'])
                    self.assertEqual([call['context']['stage'] for call in self.calls], ['study'])
                    self.assertFalse(list(self.runner.session.run_dir.rglob('ARCHITECTURE.md')))
                    revision = manifest['revisions'][0]
                    self.assertIsNone(revision['study'])
                    invocation = revision['study_invocation']
                    expected_code = ('INVALID_PLANTUML_BOUNDARY' if defect == 'boundary' else
                                     'FORBIDDEN_PLANTUML_DIRECTIVE' if defect == 'include' else
                                     'REQUIRED_DIAGRAM_MISSING')
                    self.assertEqual(invocation['error']['details']['code'], expected_code)
                    attempt = Path(invocation['artifact_directory'])
                    raw = json.loads((attempt / 'extracted.json').read_text())
                    self.assertEqual(raw['report_sections'], authored['report_sections'])
                    self.assertEqual(raw['claims'], authored['claims'])
                    if policy == 'compromise':
                        self.assertTrue(invocation['material_retained'])
                        material = revision['study_material']
                        self.assertFalse(material['strict_valid'])
                        self.assertFalse(material.get('program_checks', {}).get('policy_satisfied', False))
                        self.assertEqual(material['report_markdown'], recover_sections(authored['report_sections']))

    def test_partial_diagram_omission_preserves_authored_material(self):
        def change(runner, context, data):
            data.update(completion_status='PARTIAL', limitations=['Data ownership must be checked.'])
            section = next(section for section in data['report_sections'] if section['key'] == 'data_and_state')
            section['blocks'] = [dict(markdown='Data ownership must be checked before a diagram can be built.',
                                      claim_ids=[])]

        for language in ('Russian', 'English'):
            with self.subTest(language=language):
                self.value['output_language'] = language
                manifest, code = self.run_case(large=False, change=change)
                self.assertNotEqual(code, 0)
                self.assertFalse(manifest['accepted'])
                self.assertEqual([call['context']['stage'] for call in self.calls], ['study'])
                study = manifest['study']
                self.assertEqual(study['completion_status'], 'PARTIAL')
                self.assertEqual(study['program_checks']['diagrams']['missing_sections'], ['data_and_state'])
                self.assertFalse(study['program_checks']['diagrams']['complete'])
                self.assertFalse(study['program_checks']['policy_satisfied'])
                self.assertEqual((self.runner.session.run_dir / 'study.original.md').read_text(), study['report_markdown'])
                published = (self.runner.session.run_dir / 'ARCHITECTURE.md').read_text()
                tokens = _markdown_tokens(published)
                headings = [(token.tag, tokens[i + 1].content) for i, token in enumerate(tokens)
                            if token.type == 'heading_open']
                notice = ('Обязательная диаграмма отсутствует' if language == 'Russian' else
                          'Required diagram missing')
                self.assertEqual(sum(tag == 'h2' for tag, _ in headings), 10)
                self.assertEqual(sum(tag == 'h2' for tag, _ in headings[:headings.index(('h3', notice))]), 5)
                self.assertNotIn(notice, study['report_markdown'])


class ArchitectureGitPipelineTests(unittest.TestCase):
    setUp = git_fixtures.RepoFixture.setUp
    tearDown = git_fixtures.RepoFixture.tearDown
    git = git_fixtures.RepoFixture.git
    config = git_fixtures.RepoFixture.config

    def test_single_and_multiple_branches_publish_fixed_documents(self):
        for language, branches in (('English', ['master']), ('Russian', ['master', 'test01'])):
            with self.subTest(language=language, branches=branches):
                config = self.config()
                config.update(output_language=language)
                config['git_mode']['branches'] = branches
                config['execution']['review_enabled'] = False
                runner = Runner(config, self.base / ('architecture-' + language))
                calls = []

                def answer(command, cwd, env, payload, **kwargs):
                    context = prompt_context(payload)
                    calls.append(copy.deepcopy(context))
                    return cli_result(command, response(context))

                with patch.object(runner, 'check_cli', return_value={}), patch('src.runtime.process.process', side_effect=answer):
                    manifest, code = runner.run()
                self.assertEqual(code, 0, manifest.get('diagnostics'))
                self.assertEqual([c['stage'] for c in calls],
                                 ['study'] if len(branches) == 1 else ['study', 'study', 'compare'])
                for branch in manifest['branches']:
                    assert_architecture(self, runner.session.run_dir / branch['directory'], branch['study'],
                                        branch['study_invocation'], language)
                self.assertEqual(self.repo.symbolic(), 'master')
                self.assertEqual(self.repo.head(), self.master)
