# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Citation-only continuation never changes strict evidence or acceptance."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.analysis.coverage_plan import build_coverage_plan
from src.analysis.evidence import sha
from src.analysis.ledger import prepare_result, review_context, continuation_allowed
from src.analysis.study_shards import OBSERVATION_FIELDS, require_shard_policy, synthesis_inputs
from src.contracts.contracts import (CONTRACT_ID, ARTIFACT_FORMAT, ContractError, accepted,
    processing_satisfied, workflow_satisfied, validate_result, validate_schema)
from src.contracts.saved_contracts import SAVED_SCHEMAS
from src.model.model_context import project_model_context
from src.model.structured_output import required_unresolved
from test_evidence_ledger import study, review


class CitationPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.joinpath('app.py').write_text('print(1)\n')
        self.context = self.make_context()

    def make_context(self, branch='main', policy='compromise'):
        context = dict(source_mode='git', branch=branch, source_commit='abc', repository=str(self.root),
                       priority_scenarios=[], result_policy=policy)
        context['_inventory'] = dict(entries=[dict(path='.', type='directory'),
            dict(path='app.py', type='file', size=9, sha256=sha(b'print(1)\n'))])
        catalog = dict(task='architecture_catalog', branch=branch, source_commit='abc',
            completion_status='COMPLETE', limitations=[], exclusions=[],
            subsystems=[dict(id='S-001', name='App', purpose='Entry.', paths=['app.py'])])
        context['coverage_plan'] = build_coverage_plan(catalog, context['_inventory'], context)
        context['analysis_shard'] = dict(id='R-001', subsystem_ids=['S-001'], primary_file_paths=['app.py'])
        return context

    def wire(self, context=None, *, mismatch=True, shard=False):
        context = context or self.context
        data = study(context)
        data['evidence'][0]['quote'] = 'private invented quote' if mismatch else 'print(1)'
        data['coverage'] = [dict(area_id='S-001', status='INSPECTED',
                                 evidence_ids=['study:E-001'], limitation='')]
        if shard:
            data.pop('report_sections')
            data.update(task='architecture_study_shard', shard_id='R-001', assigned_subsystem_ids=['S-001'],
                        **{field: [] for field in OBSERVATION_FIELDS})
        return data

    def saved(self, context=None, **options):
        context = context or self.context
        data = self.wire(context, **options)
        return prepare_result('study-shard' if options.get('shard') else 'study', data, context)

    def item(self, doc):
        meta = dict(contract_id=CONTRACT_ID, artifact_format=ARTIFACT_FORMAT, publication_complete=True)
        return dict(branch=doc['branch'], study=doc, study_invocation=meta, review_enabled=False)

    def test_compromise_retains_mismatch_and_unsupported_coverage(self):
        for shard in (False, True):
            with self.subTest(shard=shard):
                data = self.saved(shard=shard)
                checks = data['program_checks']
                self.assertFalse(checks['policy_satisfied'])
                self.assertTrue(continuation_allowed(checks))
                self.assertEqual(checks['evidence'][0]['status'], 'QUOTE_MISMATCH')
                self.assertEqual(checks['coverage']['unsupported_ids'], ['S-001'])
                warning = checks['citation_warnings'][0]
                self.assertEqual((warning['evidence_id'], warning['claim_ids'], warning['action']),
                                 ('study:E-001', ['C-001'], 'UNRESOLVED'))
                self.assertNotIn('private invented quote', json.dumps(checks))
                validate_schema(data, SAVED_SCHEMAS['study-shard' if shard else 'study'])
                if shard:
                    require_shard_policy(data, self.context)
                else:
                    self.assertTrue(processing_satisfied(self.item(data)))
                    self.assertFalse(workflow_satisfied(self.item(data)))
                    self.assertFalse(accepted(self.item(data)))

    def test_absent_policy_and_strict_do_not_allow_continuation(self):
        for policy in (None, 'strict'):
            context = self.context.copy()
            context.pop('result_policy')
            if policy:
                context['result_policy'] = policy
            for shard in (False, True):
                data = self.saved(context, shard=shard)
                self.assertFalse(continuation_allowed(data['program_checks']))
                if shard:
                    with self.assertRaises(ContractError):
                        require_shard_policy(data, context)

    def test_independent_coverage_completion_and_unsafe_failures_still_block(self):
        changes = [lambda d: d.update(coverage=[]),
                   lambda d: d['coverage'][0].update(status='NOT_INSPECTED', limitation='Missing.'),
                   lambda d: d['coverage'][0].update(evidence_ids=[]),
                   lambda d: d.update(completion_status='PARTIAL', limitations=['Missing.']),
                   lambda d: d['evidence'][0].update(path='missing.py'),
                   lambda d: d['evidence'][0].update(source_id='unknown')]
        for shard in (False, True):
            for change in changes:
                with self.subTest(shard=shard, change=change):
                    data = self.wire(shard=shard)
                    change(data)
                    saved = prepare_result('study-shard' if shard else 'study', data, self.context)
                    self.assertFalse(continuation_allowed(saved['program_checks']))

    def test_clean_outputs_keep_old_shape_and_stale_flags_cannot_relax_failures(self):
        saved = self.saved(mismatch=False)
        checks = saved['program_checks']
        self.assertNotIn('continuation_allowed', checks)
        self.assertNotIn('citation_warnings', checks)
        self.assertTrue(processing_satisfied(self.item(saved)))
        checks.update(policy_satisfied=False, continuation_allowed=True)
        self.assertFalse(processing_satisfied(self.item(saved)))
        checks['citation_warnings'] = self.saved()['program_checks']['citation_warnings']
        self.assertFalse(processing_satisfied(self.item(saved)))

    def test_correction_warning_is_safe_and_does_not_reduce_strict_quality(self):
        data = self.wire(mismatch=False)
        pointer = data['evidence'][0]
        changes = [dict(evidence_index=0, id='E-001', reason='INDENTATION_ONLY',
                        original=pointer | {'quote': 'private original'}, repaired=pointer)]
        saved = prepare_result('study', data, self.context, citation_corrections=changes)
        self.assertTrue(saved['program_checks']['policy_satisfied'])
        warning = saved['program_checks']['citation_warnings'][0]
        self.assertEqual(warning['action'], 'CORRECTED')
        self.assertEqual(warning['original_end_line'], 1)
        self.assertNotIn('quote', json.dumps(warning))
        validate_schema(saved, SAVED_SCHEMAS['study'])

    def test_review_inherits_warning_but_never_becomes_accepted(self):
        doc = self.saved()
        context = review_context(doc, self.context)
        data = review(context)
        data['claims'][0]['evidence_ids'] = ['study:E-001']
        saved = prepare_result('review', data, context)
        self.assertTrue(continuation_allowed(saved['program_checks']))
        self.assertEqual(saved['verdict'], 'INCONCLUSIVE')
        self.assertEqual(saved['program_checks']['citation_warnings'], doc['program_checks']['citation_warnings'])
        item = self.item(doc) | dict(review=saved, review_invocation=self.item(doc)['study_invocation'], review_enabled=True)
        self.assertTrue(processing_satisfied(item))
        self.assertFalse(workflow_satisfied(item))
        item['review_invocation']['publication_complete'] = False
        self.assertFalse(processing_satisfied(item))

    def test_review_independent_failures_are_not_quote_exceptions(self):
        context = review_context(self.saved(), self.context)
        for kind in ('missing_claim', 'not_checked', 'omission', 'finding', 'missing_source'):
            with self.subTest(kind=kind):
                data = review(context)
                if kind == 'missing_claim':
                    data['claims'] = []
                elif kind == 'not_checked':
                    data['claims'][0].update(outcome='NOT_CHECKED', limitation='Not checked.')
                elif kind == 'omission':
                    data['omission_search'].pop()
                elif kind == 'finding':
                    data['findings'] = [dict(id='F-001', severity='HIGH', claim_ids=['C-001'], evidence_ids=['review:E-001'])]
                else:
                    data['evidence'][0]['path'] = 'missing.py'
                saved = prepare_result('review', data, context)
                self.assertFalse(continuation_allowed(saved['program_checks']))

    def test_review_own_mismatch_and_model_warning_projection(self):
        doc = self.saved(mismatch=False)
        context = review_context(doc, self.context)
        data = review(context)
        data['evidence'][0]['quote'] = 'private different quote'
        saved = prepare_result('review', data, context)
        warning = saved['program_checks']['citation_warnings'][0]
        self.assertEqual(warning['evidence_id'], 'review:E-001')
        self.assertTrue(continuation_allowed(saved['program_checks']))
        item = self.item(doc) | dict(review=saved, review_invocation=self.item(doc)['study_invocation'], review_enabled=True)
        self.assertTrue(processing_satisfied(item))
        projected = project_model_context('compare', {'branches': [item]})
        self.assertEqual(projected['branches'][0]['review']['program_checks']['citation_warnings'], [warning])
        self.assertNotIn('file_sha256', projected['branches'][0]['review']['program_checks']['evidence'][0])
        self.assertFalse(accepted(item))
        saved['findings'] = [dict(id='F-001', severity='HIGH', type='FACTUAL_ERROR', claim_ids=['C-001'],
            location='Source.', evidence_ids=['review:E-001'], impact='Independent issue.', proposed_correction='Check.')]
        self.assertFalse(processing_satisfied(item))

    def test_synthesis_rejects_unpublished_unsafe_or_independently_incomplete_shards(self):
        original = self.saved(shard=True)
        meta = dict(backend_result_valid=True, local_validation=True, publication_complete=True,
                    artifact_hashes={}, attempt_hashes={}, binding_hashes={})
        for kind in ('unpublished', 'unsafe', 'missing_coverage', 'missing_warning'):
            with self.subTest(kind=kind):
                saved, invocation = copy.deepcopy(original), meta.copy()
                if kind == 'unpublished':
                    invocation['publication_complete'] = False
                elif kind == 'unsafe':
                    saved['program_checks']['evidence'][0]['status'] = 'UNSAFE_PATH'
                elif kind == 'missing_coverage':
                    saved['program_checks']['coverage']['missing_ids'] = ['S-002']
                else:
                    saved['program_checks'].pop('citation_warnings')
                states = [dict(id='R-001', status='SUCCEEDED', directory='one',
                               **{'study-shard': saved, 'study-shard_invocation': invocation})]
                with self.assertRaises(ContractError):
                    synthesis_inputs({'shards': [self.context['analysis_shard']]}, states)

    def test_synthesis_remaps_warnings_without_source_reads(self):
        saved = self.saved(shard=True)
        planned = self.context['analysis_shard']
        meta = dict(backend_result_valid=True, local_validation=True, publication_complete=True,
                    artifact_hashes={}, attempt_hashes={}, binding_hashes={})
        states = [dict(id='R-001', status='SUCCEEDED', directory='one',
                       **{'study-shard': saved, 'study-shard_invocation': meta})]
        second = copy.deepcopy(states[0])
        second.update(id='R-002', directory='two')
        second['study-shard']['shard_id'] = 'R-002'
        combined = synthesis_inputs({'shards': [planned, planned | {'id': 'R-002'}]}, states + [second])
        self.assertEqual(combined['citation_warnings'][1]['evidence_id'], 'study:E-002')
        self.assertEqual(combined['citation_warnings'][1]['claim_ids'], ['C-002'])
        context = self.context | combined | {'prompt_variant': 'synthesis'}
        data = self.wire()
        for key in ('evidence', 'claims', 'coverage'):
            data[key] = combined['synthesis_' + key]
        data['report_sections'][0]['blocks'][0]['claim_ids'] = ['C-001', 'C-002']
        with patch('src.analysis.ledger.resolve_evidence', side_effect=AssertionError('source read')):
            result = prepare_result('study', data, context)
        self.assertTrue(continuation_allowed(result['program_checks']))
        self.assertEqual(result['program_checks']['evidence'][1]['status'], 'QUOTE_MISMATCH')
        projected = project_model_context('study', context)
        self.assertEqual(projected['citation_warnings'], combined['citation_warnings'])
        self.assertNotIn('_synthesis_resolutions', projected)

    def test_compare_warned_inputs_are_eligible_but_cannot_confirm_differences(self):
        main, other = self.saved(), self.saved(self.make_context('feature'), mismatch=False)
        context = dict(result_policy='compromise', review_enabled=False, baseline_branch='main', baseline_commit='abc',
                       requested_branches=['main', 'feature'], branches=[self.item(main), self.item(other)])
        refs = [dict(branch=doc['branch'], artifact='study', revision_id=doc['revision_id'], claim_id='C-001',
                     document_sha256=doc['review_plan']['document_sha256'],
                     registry_sha256=doc['review_plan']['registry_sha256']) for doc in (main, other)]
        data = dict(task='architecture_comparison', completion_status='COMPLETE', baseline_branch='main',
            baseline_commit='abc', compared_branches=['feature'], unresolved_branches=[], limitations=[],
            report_markdown='Unverified difference.', differences=[dict(id='D-001', branch='feature',
                category='Behavior', baseline_statement='First.', branch_statement='Second.',
                classification='REPORTED_UNVERIFIED', explanation='Reported change.', evidence_refs=refs)])
        self.assertEqual(required_unresolved(context), [])
        self.assertEqual(required_unresolved(context | {'result_policy': 'strict'}), ['main'])
        validate_result('compare', data, context)
        saved = prepare_result('compare', data, context)
        validate_schema(saved, SAVED_SCHEMAS['compare'])
        self.assertTrue(continuation_allowed(saved['program_checks']))
        self.assertFalse(saved['program_checks']['policy_satisfied'])
        data['differences'][0]['classification'] = 'CONFIRMED_DIFFERENCE'
        with self.assertRaises(ContractError):
            validate_result('compare', data, context)
        data['differences'][0]['classification'] = 'INSUFFICIENT_EVIDENCE'
        saved = prepare_result('compare', data, context)
        self.assertFalse(continuation_allowed(saved['program_checks']))

    def test_reviewed_comparison_keeps_warning_references_unverified(self):
        entries = []
        for branch in ('main', 'feature'):
            source = self.make_context(branch)
            doc = self.saved(source, mismatch=branch == 'main')
            context = review_context(doc, source)
            reviewed = prepare_result('review', review(context), context)
            item = self.item(doc)
            entries.append(item | dict(review=reviewed, review_invocation=item['study_invocation'], review_enabled=True))
        context = dict(result_policy='compromise', review_enabled=True, baseline_branch='main', baseline_commit='abc',
                       requested_branches=['main', 'feature'], branches=entries)
        refs = [dict(branch=e['branch'], artifact='review', revision_id=e['study']['revision_id'], claim_id='C-001',
                     document_sha256=e['study']['review_plan']['document_sha256'],
                     registry_sha256=e['study']['review_plan']['registry_sha256']) for e in entries]
        data = dict(task='architecture_comparison', completion_status='COMPLETE', baseline_branch='main',
            baseline_commit='abc', compared_branches=['feature'], unresolved_branches=[], limitations=[],
            report_markdown='Unverified difference.', differences=[dict(id='D-001', branch='feature',
                category='Behavior', baseline_statement='First.', branch_statement='Second.',
                classification='REPORTED_UNVERIFIED', explanation='Reported change.', evidence_refs=refs)])
        self.assertEqual(required_unresolved(context), [])
        self.assertFalse(accepted(entries[0]))
        self.assertTrue(accepted(entries[1]))
        validate_result('compare', data, context)
        saved = prepare_result('compare', data, context)
        self.assertTrue(continuation_allowed(saved['program_checks']))
        self.assertFalse(saved['program_checks']['policy_satisfied'])
        self.assertEqual(len(saved['program_checks']['citation_warnings']), 1)
        data['differences'][0]['classification'] = 'CONFIRMED_DIFFERENCE'
        with self.assertRaisesRegex(ContractError, 'Strong contrast'):
            validate_result('compare', data, context)

    def test_corrected_only_warnings_do_not_relax_unrelated_comparison_policy(self):
        entries = []
        for branch in ('main', 'feature'):
            source = self.make_context(branch)
            wire = self.wire(source, mismatch=False)
            pointer = wire['evidence'][0]
            corrections = [dict(evidence_index=0, id='E-001', reason='INDENTATION_ONLY',
                                original=pointer, repaired=pointer)]
            doc = prepare_result('study', wire, source, citation_corrections=corrections)
            context = review_context(doc, source)
            item = self.item(doc)
            entries.append(item | dict(review=prepare_result('review', review(context), context),
                                       review_invocation=item['study_invocation'], review_enabled=True))
        context = dict(result_policy='compromise', review_enabled=True, baseline_branch='main', baseline_commit='abc',
                       requested_branches=['main', 'feature'], branches=entries)
        refs = [dict(branch=e['branch'], artifact='study', revision_id=e['study']['revision_id'], claim_id='C-001',
                     document_sha256=e['study']['review_plan']['document_sha256'],
                     registry_sha256=e['study']['review_plan']['registry_sha256']) for e in entries]
        data = dict(task='architecture_comparison', completion_status='COMPLETE', baseline_branch='main',
            baseline_commit='abc', compared_branches=['feature'], unresolved_branches=[], limitations=[],
            report_markdown='Unverified difference.', differences=[dict(id='D-001', branch='feature',
                category='Behavior', baseline_statement='First.', branch_statement='Second.',
                classification='REPORTED_UNVERIFIED', explanation='Reported change.', evidence_refs=refs)])
        self.assertTrue(all(accepted(entry) for entry in entries))
        validate_result('compare', data, context)
        checks = prepare_result('compare', data, context)['program_checks']
        self.assertTrue(checks['citation_warnings'])
        self.assertFalse(checks['policy_satisfied'])
        self.assertFalse(checks['continuation_allowed'])
        self.assertFalse(continuation_allowed(checks))


if __name__ == '__main__':
    unittest.main()
