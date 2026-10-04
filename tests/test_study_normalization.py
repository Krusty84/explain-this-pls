# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline normalization, diagnostics and provenance; no provider calls."""
import copy
import io
import json
from fixtures.cli_response import cli_result
import unittest
from src.reports.document_rendering import materialize_study, recover_sections
from unittest.mock import patch

from src.contracts.contracts import ContractError, result_diagnostics, validate_result, strict_json
from src.analysis.evidence import canonical, sha, SourceChanged
from explain import AuditError, Folder, Runner, atomic
from src.analysis.ledger import prepare_result, review_context
from src.runtime.reporting import Reporter, diagnostic
from src.analysis.study_normalization import normalize_evidence, normalization_provenance, RULE
from test_evidence_ledger import study, review
from test_folder import FolderFixture
from fixtures.ledger_response import response, model_wire, prompt_context


class PureNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.context = {'branch': 'main', 'source_commit': 'abc'}
        self.data = study(self.context)
        self.data['claims'][0]['evidence_ids'] = ['E-001']

    def error(self, data, code, path):
        with self.assertRaises(ContractError) as caught:
            validate_result('study', data, self.context)
        self.assertEqual(caught.exception.details['code'], code)
        self.assertEqual(caught.exception.details['path'], path)
        self.assertEqual(result_diagnostics('study', data, self.context)['semantic_diagnostics']['violations'][0]['path'], path)
        return caught.exception

    def test_exact_edits_deep_copy_order_and_idempotence(self):
        self.data['evidence'].append(dict(self.data['evidence'][0], id='E-0001'))
        self.data['claims'].append(copy.deepcopy(self.data['claims'][0]) | {'id': 'C-002'})
        self.data['report_sections'][0]['blocks'][0]['claim_ids'].append('C-002')
        self.data['claims'][0]['evidence_ids'] += ['study:E-0001']
        self.data['claims'][1]['evidence_ids'] = ['E-0001', 'study:E-001']
        original = copy.deepcopy(self.data)
        candidate, changes = normalize_evidence('study', self.data, self.context)
        self.assertEqual(self.data, original)
        self.assertEqual(changes, [
            dict(rule=RULE, path='$.claims[0].evidence_ids[0]', before='E-001', after='study:E-001'),
            dict(rule=RULE, path='$.claims[1].evidence_ids[0]', before='E-0001', after='study:E-0001')])
        expected = copy.deepcopy(original)
        expected['claims'][0]['evidence_ids'][0] = 'study:E-001'
        expected['claims'][1]['evidence_ids'][0] = 'study:E-0001'
        self.assertEqual(candidate, expected)  # Every other field and array order is identical.
        validate_result('study', candidate, self.context)
        again, changes = normalize_evidence('study', candidate, self.context)
        self.assertEqual((again, changes), (candidate, []))
        self.assertIsNot(again, candidate)
        again['evidence'][0]['path'] = 'different'
        self.assertEqual(candidate['evidence'], original['evidence'])

    def test_unknown_foreign_and_misspelled_references_are_preserved_and_rejected(self):
        for ref in ('E-999', 'review:E-001', 'E-01', 'e-001', ' E-001', 'E-001 ',
                    'E_001', 'E-0001', 'Study:E-001', 'study:E-999', 'study:E-001:extra',
                    'SECRET\n\x1b[31m<script>'):
            with self.subTest(ref=ref):
                self.data['claims'][0]['evidence_ids'] = [ref]
                candidate, changes = normalize_evidence('study', self.data, self.context)
                self.assertEqual((candidate, changes), (self.data, []))
                code = 'INVALID_EVIDENCE_NAMESPACE' if ref in ('review:E-001', 'Study:E-001') else 'UNKNOWN_EVIDENCE_REFERENCE'
                exc = self.error(candidate, code, '$.claims[0].evidence_ids[0]')
                self.assertNotIn(ref, str(diagnostic(exc)))

    def test_ambiguous_and_invalid_definitions_block_all_changes(self):
        for identifier, code in (('E-001', 'DUPLICATE_RECORD_ID'), ('E-01', 'EVIDENCE_ID_COLLISION'),
                                 ('review:E-002', 'INVALID_RECORD_ID'), ('secret', 'INVALID_RECORD_ID')):
            data = copy.deepcopy(self.data)
            data['evidence'].append(dict(data['evidence'][0], id=identifier))
            with self.subTest(identifier=identifier), self.assertRaises(ContractError) as caught:
                normalize_evidence('study', data, self.context)
            self.assertEqual(caught.exception.details, {'code': code, 'path': '$.evidence[1].id'})
            self.assertEqual(data['claims'][0]['evidence_ids'], ['E-001'])

    def test_converging_references_remain_duplicate(self):
        self.data['claims'][0]['evidence_ids'] = ['E-001', 'study:E-001']
        candidate, changes = normalize_evidence('study', self.data, self.context)
        self.assertEqual(len(changes), 1)
        self.assertEqual(candidate['claims'][0]['evidence_ids'], ['study:E-001'] * 2)
        self.error(candidate, 'DUPLICATE_EVIDENCE_REFERENCE', '$.claims[0].evidence_ids[1]')

    def test_schema_violations_never_repaired_including_large_claims_string(self):
        for claims in ('[{"private":"' + 's' * 21295, '[]', None, {}, [None]):
            data = self.data | {'claims': claims}
            with self.subTest(typ=type(claims)), self.assertRaises(ContractError) as caught:
                normalize_evidence('study', data, self.context)
            self.assertEqual(data['claims'], claims)
            if type(claims) is not list:
                self.assertEqual(caught.exception.details['code'], 'CLAIMS_TYPE_MISMATCH')
                self.assertEqual(caught.exception.details['path'], '$.claims')
                self.assertEqual(caught.exception.details['expected_type'], 'array')
                self.assertNotIn('s' * 100, str(diagnostic(caught.exception)))
        for field, value in (('evidence', '[]'), ('accepted', True), ('normalization_provenance', {})):
            with self.assertRaises(ContractError): normalize_evidence('study', self.data | {field: value}, self.context)
        self.data['claims'][0]['evidence_ids'] = [1]
        with self.assertRaises(ContractError): normalize_evidence('study', self.data, self.context)

    def test_study_only_identity_and_folder_prerequisites(self):
        with self.assertRaises(ContractError): normalize_evidence('study', self.data, {})
        for key, value in (('task', 'architecture_review'), ('branch', 'other'), ('source_commit', 'wrong')):
            with self.subTest(key=key), self.assertRaises(ContractError):
                normalize_evidence('study', self.data | {key: value}, self.context)
        context = {'source_directory': '/source', 'source_fingerprint': 'abc'}
        data = {k: v for k, v in self.data.items() if k not in ('branch', 'source_commit')} | context
        candidate, changes = normalize_evidence('study', data, context, 'folder')
        self.assertEqual(len(changes), 1)
        validate_result('study', candidate, context, 'folder')
        with self.assertRaises(ContractError): normalize_evidence('study', data | {'source_fingerprint': 'bad'}, context, 'folder')

    def test_claims_type_diagnostic_survives_bounded_schema_detail_list(self):
        data = self.data | {'evidence': [None] * 110, 'claims': 'SECRET_INVALID_REGISTRY'}
        with self.assertRaises(ContractError) as caught: normalize_evidence('study', data, self.context)
        self.assertEqual(caught.exception.details['code'], 'CLAIMS_TYPE_MISMATCH')
        self.assertEqual(caught.exception.details['actual_type'], 'string')
        self.assertTrue(caught.exception.details['truncated'])
        self.assertNotIn('SECRET_INVALID_REGISTRY', caught.exception.safe_message)

    def test_full_validation_still_checks_block_links_ids_and_completion(self):
        self.data['report_sections'][0]['blocks'][0]['claim_ids'] = ['C-999']
        candidate, changes = normalize_evidence('study', self.data, self.context)
        self.assertEqual(len(changes), 1)
        self.error(candidate, 'UNKNOWN_REFERENCE', '$.report_sections[0].blocks[0].claim_ids[0]')
        self.data['claims'][0]['id'] = 'bad'
        candidate, _ = normalize_evidence('study', self.data, self.context)
        self.error(candidate, 'INVALID_RECORD_ID', '$.claims[0].id')
        self.data['claims'][0]['id'] = 'C-001'
        self.data['report_sections'][0]['blocks'][0]['claim_ids'] = ['C-001']
        self.data.update(completion_status='PARTIAL', limitations=['Incomplete.'])
        candidate, _ = normalize_evidence('study', self.data, self.context)
        self.assertEqual(candidate['completion_status'], 'PARTIAL')

    def test_review_keeps_explicit_namespaces_and_target_diagnostics(self):
        candidate, _ = normalize_evidence('study', self.data, self.context)
        context = review_context(prepare_result('study', candidate, self.context), self.context)
        data = review(context)
        for ref in ('E-001', 'review:E-001', 'study:E-001'):
            data['claims'][0]['evidence_ids'] = [ref]
            if ref == 'E-001':
                with self.assertRaises(ContractError) as caught: validate_result('review', data, context)
                self.assertEqual(caught.exception.details['code'], 'UNKNOWN_EVIDENCE_REFERENCE')
            else:
                validate_result('review', data, context)
            with self.assertRaises(ContractError): normalize_evidence('study', data, context)
        data['target'] = data['target'] | {'document_sha256': 'wrong'}
        with self.assertRaises(ContractError) as caught: validate_result('review', data, context)
        self.assertEqual(caught.exception.details['code'], 'TARGET_IDENTITY_MISMATCH')
        self.assertEqual(caught.exception.details['path'], '$.target.document_sha256')


