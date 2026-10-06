#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Run unchanged client validation on histories produced by the native runtime."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.backends.opencode import extract_result
from src.backends.xxx_history import SessionHistory
from src.contracts.contracts import ContractError, validate_schema


def verify(directory):
    accepted = rejected = excluded = 0
    for path in sorted(Path(directory).glob('*.json')):
        data = json.loads(path.read_text())
        if data['text'] or data['threshold']:
            # Text is outside this structured client. The stock threshold path
            # omits overflow=false and must NOT be normalized into acceptance.
            excluded += 1
            continue
        body = data['body']
        history = SessionHistory(body['sessionID'], body['messageID'], body)
        if data['baseline']:
            try:
                for snapshot in data['snapshots']:
                    history.observe(snapshot)
            except ContractError as error:
                assert error.failure_kind == 'BACKEND_INCOMPATIBLE', error
                for key, value in {'code': 'COMPACTION_FORMAT_MISSING', 'phase': 'continuation',
                                   'role': 'user', 'field': 'info.format', 'transitions': 1}.items():
                    assert error.details[key] == value, error.details
                assert history.completed == 1
                assert history.continuations == 0
                rejected += 1
                print(f'{path.name}: native baseline rejected with exact COMPACTION_FORMAT_MISSING')
                continue
            raise AssertionError('Original native continuation was unexpectedly accepted')
        for snapshot in data['snapshots']:
            history.observe(snapshot)
        parent = history.validate_final(data['snapshots'][-1], data['final'])
        value, _ = extract_result(data['final'], body['sessionID'], body['messageID'], body['agent'],
                                  expected_parent=parent)
        validate_schema(value, body['format']['schema'])
        assert history.transitions == history.completed == history.continuations == data['count']
        assert data['final'] == data['snapshots'][-1][-1]
        accepted += 1
        print(f'{path.name}: native envelope, history and local schema accepted ({data["count"]} compactions)')
    assert (accepted, rejected, excluded) == (6, 2, 4), (accepted, rejected, excluded)


if __name__ == '__main__':
    verify(sys.argv[1])
