#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Run production client validation on unmodified histories from the native runtime."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.backends.opencode import extract_result
from src.backends.xxx_history import SessionHistory
from src.contracts.contracts import ContractError, validate_schema


def verify(directory, *, stock_only=False):
    accepted = rejected = excluded = 0
    for path in sorted(Path(directory).glob('*.json')):
        data = json.loads(path.read_text())
        if stock_only:
            assert data['baseline'], 'Stock verification must not include patched histories'
        if data['text']:
            # Text is outside this structured client. Threshold histories are
            # checked unchanged: omitted overflow is valid stock wire behavior.
            excluded += 1
            continue
        body = data['body']
        history = SessionHistory(body['sessionID'], body['messageID'], body)
        if data['baseline'] and data['count']:
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
    expected = (2, 4, 0) if stock_only else (10, 4, 2)
    assert (accepted, rejected, excluded) == expected, (accepted, rejected, excluded)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--stock-only', action='store_true')
    args = parser.parse_args()
    verify(args.directory, stock_only=args.stock_only)
