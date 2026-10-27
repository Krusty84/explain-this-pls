# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Inventory defines source scope; citations never establish inspection."""
from __future__ import annotations

from src.messages import message as ui_message
from src.contracts.contracts import ARTIFACT_FORMAT, CONTRACT_ID, ContractError
from src.analysis.evidence import canonical, sha, source_catalog, valid_path


def checked_path(path):
    if path != '.' and not valid_path(path):
        raise ContractError(ui_message('diagnostic.coverage_plan.coverage_plan_contains_an_unsafe_inventory_path'))
    return path


def group_path(path):
    return path.split('/')[0] if '/' in path else '.'


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


AREA_PURPOSE = ui_message('diagnostic.coverage_plan.source_inventory_entries_in_this_directory_group_inspection')


def build_coverage_plan(inventory, context):
    """Build a reproducible navigation plan from the already filtered inventory."""
    entries = inventory['entries']
    summary = inventory_summary(inventory)
    groups = {group['path']: [] for group in summary['directories']}
    files = set()
    for entry in entries:
        path = checked_path(entry['path'])
        if entry['type'] in ('file', 'symlink'):
            groups[group_path(path)].append(path)
            if entry['type'] == 'file':
                files.add(path)
    areas = [dict(id=f'S-{index:03d}', name=ui_message('diagnostic.coverage_plan.source_root') if path == '.' else path,
                  purpose=AREA_PURPOSE, paths=[path], entry_paths=sorted(members),
                  file_paths=sorted(files.intersection(members)), required=bool(members))
             for index, (path, members) in enumerate(sorted(groups.items()), 1)]
    plan = {'contract_id': CONTRACT_ID, 'artifact_format': ARTIFACT_FORMAT, 'origin': 'SOURCE_INVENTORY',
            'sources': source_catalog(context), 'inventory_sha256': sha(canonical(entries)),
            'areas': areas, 'counts': {'files': summary['files'], 'symlinks': summary['symlinks'],
                'directories': summary['directory_count'], 'empty_directories': summary['empty_directories']}}
    return plan | {'plan_sha256': sha(canonical(plan))}


def verify_coverage_plan(plan):
    from src.contracts.saved_contracts import COVERAGE_PLAN
    from src.contracts.contracts import validate_schema
    validate_schema(plan, COVERAGE_PLAN)
    if (plan['contract_id'] != CONTRACT_ID or plan['artifact_format'] != ARTIFACT_FORMAT
            or plan['origin'] != 'SOURCE_INVENTORY'
            or plan['plan_sha256'] != sha(canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))):
        raise ContractError(ui_message('diagnostic.coverage_plan.frozen_coverage_plan_changed'))
    paths = [area['paths'][0] for area in plan['areas'] if len(area['paths']) == 1]
    if len(paths) != len(plan['areas']) or paths != sorted(set(paths)) or '.' not in paths:
        raise ContractError(ui_message('diagnostic.coverage_plan.coverage_plan_directory_groups_changed'))
    members, files = set(), set()
    for index, area in enumerate(plan['areas'], 1):
        path = checked_path(area['paths'][0])
        selected = area['entry_paths']
        selected_files = area['file_paths']
        if (area['id'] != f'S-{index:03d}' or path != '.' and '/' in path
                or area['name'] != ('Source root' if path == '.' else path)
                or area['purpose'] != AREA_PURPOSE
                or selected != sorted(set(selected)) or selected_files != sorted(set(selected_files))
                or not set(selected_files) <= set(selected) or members.intersection(selected)
                or area['required'] != bool(selected)):
            raise ContractError(ui_message('diagnostic.coverage_plan.coverage_plan_inventory_groups_changed'))
        for member in selected:
            checked_path(member)
            if member == '.' or group_path(member) != path:
                raise ContractError(ui_message('diagnostic.coverage_plan.coverage_plan_membership_is_inconsistent'))
        members.update(selected)
        files.update(selected_files)
    if plan['counts']['files'] != len(files) or plan['counts']['symlinks'] != len(members - files):
        raise ContractError(ui_message('diagnostic.coverage_plan.coverage_plan_unique_file_counts_changed'))
    return plan


def study_coverage_plan(data, context):
    """Model metadata cannot change the scope bound to Study, Review, or Revision."""
    frozen = context.get('coverage_plan')
    if frozen is not None:
        verify_coverage_plan(frozen)
        if frozen['sources'] != source_catalog(context):
            raise ContractError(ui_message('diagnostic.coverage_plan.coverage_plan_identifies_another_source_snapshot'))
    inventory = context.get('_inventory')
    if inventory is None:
        return frozen
    plan = build_coverage_plan(inventory, context)
    if frozen is not None and plan != frozen:
        raise ContractError(ui_message('diagnostic.coverage_plan.coverage_and_pinned_source_inventories_differ'))
    return plan


