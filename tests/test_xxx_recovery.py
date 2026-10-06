# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Recovery through the production HTTP transport, with a deterministic local server."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fixtures.compaction_protocol import body, chain, root
from src.backends.xxx import Server
from src.backends.xxx_history import RecoveryBudget, RecoveryRequired, SessionHistory
from src.contracts.contracts import ContractError
from src.runtime.execution import Budget


class Wire:
    def __init__(self, losses=1, mode='natural', overflow=False):
        self.losses, self.mode, self.overflow = losses, mode, overflow
        self.messages, self.posts, self.events = [], [], []
        self.active = 0
        self.parallel = False
        self.release = threading.Event()
        self.abort = threading.Event()

    def handler(self):
        wire = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, value):
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                self.send(copy.deepcopy(wire.messages) if self.path.endswith('/message') else {'ses_test': {'type': 'idle'}})

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'null')
                if self.path == '/session':
                    self.send({'id': 'ses_test'})
                    return
                if self.path.endswith('/abort'):
                    wire.events.append('abort')
                    wire.abort.set()
                    if wire.mode == 'abort-hang':
                        wire.release.wait(2)
                    self.send(True)
                    wire.events.append('idle')
                    return
                index = len(wire.posts)
                wire.posts.append(copy.deepcopy(value))
                wire.events.append('post' + str(index))
                wire.parallel |= wire.active != 0
                wire.active += 1
                wire.abort.clear()
                generated = chain(int(index < wire.losses or (index == 1 and wire.mode == 'retained')), value)
                # Each native cycle allocates new IDs; preserve client user IDs.
                mapping = {m['info']['id']: 'msg_cycle' + str(index) + m['info']['id'][4:]
                           for m in generated[1:]}
                for message in generated:
                    info = message['info']
                    info['id'] = mapping.get(info['id'], info['id'])
                    if 'parentID' in info:
                        info['parentID'] = mapping.get(info['parentID'], info['parentID'])
                    for item in message['parts']:
                        item['id'] = 'prt_cycle' + str(index) + item['id'][4:]
                        item['messageID'] = info['id']
                final = generated[-1]
                if index < wire.losses:
                    del generated[-2]['info']['format']
                    final['info'].pop('structured')
                    final['parts'] = final['parts'][:1]
                    if wire.overflow is None:
                        del generated[2]['parts'][0]['overflow']
                    else:
                        generated[2]['parts'][0]['overflow'] = wire.overflow
                previous = copy.deepcopy(wire.messages)
                wire.messages = previous + copy.deepcopy(generated)
                if index < wire.losses and wire.mode != 'natural':
                    wire.messages[-1]['info']['time'].pop('completed')
                    wire.messages[-1]['info'].pop('finish')
                    wire.abort.wait(2)
                    if wire.mode == 'stuck':
                        wire.release.wait(2)
                    elif wire.mode == 'disconnect':
                        wire.active -= 1
                        self.close_connection = True
                        return
                    elif wire.mode == 'cancelled':
                        final['info']['error'] = {'name': 'MessageAbortedError', 'data': {'message': 'Cancelled'}}
                    elif wire.mode == 'backend-error':
                        final['info']['error'] = {'name': 'APIError', 'data': {'message': 'Failure'}}
                    time.sleep(.15)
                    wire.messages = previous + copy.deepcopy(generated)
                if index and wire.mode == 'recovery-disconnect':
                    wire.active -= 1
                    self.close_connection = True
                    return
                wire.active -= 1
                wire.events.append('finished' + str(index))
                self.send(final)

        return Handler


