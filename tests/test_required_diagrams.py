# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Local required-diagram checks, without model or PlantUML calls."""
import copy
import unittest
from unittest.mock import patch

from fixtures.ledger_response import response
from fixtures.required_diagrams import missing_diagram, malformed_diagram
from src.analysis.evidence import canonical, sha
from src.analysis.ledger import prepare_result, review_context
from src.contracts.contracts import ContractError, validate_result
from src.reports.document_rendering import (inspect_required_diagrams, inspect_materialized_diagrams,
    materialize_study, serialize, validate_materialized)
from src.reports.presentation import render_stage


CONTEXT = dict(branch='main', source_commit='a' * 40)
REQUIRED = ['components', 'data_and_state']


def plantuml(body='A --> B', fence='```', language='plantuml'):
    return f'{fence}{language}\n@startuml\n{body}\n@enduml\n{fence}'


def unchecked_materialization(wire):
    """Build consistent coordinates and hashes to exercise materialized validation."""
    markdown, blocks = serialize(wire['report_sections'])
    claims = copy.deepcopy(wire['claims'])
    for claim in claims:
        claim['document_locators'] = [copy.deepcopy(block['locator']) for block in blocks
                                     if claim['id'] in block['claim_ids']]
    data = copy.deepcopy({key: value for key, value in wire.items()
                          if key not in ('report_sections', 'claims')})
    data.update(report_markdown=markdown, block_map=blocks, claims=claims,
                materialization_provenance=dict(rule='study-blocks-lf',
                    normalized_sha256=sha(canonical(wire)), document_sha256=sha(markdown.encode('utf-8')),
                    registry_sha256=sha(canonical(claims)), block_map_sha256=sha(canonical(blocks))))
    return data


