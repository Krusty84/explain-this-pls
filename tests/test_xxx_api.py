# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""The stock OpenCode contract and exact optional XXX extensions share one profile."""
import copy
import itertools
import json
from pathlib import Path
import unittest

from src.backends.openapi_contract import compare
from src.backends.xxx import PROFILE, PROFILE_PATH, Server
from src.contracts.contracts import ContractError, response_error

ROOT = Path(__file__).resolve().parents[1]


class XXXAPITests(unittest.TestCase):
    def setUp(self):
        self.base = json.loads((ROOT / 'tests/fixtures/opencode-v1.2.27/openapi.json').read_text())

    def extended(self, names, *, required=True):
        doc = copy.deepcopy(self.base)
        schemas = doc['components']['schemas']
        if 'compactionCount' in names:
            schemas['Session']['properties']['compactionCount'] = {'type': 'number'}
            if required:
                schemas['Session']['required'].append('compactionCount')
        for name, fields in (
                ('queued', {'runningTaskSize': 'number', 'waitingQueueIndex': 'number'}),
                ('unattended_retry', {'attempt': 'number', 'message': 'string', 'next': 'number'})):
            if name in names:
                properties = {key: {'type': kind} for key, kind in fields.items()}
                properties['type'] = {'type': 'string', 'const': name}
                schemas['SessionStatus']['anyOf'].append({
                    'type': 'object', 'properties': properties, 'required': list(properties)})
        return doc

    def verify(self, doc, *, valid=True):
        original = copy.deepcopy(doc)
        server = Server.__new__(Server)
        server.authorization = 'original-authorization'
        server.artifacts = Path('private-fixture')
        server.meta = {}
        saved, authorizations = {}, []

        def request(method, path):
            self.assertEqual((method, path), ('GET', '/doc'))
            authorizations.append(server.authorization)
            if server.authorization != 'original-authorization':
                raise response_error('BACKEND_ERROR', 'backend', 'Unauthorized fixture', http_status=401)
            return doc

        server.request = request
        server.save = lambda path, value: saved.update({path.name: json.loads(value)})
        if valid:
            server.verify_api()
        else:
            with self.assertRaises(ContractError) as caught:
                server.verify_api()
            self.assertEqual(caught.exception.failure_kind, 'BACKEND_INCOMPATIBLE')
        self.assertEqual(server.authorization, 'original-authorization')
        self.assertEqual(len(authorizations), 3)
        self.assertEqual(authorizations[0], '')
        self.assertNotEqual(authorizations[1], server.authorization)
        self.assertEqual(doc, original)
        self.assertEqual(server.meta['compatibility_profile'], PROFILE)
        self.assertEqual(saved['api-delta.json'], compare(json.loads(PROFILE_PATH.read_text()), doc, max_changes=50))
        return server.meta, saved

    def test_production_baseline_is_the_pinned_upstream_declaration_closure(self):
        self.assertEqual(compare(self.base, json.loads(PROFILE_PATH.read_text()))['status'], 'MATCH')

    def test_stock_and_every_independent_extension_combination(self):
        names = ('compactionCount', 'queued', 'unattended_retry')
        for flags in itertools.product((False, True), repeat=3):
            selected = [name for name, enabled in zip(names, flags) if enabled]
            for required in (False, True):
                with self.subTest(extensions=selected, required=required):
                    meta, saved = self.verify(self.extended(selected, required=required))
                    self.assertEqual(meta['api_allowed_extensions'], selected)
                    expected_changes = len(selected) + int(required and 'compactionCount' in selected)
                    self.assertEqual(meta['api_changes'], expected_changes)
                    report = saved['api-compatibility.json']
                    self.assertEqual(report['accepted_extensions'], selected)
                    self.assertEqual(report['residual_delta']['status'], 'MATCH')

    def test_extensions_do_not_hide_core_changes(self):
        for field in ('structured', 'parentID', 'tokens'):
            doc = self.extended(('compactionCount', 'queued', 'unattended_retry'))
            del doc['components']['schemas']['AssistantMessage']['properties'][field]
            with self.subTest(field=field):
                meta, saved = self.verify(doc, valid=False)
                self.assertEqual(meta['api_allowed_extensions'], ['compactionCount', 'queued', 'unattended_retry'])
                self.assertEqual(saved['api-compatibility.json']['residual_delta']['status'], 'DIFFERENT')

    def test_changed_count_shape_and_unmatched_required_are_rejected(self):
        for shape in ({'type': 'integer'}, {'type': 'number', 'minimum': 0}, {'type': 'string'}, None):
            doc = self.extended(('compactionCount',))
            props = doc['components']['schemas']['Session']['properties']
            if shape is None:
                del props['compactionCount']
            else:
                props['compactionCount'] = shape
            with self.subTest(shape=shape):
                meta, _ = self.verify(doc, valid=False)
                self.assertEqual(meta['api_allowed_extensions'], [])

    def test_changed_extra_and_duplicate_status_declarations_are_rejected(self):
        for name in ('queued', 'unattended_retry'):
            for mutation in ('type', 'required', 'extra', 'duplicate', 'unknown'):
                doc = self.extended((name,))
                variants = doc['components']['schemas']['SessionStatus']['anyOf']
                extra = variants[-1]
                if mutation == 'type':
                    field = next(key for key in extra['properties'] if key != 'type')
                    extra['properties'][field]['type'] = 'boolean'
                elif mutation == 'required':
                    extra['required'].remove('type')
                elif mutation == 'extra':
                    extra['properties']['privateField'] = {'type': 'string'}
                elif mutation == 'duplicate':
                    variants.append(copy.deepcopy(extra))
                else:
                    extra['properties']['type']['const'] = 'unknown_status'
                with self.subTest(name=name, mutation=mutation):
                    self.verify(doc, valid=False)
        doc = self.extended(('compactionCount',))
        doc['components']['schemas']['Session']['required'].append('compactionCount')
        self.verify(doc, valid=False)

    def test_documentation_order_and_unused_endpoints_still_do_not_matter(self):
        doc = self.extended(('compactionCount', 'queued', 'unattended_retry'))
        schemas = doc['components']['schemas']
        schemas['Session']['properties']['compactionCount']['description'] = 'Private documentation'
        for extra in schemas['SessionStatus']['anyOf'][-2:]:
            extra['description'] = 'Optional compatibility extension'
            extra['required'].reverse()
        doc['paths']['/unused'] = {'get': {}}
        meta, saved = self.verify(doc)
        self.assertEqual(meta['api_allowed_extensions'], ['compactionCount', 'queued', 'unattended_retry'])
        self.assertEqual(saved['api-delta.json']['scope']['candidate_extra_paths_ignored'], 1)


if __name__ == '__main__':
    unittest.main()
