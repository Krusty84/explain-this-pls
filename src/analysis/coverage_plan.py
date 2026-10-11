# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Reconcile study areas with inventory; assignment is not inspection."""
from __future__ import annotations
import copy
import re

from src.contracts.contracts import CONTRACT_ID, ContractError, contract_violation
from src.analysis.evidence import canonical, sha, source_catalog, valid_path


def checked_path(path, *, directories=None):
    """Validate syntax; supplied inventory directories constrain suffix aliases."""
    if (type(path) is str and path.endswith('/') and valid_path(path[:-1])
            and (directories is None or path[:-1] in directories)):
        path = path[:-1]
    if path != '.' and not valid_path(path):
        raise contract_violation('UNSAFE_COVERAGE_PATH', '$.paths')
    return path


def matches(path, root):
    return root == '.' or path == root or path.startswith(root + '/')


def inventory_summary(inventory):
    entries = inventory['entries']
    directories = {e['path'] for e in entries if e['type'] == 'directory'}
    parents = {e['path'].rsplit('/', 1)[0] if '/' in e['path'] else '.' for e in entries if e['path'] != '.'}
    empty = directories - parents
    groups = {p: {'path': p, 'files': 0, 'symlinks': 0, 'directories': 0, 'empty_directories': 0}
              for p in sorted({'.'} | {p.split('/')[0] for p in directories if p != '.'})}
    for entry in entries:
        path, kind = entry['path'], entry['type']
        parent = path.split('/')[0] if '/' in path or (kind == 'directory' and path != '.') else '.'
        group = groups[parent]
        if kind in ('file', 'symlink'):
            group['files' if kind == 'file' else 'symlinks'] += 1
        elif kind == 'directory':
            group['directories'] += 1
            group['empty_directories'] += path in empty
    return {'files': sum(e['type'] == 'file' for e in entries),
            'symlinks': sum(e['type'] == 'symlink' for e in entries),
            'directory_count': len(directories), 'empty_directories': len(empty),
            'directories': [groups[p] for p in sorted(groups)]}


def build_coverage_plan(subsystems, exclusions, inventory, context):
    from src.contracts.contracts import SUBSYSTEMS, EXCLUSIONS, validate_schema, nonblank, unique_ids
    validate_schema(subsystems, SUBSYSTEMS, '$.subsystems')
    validate_schema(exclusions, EXCLUSIONS, '$.exclusions')
    nonblank(subsystems)
    nonblank(exclusions)
    unique_ids(subsystems, r'S-[0-9]{3,}', '$.subsystems')
    entries = inventory['entries']
    known = {e['path']: e for e in entries}
    directories = {p for p, e in known.items() if e['type'] == 'directory'}
    leaves = {p for p, e in known.items() if e['type'] in ('file', 'symlink')}
    files = {p for p, e in known.items() if e['type'] == 'file'}

    def members(paths):
        paths = [checked_path(path, directories=directories) for path in paths]
        if len(paths) != len(set(paths)) or not paths:
            raise contract_violation('INVALID_COVERAGE_PATHS', '$.paths')
        for path in paths:
            if path not in known:
                raise contract_violation('UNKNOWN_COVERAGE_PATH', '$.paths')
        return paths, {p for p in leaves if any(matches(p, root) for root in paths)}

    expanded_exclusions, excluded = [], set()
    seen_exclusions = set()
    for exclusion in exclusions:
        path = checked_path(exclusion['path'], directories=directories)
        if path in seen_exclusions or not exclusion['reason'].strip():
            raise contract_violation('INVALID_COVERAGE_EXCLUSION', '$.exclusions')
        seen_exclusions.add(path)
        _, selected = members([path])
        excluded |= selected
        expanded_exclusions.append(dict(exclusion, path=path, entry_paths=sorted(selected), file_paths=sorted(selected & files)))
    areas, assigned = [], set()
    for subsystem in subsystems:
        paths, selected = members(subsystem['paths'])
        selected -= excluded
        assigned |= selected
        areas.append(dict(copy.deepcopy(subsystem), paths=paths, entry_paths=sorted(selected),
                          file_paths=sorted(selected & files), required=bool(selected)))
    unclassified = leaves - assigned - excluded
    if unclassified:
        areas.append({'id': 'UNCLASSIFIED', 'name': 'Unclassified source entries',
                      'purpose': 'Investigate entries not classified or explicitly excluded by the study.',
                      'paths': sorted(unclassified), 'entry_paths': sorted(unclassified),
                      'file_paths': sorted(unclassified & files), 'required': True})
    summary = inventory_summary(inventory)
    plan = {'contract_id': CONTRACT_ID,
            'sources': source_catalog(context), 'inventory_sha256': sha(canonical(entries)),
            'areas': areas, 'exclusions': expanded_exclusions, 'unclassified_paths': sorted(unclassified),
            'counts': {'files': len(files), 'symlinks': len(leaves - files),
                       'directories': summary['directory_count'], 'empty_directories': summary['empty_directories'],
                       'assigned_files': len(assigned & files), 'excluded_files': len(excluded & files),
                       'unclassified_files': len(unclassified & files),
                       'overlapping_files': sum(sum(p in a['file_paths'] for a in areas) > 1 for p in files)},
            'policy_satisfied': not unclassified}
    return plan | {'plan_sha256': sha(canonical(plan))}