class RequiredDiagramTests(unittest.TestCase):
    def inspect(self, wire):
        return inspect_required_diagrams(wire['report_sections'], wire['claims'])

    def assert_invalid(self, markdown, code):
        wire = malformed_diagram(CONTEXT, markdown)
        with self.assertRaises(ContractError) as caught:
            self.inspect(wire)
        details = caught.exception.details
        self.assertEqual(details['code'], code)
        self.assertEqual(details['section_key'], 'components')
        self.assertEqual(details['diagram_type'], 'component_overview')
        self.assertEqual(details['block_index'], 1)
        self.assertIn('$.report_sections[2].blocks[1]', details['path'])
        self.assertNotIn('PRIVATE_DIAGRAM_TEXT', str(caught.exception))
        self.assertNotIn('PRIVATE_DIAGRAM_TEXT', str(details))

    def test_two_required_diagrams_are_deterministic_pure_and_do_not_mutate(self):
        wire = response(CONTEXT)
        original = copy.deepcopy(wire)
        expected = dict(required_sections=REQUIRED, present_sections=REQUIRED,
                        missing_sections=[], complete=True, scope='STRUCTURAL_ONLY')
        with patch('builtins.open', side_effect=AssertionError('Diagram inspection must not read files')):
            self.assertEqual(self.inspect(wire), expected)
            self.assertEqual(self.inspect(wire), expected)
        self.assertEqual(wire, original)
        validate_result('study', wire, CONTEXT)

    def test_missing_required_sections_are_reported_in_fixed_order(self):
        for omitted in (['components'], ['data_and_state'], REQUIRED):
            wire = response(CONTEXT)
            for section in wire['report_sections']:
                if section['key'] in omitted:
                    section['blocks'] = section['blocks'][:1]
            with self.subTest(omitted=omitted):
                check = self.inspect(wire)
                self.assertFalse(check['complete'])
                self.assertEqual(check['missing_sections'], omitted)
                self.assertEqual(check['present_sections'], [key for key in REQUIRED if key not in omitted])
                with self.assertRaises(ContractError) as caught:
                    validate_result('study', wire, CONTEXT)
                self.assertEqual(caught.exception.details['code'], 'REQUIRED_DIAGRAM_MISSING')
                self.assertEqual(caught.exception.details['section_key'], omitted[0])
                self.assertIn('blocks', caught.exception.details['path'])
        wire['report_sections'] = [section for section in wire['report_sections']
                                   if section['key'] not in REQUIRED]
        self.assertEqual(self.inspect(wire)['missing_sections'], REQUIRED)

    def test_diagram_in_another_section_does_not_replace_required_diagram(self):
        wire = missing_diagram(CONTEXT, 'components')
        wire['report_sections'][3]['blocks'].append(dict(markdown=plantuml(), claim_ids=['C-001']))
        self.assertEqual(self.inspect(wire)['missing_sections'], ['components'])
        with self.assertRaises(ContractError):
            validate_result('study', wire, CONTEXT)

    def test_extra_diagrams_are_allowed_without_a_count_limit(self):
        wire = response(CONTEXT)
        for index in (2, 3, 4):
            wire['report_sections'][index]['blocks'].extend(
                dict(markdown=plantuml(), claim_ids=['C-001']) for _ in range(3))
        validate_result('study', wire, CONTEXT)
        self.assertTrue(self.inspect(wire)['complete'])

    def test_plain_wrong_language_and_inline_code_do_not_count(self):
        for markdown in ('@startuml\nA --> B\n@enduml', plantuml(language='text'),
                         '`@startuml A --> B @enduml`', '` ```plantuml `'):
            with self.subTest(markdown=markdown):
                wire = malformed_diagram(CONTEXT, markdown)
                self.assertEqual(self.inspect(wire)['missing_sections'], ['components'])
                with self.assertRaises(ContractError) as caught:
                    validate_result('study', wire, CONTEXT)
                self.assertEqual(caught.exception.details['code'], 'REQUIRED_DIAGRAM_MISSING')

    def test_commonmark_fences_allow_case_tildes_indentation_and_blank_lines(self):
        for markdown in (plantuml(language='PlAnTuMl'), plantuml(fence='~~~'),
                         plantuml(fence='````'), plantuml() + '\n\n', '\n\n' + plantuml(),
                         '\n'.join('   ' + line for line in plantuml().split('\n')),
                         plantuml().replace('\n', '\r\n')):
            with self.subTest(markdown=markdown):
                wire = malformed_diagram(CONTEXT, markdown)
                validate_result('study', wire, CONTEXT)
                self.assertTrue(self.inspect(wire)['complete'])

    def test_invalid_or_nonstandalone_fences_are_rejected(self):
        for markdown in (plantuml(language='plantuml extra'), plantuml().removesuffix('```'),
                         'Explanation.\n\n' + plantuml(), plantuml() + '\n\nExplanation.',
                         '[ref]: /local\n\n' + plantuml(), plantuml() + '\n\n[ref]: /local',
                         plantuml() + '\n\n' + plantuml(),
                         '\n'.join('> ' + line for line in plantuml().split('\n')),
                         '- ' + plantuml().replace('\n', '\n  ')):
            with self.subTest(markdown=markdown):
                self.assert_invalid(markdown, 'INVALID_PLANTUML_FENCE')

    def test_boundaries_are_required_once_and_in_order(self):
        for body in ('A --> B\n@enduml', '@startuml\nA --> B',
                     '@startuml\n@startuml\nA --> B\n@enduml',
                     '@startuml\nA --> B\n@enduml\n@enduml',
                     '@enduml\nA --> B\n@startuml'):
            with self.subTest(body=body):
                self.assert_invalid('```plantuml\n' + body + '\n```', 'INVALID_PLANTUML_BOUNDARY')

    def test_empty_and_comment_only_diagrams_are_rejected(self):
        for body in ('', ' \n\t', "' Only a comment", "/'\nA comment\n'/"):
            with self.subTest(body=body):
                self.assert_invalid(plantuml(body), 'EMPTY_PLANTUML_DIAGRAM')

    def test_external_include_and_import_directives_are_rejected(self):
        for directive in ('!include', '!includeurl', '!include_once', '!include_many', '!import'):
            for prefix in ('', '  ', '\t'):
                with self.subTest(directive=directive, prefix=prefix):
                    self.assert_invalid(plantuml(prefix + directive + ' PRIVATE_DIAGRAM_TEXT'),
                                        'FORBIDDEN_PLANTUML_DIRECTIVE')

    def test_comments_and_literal_examples_are_not_external_directives(self):
        bodies = ["' !include PRIVATE_DIAGRAM_TEXT\nA --> B",
                  "/'\n!include PRIVATE_DIAGRAM_TEXT\n'/\nA --> B",
                  '"!include PRIVATE_DIAGRAM_TEXT"\nA --> B',
                  'note "!import PRIVATE_DIAGRAM_TEXT" as Example\nA --> B',
                  'component A #red\n## Literal diagram text\nA --> B']
        for body in bodies:
            with self.subTest(body=body):
                wire = malformed_diagram(CONTEXT, plantuml(body))
                validate_result('study', wire, CONTEXT)
                self.assertTrue(self.inspect(wire)['complete'])

    def test_required_diagrams_need_registered_claims(self):
        for claim_ids in ([], ['C-999'], ['C-001', 'C-999']):
            wire = response(CONTEXT)
            wire['report_sections'][2]['blocks'][-1]['claim_ids'] = claim_ids
            with self.subTest(claim_ids=claim_ids), self.assertRaises(ContractError) as caught:
                self.inspect(wire)
            self.assertEqual(caught.exception.details['code'], 'UNLINKED_REQUIRED_DIAGRAM')
            with self.assertRaises(ContractError) as caught:
                validate_result('study', wire, CONTEXT)
            self.assertEqual(caught.exception.details['code'], 'UNLINKED_REQUIRED_DIAGRAM')
            with self.assertRaises(ContractError) as caught:
                validate_materialized(unchecked_materialization(wire))
            self.assertEqual(caught.exception.details['code'], 'UNLINKED_REQUIRED_DIAGRAM')

    def test_malformed_present_diagram_fails_for_each_status_even_with_a_valid_one(self):
        for status in ('COMPLETE', 'PARTIAL', 'BLOCKED'):
            wire = response(CONTEXT)
            wire.update(completion_status=status, limitations=[] if status == 'COMPLETE' else ['Unavailable input.'])
            wire['report_sections'][2]['blocks'].append(
                dict(markdown=plantuml(''), claim_ids=['C-001']))
            with self.subTest(status=status), self.assertRaises(ContractError) as caught:
                validate_result('study', wire, CONTEXT)
            self.assertEqual(caught.exception.details['code'], 'EMPTY_PLANTUML_DIAGRAM')

    def test_partial_and_blocked_omissions_are_saved_without_positive_policy(self):
        resolved = [dict(id='study:E-001', status='RESOLVED', source_id='source-001', path='app.py')]
        for status in ('PARTIAL', 'BLOCKED'):
            wire = response(CONTEXT)
            wire.update(completion_status=status, limitations=['Unavailable input.'])
            for section in wire['report_sections']:
                if section['key'] in REQUIRED:
                    section['blocks'] = [dict(markdown='Inspect the unavailable source before adding the diagram.',
                                              claim_ids=[])]
            original = copy.deepcopy(wire)
            with self.subTest(status=status), patch('src.analysis.ledger.resolve_evidence', return_value=resolved):
                validate_result('study', wire, CONTEXT)
                saved = prepare_result('study', wire, CONTEXT)
                self.assertEqual(saved['completion_status'], status)
                self.assertEqual(saved['limitations'], wire['limitations'])
                self.assertEqual(saved['program_checks']['diagrams']['missing_sections'], REQUIRED)
                self.assertFalse(saved['program_checks']['diagrams']['complete'])
                self.assertFalse(saved['program_checks']['policy_satisfied'])
                validate_materialized(saved)
                self.assertEqual(wire, original)
            wire['limitations'] = []
            with self.assertRaises(ContractError):
                validate_result('study', wire, CONTEXT)

    def test_partial_with_both_diagrams_keeps_status_and_limits(self):
        wire = response(CONTEXT)
        wire.update(completion_status='PARTIAL', limitations=['The deployment remains unverified.'])
        resolved = [dict(id='study:E-001', status='RESOLVED', source_id='source-001', path='app.py')]
        with patch('src.analysis.ledger.resolve_evidence', return_value=resolved):
            saved = prepare_result('study', wire, CONTEXT)
        self.assertEqual(saved['completion_status'], 'PARTIAL')
        self.assertEqual(saved['limitations'], wire['limitations'])
        self.assertTrue(saved['program_checks']['diagrams']['complete'])
        self.assertFalse(saved['program_checks']['policy_satisfied'])

    def test_materialized_partial_and_blocked_require_nonempty_limits(self):
        for status in ('PARTIAL', 'BLOCKED'):
            for limitations in ([], [''], [' \t\n']):
                wire = missing_diagram(CONTEXT, 'components')
                wire.update(completion_status=status, limitations=limitations)
                saved = unchecked_materialization(wire)
                with self.subTest(status=status, limitations=limitations):
                    with self.assertRaises(ContractError):
                        validate_result('study', wire, CONTEXT)
                    with self.assertRaises(ContractError):
                        validate_materialized(saved)

    def test_saved_diagram_checks_must_match_recomputed_inspection(self):
        resolved = [dict(id='study:E-001', status='RESOLVED', source_id='source-001', path='app.py')]
        with patch('src.analysis.ledger.resolve_evidence', return_value=resolved):
            saved = prepare_result('study', response(CONTEXT), CONTEXT)
        expected = inspect_materialized_diagrams(saved)
        self.assertEqual(saved['program_checks']['diagrams'], expected)
        mutations = dict(required_sections=['components'], present_sections=['components'],
                         missing_sections=['data_and_state'], complete=False, scope='COMPILED')
        for field, value in mutations.items():
            changed = copy.deepcopy(saved)
            changed['program_checks']['diagrams'][field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_materialized(changed)

    def test_malformed_program_checks_reach_saved_schema_validation(self):
        resolved = [dict(id='study:E-001', status='RESOLVED', source_id='source-001', path='app.py')]
        with patch('src.analysis.ledger.resolve_evidence', return_value=resolved):
            saved = prepare_result('study', response(CONTEXT), CONTEXT)
        for checks in (None, 1, 'invalid', []):
            changed = saved | {'program_checks': checks}
            with self.subTest(checks=checks):
                validate_materialized(changed)
                with self.assertRaises(ContractError) as caught:
                    render_stage('study', changed)
                self.assertEqual(caught.exception.details['path'], '$.program_checks')

    def test_wire_and_materialized_decisions_agree_for_repeated_blocks(self):
        wires = [response(CONTEXT), missing_diagram(CONTEXT, 'components'),
                 malformed_diagram(CONTEXT, plantuml('')),
                 malformed_diagram(CONTEXT, plantuml('!include PRIVATE_DIAGRAM_TEXT')),
                 malformed_diagram(CONTEXT, plantuml().replace('@enduml', '@startuml')),
                 malformed_diagram(CONTEXT, plantuml().removesuffix('```')),
                 malformed_diagram(CONTEXT, plantuml(language='text')),
                 malformed_diagram(CONTEXT, '[ref]: /local\n\n' + plantuml()),
                 malformed_diagram(CONTEXT, plantuml() + '\n\n[ref]: /local'),
                 malformed_diagram(CONTEXT, '\n'.join('> ' + line for line in plantuml().split('\n')))]
        for claim_ids in ([], ['C-999']):
            wire = response(CONTEXT)
            wire['report_sections'][2]['blocks'][-1]['claim_ids'] = claim_ids
            wires.append(wire)
        partial = missing_diagram(CONTEXT, 'data_and_state')
        partial.update(completion_status='PARTIAL', limitations=['Unavailable input.'])
        wires.append(partial)
        repeated = response(CONTEXT)
        repeated['report_sections'][2]['blocks'][-1]['markdown'] = plantuml()
        repeated['report_sections'][4]['blocks'][-1]['markdown'] = plantuml()
        repeated['report_sections'][2]['blocks'].append(copy.deepcopy(repeated['report_sections'][2]['blocks'][-1]))
        wires.append(repeated)
        for index, wire in enumerate(wires):
            saved = unchecked_materialization(wire)
            original = copy.deepcopy(saved)
            decisions = []
            for check in (lambda: validate_result('study', wire, CONTEXT), lambda: validate_materialized(saved)):
                try:
                    check()
                    decisions.append(True)
                except ContractError:
                    decisions.append(False)
            with self.subTest(case=index):
                self.assertEqual(decisions[0], decisions[1])
                if decisions[0]:
                    self.assertEqual(self.inspect(wire), inspect_materialized_diagrams(saved))
                self.assertEqual(saved, original)

    def test_inspection_preserves_canonical_document_and_review_target(self):
        saved = materialize_study(response(CONTEXT))
        original = copy.deepcopy(saved)
        target = review_context(saved, CONTEXT)['review_target']
        self.assertTrue(inspect_materialized_diagrams(saved)['complete'])
        validate_materialized(saved)
        self.assertEqual(saved, original)
        self.assertEqual(review_context(saved, CONTEXT)['review_target'], target)
        self.assertEqual(materialize_study(response(CONTEXT)), saved)


if __name__ == '__main__':
    unittest.main()
