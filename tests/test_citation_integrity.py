# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Citation correction sidecars and published source references remain immutable."""
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.runtime.errors import AuditError
from src.runtime.runner import Runner
from src.runtime.audit import run_audit
from src.runtime.artifacts import save_json
from fixtures.cli_response import cli_result
from fixtures.ledger_response import prompt_context, response
from src.runtime.reporting import Reporter
from test_folder import FolderFixture


class CitationIntegrityTests(FolderFixture):
    def setUp(self):
        super().setUp()
        (self.source / 'app.py').write_text('def main():\n    return 42\n')
        self.value.update(result_policy='compromise', output_language='English')

    @staticmethod
    def model_answer(context):
        data = response(context)
        if context['stage'] == 'study':
            data['evidence'][0].update(start_line=2, end_line=2, quote='        return 42')
        return data

    def test_review_cannot_change_quote_correction_sidecars(self):
        for filename in ('quote-corrected.json', 'quote-corrections.json', 'quote-diagnostics.json'):
            with self.subTest(filename=filename):
                runner = Runner(self.config(), self.base / filename)
                calls, originals = [], {}
                source_bytes = (self.source / 'app.py').read_bytes()
                attempt = runner.session.run_dir / 'revisions/001/study.logs/attempt-001'

                def process(command, cwd, env, payload, **kwargs):
                    context = prompt_context(payload)
                    calls.append(context['stage'])
                    if context['stage'] == 'review':
                        original = attempt / 'extracted.json'
                        originals[original] = original.read_bytes()
                        self.assertEqual(json.loads(originals[original])['evidence'][0]['quote'],
                                         '        return 42')
                        corrected = json.loads((attempt / 'quote-corrected.json').read_text())
                        self.assertEqual(corrected['evidence'][0]['quote'], '    return 42')
                        target = attempt / filename
                        changed = json.loads(target.read_text())
                        if filename == 'quote-corrected.json':
                            changed['evidence'][0]['quote'] = 'Injected quote'
                        elif filename == 'quote-corrections.json':
                            changed['changes'][0]['reason'] = 'Injected reason'
                        else:
                            changed[0]['diff'] = 'Injected diff'
                        save_json(target, changed)
                    return cli_result(command, self.model_answer(context))

                with patch.object(runner, 'check_cli', return_value={}), \
                        patch('src.runtime.process.process', side_effect=process):
                    manifest, code = runner.run()
                self.assertEqual(calls, ['study', 'review'])
                self.assertEqual((code, manifest['status'], manifest['critical_failure']), (1, 'FAILED', True))
                self.assertFalse(manifest['publication_complete'])
                self.assertTrue(any(d.get('failure_layer') == 'integrity' for d in manifest['diagnostics']))
                self.assertEqual(len(originals), 1)
                for original, content in originals.items():
                    self.assertEqual(original.read_bytes(), content)
                self.assertEqual((self.source / 'app.py').read_bytes(), source_bytes)

    def test_review_cannot_change_only_an_inserted_source_reference(self):
        runner = Runner(self.config(), self.base / 'review-source-reference')
        calls, preserved = [], {}

        def process(command, cwd, env, payload, **kwargs):
            context = prompt_context(payload)
            calls.append(context['stage'])
            if context['stage'] == 'review':
                directory = runner.session.run_dir / 'revisions/001'
                original = directory / 'study.original.md'
                preserved[original] = original.read_bytes()
                extracted = directory / 'study.logs/attempt-001/extracted.json'
                preserved[extracted] = extracted.read_bytes()
                report = directory / 'ARCHITECTURE.md'
                text = report.read_text()
                reference = '[E-001](#source-e-001)'
                self.assertIn(reference, text)
                report.write_text(text.replace(reference, '[E-001](#source-e-999)', 1))
            return cli_result(command, self.model_answer(context))

        with patch.object(runner, 'check_cli', return_value={}), patch('src.runtime.process.process', side_effect=process):
            manifest, code = runner.run()
        self.assertEqual(calls, ['study', 'review'])
        self.assertEqual((code, manifest['status'], manifest['critical_failure']), (1, 'FAILED', True))
        self.assertFalse(manifest['publication_complete'])
        self.assertTrue(any(d.get('failure_layer') == 'integrity' for d in manifest['diagnostics']))
        self.assertEqual(len(preserved), 2)
        for path, content in preserved.items():
            self.assertEqual(path.read_bytes(), content)

    def test_publication_rejects_tampered_source_registry_with_unchanged_canonical_document(self):
        runner = Runner(self.config(), self.base / 'publication-source-registry')
        inventory, _ = runner.session.source_inventory()
        context = dict(source_directory=str(self.source), source_fingerprint=inventory['source_fingerprint'])
        destination = runner.session.run_dir / 'study.logs'

        def process(command, cwd, env, payload, **kwargs):
            return cli_result(command, self.model_answer(prompt_context(payload)))

        with patch('src.runtime.process.process', side_effect=process):
            data, meta = runner.invoke('study', context, destination)
        self.assertTrue(meta['publication_complete'])
        report = runner.session.run_dir / 'ARCHITECTURE.md'
        original_path = runner.session.run_dir / 'study.original.md'
        original = original_path.read_bytes()
        extracted_path = destination / 'attempt-001/extracted.json'
        extracted = extracted_path.read_bytes()
        expected = report.read_bytes()
        self.assertIn(b'### Source registry\n', expected)
        self.assertIn(b'RESOLVED; locator resolved; quote matched', expected)
        tampered = expected.replace(b'RESOLVED; locator resolved; quote matched', b'TAMPERED; locator resolved; quote matched', 1)
        report.write_bytes(tampered)
        with self.assertRaises(AuditError) as caught:
            runner.publisher.publish_result('study', data, destination)
        self.assertEqual(caught.exception.failure_layer, 'publication')
        self.assertEqual(report.read_bytes(), tampered)
        self.assertEqual(original_path.read_bytes(), original)
        self.assertEqual(extracted_path.read_bytes(), extracted)

    def test_late_failure_clears_completed_warning_success_flags(self):
        original_run = Runner.run
        for failure in ('manifest_write', 'late_close'):
            with self.subTest(failure=failure):
                self.value['reports_dir'] = './reports-' + failure
                self.config()
                args = SimpleNamespace(config=self.config_path, check=False, trust_repository=False)
                out, err = io.StringIO(), io.StringIO()
                reporter = Reporter(mode='json', stdout=out, stderr=err, progress=False)
                self.addCleanup(reporter.close)
                completed = []

                def model(command, cwd, env, payload, **kwargs):
                    return cli_result(command, self.model_answer(prompt_context(payload)))

                def write(path, value):
                    if (failure == 'manifest_write' and path.name == 'manifest.json'
                            and value.get('status') == 'COMPLETE_WITH_WARNINGS'
                            and value.get('processing_completed') and not completed):
                        completed.append(copy.deepcopy(value))
                        raise OSError('Final manifest write failed')
                    save_json(path, value)

                def run(runner, check_only=False):
                    result = original_run(runner, check_only)
                    if failure == 'late_close':
                        completed.append(copy.deepcopy(result[0]))
                        runner.session.repo = SimpleNamespace(close=Mock(side_effect=OSError('Late cleanup failed')))
                    return result

                with patch.object(Runner, 'check_cli', return_value={}), \
                        patch.object(Runner, 'run', autospec=True, side_effect=run), \
                        patch('src.runtime.process.process', side_effect=model), patch('src.runtime.artifacts.save_json', side_effect=write):
                    code = run_audit(args, reporter)
                self.assertEqual(len(completed), 1)
                self.assertEqual(completed[0]['status'], 'COMPLETE_WITH_WARNINGS')
                self.assertTrue(completed[0]['processing_completed'])
                self.assertTrue(completed[0]['workflow_satisfied'])
                result = json.loads(out.getvalue())
                self.assertEqual((code, result['status'], result['exit_code']), (1, 'FAILED', 1))
                self.assertFalse(result['processing_completed'])
                self.assertFalse(result['workflow_satisfied'])
                saved = json.loads(Path(result['manifest']).read_text())
                self.assertEqual(saved['status'], 'FAILED')
                self.assertFalse(saved['processing_completed'])
                self.assertFalse(saved['workflow_satisfied'])
                self.assertFalse(saved['publication_complete'])
