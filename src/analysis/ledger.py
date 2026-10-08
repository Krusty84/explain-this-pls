# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Frozen review identity and deterministic policy; no model or source execution."""
from __future__ import annotations
import copy
from collections import Counter
from src.contracts.contracts import CONTRACT_ID, ARTIFACT_FORMAT, ContractError, contract_violation, has_ledger_structure, has_program_checks, is_recovered_material, review_verdict, workflow_satisfied, comparison_factual_sides
from src.analysis.evidence import canonical, sha, source_catalog, resolve_evidence
from src.analysis.coverage_plan import build_coverage_plan, verify_coverage_plan, coverage_checks
from src.analysis.source_decoding import normalize_source_decoding

OMISSION_AREAS = ('context', 'components', 'startup_and_flows', 'data_and_state',
                  'cross_cutting', 'constraints', 'change_navigation', 'unknowns', 'evidence_basis')


def freeze_plan(document, context):
    if 'report_sections' in document:
        raise contract_violation('UNMATERIALIZED_STUDY', '$.report_sections')
    if not is_recovered_material('study', document):
        from src.reports.document_rendering import validate_materialized
        validate_materialized(document)
    registry = copy.deepcopy(document.get('claims', [])) if has_ledger_structure('study', document) else []
    areas = [{'id': 'A-' + str(i + 1).zfill(3), 'scope': name} for i, name in enumerate(OMISSION_AREAS)]
    areas += [{'id': 'P-' + str(i + 1).zfill(3), 'scope': name}
              for i, name in enumerate(context.get('priority_scenarios', []))]
    coverage = copy.deepcopy(context.get('coverage_plan'))
    if coverage is not None:
        verify_coverage_plan(coverage)
        if coverage['sources'] != source_catalog(context):
            raise ContractError('Coverage plan identifies another source snapshot.')
        areas += [{'id': a['id'], 'scope': a['name'] + ': ' + a['purpose']}
                  for a in coverage['areas'] if a['required']]
    previous = context.get('previous_revision')
    prior = []
    prior_review_hash, previous_registry_hash, diff = None, None, None
    if previous:
        prior = [{'revision_id': previous['revision_id'], 'finding_id': f['id']}
                 for f in previous['review']['findings'] if f['severity'] in ('HIGH', 'MEDIUM')]
        prior_review_hash = sha(canonical(previous['review']))
        previous_registry_hash = sha(canonical(previous['study']['claims']))
        from src.analysis.revisions import registry_diff
        diff = registry_diff(previous['study'], document)
        diff.update(previous_revision_id=previous['revision_id'], revision_id=context.get('revision_id', '001'))
    if context.get('prior_findings', []) != prior:
        raise ContractError('Previous material findings changed or were omitted from the review context.')
    sources = source_catalog(context)
    plan = {'sources': sources, 'source_sha256': sha(canonical(sources)),
        'document_sha256': sha(document['report_markdown'].encode('utf-8')),
        'registry_sha256': sha(canonical(registry)), 'required_claim_ids': [c['id'] for c in registry],
        'omission_areas': areas, 'eligible_study': bool(document.get('program_checks', {}).get('policy_satisfied')),
        'revision_id': context.get('revision_id', '001'), 'coverage_plan': coverage,
        'source_decoding': normalize_source_decoding(context.get('source_decoding')),
        'prior_findings': prior, 'prior_review_sha256': prior_review_hash,
        'previous_registry_sha256': previous_registry_hash, 'registry_diff': diff}
    return plan | {'plan_sha256': sha(canonical(plan))}


def target(plan):
    return {k: plan[k] for k in ('source_sha256', 'document_sha256', 'registry_sha256', 'plan_sha256')}


def review_context(document, context):
    plan = freeze_plan(document, context)
    existing = document.get('review_plan')
    if existing is not None and existing != plan:
        raise ContractError('Frozen document/registry/plan identity changed')
    return context | {'architecture_document': copy.deepcopy(document),
        'document_strictly_valid': has_program_checks(document),
        'document_sha256': plan['document_sha256'], 'claim_registry': copy.deepcopy(document.get('claims', []))
            if has_ledger_structure('study', document) else [],
        'review_plan': plan, 'review_target': target(plan)}


