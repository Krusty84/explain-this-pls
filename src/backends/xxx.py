#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Explicit XXX profile: native structured result and strict local acceptance.

This profile makes no native format-retry guarantee.
It does not enable or change the upstream OpenCode capability gate.
The shared envelope classifier accepts only the already inspected stop/tool-calls
finish reasons. Raw finish metadata stays private; unknown reasons stay rejected.
"""
from __future__ import annotations
import base64
import contextlib
import json
import os
from pathlib import Path
import secrets
import sys

from src.contracts.contracts import ContractError, strict_json
from src.backends.openapi_contract import DiffError, compare
from src.backends.opencode import Server as OpenCodeServer, incompatible, object_value

PROFILE = 'xxx-http'
PROFILE_PATH = Path(__file__).resolve().parents[2] / 'schemas/xxx-declarations.json'


def retry_policy(configured=0, performed=0):
    from src.model.structured_output import retry_policy as shared_policy
    return shared_policy(configured, performed, 0)


def owns_listener(pid, port):
    """Linux: confirm this loopback socket is owned by our child process group."""
    inodes = set()
    try:
        for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
            columns = line.split()
            if columns[1] == f'0100007F:{port:04X}' and columns[3] == '0A':
                inodes.add('socket:[' + columns[9] + ']')
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) != pid:
                    continue
                for fd in (entry / 'fd').iterdir():
                    with contextlib.suppress(OSError):
                        if os.readlink(fd) in inodes:
                            return True
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return False
    return False


class Server(OpenCodeServer):
    api_doc_checks = 3

    def session_history(self, request_id, body):
        from src.backends.xxx_history import SessionHistory
        self.history = SessionHistory(self.session_id, request_id, body, emit=getattr(self, 'emit', None))
        self.history.event('compaction_capability', format_retention='unverified',
                           summary_hook='not_applied_unverified', settings='backend_profile_unchanged',
                           hook_conflict_check='unavailable')
        self.meta['compaction'] = self.history.metadata()
        return self.history

    def listener_ready(self):
        if sys.platform.startswith('linux'):
            ready = owns_listener(self.process.pid, self.port)
            self.meta['listener_ownership'] = 'linux_process_group'
            return ready
        # On macOS only the inspected upstream listener announcement is accepted.
        # Do not treat an arbitrary server's health response as ownership proof.
        self.meta['listener_ownership'] = 'owned_cli_announcement'
        return super().listener_ready()

    def check_health(self, health):
        object_value(health, 'XXX health response')
        version = health.get('version')
        if health.get('healthy') is not True or type(version) is not str or not version.strip():
            raise incompatible('XXX returned an invalid health/version response.')
        self.meta.update(api_version=version, compatibility_profile=PROFILE)

    def verify_api(self):
        original = self.authorization
        for authorization in ('', 'Basic ' + base64.b64encode(
                ('opencode:' + secrets.token_urlsafe(32)).encode()).decode()):
            self.authorization = authorization
            try:
                self.request('GET', '/doc')
            except ContractError as exc:
                if exc.failure_kind != 'BACKEND_ERROR' or exc.details.get('http_status') != 401:
                    raise
            else:
                raise incompatible('XXX must reject missing and incorrect server authentication.')
            finally:
                self.authorization = original
        spec = object_value(self.request('GET', '/doc'), 'XXX OpenAPI document')
        try:
            expected = strict_json(PROFILE_PATH.read_text())
            delta = compare(expected, spec, max_changes=50)
        except (OSError, DiffError, ContractError) as exc:
            raise incompatible('XXX OpenAPI/profile could not be verified; inspect private HTTP artifacts.') from exc
        self.save(self.artifacts / 'api-delta.json', json.dumps(delta))
        self.meta.update(compatibility_profile=PROFILE, api_changes=delta['total_changes'])
        if delta['status'] != 'MATCH':
            raise incompatible('XXX OpenAPI differs from profile xxx-http; inspect private api-delta.json. '
                               'No model request was sent.')

    def invoke(self, prompt, schema, agent_name, model, retries=0):
        if retries != 0:
            raise incompatible('XXX requires format.retryCount=0 for each orchestrator request.')
        try:
            return super().invoke(prompt, schema, agent_name, model, 0)
        except BaseException as exc:
            history = getattr(self, 'history', None)
            if history is not None and history.transitions:
                reason = (exc.details.get('code', exc.failure_kind) if isinstance(exc, ContractError) else
                          'INTERRUPTED' if isinstance(exc, KeyboardInterrupt) else 'BACKEND_ERROR')
                history.event('compaction_rejected', reason=reason)
                self.meta['compaction'] = history.metadata()
            raise
