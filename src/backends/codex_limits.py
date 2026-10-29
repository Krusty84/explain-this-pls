# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Read account quotas without starting a thread or a model turn."""
import asyncio
import contextlib
import json
import math
import os
from pathlib import Path
import signal
import tempfile

from src.contracts.contracts import ContractError, strict_json
from src.messages import message as ui_message


def parse_limits(result):
    if not isinstance(result, dict):
        raise ValueError(ui_message('tui.invalid_account_limits'))
    buckets = result.get('rateLimitsByLimitId')
    if buckets is None:
        bucket = result.get('rateLimits')
        identifier = (bucket.get('limitId') or 'codex') if isinstance(bucket, dict) else 'codex'
        if not isinstance(identifier, str):
            raise ValueError(ui_message('tui.invalid_account_limits'))
        buckets = {identifier: bucket} if isinstance(bucket, dict) else {}
    if not isinstance(buckets, dict):
        raise ValueError(ui_message('tui.invalid_account_limits'))
    windows = []
    for identifier, bucket in buckets.items():
        if not isinstance(identifier, str) or not identifier or not isinstance(bucket, dict):
            raise ValueError(ui_message('tui.invalid_account_limits'))
        name = bucket.get('limitName')
        for slot in ('primary', 'secondary'):
            raw = bucket.get(slot)
            if raw is None:
                continue
            if not isinstance(raw, dict):
                raise ValueError(ui_message('tui.invalid_account_limits'))
            used, minutes, resets = (raw.get(key) for key in ('usedPercent', 'windowDurationMins', 'resetsAt'))
            try:
                valid_used = type(used) in (int, float) and used >= 0 and math.isfinite(used)
            except OverflowError:
                valid_used = False
            windows.append({'limit_id': identifier, 'limit_name': name if isinstance(name, str) else None,
                            'window': slot, 'used_percent': used if valid_used else None,
                            'window_minutes': minutes if type(minutes) is int and minutes > 0 else None,
                            'resets_at': resets if type(resets) is int and resets >= 0 else None})
    return {'windows': windows, 'error': None if windows else ui_message('tui.account_limits_not_available')}


async def _read(executable, directory):
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        executable, 'app-server', '--stdio', cwd=directory,
        env=os.environ | {'PWD': str(directory), 'NO_COLOR': '1', 'GIT_TERMINAL_PROMPT': '0'},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True))
    child = None
    try:
        child = await asyncio.shield(spawn)

        async def send(value):
            child.stdin.write((json.dumps(value) + '\n').encode('utf-8'))
            await child.stdin.drain()

        async def response(identifier):
            while True:
                line = await child.stdout.readline()
                if not line:
                    raise ValueError(ui_message('tui.account_limits_connection_closed'))
                value = strict_json(line.decode('utf-8'))
                if not isinstance(value, dict):
                    raise ValueError(ui_message('tui.invalid_account_limits'))
                if value.get('id') == identifier:
                    if value.get('error') is not None:
                        error = value['error']
                        raise ValueError(str(error.get('message', error) if isinstance(error, dict) else error))
                    if not isinstance(value.get('result'), dict):
                        raise ValueError(ui_message('tui.invalid_account_limits'))
                    return value['result']

        await send({'id': 1, 'method': 'initialize',
                    'params': {'clientInfo': {'name': 'explain-this-pls', 'version': '1'}}})
        await response(1)
        await send({'method': 'initialized', 'params': {}})
        await send({'id': 2, 'method': 'account/rateLimits/read', 'params': {}})
        return parse_limits(await response(2))
    finally:
        child = child if child is not None else await spawn
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        await child.communicate()


async def read_limits(executable, *, timeout=5):
    try:
        async with asyncio.timeout(timeout):
            with tempfile.TemporaryDirectory(prefix='explain-limits-') as directory:
                return await _read(executable, Path(directory).resolve())
    except TimeoutError:
        error = ui_message('tui.account_limits_timeout', timeout=timeout)
    except (OSError, ValueError, RuntimeError, ContractError) as exc:
        error = str(exc) or ui_message('tui.account_limits_not_available')
    return {'windows': [], 'error': error}
