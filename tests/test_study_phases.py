# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Evidence collection and report authoring keep separate wire contracts."""
import copy
import json
from pathlib import Path
import unittest

from src.analysis.study_phases import assemble_study, freeze_collection
from src.analysis.study_normalization import normalize_evidence
from src.contracts.contracts import (ContractError, MODEL_STUDY_PHASE_SCHEMAS,
    MODEL_FOLDER_STUDY_PHASE_SCHEMAS, result_schema, validate_result)
from src.model.model_boundary import BindingRegistry
from fixtures.ledger_response import response


class StudyPhaseTests(unittest.TestCase):
    def parts(self, mode='git'):
        context = ({'branch': 'main', 'source_commit': 'abc'} if mode == 'git' else
                   {'source_directory': '/source', 'source_fingerprint': 'f' * 64})
        study = response(context)
        collector = {key: copy.deepcopy(value) for key, value in study.items() if key != 'report_sections'}
        writer = {key: copy.deepcopy(value) for key, value in study.items() if key not in ('claims', 'evidence')}
        pointer = collector['evidence'][0]
        resolution = {key: pointer[key] for key in ('source_id', 'path', 'start_line', 'end_line')}
        resolution.update(id='study:E-001', source_identity={'mode': mode}, file_sha256='a' * 64,
                          status='RESOLVED')
        return context, collector, writer, [resolution]

    def test_phase_schemas_bind_both_source_modes_without_registry_repetition(self):
        for mode in ('git', 'folder'):
            context, collector, writer, _ = self.parts(mode)
            for phase, value in (('collect', collector), ('write', writer)):
                with self.subTest(mode=mode, phase=phase):
                    current = dict(context, phase=phase)
                    binding = BindingRegistry().bind('study', current, mode)
                    self.assertEqual(binding.schema, result_schema('study', current, mode, model=True))
                    wire = copy.deepcopy(value)
                    if mode == 'folder':
                        wire.pop('source_fingerprint')
                        wire['source_snapshot_id'] = binding.project()['source_snapshot_id']
                    self.assertEqual(binding.expand(wire), value)
                    validate_result('study', value, current, mode)
                    extra = copy.deepcopy(wire)
                    if phase == 'collect':
                        extra['report_sections'] = writer['report_sections']
                    else:
                        extra.update(claims=collector['claims'], evidence=collector['evidence'])
                    with self.assertRaises(ContractError):
                        binding.expand(extra)

    def test_collection_keeps_registry_and_identity_checks_without_sections(self):
        context, collector, _, _ = self.parts()
        collector['evidence'][0]['id'] = 'E-1'
        collector['claims'][0]['evidence_ids'] = ['E-1']
        normalized, changes = normalize_evidence('study', collector, dict(context, phase='collect'))
        self.assertEqual(normalized['claims'][0]['evidence_ids'], ['study:E-001'])
        self.assertTrue(changes)
        validate_result('study', normalized, dict(context, phase='collect'))
        for mutation in ('reference', 'uncertainty', 'identity'):
            invalid = copy.deepcopy(normalized)
            if mutation == 'reference':
                invalid['claims'][0]['evidence_ids'] = ['study:E-999']
            elif mutation == 'uncertainty':
                invalid['claims'][0]['epistemic_kind'] = 'HYPOTHESIS'
            else:
                invalid['source_commit'] = 'changed'
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                validate_result('study', invalid, dict(context, phase='collect'))

    def test_package_is_a_copy_and_checks_match_the_frozen_locators(self):
        _, collector, _, resolutions = self.parts()
        original = copy.deepcopy(collector)
        package = freeze_collection(collector, resolutions)
        collector['claims'][0]['statement'] = 'Changed later.'
        resolutions[0]['status'] = 'QUOTE_MISMATCH'
        self.assertEqual(package['study'], original)
        self.assertEqual(package['evidence_checks'][0]['status'], 'RESOLVED')
        for changes in ([], [dict(package['evidence_checks'][0], id='study:E-999')],
                        [dict(package['evidence_checks'][0], end_line=5)]):
            with self.subTest(resolutions=changes), self.assertRaises(ContractError):
                freeze_collection(original, changes)

    def test_assembly_preserves_registries_and_cannot_raise_collection_status(self):
        for mode in ('git', 'folder'):
            context, collector, writer, resolutions = self.parts(mode)
            collector.update(completion_status='BLOCKED', limitations=['Source link unavailable.'])
            writer.update(completion_status='PARTIAL', limitations=['Source link unavailable.', 'Flow incomplete.'])
            package = freeze_collection(collector, resolutions)
            original = copy.deepcopy(package)
            result = assemble_study(writer, package, dict(context, phase='write'), mode)
            self.assertEqual(result['completion_status'], 'BLOCKED')
            self.assertEqual(result['limitations'], ['Source link unavailable.', 'Flow incomplete.'])
            self.assertEqual(result['claims'], collector['claims'])
            self.assertEqual(result['evidence'], collector['evidence'])
            self.assertEqual(package, original)

    def test_unresolved_evidence_forces_partial_including_citation_compromise(self):
        for status in ('QUOTE_MISMATCH', 'NOT_FOUND'):
            context, collector, writer, resolutions = self.parts()
            resolutions[0]['status'] = status
            result = assemble_study(writer, freeze_collection(collector, resolutions),
                                    dict(context, result_policy='compromise'))
            self.assertEqual(result['completion_status'], 'PARTIAL')
            self.assertEqual(result['limitations'], ['Evidence study:E-001: ' + status + '.'])

    def test_each_incomplete_phase_requires_its_own_limitations(self):
        context, collector, writer, resolutions = self.parts()
        for status in ('PARTIAL', 'BLOCKED'):
            invalid = dict(collector, completion_status=status, limitations=[])
            with self.subTest(phase='collect', status=status), self.assertRaises(ContractError):
                freeze_collection(invalid, resolutions)
            collector.update(completion_status='PARTIAL', limitations=['Collector gap.'])
            package = freeze_collection(collector, resolutions)
            invalid = dict(writer, completion_status=status)
            with self.subTest(phase='write', status=status), self.assertRaises(ContractError):
                assemble_study(invalid, package, context)

    def test_partial_writer_can_omit_diagram_but_malformed_diagram_still_fails(self):
        context, collector, writer, resolutions = self.parts()
        writer.update(completion_status='PARTIAL', limitations=['Data flow requires another check.'])
        section = next(item for item in writer['report_sections'] if item['key'] == 'data_and_state')
        diagram = section['blocks'].pop()
        package = freeze_collection(collector, resolutions)
        result = assemble_study(writer, package, context)
        self.assertEqual(result['completion_status'], 'PARTIAL')
        diagram['markdown'] = diagram['markdown'].replace('@enduml', '')
        section['blocks'].append(diagram)
        with self.assertRaises(ContractError):
            assemble_study(writer, package, context)

    def test_assembly_checks_writer_links_and_both_source_identities(self):
        context, collector, writer, resolutions = self.parts()
        package = freeze_collection(collector, resolutions)
        for mutation in ('link', 'unbound', 'writer_identity', 'collector_identity'):
            authored, frozen = copy.deepcopy(writer), copy.deepcopy(package)
            if mutation == 'link':
                authored['report_sections'][0]['blocks'][0]['claim_ids'] = ['C-999']
            elif mutation == 'unbound':
                frozen['study']['claims'].append(dict(collector['claims'][0], id='C-002'))
            elif mutation == 'writer_identity':
                authored['source_commit'] = 'changed'
            else:
                frozen['study']['source_commit'] = 'changed'
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                assemble_study(authored, frozen, context)

    def test_static_phase_schemas_match_runtime(self):
        root = Path(__file__).resolve().parents[1]
        for prefix, schemas in (('', MODEL_STUDY_PHASE_SCHEMAS), ('folder-', MODEL_FOLDER_STUDY_PHASE_SCHEMAS)):
            for phase, schema in schemas.items():
                path = root / 'schemas' / (prefix + 'study-' + phase + '.schema.json')
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), schema)


if __name__ == '__main__':
    unittest.main()
