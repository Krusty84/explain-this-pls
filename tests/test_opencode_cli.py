# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""OpenCode V2 NDJSON transport and local result acceptance."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from explain import AuditError, Folder, Runner
from fixtures.cli_response import cli_result
from fixtures.ledger_response import prompt_context, response
from src.backends import opencode_cli as adapter
from src.contracts.contracts import ContractError
from test_folder import FolderFixture


def transcript(data):
    return [json.loads(line) for line in cli_result(['run', '--format', 'json'], data)['stdout'].splitlines()]


def output(events):
    return '\n'.join(json.dumps(e) for e in events)


def session_export(data, agent='private-agent'):
    return {'info': {'id': 'ses_fixture', 'outcome': 'succeeded', 'agent': agent},
            'messages': [{'id': 'msg_fixture', 'type': 'assistant', 'agent': agent,
                          'finish': 'stop', 'time': {'created': 1, 'completed': 2},
                          'content': [{'type': 'text', 'text': json.dumps(data)}],
                          'cost': 0.01, 'tokens': {'input': 60, 'output': 20, 'reasoning': 5,
                                                 'cache': {'read': 30, 'write': 10}}},
                         {'id': 'msg_idle', 'type': 'idle', 'outcome': 'succeeded'}]}


class OpenCodeCLITests(unittest.TestCase):
    def test_missing_finish_requires_matching_successful_export(self):
        raw = output(transcript({'ok': True})[:-1])
        with self.assertRaises(ContractError) as caught:
            adapter.parse_output(raw)
        self.assertEqual(caught.exception.details['session_id'], 'ses_fixture')
        exported = session_export({'ok': True})
        data, meta = adapter.parse_output(raw, exported=json.dumps(exported), agent_name='private-agent')
        self.assertEqual(data, {'ok': True})
        self.assertEqual(meta['completion_source'], 'session_export')
        metrics = adapter.collect_metrics(raw, completed=meta['exported_finish'])['usage']
        self.assertEqual(metrics['total_tokens'], 120)
        self.assertEqual(metrics['coverage']['total_tokens'], 'complete')
        complete = output(transcript({'ok': True}))
        measured = adapter.collect_metrics(complete, completed=meta['exported_finish'])['usage']
        self.assertEqual(measured['total_tokens'], 120)
        truncated = transcript({'ok': True})
        truncated[-1]['part']['reason'] = 'length'
        with self.assertRaises(ContractError):
            adapter.parse_output(output(truncated), exported=json.dumps(exported), agent_name='private-agent')

        for failure in ('session', 'message', 'agent', 'text', 'length', 'error', 'unfinished', 'outcome', 'later_user'):
            bad = copy.deepcopy(exported)
            final = bad['messages'][0]
            if failure == 'session': bad['info']['id'] = 'ses_other'
            if failure == 'message': final['id'] = 'msg_other'
            if failure == 'agent': final['agent'] = 'other-agent'
            if failure == 'text': final['content'][0]['text'] = '{"wrong": true}'
            if failure == 'length': final['finish'] = 'length'
            if failure == 'error': final['error'] = {'message': 'PRIVATE'}
            if failure == 'unfinished': final['time'].pop('completed')
            if failure == 'outcome': bad['info']['outcome'] = 'failed'
            if failure == 'later_user': bad['messages'].append({'id': 'msg_next', 'type': 'user'})
            with self.subTest(failure=failure), self.assertRaises(ContractError):
                adapter.parse_output(raw, exported=json.dumps(bad), agent_name='private-agent')

    def test_version_and_command(self):
        for version in ('opencode v2.0.23', '2.0.23', 'opencode v2.1.0'):
            adapter.verify_version(version)
        for version in ('1.2.27', 'opencode v3.0.0', '2.0.23-custom', None):
            with self.subTest(version=version), self.assertRaises(ContractError):
                adapter.verify_version(version)
        self.assertEqual(adapter.help_command('oc'), ['oc', 'run', '--help'])
        for model in (None, 'provider/model#variant'):
            command = adapter.build_command({'executable': 'oc', 'model': model}, 'study',
                                            'folder', {}, Path('/tmp/schema'), agent_name='private-agent')
            self.assertEqual(command, ['oc', 'run', '--standalone', '--format', 'json', '--agent',
                                       'private-agent'] + (['--model', model] if model else []))

    def test_fenced_final_with_split_text_and_verified_export(self):
        text = ' \t\r\n```JSON\r\n {"ok": true} \r\n```\r\n'
        for missing_finish in (False, True):
            events = transcript({})
            events[1]['part']['text'] = text[:10]
            tail = copy.deepcopy(events[1])
            tail['part'].update(id='prt_tail', text=text[10:])
            events.insert(2, tail)
            if missing_finish: events.pop()
            exported = session_export({'ok': True})
            exported['messages'][0]['content'][0]['text'] = text
            data, meta = adapter.parse_output(output(events), exported=json.dumps(exported), agent_name='private-agent')
            self.assertEqual(data, {'ok': True})
            provenance = meta['response_normalization']
            self.assertEqual(provenance['kind'], 'markdown_json_fence')
            self.assertEqual(text[provenance['payload_start']:provenance['payload_end']], ' {"ok": true} ')
            if missing_finish:
                exported['messages'][0]['content'][0]['text'] = '{"ok": true}'
                with self.assertRaises(ContractError) as caught:
                    adapter.parse_output(output(events), exported=json.dumps(exported), agent_name='private-agent')
                self.assertEqual(caught.exception.failure_kind, 'TRANSPORT_ERROR')

    def test_permissions_preserve_v1_and_v2_profile_entries(self):
        original = {'providers': {'private': {'settings': {'baseURL': 'https://example.invalid'}}},
                    'model': 'private/model', 'agent': {'legacy': {'mode': 'primary'}},
                    'agents': {'custom': {'permissions': []}}, 'plugins': ['auth-plugin']}
        for stage in ('catalog', 'study', 'review', 'compare', 'repair'):
            env = {'OPENCODE_CONFIG_CONTENT': json.dumps(original), 'OPENCODE_CONFIG': '/profile'}
            name = adapter.prepare_environment(env, stage)
            merged = json.loads(env['OPENCODE_CONFIG_CONTENT'])
            agent = merged['agents'].pop(name)
            self.assertEqual(merged, original)
            self.assertEqual(env['OPENCODE_CONFIG'], '/profile')
            permissions = {p['action']: p['effect'] for p in agent['permissions']}
            expected = {'*': 'deny'}
            if stage not in ('compare', 'repair'): expected.update(read='allow', glob='allow', grep='allow')
            self.assertEqual(permissions, expected)

    def test_final_answer_only_with_split_and_backfilled_text(self):
        earlier = transcript({'ignore': True})
        for event in earlier:
            event['part']['messageID'] = 'msg_earlier'
        final = transcript({'ok': True})
        final[1]['part']['text'] = '{"ok":'
        tail = copy.deepcopy(final[1])
        tail['part'].update(id='prt_tail', text=' true}')
        late_earlier = copy.deepcopy(earlier[1])
        late_earlier['part'].update(id='prt_oldtail', text='ordinary earlier prose')
        events = earlier + final + [tail, late_earlier, tail]
        data, meta = adapter.parse_output(output(events))
        self.assertEqual(data, {'ok': True})
        self.assertEqual(meta, {'session_id': 'ses_fixture', 'message_id': 'msg_fixture', 'finish_reason': 'stop'})

    def test_errors_truncation_and_invalid_json_never_succeed(self):
        valid = transcript({'ok': True})
        cases = [(valid + [{'type': 'error', 'error': {'message': 'PRIVATE'}}], 'BACKEND_ERROR'),
                 (valid[:-1], 'INCOMPLETE_OUTPUT'), ([], 'INCOMPLETE_OUTPUT')]
        for reason in ('length', 'tool-calls', 'error', None, 'unknown'):
            events = copy.deepcopy(valid)
            events[-1]['part']['reason'] = reason
            cases.append((events, 'INCOMPLETE_OUTPUT'))
        for text in ('```json\n{broken\n```', '{} trailing', '{"x":1,"x":2}', '[]',
                     '```json\n{"x":1,"x":2}\n```', '```json\n{"x":NaN}\n```',
                     '```json\n{"x":Infinity}\n```', '```json\n[]\n```',
                     '```json\n{}\n```\n```json\n{}\n```'):
            events = copy.deepcopy(valid)
            events[1]['part']['text'] = text
            cases.append((events, 'INVALID_JSON'))
        started = copy.deepcopy(valid[0]); started['part']['messageID'] = 'msg_next'
        cases.append((valid + [started], 'INCOMPLETE_OUTPUT'))
        unfinished = copy.deepcopy(valid[1]); unfinished['part']['messageID'] = 'msg_next'
        cases.append((valid + [unfinished], 'INCOMPLETE_OUTPUT'))
        for kind, part_type in (('reasoning', 'reasoning'), ('tool_use', 'tool')):
            unfinished = copy.deepcopy(valid[1])
            unfinished['type'] = kind
            unfinished['part'].update(messageID='msg_next', type=part_type)
            cases.append((valid + [unfinished], 'INCOMPLETE_OUTPUT'))
        for events, kind in cases:
            with self.subTest(events=events), self.assertRaises(ContractError) as caught:
                adapter.parse_output(output(events))
            self.assertEqual(caught.exception.failure_kind, kind)
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_transport_identity_and_conflicting_duplicates(self):
        valid = transcript({})
        bad_session = copy.deepcopy(valid)
        bad_session[1]['sessionID'] = 'ses_other'
        bad_message = copy.deepcopy(valid)
        bad_message[1]['part']['messageID'] = None
        duplicate = copy.deepcopy(valid[1]); duplicate['part']['text'] = 'changed'
        for raw in ('{', '[]', output(bad_session), output(bad_message), output(valid + [duplicate])):
            with self.subTest(raw=raw), self.assertRaises(ContractError) as caught:
                adapter.parse_output(raw)
            self.assertEqual(caught.exception.failure_kind, 'TRANSPORT_ERROR')

    def test_metrics_deduplicate_steps_and_preserve_unknown_model(self):
        events = transcript({})
        metrics = adapter.collect_metrics(output(events + [events[-1]]), 'requested/model')
        self.assertEqual(metrics['usage']['input_tokens'], 100)
        self.assertEqual(metrics['usage']['total_tokens'], 120)
        self.assertEqual(metrics['usage']['cost_usd'], 0.01)
        self.assertEqual(metrics['usage']['coverage']['total_tokens'], 'complete')
        self.assertIsNone(metrics['by_model'][0]['model_actual'])
        for suffix in ('\n{', '\n' + json.dumps({'type': 'error'})):
            measured = adapter.collect_metrics(output(events) + suffix)['usage']
            self.assertEqual(measured['total_tokens'], 120)
            self.assertEqual(measured['coverage']['total_tokens'], 'partial')
        self.assertIsNone(adapter.collect_metrics('')['usage']['total_tokens'])