class RecoveryHTTPTests(unittest.TestCase):
    def run_wire(self, losses=1, mode='natural', overflow=False, *, counter=None, execution=None):
        wire = Wire(losses, mode, overflow)
        http = ThreadingHTTPServer(('127.0.0.1', 0), wire.handler())
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        self.addCleanup(wire.release.set)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        def save(path, value):
            with path.open('xb') as stream:
                stream.write(value.encode() if isinstance(value, str) else value)
        meta = {'backend': 'xxx'}
        execution = execution or {}
        server = Server('', directory, {}, directory,
            Budget(execution.get('stage_timeout_seconds', 5), execution.get('idle_timeout_seconds')),
            save, meta, execution)
        server.port = http.server_port
        server.process = SimpleNamespace(poll=lambda: None)
        server.recovery_budget = counter
        self.addCleanup(lambda: [request.close() for request in server.requests])
        self.wire, self.server, self.meta, self.directory = wire, server, meta, directory
        return server.invoke('Original task', body()['format']['schema'], 'audit', None, 0)

    def test_one_two_and_limit_with_both_overflow_forms(self):
        for count in (1, 2, 3):
            for overflow in (None, False):
                with self.subTest(count=count, overflow=overflow):
                    if count == 3:
                        with self.assertRaises(ContractError) as caught:
                            self.run_wire(count, overflow=overflow)
                        self.assertEqual(caught.exception.failure_kind, 'BACKEND_INCOMPATIBLE')
                        self.assertEqual(caught.exception.details['code'], 'COMPACTION_RECOVERY_LIMIT_EXCEEDED')
                        self.assertEqual(caught.exception.details['reason'], 'COMPACTION_FORMAT_MISSING')
                    else:
                        self.assertEqual(self.run_wire(count, overflow=overflow), {'ok': True})
                        self.assertEqual(self.meta['final_parent_id'], self.wire.posts[-1]['messageID'])
                        self.assertEqual(self.meta['request_id'], self.wire.posts[0]['messageID'])
                        self.assertEqual(self.meta['metrics']['usage']['total_tokens'], 4 + count * 12)
                        self.assertEqual(self.meta['metrics']['usage']['coverage']['total_tokens'], 'complete')
                    self.assertEqual(len(self.wire.posts), min(count, 2) + 1)
                    self.assertFalse(self.wire.parallel)
                    self.assertNotIn('abort', self.wire.events)
                    self.assertEqual(json.loads((self.directory / 'request.json').read_text()), self.wire.posts[0])
                    for index, request in enumerate(self.wire.posts[1:], 1):
                        self.assertEqual(request['format'], self.wire.posts[0]['format'])
                        self.assertEqual(request['model'], body()['model'])
                        self.assertLess(len(request['parts'][0]['text']), 200)
                        self.assertTrue((self.directory / f'recovery-{index:03d}-response.json').exists())

    def test_polling_abort_idle_precede_http_completion_and_no_double_usage(self):
        for mode in ('stream', 'cancelled'):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_wire(mode=mode), {'ok': True})
                events = self.wire.events
                self.assertLess(events.index('abort'), events.index('finished0'))
                self.assertLess(events.index('idle'), events.index('finished0'))
                self.assertLess(events.index('finished0'), events.index('post1'))
                self.assertFalse(self.wire.parallel)
                self.assertEqual(self.meta['metrics']['usage']['total_tokens'], 16)
                self.assertEqual(self.meta['metrics']['usage']['coverage']['total_tokens'],
                                 'partial' if mode == 'cancelled' else 'complete')

    def test_unconfirmed_stop_and_other_errors_never_send_recovery(self):
        for mode, kind, code in (
            ('stuck', 'TRANSPORT_ERROR', 'COMPACTION_RECOVERY_STOP_UNCONFIRMED'),
            ('disconnect', 'TRANSPORT_ERROR', 'COMPACTION_RECOVERY_STOP_UNCONFIRMED'),
            ('abort-hang', 'STAGE_TIMEOUT', None),
            ('backend-error', 'BACKEND_ERROR', None)):
            with self.subTest(mode=mode), self.assertRaises(ContractError) as caught:
                self.run_wire(mode=mode, execution={'http_timeout_seconds': .4})
            self.assertEqual(caught.exception.failure_kind, kind)
            if code:
                self.assertEqual(caught.exception.details['code'], code)
            self.assertEqual(len(self.wire.posts), 1)
            self.assertFalse(self.wire.parallel)

    def test_stage_idle_and_interrupt_keep_priority_during_stop(self):
        for execution, kind in (({'stage_timeout_seconds': .2}, 'STAGE_TIMEOUT'),
                                ({'idle_timeout_seconds': .15}, 'IDLE_TIMEOUT')):
            with self.subTest(kind=kind), self.assertRaises(ContractError) as caught:
                self.run_wire(mode='stuck', execution=execution)
            self.assertEqual(caught.exception.failure_kind, kind)
            self.assertEqual(len(self.wire.posts), 1)
        with patch.object(Server, 'stop_for_recovery', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_wire(mode='stream')
        self.assertEqual(len(self.wire.posts), 1)

    def test_uncertain_recovery_send_is_not_retried(self):
        with self.assertRaises(ContractError):
            self.run_wire(mode='recovery-disconnect')
        self.assertEqual(len(self.wire.posts), 2)
        self.assertFalse(self.wire.parallel)

    def test_budget_is_shared_across_separate_repair_attempts(self):
        budget = RecoveryBudget()
        self.run_wire(counter=budget)
        self.run_wire(counter=budget)
        with self.assertRaises(ContractError) as caught:
            self.run_wire(counter=budget)
        self.assertEqual(caught.exception.details['code'], 'COMPACTION_RECOVERY_LIMIT_EXCEEDED')
        self.assertEqual(budget.sent, 2)
        self.assertEqual(len(self.wire.posts), 1)

    def test_format_preserving_compaction_after_recovery_needs_no_extra_post(self):
        self.assertEqual(self.run_wire(mode='retained'), {'ok': True})
        self.assertEqual(len(self.wire.posts), 2)
        self.assertEqual(self.meta['compaction']['transitions'], 2)
        self.assertNotEqual(self.meta['final_parent_id'], self.meta['recovery_ids'][-1])
        self.assertEqual(self.meta['metrics']['usage']['total_tokens'], 24)

    def test_locally_closed_socket_is_not_completion_proof(self):
        self.run_wire()
        request = SimpleNamespace(done=threading.Event(), cancelled=threading.Event(), response_complete=True)
        request.done.set()
        request.cancelled.set()
        request.error = None
        with self.assertRaises(ContractError) as caught:
            self.server.stop_for_recovery(request, self.server.history)
        self.assertEqual(caught.exception.details['code'], 'COMPACTION_RECOVERY_STOP_UNCONFIRMED')


class RecoveryMembershipTests(unittest.TestCase):
    def test_registration_preserves_settings_and_rejects_replay(self):
        request = body() | {'system': 'system', 'tools': {'read': True}, 'variant': 'low'}
        messages = chain(request=request)
        for message in messages:
            if message['info']['role'] == 'user':
                message['info'].update({key: request[key] for key in ('system', 'tools', 'variant')})
        del messages[-2]['info']['format']
        history = SessionHistory('ses_test', request['messageID'], request)
        self.assertIsInstance(history.observe(messages), RecoveryRequired)
        history.confirm_stopped(messages, messages[-1])
        recovery = history.register_recovery('msg_recovery')
        for key in ('agent', 'model', 'system', 'tools', 'variant', 'format'):
            self.assertEqual(recovery[key], request[key])
        user = root(recovery)
        user['info'].update({key: request[key] for key in ('system', 'tools', 'variant')})
        history.observe(messages + [user])
        with self.assertRaises(ContractError):
            history.observe(messages + [user, user])

    def test_only_owned_abort_exception_is_ignored(self):
        for owned in (False, True):
            messages = chain()
            del messages[-2]['info']['format']
            history = SessionHistory('ses_test', 'msg_root', body())
            history.observe(messages[:-1])
            if owned:
                history.expect_cancellation()
            messages[-1]['info']['error'] = {'name': 'MessageAbortedError', 'data': {'message': 'Cancelled'}}
            if owned:
                self.assertIsInstance(history.observe(messages), RecoveryRequired)
            else:
                with self.assertRaises(ContractError):
                    history.observe(messages)

    def test_full_latest_history_and_registration_are_required(self):
        for case in ('unregistered', 'incomplete', 'wrong-parent', 'model', 'manual', 'summary', 'changed'):
            with self.subTest(case=case):
                messages = chain()
                del messages[-2]['info']['format']
                history = SessionHistory('ses_test', 'msg_root', body())
                if case == 'wrong-parent': messages[-1]['info']['parentID'] = 'msg_foreign'
                if case == 'model': messages[-1]['info']['modelID'] = 'other'
                if case == 'manual': messages[2]['parts'][0]['auto'] = False
                if case == 'summary': del messages[3]['info']['time']['completed']
                if case == 'changed': messages[-2]['info']['format'] = {'type': 'text'}
                with self.assertRaises(ContractError):
                    history.observe(messages)
                    if case == 'unregistered':
                        history.observe(messages + [root(body() | {'messageID': 'msg_unregistered'})])
                    if case == 'incomplete': history.confirm_stopped(messages[-1:], messages[-1])


if __name__ == '__main__':
    unittest.main()
