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

from src.runtime.errors import AuditError
from src.runtime.runner import Runner
from src.runtime.process import check_read_loop, cli_env, process
from fixtures.cli_response import cli_result
from fixtures.ledger_response import prompt_context, response
from fixtures.xxx_protocol import transcript
from src.model.model_context import (SOURCE_INDEX_INSTRUCTION, build_model_prompt,
                                     project_model_context, serialize_model_input)
from src.runtime.execution import Budget
from src.runtime.tool_observer import ToolObserver
from test_folder import FolderFixture
from test_model_context import decode_source_context
from fixtures.study_runner import run_case


def command_event(index):
    return {'type': 'item.completed', 'item': {
        'id': f'item-{index}', 'type': 'command_execution', 'command': 'cat app.py',
        'aggregated_output': 'def main(): return 42\n', 'exit_code': 0, 'status': 'completed'}}


class PreparedContextTests(FolderFixture):
    run_case = run_case

    def assert_prompt_measurements(self, path):
        invocation = json.loads(path.read_text())
        raw = (path.parent / 'input.prompt.txt').read_bytes()
        prompt = raw.decode('utf-8')
        context_json, schema_json = prompt.split('\n\n# Authoritative orchestration context (data)\n', 1)[1].split(
            '\n\n# Required final JSON Schema\n', 1)
        measurements = invocation['input_measurements']
        self.assertEqual(invocation['input_bytes'], len(raw))
        for name, value in (('prompt', prompt), ('projected_context', context_json), ('schema', schema_json)):
            self.assertEqual(measurements[name], {'characters': len(value), 'utf8_bytes': len(value.encode('utf-8'))})
        self.assertEqual(context_json, serialize_model_input(json.loads(context_json)))
        self.assertEqual(schema_json, serialize_model_input(json.loads(schema_json)))
        self.assertIsNone(measurements['tokens'])
        context = json.loads(context_json)
        if 'source_context' not in context:
            self.assertNotIn('source_index', measurements)
            self.assertNotIn(SOURCE_INDEX_INSTRUCTION, prompt)
            self.assertNotIn('# Source index reading instruction', prompt)
            return
        self.assertEqual(prompt.count(SOURCE_INDEX_INSTRUCTION), 1)
        self.assertEqual(prompt.count('# Source index reading instruction'), 1)
        transmitted = context['source_context']
        original = decode_source_context(transmitted)
        original_bytes = len(json.dumps(original, ensure_ascii=False).encode('utf-8'))
        transmitted_bytes = len(serialize_model_input(transmitted).encode('utf-8'))
        saved_bytes = original_bytes - transmitted_bytes
        source_index = measurements['source_index']
        self.assertEqual(source_index['original_utf8_bytes'], original_bytes)
        self.assertEqual(source_index['transmitted_utf8_bytes'], transmitted_bytes)
        self.assertEqual(source_index['saved_utf8_bytes'], saved_bytes)
        self.assertAlmostEqual(source_index['reduction_percent'], saved_bytes * 100 / original_bytes, places=2)
        preparation = json.loads((path.parent / 'source-context.manifest.json').read_text())
        self.assertEqual(preparation['original_bytes'], original_bytes)
        self.assertEqual(preparation['transmitted_bytes'], transmitted_bytes)
        self.assertEqual(preparation['saved_bytes'], saved_bytes)
        self.assertEqual(preparation['reduction_percent'], source_index['reduction_percent'])

    def test_same_preparation_all_backends_and_source_scope(self):
        self.value['source_context'] = {'mode': 'full'}
        declarations = 1000
        (self.source / 'api.js').write_text('export class Service {\n  run() { return "private body"; }\n}\n' +
            ''.join(f'function operation{i:04d}() {{ return "private function body"; }}\n'
                    for i in range(declarations)))
        (self.base / 'outside.py').write_text('def excluded(): pass\n')
        original_seed = ToolObserver.seed_prompt
        seeded = []

        def seed(observer, fragments):
            original_seed(observer, fragments)
            seeded.append((fragments, observer.counts['prompt_lines'], dict(observer.delivered)))

        contexts = []
        for backend in ('codex', 'opencode', 'xxx'):
            with patch.object(ToolObserver, 'seed_prompt', autospec=True, side_effect=seed):
                manifest, code = self.run_case(backend=backend, review=True)
            self.assertEqual(code, 0, manifest.get('diagnostics'))
            contexts.append([call['context'].get('source_context') for call in self.calls])
            for call in self.calls:
                context = call['context']
                index = decode_source_context(context['source_context'])
                self.assertEqual(index['mode'], 'index-only')
                self.assertEqual(index['fragments'], [])
                self.assertGreater(len(json.dumps(index, ensure_ascii=False).encode()), 65536)
                assigned = {p.name for p in self.source.iterdir() if p.is_file()}
                self.assertEqual({f['path'] for f in index['files']}, assigned)
                self.assertEqual(index['omissions'], {'maps': 0, 'reasons': {}, 'incomplete': False})
                native = next(f for f in index['files'] if f['path'] == 'api.js')
                self.assertEqual(native['language'], 'javascript')
                self.assertTrue(native['parser'].startswith('ast-grep-py:'))
                actual = {(s['name'], s['kind'], s['parent']) for s in native['symbols']}
                self.assertEqual(actual, {('Service', 'export', None), ('Service', 'class', None), ('run', 'method', 'Service')} |
                                 {(f'operation{i:04d}', 'function', None) for i in range(declarations)})
                self.assertEqual(len(native['symbols']), declarations + 3)
                self.assertEqual(sum(len(f['symbols']) for f in index['files']), declarations + 3 + len(assigned) - 1)
                self.assertNotIn('private body', json.dumps(index))
                self.assertNotIn('private function body', json.dumps(index))
            attempts = list(self.runner.session.run_dir.rglob('attempt-*/invocation.json'))
            self.assertEqual(len(attempts), len(self.calls))
            for path in attempts:
                self.assert_prompt_measurements(path)
            manifests = list(self.runner.session.run_dir.rglob('source-context.manifest.json'))
            self.assertEqual(len(manifests), len(self.calls))
            for path in manifests:
                preparation = json.loads(path.read_text())
                self.assertEqual(preparation['mode'], 'index-only')
                self.assertEqual(preparation['transmitted_text_bytes'], 0)
                self.assertTrue(all(f['symbols'] == f['symbols_transmitted'] for f in preparation['files']))
        self.assertTrue(all(context == contexts[0] for context in contexts))
        self.assertEqual(len(seeded), sum(len(context) for context in contexts))
        self.assertTrue(all(fragments == [] and lines == 0 and delivered == {}
                            for fragments, lines, delivered in seeded))

    def test_default_on_demand_maps_all_backends_and_independent_reviews(self):
        (self.source / 'empty.py').write_text('')
        (self.source / 'notes.txt').write_text('No declarations here.\n')
        (self.source / 'кириллица.py').write_text('class Корень:\n    def метод(self): pass\n')
        indexes = []
        for backend in ('codex', 'opencode', 'xxx'):
            manifest, code = self.run_case(backend=backend, large=False, review=True)
            self.assertEqual(code, 0, manifest.get('diagnostics'))
            roots = []
            for call in self.calls:
                wire = call['context']['source_context']
                self.assertEqual(wire['delivery'], 'on-demand')
                self.assertNotIn('symbols', wire['file_columns'])
                self.assertNotIn('symbol_columns', wire)
                self.assertNotIn('symbol_value_tables', wire)
                root = Path(wire['maps_root'])
                self.assertTrue(root.is_absolute())
                self.assertFalse(root.is_relative_to(self.source))
                roots.append(root)
                restored = decode_source_context(wire)
                indexes.append(restored)
                records = {file['path']: file for file in restored['files']}
                self.assertEqual(set(records), {'app.py', 'empty.py', 'notes.txt', 'кириллица.py'})
                self.assertEqual(records['empty.py']['symbols'], [])
                self.assertEqual(records['notes.txt']['map_reason'], 'unknown_language')
                self.assertEqual([s['name'] for s in records['кириллица.py']['symbols']], ['Корень', 'метод'])
                self.assertEqual(len(list(root.glob('*.json'))), 2)
                for path in root.glob('*.json'):
                    self.assertGreater(len(path.read_text().splitlines()), 1)
                if backend in ('opencode', 'xxx'):
                    cfg = json.loads(call['env']['OPENCODE_CONFIG_CONTENT'])
                    name = call['command'][call['command'].index('--agent') + 1]
                    permission = (cfg['agents'][name]['permissions'] if backend == 'opencode' else
                                  cfg['agent'][name]['permission']['external_directory'])
                    self.assertIn(root.as_posix() + '/*', json.dumps(permission))
            self.assertEqual(len(set(roots)), len(self.calls))
            for path in self.runner.session.run_dir.rglob('attempt-*/invocation.json'):
                self.assert_prompt_measurements(path)
                meta = json.loads(path.read_text())
                stored = sum(p.stat().st_size for p in (path.parent / 'source-maps').glob('*.json'))
                self.assertEqual(meta['input_measurements']['source_index']['stored_map_utf8_bytes'], stored)
                preparation = json.loads((path.parent / 'source-context.manifest.json').read_text())
                self.assertEqual(preparation['stored_map_utf8_bytes'], stored)
                self.assertTrue(all(f['symbols_transmitted'] == 0 for f in preparation['files']))
        self.assertTrue(all(index == indexes[0] for index in indexes))

    def test_changed_missing_or_symlinked_map_blocks_publication(self):
        for backend in ('codex', 'opencode', 'xxx'):
            for action in ('change', 'remove', 'symlink'):
                def change(runner, context, data):
                    wire = context['source_context']
                    column = wire['file_columns'].index('symbols_ref')
                    name = next(row[column] for row in wire['files'] if row[column])
                    path = Path(wire['maps_root']) / name
                    if action == 'change':
                        path.write_text('{}\n')
                    elif action == 'remove':
                        path.unlink()
                    else:
                        replacement = self.base / 'replacement.json'
                        replacement.write_bytes(path.read_bytes())
                        path.unlink()
                        path.symlink_to(replacement)
                with self.subTest(backend=backend, action=action):
                    manifest, code = self.run_case(backend=backend, large=False, change=change)
                    self.assertNotEqual(code, 0)
                    self.assertTrue(manifest['critical_failure'])
                    meta = json.loads(next(self.runner.session.run_dir.rglob('study.logs/invocation.json')).read_text())
                    self.assertFalse(meta['publication_complete'])
                    self.assertEqual(meta['error']['code'], 'MODEL_BINDING_CHANGED')

    def test_disabled_context_and_projection_exclusions(self):
        self.value['source_context'] = {'enabled': False}
        for backend in ('codex', 'opencode', 'xxx'):
            with self.subTest(backend=backend):
                manifest, code = self.run_case(large=False, backend=backend)
                self.assertEqual(code, 0, manifest.get('diagnostics'))
                self.assertTrue(all('source_context' not in c['context'] for c in self.calls))
                self.assertTrue(all('file_columns' not in call['prompt'] and 'symbol_columns' not in call['prompt']
                                    for call in self.calls))
                for path in self.runner.session.run_dir.rglob('attempt-*/invocation.json'):
                    self.assert_prompt_measurements(path)
        for stage, variant in (('compare', 'compare'), ('repair', 'repair')):
            projected = project_model_context(stage, {'source_context': {'fragments': []},
                                                     'prompt_variant': variant})
            self.assertNotIn('source_context', projected)
            prompt = build_model_prompt('', serialize_model_input(projected), '{}', 'codex')
            self.assertNotIn('file_columns', prompt)
            self.assertNotIn('symbol_columns', prompt)
            self.assertNotIn(SOURCE_INDEX_INSTRUCTION, prompt)
            self.assertNotIn('# Source index reading instruction', prompt)

    def test_no_progress_keeps_inventory_plan_logs_and_metrics_without_retry(self):
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

        with patch.object(runner, 'check_cli', return_value={}), patch('src.runtime.process.process', side_effect=answer):
            manifest, code = runner.run()
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, ['study'])
        self.assertEqual(manifest['coverage_plan']['origin'], 'SOURCE_INVENTORY')
        self.assertEqual(json.loads((runner.session.run_dir / 'coverage.plan.json').read_text()), manifest['coverage_plan'])
        attempts = list(runner.session.run_dir.rglob('attempt-*/invocation.json'))
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

        with patch.object(runner, 'check_cli', return_value={}), patch('src.runtime.process.process', side_effect=answer):
            manifest, code = runner.run()
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, ['study'])
        self.assertEqual(service_calls, ['export', 'delete'])
        self.assertEqual(exports, {})
        failed = json.loads(next(runner.session.run_dir.rglob('study.logs/attempt-001/invocation.json')).read_text())
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