class NormalizationPipelineTests(FolderFixture):
    def test_cli_review_normalization_preserves_frozen_study_in_both_policies(self):
        for backend in ('codex', 'claude-code'):
            for policy in ('strict', 'compromise'):
                runner, context, raw = self.prepare(backend, policy)
                doc, _ = self.invoke(runner, context, raw)
                original = copy.deepcopy(doc)
                ctx = review_context(doc, context)
                wire = response(ctx)
                wire['evidence'][0]['id'] = 'review:E-01'
                wire['claims'][0]['evidence_ids'] = ['review:E-01', 'study:E-001']
                runner.cfg['_agents']['review']['backend'] = backend
                def answer(command, cwd, env, payload, **kwargs):
                    model_data = model_wire(wire, prompt_context(payload), ctx)
                    envelope = model_data if backend == 'codex' else {'is_error': False, 'structured_output': model_data}
                    return cli_result(command, envelope)
                with patch('explain.process', side_effect=answer) as process:
                    saved, meta = runner.invoke('review', ctx, runner.run_dir / 'review.logs')
                self.assertEqual(process.call_count, 1)
                self.assertTrue(saved['program_checks']['policy_satisfied'])
                self.assertEqual(doc, original)
                self.assertEqual(saved['claim_registry'], original['claims'])
                self.assertEqual(saved['target'], ctx['review_target'])
                self.assertEqual(saved['normalization_provenance']['replacement_count'], 2)
                self.assertTrue(meta['publication_complete'])

    def prepare(self, backend='codex', policy='strict', local=True):
        cfg = self.config() | {'result_policy': policy}
        cfg['_agents']['study']['backend'] = backend
        reporter = Reporter(stdout=io.StringIO(), stderr=io.StringIO(), verbose=True)
        self.addCleanup(reporter.close)
        runner = Runner(cfg, self.base / ('run-' + str(len(list(self.base.glob('run-*'))))), reporter=reporter)
        context = {'source_directory': str(self.source),
                   'source_fingerprint': Folder(self.source).snapshot()['source_fingerprint']}
        data = response(context)
        if local: data['claims'][0]['evidence_ids'] = ['E-001']
        return runner, context, data

    def invoke(self, runner, context, data):
        def process(command, cwd, env, payload, **kwargs):
            model_data = model_wire(data, prompt_context(payload), context)
            envelope = model_data if runner.cfg['_agents']['study']['backend'] == 'codex' else {
                'is_error': False, 'structured_output': model_data}
            return cli_result(command, envelope)
        with patch('explain.process', side_effect=process):
            return runner.invoke('study', context, runner.run_dir / 'study.logs')

    def test_both_cli_backends_preserve_extracted_and_publish_provenance(self):
        for backend in ('codex', 'claude-code'):
            for local in (True, False):
                runner, context, raw = self.prepare(backend, local=local)
                original = copy.deepcopy(raw)
                saved, meta = self.invoke(runner, context, raw)
                attempt = runner.run_dir / 'study.logs/attempt-001'
                extracted_bytes = (attempt / 'extracted.json').read_bytes()
                extracted = strict_json(extracted_bytes.decode())
                expanded = strict_json((attempt / 'expanded.json').read_text())
                normalized = strict_json((attempt / 'normalized.json').read_text())
                journal = strict_json((attempt / 'normalization.json').read_text())
                self.assertEqual(extracted, model_wire(original, prompt_context((attempt / 'input.prompt.txt').read_bytes()), context))
                self.assertEqual(expanded, original)
                self.assertEqual(raw, original)
                candidate, changes = normalize_evidence('study', expanded, context, 'folder')
                self.assertEqual(normalized, candidate)
                self.assertEqual((attempt / 'extracted.json').read_bytes(), extracted_bytes)
                provenance = normalization_provenance(expanded, candidate, changes)
                self.assertEqual(journal, provenance | {'changes': changes})
                self.assertEqual(provenance['replacement_count'], int(local))
                self.assertEqual(provenance['input_sha256'], sha(canonical(original)))
                self.assertEqual(provenance['normalized_sha256'], sha(canonical(normalized)))
                self.assertEqual(saved['normalization_provenance'], provenance)
                self.assertEqual(meta['normalization_provenance'], provenance)
                self.assertEqual(saved['claims'], materialize_study(candidate)['claims'])
                self.assertEqual(saved['review_plan']['registry_sha256'], sha(canonical(saved['claims'])))
                self.assertEqual((runner.run_dir / 'ARCHITECTURE.md').read_bytes(), materialize_study(raw)['report_markdown'].encode())
                self.assertTrue(meta['publication_complete'])
                self.assertEqual(saved['program_checks']['semantic_quality'], 'NOT_MEASURED')
                self.assertEqual('study_normalized' in runner.reporter.stderr.getvalue(), local)

    def test_contract_failure_after_normalization_has_candidate_diagnostic(self):
        for policy in ('strict', 'compromise'):
            runner, context, data = self.prepare(policy=policy)
            data['report_sections'][0]['blocks'][0]['claim_ids'] = ['SECRET_LOCATOR']
            if policy == 'strict':
                with self.assertRaises(ContractError) as caught: self.invoke(runner, context, data)
                self.assertEqual(caught.exception.details['code'], 'UNKNOWN_REFERENCE')
            else:
                saved, meta = self.invoke(runner, context, data)
                self.assertIsNone(saved)
                self.assertIn('UNKNOWN_REFERENCE', meta['usable_material']['contract_failure']['message'])
                self.assertNotIn('UNKNOWN_EVIDENCE_REFERENCE', json.dumps(meta['usable_material']['validation_issues']))
            attempt = runner.run_dir / 'study.logs/attempt-001'
            validation = strict_json((attempt / 'validation.json').read_text())
            self.assertFalse(validation['valid'])
            self.assertEqual(validation['validated_object'], 'normalized.json')
            self.assertEqual(validation['normalization_provenance']['replacement_count'], 1)
            self.assertEqual(validation['error']['details']['path'], '$.report_sections[0].blocks[0].claim_ids[0]')
            self.assertNotIn('SECRET_LOCATOR', json.dumps(validation))
            self.assertFalse((runner.run_dir / 'study.json').exists())

    def test_large_string_claims_retained_without_conversion_or_acceptance(self):
        for policy in ('strict', 'compromise'):
            runner, context, data = self.prepare(policy=policy)
            data['claims'] = '[{"secret":"' + 'x' * 21295
            if policy == 'strict':
                with self.assertRaises(ContractError) as caught: self.invoke(runner, context, data)
                self.assertIn('CLAIMS_TYPE_MISMATCH at $.claims; expected array, got string', caught.exception.safe_message)
            else:
                saved, meta = self.invoke(runner, context, data)
                self.assertIsNone(saved)
                material = meta['usable_material']
                self.assertEqual(material['report_markdown'], recover_sections(data['report_sections']))
                self.assertNotIn('claims', material)
                runner.manifest = {}
                runner.store_stage({}, 'study', None, meta, context)
                self.assertIn('Text retained; policy checks not completed. Agent self-assessment: COMPLETE.',
                              runner.reporter.stderr.getvalue())
            attempt = runner.run_dir / 'study.logs/attempt-001'
            self.assertEqual(strict_json((attempt / 'extracted.json').read_text()),
                             model_wire(data, prompt_context((attempt / 'input.prompt.txt').read_bytes()), context))
            self.assertFalse((attempt / 'normalized.json').exists())
            self.assertFalse((runner.run_dir / 'study.json').exists())

    def test_required_artifact_writes_and_source_guards_cannot_publish(self):
        for policy in ('strict', 'compromise'):
            for filename in ('expanded.json', 'binding.json', 'normalized.json', 'normalization.json', 'materialized.json', 'provenance.json', 'validation.json', 'study.json', 'invocation.json'):
                runner, context, data = self.prepare(policy=policy)
                def fail(path, content):
                    # Fail the post-validation write, not the initial diagnostics.
                    if path.name == filename and (filename not in ('validation.json', 'invocation.json') or
                            '"valid": true' in str(content) or '"status": "SUCCEEDED"' in str(content)):
                        raise OSError('synthetic disk failure')
                    atomic(path, content)
                with patch('explain.atomic', side_effect=fail), self.assertRaises(OSError):
                    self.invoke(runner, context, data)
                meta = strict_json((runner.run_dir / 'study.logs/invocation.json').read_text())
                self.assertFalse(meta['publication_complete'])
                self.assertEqual(meta['status'], 'FAILED')
            runner, context, data = self.prepare(policy=policy)
            with patch('src.analysis.ledger.resolve_evidence', side_effect=SourceChanged), self.assertRaises(AuditError):
                self.invoke(runner, context, data)
            self.assertFalse((runner.run_dir / 'study.json').exists())

    def test_normalization_does_not_satisfy_evidence_or_completion_policy(self):
        for mutation in ('missing-evidence-file', 'unsafe-path', 'source-quote', 'unknown-source', 'partial'):
            runner, context, data = self.prepare()
            if mutation == 'partial':
                data.update(completion_status='PARTIAL', limitations=['Not finished.'])
            elif mutation == 'unsafe-path':
                data['evidence'][0]['path'] = '../outside.py'
            elif mutation == 'source-quote':
                data['evidence'][0]['quote'] = 'Synthetic wrong source quotation'
            elif mutation == 'unknown-source':
                data['evidence'][0]['source_id'] = 'source-999'
            else:
                data['evidence'][0]['path'] = 'missing.py'
            saved, meta = self.invoke(runner, context, data)
            self.assertEqual(saved['normalization_provenance']['replacement_count'], 1)
            self.assertEqual(saved['completion_status'], data['completion_status'])
            self.assertFalse(saved['program_checks']['policy_satisfied'])
            if mutation == 'missing-evidence-file':
                self.assertEqual(saved['program_checks']['evidence'][0]['status'], 'NOT_FOUND')
            elif mutation != 'partial':
                self.assertNotEqual(saved['program_checks']['evidence'][0]['status'], 'RESOLVED')


if __name__ == '__main__':
    unittest.main()
