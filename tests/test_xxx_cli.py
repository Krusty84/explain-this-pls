# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Protocol regressions for the pinned XXX CLI/export boundary."""
import copy
import json
from pathlib import Path
import unittest

from src.backends import xxx
from src.contracts.contracts import ContractError
from fixtures.xxx_protocol import transcript


class XXXCLITests(unittest.TestCase):
    def parse(self, output, exported, **kwargs):
        return xxx.parse_output(output, exported=json.dumps(exported), agent_name='audit',
                                prompt='Test prompt', cwd=Path('/fixture'), **kwargs)

    def test_command_and_v1_overlay_preserve_user_configuration(self):
        config = {'agent': {'user': {'model': 'my/model'}}, 'agents': {'v2': {}},
                  'compaction': {'auto': True}, 'plugin': ['local-plugin'], 'share': 'auto'}
        env = {'OPENCODE_CONFIG_CONTENT': json.dumps(config), 'HOME': '/home/user',
               'OPENCODE_AUTO_SHARE': '1', 'PROVIDER_API_KEY': 'private'}
        name = xxx.prepare_environment(env, 'study', source_snapshot=True)
        actual = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        for key in ('agents', 'compaction', 'plugin'):
            self.assertEqual(actual[key], config[key])
        self.assertEqual(actual['agent']['user'], config['agent']['user'])
        self.assertEqual(actual['agent'][name]['permission'], {'*': 'deny', 'read': 'allow',
            'glob': 'allow', 'grep': 'allow', 'list': 'allow', 'external_directory': 'deny'})
        self.assertEqual(env['PROVIDER_API_KEY'], 'private')
        self.assertEqual(env['HOME'], '/home/user')
        self.assertEqual(env['OPENCODE_AUTO_SHARE'], '0')
        self.assertEqual(actual['share'], 'disabled')
        command = xxx.build_command({'executable': 'xxx', 'model': 'one/model'}, 'study', 'folder', {}, Path('schema'),
                                    agent_name=name)
        self.assertEqual(command, ['xxx', 'run', '--format', 'json', '--agent', name, '--title', name, '--model', 'one/model'])
        name = xxx.prepare_environment(env, 'compare')
        self.assertEqual(json.loads(env['OPENCODE_CONFIG_CONTENT'])['agent'][name]['permission'], {'*': 'deny'})

    def test_invalid_config_is_rejected(self):
        for value in ('null', '[]', '{"agent":[]}', '{"a":1,"a":2}'):
            with self.subTest(value=value), self.assertRaises(ContractError):
                xxx.prepare_environment({'OPENCODE_CONFIG_CONTENT': value}, 'study')

    def test_success_duplicates_and_missing_terminal_event(self):
        for scenario in ('', 'duplicate', 'missing-finish'):
            output, exported = transcript({'ok': True}, scenario=scenario)
            data, meta = self.parse(output, exported)
            self.assertEqual(data, {'ok': True})
            self.assertEqual(meta['model_actual'], 'fixture/configured-model')
            self.assertEqual(meta['metrics']['usage']['total_tokens'], 10)
            self.assertEqual(meta['metrics']['usage']['coverage']['total_tokens'], 'complete')
            self.assertNotIn('response_normalization', meta)

    def test_fenced_final_is_normalized_after_transport_verification(self):
        for scenario in ('', 'duplicate', 'missing-finish'):
            text = ' \r\n```JSON\r\n {"ok": true} \r\n```\r\n'
            output, exported = transcript(text, rounds=2, scenario=scenario)
            data, meta = self.parse(output, exported)
            self.assertEqual(data, {'ok': True})
            self.assertEqual(meta['message_id'], 'msg_final')
            self.assertEqual(meta['compaction']['completed'], 2)
            provenance = meta['response_normalization']
            self.assertEqual(provenance['kind'], 'markdown_json_fence')
            self.assertEqual(text[provenance['payload_start']:provenance['payload_end']], ' {"ok": true} ')
            exported['messages'][-1]['parts'][1]['text'] = '{"ok": true}'
            with self.assertRaises(ContractError) as caught:
                self.parse(output, exported)
            self.assertEqual(caught.exception.failure_kind, 'TRANSPORT_ERROR')

    def test_fenced_final_precedes_unchanged_summary_fallback(self):
        for final, expected in (('```json\n{"final": true}\n```', {'final': True}),
                                ('Final prose', {'summary': True}),
                                ('```json\n{broken\n```', {'summary': True})):
            output, exported = transcript(final, rounds=1)
            summary = exported['messages'][2]['parts'][1]
            summary['text'] = '{"summary": true}'
            events = [json.loads(line) for line in output.splitlines()]
            for event in events:
                if event['part']['id'] == summary['id']:
                    event['part'] = copy.deepcopy(summary)
            data, meta = self.parse('\n'.join(json.dumps(e) for e in events), exported)
            self.assertEqual(data, expected)
            self.assertEqual('response_normalization' in meta, final.startswith('```'))
            if 'response_normalization' in meta:
                self.assertEqual(meta['response_normalization']['message_id'], 'msg_final')
            self.assertEqual(meta['message_id'], 'msg_final' if 'final' in expected else 'msg_summary0')

    def test_compaction_uses_final_stage_answer_and_distinct_model_usage(self):
        for rounds in (1, 2, 3):
            output, exported = transcript({'ok': True}, rounds=rounds)
            data, meta = self.parse(output, exported)
            self.assertEqual(data, {'ok': True})
            self.assertEqual(meta['compaction']['completed'], rounds)
            self.assertEqual(meta['model_actual'], 'fixture/configured-model')
            self.assertEqual(meta['metrics']['usage']['total_tokens'], 10 * (rounds + 1))
            entries = meta['metrics']['by_model']
            self.assertEqual([e['origin'] for e in entries], ['compaction'] * rounds + ['stage'])
            self.assertEqual(entries[0]['model_actual'], 'fixture/small')

    def test_summary_missing_continuation_and_foreign_user_are_not_results(self):
        for mutation in ('last-summary', 'changed-continuation', 'foreign-agent', 'foreign-model', 'manual'):
            output, exported = transcript({'ok': True}, rounds=1)
            if mutation == 'last-summary': exported['messages'] = exported['messages'][:3]
            if mutation == 'changed-continuation': exported['messages'][3]['parts'][0]['text'] = 'Another task'
            if mutation == 'foreign-agent': exported['messages'][3]['info']['agent'] = 'other'
            if mutation == 'foreign-model': exported['messages'][3]['info']['model']['modelID'] = 'other'
            if mutation == 'manual': exported['messages'][1]['parts'][0]['auto'] = False
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                self.parse(output, exported)

    def test_native_overflow_continuation_and_replay_stay_in_owned_session(self):
        prefix = ("The previous request exceeded the provider's size limit due to large media attachments. "
                  'The conversation was compacted and media files were removed from context. '
                  'If the user was asking about attached images or files, explain that the attachments were too '
                  'large to process and suggest they try again with smaller or fewer files.\n\n')
        output, exported = transcript({'ok': True}, rounds=1)
        exported['messages'][1]['parts'][0]['overflow'] = True
        exported['messages'][3]['parts'][0]['text'] = prefix + exported['messages'][3]['parts'][0]['text']
        self.assertEqual(self.parse(output, exported)[0], {'ok': True})
        output, exported = transcript({'ok': True}, rounds=2)
        exported['messages'][4]['parts'][0]['overflow'] = True
        prior = exported['messages'][3]['parts'][0]
        replay = exported['messages'][6]['parts'][0]
        for key in ('text', 'synthetic'):
            replay[key] = prior[key]
        self.assertEqual(self.parse(output, exported)[1]['compaction']['completed'], 2)
        replay['text'] = 'Unrelated new task'
        with self.assertRaises(ContractError): self.parse(output, exported)

    def test_native_tool_pruning_does_not_hide_content_changes(self):
        output, exported = transcript({'ok': True})
        tool = {'id': 'prt_read', 'messageID': 'msg_final', 'sessionID': 'ses_test', 'type': 'tool',
                'tool': 'read', 'state': {'status': 'completed', 'input': {'path': 'app.py'},
                'output': 'Source', 'time': {'start': 1, 'end': 2}}}
        event = {'type': 'tool_use', 'sessionID': 'ses_test', 'part': copy.deepcopy(tool)}
        output = json.dumps(event) + '\n' + output
        tool['state']['time']['compacted'] = 3
        exported['messages'][-1]['parts'].insert(1, tool)
        self.assertEqual(self.parse(output, exported)[0], {'ok': True})
        output += json.dumps(event | {'part': copy.deepcopy(tool)}) + '\n'
        self.assertEqual(self.parse(output, exported)[0], {'ok': True})
        tool['state']['output'] = 'Substituted source'
        with self.assertRaises(ContractError): self.parse(output, exported)

    def test_invalid_utf8_is_a_transport_error(self):
        with self.assertRaises(ContractError) as caught:
            xxx.decode_output(b'\xff')
        self.assertEqual(caught.exception.failure_kind, 'TRANSPORT_ERROR')

    def test_transport_and_completion_failures(self):
        for scenario in ('no-final', 'foreign-request', 'summary-only', 'truncated', 'unknown-finish',
                         'backend-error', 'unfinished-tool', 'conflicting-duplicate', 'foreign-session',
                         'malformed-event', 'export-foreign', 'export-prompt', 'export-text'):
            for text in ('{"ok": true}', '```json\n{"ok": true}\n```'):
                with self.subTest(scenario=scenario, text=text), self.assertRaises(ContractError):
                    self.parse(*transcript(text, scenario=scenario))

    def test_export_alone_cannot_supply_a_different_final_answer(self):
        output, exported = transcript({'ok': True})
        lines = output.splitlines()
        with self.assertRaises(ContractError):
            self.parse('\n'.join([lines[0], lines[-1]]), exported)
        exported['messages'][-1]['info']['modelID'] = 'other'
        with self.assertRaises(ContractError):
            self.parse(output, exported)

    def test_explicit_model_must_match_export(self):
        output, exported = transcript({'ok': True})
        with self.assertRaises(ContractError):
            self.parse(output, exported, requested='other/model')

    def test_invalid_json_duplicates_and_nonfinite_numbers_are_rejected(self):
        for value in ('{broken', '[]', '{"ok":1,"ok":2}', '{"ok":NaN}', 'text\n{}',
                      '```json\n{broken\n```', '```json\n{"ok":1,"ok":2}\n```',
                      '```json\n{"ok":NaN}\n```', '```json\n[]\n```',
                      '```json\n{}\n```\n```json\n{}\n```'):
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.parse(*transcript(value))

    def test_export_identity_and_shape_errors_are_controlled(self):
        for mutation in ('duplicate-message', 'duplicate-part', 'foreign-part', 'missing-time', 'bool-time', 'invalid-role'):
            output, exported = transcript({'ok': True})
            final = exported['messages'][-1]
            if mutation == 'duplicate-message': exported['messages'].append(copy.deepcopy(final))
            if mutation == 'duplicate-part': final['parts'].append(copy.deepcopy(final['parts'][0]))
            if mutation == 'foreign-part': final['parts'][0]['messageID'] = 'msg_other'
            if mutation == 'missing-time': del final['info']['time']
            if mutation == 'bool-time': final['info']['time']['completed'] = True
            if mutation == 'invalid-role': final['info']['role'] = 'system'
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                self.parse(output, exported)

    def test_metrics_keep_zero_unknown_and_partial_distinct_without_double_counting(self):
        output, exported = transcript({'ok': True}, scenario='duplicate')
        partial = xxx.collect_metrics(output)
        self.assertEqual(partial['usage']['total_tokens'], 10)
        self.assertEqual(partial['usage']['coverage']['total_tokens'], 'partial')
        assistants = [exported['messages'][-1]]
        assistants[0]['info']['tokens']['input'] = 900
        self.assertEqual(xxx.export_metrics(assistants)['usage']['total_tokens'], 10)
        assistants[0]['parts'] = [p for p in assistants[0]['parts'] if p['type'] != 'step-finish']
        self.assertEqual(xxx.export_metrics(assistants)['usage']['total_tokens'], 905)
        assistants[0]['info']['tokens']['total'] = 950
        measured = xxx.export_metrics(assistants)['usage']
        self.assertEqual(measured['total_tokens'], 950)
        self.assertFalse(measured['total_tokens_estimated'])
        self.assertEqual(measured['cost_usd'], 0)
        del assistants[0]['info']['cost']
        self.assertIsNone(xxx.export_metrics(assistants)['usage']['cost_usd'])

    def test_ownership_requires_root_prompt_title_directory_and_agent(self):
        for field in ('title', 'directory', 'id'):
            output, exported = transcript({'ok': True})
            exported['info'][field] = 'foreign'
            with self.assertRaises(ContractError):
                xxx.owned_export(json.dumps(exported), 'ses_test', 'audit', 'Test prompt', Path('/fixture'))
        self.assertIsNone(xxx.session_candidate('{"sessionID":"ses_one"}\n{"sessionID":"ses_two"}'))
        self.assertEqual(xxx.session_candidate('{"sessionID":"ses_one"}\n{'), 'ses_one')


if __name__ == '__main__':
    unittest.main()

