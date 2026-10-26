# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Scope structure checks share decisions across wire, saved and published text."""
import copy
import unittest

from fixtures.ledger_response import response
from src.analysis.evidence import canonical, sha
from src.contracts.contracts import ContractError, validate_result
from src.reports.architecture_format import section_headings
from src.reports.document_rendering import materialize_study, validate_materialized, validate_published_architecture
from test_required_diagrams import unchecked_materialization


CONTEXTS = (dict(branch='main', source_commit='abc'),
            dict(source_directory='/source', source_fingerprint='abc'))
LABELS = (('Investigated', 'Partially investigated', 'Unverified', 'Out of scope'),
          ('Исследовано', 'Частично исследовано', 'Не проверено', 'Вне области исследования'))


def scope_text(labels, changed=None):
    return '\n\n'.join((changed or {}).get(index, f'### {label}\nNone: no items apply in this synthetic fixture.')
                       for index, label in enumerate(labels))


class ScopeSectionTests(unittest.TestCase):
    def checks(self, context, markdown, status='COMPLETE'):
        wire = response(context)
        wire['report_sections'][0]['blocks'] = [dict(markdown=markdown, claim_ids=['C-001'])]
        wire.update(completion_status=status, limitations=[] if status == 'COMPLETE' else ['Runtime not inspected.'])
        saved = unchecked_materialization(wire)
        published = '\n\n'.join('## ' + heading + '\n\n' +
            '\n\n'.join(block['markdown'] for block in section['blocks'])
            for heading, section in zip(section_headings('English'), wire['report_sections']))
        return (lambda: validate_result('study', wire, context,
                                       'folder' if 'source_directory' in context else 'git'),
                lambda: validate_materialized(saved),
                lambda: validate_published_architecture(published, 'English'))

    def assert_invalid(self, markdown, code, group, status='COMPLETE'):
        for context in CONTEXTS:
            for index, check in enumerate(self.checks(context, markdown, status)):
                with self.subTest(context=context, representation=index, status=status):
                    with self.assertRaises(ContractError) as caught:
                        check()
                    self.assertEqual(caught.exception.details['code'], code)
                    self.assertEqual(caught.exception.details['section_key'], 'scope')
                    self.assertEqual(caught.exception.details['group'], group)
                    self.assertIn('path', caught.exception.details)

    def test_four_groups_and_explained_absence_in_both_languages_and_modes(self):
        for labels in LABELS:
            for context in CONTEXTS:
                with self.subTest(labels=labels, context=context):
                    for check in self.checks(context, scope_text(labels)):
                        check()

    def test_each_missing_duplicate_and_empty_group_has_a_named_diagnostic(self):
        for labels in LABELS:
            for index, label in enumerate(labels):
                group = LABELS[0][index]
                self.assert_invalid(scope_text(labels, {index: ''}), 'SCOPE_GROUP_MISSING', group)
                self.assert_invalid(scope_text(labels) + f'\n\n### {label}\nRepeated.',
                                    'DUPLICATE_SCOPE_GROUP', group)
                for body in ('', '<!-- explanation hidden -->', '---', '#### Only a subheading', '[](/empty)'):
                    self.assert_invalid(scope_text(labels, {index: f'### {label}\n\n{body}'}),
                                        'EMPTY_SCOPE_GROUP', group)

    def test_code_quotes_lists_html_and_plain_text_do_not_supply_groups(self):
        for fake in ('```markdown\n### Unverified\nNot executed.\n```',
                     '~~~\n### Unverified\nNot executed.\n~~~',
                     '    ### Unverified\n    Not executed.',
                     '> ### Unverified\n> Not executed.',
                     '- ### Unverified\n  Not executed.',
                     '<h3>Unverified</h3>\nNot executed.',
                     'The Unverified group is described in ordinary text.',
                     '**Unverified**: not executed.', '#### Unverified\nNot executed.'):
            self.assert_invalid(scope_text(LABELS[0], {2: fake}), 'SCOPE_GROUP_MISSING', 'Unverified')

    def test_aliases_cannot_duplicate_a_group(self):
        self.assert_invalid(scope_text(LABELS[0]) + '\n\n### Исследовано\nПовтор.',
                            'DUPLICATE_SCOPE_GROUP', 'Investigated')

    def test_partial_and_blocked_reports_still_require_scope_structure(self):
        for status in ('PARTIAL', 'BLOCKED'):
            self.assert_invalid(scope_text(LABELS[0], {3: ''}), 'SCOPE_GROUP_MISSING', 'Out of scope', status)

    def test_other_h3_content_cannot_fill_an_empty_group(self):
        markdown = scope_text(LABELS[0], {3: '### Out of scope\n\n### Sources\napp.py'})
        self.assert_invalid(markdown, 'EMPTY_SCOPE_GROUP', 'Out of scope')

    def test_groups_can_span_blocks_without_changing_text_or_identity(self):
        wire = response(CONTEXTS[0])
        wire['report_sections'][0]['blocks'] = [
            dict(markdown=part, claim_ids=['C-001'])
            for label in reversed(LABELS[0]) for part in (f'### {label}', 'None: no matching fixture items.')]
        original = copy.deepcopy(wire)
        saved = materialize_study(wire)
        validate_materialized(saved)
        self.assertEqual(wire, original)
        self.assertEqual(saved, materialize_study(wire))

    def test_groups_in_another_section_cannot_replace_scope(self):
        wire = response(CONTEXTS[0])
        wire['report_sections'][1]['blocks'].extend(wire['report_sections'][0]['blocks'][1:])
        wire['report_sections'][0]['blocks'] = wire['report_sections'][0]['blocks'][:1]
        with self.assertRaises(ContractError):
            validate_materialized(unchecked_materialization(wire))

    def test_valid_hash_cannot_move_scope_groups_into_another_mapped_section(self):
        saved = materialize_study(response(CONTEXTS[0]))
        saved['block_map'][1].update(section_key='context', section_index=1, block_index=0)
        saved['block_map'][2]['block_index'] = 1
        saved['materialization_provenance']['block_map_sha256'] = sha(canonical(saved['block_map']))
        with self.assertRaises(ContractError) as caught:
            validate_materialized(saved)
        self.assertEqual(caught.exception.details['code'], 'SCOPE_GROUP_MISSING')
        self.assertEqual(caught.exception.details['group'], 'Investigated')
        self.assertEqual(caught.exception.details['path'], '$.block_map')


if __name__ == '__main__':
    unittest.main()
