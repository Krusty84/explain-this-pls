# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Pure evidence-package capture and assembly of the authored Study."""
from __future__ import annotations

import copy

from src.contracts.contracts import array, contract_violation, validate_result, validate_schema


def freeze_collection(data, resolutions, *, citation_corrections=None, normalization_provenance=None):
    from src.contracts.saved_contracts import RESOLUTION
    mode = 'folder' if 'source_directory' in data else 'git'
    validate_result('study', data, dict(data, phase='collect'), mode)
    validate_schema(resolutions, array(RESOLUTION), '$.evidence_package.evidence_checks')
    if len(data['evidence']) != len(resolutions):
        raise contract_violation('EVIDENCE_PACKAGE_MISMATCH', '$.evidence_package.evidence_checks')
    for pointer, resolution in zip(data['evidence'], resolutions):
        if (resolution['id'] != 'study:' + pointer['id'] or any(
                resolution[key] != pointer[key] for key in ('source_id', 'path', 'start_line', 'end_line'))):
            raise contract_violation('EVIDENCE_PACKAGE_MISMATCH', '$.evidence_package.evidence_checks')
    return {'study': copy.deepcopy(data), 'evidence_checks': copy.deepcopy(resolutions),
            'citation_corrections': copy.deepcopy(citation_corrections or []),
            'normalization_provenance': copy.deepcopy(normalization_provenance)}


def assemble_study(writer, package, context, mode='git'):
    validate_result('study', writer, dict(context, phase='write'), mode)
    collection = package['study']
    validate_result('study', collection, dict(context, phase='collect'), mode)
    frozen = freeze_collection(collection, package['evidence_checks'],
        citation_corrections=package.get('citation_corrections'),
        normalization_provenance=package.get('normalization_provenance'))
    result = copy.deepcopy(collection)
    result['report_sections'] = copy.deepcopy(writer['report_sections'])
    status_order = ('COMPLETE', 'PARTIAL', 'BLOCKED')
    result['completion_status'] = max((collection['completion_status'], writer['completion_status']),
                                      key=status_order.index)
    limitations = list(collection['limitations']) + list(writer['limitations'])
    unresolved = [entry for entry in frozen['evidence_checks'] if entry['status'] != 'RESOLVED']
    if unresolved:
        if result['completion_status'] == 'COMPLETE':
            result['completion_status'] = 'PARTIAL'
        limitations.extend('Evidence ' + entry['id'] + ': ' + entry['status'] + '.' for entry in unresolved)
    result['limitations'] = list(dict.fromkeys(limitations))
    full_context = {key: value for key, value in context.items() if key != 'phase'}
    validate_result('study', result, full_context, mode)
    return result
