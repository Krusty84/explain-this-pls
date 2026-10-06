#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Owned fake HTTP integration; never evidence of a real XXX continuation/hook."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

import test_xxx as base
from src.backends.opencode import prepare_environment
from src.runtime.reporting import Reporter
from src.runtime.metrics import measurement


class CompactionHTTPTests(unittest.TestCase):
    setUp = base.XXXTests.setUp
    run_case = base.XXXTests.run_case
    recorded = base.XXXTests.recorded
    prompts = base.XXXTests.prompts
    git = base.XXXTests.git
    init_git = base.XXXTests.init_git

    def test_text_start_reset_is_observed_before_native_result_through_runner(self):
        manifest, code = self.run_case('history-text-completion')
        self.assertEqual(code, 0, manifest)
        self.assertTrue(manifest['accepted'])
        self.assertTrue((self.run_dir / 'ARCHITECTURE.md').exists())
        self.assertEqual(manifest['metrics']['attempts'], 3)
        meta = manifest['study_invocation']
        self.assertTrue(meta['native_envelope_valid'])
        self.assertTrue(meta['backend_result_valid'])
        self.assertEqual(meta['compaction']['transitions'], 0)
        artifacts = Path(meta['artifact_directory'])
        responses = meta['http_responses']
        histories = [json.loads((artifacts / r['artifact']).read_text()) for r in responses
                     if r['operation'] == 'GET /session/{sessionID}/message']
        # A GET advances the script once; POST waits for all three GETs. The
        # client's sequential polling validates opened and closed before it can
        # request final. Assert the private wire artifacts as well as acceptance.
        observed = [h for h in histories if len(h) == 2]
        self.assertGreaterEqual(len(observed), 3)
        opened, closed, final = observed[:3]
        for snapshot in (opened, closed):
            self.assertNotIn('completed', snapshot[-1]['info']['time'])
            self.assertNotIn('finish', snapshot[-1]['info'])
            self.assertNotIn('structured', snapshot[-1]['info'])
            self.assertEqual(len(snapshot[-1]['parts']), 1)
        self.assertEqual(opened[-1]['parts'][0]['text'], '')
        self.assertEqual(opened[-1]['parts'][0]['time'], {'start': 100})
        self.assertEqual(closed[-1]['parts'][0]['text'], 'Result.')
        self.assertEqual(closed[-1]['parts'][0]['time'], {'start': 120, 'end': 121})
        for key in ('id', 'messageID', 'sessionID', 'type'):
            self.assertEqual(opened[-1]['parts'][0][key], closed[-1]['parts'][0][key])
        envelope = json.loads((artifacts / 'response.json').read_text())
        self.assertEqual(final[-1], envelope)
        self.assertEqual(envelope['parts'][0], closed[-1]['parts'][0])
        self.assertEqual(envelope['parts'][-1]['tool'], 'StructuredOutput')
        self.assertEqual(envelope['parts'][-1]['state']['input'], envelope['info']['structured'])
        self.assertEqual((self.source / 'app.py').read_text(), 'print(1)\n')

    def test_metadata_and_stream_finalization_publish_through_production_runner(self):
        for scenario, count in (('history-metadata', 0), ('compact-metadata', 1)):
            with self.subTest(scenario=scenario):
                manifest, code = self.run_case(scenario)
                self.assertEqual(code, 0, manifest)
                self.assertTrue(manifest['accepted'])
                self.assertTrue((self.run_dir / 'ARCHITECTURE.md').exists())
                self.assertEqual(manifest['metrics']['attempts'], 3)
                meta = manifest['study_invocation']
                self.assertTrue(meta['native_envelope_valid'])
                self.assertTrue(meta['backend_result_valid'])
                self.assertEqual(meta['compaction']['continuations'], count)
                self.assertEqual(meta['metrics']['usage']['total_tokens'], 10 + 8 * count)
                artifacts = Path(meta['artifact_directory'])
                envelope = json.loads((artifacts / 'response.json').read_text())
                self.assertEqual(envelope['parts'][0]['text'], 'Результат.')
                self.assertEqual(envelope['parts'][1]['text'], 'Ход\nГотово.')
                self.assertEqual(envelope['parts'][0]['time'], {'start': 120, 'end': 121})
                self.assertEqual(envelope['parts'][1]['time'], {'start': 100, 'end': 121})
                histories = [json.loads((artifacts / r['artifact']).read_text()) for r in meta['http_responses']
                             if r['operation'] == 'GET /session/{sessionID}/message']
                histories = [h for h in histories if h]  # Polls before script publication may be empty.
                self.assertTrue(any(h[0]['info'].get('summary') == {'diffs': []} for h in histories))
                self.assertTrue(any(h[0]['info'].get('summary', {}).get('title') == 'Synthetic title' for h in histories))
                self.assertTrue(any(h[-1] == envelope for h in histories))
                pending = [h[-1] for h in histories if h[-1]['info']['id'] == envelope['info']['id']
                           and 'completed' not in h[-1]['info']['time']]
                self.assertTrue(any(p['parts'][0]['time'] == {'start': 100} for p in pending))
                self.assertTrue(any(p['parts'][0]['time'] == {'start': 120, 'end': 121} for p in pending))
                self.assertEqual((self.source / 'app.py').read_text(), 'print(1)\n')

    def test_metadata_does_not_extend_idle_and_identity_diagnostic_is_safe(self):
        self.value['execution'] = {'stage_timeout_seconds': 5, 'idle_timeout_seconds': .5}
        manifest, code = self.run_case('history-metadata-idle')
        self.assertEqual(code, 1, manifest)
        self.assertEqual(manifest['diagnostics'][0]['failure_kind'], 'IDLE_TIMEOUT')
        self.assertFalse((self.run_dir / 'ARCHITECTURE.md').exists())
        self.value['execution'] = {'stage_timeout_seconds': 5}
        manifest, code = self.run_case('history-agent-change')
        self.assertEqual(code, 1, manifest)
        details = manifest['diagnostics'][0]['details']
        self.assertEqual(details['code'], 'MESSAGE_IDENTITY_CHANGED')
        self.assertEqual(details['field'], 'info.agent')
        self.assertEqual(details['role'], 'user')
        self.assertEqual(details['phase'], 'stage')
        self.assertEqual(details['transitions'], 0)
        self.assertNotIn('private-changed-agent', json.dumps(manifest['diagnostics']))
        for pid in {c['server_pid'] for c in self.recorded()}:
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
        calls = self.recorded()
        self.assertTrue(any(c['path'].endswith('/abort') for c in calls))
        self.assertTrue(any(c['method'] == 'DELETE' for c in calls))

    def test_one_and_multiple_compactions_in_folder_and_git(self):
        for scenario in ('compact-one', 'compact-multiple'):
            with self.subTest(scenario=scenario):
                previous_prompts = len(self.prompts())
                manifest, code = self.run_case(scenario)
                self.assertEqual(code, 0, manifest)
                self.assertTrue(manifest['accepted'])
                self.assertEqual(manifest['metrics']['attempts'], 3)
                meta = manifest['study_invocation']
                count = 2 if scenario == 'compact-multiple' else 1
                self.assertEqual(meta['compaction']['continuations'], count)
                self.assertEqual(meta['compaction']['completed'], count)
                request = json.loads((Path(meta['artifact_directory']) / 'request.json').read_text())
                self.assertEqual(meta['request_id'], request['messageID'])
                self.assertEqual(meta['metrics']['usage']['total_tokens'], 10 + 8 * count)
                self.assertEqual(meta['metrics']['usage']['coverage']['total_tokens'], 'complete')
                self.assertEqual(meta['metrics']['attempts'], 1)
                prompts = self.prompts()[previous_prompts:]
                self.assertEqual(len(prompts), 3)
                self.assertEqual(len({c['path'] for c in prompts}), 3)
                events = [event['event'] for event in meta['compaction']['events']]
                self.assertEqual(events.count('compaction_started'), count)
                self.assertEqual(events.count('compaction_completed'), count)
                self.assertEqual(events.count('session_continued'), count)
                measurements = meta['input_measurements']
                self.assertEqual(measurements['prompt']['utf8_bytes'], meta['input_bytes'])
                self.assertTrue(measurements['text_schema_copy'])
                self.assertIsNone(measurements['tokens'])
        self.init_git(['main'])
        manifest, code = self.run_case('compact-one')
        self.assertEqual(code, 0, manifest)
        self.assertTrue(manifest['branches'][0]['accepted'])
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')

    def test_production_rejects_lost_format_and_foreign_continuations(self):
        self.value['execution'] = {'structured_output_repair_attempts': 2}
        for scenario, reason in (
                ('compact-lost-format', 'COMPACTION_RECOVERY_STOP_UNCONFIRMED'),
                ('compact-stock-lost-format', 'COMPACTION_RECOVERY_STOP_UNCONFIRMED'),
                ('compact-changed-schema', 'COMPACTION_FORMAT_CHANGED'),
                ('compact-forged', 'CONTINUATION_FORM_UNSUPPORTED'),
                ('compact-foreign', 'COMPACTION_SUMMARY_IDENTITY')):
            manifest, code = self.run_case(scenario)
            self.assertEqual(code, 1, manifest)
            self.assertEqual(manifest['diagnostics'][0]['details']['code'], reason)
            if scenario in ('compact-lost-format', 'compact-stock-lost-format'):
                diagnostic = manifest['diagnostics'][0]
                self.assertEqual(diagnostic['failure_kind'], 'TRANSPORT_ERROR')
                invocation = json.loads((self.run_dir / 'revisions/001/study.logs/invocation.json').read_text())
                events = invocation['compaction']['events']
                self.assertIn('compaction_recovery_detected', [e['event'] for e in events])
                self.assertNotIn('compaction_recovery_sent', [e['event'] for e in events])
            self.assertFalse((self.run_dir / 'ARCHITECTURE.md').exists())
            self.assertEqual(manifest['metrics']['attempts'], 2)
            self.assertEqual(manifest['metrics']['usage']['coverage']['total_tokens'], 'partial')
            calls = self.recorded()
            self.assertTrue(any(c['path'].endswith('/abort') for c in calls))
            self.assertTrue(any(c['method'] == 'DELETE' for c in calls))

    def test_recovery_folder_git_review_and_comparison_keep_local_validation(self):
        for scenario, count in (('recovery-one', 1), ('recovery-two', 2)):
            with self.subTest(scenario=scenario):
                manifest, code = self.run_case(scenario)
                self.assertEqual(code, 0, manifest)
                self.assertTrue(manifest['accepted'])
                for name in ('study_invocation', 'review_invocation'):
                    meta = manifest[name]
                    self.assertTrue(meta['native_envelope_valid'])
                    self.assertTrue(meta['backend_result_valid'])
                    self.assertEqual(len(meta['recovery_ids']), count)
                    self.assertEqual(meta['retry_policy']['orchestrator_repair_attempts_performed'], 0)
                self.assertNotEqual(manifest['study_invocation']['session_id'], manifest['review_invocation']['session_id'])
        self.init_git(['main', 'other'])
        manifest, code = self.run_case('recovery-one')
        self.assertEqual(code, 0, manifest)
        self.assertTrue(all(branch['accepted'] for branch in manifest['branches']))
        self.assertTrue(manifest['comparison_invocation']['publication_complete'])
        self.assertEqual(len(manifest['comparison_invocation']['recovery_ids']), 1)
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')

    def test_recovery_does_not_consume_repairs_and_limit_spans_attempts(self):
        self.value['execution'] = {'structured_output_repair_attempts': 2}
        manifest, code = self.run_case('recovery-repair')
        self.assertEqual(code, 0, manifest)
        self.assertTrue(manifest['accepted'])
        meta = manifest['study_invocation']
        self.assertEqual(meta['retry_policy']['orchestrator_repair_attempts_performed'], 1)
        self.assertEqual(meta['compaction_recoveries_used'], 2)
        # Each attempt has its own session, while both share the recovery cap.
        self.assertTrue((Path(meta['artifact_directory']).parent / 'attempt-001/recovery-001-request.json').exists())
        self.assertTrue((Path(meta['artifact_directory']) / 'recovery-002-request.json').exists())
        manifest, code = self.run_case('recovery-repair-limit')
        self.assertEqual(code, 1, manifest)
        self.assertEqual(manifest['diagnostics'][0]['details']['code'], 'COMPACTION_RECOVERY_LIMIT_EXCEEDED')
        self.assertFalse((self.run_dir / 'ARCHITECTURE.md').exists())
        for pid in {c['server_pid'] for c in self.recorded()}:
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_interrupt_during_recovery_wait_cleans_up_without_a_second_post(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.value))
        result = subprocess.run([sys.executable, '-B', str(base.ROOT / 'explain.py'), '--config', str(path)],
            env=os.environ | self.env | {'AUDIT_FAKE_CASE': 'recovery-interrupt'},
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 130, result.stderr)
        calls = self.recorded()
        self.assertEqual(len(self.prompts()), 2)  # catalog and original study
        self.assertTrue(any(c['method'] == 'DELETE' for c in calls))
        for pid in {c['server_pid'] for c in calls}:
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_recovery_stop_failures_cleanup_owned_resources(self):
        self.value['execution'] = {'http_timeout_seconds': .4, 'stage_timeout_seconds': 5}
        for scenario, kind in (('recovery-drop', 'TRANSPORT_ERROR'),
                               ('recovery-stop-hang', 'TRANSPORT_ERROR'),
                               ('recovery-abort-hang', 'STAGE_TIMEOUT')):
            with self.subTest(scenario=scenario):
                before = len(self.prompts())
                manifest, code = self.run_case(scenario)
                self.assertEqual(code, 1, manifest)
                self.assertEqual(manifest['diagnostics'][0]['failure_kind'], kind, manifest['diagnostics'])
                self.assertEqual(len(self.prompts()) - before, 2)
                self.assertFalse((self.run_dir / 'ARCHITECTURE.md').exists())
                self.assertTrue(any(c['method'] == 'DELETE' for c in self.recorded()))
                for pid in {c['server_pid'] for c in self.recorded()}:
                    with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_final_summary_mismatch_missing_native_and_unfinished_tool_never_publish(self):
        for scenario in ('compact-summary-result', 'compact-mismatch', 'compact-no-native',
                         'compact-no-tool', 'compact-unfinished-tool'):
            with self.subTest(scenario=scenario):
                manifest, code = self.run_case(scenario)
                self.assertEqual(code, 1, manifest)
                self.assertFalse(manifest['accepted'])
                self.assertFalse((self.run_dir / 'ARCHITECTURE.md').exists())
                self.assertEqual(manifest['metrics']['attempts'], 2)

    def test_timeouts_and_backend_error_cleanup_during_compaction(self):
        for scenario, execution, kind in (
                ('compact-slow', {'stage_timeout_seconds': 3, 'idle_timeout_seconds': .5}, 'IDLE_TIMEOUT'),
                ('compact-active', {'stage_timeout_seconds': .9}, 'STAGE_TIMEOUT'),
                ('compact-error', {'stage_timeout_seconds': 5}, 'BACKEND_ERROR')):
            self.value['execution'] = execution
            manifest, code = self.run_case(scenario)
            self.assertEqual(code, 1, manifest)
            self.assertEqual(manifest['diagnostics'][0]['failure_kind'], kind)
            self.assertFalse(manifest['accepted'])
            self.assertEqual(manifest['metrics']['attempts'], 2)
            self.assertEqual(manifest['metrics']['usage']['coverage']['total_tokens'], 'partial')
            for pid in {c['server_pid'] for c in self.recorded()}:
                with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_interrupt_during_compaction_aborts_owned_session(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.value))
        for signal in ('SIGINT', 'SIGTERM'):
            result = subprocess.run([sys.executable, '-B', str(base.ROOT / 'explain.py'), '--config', str(path)],
                env=os.environ | self.env | {'AUDIT_FAKE_CASE': 'compact-interrupt', 'AUDIT_FAKE_SIGNAL': signal},
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 130, result.stderr)
            manifest = json.loads(Path(json.loads(result.stdout)['manifest']).read_text())
            self.assertFalse(manifest['accepted'])
            self.assertEqual(manifest['metrics']['attempts'], 2)
            self.assertEqual(manifest['metrics']['usage']['coverage']['total_tokens'], 'partial')
            for pid in {c['server_pid'] for c in self.recorded()}:
                with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_events_are_safe_and_hook_unavailability_is_explicit(self):
        stream = io.StringIO()
        reporter = Reporter(stdout=io.StringIO(), stderr=stream, progress=False, verbose=True)
        self.addCleanup(reporter.close)
        manifest, code = self.run_case('compact-one', reporter=reporter)
        self.assertEqual(code, 0, manifest)
        meta = manifest['study_invocation']['compaction']
        self.assertEqual(meta['summary_hook'], 'not_applied_unverified')
        events = [e['event'] for e in meta['events']]
        self.assertEqual(events.count('compaction_started'), 1)
        self.assertEqual(events.count('compaction_completed'), 1)
        self.assertEqual(events.count('session_continued'), 1)
        self.assertNotIn('Synthetic summary', stream.getvalue())
        self.assertNotIn('Original task', stream.getvalue())
        self.assertNotIn('Authorization', stream.getvalue())


