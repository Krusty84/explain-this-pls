# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Public settings fail before invoking a backend and inherit deliberately."""
import copy
from unittest.mock import patch

from explain import AuditError
from test_folder import FolderFixture


class ConfigTests(FolderFixture):
    def test_claude_backend_is_rejected_before_cli_in_both_modes(self):
        original = copy.deepcopy(self.value)
        for mode in ('git', 'folder'):
            targets = ('agent', 'catalog', 'study', 'review') + (('compare',) if mode == 'git' else ())
            for target in targets:
                self.value = copy.deepcopy(original)
                self.value['mode'] = mode
                self.value['git_mode'] = {'repository': './source', 'branches': ['main', 'feature'],
                                          'baseline_branch': 'main'}
                if target == 'agent':
                    self.value['agent']['backend'] = 'claude-code'
                else:
                    self.value['stage_agents'] = {target: {'backend': 'claude-code'}}
                with self.subTest(mode=mode, target=target), patch('explain.process') as process:
                    with self.assertRaisesRegex(AuditError, '^Unsupported backend: claude-code$'):
                        self.config()
                    process.assert_not_called()

    def test_source_context_and_read_guard_defaults_and_disable(self):
        config = self.config()
        self.assertEqual(config['source_context'], {'enabled': True, 'max_bytes': 65536})
        self.assertTrue(config['execution']['read_loop_guard'])
        self.value['source_context'] = {'enabled': False, 'max_bytes': 4096}
        self.value['execution']['read_loop_guard'] = False
        config = self.config()
        self.assertEqual(config['source_context'], {'enabled': False, 'max_bytes': 4096})
        self.assertFalse(config['execution']['read_loop_guard'])

    def test_invalid_source_context_and_read_guard_before_backend_lookup(self):
        original = copy.deepcopy(self.value)
        invalid = [None, False, [], {'other': 1}, {'enabled': 1}, {'enabled': 'true'},
                   {'enabled': None}, {'max_bytes': True}, {'max_bytes': 4095},
                   {'max_bytes': 4096.0}, {'max_bytes': '65536'}, {'max_bytes': None}]
        for value in invalid:
            self.value = copy.deepcopy(original) | {'source_context': value}
            with self.subTest(value=value), patch('explain.shutil.which') as which:
                with self.assertRaises(AuditError):
                    self.config()
                which.assert_not_called()
        for value in (None, 0, 1, 'true', [], {}):
            self.value = copy.deepcopy(original) | {'execution': {'read_loop_guard': value}}
            with self.subTest(read_loop_guard=value), patch('explain.shutil.which') as which:
                with self.assertRaises(AuditError):
                    self.config()
                which.assert_not_called()

    def test_source_byte_limit_defaults_and_custom_values_for_all_backends_and_modes(self):
        self.value['git_mode'] = {'repository': './source', 'branches': ['main'], 'baseline_branch': 'main'}
        for mode in ('git', 'folder'):
            self.value['mode'] = mode
            for backend in ('codex', 'opencode', 'xxx'):
                self.value['agent']['backend'] = backend
                self.value.pop('max_source_bytes_per_session', None)
                with self.subTest(mode=mode, backend=backend):
                    self.assertEqual(self.config()['max_source_bytes_per_session'], 262144)
                    for limit in (1, 100, 1048576):
                        self.value['max_source_bytes_per_session'] = limit
                        self.assertEqual(self.config()['max_source_bytes_per_session'], limit)

    def test_invalid_source_byte_limits_fail_before_backend_lookup(self):
        for value in (0, -1, True, False, 1.0, 1.5, 262144.0, '262144', None, [], {}):
            self.value['max_source_bytes_per_session'] = value
            with self.subTest(value=value), patch('explain.shutil.which') as which:
                with self.assertRaisesRegex(AuditError, 'max_source_bytes_per_session must be a positive integer') as caught:
                    self.config()
                self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
                which.assert_not_called()

    def test_explicit_mode_and_active_section_required_before_backend(self):
        original = copy.deepcopy(self.value)
        for key in ('mode', 'folder_mode'):
            self.value = {k: v for k, v in original.items() if k != key}
            with self.subTest(key=key), patch('explain.process') as process:
                with self.assertRaisesRegex(AuditError, 'Missing configuration'):
                    self.config()
                process.assert_not_called()

    def test_top_level_source_keys_rejected_before_backend(self):
        original = copy.deepcopy(self.value)
        source = {'repository': './source', 'branches': ['main'], 'baseline_branch': 'main'}
        for mode in ('git', 'folder'):
            for key, value in source.items():
                self.value = original | {'mode': mode, 'git_mode': source, key: value}
                with self.subTest(mode=mode, key=key), patch('explain.process') as process:
                    with self.assertRaisesRegex(AuditError, 'Unknown configuration keys:.*' + key):
                        self.config()
                    process.assert_not_called()

    def test_catalog_inherits_effective_study_then_its_own_overrides(self):
        self.value['agent']['model'] = 'shared'
        self.value['stage_agents'] = {'study': {'model': 'study-model', 'expected_version': 'pinned'},
                                    'catalog': {'model': 'catalog-model'}}
        config = self.config()
        self.assertEqual(config['_agents']['catalog']['model'], 'catalog-model')
        self.assertEqual(config['_agents']['catalog']['expected_version'], 'pinned')
        self.assertEqual(config['_agents']['catalog']['executable'], config['_agents']['study']['executable'])
        self.assertEqual(config['_agents']['review']['model'], 'shared')
        self.assertNotIn('revise', config['_agents'])
        self.assertTrue(config['_prompt_paths']['catalog'].endswith('catalog.md'))
        self.assertTrue(config['_prompt_paths']['revise'].endswith('revise.md'))

    def test_decoding_is_normalized_and_invalid_settings_fail_before_any_cli(self):
        self.value['source_decoding'] = {'rules': [{'path': 'src/old', 'encoding': 'windows-1251'},
                                                  {'path': '.', 'encoding': 'UTF8'}]}
        self.assertEqual(self.config()['source_decoding'], {'rules': [
            {'path': '.', 'encoding': 'utf-8'}, {'path': 'src/old', 'encoding': 'cp1251'}]})
        for value in ({'rules': [{'path': '.', 'encoding': 'guess'}]},
                      {'rules': [{'path': '../outside', 'encoding': 'utf-8'}]},
                      {'rules': [{'path': '.', 'encoding': 'cp1251'}] * 2}):
            self.value['source_decoding'] = value
            with self.subTest(value=value), patch('explain.process') as process:
                with self.assertRaises(AuditError) as caught:
                    self.config()
                self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
                process.assert_not_called()

    def test_revision_prompt_is_checked_when_enabled_and_catalog_prompt_always(self):
        base = copy.deepcopy(self.value)
        for stage in ('catalog', 'revise'):
            self.value = copy.deepcopy(base) | {'prompts': {stage: './missing-prompt.md'}}
            with self.subTest(stage=stage), self.assertRaises(AuditError):
                self.config()
        self.value = base | {'execution': {'max_revision_rounds': 0},
                             'prompts': {'revise': './missing-inactive-prompt.md'}}
        config = self.config()
        self.assertNotIn('revise', config['_prompt_paths'])
