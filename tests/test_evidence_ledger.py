# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline contract/security regressions; locator success is not semantic support."""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.contracts.contracts import ContractError, validate_result, SCHEMAS, validate_schema, accepted
from src.analysis.evidence import resolve_evidence, SourceChanged, MAX_FILE_BYTES
from src.analysis.ledger import freeze_plan, prepare_result, review_context, CONTRACT_ID, ARTIFACT_FORMAT
from src.reports.presentation import render_stage, label
from src.reports.final_report import render_final_report, recoverable_material
from src.reports.document_rendering import materialize_study, validate_materialized
from fixtures.ledger_response import sections


def study(context):
    return dict(task='architecture_documentation',
        branch=context['branch'], source_commit=context['source_commit'],
        completion_status='COMPLETE', limitations=[], report_sections=sections('A claim.\n'), coverage=[],
        evidence=[dict(id='E-001', source_id='source-001', path='app.py', start_line=1, end_line=1, quote='')],
        claims=[dict(id='C-001', statement='A claim.', scope='Static fixture.', epistemic_kind='FACT',
            evidence_ids=['study:E-001'], uncertainty='')])


def review(context):
    return dict(task='architecture_review',
        branch=context['branch'], source_commit=context['source_commit'], target=context['review_target'],
        completion_status='COMPLETE', limitations=[], report_markdown='Agent assessment.',
        evidence=[dict(id='E-001', source_id='source-001', path='app.py', start_line=1, end_line=1, quote='')],
        claims=[dict(id=c['id'], outcome='SUPPORTED', evidence_ids=['review:E-001'], limitation='')
                for c in context['claim_registry']], findings=[], prior_findings=[],
        omission_search=[dict(area_id=a['id'], status='INSPECTED', limitation='', finding_ids=[])
                         for a in context['review_plan']['omission_areas']])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'app.py').write_bytes(b'print(1)\n')
        self.context = dict(source_mode='git', branch='main', source_commit='abc',
                            repository=str(self.root), priority_scenarios=[])

    def tearDown(self):
        self.tmp.cleanup()

    def resolved(self, pointer=None):
        return resolve_evidence('study', [pointer or study(self.context)['evidence'][0]], self.context)[0]

    def test_real_bytes_and_no_semantic_promotion(self):
        result = self.resolved()
        self.assertEqual(result['status'], 'RESOLVED')
        self.assertEqual(result['file_sha256'], hashlib.sha256(b'print(1)\n').hexdigest())
        self.assertNotIn('outcome', result)
        self.assertNotIn('fragment', result)
        # print(1) is a real fragment, but it does not establish this architectural
        # assertion. We test provenance separation, not automated truth detection.
        data = study(self.context)
        data['claims'][0]['statement'] = 'All writes use distributed transactions.'
        data['report_sections'][0]['blocks'][0]['markdown'] = data['claims'][0]['statement'] + '\n'
        validate_result('study', data, self.context)
        saved = prepare_result('study', data, self.context)
        self.assertEqual(saved['program_checks']['evidence'][0]['status'], 'RESOLVED')
        self.assertNotIn('outcome', saved['claims'][0])
        self.assertFalse(accepted({'study': saved, 'study_invocation': {'publication_complete': True}}))

    def test_bad_paths_numbers_quote_and_missing_sources(self):
        for field, value in [('path', ''), ('path', ' '), ('path', '../outside'), ('path', '/etc/passwd'),
                             ('path', 'C:/secret'), ('path', 'a\x00b'), ('path', 'missing'),
                             ('source_id', 'unknown'), ('start_line', True), ('start_line', -1),
                             ('end_line', 0), ('end_line', 100), ('quote', 'invented'), ('quote', ['print(1)'])]:
            with self.subTest(field=field, value=value):
                pointer = study(self.context)['evidence'][0] | {field: value}
                self.assertNotEqual(self.resolved(pointer)['status'], 'RESOLVED')

    def test_source_quotes_match_exact_text_within_selected_lines(self):
        fragment = '    first = "Привет"\n    second = 2\n    finish()\n'
        for newline in ('\n', '\r\n'):
            blob = ('before\n' + fragment + 'after\n').replace('\n', newline).encode()
            (self.root / 'app.py').write_bytes(blob)
            for quote in ('', fragment, fragment[:-1], 'Привет', 'second = 2',
                          '    second = 2\n    finish()', fragment.replace('\n', '\r\n')):
                with self.subTest(newline=newline, quote=quote):
                    pointer = study(self.context)['evidence'][0] | dict(start_line=2, end_line=4, quote=quote)
                    result = self.resolved(pointer)
                    self.assertEqual(result['status'], 'RESOLVED')
                    self.assertEqual(result['file_sha256'], hashlib.sha256(blob).hexdigest())
                    self.assertEqual(pointer['quote'], quote)
            for quote in ('before', 'after', 'привет', 'second =  2',
                          '\tsecond = 2', 'second = 2\nfinish()', 'second = 2\r    finish()'):
                with self.subTest(newline=newline, quote=quote):
                    pointer = study(self.context)['evidence'][0] | dict(start_line=2, end_line=4, quote=quote)
                    self.assertEqual(self.resolved(pointer)['status'], 'QUOTE_MISMATCH')

    def test_symlink_chain_and_component_swap_never_reads_outside(self):
        (self.root / 'link').symlink_to('/etc', target_is_directory=True)
        self.assertNotEqual(self.resolved(study(self.context)['evidence'][0] | {'path': 'link/passwd'})['status'], 'RESOLVED')
        directory = self.root / 'dir'; directory.mkdir(); (directory / 'app.py').write_text('safe')
        original = os.open
        def replace_component(path, flags, *args, **kwargs):
            if path == 'dir':
                directory.rename(self.root / 'old')
                directory.symlink_to('/etc', target_is_directory=True)
            return original(path, flags, *args, **kwargs)
        with patch('src.analysis.evidence.os.open', side_effect=replace_component):
            self.assertNotEqual(self.resolved(study(self.context)['evidence'][0] | {'path': 'dir/passwd'})['status'], 'RESOLVED')
        from explain import Folder, AuditError
        # Inventory must also retain canonical authorization when a root ancestor
        # is replaced after construction; it must not resolve the new symlink.
        ancestor = self.root / 'ancestor'; ancestor.mkdir()
        source = ancestor / 'source'; source.mkdir(); (source / 'app.py').write_text('safe')
        scanner = Folder(source)
        ancestor.rename(self.root / 'original-ancestor')
        ancestor.symlink_to('/etc', target_is_directory=True)
        with self.assertRaises(AuditError): scanner.snapshot()

    def test_decoding_and_limits(self):
        for blob in ['Привет\r\nмир'.encode(), 'Привет\nмир'.encode(), b'last']:
            (self.root / 'app.py').write_bytes(blob)
            self.assertEqual(self.resolved()['status'], 'RESOLVED')
            self.assertEqual(self.resolved()['file_sha256'], hashlib.sha256(blob).hexdigest())
        (self.root / 'app.py').write_bytes(b'\xff')
        self.assertEqual(self.resolved()['status'], 'DECODE_ERROR')
        (self.root / 'app.py').write_bytes(b'x' * (MAX_FILE_BYTES + 1))
        self.assertEqual(self.resolved()['status'], 'LIMIT_EXCEEDED')

    def prepared(self):
        data = study(self.context)
        validate_result('study', data, self.context)
        saved = prepare_result('study', data, self.context)
        return saved, review_context(saved, self.context)

    def test_missing_claim_cannot_disappear_from_denominator(self):
        data = study(self.context)
        data['claims'].append(copy.deepcopy(data['claims'][0]) | {'id': 'C-002'})
        data['report_sections'][0]['blocks'][0]['claim_ids'].append('C-002')
        saved = prepare_result('study', data, self.context)
        ctx = review_context(saved, self.context)
        response = review(ctx); response['claims'].pop()
        validate_result('review', response, ctx)
        result = prepare_result('review', response, ctx)
        self.assertEqual(result['program_checks']['registry_coverage']['missing_ids'], ['C-002'])
        self.assertFalse(result['program_checks']['policy_satisfied'])

    def test_target_registry_and_locator_are_exact(self):
        saved, ctx = self.prepared()
        for field in ctx['review_target']:
            response = review(ctx); response['target'][field] = 'wrong'
            with self.assertRaises(ContractError): validate_result('review', response, ctx)
            ctx = review_context(saved, self.context)
        data = materialize_study(study(self.context))
        original_quote = data['claims'][0]['document_locators'][0]['quote']
        for quote in ('different', original_quote[:-1]):
            data['claims'][0]['document_locators'][0]['quote'] = quote
            with self.assertRaises(ContractError): validate_materialized(data)
        for key in ('statement', 'scope'):
            changed = copy.deepcopy(saved); changed['claims'][0][key] += ' changed'
            with self.assertRaises(ContractError): freeze_plan(changed, self.context)

    def test_computed_fields_duplicates_and_wrong_types_rejected(self):
        saved, ctx = self.prepared()
        for edit in ('duplicate', 'unknown', 'computed', 'blank'):
            response = review(ctx)
            if edit == 'duplicate': response['claims'] *= 2
            if edit == 'unknown': response['claims'][0]['id'] = 'C-999'
            if edit == 'computed': response['program_checks'] = {'policy_satisfied': True}
            if edit == 'blank': response['report_markdown'] = ' \n'
            with self.assertRaises(ContractError): validate_result('review', response, ctx)
        with self.assertRaises(ContractError):
            validate_schema(True, {'type': 'integer', 'minimum': 1})

    def test_positive_example_and_caveat_rules(self):
        saved, ctx = self.prepared()
        response = review(ctx); validate_result('review', response, ctx)
        self.assertTrue(prepare_result('review', response, ctx)['program_checks']['policy_satisfied'])
        for kind in ('HYPOTHESIS', 'UNKNOWN'):
            data = study(self.context)
            data['claims'][0].update(epistemic_kind=kind, evidence_ids=[], uncertainty='Needs runtime observation.')
            saved = prepare_result('study', data, self.context); ctx = review_context(saved, self.context)
            response = review(ctx); response['claims'][0].update(outcome='CAVEAT_ACCEPTABLE', evidence_ids=[])
            validate_result('review', response, ctx)
            self.assertTrue(prepare_result('review', response, ctx)['program_checks']['policy_satisfied'])
            response['claims'][0]['outcome'] = 'SUPPORTED'
            with self.assertRaises(ContractError): validate_result('review', response, ctx)

    def test_empty_unchecked_and_omission_gaps_never_pass(self):
        saved, ctx = self.prepared()
        for mutation in ('unchecked', 'missing_area', 'partial_area', 'empty'):
            response = review(ctx)
            if mutation == 'unchecked':
                response['claims'][0].update(outcome='NOT_CHECKED', limitation='Access unavailable.')
            elif mutation == 'missing_area':
                response['omission_search'].pop()
            elif mutation == 'partial_area':
                response['omission_search'][0].update(status='PARTIALLY_INSPECTED', limitation='Search budget.')
            else:
                doc = study(self.context); doc['claims'] = []
                doc['report_sections'][0]['blocks'][0]['claim_ids'] = []
                ctx = review_context(prepare_result('study', doc, self.context), self.context)
                response = review(ctx)
            validate_result('review', response, ctx)
            result = prepare_result('review', response, ctx)
            self.assertEqual(result['verdict'], 'INCONCLUSIVE')
            self.assertFalse(result['program_checks']['policy_satisfied'])
            if mutation == 'empty': self.assertIsNone(result['program_checks']['registry_coverage']['fraction'])

    def test_material_fact_and_canonical_reverse_links(self):
        _, ctx = self.prepared()
        response = review(ctx)
        response['claims'][0].update(outcome='UNVERIFIABLE', limitation='Fragment does not establish the claim.')
        with self.assertRaises(ContractError): validate_result('review', response, ctx)
        response['findings'] = [dict(id='F-001', severity='MEDIUM', type='UNSUPPORTED_ASSERTION',
            claim_ids=['C-001'], location='Scope', evidence_ids=['review:E-001'],
            impact='Overstated claim.', proposed_correction='Qualify the assertion.')]
        validate_result('review', response, ctx)
        result = prepare_result('review', response, ctx)
        self.assertEqual(result['verdict'], 'CHANGES_REQUIRED')
        self.assertEqual(result['program_checks']['finding_ids_by_claim'], {'C-001': ['F-001']})
        response['findings'][0]['claim_ids'] = ['C-999']
        with self.assertRaises(ContractError): validate_result('review', response, ctx)

    def test_all_evidence_limits_are_explicit(self):
        pointer = study(self.context)['evidence'][0]
        (self.root / 'app.py').write_bytes(b'x\n' * 201)
        self.assertEqual(self.resolved(pointer | {'end_line': 201})['status'], 'LIMIT_EXCEEDED')
        self.assertEqual(self.resolved(pointer | {'quote': 'x' * 17000})['status'], 'LIMIT_EXCEEDED')
        (self.root / 'app.py').write_bytes(b'x' * 65537)
        self.assertEqual(self.resolved()['status'], 'LIMIT_EXCEEDED')
        (self.root / 'app.py').write_bytes(b'1234\n')
        with patch('src.analysis.evidence.MAX_TOTAL_BYTES', 5):
            result = resolve_evidence('study', [pointer, pointer | {'id': 'E-002'}], self.context)
        self.assertEqual([e['status'] for e in result], ['RESOLVED', 'RESOLVED'])
        with patch('src.analysis.evidence.MAX_EVIDENCE', 1):
            result = resolve_evidence('study', [pointer, pointer | {'id': 'E-002'}], self.context)
        self.assertEqual(result[-1]['status'], 'LIMIT_EXCEEDED')

    def test_unresolved_locator_never_passes_study_or_review_policy(self):
        for change in ({'path': 'invented.py'}, {'source_id': 'unknown'}, {'end_line': 100}, {'quote': 'wrong'}):
            data = study(self.context)
            data['evidence'][0].update(change)
            validate_result('study', data, self.context)
            saved = prepare_result('study', data, self.context)
            self.assertFalse(saved['program_checks']['policy_satisfied'])
            ctx = review_context(saved, self.context)
            response = review(ctx)
            validate_result('review', response, ctx)
            self.assertFalse(prepare_result('review', response, ctx)['program_checks']['policy_satisfied'])

    def test_access_error_and_mutation_are_distinct(self):
        with patch('src.analysis.evidence.os.open', side_effect=PermissionError(13, 'secret-path')):
            result = self.resolved()
        self.assertEqual(result['status'], 'ACCESS_DENIED')
        self.assertNotIn('secret-path', str(result))
        original = os.read
        def mutate(fd, size):
            data = original(fd, size)
            if data: (self.root / 'app.py').write_text('changed')
            return data
        with patch('src.analysis.evidence.os.read', side_effect=mutate), self.assertRaises(SourceChanged):
            self.resolved()
        with self.assertRaises(SourceChanged):
            resolve_evidence('study', study(self.context)['evidence'], self.context, {'app.py': 'wrong-snapshot-hash'})

    def test_nested_source_ids_pin_submodule_identity(self):
        nested = self.root / 'vendor' / 'child'; nested.mkdir(parents=True)
        (nested / 'app.py').write_text('nested source')
        self.context['submodules'] = [dict(path='vendor/child', expected_commit='nested-sha')]
        pointer = study(self.context)['evidence'][0]
        self.assertEqual(self.resolved(pointer | {'path': 'vendor/child/app.py'})['status'], 'SOURCE_SCOPE_MISMATCH')
        result = self.resolved(pointer | {'source_id': 'source-002'})
        self.assertEqual(result['status'], 'RESOLVED')
        self.assertEqual(result['source_identity']['commit'], 'nested-sha')
        _, ctx = self.prepared()
        old = review(ctx)
        self.context['submodules'][0]['expected_commit'] = 'different'
        newctx = review_context(prepare_result('study', study(self.context), self.context), self.context)
        with self.assertRaises(ContractError): validate_result('review', old, newctx)

    def pair(self, branch):
        context = self.context | {'branch': branch}
        doc = prepare_result('study', study(context), context)
        ctx = review_context(doc, context)
        rev = prepare_result('review', review(ctx), ctx)
        meta = dict(publication_complete=True, contract_id=CONTRACT_ID, artifact_format=ARTIFACT_FORMAT)
        return dict(branch=branch, source_commit='abc', study=doc, review=rev,
                    study_invocation=dict(meta), review_invocation=dict(meta))

    def test_comparison_resolves_both_sides_and_requires_accepted_inputs(self):
        from fixtures.ledger_response import response
        baseline, other = self.pair('main'), self.pair('other')
        self.assertTrue(accepted(baseline)); self.assertTrue(accepted(other))
        ctx = dict(baseline_branch='main', baseline_commit='abc', requested_branches=['main', 'other'], branches=[baseline, other])
        data = response(ctx)
        def ref(item):
            plan = item['study']['review_plan']
            return dict(branch=item['branch'], revision_id='001', artifact='study', claim_id='C-001',
                        document_sha256=plan['document_sha256'], registry_sha256=plan['registry_sha256'])
        data['differences'] = [dict(id='D-001', branch='other', category='state', classification='CONFIRMED_DIFFERENCE',
            baseline_statement='A', branch_statement='B', evidence_refs=[ref(baseline), ref(other)], explanation='Agent contrast.')]
        with patch('src.analysis.evidence.read_confined', side_effect=AssertionError('Compare must not read source')):
            validate_result('compare', data, ctx)
            self.assertTrue(prepare_result('compare', data, ctx)['program_checks']['policy_satisfied'])
        data['differences'][0]['evidence_refs'] = [ref(baseline), ref(baseline)]
        with self.assertRaises(ContractError): validate_result('compare', data, ctx)
        data['differences'][0]['evidence_refs'] = [ref(baseline), ref(other)]
        data['differences'][0]['evidence_refs'][1]['revision_id'] = '002'
        with self.assertRaises(ContractError): validate_result('compare', data, ctx)
        data['differences'][0]['evidence_refs'] = [ref(baseline), ref(other)]
        other['study_invocation']['publication_complete'] = False
        data.update(completion_status='PARTIAL', limitations=['Input incomplete.'], unresolved_branches=['other'])
        with self.assertRaises(ContractError): validate_result('compare', data, ctx)
        data['differences'][0]['classification'] = 'REPORTED_UNVERIFIED'
        validate_result('compare', data, ctx)
        other['study_invocation']['publication_complete'] = True
        data.update(completion_status='COMPLETE', limitations=[], unresolved_branches=[])
        validate_result('compare', data, ctx)
        self.assertFalse(prepare_result('compare', data, ctx)['program_checks']['policy_satisfied'])
        context = self.context | {'branch': 'other'}
        hypothetical = study(context)
        hypothetical['claims'][0].update(epistemic_kind='HYPOTHESIS', uncertainty='Needs runtime evidence.')
        other['study'] = prepare_result('study', hypothetical, context)
        rc = review_context(other['study'], context)
        assessment = review(rc); assessment['claims'][0]['outcome'] = 'CAVEAT_ACCEPTABLE'
        other['review'] = prepare_result('review', assessment, rc)
        self.assertTrue(accepted(other))
        data['differences'][0].update(classification='CONFIRMED_DIFFERENCE', evidence_refs=[ref(baseline), ref(other)])
        with self.assertRaises(ContractError): validate_result('compare', data, ctx)

    def test_replay_render_labels_and_reject_unsupported_formats(self):
        pair = self.pair('main')
        for language in ('Russian', 'English'):
            a = render_stage('review', pair['review'], language)
            self.assertEqual(a, render_stage('review', copy.deepcopy(pair['review']), language))
            self.assertIn(label('SUPPORTED', language), a)
            self.assertIn(label('PASS', language), a)
            self.assertNotIn('print(1)', a)
        unsupported = {'completion_status': 'COMPLETE', 'report_markdown': '# Unvalidated PASS', 'verdict': 'PASS'}
        self.assertFalse(accepted({'study': unsupported, 'review': unsupported}))
        with self.assertRaises(ContractError):
            render_final_report({'status': 'COMPLETE', 'study': unsupported, 'review': unsupported}, {'path': '/source'}, 'folder', 'English')
        malformed = copy.deepcopy(pair)
        malformed['study'].pop('materialization_provenance')
        malformed['study'].pop('block_map')
        for records in (malformed['study']['claims'], malformed['review']['claim_registry']):
            for claim in records:
                claim['document_locator'] = claim.pop('document_locators')[0]
        original = copy.deepcopy(malformed)
        for stage in ('study', 'review'):
            with self.assertRaises(ContractError): render_stage(stage, malformed[stage], 'English')
        with self.assertRaises(ContractError): review_context(malformed['study'], self.context)
        self.assertFalse(accepted(malformed))
        self.assertEqual(malformed, original)
        pair['study']['claims'][0]['scope'] += ' modified'
        self.assertFalse(accepted(pair))

    def test_local_subset_enforces_bounds_and_no_boolean_integer(self):
        for data, spec in [('', {'type': 'string', 'minLength': 1}),
                           ([], {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}}),
                           (-1, {'type': 'integer', 'minimum': 1}), (True, {'type': 'integer'})]:
            with self.assertRaises(ContractError): validate_schema(data, spec)
        for field in ('start_line', 'end_line'):
            data = study(self.context); data['evidence'][0][field] = True
            with self.assertRaises(ContractError): validate_result('study', data, self.context)
