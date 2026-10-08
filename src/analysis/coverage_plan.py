# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Deterministic catalog reconciliation; file assignment is not understanding."""
from __future__ import annotations
import copy
import re

from src.contracts.contracts import CONTRACT_ID, ContractError, contract_violation
from src.analysis.evidence import canonical, sha, source_catalog, valid_path


def checked_path(path):
    if path != '.' and not valid_path(path):
        raise contract_violation('UNSAFE_CATALOG_PATH', '$.paths')
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


def recover_catalog_paths(catalog, inventory, context):
    """Drop unknown selectors only after all other catalog checks pass."""
    from src.contracts.contracts import validate_result
    mode = 'folder' if context.get('source_mode') == 'folder' or 'source_directory' in context else 'git'
    validate_result('catalog', catalog, {k: v for k, v in context.items() if k != '_inventory'}, mode)
    exclusions = [e['path'] for e in catalog['exclusions']]
    if len(exclusions) != len(set(exclusions)):
        raise contract_violation('INVALID_CATALOG_EXCLUSION', '$.exclusions')
    known = {e['path'] for e in inventory['entries']}
    rejected = []

    def keep(selector, location):
        if selector in known:
            return True
        rejected.append({'code': 'UNKNOWN_CATALOG_PATH', 'path': location, 'selector': selector})
        return False

    recovered = copy.deepcopy(catalog)
    for i, subsystem in enumerate(recovered['subsystems']):
        subsystem['paths'] = [p for j, p in enumerate(subsystem['paths'])
                              if keep(p, f'$.subsystems[{i}].paths[{j}]')]
    recovered['subsystems'] = [s for s in recovered['subsystems'] if s['paths']]
    recovered['exclusions'] = [e for i, e in enumerate(recovered['exclusions'])
                               if keep(e['path'], f'$.exclusions[{i}].path')]
    if not rejected:
        return catalog, None
    recovered['completion_status'] = 'PARTIAL'
    recovered['limitations'].append('Unknown catalog paths were discarded; catalog acceptance is unavailable.')
    provenance = {'origin': 'AGENT_SALVAGED' if recovered['subsystems'] else 'DIRECTORY_FALLBACK',
                  'hash_format': 'canonical-json-utf8', 'input_sha256': sha(canonical(catalog)),
                  'recovered_sha256': sha(canonical(recovered)),
                  'completion_self_assessment': catalog['completion_status'],
                  'rejected_selectors': rejected}
    return recovered, provenance


def build_coverage_plan(catalog, inventory, context, fallback=False, *, salvaged=False):
    entries = inventory['entries']
    known = {e['path']: e for e in entries}
    leaves = {p for p, e in known.items() if e['type'] in ('file', 'symlink')}
    files = {p for p, e in known.items() if e['type'] == 'file'}
    if not fallback or catalog is not None:
        if type(catalog) is not dict:
            raise ContractError('Catalog is unavailable.')
        from src.contracts.contracts import validate_wire_identity, nonblank, unique_ids
        mode = 'folder' if context.get('source_mode') == 'folder' or 'source_directory' in context else 'git'
        validate_wire_identity('catalog', catalog, context, mode)
        nonblank(catalog)
        unique_ids(catalog['subsystems'], r'S-[0-9]{3,}', '$.subsystems')
        if catalog['completion_status'] != 'COMPLETE' and not catalog['limitations']:
            raise ContractError('PARTIAL/BLOCKED catalog requires explicit limitations.')
    if fallback:
        roots = sorted({p.split('/')[0] for p in leaves})
        catalog = {'completion_status': 'PARTIAL',
                   'limitations': copy.deepcopy((catalog or {}).get('limitations', [])) +
                                  ['Directory fallback; usable agent catalog unavailable.'],
                   'subsystems': [{'id': f'S-{i + 1:03d}', 'name': p,
                                   'purpose': 'Directory fallback grouping.', 'paths': [p]}
                                  for i, p in enumerate(roots)],
                   'exclusions': copy.deepcopy((catalog or {}).get('exclusions', []))}
    subsystems = catalog['subsystems']
    ids = [s['id'] for s in subsystems]
    if len(ids) != len(set(ids)) or 'UNCLASSIFIED' in ids:
        raise contract_violation('DUPLICATE_CATALOG_AREA', '$.subsystems')

    def members(paths):
        if len(paths) != len(set(paths)) or not paths:
            raise contract_violation('INVALID_CATALOG_PATHS', '$.paths')
        for path in paths:
            checked_path(path)
            if path not in known:
                raise contract_violation('UNKNOWN_CATALOG_PATH', '$.paths')
        return {p for p in leaves if any(matches(p, root) for root in paths)}

    exclusions, excluded = [], set()
    seen_exclusions = set()
    for exclusion in catalog['exclusions']:
        if exclusion['path'] in seen_exclusions or not exclusion['reason'].strip():
            raise contract_violation('INVALID_CATALOG_EXCLUSION', '$.exclusions')
        seen_exclusions.add(exclusion['path'])
        selected = members([exclusion['path']])
        excluded |= selected
        exclusions.append(dict(exclusion, entry_paths=sorted(selected), file_paths=sorted(selected & files)))
    areas, assigned = [], set()
    for subsystem in subsystems:
        selected = members(subsystem['paths']) - excluded
        assigned |= selected
        areas.append(dict(copy.deepcopy(subsystem), entry_paths=sorted(selected),
                          file_paths=sorted(selected & files), required=bool(selected)))
    unclassified = leaves - assigned - excluded
    if unclassified:
        areas.append({'id': 'UNCLASSIFIED', 'name': 'Unclassified source entries',
                      'purpose': 'Entries not assigned or explicitly excluded by the catalog.',
                      'paths': sorted(unclassified), 'entry_paths': sorted(unclassified),
                      'file_paths': sorted(unclassified & files), 'required': True})
    summary = inventory_summary(inventory)
    plan = {'contract_id': CONTRACT_ID,
            'origin': 'DIRECTORY_FALLBACK' if fallback else 'AGENT_SALVAGED' if salvaged else 'AGENT',
            'catalog_status': catalog['completion_status'], 'limitations': copy.deepcopy(catalog['limitations']),
            'sources': source_catalog(context), 'inventory_sha256': sha(canonical(entries)),
            'areas': areas, 'exclusions': exclusions, 'unclassified_paths': sorted(unclassified),
            'counts': {'files': len(files), 'symlinks': len(leaves - files),
                       'directories': summary['directory_count'], 'empty_directories': summary['empty_directories'],
                       'assigned_files': len(assigned & files), 'excluded_files': len(excluded & files),
                       'unclassified_files': len(unclassified & files),
                       'overlapping_files': sum(sum(p in a['file_paths'] for a in areas) > 1 for p in files)},
            'policy_satisfied': not (fallback or salvaged) and catalog['completion_status'] == 'COMPLETE' and not unclassified}
    return plan | {'plan_sha256': sha(canonical(plan))}


