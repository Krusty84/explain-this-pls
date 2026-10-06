# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Private conversion-chain integrity, using only synthetic local backends."""
import json
from fixtures.cli_response import cli_result
import unittest
from unittest.mock import patch

from src.contracts.contracts import ContractError
from explain import AuditError, Folder, Runner, save_json
from fixtures.ledger_response import response, prompt_context
from test_folder import FolderFixture
import test_explain as git_fixtures


def corrupt_json(path):
    value = json.loads(path.read_text())
    if 'report_sections' in value:
        value['report_sections'][0]['blocks'][0]['markdown'] = 'Injected facts never returned by the model.'
    else:
        value['injected_metadata'] = True
    save_json(path, value)


class PrivateChainRecoveryTests(FolderFixture):
    def test_changed_private_chain_cannot_supply_recovered_material(self):
        for filename in ('extracted.json', 'expanded.json', 'normalized.json',
                         'normalization.json', 'validation.json'):
            with self.subTest(filename=filename):
                runner = Runner(self.config() | {'result_policy': 'compromise'}, self.base / filename)
                context = {'source_directory': str(self.source),
                           'source_fingerprint': Folder(self.source).snapshot()['source_fingerprint']}
                original = runner.validate_attempt
                changed = []
                def process(command, cwd, env, payload, **kwargs):
                    wire = response(prompt_context(payload))
                    # Structurally valid, so all normalization records exist;
                    # semantically invalid, so compromise would retain the prose.
                    wire['claims'][0]['evidence_ids'] = ['study:E-999']
                    return cli_result(command, wire)
                def validate(stage, data, current, attempt, meta, binding):
                    try:
                        return original(stage, data, current, attempt, meta, binding)
                    except ContractError:
                        target = attempt / filename
                        corrupt_json(target)
                        changed.append(target)
                        raise
                with patch('explain.process', side_effect=process), \
                        patch.object(runner, 'validate_attempt', side_effect=validate), \
                        self.assertRaises(AuditError) as caught:
                    runner.invoke('study', context, runner.run_dir / 'study.logs')
                self.assertEqual(len(changed), 1)
                self.assertEqual(caught.exception.failure_layer, 'integrity')
                self.assertTrue(runner.critical_failure)
                self.assertFalse((runner.run_dir / 'study.material.json').exists())
                self.assertFalse((runner.run_dir / 'ARCHITECTURE.md').exists())

    def test_wrong_xxx_task_never_retries_or_retains_material(self):
        for policy in ('strict', 'compromise'):
            config = self.config() | {'result_policy': policy, 'execution': {'structured_output_repair_attempts': 2}}
            config['_agents']['study']['backend'] = 'xxx'
            runner = Runner(config, self.base / ('xxx-' + policy))
            context = {'source_directory': str(self.source),
                       'source_fingerprint': Folder(self.source).snapshot()['source_fingerprint']}
            calls = []
            def invoke(command, cwd, env, payload, **kwargs):
                calls.append(payload)
                wire = response(prompt_context(payload))
                wire['task'] = 'architecture_comparison'
                return wire, {}
            with patch('src.backends.xxx.invoke', side_effect=invoke), self.assertRaises(ContractError) as caught:
                runner.invoke('study', context, runner.run_dir / 'study.logs')
            self.assertEqual(caught.exception.details.get('code'), 'TASK_IDENTITY_MISMATCH')
            self.assertEqual(len(calls), 1)
            self.assertFalse((runner.run_dir / 'study.material.json').exists())
            self.assertFalse((runner.run_dir / 'study.logs/attempt-002').exists())

    def test_review_cannot_change_published_study_conversion_chain(self):
        for filename in ('extracted.json', 'expanded.json', 'normalized.json', 'normalization.json',
                         'materialized.json', 'provenance.json'):
            with self.subTest(filename=filename):
                runner = Runner(self.config(), self.base / ('later-' + filename))
                calls = []
                def process(command, cwd, env, payload, **kwargs):
                    context = prompt_context(payload)
                    calls.append(context['stage'])
                    if context['stage'] == 'review':
                        corrupt_json(runner.run_dir / 'revisions/001/study.logs/attempt-001' / filename)
                    return cli_result(command, response(context))
                with patch.object(runner, 'check_cli', return_value={}), \
                        patch('explain.process', side_effect=process):
                    manifest, code = runner.run()
                self.assertEqual(calls, ['catalog', 'study', 'review'])
                self.assertEqual((code, manifest['status'], manifest['critical_failure']), (1, 'FAILED', True))
                self.assertFalse(manifest['publication_complete'])
                self.assertTrue(any(d.get('failure_layer') == 'integrity' for d in manifest['diagnostics']))


class PrivateChainCompareTests(unittest.TestCase):
    setUp = git_fixtures.RepoFixture.setUp
    tearDown = git_fixtures.RepoFixture.tearDown
    git = git_fixtures.RepoFixture.git

    def test_compare_cannot_change_selected_revision_private_chain(self):
        for filename in ('extracted.json', 'expanded.json', 'binding.json'):
            with self.subTest(filename=filename):
                config = git_fixtures.RepoFixture.config(self)
                config['git_mode']['branches'] = ['master', 'test01']
                config.update(output_language='English')
                runner = Runner(config, self.base / ('compare-chain-' + filename))
                calls = []
                def process(command, cwd, env, payload, **kwargs):
                    context = prompt_context(payload)
                    calls.append(context['stage'])
                    if context['stage'] == 'compare':
                        branch = runner.manifest['branches'][0]
                        target = (runner.run_dir / branch['directory'] / 'revisions' /
                                  branch['selected_revision'] / 'study.logs/attempt-001' / filename)
                        corrupt_json(target)
                    return cli_result(command, response(context))
                with patch.object(runner, 'check_cli', return_value={}), \
                        patch('explain.process', side_effect=process):
                    manifest, code = runner.run()
                self.assertIn('compare', calls)
                self.assertEqual((code, manifest['status'], manifest['critical_failure']), (1, 'FAILED', True))
                self.assertFalse(manifest['publication_complete'])
                self.assertTrue(any(d.get('failure_layer') == 'integrity' for d in manifest['diagnostics']))
                self.assertEqual(self.repo.symbolic(), 'master')
                self.assertEqual(self.repo.head(), self.master)


if __name__ == '__main__':
    unittest.main()
