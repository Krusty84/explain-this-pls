# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Shared prompt data and read-loop termination through the orchestrator."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from explain import AuditError, Runner, check_read_loop, cli_env, process
from fixtures.cli_response import cli_result
from fixtures.ledger_response import prompt_context, response
from fixtures.xxx_protocol import transcript
from src.model.model_context import project_model_context
from src.runtime.execution import Budget
from src.runtime.tool_observer import ToolObserver
from test_folder import FolderFixture
from fixtures.study_runner import run_case


def command_event(index):
    return {'type': 'item.completed', 'item': {
        'id': f'item-{index}', 'type': 'command_execution', 'command': 'cat app.py',
        'aggregated_output': 'def main(): return 42\n', 'exit_code': 0, 'status': 'completed'}}


class PreparedContextTests(FolderFixture):
    run_case = run_case

    def test_same_preparation_all_backends_and_source_scope(self):
        contexts = []
        for backend in ('codex', 'opencode', 'xxx'):
            manifest, code = self.run_case(backend=backend)
            self.assertEqual(code, 0, manifest.get('diagnostics'))
            contexts.append([call['context'].get('source_context') for call in self.calls])
            for call in self.calls:
                context = call['context']
                fragments = context['source_context']['fragments']
                assigned = {p.name for p in self.source.glob('*.py')}
                self.assertTrue(fragments)
                self.assertTrue(all(f['path'] in assigned for f in fragments))
            for call in self.calls:
                if 'source_context' in call['context']:
                    self.assertLessEqual(len(json.dumps(call['context']['source_context'],
                                                        ensure_ascii=False).encode()), 65536)
            manifests = list(self.runner.run_dir.rglob('source-context.manifest.json'))
            self.assertEqual(len(manifests), len(self.calls))
        self.assertTrue(all(context == contexts[0] for context in contexts))

    def test_disabled_context_and_projection_exclusions(self):
        self.value['source_context'] = {'enabled': False}
        manifest, code = self.run_case(large=False)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertTrue(all('source_context' not in c['context'] for c in self.calls))
        for stage, variant in (('compare', 'compare'), ('repair', 'repair')):
            projected = project_model_context(stage, {'source_context': {'fragments': []},
                                                     'prompt_variant': variant})
            self.assertNotIn('source_context', projected)

    def test_no_progress_keeps_logs_metrics_without_a_plan_without_retry(self):
        cfg = self.config()
        cfg['execution']['structured_output_repair_attempts'] = 2
        cfg['execution']['opencode_format_retries'] = 2
        runner = Runner(cfg, self.base / 'guard-run')
        calls = []

        def answer(command, cwd, env, payload, **kwargs):
            context = prompt_context(payload)
            calls.append(context['stage'])
            result = cli_result(command, response(context))
            if context['stage'] == 'study':
                observer = kwargs['observer']
                current = [observer.clock()]
                observer.clock = lambda: current[0]
                output = []
                for i in range(51):
                    current[0] += 3
                    event = json.dumps(command_event(i)) + '\n'
                    output.append(event)
                    observer.feed(event)
                (kwargs['log_dir'] / 'stdout.log').write_text(''.join(output))
                check_read_loop(observer)
                self.fail('The repeated results must stop the stage.')
            return result

        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=answer):
            manifest, code = runner.run()
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, ['study'])
        self.assertNotIn('coverage_plan', manifest)
        attempts = list(runner.run_dir.rglob('attempt-*/invocation.json'))
        failed = next(json.loads(path.read_text()) for path in attempts
                      if json.loads(path.read_text())['status'] == 'FAILED')
        self.assertEqual(failed['error']['code'], 'NO_PROGRESS')
        self.assertTrue(failed['source_integrity_verified'])
        self.assertFalse(failed['publication_complete'])
        self.assertIn('tool_observation', failed['metrics'])
        self.assertTrue((Path(failed['artifact_directory']) / 'tool-events.jsonl').is_file())

    def test_xxx_stop_deletes_only_owned_session_without_observing_cleanup(self):
        self.value['agent']['backend'] = 'xxx'
        cfg = self.config()
        runner = Runner(cfg, self.base / 'xxx-guard-run')
        exports, calls, service_calls = {}, [], []

        def answer(command, cwd, env, payload=b'', **kwargs):
            if command[1] == 'export':
                self.assertNotIn('observer', kwargs)
                service_calls.append('export')
                return {'returncode': 0, 'stdout': json.dumps(exports[command[2]]).encode(), 'stderr': b''}
            if command[1:3] == ['session', 'delete']:
                self.assertNotIn('observer', kwargs)
                service_calls.append('delete')
                del exports[command[3]]
                return {'returncode': 0, 'stdout': b'', 'stderr': b''}
            context = prompt_context(payload)
            calls.append(context['stage'])
            sid = 'ses_guard' + str(len(calls))
            output, exported = transcript(response(context), session=sid,
                agent=command[command.index('--agent') + 1], prompt=payload.decode(), cwd=str(cwd))
            exports[sid] = exported
            if context['stage'] == 'study':
                observer = kwargs['observer']
                current = [observer.clock()]
                observer.clock = lambda: current[0]
                events = [json.loads(output.splitlines()[0])]
                for index in range(51):
                    events.append({'type': 'tool_use', 'sessionID': sid, 'part': {
                        'id': f'prt_{index}', 'callID': f'call_{index}', 'sessionID': sid,
                        'messageID': 'msg_guard', 'type': 'tool', 'tool': 'grep',
                        'state': {'status': 'completed', 'input': {'pattern': 'absent'}, 'output': 'No matches'}}})
                output = ''.join(json.dumps(event) + '\n' for event in events)
                (kwargs['log_dir'] / 'stdout.log').write_text(output)
                for event in events:
                    current[0] += 3
                    observer.feed(json.dumps(event) + '\n')
                check_read_loop(observer)
                self.fail('The repeated results must stop XXX.')
            return {'returncode': 0, 'stdout': output.encode(), 'stderr': b''}

        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=answer):
            manifest, code = runner.run()
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, ['study'])
        self.assertEqual(service_calls, ['export', 'delete'])
        self.assertEqual(exports, {})
        failed = json.loads(next(runner.run_dir.rglob('study.logs/attempt-001/invocation.json')).read_text())
        self.assertEqual(failed['error']['code'], 'NO_PROGRESS')
        self.assertEqual(failed['session_cleanup'], 'deleted')
        self.assertEqual(failed['tool_observation']['calls'], 51)


class ProcessGuardTests(unittest.TestCase):
    def test_real_child_stops_and_preserves_stdout_with_artificial_observation_clock(self):
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            count = [0]

            def clock():
                count[0] += 3
                return count[0]

            observer = ToolObserver('codex', clock=clock)
            events = '\n'.join(json.dumps(command_event(i)) for i in range(51)) + '\n'
            script = 'import sys,time\nsys.stdout.write(' + repr(events) + ')\nsys.stdout.flush()\ntime.sleep(60)'
            started = time.monotonic()
            with self.assertRaises(AuditError) as caught:
                process([sys.executable, '-c', script], cwd, cli_env(cwd),
                        log_dir=cwd, budget=Budget(10), observer=observer)
            self.assertEqual(caught.exception.code, 'NO_PROGRESS')
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual((cwd / 'stdout.log').read_text(), events)

