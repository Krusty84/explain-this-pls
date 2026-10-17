# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Public settings fail before invoking a backend and inherit deliberately."""
import copy
from unittest.mock import patch

from src.runtime.errors import AuditError
from test_folder import FolderFixture


class ConfigTests(FolderFixture):
    def test_report_language_is_checked_before_backend_lookup(self):
        for language in ('French', '', None, [], 1):
            self.value['output_language'] = language
            with self.subTest(language=language), patch('src.runtime.config.shutil.which') as which:
                with self.assertRaises(AuditError) as caught:
                    self.config()
                self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
                which.assert_not_called()
        for language in ('Russian', 'ru', 'русский', 'English', 'en', 'английский'):
            self.value['output_language'] = language
            with self.subTest(language=language):
                self.assertEqual(self.config()['output_language'], language)

    def test_removed_settings_are_unknown_before_backend_lookup(self):
        original = copy.deepcopy(self.value)
        for mode in ('git', 'folder'):
            for backend in ('codex', 'opencode', 'xxx'):
                for key, value in (('multi_session', True), ('max_source_bytes_per_session', 262144)):
                    self.value = copy.deepcopy(original)
                    self.value.update(mode=mode, git_mode={'repository': './source',
                        'branches': ['main'], 'baseline_branch': 'main'})
                    self.value['agent']['backend'] = backend
                    self.value[key] = value
                    with self.subTest(mode=mode, backend=backend, key=key), patch('src.runtime.process.process') as process:
                        with self.assertRaisesRegex(AuditError, 'Unknown configuration keys:.*' + key):
                            self.config()
                        process.assert_not_called()

    def test_unsupported_backend_is_rejected_before_cli_in_both_modes(self):
        original = copy.deepcopy(self.value)
        for mode in ('git', 'folder'):
            targets = ('agent', 'study', 'review') + (('compare',) if mode == 'git' else ())
            for target in targets:
                self.value = copy.deepcopy(original)
                self.value['mode'] = mode
                self.value['git_mode'] = {'repository': './source', 'branches': ['main', 'feature'],
                                          'baseline_branch': 'main'}
                if target == 'agent':
                    self.value['agent']['backend'] = 'unsupported-backend'
                else:
                    self.value['stage_agents'] = {target: {'backend': 'unsupported-backend'}}
                with self.subTest(mode=mode, target=target), patch('src.runtime.process.process') as process:
                    with self.assertRaisesRegex(AuditError, '^Unsupported backend: unsupported-backend$'):
                        self.config()
                    process.assert_not_called()

    def test_source_context_and_read_guard_defaults_and_disable(self):
        config = self.config()
        self.assertEqual(config['source_context'], {'enabled': True, 'mode': 'on-demand'})
        self.assertTrue(config['execution']['read_loop_guard'])
        self.value['source_context'] = {'enabled': False}
        self.value['execution']['read_loop_guard'] = False
        config = self.config()
        self.assertEqual(config['source_context'], {'enabled': False, 'mode': 'on-demand'})
        self.assertFalse(config['execution']['read_loop_guard'])
        self.value['source_context'] = {'mode': 'full'}
        self.assertEqual(self.config()['source_context'], {'enabled': True, 'mode': 'full'})

    def test_invalid_source_context_and_read_guard_before_backend_lookup(self):
        original = copy.deepcopy(self.value)
        invalid = [None, False, [], {'other': 1}, {'enabled': 1}, {'enabled': 'true'},
                   {'enabled': None}, {'max_bytes': 65536}, {'mode': None}, {'mode': 'lazy'}, {'mode': []}]
        for value in invalid:
            self.value = copy.deepcopy(original) | {'source_context': value}
            with self.subTest(value=value), patch('src.runtime.config.shutil.which') as which:
                with self.assertRaises(AuditError):
                    self.config()
                which.assert_not_called()
        for value in (None, 0, 1, 'true', [], {}):
            self.value = copy.deepcopy(original) | {'execution': {'read_loop_guard': value}}
            with self.subTest(read_loop_guard=value), patch('src.runtime.config.shutil.which') as which:
                with self.assertRaises(AuditError):
                    self.config()
                which.assert_not_called()

    def test_explicit_mode_and_active_section_required_before_backend(self):
        original = copy.deepcopy(self.value)
        for key in ('mode', 'folder_mode'):
            self.value = {k: v for k, v in original.items() if k != key}
            with self.subTest(key=key), patch('src.runtime.process.process') as process:
                with self.assertRaisesRegex(AuditError, 'Missing configuration'):
                    self.config()
                process.assert_not_called()

    def test_top_level_source_keys_rejected_before_backend(self):
        original = copy.deepcopy(self.value)
        source = {'repository': './source', 'branches': ['main'], 'baseline_branch': 'main'}
        for mode in ('git', 'folder'):
            for key, value in source.items():
                self.value = original | {'mode': mode, 'git_mode': source, key: value}
                with self.subTest(mode=mode, key=key), patch('src.runtime.process.process') as process:
                    with self.assertRaisesRegex(AuditError, 'Unknown configuration keys:.*' + key):
                        self.config()
                    process.assert_not_called()

    def test_removed_catalog_keys_fail_before_any_cli(self):
        base = copy.deepcopy(self.value)
        for section in ('stage_agents', 'prompts'):
            self.value = copy.deepcopy(base)
            self.value[section] = {'catalog': {} if section == 'stage_agents' else './missing.md'}
            with self.subTest(section=section), patch('src.runtime.process.process') as process:
                with self.assertRaisesRegex(AuditError, 'Remove ' + section + r'\.catalog') as caught:
                    self.config()
                self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
                process.assert_not_called()

    def test_revision_uses_study_agent(self):
        self.value['agent']['model'] = 'shared'
        self.value['agent']['reasoning_effort'] = 'high'
        self.value['stage_agents'] = {'study': {'model': 'study-model', 'expected_version': 'pinned',
                                               'reasoning_effort': 'low'}}
        config = self.config()
        self.assertEqual(config['_agents']['study']['model'], 'study-model')
        self.assertEqual(config['_agents']['study']['reasoning_effort'], 'low')
        self.assertEqual(config['_agents']['review']['model'], 'shared')
        self.assertEqual(config['_agents']['review']['reasoning_effort'], 'high')
        self.assertNotIn('revise', config['_agents'])
        self.assertTrue(config['_prompt_paths']['revise'].endswith('revise.md'))

    def test_reasoning_effort_is_optional_and_preserves_native_values(self):
        for backend in ('codex', 'opencode', 'xxx'):
            self.value['agent'].update(backend=backend, model='provider/model')
            self.value['agent'].pop('reasoning_effort', None)
            with self.subTest(backend=backend, omitted=True):
                self.assertNotIn('reasoning_effort', self.config()['_agents']['study'])
            for effort in (None, 'high', 'native-level', ' native-level '):
                self.value['agent']['reasoning_effort'] = effort
                with self.subTest(backend=backend, effort=effort):
                    self.assertEqual(self.config()['_agents']['study']['reasoning_effort'], effort)
            if backend != 'opencode':
                self.value['agent'].pop('model')
                self.assertEqual(self.config()['_agents']['study']['reasoning_effort'], ' native-level ')

    def test_reasoning_effort_inherits_and_overrides_for_each_active_stage(self):
        base = copy.deepcopy(self.value)
        for backend in ('codex', 'opencode', 'xxx'):
            for target in ('study', 'review', 'compare'):
                for effort in ('custom-level', None):
                    self.value = copy.deepcopy(base)
                    self.value.update(mode='git', git_mode={'repository': './source',
                        'branches': ['main', 'feature'], 'baseline_branch': 'main'},
                        stage_agents={target: {'reasoning_effort': effort}})
                    self.value['agent'].update(backend=backend, model='provider/model', reasoning_effort='high')
                    with self.subTest(backend=backend, target=target, effort=effort):
                        agents = self.config()['_agents']
                        self.assertEqual(set(agents), {'study', 'review', 'compare'})
                        for stage, agent in agents.items():
                            self.assertEqual(agent['reasoning_effort'], effort if stage == target else 'high')

    def test_invalid_reasoning_effort_fails_before_any_cli(self):
        base = copy.deepcopy(self.value)
        for backend in ('codex', 'opencode', 'xxx'):
            for target in ('agent', 'study', 'review', 'compare'):
                for effort in ('', ' \t ', False, 1, 1.5, [], {}):
                    self.value = copy.deepcopy(base)
                    self.value.update(mode='git', git_mode={'repository': './source',
                        'branches': ['main', 'feature'], 'baseline_branch': 'main'})
                    self.value['agent'].update(backend=backend, model='provider/model')
                    if target == 'agent':
                        self.value['agent']['reasoning_effort'] = effort
                    else:
                        self.value['stage_agents'] = {target: {'reasoning_effort': effort}}
                    with self.subTest(backend=backend, target=target, effort=effort), \
                            patch('src.runtime.process.process') as process:
                        with self.assertRaises(AuditError) as caught:
                            self.config()
                        self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
                        self.assertIn('reasoning_effort', str(caught.exception))
                        process.assert_not_called()

    def test_inactive_stages_do_not_validate_reasoning_effort(self):
        self.value.update(execution={'review_enabled': False},
            stage_agents={'review': {'reasoning_effort': []}, 'compare': {'reasoning_effort': False}})
        self.assertEqual(set(self.config()['_agents']), {'study'})
        self.value['execution']['review_enabled'] = True
        with self.assertRaises(AuditError) as caught:
            self.config()
        self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
        self.value['stage_agents']['review']['reasoning_effort'] = None
        self.assertEqual(set(self.config()['_agents']), {'study', 'review'})
        self.value.update(mode='git', git_mode={'repository': './source',
            'branches': ['main', 'feature'], 'baseline_branch': 'main'})
        with self.assertRaises(AuditError) as caught:
            self.config()
        self.assertEqual(caught.exception.code, 'INVALID_CONFIG')

    def test_opencode_reasoning_effort_requires_provider_model_and_consistent_suffix(self):
        self.value['agent'].update(backend='opencode', reasoning_effort='high')
        invalid_models = (None, 1, [], '', 'model', '/model', 'provider/', ' /model', 'provider/ ',
            'provider/model#', '#provider/model', 'provider/model#other', 'provider/model#high#high')
        for model in invalid_models:
            self.value['agent']['model'] = model
            with self.subTest(model=model), patch('src.runtime.config.shutil.which') as which:
                with self.assertRaises(AuditError) as caught:
                    self.config()
                self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
                which.assert_not_called()
        for model in ('provider/model', 'provider/model#high'):
            self.value['agent']['model'] = model
            with self.subTest(model=model):
                self.assertEqual(self.config()['_agents']['study']['model'], model)
        self.value['agent'].update(model='provider/model', reasoning_effort='high#other')
        with self.assertRaises(AuditError) as caught:
            self.config()
        self.assertEqual(caught.exception.code, 'INVALID_CONFIG')

    def test_opencode_models_without_reasoning_effort_keep_existing_validation(self):
        self.value['agent'].update(backend='opencode', reasoning_effort=None)
        for model in (None, '', 'model', 'provider/model#native'):
            self.value['agent']['model'] = model
            with self.subTest(model=model):
                self.assertEqual(self.config()['_agents']['study']['model'], model)
        self.value['agent'].pop('reasoning_effort')
        self.value['agent']['model'] = 'model'
        self.assertEqual(self.config()['_agents']['study']['model'], 'model')

    def test_decoding_is_normalized_and_invalid_settings_fail_before_any_cli(self):
        self.value['source_decoding'] = {'rules': [{'path': 'src/old', 'encoding': 'windows-1251'},
                                                  {'path': '.', 'encoding': 'UTF8'}]}
        self.assertEqual(self.config()['source_decoding'], {'rules': [
            {'path': '.', 'encoding': 'utf-8'}, {'path': 'src/old', 'encoding': 'cp1251'}]})
        for value in ({'rules': [{'path': '.', 'encoding': 'guess'}]},
                      {'rules': [{'path': '../outside', 'encoding': 'utf-8'}]},
                      {'rules': [{'path': '.', 'encoding': 'cp1251'}] * 2}):
            self.value['source_decoding'] = value
            with self.subTest(value=value), patch('src.runtime.process.process') as process:
                with self.assertRaises(AuditError) as caught:
                    self.config()
                self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
                process.assert_not_called()

    def test_study_prompt_is_checked_and_revision_prompt_is_checked_when_enabled(self):
        base = copy.deepcopy(self.value)
        for stage in ('study', 'revise'):
            self.value = copy.deepcopy(base) | {'prompts': {stage: './missing-prompt.md'}}
            with self.subTest(stage=stage), self.assertRaises(AuditError):
                self.config()
        self.value = base | {'execution': {'max_revision_rounds': 0},
                             'prompts': {'revise': './missing-inactive-prompt.md'}}
        config = self.config()
        self.assertNotIn('revise', config['_prompt_paths'])
