# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Usage accounting through real adapters, orchestration and offline CLI processes."""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from explain import Runner
from src.backends import codex, claude_code
from src.contracts.contracts import response_error
from src.runtime.metrics import HTTPUsage, RunMetrics, measurement, number, sum_usage, usage
from src.runtime.reporting import Reporter
from fixtures.cli_response import cli_result
from fixtures.ledger_response import prompt_context, response
from test_folder import FolderFixture
from test_revision_pipeline import initial_finding
import test_startup as startup
import test_xxx as native


def claude_envelope(**extra):
    return {'is_error': False, 'usage': {'input_tokens': 10, 'output_tokens': 4,
                'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 3},
            'total_cost_usd': 0.1, **extra}


def native_message(mid='msg_one', *, total=None, steps=False):
    tokens = {'input': 10, 'output': 4, 'reasoning': 2, 'cache': {'read': 20, 'write': 3}}
    if total is not None:
        tokens['total'] = total
    info = {'id': mid, 'sessionID': 'ses_test', 'parentID': 'msg_request', 'agent': 'audit',
            'role': 'assistant', 'providerID': 'provider', 'modelID': 'model',
            'time': {'completed': 100}, 'tokens': tokens, 'cost': 0.1}
    parts = [{'id': 'prt_' + str(i), 'sessionID': 'ses_test', 'messageID': mid, 'type': 'step-finish',
              'tokens': copy.deepcopy(tokens), 'cost': 0.1} for i in range(2)] if steps else []
    return {'info': info, 'parts': parts}


class NormalizerTests(unittest.TestCase):
    def test_codex_cache_reasoning_and_duplicate_completion_are_not_added(self):
        events = [{'type': 'turn.started'}, {'type': 'turn.completed', 'usage': {
            'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 20, 'reasoning_output_tokens': 5}}]
        events.append(events[-1])
        result = codex.collect_metrics('\n'.join(map(json.dumps, events)), 'requested')['usage']
        self.assertEqual(result['total_tokens'], 120)
        self.assertEqual(result['input_tokens'], 100)
        self.assertEqual(result['reasoning_tokens'], 5)
        self.assertIsNone(result['cost_usd'])
        self.assertEqual(result['coverage']['total_tokens'], 'complete')
        partial = codex.collect_metrics('\n'.join(map(json.dumps, events)) + '\n{"type":"turn.started"}\n{')
        self.assertEqual(partial['usage']['total_tokens'], 120)
        self.assertEqual(partial['usage']['coverage']['total_tokens'], 'partial')
        self.assertIsNone(partial['by_model'][0]['model_actual'])

    def test_claude_model_usage_takes_precedence_and_cost_is_not_added_twice(self):
        model = {'inputTokens': 10, 'outputTokens': 4, 'cacheReadInputTokens': 20,
                 'cacheCreationInputTokens': 3, 'costUSD': 0.1}
        envelope = claude_envelope(modelUsage={'one': model, 'two': model}, total_cost_usd=0.2)
        result = claude_code.collect_metrics(json.dumps(envelope))
        self.assertEqual(result['usage']['input_tokens'], 66)
        self.assertEqual(result['usage']['total_tokens'], 74)
        self.assertEqual(result['usage']['cost_usd'], 0.2)
        self.assertEqual([e['model_actual'] for e in result['by_model']], ['one', 'two'])

    def test_claude_fallback_and_error_result_keep_usage(self):
        for failed in (False, True):
            result = claude_code.collect_metrics(json.dumps(claude_envelope(is_error=failed)))['usage']
            self.assertEqual((result['input_tokens'], result['total_tokens'], result['cost_usd']), (33, 37, 0.1))
        crash = claude_code.collect_metrics(json.dumps(claude_envelope(
            is_error=True, subtype='error_during_execution', total_cost_usd=0)))['usage']
        self.assertEqual(crash['cost_usd'], 0)
        self.assertEqual(crash['coverage']['cost_usd'], 'partial')
        self.assertEqual(crash['coverage']['total_tokens'], 'partial')

    def test_absent_zero_and_invalid_numbers_are_distinct(self):
        for value in (True, -1, float('nan'), float('inf'), '10', 10 ** 1000):
            self.assertIsNone(number(value))
        self.assertIsNone(number(1.5, tokens=True))
        self.assertEqual(number(0), 0)
        mixed = sum_usage([usage({'total_tokens': 10, 'cost_usd': 0}), usage()])
        self.assertEqual((mixed['total_tokens'], mixed['cost_usd']), (10, 0))
        self.assertEqual(mixed['coverage']['total_tokens'], 'partial')
        self.assertEqual(mixed['coverage']['cost_usd'], 'partial')

    def test_http_whole_history_prefers_steps_and_replaces_polled_snapshots(self):
        collector = HTTPUsage('xxx', None, 'ses_test', 'msg_request', 'audit')
        one, two = native_message(steps=True), native_message('msg_two', total=40)
        two['info']['modelID'] = 'second-model'
        one['parts'].append(copy.deepcopy(one['parts'][0]))
        collector.observe([one])
        collector.observe([one])
        result = collector.observe([one, two], complete=True)
        self.assertEqual(result['usage']['total_tokens'], 37 * 2 + 40)
        self.assertAlmostEqual(result['usage']['cost_usd'], 0.3)
        self.assertEqual(result['usage']['reasoning_tokens'], 6)
        self.assertTrue(result['usage']['total_tokens_estimated'])
        self.assertEqual(result['usage']['coverage']['total_tokens'], 'complete')
        self.assertEqual({entry['model_actual'] for entry in result['by_model']},
                         {'provider/model', 'provider/second-model'})
        self.assertEqual(collector.observe([one, two], complete=True), result)
        updated = copy.deepcopy(one)
        updated['parts'][1]['tokens']['output'] = 8
        self.assertEqual(collector.observe([updated, two], complete=True)['usage']['total_tokens'],
                         result['usage']['total_tokens'] + 4)

    def test_http_foreign_data_and_unfinished_placeholder_are_not_counted(self):
        for field, wrong in (('sessionID', 'ses_foreign'), ('parentID', 'msg_foreign'), ('agent', 'other')):
            collector = HTTPUsage('xxx', None, 'ses_test', 'msg_request', 'audit')
            foreign = native_message('msg_foreign')
            foreign['info'][field] = wrong
            result = collector.observe([native_message(), foreign], complete=True)['usage']
            self.assertEqual(result['total_tokens'], 37)
            self.assertEqual(result['coverage']['total_tokens'], 'partial')
        collector = HTTPUsage('xxx', None, 'ses_test', 'msg_request', 'audit')
        unfinished = native_message()
        unfinished['info']['time'] = {}
        waiting = collector.observe([unfinished])
        self.assertIsNone(waiting['usage']['total_tokens'])
        self.assertEqual(waiting['by_model'][0]['model_actual'], 'provider/model')

    def test_http_reported_total_and_zero_cost_are_preserved(self):
        item = native_message(total=50)
        item['info']['cost'] = 0
        collector = HTTPUsage('xxx', None, 'ses_test', 'msg_request', 'audit')
        result = collector.observe([item], complete=True)['usage']
        self.assertEqual(result['total_tokens'], 50)
        self.assertFalse(result['total_tokens_estimated'])
        self.assertEqual(result['cost_usd'], 0)
        del item['info']['cost']
        self.assertIsNone(collector.observe([item], complete=True)['usage']['cost_usd'])

    def test_registry_deduplicates_attempts_and_measures_wall_time(self):
        clock = [0]
        registry = RunMetrics(lambda: clock[0])
        context = {'branch': 'main', 'stage': 'study', 'revision_id': '001'}
        registry.start(context)
        metrics = measurement('codex', 'requested', measured=usage({'total_tokens': 100}))
        for invocation in ('one', 'one', 'two'):
            registry.record(context, {'invocation_id': invocation, 'backend': 'codex', 'metrics': metrics})
        registry.record(context, {'invocation_id': 'two', 'backend': 'claude-code',
                                 'metrics': measurement('claude-code', 'requested', 'reported',
                                                        usage({'total_tokens': 100, 'cost_usd': 0.1}))})
        clock[0] = 5
        registry.finish_stage(context, 'FAILED')
        clock[0] = 9
        result = registry.snapshot(finish=True)
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['usage']['total_tokens'], 200)
        self.assertEqual({row['backend']: row['usage']['total_tokens'] for row in result['by_backend']},
                         {'codex': 100, 'claude-code': 100})
        self.assertEqual(result['by_model'][1]['model_actual'], 'reported')
        self.assertEqual(result['duration_seconds'], 9)
        self.assertEqual(result['stages'][0]['duration_seconds'], 5)
        clock[0] = 10
        self.assertEqual(registry.snapshot()['duration_seconds'], 9)


class PipelineMetricsTests(FolderFixture):
    def test_revision_totals_include_all_five_stages_without_selected_aliases(self):
        cfg = self.config()
        out, err = io.StringIO(), io.StringIO()
        reporter = Reporter(stdout=out, stderr=err, progress=False)
        self.addCleanup(reporter.close)
        runner = Runner(cfg, self.base / 'run', reporter=reporter)
        def answer(command, cwd, env, payload, **kwargs):
            context = prompt_context(payload)
            return cli_result(command, initial_finding(context['stage'], context, response(context)))
        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=answer):
            manifest, code = runner.run()
        self.assertEqual(code, 0)
        metrics = manifest['metrics']
        self.assertEqual(metrics['attempts'], 5)
        self.assertEqual(metrics['usage']['total_tokens'], 600)
        self.assertEqual([r['stage'] for r in metrics['stages']], ['catalog', 'study', 'review', 'revise', 'review'])
        self.assertEqual(err.getvalue().count('Tokens: 120'), 5)
        self.assertEqual(json.loads((runner.run_dir / 'manifest.json').read_text())['metrics'], metrics)
        self.assertNotIn('Total usage:', Path(manifest['final_report']).read_text())

    def test_errors_timeouts_and_interruptions_retain_cli_usage(self):
        for backend in ('codex', 'claude-code'):
            for failure in ('exit', 'timeout', 'interrupt', 'validation'):
                cfg = self.config()
                for agent in cfg['_agents'].values():
                    agent['backend'] = backend
                cfg['continue_on_error'] = False
                runner = Runner(cfg, self.base / (backend + failure))
                def answer(command, cwd, env, payload, **kwargs):
                    context = prompt_context(payload)
                    data = response(context)
                    failed = context['stage'] == 'study'
                    if failed and failure == 'validation':
                        data['unexpected'] = 'invalid schema'
                    if backend == 'claude-code':
                        result = cli_result(command, claude_envelope(structured_output=data,
                                            is_error=failed and failure == 'exit'))
                    else:
                        result = cli_result(command, data)
                    if failed and failure in ('timeout', 'interrupt'):
                        output = result['stdout'] + (b'\n{"type":"turn.started"}\n' if backend == 'codex' else b'')
                        (kwargs['log_dir'] / 'stdout.log').write_bytes(output)
                        if failure == 'interrupt':
                            raise KeyboardInterrupt()
                        raise response_error('STAGE_TIMEOUT', 'execution', 'Fixture timeout')
                    if failed and failure == 'exit':
                        result['returncode'] = 17
                    return result
                with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=answer):
                    manifest, code = runner.run()
                with self.subTest(backend=backend, failure=failure):
                    self.assertNotEqual(code, 0)
                    metrics = manifest['metrics']
                    self.assertEqual(metrics['attempts'], 2)
                    self.assertEqual(metrics['usage']['total_tokens'], 240 if backend == 'codex' else 74)
                    self.assertEqual(metrics['stages'][-1]['status'], 'FAILED')
                    if backend == 'codex' and failure in ('timeout', 'interrupt'):
                        self.assertEqual(metrics['usage']['coverage']['total_tokens'], 'partial')
                    attempt = json.loads((runner.run_dir / 'revisions/001/study.logs/attempt-001/invocation.json').read_text())
                    self.assertEqual(attempt['metrics']['attempts'], 1)
                    self.assertGreaterEqual(attempt['metrics']['duration_seconds'], 0)


class CLIMetricsTests(unittest.TestCase):
    setUp = startup.StartupCLIIntegrationTests.setUp
    tearDown = startup.StartupCLIIntegrationTests.tearDown
    git = startup.StartupCLIIntegrationTests.git
    prepare = startup.StartupCLIIntegrationTests.prepare

    def test_json_manifest_text_and_no_progress_agree(self):
        for mode, attempts in (('folder', 3), ('git', 7)):
            for backend in ('codex', 'claude-code'):
                args = self.prepare(mode)
                config = json.loads(self.config_path.read_text().split('\n', 1)[1])
                config['agent']['backend'] = backend
                self.config_path.write_text(json.dumps(config))
                result = subprocess.run([sys.executable, '-B', str(startup.ROOT / 'explain.py'),
                    *args, '--output', 'json', '--no-progress'], env=self.env,
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                summary = json.loads(result.stdout)
                manifest = json.loads(Path(summary['manifest']).read_text())
                metrics = summary['metrics']
                self.assertEqual(metrics, manifest['metrics'])
                self.assertEqual(metrics['attempts'], attempts)
                self.assertEqual(metrics['usage']['total_tokens'], 120 * attempts)
                self.assertEqual(result.stderr.count('Tokens: 120'), attempts)
                out = io.StringIO()
                reporter = Reporter(stdout=out, stderr=io.StringIO(), progress=False)
                reporter.finish(summary, manifest, check_only=False, config_path=self.config_path)
                reporter.close()
                self.assertIn('Total usage:', out.getvalue())
                self.assertIn(str(120 * attempts), out.getvalue())
                self.assertIn('Agent-estimated cost', out.getvalue())
        result = subprocess.run([sys.executable, '-B', str(startup.ROOT / 'explain.py'),
            *self.prepare('folder', check=True), '--output', 'json'], env=self.env,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(result.stdout)['metrics']['attempts'], 0)


class XXXMetricsTests(unittest.TestCase):
    setUp = native.XXXTests.setUp
    run_case = native.XXXTests.run_case

    def test_native_normal_error_and_format_repair_totals(self):
        for scenario in ('', 'backend-error', 'repair-ok'):
            self.value['execution'] = {'structured_output_repair_attempts': 1}
            manifest, code = self.run_case(scenario)
            attempts = 3 if not scenario else 2 if scenario == 'backend-error' else 5
            metrics = manifest['metrics']
            self.assertEqual(metrics['attempts'], attempts)
            self.assertEqual(metrics['usage']['total_tokens'], 10 * attempts)
            self.assertTrue(metrics['usage']['total_tokens_estimated'])
            self.assertEqual(metrics['usage']['cost_usd'], 0)
            self.assertEqual(metrics['usage']['coverage']['total_tokens'],
                             'partial' if scenario == 'backend-error' else 'complete')
            files = [p for p in self.run_dir.rglob('attempt-*/invocation.json')
                     if 'metrics' in json.loads(p.read_text())]
            self.assertEqual(len(files), attempts)
            self.assertEqual(sum(json.loads(p.read_text())['metrics']['usage']['total_tokens'] for p in files),
                             metrics['usage']['total_tokens'])
            for path in files:
                if path.parent.name == 'attempt-002':
                    attempt = json.loads(path.read_text())
                    self.assertLess(attempt['metrics']['duration_seconds'], attempt['duration_seconds'])
