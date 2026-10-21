# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Small offline experiments; no credentials or real model subprocesses."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fixtures.cli_response import cli_result
from fixtures.ledger_response import prompt_context, response

from src.runtime.errors import AuditError
from src.analysis.folder import Folder
from src.runtime.runner import Runner
from src.runtime.artifacts import save_json
from src.runtime.process import process as real_process
from src.analysis.coverage_plan import build_coverage_plan
from src.runtime.context_comparison import apply_assessments, frozen_inputs, run_comparison
from test_folder import FolderFixture
import test_explain
from src.runtime.execution import execution_settings
from src.analysis.source_context import source_context_settings


class ContextComparisonTests(FolderFixture):
    def baseline(self):
        config = self.config()
        folder = Folder(self.source, source_filter=config['source_filter'])
        inventory = folder.snapshot()
        context = {'source_directory': str(self.source), 'source_fingerprint': inventory['source_fingerprint']}
        coverage = build_coverage_plan(inventory, context)
        baseline = self.base / 'baseline'
        baseline.mkdir()
        for name, value in [('source.inventory.json', inventory), ('coverage.plan.json', coverage)]:
            save_json(baseline / name, value)
        facts = [{'id': 'F-001', 'statement': 'The function returns 42.', 'evidence': [
            {'id': 'E-001', 'source_id': 'source-001', 'path': 'app.py', 'start_line': 1, 'end_line': 1,
             'quote': 'return 42'}]}]
        return config, baseline, facts

    def test_without_live_flag_never_checks_or_invokes_cli(self):
        config, baseline, facts = self.baseline()
        with patch.object(Runner, 'check_cli', side_effect=AssertionError('CLI invoked')), \
                patch.object(Runner, 'invoke', side_effect=AssertionError('Model invoked')):
            report = run_comparison(config, baseline, facts, self.base / 'planned')
        self.assertEqual(report['status'], 'planned')
        self.assertEqual(report['attempts'], [])
        self.assertTrue((self.base / 'planned/comparison.md').is_file())

    def test_live_variants_reuse_source_assignments_settings_and_fact_set(self):
        config, baseline, facts = self.baseline()
        contexts, options = [], []
        def invoke(runner, stage, context, destination):
            contexts.append(copy.deepcopy(context))
            options.append((runner.session.cfg['source_context']['enabled'], runner.session.execution['read_loop_guard']))
            result = {'completion_status': 'COMPLETE', 'program_checks': {'evidence_counts': {'RESOLVED': 1}}}
            return result, {'publication_complete': True, 'duration_seconds': 1.5, 'model_actual': 'fixed-model'}
        with patch.object(Runner, 'check_cli', return_value={'fixture': {'version': '1.0'}}), \
                patch.object(Runner, 'invoke', invoke):
            report = run_comparison(config, baseline, facts, self.base / 'live', live=True, repetitions=2)
        self.assertEqual(options, [(False, False), (False, True), (True, True)] * 2)
        self.assertTrue(all(context == contexts[0] for context in contexts))
        self.assertEqual(len(report['fact_source_checks']), 1)
        self.assertEqual([r['outcome'] for r in report['attempts']], ['successful'] * 6)
        self.assertEqual(report['attempts'][0]['fact_assessment']['status'], 'unavailable')
        self.assertEqual(json.loads((self.base / 'live/coverage.plan.json').read_text()),
                         json.loads((baseline / 'coverage.plan.json').read_text()))

    def test_comparison_runs_normal_prompt_validation_and_publication(self):
        config, baseline, facts = self.baseline()
        prompts = []
        def process(command, cwd, env, payload, **kwargs):
            context = prompt_context(payload)
            prompts.append(context)
            return cli_result(command, response(context))
        with patch.object(Runner, 'check_cli', return_value={'version': '1'}), \
                patch('src.runtime.process.process', side_effect=process):
            report = run_comparison(config, baseline, facts, self.base / 'pipeline', live=True)
        self.assertEqual([p['stage'] for p in prompts], ['study'] * 6)
        self.assertEqual([p['phase'] for p in prompts], ['collect', 'write'] * 3)
        self.assertEqual(['source_context' in p for p in prompts], [False, False, False, False, True, False])
        self.assertEqual([r['outcome'] for r in report['attempts']], ['successful'] * 3)
        self.assertTrue(all(Path(r['result_path']).is_file() for r in report['attempts']))

    def test_changed_source_aborts_before_model(self):
        config, baseline, facts = self.baseline()
        (self.source / 'app.py').write_text('changed\n')
        with patch.object(Runner, 'invoke', side_effect=AssertionError('Model invoked')), \
                self.assertRaisesRegex(ValueError, 'inventory changed'):
            run_comparison(config, baseline, facts, self.base / 'changed', live=True)

    def test_invalid_fact_reference_aborts_before_model(self):
        config, baseline, facts = self.baseline()
        facts[0]['evidence'][0]['start_line'] = 500
        with patch.object(Runner, 'invoke', side_effect=AssertionError('Model invoked')), \
                self.assertRaisesRegex(ValueError, 'unresolved source evidence'):
            run_comparison(config, baseline, facts, self.base / 'invalid-fact', live=True)

    def test_version_change_aborts_before_next_variant(self):
        config, baseline, facts = self.baseline()
        with patch.object(Runner, 'check_cli', side_effect=[{'version': '1'}, {'version': '2'}]), \
                patch.object(Runner, 'invoke', side_effect=AssertionError('Model invoked')), \
                self.assertRaisesRegex(ValueError, 'CLI version'):
            run_comparison(config, baseline, facts, self.base / 'version', live=True)

    def test_preregistered_facts_cannot_change_between_variants(self):
        config, baseline, facts = self.baseline()
        output = self.base / 'changed-facts'
        calls = []
        def invoke(runner, stage, context, destination):
            calls.append(destination)
            save_json(output / 'facts.json', [])
            return {'completion_status': 'COMPLETE'}, {'publication_complete': True, 'duration_seconds': 1}
        with patch.object(Runner, 'check_cli', return_value={'version': '1'}), \
                patch.object(Runner, 'invoke', invoke), \
                self.assertRaisesRegex(ValueError, 'Frozen experiment file changed'):
            run_comparison(config, baseline, facts, output, live=True)
        self.assertEqual(len(calls), 1)

    def test_no_progress_is_separate_from_success_and_has_no_repeat(self):
        config, baseline, facts = self.baseline()
        calls = []
        def invoke(runner, stage, context, destination):
            calls.append(destination)
            save_json(destination / 'invocation.json', {'error': {'code': 'NO_PROGRESS'}, 'duration_seconds': 125})
            raise AuditError('Read loop stopped.', code='NO_PROGRESS', failure_layer='execution')
        with patch.object(Runner, 'check_cli', return_value={'version': '1'}), patch.object(Runner, 'invoke', invoke):
            report = run_comparison(config, baseline, facts, self.base / 'stopped', live=True)
        self.assertEqual(len(calls), 3)
        self.assertEqual([r['outcome'] for r in report['attempts']], ['interrupted'] * 3)

    def test_changed_coverage_and_inventory_are_rejected(self):
        config, baseline, _ = self.baseline()
        coverage = json.loads((baseline / 'coverage.plan.json').read_text())
        coverage['areas'][0]['file_paths'] = []
        save_json(baseline / 'coverage.plan.json', coverage)
        with self.assertRaises(ValueError):
            frozen_inputs(baseline, config)

    def test_inventory_must_match_frozen_coverage(self):
        config, baseline, _ = self.baseline()
        inventory = json.loads((baseline / 'source.inventory.json').read_text())
        inventory['entries'][1]['size'] += 1
        save_json(baseline / 'source.inventory.json', inventory)
        with self.assertRaisesRegex(ValueError, 'inventories differ'):
            frozen_inputs(baseline, config)

    def test_old_scope_format_is_rejected_without_changing_baseline(self):
        config, baseline, _ = self.baseline()
        from src.analysis.evidence import canonical, sha
        coverage = json.loads((baseline / 'coverage.plan.json').read_text())
        coverage.update(origin='AGENT', catalog_status='COMPLETE', limitations=[])
        coverage['plan_sha256'] = sha(canonical({k: v for k, v in coverage.items() if k != 'plan_sha256'}))
        save_json(baseline / 'coverage.plan.json', coverage)
        before = {p.name: p.read_bytes() for p in baseline.iterdir()}
        with self.assertRaises(ValueError):
            frozen_inputs(baseline, config)
        self.assertEqual(before, {p.name: p.read_bytes() for p in baseline.iterdir()})

    def test_fact_assessment_is_explicit_and_covers_every_fact(self):
        report = {'facts': [{'id': 'F-001'}], 'attempts': [{'variant': 'context', 'repetition': 1}]}
        assessment = [{'variant': 'context', 'repetition': 1,
                       'facts': [{'id': 'F-001', 'status': 'supported', 'claim_ids': ['C-001']}]}]
        assessed = apply_assessments(report, assessment)
        self.assertEqual(assessed['attempts'][0]['fact_assessment']['counts'], {'supported': 1})
        self.assertNotIn('fact_assessment', report['attempts'][0])
        assessment[0]['facts'] = []
        with self.assertRaises(ValueError):
            apply_assessments(report, assessment)


