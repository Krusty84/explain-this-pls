#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Real production HTTP client against the pinned native test server."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.backends.xxx import Server
from src.contracts.contracts import ContractError, validate_schema
from src.runtime.execution import Budget


port, directory, count = int(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
directory.mkdir(parents=True, exist_ok=True)
meta = {'backend': 'xxx'}


def save(path, value):
    with path.open('xb') as stream:
        stream.write(value.encode() if isinstance(value, str) else value)


schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean', 'enum': [True]}},
          'required': ['ok'], 'additionalProperties': False}
server = Server('', directory, {}, directory, Budget(20), save, meta,
                {'http_timeout_seconds': 3})
server.port = port
server.process = SimpleNamespace(poll=lambda: None)
try:
    value = server.invoke('Read evidence.txt, then report success.', schema, 'audit', None, 0)
    validate_schema(value, schema)
    assert count <= 2
    assert len(meta['recovery_ids']) == count
    assert meta['request_id'] != meta['final_parent_id']
    assert meta['final_parent_id'] == meta['recovery_ids'][-1]
    assert meta['compaction']['transitions'] == count
except ContractError as exc:
    meta['failure'] = {'kind': exc.failure_kind, **exc.details}
    if count != 3 or exc.details.get('code') != 'COMPACTION_RECOVERY_LIMIT_EXCEEDED':
        raise
finally:
    save(directory / 'client-meta.json', json.dumps(meta))
    # Native test owns the process; use the actual API for session cleanup.
    if server.session_id:
        server.request('POST', f'/session/{server.session_id}/abort')
        server.request('DELETE', f'/session/{server.session_id}')
    for request in server.requests:
        request.close()
