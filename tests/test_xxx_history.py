#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline membership/format/usage tests; successful continuations are synthetic."""
import copy
import unittest

from src.backends.xxx_history import SessionHistory, RecoveryRequired
from src.backends.opencode import extract_result, validate_history
from src.contracts.contracts import ContractError
from src.runtime.execution import Budget
from src.runtime.metrics import HTTPUsage, summarize
from fixtures.compaction_protocol import assistant, body, chain, part, snapshots, text_completion_snapshots


class HistoryTests(unittest.TestCase):
    def history(self):
        return SessionHistory('ses_test', 'msg_root', body())

    def rejected(self, messages, code, *, history=None):
        history = history or self.history()
        with self.assertRaises(ContractError) as caught:
            history.observe(messages)
        self.assertEqual(caught.exception.details.get('code'), code)
        return caught.exception

    def test_no_compaction_and_many_reference_transitions(self):
        for count in (0, 1, 3):
            messages = chain(count)
            history = self.history()
            for snapshot in snapshots(messages):
                history.observe(snapshot)
                self.assertFalse(history.observe(snapshot))
            parent = history.validate_final(messages, messages[-1])
            data, meta = extract_result(messages[-1], 'ses_test', 'msg_root', 'audit', expected_parent=parent)
            self.assertEqual(data, {'ok': True})
            self.assertEqual(meta['request_id'], 'msg_root')
            self.assertEqual(history.transitions, count)
            self.assertEqual(history.completed, count)
            self.assertEqual(history.continuations, count)

    def test_empty_summary_is_observed_without_rejecting_or_accepting(self):
        messages = chain()[:4]
        messages[-1]['parts'] = []
        messages[-1]['info']['time'].pop('completed')
        messages[-1]['info'].pop('finish')
        history = self.history()
        self.assertTrue(history.observe(messages))
        self.assertFalse(history.observe(messages))
        self.assertEqual(history.phase, 'summary')
        self.assertEqual(history.transitions, 1)
        with self.assertRaises(ContractError) as caught:
            history.validate_final(messages, messages[-1])
        self.assertEqual(caught.exception.details['code'], 'COMPACTION_IS_NOT_STAGE_RESULT')

    def test_stock_threshold_compaction_preserves_wire_and_reports_format_loss(self):
        for count in (1, 2):
            for retained in (False, True):
                with self.subTest(count=count, retained=retained):
                    messages = chain(count)
                    for message in messages:
                        if message['parts'][0]['type'] == 'compaction':
                            del message['parts'][0]['overflow']
                    if not retained:
                        del messages[-2]['info']['format']
                    original = copy.deepcopy(messages)
                    history = self.history()
                    for snapshot in snapshots(messages[:-2]):
                        history.observe(snapshot)
                    if retained:
                        history.observe(messages)
                        history.validate_final(messages, messages[-1])
                        self.assertEqual(history.continuations, count)
                    else:
                        state = history.observe(messages)
                        self.assertIsInstance(state, RecoveryRequired)
                        self.assertFalse(history.failed)
                        self.assertEqual(history.completed, count)
                        self.assertEqual(history.continuations, count)
                    self.assertEqual(messages, original)

    def test_stock_overflow_field_remains_typed_and_immutable(self):
        for value in (True, None, 0, 1, 'false'):
            messages = chain()
            messages[2]['parts'][0]['overflow'] = value
            self.rejected(messages, 'COMPACTION_FORM_UNSUPPORTED')
        for before, after in ((False, None), (None, False)):
            messages = chain()[:4]
            if before is None:
                del messages[2]['parts'][0]['overflow']
            history = self.history()
            history.observe(messages)
            if after is None:
                del messages[2]['parts'][0]['overflow']
            else:
                messages[2]['parts'][0]['overflow'] = after
            self.rejected(messages, 'PART_IDENTITY_CHANGED', history=history)

    def test_user_summary_updates_are_saved_without_activity_or_input_mutation(self):
        messages = chain(0)
        history = self.history()
        history.observe(messages)
        for summary in ({'diffs': []}, {'title': 'Title', 'body': 'Metadata', 'diffs': []},
                        {'title': 'Updated', 'diffs': [{'file': 'fixture.py', 'before': '',
                            'after': 'data', 'additions': 1, 'deletions': 0, 'status': 'added'}]}):
            messages[0]['info']['summary'] = summary
            original = copy.deepcopy(messages)
            self.assertFalse(history.observe(messages))
            self.assertFalse(history.observe(messages))
            self.assertEqual(messages, original)
            self.assertEqual(history.messages['msg_root'], original[0])
            history.validate_final(messages, messages[-1])
        self.assertEqual(history.transitions, 0)
        self.assertEqual(history.body, body())

    def test_user_summary_shape_and_assistant_summary_type(self):
        for summary in (True, 1, None, [], {}, {'diffs': {}}, {'diffs': [], 'title': 1},
                        {'diffs': [], 'body': False}, {'diffs': [], 'instructions': 'private'},
                        {'diffs': [{'file': 'x', 'before': '', 'after': '', 'additions': True, 'deletions': 0}]}):
            messages = chain(0)
            messages[0]['info']['summary'] = summary
            with self.subTest(summary=summary):
                self.rejected(messages, 'USER_SUMMARY_INVALID')
        for summary in ({'diffs': []}, 1, None):
            messages = chain(0)
            messages[-1]['info']['summary'] = summary
            self.rejected(messages, 'ASSISTANT_SUMMARY_INVALID')

    def test_stream_append_trim_and_last_chunk_with_finalization(self):
        for kind, start in (('text', 120), ('text', 100), ('reasoning', 100)):
            for previous, update in (('', 'Result.'), ('Result.', 'Result.'),
                                     ('Result.\n', 'Result.'), ('Result', 'Result. done'),
                                     ('Result.\n', 'Result.\nNext'), ('Result.\ufeff', 'Result.')):
                with self.subTest(kind=kind, start=start, previous=previous, update=update):
                    opened, closed, final = text_completion_snapshots(chain(0))
                    opened[-1]['parts'][0].update(type=kind, text=previous)
                    for snapshot in (closed, final):
                        snapshot[-1]['parts'][0].update(type=kind, text=update, time={'start': start, 'end': 121})
                    history = self.history()
                    originals = copy.deepcopy([opened, closed, final])
                    for snapshot in (opened, closed, final):
                        self.assertTrue(history.observe(snapshot))
                        self.assertFalse(history.observe(snapshot))
                    self.assertEqual([opened, closed, final], originals)
                    self.assertEqual(history.messages['msg_final'], final[-1])
                    parent = history.validate_final(final, final[-1])
                    data, _ = extract_result(final[-1], 'ses_test', 'msg_root', 'audit', expected_parent=parent)
                    self.assertEqual(data, {'ok': True})

    def test_text_completion_replaces_start_before_or_with_message_completion(self):
        for completes_message in (False, True):
            with self.subTest(completes_message=completes_message):
                opened, closed, final = text_completion_snapshots(chain(0))
                history = self.history()
                self.assertNotIn('completed', opened[-1]['info']['time'])
                self.assertNotIn('finish', opened[-1]['info'])
                self.assertEqual(opened[-1]['parts'][0]['time'], {'start': 100})
                self.assertEqual(opened[-1]['parts'][0]['text'], '')
                sequence = [opened, final] if completes_message else [opened, closed, final]
                originals = copy.deepcopy(sequence)
                for snapshot in sequence:
                    self.assertTrue(history.observe(snapshot))
                    self.assertFalse(history.observe(snapshot))
                    self.assertEqual(history.messages['msg_final'], snapshot[-1])
                self.assertEqual(sequence, originals)
                self.assertNotIn('completed', closed[-1]['info']['time'])
                self.assertNotIn('finish', closed[-1]['info'])
                self.assertEqual(history.messages['msg_final']['parts'][0]['time'],
                                 {'start': 120, 'end': 121})
                parent = history.validate_final(final, final[-1])
                data, _ = extract_result(final[-1], 'ses_test', 'msg_root', 'audit', expected_parent=parent)
                self.assertEqual(data, {'ok': True})

    def test_text_completion_is_independent_of_compaction_count_and_phase(self):
        for count in (1, 3):
            with self.subTest(compactions=count):
                history = self.history()
                sequence = text_completion_snapshots(chain(count))
                for snapshot in sequence:
                    history.observe(snapshot)
                self.assertEqual(history.transitions, count)
                history.validate_final(sequence[-1], sequence[-1][-1])
        messages = chain()[:4]
        summary = messages[-1]
        summary['info']['time'] = {'created': 90}
        summary['info'].pop('finish')
        summary['parts'][0].update(text='', time={'start': 100})
        history = self.history()
        history.observe(messages)
        self.assertEqual(history.phase, 'summary')
        summary['parts'][0].update(text='Summary.', time={'start': 120, 'end': 121})
        self.assertTrue(history.observe(messages))
        self.assertEqual(history.phase, 'summary')
        summary['info'].update(time={'created': 90, 'completed': 130}, finish='stop')
        history.observe(messages)
        self.assertEqual(history.phase, 'continuation')
        self.assertEqual(history.completed, 1)

    def test_completion_does_not_relax_other_timestamp_transitions(self):
        for kind, ended, timing in (
                ('text', False, {'start': 120}),
                ('reasoning', False, {'start': 120}),
                ('reasoning', False, {'start': 120, 'end': 121}),
                ('text', True, {'start': 119, 'end': 121}),
                ('text', True, {'start': 120, 'end': 122}),
                ('text', True, {'start': 120}),
                ('text', True, None)):
            with self.subTest(kind=kind, ended=ended, timing=timing):
                opened, closed, _ = text_completion_snapshots(chain(0))
                messages = closed if ended else opened
                streamed = messages[-1]['parts'][0]
                streamed['type'] = kind
                history = self.history()
                history.observe(messages)
                if timing is None:
                    streamed.pop('time')
                else:
                    streamed['time'] = timing
                self.rejected(messages, 'PART_TIME_CHANGED', history=history)

    def test_completion_validates_received_timestamp_shape_before_merging(self):
        invalid = (True, False, None, '120', float('nan'), float('inf'), -float('inf'), -1, 10 ** 400)
        timings = [{'start': value, 'end': 121} for value in invalid]
        timings += [{'start': 120, 'end': value} for value in invalid]
        timings += [{'end': 121}, {'start': 120, 'end': 119},
                    {'start': 120, 'end': 121, 'extra': 1}]
        for timing in timings:
            with self.subTest(timing=timing):
                opened, closed, _ = text_completion_snapshots(chain(0))
                history = self.history()
                history.observe(opened)
                closed[-1]['parts'][0]['time'] = timing
                self.rejected(closed, 'PART_TIME_INVALID', history=history)
        # The existing numeric policy accepts finite floats and equal endpoints.
        for timing in ({'start': 120.5, 'end': 121.5}, {'start': 120, 'end': 120}):
            opened, closed, _ = text_completion_snapshots(chain(0))
            history = self.history()
            history.observe(opened)
            closed[-1]['parts'][0]['time'] = timing
            history.observe(closed)
            self.assertEqual(history.messages['msg_final']['parts'][0]['time'], timing)

    def test_text_completion_preserves_identity_ownership_and_request_protection(self):
        for target, key, value, code in (
                ('info', 'sessionID', 'ses_other', 'FOREIGN_SESSION'),
                ('info', 'parentID', 'msg_other', 'MESSAGE_IDENTITY_CHANGED'),
                ('info', 'agent', 'other', 'MESSAGE_IDENTITY_CHANGED'),
                ('info', 'id', 'msg_other', 'FOREIGN_PART'),
                ('part', 'sessionID', 'ses_other', 'FOREIGN_PART'),
                ('part', 'messageID', 'msg_other', 'FOREIGN_PART'),
                ('part', 'type', 'reasoning', 'PART_IDENTITY_CHANGED'),
                ('part', 'synthetic', True, 'PART_IDENTITY_CHANGED'),
                ('part', 'ignored', True, 'PART_IDENTITY_CHANGED')):
            with self.subTest(target=target, key=key):
                opened, closed, _ = text_completion_snapshots(chain(0))
                history = self.history()
                history.observe(opened)
                value_target = closed[-1]['info'] if target == 'info' else closed[-1]['parts'][0]
                value_target[key] = value
                self.rejected(closed, code, history=history)
        for rewrite in (False, True):
            opened, closed, _ = text_completion_snapshots(chain(0))
            opened[0]['parts'][0]['time'] = {'start': 0}
            closed[0]['parts'][0]['time'] = {'start': 1, 'end': 2}
            if rewrite:
                closed[0]['parts'][0]['text'] += ' changed'
            history = self.history()
            history.observe(opened)
            self.rejected(closed, 'USER_PART_CHANGED', history=history)

    def test_text_completion_does_not_accept_invalid_native_results(self):
        for invalid in ('missing-native', 'missing-tool', 'unfinished', 'mismatch', 'type-mismatch'):
            with self.subTest(invalid=invalid):
                opened, closed, final = text_completion_snapshots(chain(0))
                history = self.history()
                history.observe(opened)
                history.observe(closed)
                envelope = final[-1]
                if invalid == 'missing-native': envelope['info'].pop('structured')
                if invalid == 'missing-tool': envelope['parts'].pop()
                if invalid == 'unfinished': envelope['parts'][-1]['state']['status'] = 'running'
                if invalid == 'mismatch': envelope['parts'][-1]['state']['input'] = {}
                if invalid == 'type-mismatch': envelope['parts'][-1]['state']['input'] = {'ok': 1}
                history.observe(final)
                parent = history.validate_final(final, envelope)
                with self.assertRaises(ContractError):
                    extract_result(envelope, 'ses_test', 'msg_root', 'audit', expected_parent=parent)

    def test_summary_does_not_weaken_protected_fields(self):
        cases = (('sessionID', 'ses_other'), ('role', 'assistant'), ('agent', 'private-agent'),
                 ('parentID', 'msg_other'), ('format', body()['format'] | {'retryCount': True}),
                 ('model', {'providerID': 'provider', 'modelID': 'other'}), ('system', 'private-system'),
                 ('tools', {'read': False}), ('variant', 'other'), ('time', {'created': 1}))
        for with_summary in (False, True):
            for key, value in cases:
                with self.subTest(key=key, summary=with_summary):
                    messages = chain(0)
                    history = self.history()
                    history.observe(messages)
                    messages[0]['info'][key] = value
                    if with_summary:
                        messages[0]['info']['summary'] = {'title': 'private-title', 'diffs': []}
                    with self.assertRaises(ContractError) as caught:
                        history.observe(messages)
                    self.assertEqual(caught.exception.details['field'], 'info.time.created' if key == 'time' else 'info.' + key)
                    self.assertEqual(caught.exception.details['transitions'], 0)
                    self.assertEqual(caught.exception.details['phase'], 'stage')
                    self.assertNotIn('private-', str(caught.exception))
                    self.assertEqual(history.events[-1]['event'], 'session_rejected')
        # A changed message ID is also bound by the unchanged part ownership.
        messages = chain(0)
        history = self.history()
        history.observe(messages)
        messages[0]['info']['id'] = 'msg_replacement'
        messages[0]['parts'][0]['messageID'] = 'msg_replacement'
        self.rejected(messages, 'PART_OWNER_CHANGED', history=history)
        # Changing the entire root identity does not establish another request.
        messages[0]['parts'][0]['id'] = 'prt_replacement'
        self.rejected(messages, 'ROOT_REQUEST_MISSING')

    def test_user_summary_does_not_relax_assistant_summary_identity(self):
        for previous, update in ((None, True), (False, True), (True, False), (False, 0), (True, 1)):
            messages = chain()[:4] if previous is True else chain(0)
            if previous is not None:
                messages[-1]['info']['summary'] = previous
            history = self.history()
            history.observe(messages)
            messages[0]['info']['summary'] = {'diffs': []}
            messages[-1]['info']['summary'] = update
            self.rejected(messages, 'MESSAGE_IDENTITY_CHANGED', history=history)

    def test_replaced_assistant_ids_cannot_erase_the_observed_final_history(self):
        messages = chain(0)
        history = self.history()
        history.observe(messages)
        messages[-1]['info']['id'] = 'msg_replacement'
        for index, item in enumerate(messages[-1]['parts']):
            item.update(messageID='msg_replacement', id='prt_replacement' + str(index))
        history.observe(messages)  # A new assistant may appear in a subset poll.
        with self.assertRaises(ContractError) as caught:
            history.validate_final(messages, messages[-1])
        self.assertEqual(caught.exception.details['code'], 'FINAL_HISTORY_INCOMPLETE')

    def test_unknown_info_fields_have_closed_policy_and_safe_diagnostics(self):
        for index in (0, -1):
            for initial in (False, True):
                messages = chain(0)
                history = self.history()
                if not initial:
                    history.observe(messages)
                messages[index]['info']['private-secret-key'] = 'private-value'
                error = self.rejected(messages, 'MESSAGE_FIELD_UNSUPPORTED', history=history)
                self.assertEqual(error.details['field'], 'info.unknown')
                self.assertNotIn('private', str(error))
                self.assertNotIn('private', str(history.events))

    def test_stream_rewrites_truncation_and_completed_changes_are_rejected(self):
        for kind in ('text', 'reasoning'):
            for previous, update, ended, closing in (
                    ('Result.\n', 'Result.', False, False),
                    ('Result.', 'Result', False, True),
                    ('Result.', 'Replaced.', False, True),
                    ('Result.\n', 'Result.More', False, True),
                    ('Result.\x85', 'Result.', False, True),
                    ('Result.\x1c', 'Result.', False, True),
                    ('Result.\u200b', 'Result.', False, True),
                    ('Result.\n', 'Result.', True, True),
                    ('Result.', 'Result.More', True, True)):
                with self.subTest(kind=kind, previous=previous, update=update, ended=ended, closing=closing):
                    messages = chain(0)
                    streamed = messages[-1]['parts'][0]
                    streamed.update(type=kind, text=previous, time={'start': 1})
                    if ended: streamed['time']['end'] = 2
                    history = self.history()
                    history.observe(messages)
                    streamed['text'] = update
                    if closing: streamed['time']['end'] = 2
                    if closing and not ended and kind == 'text': streamed['time']['start'] = 2
                    error = self.rejected(messages, 'PART_CONTENT_CHANGED', history=history)
                    self.assertEqual(error.details['part_type'], kind)
                    self.assertEqual(error.details['field'], 'part.text')

    def test_stream_timing_and_terminal_metadata_are_protected(self):
        for update, code in (({'start': 2}, 'PART_TIME_CHANGED'),
                             ({'start': 1, 'end': True}, 'PART_TIME_INVALID'),
                             ({'start': 1, 'end': 0}, 'PART_TIME_INVALID')):
            messages = chain(0)
            messages[-1]['parts'][0]['time'] = {'start': 1}
            history = self.history()
            history.observe(messages)
            messages[-1]['parts'][0]['time'] = update
            self.rejected(messages, code, history=history)
        messages = chain(0)
        messages[-1]['parts'][0]['time'] = {'start': 1, 'end': 2}
        history = self.history()
        history.observe(messages)
        messages[-1]['parts'][0]['metadata'] = {'private': 'new'}
        self.rejected(messages, 'COMPLETED_PART_CHANGED', history=history)

    def test_ecmascript_final_whitespace_set_and_open_append(self):
        # Independent reference list; do not derive expected behavior from the validator.
        characters = [9, 10, 11, 12, 13, 32, 0xa0, 0x1680, *range(0x2000, 0x200b),
                      0x2028, 0x2029, 0x202f, 0x205f, 0x3000, 0xfeff]
        for kind in ('text', 'reasoning'):
            for codepoint in characters:
                messages = chain(0)
                streamed = messages[-1]['parts'][0]
                streamed.update(type=kind, text='Text', time={'start': 1})
                history = self.history()
                history.observe(messages)
                streamed['text'] += '.' + chr(codepoint)
                self.assertTrue(history.observe(messages))
                streamed.update(text='Text.', time={'start': 2 if kind == 'text' else 1, 'end': 2})
                self.assertTrue(history.observe(messages))

    def test_partial_message_and_part_timing_shape_is_checked_on_each_update(self):
        for timing in (None, [], {'created': True}, {'created': 0, 'completed': False}):
            messages = chain(0)
            history = self.history()
            history.observe(messages)
            messages[-1]['info']['time'] = timing
            self.rejected(messages, 'MESSAGE_TIME_INVALID', history=history)
        messages = chain()[:4]
        messages[-1]['parts'][0]['time'] = {'start': 1}
        history = self.history()
        history.observe(messages)
        self.assertEqual(history.phase, 'summary')
        messages[-1]['parts'][0]['time']['end'] = 2
        history.observe(messages)
        self.assertEqual(history.phase, 'continuation')

    def test_received_root_settings_are_checked_against_the_request(self):
        for key, value in (('system', 'original'), ('variant', 'original'), ('tools', {'read': True}),
                           ('model', {'providerID': 'provider', 'modelID': 'other'})):
            request = body() | {key: value}
            history = SessionHistory('ses_test', 'msg_root', request)
            error = self.rejected(chain(0), 'ROOT_SETTINGS_MISMATCH', history=history)
            self.assertEqual(error.details['field'], 'info.' + key)

    def test_summary_metadata_and_repeats_do_not_refresh_idle_or_usage(self):
        now = [0]
        budget = Budget(5, 2, clock=lambda: now[0])
        messages = chain(0)
        history = self.history()
        history.observe(messages)
        collector = HTTPUsage('xxx', None, 'ses_test', 'msg_root', 'audit')
        usage = collector.validated(history)
        for moment in (1, 1.5, 2):
            now[0] = moment
            messages[0]['info']['summary'] = {'title': str(moment), 'diffs': []}
            if history.observe(messages): budget.activity()
            self.assertEqual(collector.validated(history), usage)
        self.assertEqual(budget.last_activity, 0)
        with self.assertRaises(ContractError) as caught: budget.check()
        self.assertEqual(caught.exception.failure_kind, 'IDLE_TIMEOUT')

    def test_continuations_cannot_restart_total_budget(self):
        now = [0]
        budget = Budget(5, 2, clock=lambda: now[0])
        history = self.history()
        for moment, count in ((1, 1), (2, 2), (3, 3)):
            now[0] = moment
            # Observe each full transition, before the final stage response.
            if history.observe(chain(count)[:-1]): budget.activity()
        self.assertEqual(budget.deadline, 5)
        now[0] = 5
        with self.assertRaises(ContractError) as caught: budget.check()
        self.assertEqual(caught.exception.failure_kind, 'STAGE_TIMEOUT')

    def test_synthetic_flag_alone_or_incomplete_summary_never_authorizes(self):
        messages = chain()
        messages[2]['parts'] = copy.deepcopy(messages[4]['parts'])
        messages[2]['info']['format'] = body()['format']
        for item in messages[2]['parts']:
            item.update(messageID=messages[2]['info']['id'], id='prt_forged')
        self.rejected(messages, 'UNEXPECTED_USER_REQUEST')
        for missing in ('completed', 'text'):
            messages = chain()
            if missing == 'completed': messages[3]['info']['time'].pop('completed')
            else: messages[3]['parts'] = []
            self.rejected(messages, 'UNEXPECTED_USER_REQUEST')
        messages = chain()
        messages[4]['info']['parentID'] = messages[3]['info']['id']
        self.rejected(messages, 'USER_PARENT_UNSUPPORTED')

    def test_continuation_settings_preserved_with_summary_updates(self):
        for field, value in (('system', 'private'), ('tools', {'read': True}), ('variant', 'selected')):
            messages = chain()
            for message in messages:
                if message['info']['role'] == 'user':
                    message['info'][field] = value
                    message['info']['summary'] = {'diffs': []}
            history = self.history()
            history.observe(messages)
            history.validate_final(messages, messages[-1])
            for replacement in (None, {'changed': True} if field == 'tools' else 'other'):
                changed = copy.deepcopy(messages)
                if replacement is None: changed[4]['info'].pop(field)
                else: changed[4]['info'][field] = replacement
                self.rejected(changed, 'USER_SETTINGS_CHANGED')

    def test_format_is_authoritative_not_taken_from_summary(self):
        for change, code in ((None, 'COMPACTION_FORMAT_MISSING'),
                             ({'type': 'text'}, 'COMPACTION_FORMAT_CHANGED'),
                             ({'type': 'json_schema', 'schema': {}, 'retryCount': 0}, 'COMPACTION_FORMAT_CHANGED'),
                             (body()['format'] | {'retryCount': True}, 'COMPACTION_FORMAT_CHANGED')):
            for index in (0, 4):
                messages = chain()
                if change is None:
                    del messages[index]['info']['format']
                else:
                    messages[index]['info']['format'] = change
                if change is None and index == 4:
                    self.assertIsInstance(self.history().observe(messages), RecoveryRequired)
                else:
                    self.rejected(messages, code)

    def test_missing_format_after_completed_summary_has_typed_recoverable_state(self):
        # Reproduce the reported failure, including loss on a later compaction.
        # Stop at the continuation: no fabricated final result is needed.
        for count in (1, 2):
            with self.subTest(compactions=count):
                messages = chain(count)[:-1]
                del messages[-1]['info']['format']
                original = copy.deepcopy(messages)
                history = self.history()
                for snapshot in snapshots(messages[:-1]):
                    history.observe(snapshot)
                self.assertEqual(history.phase, 'continuation')
                self.assertEqual(history.completed, count)
                self.assertEqual(history.continuations, count - 1)
                state = history.observe(messages)
                self.assertIsInstance(state, RecoveryRequired)
                self.assertEqual(state.continuation_id, messages[-1]['info']['id'])
                self.assertFalse(history.failed)
                self.assertEqual([e['event'] for e in history.events[-3:]],
                                 ['compaction_started', 'compaction_completed', 'compaction_recovery_detected'])
                self.assertIn(messages[-1]['info']['id'], history.origins)
                self.assertEqual(messages, original)

    def test_service_format_is_optional_but_never_allowed_to_conflict(self):
        # Existing supported fields can carry a native repair's contract. This
        # checks only client acceptance, not native persistence/tool selection.
        for count in (1, 2):
            messages = chain(count)
            services = [m for m in messages if m['parts'][0]['type'] == 'compaction']
            for message in services:
                message['info']['format'] = copy.deepcopy(body()['format'])
            history = self.history()
            for snapshot in snapshots(messages):
                history.observe(snapshot)
            history.validate_final(messages, messages[-1])
            self.assertEqual(history.continuations, count)
            for retry in (True, False, None, '0', 1, -1):
                with self.subTest(compactions=count, retry=retry):
                    changed = copy.deepcopy(messages)
                    service = next(m for m in changed if m['info']['id'] == services[-1]['info']['id'])
                    service['info']['format']['retryCount'] = retry
                    self.rejected(changed, 'COMPACTION_FORMAT_CHANGED')

    def test_interleaved_sessions_keep_complete_independent_contracts(self):
        histories, sequences, originals = [], [], []
        for number in (1, 2):
            request = body()
            request['format']['schema'] = {
                'type': 'object', 'required': ['ok'], 'additionalProperties': False,
                'properties': {'ok': {'type': 'boolean', 'enum': [True]}},
                'description': 'Contract ' + str(number),
                '$defs': {'nested': {'type': 'array', 'items': {'type': 'string', 'enum': ['a', 'b']}}}}
            final = assistant()
            session = 'ses_contract' + str(number)
            final['info']['sessionID'] = session
            messages = chain(2, request, final)
            for message in messages:
                if message['parts'][0]['type'] == 'compaction':
                    message['info']['format'] = copy.deepcopy(request['format'])
            histories.append(SessionHistory(session, request['messageID'], request))
            sequences.append(snapshots(messages))
            originals.append(copy.deepcopy(request['format']))
        for pair in zip(*sequences):
            for history, snapshot, expected in zip(histories, pair, originals):
                history.observe(snapshot)
                self.assertEqual(history.body['format'], expected)
                for message in snapshot:
                    if message['info']['role'] == 'user':
                        self.assertEqual(message['info']['format'], expected)
                        self.assertIs(type(message['info']['format']['retryCount']), int)
        for history, sequence in zip(histories, sequences):
            history.validate_final(sequence[-1], sequence[-1][-1])
            self.assertEqual(history.continuations, 2)
        # A different session's valid contract must still be rejected here.
        changed = copy.deepcopy(sequences[0][-1])
        changed[-2]['info']['format'] = originals[1]
        history = SessionHistory(histories[0].session_id, 'msg_root', histories[0].body)
        self.rejected(changed, 'COMPACTION_FORMAT_CHANGED', history=history)

    def test_unknown_user_foreign_session_parent_and_part_ownership(self):
        cases = [
            (2, 'info', 'sessionID', 'ses_other', 'FOREIGN_SESSION'),
            (3, 'info', 'parentID', 'msg_root', 'COMPACTION_SUMMARY_IDENTITY'),
            (5, 'info', 'parentID', 'msg_root', 'UNCONFIRMED_ASSISTANT_PARENT'),
            (4, 'info', 'agent', 'other', 'USER_AGENT_CHANGED'),
            (4, 'info', 'model', {'providerID': 'other', 'modelID': 'model'}, 'USER_SETTINGS_CHANGED')]
        for index, section, key, value, code in cases:
            messages = chain()
            messages[index][section][key] = value
            with self.subTest(code=code):
                self.rejected(messages, code)
        messages = chain()
        messages[2]['parts'] = [part('msg_compact0', 'text', text='ordinary user')]
        self.rejected(messages, 'UNEXPECTED_USER_REQUEST')
        messages = chain()
        messages[3]['parts'][0]['messageID'] = 'msg_root'
        self.rejected(messages, 'FOREIGN_PART')
        messages = chain()
        messages[4]['parts'][0]['text'] = 'synthetic forgery'
        self.rejected(messages, 'CONTINUATION_FORM_UNSUPPORTED')
        messages = chain()
        messages[3], messages[4] = messages[4], messages[3]
        self.rejected(messages, 'UNEXPECTED_USER_REQUEST')

    def test_duplicate_and_mutated_identities(self):
        messages = chain(0)
        self.rejected(messages + [messages[-1]], 'DUPLICATE_MESSAGE_ID')
        messages = chain(0)
        messages[-1]['parts'].append(copy.deepcopy(messages[-1]['parts'][0]))
        self.rejected(messages, 'DUPLICATE_PART_ID')
        for key, value in (('parentID', 'msg_other'), ('agent', 'other'), ('modelID', 'other'), ('summary', True)):
            history = self.history()
            history.observe(chain(0))
            messages = chain(0)
            messages[-1]['info'][key] = value
            self.rejected(messages, 'MESSAGE_IDENTITY_CHANGED', history=history)
        history = self.history()
        history.observe(chain(0))
        messages = chain(0)
        messages[0]['parts'][0]['text'] += ' modified'
        self.rejected(messages, 'USER_PART_CHANGED', history=history)

    def test_parts_stream_but_terminal_tools_and_part_types_do_not_change(self):
        messages = chain(0)
        last = messages[-1]
        last['info']['time'].pop('completed')
        last['info'].pop('structured')
        last['info'].pop('finish')
        last['parts'][0]['text'] = 'Untrusted'
        last['parts'][1]['state']['status'] = 'running'
        history = self.history()
        history.observe(messages)
        history.observe(chain(0))
        messages = chain(0)
        messages[-1]['parts'][1]['state']['status'] = 'running'
        self.rejected(messages, 'TOOL_STATE_REGRESSED', history=history)
        history = self.history()
        history.observe(chain(0))
        messages = chain(0)
        messages[-1]['parts'][0]['type'] = 'reasoning'
        self.rejected(messages, 'PART_IDENTITY_CHANGED', history=history)

    def test_invalid_shapes_and_unsupported_modes_have_controlled_errors(self):
        for index, key, value, code in ((0, 'time', [], 'MESSAGE_TIME_INVALID'),
                (3, 'time', {'created': True}, 'MESSAGE_TIME_INVALID'),
                (1, 'structured', {}, 'COMPACTION_AFTER_STAGE_RESULT')):
            messages = chain()
            messages[index]['info'][key] = value
            self.rejected(messages, code)
        messages = chain()
        messages[2]['parts'][0]['overflow'] = True
        self.rejected(messages, 'COMPACTION_FORM_UNSUPPORTED')
        messages = chain()
        messages[2]['parts'][0]['auto'] = False
        self.rejected(messages, 'COMPACTION_FORM_UNSUPPORTED')
        messages = chain(0)
        messages[-1]['parts'][-1]['state'] = None
        self.rejected(messages, 'TOOL_PART_INVALID')

    def test_unclassified_user_cannot_authorize_an_assistant_or_finish(self):
        messages = chain()
        messages[4]['parts'] = []
        history = self.history()
        history.observe(messages)
        self.assertNotIn(messages[-1]['info']['id'], history.origins)
        with self.assertRaises(ContractError) as caught:
            history.validate_final(messages, messages[-1])
        self.assertEqual(caught.exception.details['code'], 'TRANSITION_NOT_FINISHED')

    def test_part_owner_identity_and_terminal_updates(self):
        history = self.history()
        messages = chain(0)
        history.observe(messages)
        extra = assistant('msg_other')
        extra['parts'][0]['id'] = messages[-1]['parts'][0]['id']
        self.rejected([extra], 'PART_OWNER_CHANGED', history=history)
        history = self.history()
        messages = chain(0)
        history.observe(messages)
        messages[-1]['parts'][1]['state']['input'] = {'changed': True}
        self.rejected(messages, 'COMPLETED_TOOL_CHANGED', history=history)
        history = self.history()
        messages = chain(0)
        history.observe(messages)
        # Real prune adds a timestamp to the old tool, never rewrites its input.
        messages[-1]['parts'][1]['state']['time']['compacted'] = 3
        history.observe(messages)
        history = self.history()
        messages = chain(0)
        history.observe(messages)
        messages[0]['parts'].append(part('msg_root', 'reasoning', text='new instructions'))
        self.rejected(messages, 'USER_PART_ADDED', history=history)

    def test_missing_snapshots_do_not_erase_history_or_count_as_activity(self):
        history = self.history()
        messages = chain(0)
        history.observe(messages)
        self.assertFalse(history.observe([]))
        self.assertFalse(history.observe([messages[-1]]))
        with self.assertRaises(ContractError) as caught:
            history.validate_final([messages[-1]], messages[-1])
        self.assertEqual(caught.exception.details['code'], 'FINAL_HISTORY_INCOMPLETE')

    def test_order_comes_from_sequence_not_sortable_message_ids(self):
        request = body() | {'messageID': 'msg_zzzz'}
        messages = chain(0, request=request)
        history = SessionHistory('ses_test', request['messageID'], request)
        history.observe(messages)
        self.assertEqual(history.validate_final(messages, messages[-1]), 'msg_zzzz')
        self.rejected(list(reversed(messages)), 'HISTORY_ORDER_CHANGED', history=history)

    def test_final_envelope_must_be_latest_and_match_and_use_native_tool(self):
        for change, expected in (('text', 'FINAL_SNAPSHOT_MISMATCH'), ('previous', 'FINAL_NOT_LATEST_STAGE_RESULT')):
            messages = chain()
            history = self.history()
            history.observe(messages)
            envelope = copy.deepcopy(messages[-1] if change == 'text' else messages[1])
            if change == 'text':
                envelope['parts'][0]['text'] = 'changed'
            with self.assertRaises(ContractError) as caught:
                history.validate_final(messages, envelope)
            self.assertEqual(caught.exception.details['code'], expected)
        for change in ('missing-native', 'missing-tool', 'unfinished', 'tool-mismatch', 'tool-type-mismatch', 'summary'):
            messages = chain(0)
            envelope = messages[-1]
            if change == 'missing-native': del envelope['info']['structured']
            if change == 'missing-tool': envelope['parts'].pop()
            if change == 'unfinished': envelope['parts'][-1]['state']['status'] = 'pending'
            if change == 'tool-mismatch': envelope['parts'][-1]['state']['input'] = {}
            if change == 'tool-type-mismatch': envelope['parts'][-1]['state']['input'] = {'ok': 1}
            if change == 'summary': envelope['info']['summary'] = True
            with self.subTest(change=change), self.assertRaises(ContractError):
                extract_result(envelope, 'ses_test', 'msg_root', 'audit')

    def test_malformed_final_identifier_is_a_transport_error(self):
        history = self.history()
        messages = chain(0)
        history.observe(messages)
        for invalid_id in ({}, [], None, 'bad id'):
            envelope = copy.deepcopy(messages[-1])
            envelope['info']['id'] = invalid_id
            with self.assertRaises(ContractError) as caught:
                history.validate_final(messages, envelope)
            self.assertEqual(caught.exception.failure_kind, 'TRANSPORT_ERROR')

    def test_other_backend_history_is_still_closed(self):
        with self.assertRaises(ContractError):
            validate_history(chain(), 'ses_test', 'msg_root')

    def test_stage_and_idle_budgets_are_not_restarted_by_compaction_or_repeats(self):
        clock = [0]
        history = self.history()
        budget = Budget(5, 2, clock=lambda: clock[0])
        for moment, snapshot in ((1, chain()[:2]), (2, chain()[:4])):
            clock[0] = moment
            if history.observe(snapshot): budget.activity()
        clock[0] = 4
        self.assertFalse(history.observe(chain()[:4]))
        with self.assertRaises(ContractError) as caught: budget.check()
        self.assertEqual(caught.exception.failure_kind, 'IDLE_TIMEOUT')
        self.assertEqual(budget.deadline, 5)


