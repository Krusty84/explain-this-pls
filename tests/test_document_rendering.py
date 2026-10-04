# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Document rendering with synthetic fixtures and no real model calls."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from contracts import (ContractError, SCHEMAS, FOLDER_SCHEMAS, has_ledger_structure,
                       validate_result, validate_schema)
from document_rendering import materialize_study, validate_materialized, recover_sections
from evidence import canonical, lines, sha
from ledger import review_context, verify_review_context, prepare_result
from study_normalization import normalize_evidence
from fixtures.ledger_response import response
from final_report import recoverable_material


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.context = dict(branch='main', source_commit='abc')
        self.wire = response(self.context)

    def test_exact_serialization_all_fragment_types_and_multiple_mentions(self):
        fragments = ['  Кириллица 😀 e\u0301 é\t  \r\nsecond\r\n',
            '| A | B |\n|---|---|\n| x | y |\n', '- one\n- two',
            '```python\n\tprint("😀")  \n\n```', 'repeat', 'repeat',
            'first\n\nlast\n\n', 'final without LF']
        self.wire['claims'].append(self.wire['claims'][0] | {'id': 'C-002'})
        self.wire['report_sections'][0]['blocks'] = [dict(markdown=f, claim_ids=['C-001', 'C-002']) for f in fragments]
        original = copy.deepcopy(self.wire)
        validate_result('study', self.wire, self.context)
        with patch('evidence.read_confined', side_effect=AssertionError('pure renderer')):
            saved = materialize_study(self.wire)
        self.assertEqual(self.wire, original)
        self.assertEqual(saved, materialize_study(copy.deepcopy(self.wire)))
        self.assertEqual(canonical(saved), canonical(materialize_study(self.wire)))
        self.assertNotIn('report_sections', saved)
        self.assertFalse(has_ledger_structure('study', self.wire))
        with self.assertRaises(ContractError) as caught: review_context(self.wire, self.context)
        self.assertEqual(caught.exception.details['code'], 'UNMATERIALIZED_STUDY')
        self.assertTrue(has_ledger_structure('study', saved))
        doc_lines = lines(saved['report_markdown'])
        for claim in saved['claims']:
            self.assertEqual(len(claim['document_locators']), len(fragments))
            for fragment, loc in zip(fragments, claim['document_locators']):
                serialized = fragment.replace('\r\n', '\n')
                if not serialized.endswith('\n'): serialized += '\n'
                self.assertEqual(loc['quote'], serialized)
                self.assertEqual(loc['quote'], ''.join(doc_lines[loc['start_line'] - 1:loc['end_line']]))
                self.assertEqual(doc_lines[loc['end_line']], '\n')
        self.assertIn('e\u0301 é\t  \n', saved['report_markdown'])
        first = saved['claims'][0]['document_locators']
        self.assertEqual(first[4]['quote'], first[5]['quote'])
        self.assertNotEqual(first[4]['start_line'], first[5]['start_line'])
        validate_materialized(saved)

    def test_wire_and_materialized_are_separate_for_both_modes(self):
        for context, mode in ((self.context, 'git'),
                (dict(source_directory='/source', source_fingerprint='abc'), 'folder')):
            wire = response(context)
            validate_result('study', wire, context, mode)
            for key, value in (('report_markdown', 'hybrid'), ('block_map', []),
                               ('materialization_provenance', {}), ('program_checks', {})):
                with self.assertRaises(ContractError): validate_result('study', wire | {key: value}, context, mode)
                with self.assertRaises(ContractError): materialize_study(wire | {key: value})
            for key in ('document_locator', 'document_locators', 'block_ids'):
                invalid = copy.deepcopy(wire); invalid['claims'][0][key] = []
                with self.assertRaises(ContractError): validate_result('study', invalid, context, mode)
            with self.assertRaises(ContractError):
                validate_schema(materialize_study(wire), (SCHEMAS if mode == 'git' else FOLDER_SCHEMAS)['study'])

    def test_bare_cr_adjacent_to_inserted_lf_uses_evidence_line_view(self):
        for fragment in ('bare\rCR', 'trailing\r', 'adjacent\r\r\n'):
            self.wire['report_sections'][0]['blocks'][0]['markdown'] = fragment
            saved = materialize_study(self.wire)
            serialized = fragment.replace('\r\n', '\n')
            if not serialized.endswith('\n'): serialized += '\n'
            self.assertIn(serialized, saved['report_markdown'])
            loc = saved['claims'][0]['document_locators'][0]
            self.assertEqual(loc['quote'], ''.join(lines(serialized)))
            validate_materialized(saved)

    def test_graph_and_sections_rejected_locally(self):
        for mutation, code in (('unknown', 'UNKNOWN_REFERENCE'), ('repeat', 'DUPLICATE_REFERENCE'),
                               ('unbound', 'UNBOUND_CLAIM'), ('duplicate', 'DUPLICATE_RECORD_ID'),
                               ('order', 'SECTION_ORDER_MISMATCH'), ('empty', 'EMPTY_SECTION'),
                               ('title', 'INVALID_SECTION_TITLE')):
            data = copy.deepcopy(self.wire)
            if mutation == 'unknown': data['report_sections'][0]['blocks'][0]['claim_ids'] = ['C-999']
            if mutation == 'repeat': data['report_sections'][0]['blocks'][0]['claim_ids'] *= 2
            if mutation == 'unbound': data['report_sections'][0]['blocks'][0]['claim_ids'] = []
            if mutation == 'duplicate': data['claims'] *= 2
            if mutation == 'order': data['report_sections'].reverse()
            if mutation == 'empty': data['report_sections'][0]['blocks'] = []
            if mutation == 'title': data['report_sections'][0]['title'] = 'a\nb'
            with self.subTest(mutation=mutation), self.assertRaises(ContractError) as caught:
                validate_result('study', data, self.context)
            self.assertEqual(caught.exception.details['code'], code)

    def test_frozen_context_catches_document_claim_and_locator_changes(self):
        doc = materialize_study(self.wire)
        ctx = review_context(doc, self.context)
        self.assertEqual(ctx['claim_registry'], doc['claims'])
        for mutation in ('text', 'claim', 'locator', 'registry'):
            modified = copy.deepcopy(ctx)
            if mutation == 'text': modified['architecture_document']['report_markdown'] += 'changed'
            if mutation == 'claim': modified['architecture_document']['claims'][0]['scope'] += 'changed'
            if mutation == 'locator': modified['architecture_document']['claims'][0]['document_locators'][0]['start_line'] += 1
            if mutation == 'registry': modified['claim_registry'] = []
            with self.subTest(mutation=mutation), self.assertRaises(ContractError): verify_review_context(modified)
        doc['normalization_provenance'] = {'normalized_sha256': 'wrong'}
        with self.assertRaises(ContractError): validate_materialized(doc)

    def test_recovery_is_authored_blocks_without_registry_or_positive_acceptance(self):
        data = copy.deepcopy(self.wire)
        data['claims'] = 'invalid'
        data['report_sections'][1]['blocks'].append({'markdown': 123})
        recovered = recoverable_material('study', data, self.context, 'git', {})
        self.assertEqual(recovered['narrative_origin'], 'PROGRAM_ASSEMBLED_AUTHOR_BLOCKS')
        self.assertEqual(recovered['report_markdown'], recover_sections(data['report_sections']))
        self.assertNotIn('claims', recovered)
        ctx = review_context(recovered, self.context)
        self.assertEqual(ctx['claim_registry'], [])
        self.assertFalse(ctx['review_plan']['eligible_study'])
        from presentation import render_stage
        self.assertIn('Text retained after contract rejection', render_stage('study', recovered))
        for key in ('strict_valid', 'narrative_origin'):
            unmarked = {k: v for k, v in recovered.items() if k != key}
            with self.assertRaises(ContractError): review_context(unmarked, self.context)
            with self.assertRaises(ContractError): render_stage('study', unmarked)

    def test_markdown_study_is_rejected_without_recovery_or_review(self):
        from presentation import render_stage
        for mode, context in (('git', self.context),
                ('folder', dict(source_directory='/source', source_fingerprint='abc'))):
            data = response(context)
            data.pop('report_sections')
            data['report_markdown'] = '# Report without author blocks'
            original = copy.deepcopy(data)
            with self.subTest(mode=mode):
                with self.assertRaises(ContractError) as caught: validate_result('study', data, context, mode)
                self.assertEqual(caught.exception.failure_kind, 'SCHEMA_ERROR')
                self.assertEqual(caught.exception.failure_layer, 'schema')
                self.assertEqual(caught.exception.details['path'], '$')
                self.assertIn('report_sections', caught.exception.details['missing_keys'])
                self.assertIsNone(recoverable_material('study', data, context, mode, {}))
                self.assertFalse(has_ledger_structure('study', data, representation='wire'))
                with self.assertRaises(ContractError): review_context(data, context)
                with self.assertRaises(ContractError): render_stage('study', data)
                self.assertEqual(data, original)

    def test_link_counts_are_claims_not_occurrences(self):
        self.wire['report_sections'][0]['blocks'] *= 3
        with patch('ledger.resolve_evidence', return_value=[{'status': 'RESOLVED'}]):
            saved = prepare_result('study', self.wire, self.context)
        self.assertEqual(len(saved['claims'][0]['document_locators']), 3)
        self.assertEqual(saved['program_checks']['document_links']['matched'], 1)


