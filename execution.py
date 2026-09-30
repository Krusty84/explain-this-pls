# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Monotonic execution budgets. Cleanup and source guards use separate budgets."""
import math
import time

from contracts import response_error

DEFAULT_EXECUTION = {'stage_timeout_seconds': 3600, 'idle_timeout_seconds': None,
                     'opencode_format_retries': 2}


def execution_settings(value=None):
    if value is None:
        value = {}
    if type(value) is not dict or set(value) - set(DEFAULT_EXECUTION):
        raise ValueError('execution must be an object containing only documented settings.')
    result = DEFAULT_EXECUTION | value
    for key in ('stage_timeout_seconds', 'idle_timeout_seconds'):
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
    retries = result['opencode_format_retries']
    if type(retries) is not int or not 0 <= retries <= 2:
        raise ValueError('execution.opencode_format_retries must be an integer from 0 to 2.')
    return result


class Budget:
    def __init__(self, seconds, idle=None, *, clock=time.monotonic):
        self.clock = clock
        self.started = self.last_activity = clock()
        self.deadline = self.started + seconds
        self.idle = idle

    def check(self):
        now = self.clock()
        if now >= self.deadline:
            raise response_error('STAGE_TIMEOUT', 'execution', 'Stage time budget exceeded.')
        if self.idle is not None and now - self.last_activity >= self.idle:
            raise response_error('IDLE_TIMEOUT', 'execution', 'No substantive backend activity within idle budget.')
        return min(self.deadline - now, self.idle - (now - self.last_activity)
                   if self.idle is not None else float('inf'))

    def activity(self):
        # Activity cannot revive an already expired budget.
        self.check()
        self.last_activity = self.clock()
