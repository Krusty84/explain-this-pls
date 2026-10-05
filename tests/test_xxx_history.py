#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline membership/format/usage tests; successful continuations are synthetic."""
import copy
import unittest

from src.backends.xxx_history import SessionHistory
from src.backends.opencode import extract_result, validate_history
from src.contracts.contracts import ContractError
from src.runtime.execution import Budget
from src.runtime.metrics import HTTPUsage, summarize
from fixtures.compaction_protocol import assistant, body, chain, part, snapshots


class SyntheticHistory(SessionHistory):
    """Test-only assumed pre-model preservation; no production setting enables it."""
    def authorize_continuation(self):
        pass


class HistoryTests(unittest.TestCase):
    def history(self, cls=SyntheticHistory):
        return cls('ses_test', 'msg_root', body())

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
        history = self.history(SessionHistory)
        self.assertTrue(history.observe(messages))
        self.assertFalse(history.observe(messages))
        self.assertEqual(history.phase, 'summary')
        self.assertEqual(history.transitions, 1)
        with self.assertRaises(ContractError) as caught:
            history.validate_final(messages, messages[-1])
        self.assertEqual(caught.exception.details['code'], 'COMPACTION_IS_NOT_STAGE_RESULT')

    def test_production_gate_does_not_treat_wire_format_as_pre_model_proof(self):
        error = self.rejected(chain(), 'COMPACTION_FORMAT_RETENTION_UNVERIFIED', history=self.history(SessionHistory))
        self.assertEqual(error.failure_kind, 'BACKEND_INCOMPATIBLE')

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
                self.rejected(messages, code)

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
        history = SyntheticHistory('ses_test', request['messageID'], request)
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
    def test_same_actual_model_keeps_distinct_origins(self):
        messages = chain()
        messages[3]['info']['modelID'] = 'study'
        history = SyntheticHistory('ses_test', 'msg_root', body())
        history.observe(messages)
        collector = HTTPUsage('xxx', None, 'ses_test', 'msg_root', 'audit')
        result = summarize([{'backend': 'xxx', 'metrics': collector.validated(history, complete=True)}])
        self.assertEqual(len(result['by_model']), 2)
        self.assertEqual({e['origin'] for e in result['by_model']}, {'stage', 'compaction'})

    def test_usage_before_during_and_after_with_dedup_and_provenance(self):
        history = SyntheticHistory('ses_test', 'msg_root', body())
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

    def test_failed_continuation_keeps_only_verified_partial_usage(self):
        history = SessionHistory('ses_test', 'msg_root', body())
        with self.assertRaises(ContractError): history.observe(chain())
        collector = HTTPUsage('xxx', None, 'ses_test', 'msg_root', 'audit')
        result = collector.validated(history, complete=True)
        self.assertEqual(result['usage']['total_tokens'], 8)
        self.assertEqual(result['usage']['coverage']['total_tokens'], 'partial')
        self.assertEqual({r['origin'] for r in result['by_model']}, {'stage', 'compaction'})

    def test_missing_summary_counters_stay_unavailable(self):
        messages = chain()
        messages[3]['info'].pop('tokens')
        history = SyntheticHistory('ses_test', 'msg_root', body())
        history.observe(messages)
        result = HTTPUsage('xxx', None, 'ses_test', 'msg_root', 'audit').validated(history, complete=True)
        summary = next(e for e in result['by_model'] if e['origin'] == 'compaction')
        self.assertIsNone(summary['usage']['total_tokens'])
        self.assertEqual(summary['usage']['coverage']['total_tokens'], 'unavailable')
        self.assertEqual(result['usage']['coverage']['total_tokens'], 'partial')


if __name__ == '__main__':
    unittest.main()