def verify_review_context(context):
    try:
        expected = freeze_plan(context['architecture_document'], context)
        if (context['review_plan'] != expected or context['review_target'] != target(expected) or
                context['document_sha256'] != expected['document_sha256'] or
                context['document_strictly_valid'] != has_program_checks(context['architecture_document']) or
                sha(canonical(context['claim_registry'])) != expected['registry_sha256']):
            raise ContractError('Frozen review context changed')
    except KeyError:
        raise ContractError('Required frozen review plan is missing') from None


def prepare_result(stage, data, context, expected_files=None, *, expected_metadata=None, catalog_recovery=None):
    """Caller must pass source/cleanup guards and validate its input first.

    Runner supplies materialized study (wire review/compare). Library callers may
    supply study wire; that path validates and materializes before any source read.

    Returns a separate saved representation. Never mutates the extracted response.
    Resolver reads are performed by the orchestrator, not credited to an agent.
    """
    if stage == 'study':
        from src.reports.document_rendering import materialize_study, validate_materialized
        if 'report_sections' in data:
            from src.contracts.contracts import validate_result
            validate_result(stage, data, context, 'folder' if 'source_directory' in context else 'git')
            data = materialize_study(data)
        validate_materialized(data)
    result = copy.deepcopy(data)
    checks = {'execution': 'COMPLETED', 'contract': 'VALID', 'source': 'MATCHED_AT_BOUNDARIES',
        'evidence': [], 'policy_satisfied': False, 'semantic_quality': 'NOT_MEASURED',
        'completion_self_assessment': data['completion_status'],
        'meaning': 'Policy checks concern processing and agent assessments; factual correctness is not established.'}
    result['program_checks'] = checks
    if stage == 'catalog':
        plan = build_coverage_plan(data, context['_inventory'], context,
            fallback=bool(catalog_recovery and not data['subsystems']), salvaged=bool(catalog_recovery))
        result['coverage_plan'] = plan
        if catalog_recovery:
            checks['completion_self_assessment'] = catalog_recovery['completion_self_assessment']
        checks['policy_satisfied'] = plan['policy_satisfied']
        return result
    if stage == 'compare':
        review_enabled = context.get('review_enabled', True)
        entries = {b['branch']: b for b in context['branches']}
        differences_ok = all(d['classification'] == 'CONFIRMED_DIFFERENCE' for d in data['differences'])
        if not review_enabled:
            differences_ok = all(d['classification'] == 'REPORTED_UNVERIFIED' and
                comparison_factual_sides(d, context) == {context['baseline_branch'], d['branch']}
                for d in data['differences'])
            checks['meaning'] = ('Configured processing checks only; review was disabled. '
                                 'Differences are author-reported and unverified.')
        checks.update(evidence_scope='SUPPLIED_REPORTS_ONLY', source='NOT_INSPECTED_IN_COMPARISON',
                      policy_satisfied=data['completion_status'] == 'COMPLETE' and not data['unresolved_branches']
                      and differences_ok and all(workflow_satisfied(entries.get(b, {}), review_enabled=review_enabled)
                                                 for b in context['requested_branches']))
        if context.get('generated_by') == 'orchestrator':
            checks['completion_self_assessment'] = None
        return result
    if stage == 'study' and context.get('prompt_variant') == 'synthesis':
        # Exact evidence/claim equality was validated before materialization.
        # Reuse the pinned resolutions: synthesis has no new source evidence.
        checks['evidence'] = copy.deepcopy(context['_synthesis_resolutions'])
    else:
        checks['evidence'] = resolve_evidence('study' if stage == 'study-shard' else stage, data['evidence'], context, expected_files,
                                             expected_metadata=expected_metadata)
    checks['evidence_counts'] = dict(sorted(Counter(e['status'] for e in checks['evidence']).items()))
    evidence_ok = all(e['status'] == 'RESOLVED' for e in checks['evidence'])
    if stage == 'study-shard':
        from src.analysis.study_shards import shard_checks
        shard_checks(data, context, checks)
        return result
    if stage == 'study':
        result['revision_id'] = context.get('revision_id', '001')
        result['registry_diff'] = None
        if context.get('previous_revision'):
            from src.analysis.revisions import registry_diff
            result['registry_diff'] = registry_diff(context['previous_revision']['study'], result)
            result['registry_diff'].update(previous_revision_id=context['previous_revision']['revision_id'],
                                           revision_id=result['revision_id'])
        checks['coverage'] = coverage_checks(data, context, checks['evidence'])
        checks['document_links'] = {'registered': len(data['claims']), 'matched': len(data['claims']),
                                   'scope': 'REGISTERED_CLAIMS_ONLY'}
        checks['policy_satisfied'] = bool(data['claims'] and evidence_ok and data['completion_status'] == 'COMPLETE'
            and all(c['evidence_ids'] or c['epistemic_kind'] != 'FACT' for c in data['claims'])
            and checks['coverage']['policy_satisfied'])
        result['review_plan'] = freeze_plan(result, context)
        return result
    verify_review_context(context)
    registry = context['claim_registry']
    replies = {c['id']: c for c in data['claims']}
    expected = [c['id'] for c in registry]
    missing = [cid for cid in expected if cid not in replies]
    unchecked = [cid for cid in expected if replies.get(cid, {}).get('outcome') == 'NOT_CHECKED']
    assessed = [cid for cid in expected if cid in replies and cid not in unchecked]
    checks['registry_coverage'] = {'expected_ids': expected, 'received_ids': [cid for cid in expected if cid in replies],
        'assessed_ids': assessed, 'not_checked_ids': unchecked, 'missing_ids': missing,
        'expected_count': len(expected), 'received_count': len(replies), 'assessed_count': len(assessed),
        'fraction': len(assessed) / len(expected) if expected else None, 'scope': 'FROZEN_REGISTRY_ONLY'}
    checks['outcome_counts'] = dict(sorted(Counter(replies.get(cid, {}).get('outcome', 'MISSING') for cid in expected).items()))
    checks['document_links'] = copy.deepcopy(context['architecture_document'].get('program_checks', {}).get('document_links',
        {'registered': 0, 'matched': 0, 'scope': 'REGISTERED_CLAIMS_ONLY'}))
    areas = {a['area_id']: a for a in data['omission_search']}
    area_ids = [a['id'] for a in context['review_plan']['omission_areas']]
    checks['omission_coverage'] = {'expected_ids': area_ids,
        'missing_ids': [a for a in area_ids if a not in areas],
        'unfinished_ids': [a for a in area_ids if a in areas and areas[a]['status'] != 'INSPECTED'],
        'reported_by': 'AGENT', 'completeness_measured': False}
    previous_findings = context['review_plan']['prior_findings']
    previous_replies = {(f['revision_id'], f['finding_id']): f for f in data['prior_findings']}
    checks['prior_finding_coverage'] = {'expected': copy.deepcopy(previous_findings),
        'missing': [f for f in previous_findings if (f['revision_id'], f['finding_id']) not in previous_replies],
        'unresolved': [f for f in previous_findings if (f['revision_id'], f['finding_id']) in previous_replies
                       and previous_replies[(f['revision_id'], f['finding_id'])]['status'] != 'RESOLVED']}
    checks['finding_ids_by_claim'] = {cid: [f['id'] for f in data['findings'] if cid in f['claim_ids']] for cid in expected}
    resolved = {e['id'] for e in checks['evidence'] if e['status'] == 'RESOLVED'}
    resolved |= {e['id'] for e in context['architecture_document'].get('program_checks', {}).get('evidence', [])
                 if e['status'] == 'RESOLVED'}
    refs_ok = all(set(r['evidence_ids']) <= resolved for r in data['claims'] + data['findings'])
    checks['policy_satisfied'] = bool(expected and not missing and not unchecked and evidence_ok and refs_ok
        and context['review_plan']['eligible_study'] and data['completion_status'] == 'COMPLETE'
        and all(c['outcome'] in ('SUPPORTED', 'CAVEAT_ACCEPTABLE') for c in data['claims'])
        and not any(f['severity'] in ('HIGH', 'MEDIUM') for f in data['findings'])
        and not checks['omission_coverage']['missing_ids'] and not checks['omission_coverage']['unfinished_ids']
        and not checks['prior_finding_coverage']['missing'] and not checks['prior_finding_coverage']['unresolved'])
    result['verdict'] = review_verdict(result)
    result['claim_registry'] = copy.deepcopy(registry)
    result['review_plan'] = copy.deepcopy(context['review_plan'])
    result['revision_id'] = context.get('revision_id', '001')
    return result


