#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline structural comparison, with the tagged OpenCode contract as baseline."""
import copy
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'tests/fixtures/opencode-v1.2.27/openapi.json'


class OpenAPIContractTests(unittest.TestCase):
    def setUp(self):
        self.base = json.loads(BASELINE.read_text())

    def compare(self, candidate, baseline=None, **kwargs):
        from src.backends.openapi_contract import compare
        return compare(baseline or self.base, candidate, **kwargs)

    def test_identical_subset_and_extra_unused_endpoints_match(self):
        candidate = copy.deepcopy(self.base)
        candidate['paths']['/irrelevant'] = {'get': {'description': 'Private documentation'}}
        candidate['components']['schemas']['Unused'] = {'type': 'string'}
        candidate['info'] = {'title': 'Fork', 'version': 'unknown'}
        result = self.compare(candidate)
        self.assertEqual(result['format'], 'opencode-openapi-diff')
        self.assertEqual(result['status'], 'MATCH')
        self.assertEqual(result['scope']['candidate_extra_paths_ignored'], 1)
        self.assertEqual(result['total_changes'], 0)

    def test_annotations_and_set_order_are_ignored(self):
        candidate = copy.deepcopy(self.base)
        schema = candidate['components']['schemas']['AssistantMessage']
        schema['description'] = 'SECRET_DOCUMENTATION'
        schema['required'].reverse()
        result = self.compare(candidate)
        self.assertEqual(result['status'], 'MATCH')
        self.assertNotIn('SECRET_DOCUMENTATION', json.dumps(result))
        self.assertEqual(self.compare(candidate, include_docs=True)['status'], 'DIFFERENT')

    def test_real_schema_changes_are_located(self):
        candidate = copy.deepcopy(self.base)
        props = candidate['components']['schemas']['AssistantMessage']['properties']
        props['structured_output'] = props.pop('structured')
        result = self.compare(candidate)
        self.assertEqual(result['status'], 'DIFFERENT')
        self.assertTrue(any(c['path'].endswith('/properties/structured') and c['op'] == 'removed' for c in result['changes']))
        self.assertTrue(any(c['path'].endswith('/properties/structured_output') and c['op'] == 'added' for c in result['changes']))

    def test_sdk_code_samples_do_not_differ_from_runtime_doc(self):
        candidate = copy.deepcopy(self.base)
        for path in candidate['paths'].values():
            for operation in path.values():
                if isinstance(operation, dict):
                    operation.pop('x-codeSamples', None)
        self.assertEqual(self.compare(candidate)['status'], 'MATCH')
        self.assertEqual(self.compare(candidate, include_docs=True)['status'], 'DIFFERENT')

    def test_large_numeric_values_are_summarized_without_crashing(self):
        base = {'paths': {'/test': {'get': {'schema': {'minimum': 1}}}}}
        candidate = copy.deepcopy(base)
        candidate['paths']['/test']['get']['schema']['minimum'] = 10 ** 300
        result = self.compare(candidate, baseline=base)
        self.assertEqual(result['status'], 'DIFFERENT')
        self.assertEqual(result['changes'][0]['after']['summary']['type'], 'integer')

    def test_required_property_names_default_and_literal_data_are_preserved(self):
        base = {'paths': {'/test': {'get': {'schema': {'type': 'object', 'properties': {
            'description': {'type': 'string'}, 'default': {'type': 'integer', 'default': 2}},
            'required': ['description'], 'const': {'description': 'literal'}}}}}}
        candidate = copy.deepcopy(base)
        schema = candidate['paths']['/test']['get']['schema']
        schema['properties']['description']['type'] = 'integer'
        schema['properties']['default']['default'] = 0
        schema['const']['description'] = 'changed'
        schema['required'].append('default')
        result = self.compare(candidate, baseline=base)
        self.assertEqual(result['total_changes'], 4)

    def test_transitive_and_recursive_refs_terminate(self):
        base = {'paths': {'/test': {'get': {'schema': {'$ref': '#/components/schemas/A'}}}},
            'components': {'schemas': {'A': {'$ref': '#/components/schemas/B'},
                'B': {'type': 'object', 'properties': {'self': {'$ref': '#/components/schemas/A'}, 'value': {'type': 'integer'}}}}}}
        candidate = copy.deepcopy(base)
        candidate['components']['schemas']['B']['properties']['value']['type'] = 'string'
        result = self.compare(candidate, baseline=base)
        self.assertEqual(result['total_changes'], 1)
        self.assertIn('/schemas/B/', result['changes'][0]['path'])

    def test_external_and_missing_refs_are_incomplete(self):
        for reference in ('https://example.invalid/private.json', '#/components/schemas/Missing'):
            with self.subTest(reference=reference):
                candidate = copy.deepcopy(self.base)
                candidate['paths']['/session']['post']['requestBody'] = {'$ref': reference}
                result = self.compare(candidate)
                self.assertEqual(result['status'], 'INCOMPLETE')
                self.assertTrue(result['reference_issues'])

    def test_bool_and_number_are_not_equal(self):
        base = {'paths': {'/test': {'get': {'schema': {'const': True}}}}}
        candidate = {'paths': {'/test': {'get': {'schema': {'const': 1}}}}}
        self.assertEqual(self.compare(candidate, baseline=base)['status'], 'DIFFERENT')

    def test_enum_order_is_ignored_but_arrays_in_const_are_ordered(self):
        base = {'paths': {'/test': {'get': {'schema': {'enum': ['a', 'b'], 'const': {'enum': [1, 2]}}}}}}
        candidate = copy.deepcopy(base)
        candidate['paths']['/test']['get']['schema']['enum'].reverse()
        self.assertEqual(self.compare(candidate, baseline=base)['status'], 'MATCH')
        candidate['paths']['/test']['get']['schema']['const']['enum'].reverse()
        result = self.compare(candidate, baseline=base)
        self.assertEqual(result['status'], 'DIFFERENT')
        self.assertEqual(result['total_changes'], 2)

    def test_escaped_reference_names_and_malformed_component_maps(self):
        base = {'paths': {'/test': {'get': {'schema': {'$ref': '#%2Fcomponents%2Fschemas%2Fa~1b'}}}},
                'components': {'schemas': {'a/b': {'type': 'string'}}}}
        self.assertEqual(self.compare(copy.deepcopy(base), baseline=base)['status'], 'MATCH')
        candidate = copy.deepcopy(base)
        candidate['components']['schemas']['a/b']['type'] = 'integer'
        self.assertEqual(self.compare(candidate, baseline=base)['total_changes'], 1)
        broken = {'paths': {'/test': {'get': {'schema': {'$ref': '#/components/schemas/0'}}}},
                  'components': {'schemas': [{'type': 'string'}]}}
        self.assertEqual(self.compare(broken, baseline=base)['status'], 'INCOMPLETE')

    def test_truncation_is_explicit_and_subtrees_are_compact(self):
        candidate = {'paths': {}}
        result = self.compare(candidate, max_changes=2)
        self.assertEqual(result['status'], 'DIFFERENT')
        self.assertGreater(result['total_changes'], 2)
        self.assertEqual(len(result['changes']), 2)
        self.assertTrue(result['truncated'])
        self.assertLess(len(json.dumps(result)), 12000)


if __name__ == '__main__':
    unittest.main()
