# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Both native adapters with explicitly synthetic local HTTP runtimes.

The test-only capability bypass proves no native OpenCode retry guarantee.
"""
import copy
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from contracts import ContractError, SCHEMAS, schema_diagnostics, validate_result
from execution import Budget, execution_settings
from explain import AuditError, Folder, Runner, atomic
import opencode
import xxx
from structured_output import blocked_comparison, required_unresolved
import test_opencode_http as http_fixtures
from test_explain import doc, review
from fixtures.ledger_response import response
from ledger import review_context
import test_xxx as xxx_fixtures


class NativeStructuredOutputTests(unittest.TestCase):
    setUp = http_fixtures.HTTPFixture.setUp
    close = http_fixtures.HTTPFixture.close
    config = http_fixtures.HTTPFixture.config

    def stage(self, backend, scenario, repairs=0, stage='study', settings=None):
        config = self.config()
        for agent in config['_agents'].values():
            agent['backend'] = backend
        config['execution'] = {'structured_output_repair_attempts': repairs, **(settings or {})}
        destination = self.root / f'case-{len(list(self.root.glob("case-*")))}' / (stage + '.logs')
        runner = Runner(config, destination.parent)
        runner.versions[backend + ':' + str(self.cli)] = '1.2.27'
        context = {'source_mode': 'folder', 'source_directory': str(self.source),
                   'source_fingerprint': Folder(self.source).snapshot()['source_fingerprint']}
        if stage == 'review':
            context = review_context(response(context), context)
        self.destination = destination
        self.last_runner = runner
        env = self.env | {'AUDIT_FAKE_BACKEND': backend, 'AUDIT_FAKE_CASE': scenario}
        # A real fixture subprocess, not an installed binary or paid provider.
        with patch('opencode.verify_native_retries'), patch.dict(os.environ, env):
            return runner.invoke(stage, context, destination)

    def prompts(self):
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return [c for c in calls if c['method'] == 'POST' and c['path'].endswith('/message')]

    def test_legacy_contract_never_triggers_invented_registry_repair(self):
        for backend in ('xxx', 'opencode'):
            for policy in ('strict', 'compromise'):
                config = self.config() | {'result_policy': policy}
                before = len(self.prompts())
                with self.subTest(backend=backend, policy=policy), patch.object(self, 'config', return_value=config):
                    if policy == 'strict':
                        with self.assertRaises(ContractError) as caught:
                            self.stage(backend, 'legacy', repairs=2)
                        self.assertIn('Expected the current evidence ledger structure', caught.exception.safe_message)
                    else:
                        data, meta = self.stage(backend, 'legacy', repairs=2)
                        self.assertIsNone(data)
                        self.assertNotIn('claims', meta['usable_material'])
                        self.assertFalse(meta['local_validation'])
                self.assertEqual(len(self.prompts()) - before, 1)

    def test_claim_counts_and_no_normalization_even_empty(self):
        for backend in ('xxx', 'opencode'):
            for scenario, count in (('claims-44', 44), ('claims-40', 40), ('claims-empty', 44)):
                with self.subTest(backend=backend, scenario=scenario), self.assertRaises(ContractError) as caught:
                    self.stage(backend, scenario, stage='review')
                exc = caught.exception
                self.assertEqual(exc.failure_kind, 'SCHEMA_ERROR')
                self.assertEqual(exc.details['total_violations'], count)
                self.assertEqual([v['path'] for v in exc.details['violations']], [f'$.claims[{i}]' for i in range(count)])
                self.assertTrue(all(v['missing_keys'] == [] and v['extra_key_count'] == 1 for v in exc.details['violations']))
                self.assertNotIn('claim_ids', json.dumps(exc.details))
                artifact = self.destination / 'attempt-001'
                extracted = json.loads((artifact / 'extracted.json').read_text())
                self.assertEqual(extracted['claims'][0]['claim_ids'], [] if scenario == 'claims-empty' else ['C-PRIVATE'])
                self.assertEqual(json.loads((artifact / 'response.json').read_text())['info']['structured'], extracted)
                self.assertEqual(json.loads((artifact / 'validation.json').read_text())['schema_diagnostics']['violations'][0]['extra_keys'], ['claim_ids'])
                self.assertFalse((self.destination.parent / 'review.json').exists())

    def test_explicit_repair_bounds_and_complete_validation(self):
        for backend in ('xxx', 'opencode'):
            for scenario, repairs, expected, count in (
                    ('repair-ok', 0, 'SCHEMA_ERROR', 1),
                    ('repair-ok', 1, None, 2),
                    ('repair-invalid', 1, 'SCHEMA_ERROR', 2),
                    ('repair-invalid', 2, 'SCHEMA_ERROR', 3),
                    ('repair-semantic', 1, 'IDENTITY_MISMATCH', 2),
                    ('wrong-identity', 2, 'IDENTITY_MISMATCH', 1),
                    ('foreign-request', 2, 'TRANSPORT_ERROR', 1),
                    ('backend-error', 2, 'BACKEND_ERROR', 1),
                    ('prose-only', 2, 'INCOMPLETE_OUTPUT', 1)):
                with self.subTest(backend=backend, scenario=scenario, repairs=repairs):
                    start = len(self.prompts())
                    if expected:
                        with self.assertRaises(ContractError) as caught:
                            self.stage(backend, scenario, repairs)
                        self.assertEqual(caught.exception.failure_kind, expected)
                    else:
                        data, meta = self.stage(backend, scenario, repairs)
                        self.assertTrue(meta['local_validation'])
                        self.assertEqual(data['completion_status'], 'COMPLETE')
                    prompts = self.prompts()[start:]
                    self.assertEqual(len(prompts), count)
                    self.assertEqual((self.destination.parent / 'study.json').exists(), expected is None)
                    meta = json.loads((self.destination / 'invocation.json').read_text())
                    policy = meta['retry_policy']
                    self.assertEqual(policy['orchestrator_repair_attempts_configured'], repairs)
                    self.assertEqual(policy['orchestrator_repair_attempts_performed'], count - 1)
                    self.assertEqual(policy['format_retries_requested'], 0 if backend == 'xxx' else 2)
                    self.assertEqual(len(list(self.destination.glob('attempt-*'))), count)
                    if count == 2:
                        self.assertNotEqual(prompts[0]['path'], prompts[1]['path'])
                        self.assertNotEqual(prompts[0]['body']['agent'], prompts[1]['body']['agent'])
                        self.assertNotEqual(prompts[1]['cwd'], str(self.source))
                        self.assertEqual(prompts[1]['permissions'], {'*': 'deny', 'StructuredOutput': 'allow'})
                        self.assertEqual(prompts[0]['body']['format'], prompts[1]['body']['format'])
                        self.assertEqual(prompts[1]['body']['model'],
                                         {'providerID': 'fixture', 'modelID': 'configured-model'})
                        self.assertIn('extra_private_key', prompts[1]['body']['parts'][0]['text'])
                        self.assertNotIn('single_prompt', policy['mode'])
                        first = json.loads((self.destination / 'attempt-001/invocation.json').read_text())
                        self.assertEqual(first['error']['failure_kind'], 'SCHEMA_ERROR')
                        self.assertFalse(first['local_validation'])

    def test_stage_budget_is_shared_with_repair(self):
        original = opencode.Server.start
        for backend in ('xxx', 'opencode'):
            deadlines = []
            def record(server):
                deadlines.append(server.budget.deadline)
                return original(server)
            start = time.monotonic()
            with patch.object(opencode.Server, 'start', record), self.assertRaises(ContractError) as caught:
                self.stage(backend, 'repair-timeout', 1, settings={'stage_timeout_seconds': 1.5})
            self.assertEqual(caught.exception.failure_kind, 'STAGE_TIMEOUT')
            self.assertEqual(len(deadlines), 2)
            self.assertEqual(deadlines[0], deadlines[1])
            self.assertLess(time.monotonic() - start, 2.5)
            self.assertFalse((self.destination.parent / 'study.json').exists())

    def test_source_change_is_not_repaired_or_published(self):
        for backend in ('xxx', 'opencode'):
            (self.source / 'app.py').write_text('print(1)')
            start = len(self.prompts())
            with self.assertRaises(AuditError):
                self.stage(backend, 'source-change', 2)
            self.assertEqual(len(self.prompts()) - start, 1)
            self.assertEqual((self.source / 'app.py').read_text(), 'unexpected fixture mutation\n')
            self.assertFalse((self.destination.parent / 'study.json').exists())

    def test_repair_cannot_improve_verdict_or_replace_evidence(self):
        for backend in ('xxx', 'opencode'):
            for scenario in ('repair-verdict', 'repair-evidence'):
                start = len(self.prompts())
                with self.assertRaises(ContractError) as caught:
                    self.stage(backend, scenario, 2, stage='review')
                self.assertEqual(caught.exception.failure_kind, 'SEMANTIC_ERROR')
                self.assertEqual(len(self.prompts()) - start, 2)
                self.assertFalse((self.destination.parent / 'review.json').exists())

    def test_cleanup_failure_keeps_schema_error_and_prevents_repair(self):
        original = opencode.Server.close
        def fail(server):
            original(server)
            raise OSError('synthetic cleanup failure')
        for backend in ('xxx', 'opencode'):
            start = len(self.prompts())
            with patch.object(opencode.Server, 'close', fail), self.assertRaises(ContractError) as caught:
                self.stage(backend, 'repair-ok', 1)
            self.assertEqual(caught.exception.failure_kind, 'SCHEMA_ERROR')
            self.assertEqual(len(self.prompts()) - start, 1)
            self.assertFalse((self.destination.parent / 'study.json').exists())

    def test_doc_slower_than_old_five_seconds_then_exactly_one_model_request(self):
        self.env['AUDIT_FAKE_DOC_DELAY'] = '5.1'
        for backend in ('xxx', 'opencode'):
            start = len(self.prompts())
            data, meta = self.stage(backend, 'doc-delay')
            self.assertEqual(data['completion_status'], 'COMPLETE')
            self.assertEqual(len(self.prompts()) - start, 1)
            responses = [r for r in meta['http_responses'] if r['operation'] == 'GET /doc']
            self.assertEqual(len(responses), 3 if backend == 'xxx' else 1)
            self.assertTrue(all(r['limit_seconds'] == 30 and r['elapsed_seconds'] > 5 for r in responses))

    def test_socket_timeout_has_same_category_and_context_as_watchdog(self):
        from types import SimpleNamespace
        for backend, cls in (('xxx', xxx.Server), ('opencode', opencode.Server)):
            meta = {}
            server = cls(str(self.cli), self.source, self.env, self.artifacts, Budget(5), atomic, meta)
            request = SimpleNamespace(number=1, body=b'{"partial":', status=200,
                operation='GET /doc', limit=30, started=time.monotonic(),
                budget_source='http_operation', error=TimeoutError('synthetic socket timeout'))
            with self.assertRaises(ContractError) as caught:
                server.finish(request)
            self.assertEqual(caught.exception.failure_kind, 'STAGE_TIMEOUT')
            self.assertEqual(caught.exception.details['operation'], 'GET /doc')
            self.assertEqual(caught.exception.details['limit_seconds'], 30)
            self.assertEqual(caught.exception.details['budget_source'], 'http_operation')
            self.assertEqual((self.artifacts / 'response-001.json').read_bytes(), request.body)

    def test_doc_limits_and_absolute_watchdog_for_both_backends(self):
        for backend, cls in (('xxx', xxx.Server), ('opencode', opencode.Server)):
            for scenario in ('doc-delay', 'doc-hang', 'doc-drip-body', 'doc-drip-headers'):
                with self.subTest(backend=backend, scenario=scenario):
                    artifacts = self.root / (backend + scenario); artifacts.mkdir()
                    env = self.env | {'AUDIT_FAKE_BACKEND': backend, 'AUDIT_FAKE_CASE': scenario,
                                      'AUDIT_FAKE_DOC_DELAY': '.15' if scenario == 'doc-delay' else '2'}
                    settings = execution_settings({'http_timeout_seconds': .1, 'api_doc_timeout_seconds': .4})
                    meta = {}
                    server = cls(str(self.cli), self.source, env, artifacts, Budget(5), atomic, meta, settings)
                    start = time.monotonic()
                    try:
                        server.start()
                        if scenario == 'doc-delay':
                            server.verify_api()
                        else:
                            with self.assertRaises(ContractError) as caught:
                                server.verify_api()
                            self.assertEqual(caught.exception.failure_kind, 'STAGE_TIMEOUT')
                            detail = caught.exception.details
                            self.assertEqual(detail['operation'], 'GET /doc')
                            self.assertEqual(detail['limit_seconds'], .4)
                            self.assertEqual(detail['budget_source'], 'http_operation')
                            self.assertGreaterEqual(detail['elapsed_seconds'], .38)
                            partial = list(artifacts.glob('*.partial'))
                            if scenario == 'doc-drip-body':
                                self.assertTrue(any(p.stat().st_size for p in partial))
                    finally:
                        server.close()
                    self.assertLess(time.monotonic() - start, 2)
                    self.assertEqual(meta['cleanup_errors'], [])
                    with self.assertRaises(ProcessLookupError):
                        os.kill(server.process.pid, 0)
            self.assertGreaterEqual(cls.preflight_seconds(settings),
                                    opencode.STARTUP_SECONDS + cls.api_doc_checks * settings['api_doc_timeout_seconds'])


class ComparisonAcceptanceTests(unittest.TestCase):
    def context(self):
        return {'baseline_branch': 'master', 'baseline_commit': 'abc',
                'requested_branches': ['master', 'master_bnt3_glm', 'master_bnt3_deepseek'],
                'branches': [{'branch': b, 'study': doc(b, 'abc'), 'review': None, 'accepted': True}
                             for b in ['master', 'master_bnt3_glm', 'master_bnt3_deepseek']]}

    def test_model_cannot_hide_two_unaccepted_reviews_or_trust_accepted_flag(self):
        context = self.context()
        data = blocked_comparison(context)
        validate_result('compare', data, context)
        data['unresolved_branches'] = ['master_bnt3_deepseek']
        with self.assertRaises(ContractError) as caught:
            validate_result('compare', data, context)
        self.assertEqual(caught.exception.failure_kind, 'SEMANTIC_ERROR')

    def test_unaccepted_baseline_is_required_even_with_other_accepted_inputs(self):
        context = self.context()
        for item in context['branches'][1:]:
            item['review'] = review(item['branch'], 'abc')
        # Raw wire fixtures have no orchestrator checks/publication record and
        # cannot be promoted merely by attaching a review response.
        self.assertEqual(required_unresolved(context), context['requested_branches'])
        data = blocked_comparison(context)
        data['unresolved_branches'] = []
        with self.assertRaises(ContractError):
            validate_result('compare', data, context)

    def test_diagnostics_are_bounded_counted_and_do_not_expose_extra_keys(self):
        data = review('master', 'abc')
        data['claims'] = [dict(data['claims'][0], secret_key=['private']) for _ in range(150)]
        original = copy.deepcopy(data)
        public = schema_diagnostics(data, SCHEMAS['review'])
        self.assertEqual(public['total_violations'], 150)
        self.assertEqual(len(public['violations']), 100)
        self.assertTrue(public['truncated'])
        self.assertNotIn('secret_key', json.dumps(public))
        self.assertNotIn('private', json.dumps(public))
        self.assertEqual(data, original)


class NativeGitComparisonTests(unittest.TestCase):
    setUp = xxx_fixtures.XXXTests.setUp
    git = xxx_fixtures.XXXTests.git
    init_git = xxx_fixtures.XXXTests.init_git
    run_case = xxx_fixtures.XXXTests.run_case
    recorded = xxx_fixtures.XXXTests.recorded
    prompts = xxx_fixtures.XXXTests.prompts

    def test_no_accepted_inputs_never_call_compare_and_keep_failure_exit_code(self):
        self.init_git(['main', 'other'])
        original = self.git('rev-parse', 'HEAD')
        for backend in ('xxx', 'opencode'):
            self.value['agent']['backend'] = backend
            self.env['AUDIT_FAKE_BACKEND'] = backend
            for scenario, expected_code in (('claims-44', 1), ('schema-error', 1), ('partial-review', 2)):
                with self.subTest(backend=backend, scenario=scenario), patch('opencode.verify_native_retries'):
                    start = len(self.prompts())
                    manifest, code = self.run_case(scenario)
                self.assertEqual(code, expected_code, manifest)
                self.assertFalse(any(b['accepted'] for b in manifest['branches']))
                self.assertEqual(len(self.prompts()) - start, 2 if scenario == 'schema-error' else 4)
                result = manifest['comparison']
                self.assertEqual(result['completion_status'], 'BLOCKED')
                self.assertEqual(result['compared_branches'], ['other'])
                self.assertEqual(result['unresolved_branches'], ['main', 'other'])
                self.assertEqual(result['differences'], [])
                self.assertEqual(len(result['limitations']), 2)
                meta = manifest['comparison_invocation']
                self.assertEqual(meta['generated_by'], 'orchestrator')
                self.assertFalse(set(meta) & {'session_id', 'request_id', 'message_id', 'model_actual'})
                inputs = json.loads((self.run_dir / 'comparison/inputs.json').read_text())
                self.assertEqual(inputs['required_unresolved_branches'], ['main', 'other'])
                self.assertTrue(all(not b['accepted'] for b in inputs['branches']))
                wire = {k: result[k] for k in SCHEMAS['compare']['properties']}
                validate_result('compare', wire, inputs)
                from presentation import render_stage
                self.assertEqual((self.run_dir / 'comparison/BRANCH_COMPARISON.md').read_text(), render_stage('compare', result, 'Russian'))
                self.assertEqual((self.run_dir / 'comparison/compare.original.md').read_text(), result['report_markdown'])
                self.assertEqual(self.git('rev-parse', 'HEAD'), original)
                self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')
                self.assertEqual(self.git('status', '--porcelain'), '')

    def test_partial_acceptance_passes_authoritative_baseline_status(self):
        self.init_git(['main', 'other'])
        for backend in ('xxx', 'opencode'):
            self.value['agent']['backend'] = backend
            self.env['AUDIT_FAKE_BACKEND'] = backend
            with patch('opencode.verify_native_retries'):
                manifest, code = self.run_case('fail-main-study')
            self.assertEqual(code, 1, manifest)
            inputs = json.loads((self.run_dir / 'comparison/inputs.json').read_text())
            self.assertEqual(inputs['required_unresolved_branches'], ['main'])
            self.assertEqual([b['accepted'] for b in inputs['branches']], [False, True])
            self.assertEqual(manifest['comparison']['unresolved_branches'], ['main'])


if __name__ == '__main__':
    unittest.main()
