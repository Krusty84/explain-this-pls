# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""XXX session membership, independent of IDs' spelling and model summaries.

Only the start of auto/non-overflow compaction is evidenced for XXX. The
continuation recognizer is an upstream-reference shape, NOT a capability claim.
Production authorization stays closed until a fork's pre-model format retention
mechanism is verified. Tests override that one gate for explicitly synthetic data.
"""
from __future__ import annotations

import copy
import math

from src.contracts.contracts import ContractError, response_error
from src.backends.opencode import identifier, native_error, object_value, json_equal as equal

CONTINUE_TEXT = 'Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.'


def timestamp(value):
    try:
        return type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


class SessionHistory:
    def __init__(self, session_id, request_id, body, *, emit=None):
        self.session_id, self.request_id = session_id, request_id
        self.body = copy.deepcopy(body)
        self.agent = body['agent']
        self.messages = {}
        self.order = []
        self.part_owners = {}
        self.origins = {}
        self.events = []
        self.emit = emit or (lambda *args, **kwargs: None)
        self.failed = False
        self.transitions = 0
        self.completed = 0
        self.continuations = 0
        self.current_request = request_id
        self.phase = 'root'
        self.summary_id = None
        self.service_id = None
        self.pending_user = None
        self.last_assistant = None

    def event(self, name, **details):
        value = {'event': name, 'transitions': self.transitions, **details}
        if value not in self.events:
            self.events.append(value)
            self.emit(name, transitions=self.transitions, **details)

    def reject(self, code, *, compatibility=False):
        self.failed = True
        self.event('compaction_rejected', reason=code)
        raise response_error('BACKEND_INCOMPATIBLE' if compatibility else 'TRANSPORT_ERROR',
                             'compatibility' if compatibility else 'transport',
                             'XXX session transition rejected: ' + code + '.', code=code,
                             transitions=self.transitions)

    def authorize_continuation(self):
        # No environment/config/version/API-schema bypass. A wire snapshot cannot
        # prove that format was retained BEFORE the next provider call.
        self.reject('COMPACTION_FORMAT_RETENTION_UNVERIFIED', compatibility=True)

    def _format(self, info, *, required=True):
        if 'format' not in info:
            if required:
                self.reject('COMPACTION_FORMAT_MISSING', compatibility=True)
        elif not equal(info['format'], self.body['format']):
            self.reject('COMPACTION_FORMAT_CHANGED', compatibility=True)

    def _user_settings(self, info):
        root = self.messages[self.request_id]['info']
        if info.get('agent') != self.agent:
            self.reject('USER_AGENT_CHANGED')
        for key in ('model', 'variant', 'tools', 'system'):
            if not equal(info.get(key), root.get(key)):
                self.reject('USER_SETTINGS_CHANGED')

    def _merge(self, old, new, *, part=False):
        before, after = (old, new) if part else (old['info'], new['info'])
        keys = (('id', 'messageID', 'sessionID', 'type', 'tool', 'callID', 'synthetic', 'auto', 'overflow')
                if part else ('id', 'sessionID', 'role', 'parentID', 'agent', 'summary', 'mode',
                              'providerID', 'modelID', 'path', 'model', 'format', 'variant', 'tools', 'system'))
        for key in keys:
            if not equal(before.get(key), after.get(key)):
                self.reject('PART_IDENTITY_CHANGED' if part else 'MESSAGE_IDENTITY_CHANGED')
        if not part:
            for key in ('created', 'completed'):
                value = before.get('time', {}).get(key)
                if value is not None and not equal(value, after.get('time', {}).get(key)):
                    self.reject('MESSAGE_TIME_CHANGED')
            for key in ('structured', 'finish', 'error'):
                if key in before and not equal(before[key], after.get(key)):
                    self.reject('MESSAGE_RESULT_CHANGED')
            if before['role'] == 'user' and not equal(before, after):
                self.reject('USER_IDENTITY_CHANGED')
            return
        kind = before['type']
        if self.origins.get(before['messageID']) == 'request' and not equal(before, after):
            self.reject('USER_PART_CHANGED')
        if kind in ('text', 'reasoning'):
            text, update = before.get('text'), after.get('text')
            if (type(text) is not str or type(update) is not str or not update.startswith(text)
                    or (before.get('time', {}).get('end') is not None and text != update)):
                self.reject('PART_CONTENT_CHANGED')
        elif kind == 'tool':
            previous, current = before.get('state', {}), after.get('state', {})
            allowed = {'pending': ('pending', 'running', 'completed', 'error'),
                       'running': ('running', 'completed', 'error'),
                       'completed': ('completed',), 'error': ('error',)}
            if current.get('status') not in allowed.get(previous.get('status'), ()):
                self.reject('TOOL_STATE_REGRESSED')
            if previous.get('status') in ('completed', 'error'):
                # Native prune marks an old output; it does not replace it.
                a, b = copy.deepcopy(before), copy.deepcopy(after)
                for value in (a, b):
                    value.get('state', {}).get('time', {}).pop('compacted', None)
                if not equal(a, b):
                    self.reject('COMPLETED_TOOL_CHANGED')
        elif not equal(before, after):
            self.reject('PART_CONTENT_CHANGED')

    def _ingest(self, snapshot):
        if type(snapshot) is not list:
            self.reject('HISTORY_NOT_LIST')
        mids, pids = set(), set()
        previous_position = -1
        changed = False
        # Validate all ownership/duplicates before counting any new usage.
        for message in snapshot:
            info = object_value(object_value(message, 'session message').get('info'), 'message info')
            mid = identifier(info.get('id'), 'msg')
            if mid in mids:
                self.reject('DUPLICATE_MESSAGE_ID')
            mids.add(mid)
            if info.get('sessionID') != self.session_id:
                self.reject('FOREIGN_SESSION')
            timing = info.get('time')
            if (type(timing) is not dict or not timestamp(timing.get('created')) or
                    ('completed' in timing and (not timestamp(timing['completed']) or
                                               timing['completed'] < timing['created']))):
                self.reject('MESSAGE_TIME_INVALID')
            if type(message.get('parts')) is not list:
                self.reject('PARTS_NOT_LIST')
            for part in message['parts']:
                object_value(part, 'history part')
                pid = identifier(part.get('id'), 'prt')
                if pid in pids:
                    self.reject('DUPLICATE_PART_ID')
                pids.add(pid)
                if part.get('messageID') != mid or part.get('sessionID') != self.session_id:
                    self.reject('FOREIGN_PART')
                if self.part_owners.get(pid, mid) != mid:
                    self.reject('PART_OWNER_CHANGED')
                if type(part.get('type')) is not str:
                    self.reject('PART_TYPE_MISSING')
                if part['type'] in ('text', 'reasoning'):
                    if type(part.get('text')) is not str or ('time' in part and type(part['time']) is not dict):
                        self.reject('TEXT_PART_INVALID')
                if part['type'] == 'tool':
                    state = part.get('state')
                    if (type(state) is not dict or type(state.get('input')) is not dict or
                            state.get('status') not in ('pending', 'running', 'completed', 'error') or
                            any(type(part.get(k)) is not str or not part[k] for k in ('tool', 'callID')) or
                            ('time' in state and type(state['time']) is not dict)):
                        self.reject('TOOL_PART_INVALID')
        for message in snapshot:
            mid = message['info']['id']
            old = self.messages.get(mid)
            if old is not None:
                position = self.order.index(mid)
                self._merge(old, message)
            else:
                position = len(self.order)
                self.order.append(mid)
                old = {'info': {}, 'parts': []}
            if position <= previous_position:
                self.reject('HISTORY_ORDER_CHANGED')
            previous_position = position
            merged_parts = {p['id']: p for p in old['parts']}
            positions = {pid: i for i, pid in enumerate(merged_parts)}
            previous_part = -1
            for part in message['parts']:
                if (message['info'].get('summary') is True and part['type'] not in
                        ('text', 'reasoning', 'step-start', 'step-finish')):
                    self.reject('COMPACTION_SUMMARY_PART_UNSUPPORTED')
                pid = part['id']
                if pid in merged_parts:
                    self._merge(merged_parts[pid], part, part=True)
                else:
                    if self.origins.get(mid) == 'request':
                        self.reject('USER_PART_ADDED')
                    positions[pid] = len(positions)
                if positions[pid] <= previous_part:
                    self.reject('PART_ORDER_CHANGED')
                previous_part = positions[pid]
                merged_parts[pid] = copy.deepcopy(part)
                self.part_owners[pid] = mid
            merged = {'info': copy.deepcopy(message['info']), 'parts': list(merged_parts.values())}
            changed |= not equal(old, merged)
            self.messages[mid] = merged
        return changed

    def _complete_summary(self):
        if self.phase != 'summary':
            return
        summary = self.messages[self.summary_id]
        info = summary['info']
        if any(p['type'] not in ('text', 'reasoning', 'step-start', 'step-finish') for p in summary['parts']):
            self.reject('COMPACTION_SUMMARY_PART_UNSUPPORTED')
        if 'error' in info:
            native_error(info['error'])
        timing = info.get('time', {})
        if 'completed' not in timing:
            return
        if (type(timing.get('created')) not in (int, float) or
                type(timing['completed']) not in (int, float) or timing['completed'] < timing['created'] or
                info.get('finish') != 'stop'):
            self.reject('COMPACTION_SUMMARY_INCOMPLETE')
        # The info can complete before parts arrive in a subsequent snapshot.
        if not any(p['type'] == 'text' and type(p.get('text')) is str and p['text'].strip()
                   for p in summary['parts']):
            return
        self.phase = 'continuation'
        self.completed += 1
        self.event('compaction_completed', completed=self.completed)

    def _accept_user(self, mid):
        message = self.messages[mid]
        info, parts = message['info'], message['parts']
        if 'parentID' in info:
            self.reject('USER_PARENT_UNSUPPORTED')
        if mid == self.request_id:
            if self.phase != 'root':
                self.reject('ROOT_REQUEST_OUT_OF_ORDER')
            if info.get('agent') != self.agent:
                self.reject('ROOT_AGENT_MISMATCH')
            self._format(info)
            if 'model' in self.body and not equal(info.get('model'), self.body['model']):
                self.reject('ROOT_MODEL_MISMATCH')
            if not parts:
                return False
            if len(parts) != 1 or parts[0].get('type') != 'text' or parts[0].get('text') != self.body['parts'][0]['text']:
                self.reject('ROOT_PROMPT_MISMATCH')
            self.phase = 'stage'
            return True
        if self.request_id not in self.messages or self.phase == 'root':
            self.reject('ROOT_REQUEST_MISSING')
        self._user_settings(info)
        if not parts:
            # A user identity alone grants no membership or assistant parent.
            return False
        if len(parts) != 1:
            self.reject('SERVICE_PARTS_UNSUPPORTED')
        part = parts[0]
        if part['type'] == 'compaction':
            if self.phase != 'stage' or self.last_assistant is None:
                self.reject('COMPACTION_OUT_OF_ORDER')
            if 'structured' in self.messages[self.last_assistant]['info']:
                self.reject('COMPACTION_AFTER_STAGE_RESULT')
            if part.get('auto') is not True or part.get('overflow') is not False:
                self.reject('COMPACTION_FORM_UNSUPPORTED', compatibility=True)
            if set(part) - {'id', 'sessionID', 'messageID', 'type', 'auto', 'overflow'}:
                self.reject('COMPACTION_PART_UNSUPPORTED')
            self._format(info, required=False)
            self.service_id = mid
            self.phase = 'compaction'
            self.transitions += 1
            self.event('compaction_started')
            return True
        self._complete_summary()
        if self.phase != 'continuation':
            self.reject('UNEXPECTED_USER_REQUEST')
        if (part['type'] != 'text' or part.get('synthetic') is not True or
                part.get('text') != CONTINUE_TEXT or part.get('ignored', False) is not False):
            self.reject('CONTINUATION_FORM_UNSUPPORTED')
        if set(part) - {'id', 'sessionID', 'messageID', 'type', 'synthetic', 'text', 'time'}:
            self.reject('CONTINUATION_PART_UNSUPPORTED')
        timing = part.get('time', {})
        if (type(timing.get('start')) not in (int, float) or
                type(timing.get('end')) not in (int, float) or timing['end'] < timing['start']):
            self.reject('CONTINUATION_TIME_INVALID')
        self._format(info)
        self.authorize_continuation()
        self.current_request = mid
        self.continuations += 1
        self.last_assistant = None
        self.phase = 'stage'
        self.event('session_continued', continuations=self.continuations)
        return True

    def observe(self, snapshot):
        if self.failed:
            self.reject('HISTORY_ALREADY_REJECTED')
        try:
            changed = self._ingest(snapshot)
            for mid in self.order:
                message = self.messages[mid]
                info = message['info']
                if mid in self.origins:
                    if 'error' in info:
                        native_error(info['error'])
                    self._complete_summary()
                    continue
                if self.pending_user is not None and self.pending_user != mid:
                    self.reject('UNCLASSIFIED_USER_PARENT')
                if info.get('role') == 'user':
                    if not self._accept_user(mid):
                        self.pending_user = mid
                        break
                    self.pending_user = None
                    self.origins[mid] = 'request'
                elif info.get('role') == 'assistant':
                    if self.phase == 'compaction':
                        if (info.get('parentID') != self.service_id or info.get('agent') != 'compaction'
                                or info.get('summary') is not True or info.get('mode') != 'compaction'):
                            self.reject('COMPACTION_SUMMARY_IDENTITY')
                        self.summary_id = mid
                        self.phase = 'summary'
                        self.origins[mid] = 'compaction'
                        self._complete_summary()
                    elif (self.phase == 'stage' and info.get('parentID') == self.current_request
                          and info.get('agent') == self.agent and not info.get('summary', False)):
                        self.origins[mid] = 'stage'
                        self.last_assistant = mid
                    else:
                        self.reject('UNCONFIRMED_ASSISTANT_PARENT')
                    if 'error' in info:
                        native_error(info['error'])
                else:
                    self.reject('MESSAGE_ROLE_UNSUPPORTED')
            return changed
        except ContractError as exc:
            self.failed = True
            if not self.events or self.events[-1]['event'] != 'compaction_rejected':
                self.event('compaction_rejected', reason=exc.details.get('code', exc.failure_kind))
            raise

    def usage_messages(self):
        return [(self.messages[mid], origin) for mid, origin in self.origins.items()
                if origin in ('stage', 'compaction')]

    def validate_final(self, snapshot, envelope):
        if self.failed:
            self.reject('HISTORY_ALREADY_REJECTED')
        info = object_value(object_value(envelope, 'final envelope').get('info'), 'final info')
        mid = identifier(info.get('id'), 'msg')
        if info.get('agent') == 'compaction' or info.get('summary') is True:
            self.reject('COMPACTION_IS_NOT_STAGE_RESULT')
        if self.phase != 'stage' or self.pending_user is not None:
            self.reject('TRANSITION_NOT_FINISHED', compatibility=True)
        if self.origins.get(mid) != 'stage' or mid != self.last_assistant or mid != self.order[-1]:
            self.reject('FINAL_NOT_LATEST_STAGE_RESULT')
        if not equal(snapshot, [self.messages[key] for key in self.order]):
            self.reject('FINAL_HISTORY_INCOMPLETE')
        if not equal(self.messages[mid], envelope):
            self.reject('FINAL_SNAPSHOT_MISMATCH')
        return self.current_request

    def metadata(self):
        return {'profile': 'xxx-observed-auto-prefix', 'format_retention': 'unverified',
                'summary_hook': 'not_applied_unverified', 'transitions': self.transitions,
                'hook_conflict_check': 'unavailable', 'applied_settings': {},
                'settings_policy': 'preserve_backend_profile',
                'completed': self.completed, 'continuations': self.continuations,
                'events': copy.deepcopy(self.events)}
