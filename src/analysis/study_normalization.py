# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Pure evidence-ID normalization. No source reads, model calls or acceptance."""
from __future__ import annotations

import copy
import re

from src.contracts.contracts import contract_violation, validate_wire_identity
from src.analysis.evidence import canonical, sha

RULE = 'EVIDENCE_IDS'
HASH_FORMAT = 'canonical-json-utf8'


def normalize_evidence(stage, value, context, mode='git'):
    if stage not in ('study', 'review'):
        raise ValueError('Evidence normalization requires study or review')
    validate_wire_identity(stage, value, context, mode)
    definitions, seen, aliases = {}, set(), {}
    for i, evidence in enumerate(value['evidence']):
        original = evidence['id']
        match = re.fullmatch(r'(?:' + stage + r':)?E-([0-9]+)', original)
        if not match:
            raise contract_violation('INVALID_RECORD_ID', f'$.evidence[{i}].id')
        identifier = 'E-' + match[1].zfill(3)
        if identifier in seen:
            code = 'DUPLICATE_RECORD_ID' if original in definitions else 'EVIDENCE_ID_COLLISION'
            raise contract_violation(code, f'$.evidence[{i}].id')
        definitions[original] = identifier
        seen.add(identifier)
        local = original.removeprefix(stage + ':')
        aliases[stage + ':' + local] = stage + ':' + identifier
        aliases[stage + ':' + identifier] = stage + ':' + identifier
        if stage == 'study':
            aliases[local] = stage + ':' + identifier
            aliases[identifier] = stage + ':' + identifier
    candidate = copy.deepcopy(value)
    changes = []
    def change(container, key, after, path):
        before = container[key]
        if before != after:
            container[key] = after
            changes.append(dict(rule=RULE, path=path, before=before, after=after))
    for i, evidence in enumerate(candidate['evidence']):
        change(evidence, 'id', definitions[evidence['id']], f'$.evidence[{i}].id')
    for field in ('claims', 'findings') if stage == 'review' else ('claims', 'coverage'):
        for i, record in enumerate(candidate[field]):
            for j, ref in enumerate(record['evidence_ids']):
                change(record['evidence_ids'], j, aliases.get(ref, ref), f'$.{field}[{i}].evidence_ids[{j}]')
    return candidate, changes


def normalization_provenance(original, candidate, changes):
    """Hashes cover canonical objects, not pretty-printed artifact file bytes."""
    return {'rule': RULE, 'hash_format': HASH_FORMAT,
            'input_sha256': sha(canonical(original)),
            'normalized_sha256': sha(canonical(candidate)), 'replacement_count': len(changes)}
