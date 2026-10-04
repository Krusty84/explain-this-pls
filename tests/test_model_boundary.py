import copy
import json
import unittest

from src.contracts.contracts import (ContractError, MODEL_SCHEMAS, MODEL_FOLDER_SCHEMAS, SCHEMAS,
                       FOLDER_SCHEMAS, validate_schema)
from src.reports.document_rendering import materialize_study
from src.analysis.evidence import canonical, sha, source_catalog
from fixtures.ledger_response import response
from src.analysis.ledger import review_context
from src.model.model_boundary import BindingRegistry
from src.model.model_context import project_model_context, project_model_response


class ModelBoundaryTests(unittest.TestCase):
    def folder(self):
        context = {'source_mode': 'folder', 'source_directory': '/source', 'source_fingerprint': 'f' * 64}
        context['sources'] = source_catalog(context)
        return context

    def review(self, revision='001'):
        context = {'branch': 'main', 'source_commit': 'a' * 64, 'revision_id': revision}
        wire = response(context)
        document = materialize_study(wire)
        return review_context(document, context)

    def assert_hashless(self, value):
        if isinstance(value, dict):
            self.assertFalse(any(key.endswith('_sha256') or key in ('sha256', 'fingerprint',
                'source_fingerprint', 'artifact_hashes', 'material_hashes') for key in value))
            for item in value.values():
                self.assert_hashless(item)
        elif isinstance(value, list):
            for item in value:
                self.assert_hashless(item)

    def test_backend_schemas_are_separate_and_hashless(self):
        for schemas in (MODEL_SCHEMAS, MODEL_FOLDER_SCHEMAS):
            self.assert_hashless(schemas)
        self.assertIn('target', SCHEMAS['review']['properties'])
        self.assertIn('source_fingerprint', FOLDER_SCHEMAS['study']['properties'])
        self.assertIn('review_target_id', MODEL_SCHEMAS['review']['properties'])
        self.assertIn('source_snapshot_id', MODEL_FOLDER_SCHEMAS['study']['properties'])

    def test_source_binding_is_run_local_stable_and_lossless(self):
        context = self.folder()
        original = copy.deepcopy(context)
        registry = BindingRegistry()
        binding = registry.bind('study', context, 'folder')
        projected = binding.project()
        identifier = projected['source_snapshot_id']
        self.assertRegex(identifier, r'^S-[A-Za-z0-9_-]{16}$')
        self.assertEqual(projected['sources'][0]['identity']['source_snapshot_id'], identifier)
        self.assertEqual(registry.bind('catalog', context, 'folder').project()['source_snapshot_id'], identifier)
        self.assertNotEqual(BindingRegistry().bind('study', context, 'folder').project()['source_snapshot_id'], identifier)
        self.assert_hashless(projected)
        wire = response(projected)
        expanded = binding.expand(wire)
        self.assertEqual(expanded, response(context))
        self.assertEqual(context, original)
        record = binding.record(wire, expanded)
        self.assertEqual(record['wire_sha256'], sha(canonical(wire)))
        self.assertEqual(record['expanded_sha256'], sha(canonical(expanded)))
        self.assertEqual(record['mappings'][0]['identity']['fingerprint'], context['source_fingerprint'])
        self.assertIsNone(binding.record(None, None)['wire_sha256'])
        validate_schema(expanded, FOLDER_SCHEMAS['study'])

    def test_review_expansion_and_repair_preserve_contents_and_commits(self):
        context = self.review()
        context['architecture_document']['program_checks'] = {'evidence': [
            {'file_sha256': 'b' * 64, 'fragment_sha256': 'c' * 64, 'encoding': 'utf-8', 'status': 'RESOLVED'}]}
        context['diagnostics'] = {'explanation': 'SHA-256 is used by this system.', 'quote': 'd' * 64}
        context['symlink'] = {'target': '../outside'}
        original = copy.deepcopy(context)
        binding = BindingRegistry().bind('review', context)
        projected = binding.project()
        self.assert_hashless(projected)
        self.assertRegex(projected['review_target_id'], r'^T-[A-Za-z0-9_-]{16}$')
        self.assertEqual(projected['source_commit'], 'a' * 64)
        self.assertEqual(projected['diagnostics'], context['diagnostics'])
        self.assertEqual(projected['symlink'], {'target': '../outside'})
        wire = response(projected)
        expanded = binding.expand(wire)
        self.assertEqual(expanded['target'], context['review_target'])
        validate_schema(expanded, SCHEMAS['review'])
        invalid = copy.deepcopy(wire)
        invalid.update(target={'document_sha256': 'PRIVATE'}, unexpected={'file_sha256': 'PRIVATE', 'quote': 'retain'})
        sanitized = project_model_response(invalid)
        self.assertNotIn('PRIVATE', json.dumps(sanitized))
        self.assertEqual(sanitized['unexpected']['quote'], 'retain')
        self.assertEqual(context, original)

    def test_identity_failures_cannot_be_recovered(self):
        context = self.folder()
        binding = BindingRegistry().bind('study', context, 'folder')
        wire = response(binding.project())
        for changes in ({'source_snapshot_id': 'S-unknown'}, {'source_directory': '/other'},
                        {'source_fingerprint': context['source_fingerprint']},
                        {'review_target_id': 'T-wrong-role'}):
            for recover in (False, True):
                with self.subTest(changes=changes, recover=recover), self.assertRaises(ContractError) as error:
                    binding.expand(wire | changes, allow_invalid=recover)
                self.assertEqual(error.exception.failure_kind, 'IDENTITY_MISMATCH')
        malformed = wire | {'claims': 'wrong type'}
        self.assertEqual(binding.expand(malformed, allow_invalid=True)['claims'], 'wrong type')
        with self.assertRaises(ContractError) as error:
            binding.expand(malformed)
        self.assertEqual(error.exception.failure_kind, 'SCHEMA_ERROR')
        review = self.review()
        registry = BindingRegistry()
        old = registry.bind('review', review)
        current = registry.bind('review', self.review('002'))
        with self.assertRaises(ContractError):
            current.expand(response(current.project()) | {'review_target_id': old.project()['review_target_id']}, allow_invalid=True)

    def compare(self):
        review = self.review()
        study = review['architecture_document'] | {'revision_id': '001', 'review_plan': review['review_plan']}
        return {'baseline_branch': 'main', 'baseline_commit': 'a' * 64,
                'requested_branches': ['main', 'other'], 'branches': [
                    {'branch': 'main', 'selected_revision': '001', 'study': study,
                     'review': {'claims': [{'id': 'C-001'}], 'target': review['review_target']}}]}

    def test_compare_references_use_selected_role_and_revision(self):
        context = self.compare()
        binding = BindingRegistry().bind('compare', context)
        projected = binding.project()
        identifier = projected['branches'][0]['study']['review_plan']['review_target_id']
        ref = {'branch': 'main', 'revision_id': '001', 'artifact': 'study', 'claim_id': 'C-001',
               'review_target_id': identifier}
        wire = response(projected)
        wire['differences'] = [{'id': 'D-001', 'branch': 'other', 'category': 'flow',
            'classification': 'REPORTED_UNVERIFIED', 'baseline_statement': 'a', 'branch_statement': 'b',
            'evidence_refs': [ref], 'explanation': 'test'}]
        expanded = binding.expand(wire)
        validate_schema(expanded, SCHEMAS['compare'])
        self.assertEqual(expanded['differences'][0]['evidence_refs'][0]['document_sha256'],
                         context['branches'][0]['study']['review_plan']['document_sha256'])
        for changes in ({'revision_id': '002'}, {'artifact': 'catalog'}, {'branch': 'other'},
                        {'claim_id': 'C-999'}, {'review_target_id': 'T-unknown'}, {'document_sha256': 'forged'}):
            invalid = copy.deepcopy(wire)
            invalid['differences'][0]['evidence_refs'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                binding.expand(invalid, allow_invalid=True)

    def test_context_and_binding_mutations_detected(self):
        context = self.folder()
        binding = BindingRegistry().bind('study', context, 'folder')
        context['source_fingerprint'] = 'mutated'
        with self.assertRaises(ContractError):
            binding.assert_unchanged(context)
        binding._sources[next(iter(binding._sources))] = 'S-mutated'
        with self.assertRaises(ContractError):
            binding.project()

    def test_pure_projection_handles_nested_service_values_idempotently(self):
        value = {'nested': [{'program_checks': {'evidence': [{'file_sha256': 'a', 'fragment_sha256': 'b'}]},
                            'source_identity': {'mode': 'folder', 'directory': '/s', 'fingerprint': 'c'},
                            'normalization_provenance': {'input_sha256': 'd'}, 'source_commit': 'e' * 64}],
                 'report_markdown': 'SHA-256: ' + 'f' * 64}
        before = copy.deepcopy(value)
        projected = project_model_context('study', value)
        self.assert_hashless(projected)
        self.assertEqual(projected['report_markdown'], value['report_markdown'])
        self.assertEqual(projected, project_model_context('study', projected))
        self.assertEqual(value, before)


if __name__ == '__main__':
    unittest.main()
