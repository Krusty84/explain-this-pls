# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.tool_observer import CompletedTool, ToolObserver
from scripts.replay_tool_observation import main, replay


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


def call(number, result='same', *, tool='read', arguments=None, rows=None, complete=True, status='success', session='session'):
    return CompletedTool(str(number), tool, arguments or {'path': 'unit.py'}, status, result, complete,
                         session, rows or [])


def native(number, *, output=None, backend='opencode', truncated=False, path='/source/unit.py'):
    # V2.0.23 captured transport shape; V1.2.27 upstream read.ts envelope.
    if output is None:
        output = (f'Read file {path}, lines 1-2\n1: abc\n2: xyz' if backend == 'opencode' else
                  f'<path>{path}</path>\n<type>file</type>\n<content>1: abc\n2: xyz\n\n(End of file - total 2 lines)\n</content>')
    arguments = {'path' if backend == 'opencode' else 'filePath': path, 'offset': 1, 'limit': 200}
    metadata = {'metadata': {'truncated': truncated}} if backend == 'opencode' else {'truncated': truncated}
    return {'type': 'tool_use', 'timestamp': number * 3000, 'sessionID': 'ses_unit',
            'part': {'id': 'prt_' + str(number), 'messageID': 'msg_unit', 'sessionID': 'ses_unit',
                     'type': 'tool', 'tool': 'read', 'callID': 'call_' + str(number),
                     'state': {'status': 'completed', 'input': arguments,
                               'output': output, 'metadata': metadata,
                               'time': {'start': number * 3000, 'end': number * 3000 + 1}}}}


CONTEXT = {'source_mode': 'folder', 'source_directory': '/source', 'source_fingerprint': 'snapshot',
           '_inventory': {'entries': [{'type': 'file', 'path': 'unit.py'}]}}


