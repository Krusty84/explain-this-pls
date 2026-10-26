# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Monotonic execution budgets. Cleanup and source guards use separate budgets."""
import contextlib
import fcntl
import os
import tempfile
import time
import math
from pathlib import Path
from src.contracts.contracts import response_error
from src.runtime.errors import AuditError
from src.runtime.artifacts import digest

DEFAULT_EXECUTION = {'stage_timeout_seconds': 3600, 'idle_timeout_seconds': None,
                     'review_enabled': True,
                     'read_loop_guard': True,
                     'max_revision_rounds': 1,
                     'structured_output_repair_attempts': 0,
                     'http_timeout_seconds': 5, 'api_doc_timeout_seconds': 30}


def execution_settings(value=None):
    if value is None:
        value = {}
    if type(value) is not dict or set(value) - set(DEFAULT_EXECUTION):
        raise ValueError('execution must be an object containing only documented settings.')
    result = DEFAULT_EXECUTION | value
    for key in ('review_enabled', 'read_loop_guard'):
        if type(result[key]) is not bool:
            raise ValueError(f'execution.{key} must be boolean.')
    if type(result['max_revision_rounds']) is not int or result['max_revision_rounds'] not in (0, 1):
        raise ValueError('execution.max_revision_rounds must be 0 or 1.')
    for key in ('stage_timeout_seconds', 'idle_timeout_seconds',
                'http_timeout_seconds', 'api_doc_timeout_seconds'):
        number = result[key]
        if number is None and key == 'idle_timeout_seconds':
            continue
        try:
            valid = type(number) in (int, float) and math.isfinite(number) and number > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f'execution.{key} must be a positive finite number' +
                             (' or null.' if key == 'idle_timeout_seconds' else '.'))
    retries = result['structured_output_repair_attempts']
    if type(retries) is not int or not 0 <= retries <= 2:
        raise ValueError('execution.structured_output_repair_attempts must be an integer from 0 to 2.')
    return result


class Budget:
    def __init__(self, seconds, idle=None, *, clock=time.monotonic, label='stage'):
        self.clock = clock
        self.started = self.last_activity = clock()
        self.deadline = self.started + seconds
        self.idle = idle
        self.label = label

    def check(self):
        now = self.clock()
        if now >= self.deadline:
            raise response_error('STAGE_TIMEOUT', 'execution', 'Execution time budget exceeded.', budget_source=self.label)
        if self.idle is not None and now - self.last_activity >= self.idle:
            raise response_error('IDLE_TIMEOUT', 'execution', 'No substantive backend activity within idle budget.', budget_source='idle')
        return min(self.deadline - now, self.idle - (now - self.last_activity)
                   if self.idle is not None else float('inf'))

    def activity(self):
        # Activity cannot revive an already expired budget.
        self.check()
        self.last_activity = self.clock()


@contextlib.contextmanager
def repository_lock(repo: Path):
    repo = repo.resolve()
    base = Path(tempfile.gettempdir()).resolve() / f'architecture-audit-locks-{os.getuid()}'
    base.mkdir(mode=0o700, exist_ok=True)
    if base.is_symlink() or base.stat().st_uid != os.getuid() or base.stat().st_mode & 0o077:
        raise AuditError('Unsafe lock directory')
    path = base / (digest(str(repo).encode()) + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AuditError('Another architecture audit holds this repository lock.') from exc
        yield
    finally:
        os.close(fd)