class GitContextComparisonTests(unittest.TestCase):
    def test_frozen_git_study_uses_expected_commit_and_restores_checkout(self):
        fixture = test_explain.RepoFixture(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        config = fixture.config()
        config['execution'] = execution_settings(config['execution'])
        config['source_context'] = source_context_settings()
        baseline = fixture.base / 'frozen'
        setup = Runner(config, baseline)
        try:
            pins = setup.session.repo.preflight(['test01'])
            setup.session.repo.checkout(pins['test01'])
            inventory, _ = setup.session.source_inventory()
            context = {'branch': 'test01', 'source_commit': pins['test01'], 'repository': str(fixture.repo_path)}
            coverage = build_coverage_plan(inventory, context)
            for name, value in [('source.inventory.json', inventory), ('coverage.plan.json', coverage)]:
                save_json(baseline / name, value)
        finally:
            setup.session.repo.restore()
            setup.session.repo.close()
        facts = [{'id': 'F-001', 'statement': 'The function returns the branch label.', 'evidence': [
            {'id': 'E-001', 'source_id': 'source-001', 'path': 'app.py', 'start_line': 1, 'end_line': 1,
             'quote': 'test01'}]}]
        seen = []
        def process(command, cwd, env, payload=b'', **kwargs):
            if Path(command[0]).name == 'git':
                return real_process(command, cwd, env, payload, **kwargs)
            context = prompt_context(payload)
            seen.append(fixture.git('rev-parse', 'HEAD').strip())
            self.assertEqual(context['source_commit'], pins['test01'])
            return cli_result(command, response(context))
        with patch.object(Runner, 'check_cli', return_value={'version': '1'}), \
                patch('src.runtime.process.process', side_effect=process):
            report = run_comparison(config, baseline, facts, fixture.base / 'comparison', live=True)
        self.assertEqual(seen, [pins['test01']] * 6)
        self.assertEqual([r['outcome'] for r in report['attempts']], ['successful'] * 3)
        self.assertTrue(report['restoration']['restored'])
        self.assertEqual(fixture.git('branch', '--show-current').strip(), 'master')
        self.assertEqual(fixture.git('rev-parse', 'HEAD').strip(), fixture.master)
