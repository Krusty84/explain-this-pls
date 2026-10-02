# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Synthetic model wire fixture, not a response from the user's historical run."""
import copy
# Keep the child-process fixture self-contained; production checks enforce keys.
SECTION_KEYS = ('scope', 'context', 'components', 'startup_and_flows', 'data_and_state',
                'cross_cutting', 'constraints', 'change_navigation', 'unknowns', 'evidence_basis')


def sections(markdown='C-001: Has an entry point\n'):
    return [dict(key=key, title='Report: configured-model' if i == 0 else key,
                 blocks=[dict(markdown=markdown if i == 0 else 'Not applicable in this synthetic fixture.',
                              claim_ids=['C-001'] if i == 0 else [])]) for i, key in enumerate(SECTION_KEYS)]

def response(context):
    data = {'completion_status': 'COMPLETE',
            'report_markdown': '# Report: configured-model\nC-001: Has an entry point\n', 'limitations': []}
    if 'baseline_branch' in context:
        unresolved = context.get('required_unresolved_branches', [])
        data.update(task='architecture_comparison', baseline_branch=context['baseline_branch'],
                    baseline_commit=context['baseline_commit'], unresolved_branches=unresolved,
                    differences=[], compared_branches=[b for b in context['requested_branches'] if b != context['baseline_branch']])
        if unresolved:
            data.update(completion_status='PARTIAL', limitations=['Missing fixture input.'])
        return data
    if 'source_directory' in context:
        data.update(source_directory=context['source_directory'], source_fingerprint=context['source_fingerprint'])
    else:
        data.update(branch=context['branch'], source_commit=context['source_commit'])
    data['evidence'] = [dict(id='E-001', source_id='source-001', path='app.py', start_line=1, end_line=1, quote='')]
    if 'architecture_document' in context:
        data.update(task='architecture_review', target=copy.deepcopy(context['review_target']),
            claims=[dict(id=c['id'], outcome='SUPPORTED' if c['epistemic_kind'] == 'FACT' else 'CAVEAT_ACCEPTABLE',
                         evidence_ids=['review:E-001'], limitation='') for c in context['claim_registry']], findings=[],
            omission_search=[dict(area_id=a['id'], status='INSPECTED', limitation='', finding_ids=[])
                             for a in context['review_plan']['omission_areas']])
    else:
        del data['report_markdown']
        data['report_sections'] = sections()
        data.update(task='architecture_documentation', claims=[dict(id='C-001', statement='Has an entry point',
            scope='Static source inspection.', epistemic_kind='FACT', evidence_ids=['study:E-001'], uncertainty='')])
    return data
