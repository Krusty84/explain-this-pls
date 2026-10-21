# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Quote warnings must preserve publication, source safety and strict quality."""
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.runtime.errors import AuditError
from src.runtime.runner import Runner
from fixtures.cli_response import cli_result
from fixtures.ledger_response import prompt_context, response
from src.analysis.evidence import canonical, sha
from src.analysis.ledger import continuation_allowed
from src.contracts.contracts import accepted, processing_satisfied, workflow_satisfied
from src.reports.document_rendering import validate_materialized
from src.runtime.reporting import Reporter
from src.runtime.execution import response_error
import test_folder
from fixtures.study_runner import run_case
from fixtures.architecture_checks import assert_architecture
import test_explain as git_fixtures


class CitationPipelineTests(test_folder.FolderFixture):
    run_case = run_case

    @staticmethod
    def mismatch(runner, context, data):
        if context['stage'] == 'study':
            data['evidence'][0]['quote'] = 'private unmatched source text'

    def test_codex_continue_study_and_review_with_warnings(self):
        backend = 'codex'
        with self.subTest(backend=backend):
            reporter = Reporter(stdout=io.StringIO(), stderr=io.StringIO(), progress=False)
            self.addCleanup(reporter.close)
            manifest, code = self.run_case(backend=backend, policy='compromise', review=True,
                                         change=self.mismatch, reporter=reporter)
            self.assertEqual(code, 0, manifest.get('diagnostics'))
            self.assertEqual(manifest['status'], 'COMPLETE_WITH_WARNINGS')
            self.assertEqual([c['context']['stage'] for c in self.calls],
                             ['study', 'review'])
            self.assertTrue(processing_satisfied(manifest))
            self.assertFalse(workflow_satisfied(manifest))
            self.assertFalse(accepted(manifest))
            checks = manifest['study']['program_checks']
            self.assertFalse(checks['policy_satisfied'])
            self.assertTrue(checks['continuation_allowed'])
            self.assertEqual(checks['evidence_counts']['QUOTE_MISMATCH'], 1)
            warnings = checks['citation_warnings']
            self.assertEqual(len(warnings), 1)
            self.assertTrue(warnings[0]['claim_ids'])
            self.assertEqual(self.calls[-1]['context']['architecture_document']['program_checks']['citation_warnings'], warnings)
            document = manifest['study']
            validate_materialized(document)
            architecture = (self.runner.session.run_dir / 'ARCHITECTURE.md').read_text()
            assert_architecture(self, self.runner.session.run_dir, document,
                                manifest['study_invocation'], self.runner.session.cfg['output_language'])
            self.assertGreater(len(architecture), len(document['report_markdown']))
            evidence_id = warnings[0]['evidence_id'].split(':', 1)[1]
            self.assertIn(f'[{evidence_id}](#source-{evidence_id.lower()})', architecture)
            self.assertIn('QUOTE_MISMATCH', architecture)
            self.assertNotIn('private unmatched source text', reporter.stderr.getvalue())

    def test_strict_keeps_unmatched_quotes_without_allowing_continuation(self):
        manifest, code = self.run_case(policy='strict', change=self.mismatch)
        self.assertNotEqual(code, 0)
        self.assertFalse(manifest['processing_completed'])
        checks = manifest['study']['program_checks']
        self.assertFalse(checks['policy_satisfied'])
        self.assertFalse(continuation_allowed(checks))
        self.assertEqual(checks['evidence_counts']['QUOTE_MISMATCH'], 1)

    def test_warning_does_not_hide_bad_range_or_missing_source(self):
        for kind in ('range', 'source'):
            with self.subTest(kind=kind):
                def change(runner, context, data):
                    self.mismatch(runner, context, data)
                    if context['stage'] != 'study':
                        return
                    if kind == 'range':
                        data['evidence'][1]['end_line'] = 999
                    else:
                        data['evidence'][1]['source_id'] = 'source-999'
                manifest, code = self.run_case(policy='compromise', change=change)
                self.assertNotEqual(code, 0)
                self.assertFalse(manifest['processing_completed'])

    def test_direct_study_with_quote_warning_still_publishes(self):
        def change(runner, context, data):
            if context['stage'] == 'study':
                data['evidence'][0]['quote'] = 'unmatched text'
        manifest, code = self.run_case(large=False, policy='compromise', change=change)
        self.assertEqual(code, 0, manifest.get('diagnostics'))
        self.assertEqual(manifest['status'], 'COMPLETE_WITH_WARNINGS')
        self.assertFalse(manifest['study']['program_checks']['policy_satisfied'])
        self.assertTrue((self.runner.session.run_dir / 'ARCHITECTURE.md').exists())
        meta = manifest['study_invocation']
        attempt = Path(meta['artifact_directory'])
        details = json.loads((attempt / 'quote-diagnostics.json').read_text())
        self.assertEqual(details[0]['action'], 'UNRESOLVED')
        self.assertIn('-unmatched text', details[0]['diff'])
        self.assertIn('study.logs/attempt-001/quote-diagnostics.json', meta['attempt_hashes'])

    def test_warning_cannot_hide_execution_or_cleanup_failure(self):
        for kind in ('NO_PROGRESS', 'STAGE_TIMEOUT', 'cleanup'):
            with self.subTest(kind=kind):
                def change(runner, context, data):
                    self.mismatch(runner, context, data)
                    if context['stage'] == 'study':
                        if kind == 'NO_PROGRESS':
                            raise AuditError('Confirmed loop.', code=kind, failure_kind=kind, failure_layer='execution')
                        if kind == 'STAGE_TIMEOUT':
                            raise response_error(kind, 'execution', 'Execution time budget exceeded.')
                        raise AuditError('Cleanup failed.', failure_layer='cleanup')
                manifest, code = self.run_case(policy='compromise', change=change)
                self.assertNotEqual(code, 0)
                self.assertFalse(manifest['processing_completed'])
                self.assertFalse(manifest['processing_completed'])
                self.assertEqual([c['context']['stage'] for c in self.calls],
                                 ['study'])

    def test_correction_preserves_original_and_materialization_chain(self):
        (self.source / 'app.py').write_text('def main():\n    return 42\n# end\n')
        self.value.update(result_policy='compromise', execution={'review_enabled': False})
        for kind in ('range', 'indent'):
            with self.subTest(kind=kind):
                runner = Runner(self.config(), self.base / ('correct-' + kind))
                inventory, _ = runner.session.source_inventory()
                context = dict(source_directory=str(self.source), source_fingerprint=inventory['source_fingerprint'])
                calls = []
                def answer(command, cwd, env, payload, **kwargs):
                    data = response(prompt_context(payload))
                    data['evidence'][0].update(start_line=1 if kind == 'range' else 2,
                        end_line=1 if kind == 'range' else 2,
                        quote='def main():\n    return 42' if kind == 'range' else '        return 42')
                    data['evidence'][0]['id'] = 'E-1'
                    calls.append(copy.deepcopy(data))
                    return cli_result(command, data)
                with patch('src.runtime.process.process', side_effect=answer):
                    data, meta = runner.invoke('study', context, runner.session.run_dir / 'study.logs')
                self.assertEqual(len(calls), 1)
                self.assertTrue(data['program_checks']['policy_satisfied'])
                self.assertEqual(data['program_checks']['citation_warnings'][0]['action'], 'CORRECTED')
                self.assertEqual(data['program_checks']['citation_warnings'][0]['evidence_id'], 'study:E-001')
                attempt = Path(meta['artifact_directory'])
                self.assertEqual(json.loads((attempt / 'extracted.json').read_text()), calls[0])
                original = json.loads((attempt / 'expanded.json').read_text())
                corrected = json.loads((attempt / 'quote-corrected.json').read_text())
                journal = json.loads((attempt / 'quote-corrections.json').read_text())
                normalization = json.loads((attempt / 'normalization.json').read_text())
                self.assertEqual(journal['input_sha256'], sha(canonical(original)))
                self.assertEqual(journal['output_sha256'], sha(canonical(corrected)))
                self.assertEqual(normalization['input_sha256'], journal['output_sha256'])
                self.assertNotEqual(normalization['normalized_sha256'], journal['output_sha256'])
                self.assertEqual(data['materialization_provenance']['normalized_sha256'], normalization['normalized_sha256'])
                self.assertIn('study.logs/attempt-001/quote-corrections.json', meta['attempt_hashes'])
                validate_materialized(data)