class OpenCodeAcceptanceTests(FolderFixture):
    def test_failed_session_export_remains_incomplete_without_another_model_call(self):
        config = self.config()
        agent = config['_agents']['study']
        agent['backend'] = 'opencode'
        runner = Runner(config, self.base / 'failed-export')
        runner.versions['opencode:' + agent['executable']] = 'opencode v2.0.23'
        context = {'source_directory': str(self.source),
                   'source_fingerprint': Folder(self.source).snapshot()['source_fingerprint']}
        replies = [{'returncode': 0, 'stdout': output(transcript({})[:-1]).encode(), 'stderr': b''},
                   {'returncode': 1, 'stdout': b'', 'stderr': b'Export failed'}]
        with patch('explain.process', side_effect=replies) as mocked, self.assertRaises(ContractError) as caught:
            runner.invoke('study', context, runner.run_dir / 'study.logs')
        self.assertEqual(caught.exception.failure_kind, 'INCOMPLETE_OUTPUT')
        self.assertEqual(mocked.call_count, 2)
        self.assertFalse((runner.run_dir / 'study.json').exists())

    def test_missing_finish_exports_once_without_repeating_the_model_call(self):
        config = self.config() | {'result_policy': 'strict'}
        agent = config['_agents']['study']
        agent['backend'] = 'opencode'
        runner = Runner(config, self.base / 'export')
        runner.versions['opencode:' + agent['executable']] = 'opencode v2.0.23'
        context = {'source_directory': str(self.source),
                   'source_fingerprint': Folder(self.source).snapshot()['source_fingerprint']}
        exported = None
        def process(command, cwd, env, payload=b'', **kwargs):
            nonlocal exported
            if command[1:3] == ['session', 'export']:
                self.assertEqual(command[3:], ['--standalone', 'ses_fixture'])
                self.assertEqual(payload, b'')
                return {'returncode': 0, 'stdout': json.dumps(exported).encode(), 'stderr': b''}
            wire = response(prompt_context(payload))
            exported = session_export(wire, command[command.index('--agent') + 1])
            return {'returncode': 0, 'stdout': output(transcript(wire)[:-1]).encode(), 'stderr': b''}
        with patch('explain.process', side_effect=process) as mocked:
            data, meta = runner.invoke('study', context, runner.run_dir / 'study.logs')
        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(meta['publication_complete'])
        self.assertEqual(meta['provider_metadata']['completion_source'], 'session_export')
        self.assertEqual(mocked.call_args.kwargs['log_dir'], Path(meta['artifact_directory']) / 'session-export')

    def test_preflight_uses_run_help_and_checks_version_without_prompt(self):
        config = self.config()
        for agent in config['_agents'].values(): agent['backend'] = 'opencode'
        runner = Runner(config, self.base / 'check')
        for version, flags, error in (
                ('opencode v2.0.23', adapter.required_flags('folder'), False),
                ('1.2.27', adapter.required_flags('folder'), True),
                ('opencode v2.0.23', ['--format', '--model', '--agent'], True)):
            responses = [{'returncode': 0, 'stdout': version.encode(), 'stderr': b''},
                         {'returncode': 0, 'stdout': ' '.join(flags).encode(), 'stderr': b''}]
            with self.subTest(version=version, flags=flags), patch('explain.process', side_effect=responses) as call:
                if error:
                    with self.assertRaises((AuditError, ContractError)): runner.check_cli()
                else:
                    runner.check_cli()
                self.assertEqual(call.call_count, 2)
                self.assertEqual(call.call_args_list[1].args[0][1:], ['run', '--help'])

    def test_local_validation_and_failure_do_not_trigger_native_repairs(self):
        for fenced, failure in ((fenced, failure) for fenced in (False, True)
                                for failure in (None, 'schema', 'identity', 'exit')):
            with self.subTest(fenced=fenced, failure=failure):
                config = self.config() | {'result_policy': 'strict', 'execution': {
                    'opencode_format_retries': 2, 'structured_output_repair_attempts': 2}}
                agent = config['_agents']['study']
                agent['backend'] = 'opencode'
                runner = Runner(config, self.base / f'{fenced}-{failure}')
                runner.versions['opencode:' + agent['executable']] = 'opencode v2.0.23'
                context = {'source_directory': str(self.source),
                           'source_fingerprint': Folder(self.source).snapshot()['source_fingerprint']}
                def process(command, cwd, env, payload, **kwargs):
                    self.assertIn('--standalone', command)
                    self.assertIn('exactly one JSON object', payload.decode())
                    self.assertNotIn('Use the StructuredOutput tool', payload.decode())
                    wire = response(prompt_context(payload))
                    if failure == 'schema': wire['unexpected'] = True
                    if failure == 'identity': wire['source_snapshot_id'] = 'wrong'
                    raw = (' \r\n```JSON\r\n' + json.dumps(wire) + '\r\n```\r\n').encode() if fenced else wire
                    result = cli_result(command, raw)
                    (kwargs['log_dir'] / 'stdout.log').write_bytes(result['stdout'])
                    if failure == 'exit': result['returncode'] = 1
                    return result
                with patch('explain.process', side_effect=process) as mocked:
                    if failure is None:
                        data, meta = runner.invoke('study', context, runner.run_dir / 'study.logs')
                        self.assertTrue(meta['publication_complete'])
                        self.assertIsNone(meta['model_actual'])
                        self.assertNotIn('native_envelope_valid', meta)
                    else:
                        with self.assertRaises((ContractError, AuditError)) as caught:
                            runner.invoke('study', context, runner.run_dir / 'study.logs')
                        expected = {'schema': 'SCHEMA_ERROR', 'identity': 'IDENTITY_MISMATCH', 'exit': 'BACKEND_ERROR'}
                        self.assertEqual(caught.exception.failure_kind, expected[failure])
                        self.assertFalse((runner.run_dir / 'study.json').exists())
                    self.assertEqual(mocked.call_count, 1)
                attempt = runner.run_dir / 'study.logs/attempt-001'
                meta = json.loads((attempt / 'invocation.json').read_text())
                self.assertEqual('response_normalization' in meta.get('provider_metadata', {}), fenced and failure != 'exit')
                if fenced and failure != 'exit':
                    provenance = meta['provider_metadata']['response_normalization']
                    self.assertEqual(provenance['kind'], 'markdown_json_fence')
                    events = [json.loads(line) for line in (attempt / 'stdout.log').read_bytes().splitlines()]
                    original = events[1]['part']['text']
                    self.assertTrue(original.startswith(' \r\n```JSON\r\n'))
                    extracted = json.loads((attempt / 'extracted.json').read_bytes())
                    self.assertEqual(json.loads(original[provenance['payload_start']:provenance['payload_end']]), extracted)


if __name__ == '__main__':
    unittest.main()
