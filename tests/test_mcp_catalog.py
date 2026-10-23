# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Bundled Codex model identity and source-tool isolation."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from src.backends.codex_catalog import load_catalog, sanitize_catalog
from src.runtime.errors import AuditError


def catalog():
    return {'models': [{'slug': 'selected-model', 'display_name': 'Selected model',
                       'tool_mode': 'code_mode_only', 'node_repl_disabled': False,
                       'experimental_supported_tools': ['read_file', 'list_dir'],
                       'apply_patch_tool_type': 'freeform', 'supports_search_tool': True,
                       'base_instructions': 'Keep the exact model instructions.',
                       'supported_reasoning_levels': [{'effort': 'high', 'description': 'High'}],
                       'default_reasoning_level': 'high', 'context_window': 200000,
                       'input_modalities': ['text', 'image'], 'supported_in_api': True}],
            'revision': 'external-catalog-revision'}


class MCPCatalogTests(unittest.TestCase):
    def test_sanitizes_tools_without_changing_external_model_metadata(self):
        original = catalog()
        before = deepcopy(original)
        result = sanitize_catalog(original)
        expected = deepcopy(before)
        expected['models'][0].update(tool_mode='direct', node_repl_disabled=True,
                                    experimental_supported_tools=[], apply_patch_tool_type=None,
                                    supports_search_tool=False)
        self.assertEqual(result, expected)
        self.assertEqual(original, before)
        result['models'][0]['supported_reasoning_levels'][0]['effort'] = 'low'
        self.assertEqual(original, before)

    def test_clears_tools_for_every_model_and_preserves_order(self):
        value = catalog()
        value['models'].append({'slug': 'other-model', 'tool_mode': None, 'context_window': 8192})
        result = sanitize_catalog(value)
        self.assertEqual([model['slug'] for model in result['models']], ['selected-model', 'other-model'])
        for model in result['models']:
            self.assertEqual(model['experimental_supported_tools'], [])
            self.assertTrue(model['node_repl_disabled'])
            self.assertIsNone(model['apply_patch_tool_type'])
            self.assertFalse(model['supports_search_tool'])
            self.assertEqual(model['tool_mode'], 'direct')
        self.assertEqual(result['models'][1]['context_window'], 8192)

    def test_invalid_catalog_fails_closed(self):
        for value in (None, [], {}, {'models': []}, {'models': {}}, {'models': [None]},
                      {'models': [{}]}, {'models': [{'slug': ' '}]}, {'models': [{'slug': 1}]},
                      {'models': [{'slug': 'same'}, {'slug': 'same'}]}):
            with self.subTest(value=value), self.assertRaises(AuditError) as caught:
                sanitize_catalog(value)
            self.assertEqual(caught.exception.code, 'MCP_COMPATIBILITY')
            self.assertFalse(caught.exception.details['native_fallback'])

    def test_load_uses_bundled_catalog_in_isolated_home(self):
        process = Mock(return_value={'returncode': 0, 'stdout': json.dumps(catalog()).encode(), 'stderr': b''})
        inherited = {'CODEX_HOME': '/user/home', 'PWD': '/source', 'OTHER': 'retained'}
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            result = load_catalog({'executable': '/configured/codex'}, state, process, inherited)
            args, kwargs = process.call_args
            self.assertEqual(args[0], ['/configured/codex', 'debug', 'models', '--bundled'])
            self.assertEqual(args[1], state / 'catalog-home')
            self.assertTrue(args[1].is_dir())
            self.assertEqual(args[2], {'CODEX_HOME': str(args[1]), 'PWD': str(args[1]), 'OTHER': 'retained'})
            self.assertIn('budget', kwargs)
        self.assertEqual(inherited, {'CODEX_HOME': '/user/home', 'PWD': '/source', 'OTHER': 'retained'})
        self.assertEqual(result, sanitize_catalog(catalog()))

    def test_load_errors_are_compatibility_failures_without_fallback(self):
        results = ({'returncode': 2, 'stdout': b'', 'stderr': b'No bundled catalog command'},
                   {'returncode': 0, 'stdout': b'not JSON', 'stderr': b''},
                   {'returncode': 0, 'stdout': b'\xff', 'stderr': b''},
                   {'returncode': 0, 'stdout': b'{"models":[]}', 'stderr': b''})
        for result in results:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as raw, \
                    self.assertRaises(AuditError) as caught:
                load_catalog({'executable': 'codex'}, Path(raw), Mock(return_value=result), {})
            self.assertEqual(caught.exception.code, 'MCP_COMPATIBILITY')
            self.assertFalse(caught.exception.details['native_fallback'])
        with tempfile.TemporaryDirectory() as raw, self.assertRaises(AuditError) as caught:
            load_catalog({'executable': 'missing'}, Path(raw), Mock(side_effect=FileNotFoundError()), {})
        self.assertEqual(caught.exception.code, 'MCP_COMPATIBILITY')


if __name__ == '__main__':
    unittest.main()
