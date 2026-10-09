# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Backend-independent file batching over a frozen inventory."""
from fractions import Fraction

from src.analysis.coverage_plan import verify_coverage_plan
from src.analysis.evidence import canonical, sha
from src.contracts.contracts import ContractError

MAX_SUBSYSTEMS_PER_SESSION = 4
MAX_SOURCE_FILES_PER_SESSION = 300
MAX_SOURCE_LINES_PER_SESSION = 50_000


def build_analysis_plan(inventory, coverage_plan, multi_session=True):
    if type(multi_session) is not bool:
        raise ContractError('multi_session must be boolean.')
    verify_coverage_plan(coverage_plan)
    if coverage_plan['inventory_sha256'] != sha(canonical(inventory['entries'])):
        raise ContractError('Analysis and coverage inventories differ.')
    # Keep the full inventory seal, but size only nonexcluded regular files.
    # UNCLASSIFIED files still count; overlapping exclusions count once.
    excluded = {path for area in coverage_plan['exclusions'] for path in area['file_paths']}
    files = {e['path']: e for e in inventory['entries'] if e['type'] == 'file' and e['path'] not in excluded}
    if any(type(e.get('source_lines')) is not int or e['source_lines'] < 0 for e in files.values()):
        raise ContractError('Inventory is missing physical line counts.')
    areas = coverage_plan['areas']
    memberships = {a['id']: set(a['file_paths']) for a in areas}
    if set().union(*memberships.values()) != set(files):
        raise ContractError('Coverage areas do not match eligible inventory files.')
    capacities = (MAX_SUBSYSTEMS_PER_SESSION, MAX_SOURCE_FILES_PER_SESSION, MAX_SOURCE_LINES_PER_SESSION)
    totals = (len(areas), len(files), sum(e['source_lines'] for e in files.values()))
    required = max(1, *((size + cap - 1) // cap for size, cap in zip(totals, capacities))) if multi_session else 1

    def size(paths):
        return len(paths), sum(files[p]['source_lines'] for p in paths)

    def load(count, paths):
        return max(Fraction(n, cap) for n, cap in zip((count, *size(paths)), capacities))

    ordered = sorted(memberships, key=lambda sid: (-load(1, memberships[sid]), sid))
    bins = [{'subsystem_ids': set(), 'paths': set()} for _ in range(required)]
    for sid in ordered:
        if not memberships[sid]:
            continue
        batches, batch, lines = [], set(), 0
        for path in sorted(memberships[sid]):
            count = files[path]['source_lines']
            if multi_session and batch and (len(batch) == MAX_SOURCE_FILES_PER_SESSION
                    or lines + count > MAX_SOURCE_LINES_PER_SESSION):
                batches.append(batch)
                batch, lines = set(), 0
            batch.add(path)
            lines += count
        batches.append(batch)
        for paths in batches:
            candidates = [b for b in bins if not multi_session or not b['paths'] or
                          load(len(b['subsystem_ids'] | {sid}), b['paths'] | paths) <= 1]
            if not candidates:
                bins.append({'subsystem_ids': set(), 'paths': set()})
                candidates = [bins[-1]]
            shard = min(candidates, key=lambda b: load(len(b['subsystem_ids']), b['paths']))
            shard['subsystem_ids'].add(sid)
            # Overlapping catalog hints can share work; physical paths count only
            # once within a shard and once in the global inventory totals.
            shard['paths'].update(paths)
    bins = [b for b in bins if b['paths']] or [bins[0]]
    # Empty/excluded areas still need a coverage report, not their own session.
    for sid in sorted(sid for sid in memberships if not memberships[sid]):
        min(bins, key=lambda b: load(len(b['subsystem_ids']), b['paths']))['subsystem_ids'].add(sid)
    shards = []
    for i, shard in enumerate(bins):
        count = len(shard['subsystem_ids'])
        file_count, lines = size(shard['paths'])
        weight = load(count, shard['paths'])
        shards.append(dict(id=f'R-{i + 1:03d}', subsystem_ids=sorted(shard['subsystem_ids']),
            primary_file_paths=sorted(shard['paths']),
            oversized_file_paths=sorted(p for p in shard['paths'] if files[p]['source_lines'] > MAX_SOURCE_LINES_PER_SESSION),
            subsystem_count=count, source_files=file_count, source_lines=lines,
            normalized_load=float(weight), over_capacity=weight > 1))
    plan = {'multi_session': multi_session, 'required_sessions': len(shards),
        'thresholds': dict(zip(('max_subsystems_per_session', 'max_source_files_per_session',
                               'max_source_lines_per_session'), capacities)),
        'metric': 'nonexcluded_regular_files; LF_count_plus_unterminated_final_line',
        'inventory_sha256': coverage_plan['inventory_sha256'], 'coverage_plan_sha256': coverage_plan['plan_sha256'],
        'totals': dict(zip(('subsystems', 'source_files', 'source_lines'), totals)),
        'subsystems': [dict(subsystem_id=sid, file_count=size(memberships[sid])[0],
                            source_lines=size(memberships[sid])[1]) for sid in sorted(memberships)],
        'shards': shards}
    return plan | {'plan_sha256': sha(canonical(plan))}


def verify_analysis_plan(plan, inventory, coverage_plan):
    """Rebuild locally to check assignment, metrics, ordering, thresholds and seal."""
    if type(plan) is not dict or canonical(plan) != canonical(build_analysis_plan(inventory, coverage_plan, plan.get('multi_session'))):
        raise ContractError('Frozen analysis plan changed or is inconsistent.')
    return plan
