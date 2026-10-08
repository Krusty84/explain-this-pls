#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import copy
import json
import unittest

from src.analysis.study_shards import OBSERVATION_FIELDS
from src.model.model_boundary import BindingRegistry
from src.reports.document_rendering import materialize_study
from fixtures.ledger_response import response
from src.analysis.ledger import review_context, verify_review_context
from src.model.model_context import project_model_context, input_measurements


class ModelContextTests(unittest.TestCase):
    def synthesis_context(self, mode):
        identity = ({'source_directory': '/source', 'source_fingerprint': 'f' * 64} if mode == 'folder'
                    else {'branch': 'main', 'source_commit': 'a' * 40})
        context = dict(identity, stage='study', prompt_variant='synthesis', validated_shards=[],
                       synthesis_evidence=[], synthesis_claims=[], synthesis_coverage=[], shard_id_mappings=[],
                       coverage_plan={'areas': [], 'exclusions': []})
        for i in range(2):
            sid, area = f'R-{i + 1:03d}', f'S-{i + 1:03d}'
            eids = [f'E-{2 * i + n:03d}' for n in (1, 2)]
            cids = [f'C-{3 * i + n:03d}' for n in (1, 2, 3)]
            refs = ['study:' + eid for eid in eids]
            limitation = 'Runtime queue delivery was not checked.'
            evidence = [dict(id=eid, source_id='source-001', path='src/queue.py',
                             start_line=1, end_line=1, quote='# очередь') for eid in eids]
            claims = [dict(id=cid, statement='Uses the shared queue.', scope=area,
                           epistemic_kind='FACT', evidence_ids=refs[:n], uncertainty='')
                      for n, cid in enumerate(cids[:2], 1)]
            claims.append(dict(id=cids[2], statement='Delivery guarantees are unknown.', scope=area,
                               epistemic_kind='UNKNOWN', evidence_ids=[], uncertainty=limitation))
            coverage = [dict(area_id=area, status='PARTIALLY_INSPECTED' if i else 'INSPECTED',
                             evidence_ids=refs, limitation=limitation if i else '')]
            shard = dict(identity, task='architecture_study_shard', shard_id=sid,
                         assigned_subsystem_ids=[area], completion_status='PARTIAL' if i else 'COMPLETE',
                         evidence=evidence, claims=claims, coverage=coverage, limitations=[limitation])
            for field in OBSERVATION_FIELDS:
                shard[field] = [dict(description='Shared queue: очередь.', claim_ids=cids[:])]
            shard['relationships'][0].update(subsystem_id=area, related_path='src/worker.py')
            context['validated_shards'].append(shard)
            for field in ('evidence', 'claims', 'coverage'):
                context['synthesis_' + field].extend(copy.deepcopy(shard[field]))
            context['shard_id_mappings'].append(dict(shard_id=sid,
                evidence_ids={f'E-{n:03d}': eid for n, eid in enumerate(eids, 1)},
                claim_ids={f'C-{n:03d}': cid for n, cid in enumerate(cids, 1)}))
            context['coverage_plan']['areas'].append(dict(id=area, paths=['src' if i == 0 else 'src/queue.py']))
        return context

    def test_synthesis_preserves_registries_observations_and_global_links_once(self):
        for mode in ('git', 'folder'):
            with self.subTest(mode=mode):
                context = self.synthesis_context(mode)
                original = copy.deepcopy(context)
                registry = BindingRegistry()
                binding = registry.bind('study', context, mode)
                projected = binding.project()
                # The catalog projection retains the old synthesis input shape.
                before = registry.bind('catalog', context, mode).project()
                expected = copy.deepcopy(before)
                expected.pop('shard_id_mappings')
                for shard in expected['validated_shards']:
                    for field in ('evidence', 'claims', 'coverage'):
                        shard.pop(field)
                self.assertEqual(projected, expected)
                self.assertEqual(project_model_context('study', projected), projected)
                for field in ('evidence', 'claims', 'coverage'):
                    self.assertEqual(projected['synthesis_' + field], original['synthesis_' + field])
                claims = {c['id']: c for c in projected['synthesis_claims']}
                evidence = {'study:' + e['id'] for e in projected['synthesis_evidence']}
                self.assertEqual((len(claims), len(evidence)), (6, 4))
                for shard in projected['validated_shards']:
                    for field in OBSERVATION_FIELDS:
                        for observation in shard[field]:
                            self.assertTrue(set(observation['claim_ids']) <= claims.keys())
                            self.assertTrue(all(claims[c]['scope'] in shard['assigned_subsystem_ids']
                                                for c in observation['claim_ids']))
                for record in projected['synthesis_claims'] + projected['synthesis_coverage']:
                    self.assertTrue(set(record['evidence_ids']) <= evidence)
                self.assertLess(len(json.dumps(projected, ensure_ascii=False).encode('utf-8')),
                                len(json.dumps(before, ensure_ascii=False).encode('utf-8')))
                self.assertEqual(context, original)
                for output in (projected, project_model_context('study', context)):
                    output['validated_shards'][0]['relationships'][0]['claim_ids'].clear()
                    output['synthesis_evidence'][0]['quote'] = 'changed'
                    output['synthesis_claims'][0]['evidence_ids'].clear()
                    output['synthesis_coverage'][1]['limitation'] = 'changed'
                    self.assertEqual(context, original)
                binding.assert_unchanged(context)

    def test_non_synthesis_projections_keep_shard_registries_and_mappings(self):
        for stage, variant in (('study', None), ('study', 'revise'), ('catalog', 'synthesis'),
                               ('review', 'synthesis'), ('compare', 'synthesis'), ('study-shard', 'synthesis')):
            with self.subTest(stage=stage, variant=variant):
                context = self.synthesis_context('git')
                context['analysis_shard'] = {'subsystem_ids': ['S-001', 'S-002']}
                if variant is None:
                    context.pop('prompt_variant')
                else:
                    context['prompt_variant'] = variant
                projected = project_model_context(stage, context)
                self.assertEqual(projected['validated_shards'], context['validated_shards'])
                self.assertEqual(projected['shard_id_mappings'], context['shard_id_mappings'])

    def test_sizes_are_exact_bytes_and_characters_without_token_claims(self):
        measured = input_measurements('АБ', '{"x":1}', '{}', 'АБ test')
        self.assertEqual(measured['template'], {'characters': 2, 'utf8_bytes': 4})
        self.assertEqual(measured['projected_context']['utf8_bytes'], 7)
        self.assertEqual(measured['schema']['utf8_bytes'], 2)
        self.assertIsNone(measured['tokens'])
        self.assertTrue(measured['text_schema_copy'])
        correction = input_measurements('unused template', '{}', '{}', 'correction', correction=True)
        self.assertEqual(correction['template']['utf8_bytes'], 0)
        self.assertFalse(correction['template_applied'])

    def test_twenty_claim_table_is_sent_once_without_changing_frozen_context(self):
        context = {'branch': 'main', 'source_commit': 'abc'}
        wire = response(context)
        wire.setdefault('coverage', [])
        wire['claims'] = [wire['claims'][0] | {'id': f'C-{n:03}', 'statement': f'Assertion {n}'}
                          for n in range(1, 21)]
        table = '| Key | Value |\n| --- | --- |\n' + ''.join(f'| {n} | unique row {n} |\n' for n in range(1, 21))
        wire['report_sections'][0]['blocks'] = [{'markdown': table, 'claim_ids': [c['id'] for c in wire['claims']]}]
        context = review_context(materialize_study(wire), context)
        original = copy.deepcopy(context)
        projected = project_model_context('review', context)
        payload = json.dumps(projected, ensure_ascii=False)
        self.assertEqual(payload.count('unique row 20'), 1)
        self.assertEqual(payload.count('Assertion 20'), 1)
        self.assertEqual(len(projected['claim_registry']), 20)
        self.assertNotIn('claims', projected['architecture_document'])
        for claim in projected['claim_registry']:
            self.assertNotIn('quote', claim['document_locators'][0])
        for key in ('block_map', 'materialization_provenance', 'normalization_provenance'):
            self.assertNotIn(key, projected['architecture_document'])
        self.assertEqual(context, original)
        verify_review_context(context)
        projected['claim_registry'][0]['statement'] = 'edited'
        self.assertEqual(context, original)

    def test_compare_and_revision_retain_content_once(self):
        pair = {'study': {'report_markdown': 'document', 'claims': [{'id': 'C-001',
            'statement': 'statement', 'document_locators': [{'quote': 'document', 'start_line': 1, 'end_line': 1}]}]},
            'review': {'report_markdown': 'review', 'claim_registry': [{'statement': 'statement'}],
                       'findings': [{'impact': 'issue'}], 'limitations': ['limit']}}
        context = {'branches': [pair], 'previous_revision': pair}
        original = copy.deepcopy(context)
        projected = project_model_context('compare', context)
        for item in (projected['branches'][0], projected['previous_revision']):
            payload = json.dumps(item)
            self.assertEqual(payload.count('"document"'), 1)
            self.assertNotIn('claim_registry', item['review'])
            self.assertEqual(item['review']['findings'], pair['review']['findings'])
        self.assertEqual(context, original)


    def test_coverage_projection_uses_selectors_without_expanded_inventory(self):
        plan = {'areas': [{'id': 'S-001', 'paths': ['src'], 'entry_paths': ['src/app.py', 'src/link'],
                           'file_paths': ['src/app.py']}], 'exclusions': [], 'plan_sha256': 'frozen'}
        context = {'coverage_plan': plan, 'review_plan': {'coverage_plan': plan, 'plan_sha256': 'review'}}
        before = copy.deepcopy(context)
        projected = project_model_context('review', context)
        self.assertNotIn('coverage_plan', projected['review_plan'])
        self.assertEqual(projected['coverage_plan']['areas'][0],
                         {'id': 'S-001', 'paths': ['src'], 'entry_count': 2, 'file_count': 1})
        self.assertEqual(project_model_context('review', projected), projected)
        self.assertEqual(context, before)