class EvidenceIDTests(unittest.TestCase):
    def setUp(self):
        self.context = dict(branch='main', source_commit='abc')

    def test_definition_mapping_padding_prefix_long_ids_and_no_prose_replacement(self):
        for stage in ('study', 'review'):
            context = self.context if stage == 'study' else review_context(materialize_study(response(self.context)), self.context)
            for identifier in ('E-1', 'E-01', 'E-001', 'E-0001', stage + ':E-1', stage + ':E-001'):
                wire = response(context)
                wire['evidence'][0]['id'] = identifier
                canonical_id = 'E-' + identifier.split('E-')[1].zfill(3)
                local = identifier.removeprefix(stage + ':')
                wire['claims'][0]['evidence_ids'] = [stage + ':' + local]
                wire['evidence'][0]['quote'] = identifier + '\t source quotation'
                original = copy.deepcopy(wire)
                candidate, changes = normalize_evidence(stage, wire, context)
                self.assertEqual(wire, original)
                self.assertEqual(candidate['evidence'][0]['id'], canonical_id)
                self.assertEqual(candidate['evidence'][0]['quote'], original['evidence'][0]['quote'])
                self.assertEqual(candidate['claims'][0]['evidence_ids'], [stage + ':' + canonical_id])
                validate_result(stage, candidate, context)
                self.assertEqual(normalize_evidence(stage, candidate, context), (candidate, []))

    def test_collisions_and_bad_definitions_are_atomic(self):
        for identifier in ('E-001', 'E-01', 'study:E-001', 'review:E-1', 'e-1', ' E-1', 'E-1 ', 'E_1', 'arbitrary'):
            wire = response(self.context)
            wire['evidence'][0]['id'] = 'E-1'
            wire['evidence'].append(wire['evidence'][0] | {'id': identifier})
            original = copy.deepcopy(wire)
            with self.subTest(identifier=identifier), self.assertRaises(ContractError): normalize_evidence('study', wire, self.context)
            self.assertEqual(wire, original)

    def test_review_updates_findings_but_never_study_or_bare_refs(self):
        ctx = review_context(materialize_study(response(self.context)), self.context)
        original_context = copy.deepcopy(ctx)
        wire = response(ctx)
        wire['evidence'][0]['id'] = 'review:E-1'
        wire['claims'][0]['evidence_ids'] = ['study:E-001', 'review:E-1']
        wire['findings'] = [dict(id='F-001', severity='LOW', type='SCOPE_MISMATCH', claim_ids=['C-001'],
            location='C-001', evidence_ids=['review:E-1'], impact='Synthetic', proposed_correction='Synthetic')]
        candidate, _ = normalize_evidence('review', wire, ctx)
        validate_result('review', candidate, ctx)
        self.assertEqual(candidate['claims'][0]['evidence_ids'], ['study:E-001', 'review:E-001'])
        self.assertEqual(candidate['findings'][0]['evidence_ids'], ['review:E-001'])
        self.assertEqual(ctx, original_context)
        for ref in ('E-1', 'study:E-1', 'foreign:E-1', 'review:E-999'):
            wire['claims'][0]['evidence_ids'] = [ref]
            candidate, _ = normalize_evidence('review', wire, ctx)
            self.assertEqual(candidate['claims'][0]['evidence_ids'], [ref])
            with self.assertRaises(ContractError): validate_result('review', candidate, ctx)
        wire['claims'][0]['evidence_ids'] = ['review:E-1', 'review:E-001']
        candidate, _ = normalize_evidence('review', wire, ctx)
        with self.assertRaises(ContractError): validate_result('review', candidate, ctx)

    def test_identity_and_frozen_target_precede_every_edit(self):
        for stage in ('study', 'review'):
            ctx = self.context if stage == 'study' else review_context(materialize_study(response(self.context)), self.context)
            wire = response(ctx); wire['evidence'][0]['id'] = 'E-1'
            wire['source_commit'] = 'wrong'
            with self.assertRaises(ContractError): normalize_evidence(stage, wire, ctx)
            if stage == 'review':
                wire['source_commit'] = 'abc'
                wire['target']['document_sha256'] = 'wrong'
                with self.assertRaises(ContractError): normalize_evidence(stage, wire, ctx)
                wire['target'] = copy.deepcopy(ctx['review_target'])
                ctx['claim_registry'][0]['statement'] = 'changed'
                with self.assertRaises(ContractError): normalize_evidence(stage, wire, ctx)

    def test_repair_compares_only_original_wire_content(self):
        from structured_output import validate_repair
        original = response(self.context) | {'extra': True}
        corrected = {k: v for k, v in original.items() if k != 'extra'}
        validate_repair(original, corrected, SCHEMAS['study'])
        for mutation in ('blocks', 'claims', 'status', 'count'):
            changed = copy.deepcopy(corrected)
            if mutation == 'blocks': changed['report_sections'][0]['blocks'][0]['markdown'] = 'new facts'
            if mutation == 'claims': changed['claims'][0]['statement'] = 'new facts'
            if mutation == 'status': changed['completion_status'] = 'PARTIAL'
            if mutation == 'count': changed['claims'] = []
            with self.assertRaises(ContractError): validate_repair(original, changed, SCHEMAS['study'])


if __name__ == '__main__':
    unittest.main()