class CompactionUsageTests(unittest.TestCase):
    def test_usage_progresses_until_completion_then_totals_are_protected(self):
        messages = chain(0)
        info = messages[-1]['info']
        info['time'].pop('completed')
        info['tokens']['output'] = 0
        history = SessionHistory('ses_test', 'msg_root', body())
        history.observe(messages)
        collector = HTTPUsage('xxx', None, 'ses_test', 'msg_root', 'audit')
        self.assertIsNone(collector.validated(history)['usage']['total_tokens'])
        info['tokens']['output'] = 1
        info['time']['completed'] = 2
        history.observe(messages)
        history.validate_final(messages, messages[-1])
        result = collector.validated(history, complete=True)
        self.assertEqual(result['usage']['total_tokens'], 4)
        info['tokens']['output'] = True  # bool cannot masquerade as 1
        with self.assertRaises(ContractError) as caught:
            history.observe(messages)
        self.assertEqual(caught.exception.details['code'], 'COMPLETED_USAGE_CHANGED')
        self.assertEqual(collector.validated(history)['usage']['total_tokens'], 4)
        self.assertEqual(collector.validated(history)['usage']['coverage']['total_tokens'], 'partial')

    def test_same_actual_model_keeps_distinct_origins(self):
        messages = chain()
        messages[3]['info']['modelID'] = 'study'
        history = SessionHistory('ses_test', 'msg_root', body())
        history.observe(messages)
        collector = HTTPUsage('xxx', None, 'ses_test', 'msg_root', 'audit')
        result = summarize([{'backend': 'xxx', 'metrics': collector.validated(history, complete=True)}])
        self.assertEqual(len(result['by_model']), 2)
        self.assertEqual({e['origin'] for e in result['by_model']}, {'stage', 'compaction'})

    def test_usage_before_during_and_after_with_dedup_and_provenance(self):
        history = SessionHistory('ses_test', 'msg_root', body())
        collector = HTTPUsage('xxx', 'provider/study', 'ses_test', 'msg_root', 'audit')
        messages = chain(2)
        messages[1]['info']['tokens'] = messages[1]['info']['tokens'] | {'total': 999}  # steps win
        for snapshot in snapshots(messages):
            history.observe(snapshot)
            partial = collector.validated(history)
            self.assertNotEqual(partial['usage']['coverage']['total_tokens'], 'complete')
            self.assertEqual(collector.validated(history), partial)
        result = collector.validated(history, complete=True)
        self.assertEqual(result['usage']['total_tokens'], 20)
        self.assertEqual(result['usage']['coverage']['total_tokens'], 'complete')
        grouped = summarize([{'backend': 'xxx', 'metrics': result}])
        self.assertEqual(grouped['attempts'], 1)
        entries = {r['origin']: r for r in grouped['by_model']}
        self.assertEqual(entries['compaction']['usage']['total_tokens'], 8)
        self.assertEqual(entries['compaction']['model_actual'], 'provider/small')
        self.assertIsNone(entries['compaction']['model_requested'])
        self.assertEqual(entries['stage']['usage']['total_tokens'], 12)

    def test_recoverable_continuation_accounts_verified_discarded_usage_as_partial(self):
        history = SessionHistory('ses_test', 'msg_root', body())
        messages = chain()
        del messages[4]['info']['format']
        self.assertIsInstance(history.observe(messages), RecoveryRequired)
        collector = HTTPUsage('xxx', None, 'ses_test', 'msg_root', 'audit')
        result = collector.validated(history, complete=True)
        self.assertEqual(result['usage']['total_tokens'], 12)
        self.assertEqual(result['usage']['coverage']['total_tokens'], 'partial')
        self.assertEqual({r['origin'] for r in result['by_model']}, {'stage', 'compaction', 'discarded'})

    def test_missing_summary_counters_stay_unavailable(self):
        messages = chain()
        messages[3]['info'].pop('tokens')
        history = SessionHistory('ses_test', 'msg_root', body())
        history.observe(messages)
        result = HTTPUsage('xxx', None, 'ses_test', 'msg_root', 'audit').validated(history, complete=True)
        summary = next(e for e in result['by_model'] if e['origin'] == 'compaction')
        self.assertIsNone(summary['usage']['total_tokens'])
        self.assertEqual(summary['usage']['coverage']['total_tokens'], 'unavailable')
        self.assertEqual(result['usage']['coverage']['total_tokens'], 'partial')


if __name__ == '__main__':
    unittest.main()
