# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Scope survives revision, review and publication in both source modes."""
import json
import unittest

from src.contracts.contracts import accepted, workflow_satisfied
from src.reports.document_rendering import validate_materialized
from fixtures.architecture_checks import assert_architecture
import test_explain as git_fixtures
import test_folder as folder_fixtures
import test_optional_review as optional_fixtures
import test_revision_pipeline as revision_fixtures


GROUPS = {
    'English': ('Investigated', 'Partially investigated', 'Unverified', 'Out of scope'),
    'Russian': ('Исследовано', 'Частично исследовано', 'Не проверено', 'Вне области исследования'),
}


class ScopePipelineMixin(optional_fixtures.PipelineMixin):
    def result_item(self, manifest):
        return manifest['branches'][0] if self.mode == 'git' else manifest

    def test_revision_and_repeated_review_preserve_scope_and_publication_identity(self):
        for language in GROUPS:
            def change(context, data):
                revision_fixtures.initial_finding(context['stage'], context, data)
                if language == 'Russian':
                    for section in data.get('report_sections', []):
                        if section['key'] == 'scope':
                            for block in section['blocks']:
                                for english, russian in zip(GROUPS['English'], GROUPS['Russian']):
                                    block['markdown'] = block['markdown'].replace(
                                        '### ' + english + '\n', '### ' + russian + '\n')
                return data

            with self.subTest(language=language):
                cfg = self.scope_config(language=language)
                manifest, code = self.run_pipeline(cfg, change)
                item = self.result_item(manifest)
                self.assertEqual((code, manifest['status']), (0, 'COMPLETE'))
                self.assertTrue(item['accepted'])
                self.assertTrue(item['workflow_satisfied'])
                self.assertTrue(accepted(item))
                self.assertTrue(workflow_satisfied(item))
                self.assertEqual(item['selected_revision'], '002')
                self.assertEqual([c.get('prompt_variant', c.get('phase', c['stage'])) for c in self.calls],
                                 ['collect', 'write', 'review', 'revise', 'review'])
                for revision in item['revisions']:
                    study, review = revision['study'], revision['review']
                    directory = self.run_dir / revision['directory']
                    validate_materialized(json.loads((directory / 'study.json').read_text()))
                    published = assert_architecture(self, directory, study,
                        revision['study_invocation'], language,
                        source_root=cfg[self.mode + '_mode']['repository' if self.mode == 'git' else 'path'])
                    scope_id = next(a['id'] for a in study['review_plan']['omission_areas']
                                    if a['scope'] == 'scope')
                    self.assertIn(scope_id, review['program_checks']['omission_coverage']['expected_ids'])
                    self.assertFalse(review['program_checks']['omission_coverage']['missing_ids'])
                    for group in GROUPS[language]:
                        self.assertIn('### ' + group + '\n', study['report_markdown'])
                        self.assertIn('### ' + group + '\n', published)
                    self.assertEqual(review['target']['document_sha256'],
                                     study['review_plan']['document_sha256'])
                self.assertEqual(item['review']['prior_findings'][0]['status'], 'RESOLVED')
                self.assert_source_restored()

    def test_missing_scope_review_blocks_acceptance_in_each_policy(self):
        def change(context, data):
            if context['stage'] == 'review':
                scope_id = next(a['id'] for a in context['review_plan']['omission_areas']
                                if a['scope'] == 'scope')
                data['omission_search'] = [a for a in data['omission_search'] if a['area_id'] != scope_id]
            return data

        for policy in ('strict', 'compromise'):
            with self.subTest(policy=policy):
                manifest, code = self.run_pipeline(self.scope_config(policy=policy), change)
                item = self.result_item(manifest)
                self.assertEqual((code, manifest['status']), (2, 'PARTIAL'))
                self.assertFalse(item['accepted'])
                self.assertFalse(item['workflow_satisfied'])
                self.assertFalse(accepted(item))
                self.assertFalse(workflow_satisfied(item))
                self.assertFalse(manifest['workflow_satisfied'])
                scope_id = next(a['id'] for a in item['study']['review_plan']['omission_areas']
                                if a['scope'] == 'scope')
                review = item['review']
                self.assertEqual(review['completion_status'], 'COMPLETE')
                self.assertEqual(review['program_checks']['omission_coverage']['missing_ids'], [scope_id])
                self.assertFalse(review['program_checks']['omission_coverage']['completeness_measured'])
                self.assertFalse(review['program_checks']['policy_satisfied'])
                self.assertEqual(review['verdict'], 'INCONCLUSIVE')
                self.assertEqual(len(item['revisions']), 1)
                self.assertTrue(item['study_invocation']['publication_complete'])
                self.assert_source_restored()


class FolderScopePipelineTests(ScopePipelineMixin, folder_fixtures.FolderFixture):
    mode = 'folder'

    def scope_config(self, *, language='English', policy='strict'):
        self.value.update(output_language=language, result_policy=policy,
                          execution={'max_revision_rounds': 1, 'review_enabled': True})
        return self.config()

    def assert_source_restored(self):
        self.assertEqual((self.source / 'app.py').read_text(), 'def main(): return 42\n')
        self.assertFalse((self.source / '.git').exists())


class GitScopePipelineTests(ScopePipelineMixin, unittest.TestCase):
    mode = 'git'
    setUp = git_fixtures.RepoFixture.setUp
    tearDown = git_fixtures.RepoFixture.tearDown
    git = git_fixtures.RepoFixture.git

    def scope_config(self, *, language='English', policy='strict'):
        cfg = git_fixtures.RepoFixture.config(self)
        cfg.update(output_language=language, result_policy=policy)
        cfg['execution'].update(max_revision_rounds=1)
        cfg['git_mode']['branches'] = ['master']
        return cfg

    def assert_source_restored(self):
        self.assertEqual(self.repo.symbolic(), 'master')
        self.assertEqual(self.repo.head(), self.master)
        self.repo.clean()


if __name__ == '__main__':
    unittest.main()