class CitationGitPipelineTests(unittest.TestCase):
    setUp = git_fixtures.RepoFixture.setUp
    tearDown = git_fixtures.RepoFixture.tearDown
    git = git_fixtures.RepoFixture.git

    def test_quote_warnings_allow_unverified_comparison_and_restore_git(self):
        for review in (False, True):
            with self.subTest(review=review):
                config = git_fixtures.RepoFixture.config(self)
                config.update(result_policy='compromise')
                config['git_mode']['branches'] = ['master', 'test01']
                config['execution']['review_enabled'] = review
                runner = Runner(config, self.base / ('citation-compare-' + str(review)))
                calls = []

                def answer(command, cwd, env, payload, **kwargs):
                    context = prompt_context(payload)
                    calls.append(context)
                    data = response(context)
                    if context['stage'] == 'study' and context['branch'] == 'master':
                        data['evidence'][0]['quote'] = 'private unmatched text'
                    elif context['stage'] == 'compare':
                        refs = []
                        for branch in context['branches']:
                            doc = branch['study']
                            refs.append(dict(branch=branch['branch'], artifact='study',
                                revision_id=doc['revision_id'], claim_id=doc['claims'][0]['id'],
                                review_target_id=doc['review_plan']['review_target_id']))
                        data['differences'] = [dict(id='D-001', branch='test01', category='Source behavior',
                            classification='REPORTED_UNVERIFIED', baseline_statement='Reported baseline behavior.',
                            branch_statement='Reported branch behavior.', evidence_refs=refs,
                            explanation='The baseline citation needs manual verification.')]
                    return cli_result(command, data)

                with patch.object(runner, 'check_cli', return_value={}), patch('src.runtime.process.process', side_effect=answer):
                    manifest, code = runner.run()
                self.assertEqual((code, manifest['status']), (0, 'COMPLETE_WITH_WARNINGS'), manifest.get('diagnostics'))
                self.assertEqual(calls[-1]['stage'], 'compare')
                self.assertEqual(calls[-1]['required_unresolved_branches'], [])
                checks = manifest['comparison']['program_checks']
                self.assertFalse(checks['policy_satisfied'])
                self.assertTrue(checks['continuation_allowed'])
                self.assertEqual(checks['citation_warnings'][0]['branch'], 'master')
                self.assertFalse(manifest['workflow_satisfied'])
                self.assertTrue(manifest['processing_completed'])
                self.assertEqual(self.repo.symbolic(), 'master')
                self.assertEqual(self.repo.head(), self.master)
                for branch in manifest['branches']:
                    assert_architecture(self, runner.session.run_dir / branch['directory'], branch['study'],
                                        branch['study_invocation'], config['output_language'])

