# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Publication structure does not change Study calls or canonical Review inputs."""
import copy
import unittest
from unittest.mock import patch

from explain import Runner
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
                        assert_architecture(self, self.runner.run_dir, study,
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
                self.assertFalse(list(self.runner.run_dir.rglob('ARCHITECTURE.md')))
                revision = manifest['revisions'][0]
                error = revision['study_invocation']['error']
                self.assertEqual(error['details']['code'], 'UNEXPECTED_TOP_LEVEL_HEADING')
                if policy == 'compromise':
                    self.assertTrue(revision['study_invocation']['material_retained'])
                    self.assertFalse(revision['study_material']['strict_valid'])


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

                with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=answer):
                    manifest, code = runner.run()
                self.assertEqual(code, 0, manifest.get('diagnostics'))
                self.assertEqual([c['stage'] for c in calls],
                                 ['study'] if len(branches) == 1 else ['study', 'study', 'compare'])
                for branch in manifest['branches']:
                    assert_architecture(self, runner.run_dir / branch['directory'], branch['study'],
                                        branch['study_invocation'], language)
                self.assertEqual(self.repo.symbolic(), 'master')
                self.assertEqual(self.repo.head(), self.master)
