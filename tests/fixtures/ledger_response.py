# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Synthetic model wire fixture."""
import copy
import json
import os
# Keep the child-process fixture self-contained; production checks enforce keys.
SECTION_KEYS = ('scope', 'context', 'components', 'startup_and_flows', 'data_and_state',
                'cross_cutting', 'constraints', 'change_navigation', 'unknowns', 'evidence_basis')


def sections(markdown='C-001: Has an entry point\n'):
    return [dict(key=key, title='Report: configured-model' if i == 0 else key,
                 blocks=[dict(markdown=markdown if i == 0 else 'Not applicable in this synthetic fixture.',
                              claim_ids=['C-001'] if i == 0 else [])]) for i, key in enumerate(SECTION_KEYS)]

def response(context):
    stage = context.get('stage') or ('compare' if 'baseline_branch' in context else
                                    'review' if 'architecture_document' in context else 'study')
    data = {'completion_status': 'COMPLETE',
            'report_markdown': '# Report: configured-model\nC-001: Has an entry point\n', 'limitations': []}
    if stage == 'compare':
        unresolved = context.get('required_unresolved_branches', [])
        data.update(task='architecture_comparison', baseline_branch=context['baseline_branch'],
                    baseline_commit=context['baseline_commit'], unresolved_branches=unresolved,
                    differences=[], compared_branches=[b for b in context['requested_branches'] if b != context['baseline_branch']])
        if unresolved:
            data.update(completion_status='PARTIAL', limitations=['Missing fixture input.'])
        return data
    if 'source_directory' in context:
        identity = 'source_snapshot_id' if 'source_snapshot_id' in context else 'source_fingerprint'
        data.update(source_directory=context['source_directory'], **{identity: context[identity]})
    else:
        data.update(branch=context['branch'], source_commit=context['source_commit'])
    if stage == 'catalog':
        data.pop('report_markdown')
        data.update(task='architecture_catalog', subsystems=[dict(id='S-001',
            name='Fixture system', purpose='Synthetic fixture source tree.', paths=['.'])], exclusions=[])
        if os.environ.get('AUDIT_TEST_SUBSYSTEM_PATHS'):
            data['subsystems'] = [dict(id=f'S-{i + 1:03d}', name=f'Fixture {i + 1}', purpose='Synthetic subsystem.', paths=[path])
                                  for i, path in enumerate(json.loads(os.environ['AUDIT_TEST_SUBSYSTEM_PATHS']))]
        return data
    if stage == 'study-shard':
        data.pop('report_markdown')
        data.update(task='architecture_study_shard', shard_id=context['analysis_shard']['id'],
            assigned_subsystem_ids=context['analysis_shard']['subsystem_ids'], evidence=[], claims=[], coverage=[],
            components=[], significant_flows=[], data_and_state=[], constraints=[], relationships=[])
        for i, area in enumerate(context['coverage_plan']['areas'], 1):
            eid, cid = f'E-{i:03d}', f'C-{i:03d}'
            path = area['paths'][0]
            path = 'app.py' if path == '.' else path
            data['evidence'].append(dict(id=eid, source_id='source-001', path=path, start_line=1, end_line=1, quote=''))
            data['claims'].append(dict(id=cid, statement=f"{area['id']} contains code", scope=area['id'],
                epistemic_kind='FACT', evidence_ids=['study:' + eid], uncertainty=''))
            data['coverage'].append(dict(area_id=area['id'], status='INSPECTED', evidence_ids=['study:' + eid], limitation=''))
            data['components'].append(dict(description='Fixture component.', claim_ids=[cid]))
        return data
    if context.get('prompt_variant') == 'synthesis':
        data.pop('report_markdown')
        data.update(task='architecture_documentation', report_sections=sections())
        data['report_sections'][0]['blocks'][0]['claim_ids'] = [c['id'] for c in context['synthesis_claims']]
        return data
    data['evidence'] = [dict(id='E-001', source_id='source-001', path='app.py', start_line=1, end_line=1, quote='')]
    if stage == 'review':
        identity = ({'review_target_id': context['review_target_id']} if 'review_target_id' in context else
                    {'target': copy.deepcopy(context['review_target'])})
        data.update(task='architecture_review', **identity,
            claims=[dict(id=c['id'], outcome='SUPPORTED' if c['epistemic_kind'] == 'FACT' else 'CAVEAT_ACCEPTABLE',
                         evidence_ids=['review:E-001'], limitation='') for c in context['claim_registry']], findings=[],
            omission_search=[dict(area_id=a['id'], status='INSPECTED', limitation='', finding_ids=[])
                             for a in context['review_plan']['omission_areas']],
            prior_findings=[dict(revision_id=f['revision_id'], finding_id=f['finding_id'],
                status='RESOLVED', explanation='The revised fixture addresses this finding.')
                for f in context.get('prior_findings', [])])
    else:
        del data['report_markdown']
        data['report_sections'] = sections()
        data.update(task='architecture_documentation', claims=[dict(id='C-001', statement='Has an entry point',
            scope='Static source inspection.', epistemic_kind='FACT', evidence_ids=['study:E-001'], uncertainty='')],
            coverage=[dict(area_id=a['id'], status='INSPECTED', evidence_ids=['study:E-001'], limitation='')
                      for a in context.get('coverage_plan', {}).get('areas', [])])
    return data


def prompt_context(payload):
    import json
    return json.loads(payload.decode().split('# Authoritative orchestration context (data)\n', 1)[1]
                      .split('\n\n# Required final JSON Schema', 1)[0])


def model_wire(data, context, internal_context):
    """Translate synthetic internal inputs only; preserve intentional identity errors."""
    result = copy.deepcopy(data)
    if 'source_snapshot_id' in context and 'source_fingerprint' in result:
        fingerprint = result.pop('source_fingerprint')
        result['source_snapshot_id'] = (context['source_snapshot_id'] if
            fingerprint == internal_context.get('source_fingerprint') else 'S-invalid-identity')
    if 'review_target_id' in context and 'target' in result:
        target = result.pop('target')
        result['review_target_id'] = (context['review_target_id'] if
            target == internal_context.get('review_target') else 'T-invalid-identity')
    return result