class ToolObserverTests(unittest.TestCase):
    def observer(self, **kwargs):
        clock = Clock()
        return ToolObserver('opencode', clock=clock, **kwargs), clock

    def test_both_thresholds_and_first_repeat_completion_start(self):
        observer, clock = self.observer()
        observer.observe(call(0))
        clock.now = 10
        observer.observe(call(1))
        for n in range(2, 50):
            clock.now = 130
            observer.observe(call(n))
        self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 49)
        self.assertFalse(observer.triggered)
        observer.observe(call(50))
        self.assertTrue(observer.triggered)
        self.assertEqual(observer.diagnostic['repeat_streak_duration_seconds'], 120)
        self.assertEqual(observer.diagnostic['call'], 51)
        self.assertEqual(observer.snapshot()['max_repeat_streak'], 50)

    def test_fifty_repeats_below_time_threshold(self):
        observer, clock = self.observer()
        for n in range(51):
            clock.now = n
            observer.observe(call(n))
        self.assertFalse(observer.triggered)
        self.assertEqual(observer.snapshot()['max_repeat_streak'], 50)

    def test_new_unknown_error_and_truncated_reset(self):
        for reset in (call('new', 'changed'), call('unknown', complete=False), call('error', status='error')):
            with self.subTest(reset=reset):
                observer, clock = self.observer()
                for n in range(50):
                    clock.now = n * 3
                    observer.observe(call(n))
                observer.observe(reset)
                observer.observe(call(51))
                self.assertFalse(observer.triggered)
                self.assertLessEqual(observer.snapshot()['consecutive_confirmed_repeats'], 1)

    def test_prompt_delivery_does_not_seed_stop_evidence(self):
        observer, _ = self.observer()
        observer.seed_prompt([{'source_id': 'source-001', 'path': 'unit.py', 'start_line': 1,
                               'end_line': 2, 'text': 'abc\nxyz'}])
        observer.observe(call(0, rows=[('source-001', 'unit.py', 1, 'abc'), ('source-001', 'unit.py', 2, 'xyz')]))
        metrics = observer.snapshot()
        self.assertEqual(metrics['delivery']['repeated_lines'], 2)
        self.assertEqual(metrics['unique_lines'], 2)
        self.assertEqual(metrics['consecutive_confirmed_repeats'], 0)
        for n in range(1, 5):
            observer.observe(call(n, rows=[('source-001', 'unit.py', 1, 'abc')]))
        self.assertEqual(observer.snapshot()['fully_repeated_reads'], 4)
        self.assertFalse(observer.triggered)

    def test_sequential_overlapping_and_conflicting_source_lines(self):
        observer, _ = self.observer()
        observer.observe(call(0, rows=[('s1', 'p', 1, 'one')]))
        observer.observe(call(1, rows=[('s1', 'p', 2, 'two')]))
        observer.observe(call(2, rows=[('s1', 'p', 2, 'two'), ('s1', 'p', 3, 'three')]))
        metrics = observer.snapshot()
        self.assertEqual((metrics['unique_lines'], metrics['repeated_lines'], metrics['mixed_reads']), (3, 1, 1))
        observer.observe(call(3, rows=[('s1', 'p', 3, 'changed')]))
        self.assertEqual(observer.snapshot()['conflicting_lines'], 1)
        self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 0)
        observer.observe(call(4, rows=[('s2', 'p', 1, 'one')]))
        self.assertEqual(observer.snapshot()['unique_lines'], 5)

    def test_exact_repeats_preserve_arguments_content_and_whitespace(self):
        observer, _ = self.observer()
        for n, (args, result) in enumerate([({'query': 'x'}, ''), ({'query': 'y'}, ''),
                                           ({'query': 'x'}, ' '), ({'query': 'x'}, '1'),
                                           ({'query': 'x'}, '2')]):
            observer.observe(call(n, result, arguments=args, tool='grep'))
        self.assertEqual(observer.snapshot()['exact_repeats'], 0)
        observer.observe(call(5, '2', arguments={'query': 'x'}, tool='grep'))
        self.assertEqual(observer.snapshot()['exact_repeats'], 1)

    def test_duplicate_ids_are_noop_but_session_ids_distinct(self):
        observer, clock = self.observer()
        observer.observe(call(0))
        observer.observe(call(1))
        clock.now = 150
        observer.observe(call(1, 'updated', complete=False))
        metrics = observer.snapshot()
        self.assertEqual(metrics['calls'], 2)
        self.assertEqual(metrics['consecutive_confirmed_repeats'], 1)
        self.assertEqual(metrics['duplicate_events'], 1)
        observer.observe(call(1, session='other-session'))
        self.assertEqual(observer.snapshot()['calls'], 3)

    def test_reasoning_and_stderr_do_not_poll_guard(self):
        observer, clock = self.observer()
        for n in range(51): observer.observe(call(n))
        clock.now = 999
        observer.feed(json.dumps({'type': 'reasoning', 'part': {'text': 'thinking'}}) + '\n')
        self.assertFalse(observer.triggered)
        self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 50)

    def test_disabled_guard_retains_metrics_and_counterfactual(self):
        observer, clock = self.observer(enabled=False)
        for n in range(60):
            clock.now = 3 * n
            observer.observe(call(n))
        metrics = observer.snapshot()
        self.assertFalse(observer.triggered)
        self.assertEqual(metrics['calls'], 60)
        self.assertIsNotNone(metrics['guard']['would_stop'])

    def test_chunked_json_and_unicode(self):
        observer, _ = self.observer(context=CONTEXT)
        event = native(0)
        event['part']['state']['output'] = event['part']['state']['output'].replace('abc', 'λ')
        raw = (json.dumps(event, ensure_ascii=False) + '\n').encode()
        for byte in raw:
            observer.feed(bytes([byte]))
        observer.finish()
        self.assertEqual(observer.snapshot()['calls'], 1)
        self.assertEqual(observer.snapshot()['unique_lines'], 2)

    def test_stop_inside_single_large_chunk(self):
        clock = Clock()
        def advance(record): clock.now += 3
        observer = ToolObserver('opencode', clock=clock, context=CONTEXT, event_sink=advance)
        observer.feed(''.join(json.dumps(native(n)) + '\n' for n in range(80)))
        self.assertTrue(observer.triggered)
        self.assertEqual(observer.snapshot()['calls'], 51)

    def test_native_versions_source_coordinates_and_status(self):
        for backend in ('opencode', 'xxx'):
            with self.subTest(backend=backend):
                observer = ToolObserver(backend, context=CONTEXT)
                observer.event(native(1, backend=backend))
                observer.event(native(2, backend=backend))
                observer.event(native(3, truncated=True, backend=backend))
                metrics = observer.snapshot()
                self.assertEqual(metrics['unique_lines'], 2)
                self.assertEqual(metrics['repeated_lines'], 4)
                self.assertEqual(metrics['fully_repeated_reads'], 1)
                self.assertEqual(metrics['consecutive_confirmed_repeats'], 0)
                self.assertEqual(metrics['incomplete_results'], 1)

    def test_external_symbol_map_reads_keep_repeat_guard_without_source_coverage(self):
        path = '/reports/study/attempt-001/source-maps/M-000001.json'
        for backend in ('codex', 'opencode', 'xxx'):
            with self.subTest(backend=backend):
                clock = Clock()
                observer = ToolObserver(backend, context=CONTEXT, clock=clock)
                if backend == 'codex':
                    observer.event({'type': 'item.completed', 'item': {
                        'id': 'source', 'type': 'command_execution', 'command': 'cat /source/unit.py',
                        'aggregated_output': 'abc\nxyz', 'exit_code': 0, 'status': 'completed'}})
                else:
                    observer.event(native(0, backend=backend))
                before = observer.snapshot()
                source_lines = dict(observer.tool_lines)
                delivered = dict(observer.delivered)
                for number in range(1, 52):
                    clock.now = number * 3
                    if backend == 'codex':
                        observer.event({'type': 'item.completed', 'item': {
                            'id': 'map-' + str(number), 'type': 'command_execution',
                            'command': 'cat ' + path, 'aggregated_output': '{"symbols": []}',
                            'exit_code': 0, 'status': 'completed'}})
                    else:
                        event = native(number, backend=backend, path=path)
                        event['part']['state']['output'] = event['part']['state']['output'].replace(
                            'abc', '{"symbols": [').replace('xyz', ']}')
                        observer.event(event)
                metrics = observer.snapshot()
                self.assertEqual(metrics['calls'], 52)
                self.assertEqual(metrics['successful_results'], 52)
                self.assertEqual(metrics['incomplete_results'], 0)
                self.assertEqual(metrics['exact_repeats'], 50)
                self.assertTrue(observer.triggered)
                self.assertEqual(metrics['unique_lines'], before['unique_lines'])
                self.assertEqual(metrics['delivery']['unique_lines'], before['delivery']['unique_lines'])
                self.assertEqual(observer.tool_lines, source_lines)
                self.assertEqual(observer.delivered, delivered)

    def test_changed_native_semantic_metadata_is_not_exact_repeat(self):
        observer, _ = self.observer()
        event = native(0)
        event['part']['tool'] = 'grep'
        observer.event(event)
        changed = native(1)
        changed['part']['tool'] = 'grep'
        changed['part']['state']['metadata']['matched_files'] = 2
        observer.event(changed)
        self.assertEqual(observer.snapshot()['exact_repeats'], 0)
        self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 0)

    def test_conflicting_source_stays_ambiguous(self):
        observer, _ = self.observer()
        for n, content in enumerate(('a', 'b', 'a', 'a')):
            observer.observe(call(n, rows=[('s', 'p', 1, content)]))
            self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 0)
        self.assertEqual(observer.snapshot()['conflicting_lines'], 3)

    def test_native_long_line_truncation_never_confirms_repeat(self):
        for backend in ('opencode', 'xxx'):
            observer = ToolObserver(backend, context=CONTEXT)
            for n in range(2):
                event = native(n, backend=backend)
                event['part']['state']['output'] = event['part']['state']['output'].replace(
                    'abc', 'abc... (line truncated to 2000 chars)')
                observer.event(event)
            self.assertEqual(observer.snapshot()['incomplete_results'], 2)
            self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 0)

    def test_truncated_read_without_source_ownership_cannot_stop(self):
        for backend in ('opencode', 'xxx'):
            for context in (None, CONTEXT):
                with self.subTest(backend=backend, context=context):
                    clock = Clock()
                    observer = ToolObserver(backend, context=context, clock=clock)
                    for n in range(51):
                        clock.now = 3 * n
                        event = native(n, backend=backend, path='/outside/unit.py')
                        event['part']['state']['output'] = event['part']['state']['output'].replace(
                            'abc', 'abc... (line truncated to 2000 chars)')
                        observer.event(event)
                    self.assertFalse(observer.triggered)
                    self.assertEqual(observer.snapshot()['incomplete_results'], 51)
                    self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 0)

    def test_native_output_path_mismatch_resets(self):
        observer, _ = self.observer(context=CONTEXT)
        event = native(0)
        event['part']['state']['output'] = event['part']['state']['output'].replace('/source/unit.py', '/other/unit.py')
        observer.event(event)
        self.assertEqual(observer.snapshot()['incomplete_results'], 1)

    def test_native_error_and_running_events(self):
        observer, _ = self.observer(context=CONTEXT)
        event = native(0)
        event['part']['state']['status'] = 'running'
        observer.event(event)
        event['part']['state'] = {'status': 'error', 'input': {}, 'error': 'failed'}
        observer.event(event)
        self.assertEqual(observer.snapshot()['calls'], 1)
        self.assertEqual(observer.snapshot()['error_results'], 1)

    def test_native_malformed_state_resets_but_running_does_not(self):
        for state in (None, 'bad', {}, {'status': 'new-status'}):
            with self.subTest(state=state):
                observer, _ = self.observer()
                observer.observe(call(0))
                observer.observe(call(1))
                event = native(2)
                event['part']['state'] = {'status': 'running'}
                observer.event(event)
                self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 1)
                event['part']['state'] = state
                observer.event(event)
                self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 0)
                self.assertEqual(observer.snapshot()['invalid_events'], 1)

    def test_native_changed_completed_event_does_not_reset(self):
        observer, _ = self.observer(context=CONTEXT)
        observer.event(native(0))
        observer.event(native(1))
        event = native(1)
        event['part']['state'] = None
        observer.event(event)
        self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 1)
        self.assertEqual(observer.snapshot()['calls'], 2)
        self.assertEqual(observer.snapshot()['invalid_events'], 0)

    def test_native_invalid_tool_name_is_incomplete(self):
        observer, _ = self.observer(context=CONTEXT)
        event = native(1)
        event['part']['tool'] = ['read']
        observer.event(event)
        self.assertEqual(observer.snapshot()['calls_by_tool'], {'unknown': 1})
        self.assertEqual(observer.snapshot()['incomplete_results'], 1)

    def test_native_unfinished_tool_and_new_request_do_not_claim_complete(self):
        observer, _ = self.observer(context=CONTEXT)
        observer.event(native(0))
        observer.event({'type': 'step_finish', 'part': {'reason': 'stop'}})
        event = native(1)
        event['part']['state']['status'] = 'running'
        observer.event(event)
        metrics = observer.finish()
        self.assertEqual(metrics['observation']['coverage'], 'partial')
        self.assertIn('Tool calls without completed results.', metrics['observation']['limitations'])
        observer, _ = self.observer(context=CONTEXT)
        observer.event({'type': 'step_finish', 'part': {'reason': 'stop'}})
        observer.event({'type': 'step_start', 'sessionID': 's', 'part': {'id': 'request', 'messageID': 'm'}})
        self.assertFalse(observer.terminal)

    def test_native_reads_do_not_assume_requested_offset_limit(self):
        observer, _ = self.observer(context=CONTEXT)
        event = native(1)
        event['part']['state']['input'].update(offset=900, limit=800)
        observer.event(event)
        self.assertEqual(observer.snapshot()['unique_lines'], 2)
        self.assertTrue(all(key[-1] in (1, 2) for key in observer.tool_lines))

    def test_same_submodule_path_distinct_and_excluded_unavailable(self):
        context = {'repository': '/source', 'branch': 'unit', 'source_commit': 'a',
                   'submodules': [{'path': 'first', 'expected_commit': 'b'}, {'path': 'second', 'expected_commit': 'c'}],
                   '_inventory': {'entries': [{'type': 'file', 'path': 'first/unit.py'},
                                              {'type': 'file', 'path': 'second/unit.py'}]}}
        observer, _ = self.observer(context=context)
        observer.event(native(1, path='/source/first/unit.py'))
        observer.event(native(2, path='/source/second/unit.py'))
        observer.event(native(3, path='/source/excluded.py'))
        self.assertEqual(observer.snapshot()['unique_lines'], 4)
        self.assertEqual(observer.snapshot()['line_coverage'], 'partial')

    def test_codex_only_completed_items_exact_no_coordinates(self):
        observer = ToolObserver('codex', context=CONTEXT)
        item = {'id': 'c1', 'type': 'command_execution', 'command': 'cat unit.py',
                'aggregated_output': '1: abc\n2: xyz', 'exit_code': 0, 'status': 'completed'}
        observer.event({'type': 'item.started', 'item': item})
        observer.event({'type': 'item.completed', 'item': item})
        item['id'] = 'c2'
        observer.event({'type': 'item.completed', 'item': item})
        observer.event({'type': 'turn.completed', 'usage': {'input_tokens': 20}})
        metrics = observer.finish()
        self.assertEqual(metrics['calls'], 2)
        self.assertEqual(metrics['exact_repeats'], 1)
        self.assertEqual(metrics['line_coverage'], 'unavailable')
        self.assertIsNone(metrics['unique_lines'])
        self.assertIsNone(metrics['model_requests'])

    def test_codex_unknown_completed_tool_resets(self):
        observer = ToolObserver('codex')
        observer.observe(call(0))
        observer.observe(call(1))
        observer.event({'type': 'item.completed', 'item': {'type': 'agent_message', 'id': 'text', 'text': 'thinking'}})
        self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 1)
        observer.event({'type': 'item.completed', 'item': {'type': 'new_tool_type', 'id': 'unknown'}})
        self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 0)
        self.assertEqual(observer.snapshot()['unknown_results'], 1)

    def test_malformed_event_resets_without_throwing(self):
        observer, _ = self.observer()
        observer.observe(call(0))
        observer.observe(call(1))
        observer.feed('{invalid\n')
        self.assertEqual(observer.snapshot()['consecutive_confirmed_repeats'], 0)
        self.assertEqual(observer.snapshot()['invalid_events'], 1)

    def test_replay_uses_same_observer_and_recorded_monotonic_time(self):
        saved = []
        observer, clock = self.observer(event_sink=saved.append)
        for n in range(60):
            clock.now = n * 3
            observer.observe(call(n))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tool-events.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in saved), encoding='utf-8')
            result = replay(path, 'opencode')
            self.assertEqual(result['possible_stop'], observer.diagnostic)
            self.assertEqual(result['timing'], 'monotonic-recorded')
            output = Path(folder) / 'report'
            self.assertEqual(main([folder, '--attempt', '.', '--backend', 'opencode', '--output', str(output)]), 0)
            self.assertTrue(output.with_suffix('.json').is_file())
            self.assertIn('No live model calls', output.with_suffix('.md').read_text())

    def test_replay_missing_time_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stdout.log'
            item = {'id': 'c', 'type': 'command_execution', 'command': 'cat unit.py',
                    'aggregated_output': 'abc', 'exit_code': 0, 'status': 'completed'}
            path.write_text(json.dumps({'type': 'item.completed', 'item': item}), encoding='utf-8')
            result = replay(path, 'codex')
            self.assertEqual(result['timing'], 'unavailable')
            self.assertIsNone(result['possible_stop'])
            self.assertIn('No per-call timestamps', ' '.join(result['metrics']['observation']['limitations']))

    def test_replay_cli_rejects_unsupported_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stdout.log'
            path.write_text('')
            output = Path(folder) / 'report'
            error = io.StringIO()
            with contextlib.redirect_stderr(error), patch('scripts.replay_tool_observation.replay', wraps=replay) as replay_attempt:
                with self.assertRaises(SystemExit) as caught:
                    main([folder, '--attempt', 'stdout.log', '--backend', 'unsupported-backend', '--output', str(output)])
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("invalid choice: 'unsupported-backend'", error.getvalue())
            replay_attempt.assert_not_called()
            self.assertFalse(output.with_suffix('.json').exists())
            self.assertFalse(output.with_suffix('.md').exists())

    def test_replay_preserves_prompt_invalid_duplicate_and_model_events(self):
        saved = []
        observer, clock = self.observer(context=CONTEXT, event_sink=saved.append)
        observer.seed_prompt([{'source_id': 'source-001', 'path': 'unit.py', 'start_line': 1, 'text': 'abc\nxyz'}])
        observer.event({'type': 'step_start', 'sessionID': 's', 'part': {'id': 'request', 'messageID': 'm'}})
        observer.event(native(0))
        observer.event(native(1))
        observer.event(native(1))
        observer.feed('bad json\n')
        observer.event({'type': 'step_finish', 'part': {'reason': 'stop'}})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in saved))
            result = replay(path, 'opencode')
            self.assertEqual(result['metrics'], observer.finish())

    def test_replay_preserves_unfinished_native_call_after_terminal_event(self):
        saved = []
        observer = ToolObserver('opencode', clock=lambda: 0.0, event_sink=saved.append)
        event = native(0)
        event['part']['state']['status'] = 'running'
        observer.event(event)
        observer.event({'type': 'step_finish', 'part': {'reason': 'stop'}})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in saved))
            result = replay(path, 'opencode')
            self.assertEqual(result['metrics'], observer.finish())
            self.assertIn('Tool calls without completed results.', result['metrics']['observation']['limitations'])

    def test_replay_nonfinite_and_incomplete_normalized_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.jsonl'
            path.write_text('\n'.join(json.dumps({'format': 'tool-observer-v1', 'observed_at_seconds': t,
                                                   'call': {'unexpected': True}})
                                      for t in (float('inf'), float('nan'), 4)))
            result = replay(path, 'opencode')
            self.assertEqual(result['metrics']['invalid_events'], 3)
            self.assertIsNone(result['possible_stop'])

    def test_replay_missing_native_call_time_resets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stdout.log'
            events = [native(n) for n in range(51)]
            del events[-2]['timestamp']
            path.write_text('\n'.join(json.dumps(e) for e in events))
            result = replay(path, 'opencode', context=CONTEXT)
            self.assertIsNone(result['possible_stop'])
            self.assertEqual(result['timing'], 'native-wall-clock-partial')
            self.assertEqual(result['metrics']['consecutive_confirmed_repeats'], 1)

    def test_replay_untimed_duplicate_or_pending_does_not_reset(self):
        for state in ('completed', 'running', 'pending'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'stdout.log'
                events = [native(n) for n in range(51)]
                extra = native(49 if state == 'completed' else 999)
                extra['part']['state']['status'] = state
                del extra['timestamp']
                events.insert(50, extra)
                path.write_text('\n'.join(json.dumps(e) for e in events))
                result = replay(path, 'opencode', context=CONTEXT)
                self.assertEqual(result['possible_stop']['call'], 51)
                self.assertEqual(result['possible_stop']['consecutive_confirmed_repeats'], 50)
                self.assertEqual(result['metrics']['invalid_events'], 0)


if __name__ == '__main__':
    unittest.main()
