# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Document acceptance and inventory scope do not depend on model classification."""
import copy
import json
from pathlib import Path

from fixtures.study_runner import run_case
from src.contracts.contracts import MODEL_SCHEMAS, MODEL_FOLDER_SCHEMAS
from src.reports.document_rendering import render_document
from test_folder import FolderFixture


class AdvisoryIndependenceTests(FolderFixture):
    run_case = run_case

    def test_wire_schema_requires_the_document_only(self):
        for schemas in (MODEL_SCHEMAS, MODEL_FOLDER_SCHEMAS):
            schema = schemas['study']
            for field in ('subsystems', 'exclusions', 'coverage', 'relevance_notes', 'advisory'):
                self.assertNotIn(field, schema['properties'])
                self.assertNotIn(field, schema['required'])
            for field in ('report_sections', 'claims', 'evidence', 'completion_status', 'limitations'):
                self.assertIn(field, schema['required'])

    def test_codex_publish_valid_documents_with_missing_or_malformed_advice(self):
        variants = ({}, {'subsystems': 'invalid', 'exclusions': 17, 'coverage': None, 'advisory': False},
                    {'subsystems': [dict(id='S-001', name='App', purpose='Observation.', paths=['absent'])],
                     'exclusions': [dict(path='absent', reason='Unconfirmed model observation.')],
                     'coverage': [dict(area_id='missing', status='NOT_INSPECTED', evidence_ids=['bad'], limitation='Unconfirmed.')],
                     'relevance_notes': ['vendor/ is outside the main architecture.']})
        backend = 'codex'
        for policy in ('strict', 'compromise'):
            for advisory in variants:
                original = {}

                def change(runner, context, data):
                    if context['stage'] == 'study':
                        data.update(copy.deepcopy(advisory))
                        original.update(copy.deepcopy(data))

                with self.subTest(backend=backend, policy=policy, advisory=advisory):
                    manifest, code = self.run_case(backend=backend, policy=policy, large=False,
                                                  review=True, change=change)
                    self.assertEqual((code, manifest['status'], manifest['accepted']), (0, 'COMPLETE', True),
                                     manifest.get('diagnostics'))
                    document = manifest['study']
                    self.assertTrue(document['program_checks']['policy_satisfied'])
                    self.assertEqual(document['report_markdown'],
                                     render_document(original['report_sections'], original['claims'])[0])
                    self.assertEqual(document['evidence'], original['evidence'])
                    self.assertEqual(document['claims'][0]['statement'], original['claims'][0]['statement'])
                    self.assertNotIn('study_material', manifest)
                    attempt = Path(manifest['study_invocation']['artifact_directory'])
                    self.assertEqual(json.loads((attempt / 'extracted.json').read_text()), original)
                    self.assertFalse((attempt / 'recovered.json').exists())
                    if advisory:
                        self.assertEqual(json.loads(document['advisory']['raw_json']), advisory)
                        self.assertTrue(document['advisory']['diagnostics'])
                    self.assertEqual({p for area in manifest['coverage_plan']['areas'] for p in area['file_paths']},
                                     {'app.py'})

    def test_model_exclusion_cannot_remove_sources_or_bypass_citation_checks(self):
        (self.source / 'src').mkdir()
        (self.source / 'src/main.py').write_text('def main(): return 42\n')
        plans, inventories = [], []
        for valid_pointer in (True, False):
            def change(runner, context, data):
                if context['stage'] == 'study':
                    data['exclusions'] = [dict(path='src/', reason='Model considers this irrelevant.')]
                    data['relevance_notes'] = ['src/ does not belong to the main architecture.']
                    data['evidence'][0]['path'] = 'src/main.py'
                    if not valid_pointer:
                        data['evidence'][0]['end_line'] = 99

            with self.subTest(valid_pointer=valid_pointer):
                manifest, code = self.run_case(large=False, policy='strict', review=True, change=change)
                inventory = json.loads((self.runner.session.run_dir / 'source.inventory.json').read_text())
                inventories.append(inventory)
                plans.append(manifest['coverage_plan'])
                paths = {entry['path'] for entry in inventory['entries'] if entry['type'] == 'file'}
                self.assertEqual(paths, {'app.py', 'src/main.py'})
                self.assertEqual({p for area in plans[-1]['areas'] for p in area['file_paths']}, paths)
                checks = manifest['study']['program_checks']
                self.assertEqual(checks['evidence'][0]['path'], 'src/main.py')
                self.assertEqual(checks['policy_satisfied'], valid_pointer)
                self.assertEqual(manifest['accepted'], valid_pointer)
                self.assertEqual(code == 0, valid_pointer)
        self.assertEqual(inventories[0], inventories[1])
        self.assertEqual(plans[0], plans[1])

    def test_mandatory_contract_and_source_integrity_still_block_strict_acceptance(self):
        for failure in ('sections', 'source_identity', 'evidence_source', 'source_changed'):
            original_bytes = (self.source / 'app.py').read_bytes()

            def change(runner, context, data):
                if context['stage'] != 'study':
                    return
                data.update(subsystems='invalid', exclusions=None, coverage=False)
                if failure == 'sections':
                    data['report_sections'] = 'Invalid report structure.'
                elif failure == 'source_identity':
                    data['source_snapshot_id'] = 'S-invalid-identity'
                elif failure == 'evidence_source':
                    data['evidence'][0]['source_id'] = 'source-999'
                else:
                    (self.source / 'app.py').write_text('changed source\n')

            with self.subTest(failure=failure):
                manifest, code = self.run_case(large=False, policy='strict', review=True, change=change)
                self.assertNotEqual(code, 0)
                self.assertFalse(manifest['accepted'])
                if failure == 'source_changed':
                    self.assertFalse(manifest['publication_complete'])
                    self.assertTrue(manifest['critical_failure'])
                if failure in ('sections', 'source_identity'):
                    self.assertIsNone(manifest['study'])
            (self.source / 'app.py').write_bytes(original_bytes)

    def test_partial_document_preserves_its_limitations_and_registry(self):
        def change(runner, context, data):
            if context['stage'] == 'study':
                data.update(completion_status='PARTIAL', limitations=['Runtime behavior remains unverified.'],
                            subsystems=None, coverage='unverified')

        manifest, code = self.run_case(large=False, policy='strict', change=change)
        self.assertEqual(code, 2)
        document = manifest['study']
        self.assertEqual(document['completion_status'], 'PARTIAL')
        self.assertEqual(document['limitations'], ['Runtime behavior remains unverified.'])
        self.assertEqual(document['claims'][0]['id'], 'C-001')
        self.assertTrue((self.runner.session.run_dir / 'study.json').exists())
        self.assertTrue((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())
