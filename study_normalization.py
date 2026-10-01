# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Pure study wire compatibility. No source reads, model calls or acceptance."""
from __future__ import annotations

import copy

from contracts import unique_ids, validate_wire_identity
from evidence import canonical, sha

RULE = 'STUDY_LOCAL_EVIDENCE_REF_V1'
HASH_FORMAT = 'canonical-json-utf8-v1'


def normalize_study(value, context, mode='git'):
    """Return an independent candidate and exact edits after strict prerequisites.

    This function accepts study wire objects only. Transport completion and
    consistency must already have been checked by a live caller. Offline callers
    get no assertion about transport, sources, policy or publication.
    """
    validate_wire_identity('study', value, context, mode)
    local_ids = unique_ids(value['evidence'], r'E-[0-9]{3,}', '$.evidence')
    candidate = copy.deepcopy(value)
    changes = []
    for i, claim in enumerate(candidate['claims']):
        for j, ref in enumerate(claim['evidence_ids']):
            if ref in local_ids:
                after = 'study:' + ref
                claim['evidence_ids'][j] = after
                changes.append({'rule': RULE, 'path': f'$.claims[{i}].evidence_ids[{j}]',
                                'before': ref, 'after': after})
    return candidate, changes


def normalization_provenance(original, candidate, changes):
    """Hashes cover canonical objects, not pretty-printed artifact file bytes."""
    return {'rule': RULE, 'hash_format': HASH_FORMAT,
            'extracted_sha256': sha(canonical(original)),
            'normalized_sha256': sha(canonical(candidate)), 'replacement_count': len(changes)}