def verify_coverage_plan(plan):
    from src.contracts.saved_contracts import COVERAGE_PLAN
    from src.contracts.contracts import validate_schema
    validate_schema(plan, COVERAGE_PLAN)
    if (type(plan) is not dict or plan.get('contract_id') != CONTRACT_ID
            or plan.get('plan_sha256') != sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))):
        raise ContractError('Frozen coverage plan changed.')
    identifiers = [a['id'] for a in plan['areas']]
    if (len(identifiers) != len(set(identifiers)) or
            any(identifier != 'UNCLASSIFIED' and not re.fullmatch(r'S-[0-9]{3,}', identifier) for identifier in identifiers)):
        raise ContractError('Coverage plan has invalid or duplicate area IDs.')
    assigned, excluded, files = set(), set(), set()
    for record in plan['areas'] + plan['exclusions']:
        for path in record['entry_paths'] + record['file_paths']:
            checked_path(path, directories=())
        entries = set(record['entry_paths'])
        if len(entries) != len(record['entry_paths']) or not set(record['file_paths']) <= entries:
            raise ContractError('Coverage plan membership is inconsistent.')
        files.update(record['file_paths'])
        if 'id' in record:
            if record['required'] != bool(entries):
                raise ContractError('Coverage plan required areas changed.')
            if record['id'] != 'UNCLASSIFIED':
                assigned.update(entries)
        else:
            excluded.update(entries)
    unclassified = set(plan['unclassified_paths'])
    unclassified_areas = [a for a in plan['areas'] if a['id'] == 'UNCLASSIFIED']
    if (assigned & excluded or unclassified & (assigned | excluded)
            or bool(unclassified_areas) != bool(unclassified)
            or (unclassified_areas and set(unclassified_areas[0]['entry_paths']) != unclassified)):
        raise ContractError('Coverage plan assignments changed.')
    expected_policy = not unclassified
    expected_counts = {'files': len(files), 'symlinks': len((assigned | excluded | unclassified) - files),
        'assigned_files': len(assigned & files), 'excluded_files': len(excluded & files),
        'unclassified_files': len(unclassified & files),
        'overlapping_files': sum(sum(p in a['file_paths'] for a in plan['areas']) > 1 for p in files)}
    if plan['policy_satisfied'] != expected_policy or any(plan['counts'][k] != v for k, v in expected_counts.items()):
        raise ContractError('Coverage plan policy or unique-file counts changed.')
    return plan


