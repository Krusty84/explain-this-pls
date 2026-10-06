#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""XXX is the OpenCode 1.2.27 HTTP profile, with known optional extensions.

This profile makes no native format-retry guarantee.
OpenCode V2 uses a separate CLI adapter.
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
from src.backends.openapi_contract import DiffError, compare, normalize
from src.backends.opencode import Server as OpenCodeServer, incompatible, object_value

PROFILE = 'opencode-v1.2.27-http'
PROFILE_PATH = Path(__file__).resolve().parents[2] / 'schemas/opencode-v1.2.27-declarations.json'


def compatible_api(spec):
    """Remove only exact known extensions from a private comparison copy.

    The raw document/delta stay intact. Duplicate or changed declarations remain
    visible to the ordinary strict comparator, as do all unrelated differences.
    """
    candidate = normalize(spec)
    accepted = []
    components = candidate.get('components')
    schemas = components.get('schemas') if type(components) is dict else None
    if type(schemas) is not dict:
        return candidate, accepted
    session = schemas.get('Session')
    if type(session) is dict:
        props, required = session.get('properties'), session.get('required')
        if type(props) is dict and props.get('compactionCount') == {'type': 'number'}:
            del props['compactionCount']
            if type(required) is list and required.count('compactionCount') == 1:
                required.remove('compactionCount')
            accepted.append('compactionCount')
    status = schemas.get('SessionStatus')
    variants = status.get('anyOf') if type(status) is dict else None
    if type(variants) is list:
        for name, props in (
                ('queued', {'runningTaskSize': {'type': 'number'}, 'waitingQueueIndex': {'type': 'number'}}),
                ('unattended_retry', {'attempt': {'type': 'number'}, 'message': {'type': 'string'},
                                      'next': {'type': 'number'}})):
            props['type'] = {'const': name, 'type': 'string'}
            expected = {'type': 'object', 'properties': props, 'required': sorted(props)}
            # Remove at most one; duplicate variants are not a known extension.
            if expected in variants:
                variants.remove(expected)
                accepted.append(name)
    return candidate, accepted


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
        self.history.event('compaction_capability', format_retention='checked_on_continuation',
                           backend_pre_model_retention='unverified',
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
            candidate, accepted = compatible_api(spec)
            residual = compare(expected, candidate, max_changes=50)
        except (OSError, DiffError, ContractError) as exc:
            raise incompatible('XXX OpenAPI/profile could not be verified; inspect private HTTP artifacts.') from exc
        self.save(self.artifacts / 'api-delta.json', json.dumps(delta))
        self.save(self.artifacts / 'api-compatibility.json', json.dumps({
            'profile': PROFILE, 'accepted_extensions': accepted, 'residual_delta': residual}))
        self.meta.update(compatibility_profile=PROFILE, api_changes=delta['total_changes'],
                         api_allowed_extensions=accepted)
        if residual['status'] != 'MATCH':
            raise incompatible('XXX OpenAPI differs from profile ' + PROFILE + '; '
                               'inspect private api-delta.json and api-compatibility.json. '
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
                # History rejections already carry their safe field/role/phase.
                # Add an event only for failures outside that validator (e.g. timeout).
                if not history.failed:
                    history.event('compaction_rejected', reason=reason, phase=history.phase)
                self.meta['compaction'] = history.metadata()
            raise