class CompactionConfigurationTests(unittest.TestCase):
    def test_summary_model_is_not_used_as_the_stages_actual_model(self):
        runner = base.Runner.__new__(base.Runner)
        runner.reporter = SimpleNamespace(clock=lambda: 1)
        runner.metrics = Mock()
        runner.stage_context = lambda stage, context: {'stage': stage}
        metrics = measurement('xxx', None, 'provider/small')
        metrics['by_model'][0]['origin'] = 'compaction'
        meta = {'backend': 'xxx', 'model_requested': None, 'model_actual': None,
                'stage': 'study', 'invocation_id': 'test', 'metrics': metrics}
        runner.record_attempt_metrics(meta, {}, 0)
        self.assertIsNone(meta['model_actual'])
        self.assertEqual(meta['models_reported'], ['provider/small'])

    def test_overlay_preserves_user_compaction_and_hooks_without_installing_any(self):
        config = {'plugin': ['user-hook.js'], 'model': 'private/model', 'agent': {'compaction': {'model': 'p/small'}},
                  'compaction': {'auto': True, 'prune': False, 'reserved': 1234}, 'unrelated': {'keep': True}}
        env = {'OPENCODE_CONFIG_CONTENT': json.dumps(config), 'OPENCODE_CONFIG': '/user/profile'}
        name = prepare_environment(env, 'study')
        merged = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        del merged['agent'][name]
        self.assertEqual(merged, config)
        self.assertEqual(env['OPENCODE_CONFIG'], '/user/profile')
        self.assertEqual(config['plugin'], ['user-hook.js'])


if __name__ == '__main__':
    unittest.main()