def study_coverage_plan(data, context):
    """Derive scope without changing the invocation's bound input context."""
    from src.contracts.contracts import unique_ids
    unique_ids(data['subsystems'], r'S-[0-9]{3,}', '$.subsystems')
    inventory = context.get('_inventory')
    directories = ({e['path'] for e in inventory['entries'] if e['type'] == 'directory'}
                   if inventory is not None else None)
    subsystems = copy.deepcopy(data['subsystems'])
    exclusions = copy.deepcopy(data['exclusions'])
    for subsystem in subsystems:
        subsystem['paths'] = [checked_path(p, directories=directories) for p in subsystem['paths']]
        if not subsystem['paths'] or len(set(subsystem['paths'])) != len(subsystem['paths']):
            raise contract_violation('INVALID_COVERAGE_PATHS', '$.subsystems')
    for exclusion in exclusions:
        exclusion['path'] = checked_path(exclusion['path'], directories=directories)
    if len({e['path'] for e in exclusions}) != len(exclusions):
        raise contract_violation('INVALID_COVERAGE_EXCLUSION', '$.exclusions')
    frozen = context.get('coverage_plan')
    if frozen is not None:
        verify_coverage_plan(frozen)
        expected_subsystems = [{k: a[k] for k in ('id', 'name', 'purpose', 'paths')}
                               for a in frozen['areas'] if a['id'] != 'UNCLASSIFIED']
        expected_exclusions = [{k: e[k] for k in ('path', 'reason')} for e in frozen['exclusions']]
        if subsystems != expected_subsystems or exclusions != expected_exclusions:
            raise contract_violation('COVERAGE_SCOPE_CHANGED', '$.subsystems')
    if inventory is None:  # Direct validation can use a supplied frozen scope.
        return frozen
    plan = build_coverage_plan(subsystems, exclusions, inventory, context)
    if frozen is not None and plan != frozen:
        raise contract_violation('COVERAGE_SCOPE_CHANGED', '$.subsystems')
    return plan


def coverage_checks(data, context, resolutions):
    plan = context.get('coverage_plan')
    if plan is None:  # Direct library callers can validate a study without orchestration.
        return {'expected_ids': [], 'missing_ids': [], 'unfinished_ids': [], 'unsupported_ids': [],
                'metadata_verified_empty_paths': [],
                'reported_by': 'AGENT', 'completeness_measured': False, 'policy_satisfied': True}
    verify_coverage_plan(plan)
    areas = plan['areas']
    reports = {r['area_id']: r for r in data['coverage']}
    resolved = {e['id']: e for e in resolutions if e['status'] == 'RESOLVED'}
    sources = {s['id']: s for s in source_catalog(context)}
    inventory = context.get('_inventory')
    if inventory is not None and sha(canonical(inventory['entries'])) != plan['inventory_sha256']:
        raise ContractError('Coverage and pinned source inventories differ.')
    entries = {entry['path']: entry for entry in inventory['entries']} if inventory is not None else {}
    metadata_paths = set()

    def empty_scope(area):
        paths = set(area['entry_paths'])
        if not paths or not all(entries.get(path, {}).get('type') == 'file'
                and type(entries[path].get('size')) is int and entries[path]['size'] == 0
                and entries[path].get('sha256') == sha(b'') for path in paths):
            return False
        metadata_paths.update(paths)
        return True

    def within(evidence, area):
        source = sources.get(evidence['source_id'], {})
        prefix = source.get('identity', {}).get('submodule_path', '')
        path = prefix + '/' + evidence['path'] if prefix else evidence['path']
        return path in area['file_paths']

    expected = [a['id'] for a in areas]
    required = {a['id'] for a in areas if a['required']}
    unsupported = [a['id'] for a in areas if a['id'] in reports
                   and reports[a['id']]['status'] == 'INSPECTED'
                   and not empty_scope(a)
                   and not any(ref in resolved and within(resolved[ref], a)
                               for ref in reports[a['id']]['evidence_ids'])]
    missing = [area for area in expected if area not in reports]
    unfinished = [area for area in expected if area in required and area in reports and reports[area]['status'] != 'INSPECTED']
    return {'expected_ids': expected, 'missing_ids': missing, 'unfinished_ids': unfinished,
            'unsupported_ids': unsupported, 'metadata_verified_empty_paths': sorted(metadata_paths),
            'reported_by': 'AGENT', 'completeness_measured': False,
            'policy_satisfied': plan['policy_satisfied']
                                and not missing and not unfinished and not unsupported}
