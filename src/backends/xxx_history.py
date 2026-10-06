# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""XXX session membership, independent of IDs' spelling and model summaries.

Accepts a bounded, format-preserving wire profile, not a claim about unseen
backend internals. Reference/synthetic tests exercise this production validator.
"""
from __future__ import annotations

import copy
import math

from src.contracts.contracts import ContractError, response_error
from src.backends.opencode import identifier, native_error, object_value, json_equal as equal

CONTINUE_TEXT = 'Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.'

# ECMAScript WhiteSpace + LineTerminator (trimEnd), explicitly enumerated.
# Python's default rstrip additionally removes e.g. U+0085, and misses U+FEFF.
TRIM_END = ('\u0009\u000b\u000c\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005'
            '\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000\ufeff\u000a\u000d\u2028\u2029')
USER_FIELDS = frozenset(('id', 'sessionID', 'role', 'time', 'agent', 'model',
                         'format', 'system', 'tools', 'variant', 'summary'))
ASSISTANT_FIELDS = frozenset(('id', 'sessionID', 'role', 'time', 'parentID', 'agent',
    'mode', 'providerID', 'modelID', 'path', 'summary', 'cost', 'tokens', 'structured',
    'variant', 'finish', 'error'))
USER_PROTECTED = ('id', 'sessionID', 'role', 'parentID', 'agent', 'model', 'format',
                  'system', 'tools', 'variant')
ASSISTANT_PROTECTED = ('id', 'sessionID', 'role', 'parentID', 'agent', 'summary',
                       'mode', 'providerID', 'modelID', 'path', 'variant')
PART_PROTECTED = ('id', 'messageID', 'sessionID', 'type', 'tool', 'callID',
                  'synthetic', 'ignored', 'auto', 'overflow')
SAFE_FIELDS = frozenset('info.' + k for k in USER_FIELDS | ASSISTANT_FIELDS) | frozenset(
    'part.' + k for k in PART_PROTECTED + ('text', 'time', 'state', 'metadata')) | frozenset(
    ('info.time.created', 'info.time.completed', 'info.unknown', 'part.unknown', 'message.unknown'))
PART_TYPES = frozenset(('text', 'reasoning', 'tool', 'compaction', 'step-start',
    'step-finish', 'snapshot', 'patch', 'file', 'agent', 'subtask', 'retry'))


def same_field(before, after, key):
    return (key in before) == (key in after) and equal(before.get(key), after.get(key))


def user_summary(value):
    """Closed metadata shape from UserMessage.summary / FileDiff declarations."""
    if (type(value) is not dict or set(value) - {'title', 'body', 'diffs'} or
            type(value.get('diffs')) is not list or
            any(type(value[k]) is not str for k in ('title', 'body') if k in value)):
        return False
    for diff in value['diffs']:
        if (type(diff) is not dict or
                set(diff) - {'file', 'before', 'after', 'additions', 'deletions', 'status'} or
                any(type(diff.get(k)) is not str for k in ('file', 'before', 'after')) or
                any(not finite_number(diff.get(k)) for k in ('additions', 'deletions')) or
                ('status' in diff and diff['status'] not in ('added', 'deleted', 'modified'))):
            return False
    return True


def finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def timestamp(value):
    return finite_number(value) and value >= 0


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
        self.diagnostic_role = 'unknown'
        self.diagnostic_part = None

    def event(self, name, **details):
        value = {'event': name, 'transitions': self.transitions, **details}
        if value not in self.events:
            self.events.append(value)
            self.emit(name, transitions=self.transitions, **details)

    def reject(self, code, *, compatibility=False, field=None):
        self.failed = True
        details = {'phase': self.phase, 'role': self.diagnostic_role}
        if field is not None:
            details['field'] = field if field in SAFE_FIELDS else 'info.unknown'
        if self.diagnostic_part is not None:
            details['part_type'] = self.diagnostic_part
        self.event('compaction_rejected' if self.transitions else 'session_rejected', reason=code, **details)
        raise response_error('BACKEND_INCOMPATIBLE' if compatibility else 'TRANSPORT_ERROR',
                             'compatibility' if compatibility else 'transport',
                             'XXX session transition rejected: ' + code + '. ' +
                             ' '.join(f'{k}={v}' for k, v in details.items()) +
                             f' transitions={self.transitions}', code=code,
                             transitions=self.transitions, **details)

    def authorize_continuation(self, mid):
        """Authorize observed membership/settings; cannot attest pre-model internals."""
        if (self.phase != 'continuation' or self.completed != self.transitions or
                self.continuations + 1 != self.completed or
                self.origins.get(self.service_id) != 'request' or
                self.origins.get(self.summary_id) != 'compaction'):
            self.reject('CONTINUATION_CHAIN_INVALID')
        # User messages have no parentID in this profile. Their position after
        # the linked summary, plus the exact service part, supplies membership.
        index = self.order.index(mid)
        if index == 0 or self.order[index - 1] != self.summary_id:
            self.reject('CONTINUATION_CHAIN_INVALID')
        info = self.messages[mid]['info']
        self._user_settings(info)
        self._format(info)

    def _format(self, info, *, required=True):
        if 'format' not in info:
            if required:
                self.reject('COMPACTION_FORMAT_MISSING', compatibility=True, field='info.format')
        elif not equal(info['format'], self.body['format']):
            self.reject('COMPACTION_FORMAT_CHANGED', compatibility=True, field='info.format')

    def _user_settings(self, info):
        root = self.messages[self.request_id]['info']
        if info.get('agent') != self.agent:
            self.reject('USER_AGENT_CHANGED', field='info.agent')
        for key in ('model', 'variant', 'tools', 'system'):
            if not same_field(info, root, key):
                self.reject('USER_SETTINGS_CHANGED', field='info.' + key)

    def _info_shape(self, info):
        role = info.get('role')
        if role not in ('user', 'assistant'):
            self.reject('MESSAGE_ROLE_UNSUPPORTED', field='info.role')
        allowed = USER_FIELDS if role == 'user' else ASSISTANT_FIELDS
        if role == 'user' and 'parentID' in info:
            self.reject('USER_PARENT_UNSUPPORTED', field='info.parentID')
        if set(info) - allowed:
            self.reject('MESSAGE_FIELD_UNSUPPORTED', field='info.unknown')
        if 'summary' in info:
            if role == 'user' and not user_summary(info['summary']):
                self.reject('USER_SUMMARY_INVALID', field='info.summary')
            if role == 'assistant' and type(info['summary']) is not bool:
                self.reject('ASSISTANT_SUMMARY_INVALID', field='info.summary')
        if role == 'user':
            model = info.get('model')
            if (type(model) is not dict or set(model) != {'providerID', 'modelID'} or
                    any(type(model[k]) is not str or not model[k] for k in model)):
                self.reject('USER_SETTINGS_INVALID', field='info.model')
            for key in ('agent', 'system', 'variant'):
                if (key == 'agent' or key in info) and type(info.get(key)) is not str:
                    self.reject('USER_SETTINGS_INVALID', field='info.' + key)
            if 'tools' in info and (type(info['tools']) is not dict or
                    any(type(v) is not bool for v in info['tools'].values())):
                self.reject('USER_SETTINGS_INVALID', field='info.tools')

    def _merge(self, old, new, *, part=False):
        before, after = (old, new) if part else (old['info'], new['info'])
        keys = PART_PROTECTED if part else (USER_PROTECTED if before['role'] == 'user' else ASSISTANT_PROTECTED)
        for key in keys:
            if not same_field(before, after, key):
                self.reject('PART_IDENTITY_CHANGED' if part else 'MESSAGE_IDENTITY_CHANGED',
                            field=('part.' if part else 'info.') + key)
        if not part:
            for key in ('created', 'completed'):
                value = before.get('time', {}).get(key)
                if value is not None and not equal(value, after.get('time', {}).get(key)):
                    self.reject('MESSAGE_TIME_CHANGED', field='info.time.' + key)
            for key in ('structured', 'finish', 'error'):
                if key in before and not equal(before[key], after.get(key)):
                    self.reject('MESSAGE_RESULT_CHANGED', field='info.' + key)
            if 'completed' in before.get('time', {}):
                for key in ('cost', 'tokens'):
                    if key in before and not same_field(before, after, key):
                        self.reject('COMPLETED_USAGE_CHANGED', field='info.' + key)
            return
        kind = before['type']
        if self.origins.get(before['messageID']) == 'request' and not equal(before, after):
            self.reject('USER_PART_CHANGED')
        if kind in ('text', 'reasoning'):
            text, update = before.get('text'), after.get('text')
            old_time, new_time = before.get('time', {}), after.get('time', {})
            finished = 'end' in old_time
            finishing = not finished and 'end' in new_time
            for key in ('start', 'end'):
                if key in old_time and not same_field(old_time, new_time, key):
                    # OpenCode 1.2.27 text-end replaces the time object. _ingest
                    # has validated its shape/order; reasoning retains start.
                    if key == 'start' and kind == 'text' and finishing:
                        continue
                    self.reject('PART_TIME_CHANGED', field='part.time')
            # A poll may include both the last chunk and finalization. It must
            # preserve the entire observed prefix, or remove only its final
            # ECMAScript whitespace. Never trim an observed prefix then append.
            valid_text = (text == update if finished else
                          update.startswith(text) or (finishing and update == text.rstrip(TRIM_END)))
            if not valid_text:
                self.reject('PART_CONTENT_CHANGED', field='part.text')
            if finished and not equal(before, after):
                self.reject('COMPLETED_PART_CHANGED', field='part.metadata')
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

    @staticmethod
    def _activity(message):
        """A separate projection: never strip metadata from stored/raw snapshots."""
        info = message['info']
        fields = ('time', 'cost', 'tokens', 'structured', 'finish', 'error') if info.get('role') == 'assistant' else ()
        return {'info': {k: info[k] for k in fields if k in info}, 'parts': message['parts']}

    def _ingest(self, snapshot):
        if type(snapshot) is not list:
            self.reject('HISTORY_NOT_LIST')
        mids, pids = set(), set()
        previous_position = -1
        active = False
        # Validate all ownership/duplicates before counting any new usage.
        for message in snapshot:
            info = object_value(object_value(message, 'session message').get('info'), 'message info')
            self.diagnostic_role = info.get('role') if info.get('role') in ('user', 'assistant') else 'unknown'
            self.diagnostic_part = None
            if set(message) - {'info', 'parts'}:
                self.reject('MESSAGE_FIELD_UNSUPPORTED', field='message.unknown')
            mid = identifier(info.get('id'), 'msg')
            if mid in mids:
                self.reject('DUPLICATE_MESSAGE_ID')
            mids.add(mid)
            if info.get('sessionID') != self.session_id:
                self.reject('FOREIGN_SESSION', field='info.sessionID')
            timing = info.get('time')
            if (type(timing) is not dict or not timestamp(timing.get('created')) or
                    ('completed' in timing and (not timestamp(timing['completed']) or
                                               timing['completed'] < timing['created']))):
                self.reject('MESSAGE_TIME_INVALID', field='info.time')
            old = self.messages.get(mid)
            if old is not None:
                self._merge(old, message)
            self._info_shape(info)
            if set(timing) - ({'created'} if info['role'] == 'user' else {'created', 'completed'}):
                self.reject('MESSAGE_FIELD_UNSUPPORTED', field='info.time')
            if type(message.get('parts')) is not list:
                self.reject('PARTS_NOT_LIST')
            for part in message['parts']:
                object_value(part, 'history part')
                kind = part.get('type')
                self.diagnostic_part = kind if type(kind) is str and kind in PART_TYPES else 'unknown'
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
                    if set(part) - {'id', 'messageID', 'sessionID', 'type', 'text', 'synthetic', 'ignored', 'time', 'metadata'}:
                        self.reject('PART_FIELD_UNSUPPORTED', field='part.unknown')
                    if any(type(part[k]) is not bool for k in ('synthetic', 'ignored') if k in part):
                        self.reject('TEXT_PART_INVALID')
                    timing = part.get('time', {})
                    if 'time' in part and (set(timing) - {'start', 'end'} or not timestamp(timing.get('start')) or
                            ('end' in timing and (not timestamp(timing['end']) or timing['end'] < timing['start']))):
                        self.reject('PART_TIME_INVALID', field='part.time')
                    if 'metadata' in part and type(part['metadata']) is not dict:
                        self.reject('TEXT_PART_INVALID', field='part.metadata')
                if part['type'] == 'tool':
                    state = part.get('state')
                    if (type(state) is not dict or type(state.get('input')) is not dict or
                            state.get('status') not in ('pending', 'running', 'completed', 'error') or
                            any(type(part.get(k)) is not str or not part[k] for k in ('tool', 'callID')) or
                            ('time' in state and type(state['time']) is not dict)):
                        self.reject('TOOL_PART_INVALID')
                    if (set(part) - {'id', 'messageID', 'sessionID', 'type', 'tool', 'callID', 'state', 'metadata'} or
                            set(state) - {'status', 'input', 'raw', 'title', 'metadata', 'time', 'output', 'attachments', 'error'}):
                        self.reject('PART_FIELD_UNSUPPORTED', field='part.unknown')
                    timing = state.get('time', {})
                    if ('compacted' in timing and (state['status'] != 'completed' or
                            not timestamp(timing['compacted']))):
                        self.reject('TOOL_PART_INVALID', field='part.time')
        for message in snapshot:
            self.diagnostic_role = message['info']['role']
            self.diagnostic_part = None
            mid = message['info']['id']
            old = self.messages.get(mid)
            if old is not None:
                position = self.order.index(mid)
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
                self.diagnostic_part = part['type'] if part['type'] in PART_TYPES else 'unknown'
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
            active |= not old['info'] or not equal(self._activity(old), self._activity(merged))
            self.messages[mid] = merged
        self.diagnostic_part = None
        return active

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
        if any(p['type'] in ('text', 'reasoning') and 'time' in p and 'end' not in p['time']
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
            for key in ('model', 'system', 'tools', 'variant'):
                if key in self.body and not equal(info.get(key), self.body[key]):
                    self.reject('ROOT_SETTINGS_MISMATCH', field='info.' + key)
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
        self.authorize_continuation(mid)
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
                self.diagnostic_role = info['role']
                self.diagnostic_part = None
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
            if not self.events or self.events[-1]['event'] not in ('compaction_rejected', 'session_rejected'):
                self.event('compaction_rejected' if self.transitions else 'session_rejected',
                           reason=exc.details.get('code', exc.failure_kind), phase=self.phase,
                           role=self.diagnostic_role)
            raise

    def usage_messages(self):
        return [(self.messages[mid], origin) for mid, origin in self.origins.items()
                if origin in ('stage', 'compaction')]

    def validate_final(self, snapshot, envelope):
        if self.failed:
            self.reject('HISTORY_ALREADY_REJECTED')
        info = object_value(object_value(envelope, 'final envelope').get('info'), 'final info')
        self.diagnostic_role = info.get('role') if info.get('role') in ('user', 'assistant') else 'unknown'
        self.diagnostic_part = None
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
        return {'profile': 'xxx-auto-format-preserving-v1', 'format_retention': 'checked_on_continuation',
                'backend_pre_model_retention': 'unverified', 'phase': self.phase,
                'summary_hook': 'not_applied_unverified', 'transitions': self.transitions,
                'hook_conflict_check': 'unavailable', 'applied_settings': {},
                'settings_policy': 'preserve_backend_profile',
                'completed': self.completed, 'continuations': self.continuations,
                'events': copy.deepcopy(self.events)}