def completed_study(item):
    """A published, intact study meeting processing policy, independently of review."""
    doc = item.get('study')
    if not doc or item.get('critical_failure'):
        return False
    try:
        from src.reports.document_rendering import validate_materialized
        from src.contracts.saved_contracts import SAVED_SCHEMAS, SAVED_FOLDER_SCHEMAS
        from src.contracts.contracts import validate_schema
        validate_materialized(doc)
        schemas = SAVED_FOLDER_SCHEMAS if 'source_directory' in doc else SAVED_SCHEMAS
        validate_schema(doc, schemas['study'])
    except (ContractError, KeyError, TypeError):
        return False
    if any(k in item and item[k] != doc.get(k) for k in ('branch', 'source_commit', 'source_directory', 'source_fingerprint', 'revision_id')):
        return False
    meta = item.get('study_invocation') or {}
    if (meta.get('contract_id') != CONTRACT_ID or meta.get('artifact_format') != ARTIFACT_FORMAT or
            not has_program_checks(doc) or not doc['program_checks']['policy_satisfied'] or
            doc.get('completion_status') != 'COMPLETE' or not meta.get('publication_complete')):
        return False
    plan = doc.get('review_plan', {})
    try:
        if plan.get('coverage_plan'):
            verify_coverage_plan(plan['coverage_plan'])
            if plan['coverage_plan']['sources'] != plan['sources']:
                return False
        root_identity = plan['sources'][0]['identity']
        source_matches = (root_identity == {'mode': 'folder', 'directory': doc['source_directory'],
                                            'fingerprint': doc['source_fingerprint']} if 'source_directory' in doc else
                          root_identity == {'mode': 'git', 'branch': doc['branch'], 'commit': doc['source_commit']})
        if root_identity.get('snapshot_id') and 'source_directory' not in doc:
            source_matches = (root_identity.get('mode') == 'git'
                and root_identity.get('base_commit') == doc['source_commit']
                and root_identity.get('source_type') in ('commit', 'working_tree')
                and bool(root_identity.get('fingerprint'))
                and ('source_snapshot' not in item or root_identity == dict(mode='git', **item['source_snapshot'])))
        return (plan['document_sha256'] == sha(doc['report_markdown'].encode('utf-8')) and
            plan['registry_sha256'] == sha(canonical(doc['claims'])) and
            plan['source_sha256'] == sha(canonical(plan['sources'])) and
            plan['plan_sha256'] == sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'})) and
            source_matches and doc.get('revision_id') == plan.get('revision_id') and
            (not plan.get('coverage_plan') or plan['coverage_plan']['policy_satisfied']))
    except (ContractError, KeyError, TypeError, IndexError):
        return False


def accepted_pair(item):
    doc, rev = item.get('study'), item.get('review')
    if not rev or not completed_study(item):
        return False
    try:
        from src.contracts.saved_contracts import SAVED_SCHEMAS, SAVED_FOLDER_SCHEMAS
        from src.contracts.contracts import validate_schema
        schemas = SAVED_FOLDER_SCHEMAS if 'source_directory' in doc else SAVED_SCHEMAS
        validate_schema(rev, schemas['review'])
        meta = item.get('review_invocation') or {}
        plan = doc['review_plan']
        return bool(meta.get('contract_id') == CONTRACT_ID and meta.get('artifact_format') == ARTIFACT_FORMAT
            and has_program_checks(rev) and rev['program_checks']['policy_satisfied']
            and rev.get('completion_status') == 'COMPLETE' and meta.get('publication_complete')
            and rev.get('target') == target(plan) and rev.get('review_plan') == plan
            and rev['claim_registry'] == doc['claims'] and rev['verdict'] == 'PASS'
            and rev.get('revision_id') == doc.get('revision_id')
            and all(doc.get(k) == rev.get(k) for k in ('branch', 'source_commit', 'source_directory', 'source_fingerprint')))
    except (ContractError, KeyError, TypeError, IndexError):
        return False
