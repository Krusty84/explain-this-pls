# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Backend-independent whole-subsystem planning over a frozen inventory."""
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
    # No authoritative source-language classification exists. Use all regular
    # files in the analyzed inventory; Git preparation already excludes ignored
    # untracked files. Catalog exclusions do not change the global size metric.
    files = {e['path']: e for e in inventory['entries'] if e['type'] == 'file'}
    if any(type(e.get('source_lines')) is not int or e['source_lines'] < 0 for e in files.values()):
        raise ContractError('Inventory is missing physical line counts.')
    areas = [a for a in coverage_plan['areas'] if a['id'] != 'UNCLASSIFIED']
    capacities = (MAX_SUBSYSTEMS_PER_SESSION, MAX_SOURCE_FILES_PER_SESSION, MAX_SOURCE_LINES_PER_SESSION)
    totals = (len(areas), len(files), sum(e['source_lines'] for e in files.values()))
    required = max(1, *((size + cap - 1) // cap for size, cap in zip(totals, capacities))) if multi_session else 1

    def size(paths):
        return len(paths), sum(files[p]['source_lines'] for p in paths)

    def load(count, paths):
        return max(Fraction(n, cap) for n, cap in zip((count, *size(paths)), capacities))

    memberships = {a['id']: set(a['file_paths']) for a in areas}
    ordered = sorted(memberships, key=lambda sid: (-load(1, memberships[sid]), sid))
    bins = [{'id': f'R-{i + 1:03d}', 'subsystem_ids': [], 'paths': set()} for i in range(required)]
    for sid in ordered:
        shard = min(bins, key=lambda b: (load(len(b['subsystem_ids']), b['paths']), b['id']))
        shard['subsystem_ids'].append(sid)
        # A physical inventory path counts once per shard, even across overlapping
        # subsystems. Shard totals may overlap across shards; global totals never do.
        shard['paths'].update(memberships[sid])
    shards = []
    for shard in bins:
        count = len(shard['subsystem_ids'])
        file_count, lines = size(shard['paths'])
        weight = load(count, shard['paths'])
        shards.append(dict(id=shard['id'], subsystem_ids=sorted(shard['subsystem_ids']),
            subsystem_count=count, source_files=file_count, source_lines=lines,
            normalized_load=float(weight), over_capacity=weight > 1))
    plan = {'multi_session': multi_session, 'required_sessions': required,
        'thresholds': dict(zip(('max_subsystems_per_session', 'max_source_files_per_session',
                               'max_source_lines_per_session'), capacities)),
        'metric': 'analyzed_regular_files; LF_count_plus_unterminated_final_line',
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