def coverage_checks(data, context, resolutions):
    """Report citation distribution and pinned metadata without judging the document."""
    plan = study_coverage_plan(data, context)
    areas = plan['areas'] if plan is not None else []
    sources = {source['id']: source for source in source_catalog(context)} if plan is not None else {}
    cited_paths = set()
    for evidence in resolutions:
        source = sources.get(evidence['source_id'])
        if evidence['status'] == 'RESOLVED' and source is not None:
            prefix = source['identity'].get('submodule_path', '')
            cited_paths.add(prefix + '/' + evidence['path'] if prefix else evidence['path'])
    expected = [area['id'] for area in areas]
    cited = [area['id'] for area in areas if cited_paths.intersection(area['file_paths'])]
    inventory = context.get('_inventory')
    metadata_paths = sorted(entry['path'] for entry in inventory['entries']
        if entry['type'] == 'file' and type(entry.get('size')) is int and entry['size'] == 0
        and entry.get('sha256') == sha(b'')) if inventory is not None else []
    return {'expected_ids': expected, 'cited_ids': cited,
            'not_cited_ids': [identifier for identifier in expected if identifier not in cited],
            'unverified_ids': expected, 'metadata_verified_empty_paths': metadata_paths,
            'reported_by': 'ORCHESTRATOR', 'completeness_measured': False}


def advisory_diagnostics(observations, context):
    """Retain observations as submitted; auxiliary defects are warnings only."""
    from src.contracts.contracts import SUBSYSTEMS, EXCLUSIONS, COVERAGE, schema_diagnostics
    diagnostics = []

    def warn(path, code, message):
        diagnostics.append({'path': path, 'code': code, 'message': message})

    if type(observations) is not dict:
        warn('$.advisory', 'INVALID_ADVISORY_DATA', ui_message('diagnostic.coverage_plan.auxiliary_observations_must_be_an_object'))
        return diagnostics
    for key in ('advisory', 'relevance_notes'):
        if key in observations:
            warn('$.' + key, 'UNVERIFIED_ADVISORY_DATA',
                 ui_message('diagnostic.coverage_plan.free_form_observations_are_retained_without_verifying_classification'))
    for key, schema in (('subsystems', SUBSYSTEMS), ('exclusions', EXCLUSIONS), ('coverage', COVERAGE)):
        if key in observations:
            defects = schema_diagnostics(observations[key], schema, '$.' + key)
            for defect in defects['violations']:
                warn(defect['path'], 'INVALID_ADVISORY_DATA',
                     ui_message('diagnostic.coverage_plan.auxiliary_observation_does_not_match_its_legacy_schema'))
    inventory = context.get('_inventory')
    known = {entry['path'] for entry in inventory['entries']} if inventory is not None else None

    def check_path(path, location):
        if path != '.' and not valid_path(path):
            warn(location, 'UNSAFE_COVERAGE_PATH', ui_message(
                'diagnostic.coverage_plan.auxiliary_path_has_invalid_source_path_syntax',
            ))
        elif known is None:
            warn(location, 'UNVERIFIED_COVERAGE_PATH', ui_message(
                'diagnostic.coverage_plan.no_pinned_inventory_is_available_to_verify_this',
            ))
        elif path not in known:
            warn(location, 'UNKNOWN_COVERAGE_PATH', ui_message(
                'diagnostic.coverage_plan.auxiliary_path_is_absent_from_the_source_inventory',
            ))

    subsystems = observations.get('subsystems')
    if type(subsystems) is list:
        for index, subsystem in enumerate(subsystems):
            if type(subsystem) is dict and type(subsystem.get('paths')) is list:
                for path_index, path in enumerate(subsystem['paths']):
                    if type(path) is str:
                        check_path(path, f'$.subsystems[{index}].paths[{path_index}]')
    if 'exclusions' in observations:
        exclusions = observations['exclusions']
        if type(exclusions) is not list:
            exclusions = [exclusions]
        for index, exclusion in enumerate(exclusions):
            location = f'$.exclusions[{index}]'
            warn(location, 'MODEL_EXCLUSION_IGNORED',
                 ui_message('diagnostic.coverage_plan.model_exclusion_cannot_change_the_source_inventory_only'))
            if type(exclusion) is dict and type(exclusion.get('path')) is str:
                check_path(exclusion['path'], location + '.path')
    return diagnostics
