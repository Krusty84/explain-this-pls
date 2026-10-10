# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Shard contracts and deterministic synthesis inputs; no provider scheduling."""
import copy

from src.analysis.coverage_plan import coverage_checks
from src.contracts.contracts import ContractError, contract_violation, references, unique_ids

OBSERVATION_FIELDS = ('components', 'significant_flows', 'data_and_state', 'constraints', 'relationships')


def empty_shard_result(shard, context):
    if shard['subsystem_ids'] or shard.get('primary_file_paths'):
        raise ContractError('Only unassigned shards can be completed locally.')
    identity = ('source_directory', 'source_fingerprint') if 'source_directory' in context else ('branch', 'source_commit')
    return dict(task='architecture_study_shard', shard_id=shard['id'], assigned_subsystem_ids=[],
                completion_status='COMPLETE', **{key: context[key] for key in identity},
                **{key: [] for key in (*OBSERVATION_FIELDS, 'evidence', 'claims', 'coverage', 'limitations')})


def validate_shard(data, context):
    shard = context['analysis_shard']
    assigned = shard['subsystem_ids']
    if data['shard_id'] != shard['id'] or data['assigned_subsystem_ids'] != assigned:
        raise contract_violation('SHARD_IDENTITY_MISMATCH', '$.shard_id', kind='IDENTITY_MISMATCH', layer='identity')
    unique_ids(data['evidence'], r'E-[0-9]{3,}', '$.evidence')
    unique_ids(data['claims'], r'C-[0-9]{3,}', '$.claims')
    for e in data['evidence']:
        if e['start_line'] < 1 or e['end_line'] < e['start_line']:
            raise contract_violation('INVALID_EVIDENCE_LINE_RANGE', '$.evidence')
    evidence_ids = {'study:' + e['id'] for e in data['evidence']}
    for claim in data['claims']:
        references(claim['evidence_ids'], evidence_ids, '$.claims[].evidence_ids', namespaces={'study'})
        if claim['epistemic_kind'] == 'FACT' and not claim['evidence_ids']:
            raise ContractError('Shard factual claims require source evidence.')
        if claim['epistemic_kind'] != 'FACT' and not claim['uncertainty'].strip():
            raise ContractError('Hypothesis/unknown requires a concrete missing check or limitation')
    claims = {c['id'] for c in data['claims']}
    paths = {e['path'] for e in context['_inventory']['entries']}
    for field in OBSERVATION_FIELDS:
        for index, record in enumerate(data[field]):
            references(record['claim_ids'], claims, '$.' + field + '[].claim_ids')
            if not record['claim_ids']:
                raise ContractError('Shard observations must link registered claims.')
            if field == 'relationships':
                if record['subsystem_id'] not in assigned:
                    raise contract_violation('UNASSIGNED_RELATIONSHIP_SUBSYSTEM', f'$.relationships[{index}].subsystem_id')
                if record['related_path'] not in paths:
                    raise contract_violation('UNKNOWN_RELATIONSHIP_PATH', f'$.relationships[{index}].related_path')
    reported = [a['area_id'] for a in data['coverage']]
    if len(reported) != len(set(reported)) or set(reported) != set(assigned):
        raise ContractError('Shard coverage must match exactly its primary assignment.')
    for area in data['coverage']:
        references(area['evidence_ids'], evidence_ids, '$.coverage[].evidence_ids', namespaces={'study'})
        if area['status'] != 'INSPECTED' and not area['limitation'].strip():
            raise ContractError('Unfinished coverage requires a limitation')
    if not assigned and any(data[key] for key in (*OBSERVATION_FIELDS, 'evidence', 'claims', 'coverage')):
        raise ContractError('An empty shard has no source-audit responsibility.')


def metadata_covers_primary(context, coverage):
    primary = set((context or {}).get('analysis_shard', {}).get('primary_file_paths', []))
    return bool(primary) and primary == set(coverage.get('metadata_verified_empty_paths', []))


def shard_checks(data, context, checks):
    checks['coverage'] = coverage_checks(data, context, checks['evidence'], area_ids=data['assigned_subsystem_ids'])
    checks['policy_satisfied'] = bool(data['completion_status'] == 'COMPLETE'
        and (data['claims'] or not data['assigned_subsystem_ids'] or metadata_covers_primary(context, checks['coverage']))
        and all(e['status'] == 'RESOLVED' for e in checks['evidence'])
        and checks['coverage']['policy_satisfied'])


