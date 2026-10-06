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
from src.backends.xxx_history import SessionHistory, RecoveryRequired
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
            for snapshot in data['snapshots']:
                state = history.observe(snapshot)
            assert isinstance(state, RecoveryRequired)
            assert history.completed == history.continuations == 1
            assert not history.failed
            try:
                history.validate_final(data['snapshots'][-1], data['final'])
            except ContractError as error:
                assert error.details['code'] == 'TRANSITION_NOT_FINISHED'
            else:
                raise AssertionError('Formatless native response accepted as a result')
            rejected += 1
            print(f'{path.name}: native format loss requires recovery; text cannot be final')
            continue
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