def verify_coverage_plan(plan):
    from src.contracts.saved_contracts import COVERAGE_PLAN
    from src.contracts.contracts import validate_schema
    validate_schema(plan, COVERAGE_PLAN)
    if (type(plan) is not dict or plan.get('contract_id') != CONTRACT_ID
            or plan.get('plan_sha256') != sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))):
        raise ContractError('Frozen coverage plan changed.')
    identifiers = [a['id'] for a in plan['areas']]
    if plan['origin'] != 'AGENT' and (plan['catalog_status'] != 'PARTIAL' or not plan['limitations']):
        raise ContractError('Recovered and fallback coverage plans require PARTIAL status and limitations.')
    if (len(identifiers) != len(set(identifiers)) or
            any(identifier != 'UNCLASSIFIED' and not re.fullmatch(r'S-[0-9]{3,}', identifier) for identifier in identifiers)):
        raise ContractError('Coverage plan has invalid or duplicate area IDs.')
    assigned, excluded, files = set(), set(), set()
    for record in plan['areas'] + plan['exclusions']:
        for path in record['entry_paths'] + record['file_paths']:
            checked_path(path)
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
    expected_policy = plan['origin'] == 'AGENT' and plan['catalog_status'] == 'COMPLETE' and not unclassified
    expected_counts = {'files': len(files), 'symlinks': len((assigned | excluded | unclassified) - files),
        'assigned_files': len(assigned & files), 'excluded_files': len(excluded & files),
        'unclassified_files': len(unclassified & files),
        'overlapping_files': sum(sum(p in a['file_paths'] for a in plan['areas']) > 1 for p in files)}
    if plan['policy_satisfied'] != expected_policy or any(plan['counts'][k] != v for k, v in expected_counts.items()):
        raise ContractError('Coverage plan policy or unique-file counts changed.')
    return plan


def coverage_checks(data, context, resolutions, *, area_ids=None):
    plan = context.get('coverage_plan')
    if plan is None:  # Direct library callers can validate a study without orchestration.
        return {'expected_ids': [], 'missing_ids': [], 'unfinished_ids': [], 'unsupported_ids': [],
                'reported_by': 'AGENT', 'completeness_measured': False, 'policy_satisfied': True}
    verify_coverage_plan(plan)
    areas = [a for a in plan['areas'] if area_ids is None or a['id'] in area_ids]
    reports = {r['area_id']: r for r in data['coverage']}
    resolved = {e['id']: e for e in resolutions if e['status'] == 'RESOLVED'}
    sources = {s['id']: s for s in source_catalog(context)}

    def within(evidence, area):
        source = sources.get(evidence['source_id'], {})
        prefix = source.get('identity', {}).get('submodule_path', '')
        path = prefix + '/' + evidence['path'] if prefix else evidence['path']
        return path in area['file_paths']

    expected = [a['id'] for a in areas]
    required = {a['id'] for a in areas if a['required']}
    unsupported = [a['id'] for a in areas if a['id'] in reports
                   and reports[a['id']]['status'] == 'INSPECTED'
                   and not any(ref in resolved and within(resolved[ref], a)
                               for ref in reports[a['id']]['evidence_ids'])]
    missing = [area for area in expected if area not in reports]
    unfinished = [area for area in expected if area in required and area in reports and reports[area]['status'] != 'INSPECTED']
    return {'expected_ids': expected, 'missing_ids': missing, 'unfinished_ids': unfinished,
            'unsupported_ids': unsupported, 'reported_by': 'AGENT', 'completeness_measured': False,
            'policy_satisfied': (area_ids is not None or plan['policy_satisfied'])
                                and not missing and not unfinished and not unsupported}