def require_shard_policy(data, context=None):
    checks = data['program_checks']
    if checks['policy_satisfied']:
        return
    reasons = []
    if data['completion_status'] != 'COMPLETE':
        reasons.append('completion_status=' + data['completion_status'])
    if data['assigned_subsystem_ids'] and not data['claims'] and not metadata_covers_primary(context, checks['coverage']):
        reasons.append('claims=0')
    reasons.extend(f'{status}={count}' for status, count in sorted(checks['evidence_counts'].items())
                   if status != 'RESOLVED')
    coverage = checks['coverage']
    for key in ('missing_ids', 'unfinished_ids', 'unsupported_ids'):
        if coverage[key]:
            reasons.append(f'coverage_{key.removesuffix("_ids")}={len(coverage[key])}')
    raise ContractError('Study shard rejected: ' + ', '.join(reasons) + '.', safe=True,
        details=dict(code='SHARD_POLICY_UNSATISFIED', completion_status=data['completion_status'],
                     evidence_counts=checks['evidence_counts'], coverage=coverage))


def synthesis_inputs(plan, states):
    """Remap local IDs to the existing global E-/C- ID space, in shard/ID order.

    A synthesis supplies prose and blocks. Python copies these registries into
    the study and checks exact content and order before publication.
    """
    expected = [s['id'] for s in plan['shards']]
    if [s['id'] for s in states] != expected or any(s['status'] != 'SUCCEEDED' for s in states):
        raise ContractError('Every planned shard must succeed before synthesis.')
    evidence, claims, coverage, shards, mappings, resolutions = [], [], [], [], [], []
    artifact_hashes = {}
    for planned, state in zip(plan['shards'], states):
        original = state['study-shard']
        if (original['shard_id'] != planned['id'] or original['assigned_subsystem_ids'] != planned['subsystem_ids']):
            raise ContractError('Synthesis shard assignment changed.')
        meta = state['study-shard_invocation']
        local = meta.get('generated_by') == 'orchestrator'
        if local:
            if (meta.get('reason') != 'empty_subsystem_assignment' or planned['subsystem_ids']
                    or not meta.get('source_integrity_verified')
                    or {k: v for k, v in original.items() if k != 'program_checks'} != empty_shard_result(planned, original)):
                raise ContractError('Only validated empty assignments may bypass backend invocation.')
        if ((not local and not meta.get('backend_result_valid')) or not meta.get('local_validation')
                or not meta.get('publication_complete') or not original['program_checks']['policy_satisfied']):
            raise ContractError('Synthesis requires published, locally validated shard results.')
        for field in (('artifact_hashes',) if local else ('artifact_hashes', 'attempt_hashes', 'binding_hashes')):
            artifact_hashes.update({state['directory'] + '/' + name: pin for name, pin in meta[field].items()})
        shard = copy.deepcopy(original)
        shard.pop('program_checks')
        eid_map = {e['id']: f'E-{len(evidence) + i + 1:03d}'
                   for i, e in enumerate(sorted(shard['evidence'], key=lambda e: e['id']))}
        cid_map = {c['id']: f'C-{len(claims) + i + 1:03d}'
                   for i, c in enumerate(sorted(shard['claims'], key=lambda c: c['id']))}
        for resolved in original['program_checks']['evidence']:
            resolutions.append(copy.deepcopy(resolved) | {'id': 'study:' + eid_map[resolved['id'].removeprefix('study:')]})
        for e in shard['evidence']:
            e['id'] = eid_map[e['id']]
        for c in shard['claims']:
            c['id'] = cid_map[c['id']]
        for record in shard['claims'] + shard['coverage']:
            record['evidence_ids'] = ['study:' + eid_map[e.removeprefix('study:')] for e in record['evidence_ids']]
        for field in OBSERVATION_FIELDS:
            for record in shard[field]:
                record['claim_ids'] = [cid_map[c] for c in record['claim_ids']]
        evidence.extend(sorted(shard['evidence'], key=lambda e: e['id']))
        claims.extend(sorted(shard['claims'], key=lambda c: c['id']))
        coverage.extend(shard['coverage'])
        shards.append(shard)
        mappings.append({'shard_id': state['id'], 'evidence_ids': eid_map, 'claim_ids': cid_map})
    # A catalog area can span several primary file batches. Keep one global
    # coverage record and retain every contributing evidence reference and limit.
    merged = {}
    for area in coverage:
        sid = area['area_id']
        if sid not in merged:
            merged[sid] = copy.deepcopy(area)
            continue
        previous = merged[sid]
        if previous['status'] != area['status']:
            previous['status'] = 'PARTIALLY_INSPECTED'
        previous['evidence_ids'] = sorted(set(previous['evidence_ids'] + area['evidence_ids']))
        previous['limitation'] = '\n'.join(dict.fromkeys(s for s in (previous['limitation'], area['limitation']) if s))
    return {'validated_shards': shards, 'synthesis_evidence': evidence, 'synthesis_claims': claims,
            'synthesis_coverage': [merged[sid] for sid in sorted(merged)], 'shard_id_mappings': mappings,
            '_synthesis_resolutions': resolutions, '_shard_artifact_hashes': artifact_hashes}


def validate_synthesis(data, context):
    for field in ('evidence', 'claims', 'coverage'):
        if data[field] != context['synthesis_' + field]:
            raise contract_violation('SYNTHESIS_INPUT_CHANGED', '$.' + field)
