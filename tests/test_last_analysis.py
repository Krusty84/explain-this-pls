# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Config metadata persists only after useful results and never changes the audit outcome."""
from argparse import Namespace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.contracts.contracts import jsonc
from src.runtime import artifacts
from src.runtime.audit import run_audit
from src.runtime.config_edit import save_last_analysis
from src.runtime.reporting import Reporter
from src.runtime.runner import Runner
from test_folder import FolderFixture


FINISHED = '2026-10-17T12:00:00+00:00'
PREVIOUS = {'status': 'PARTIAL', 'finished_at': '2026-10-16T12:00:00+00:00', 'results_dir': '/previous/run'}


class LastAnalysisEditTests(unittest.TestCase):
    def test_insert_json_and_jsonc_preserves_other_fields_and_comments(self):
        texts = [('json', '{"reports_dir": "../reports", "agent": {"model": "unchanged"}}\r\n'),
                 ('jsonc', '{\r\n\t// keep settings\r\n\t"url": "https://example.invalid/a//b", // keep URL\r\n}\r\n'),
                 ('jsonc', '{\n  "agent": {} // keep comment without a trailing comma\n}\n'),
                 ('jsonc', '{ /* empty config */ }'),
                 ('json', '{"last_analysis":null, "reports_dir":"../reports"}')]
        record = {'status': 'COMPLETED', 'finished_at': FINISHED, 'results_dir': '/reports/深い [bold]/run'}
        with tempfile.TemporaryDirectory() as directory:
            for suffix, text in texts:
                with self.subTest(text=text):
                    path = Path(directory) / ('config.' + suffix)
                    original = text.encode('utf-8')
                    path.write_bytes(original)
                    save_last_analysis(path, record, original)
                    updated = path.read_bytes().decode('utf-8')
                    self.assertEqual(jsonc(updated), jsonc(text) | {'last_analysis': record})
                    if suffix == 'json':
                        json.loads(updated)
                    for comment in ('// keep settings', '// keep URL', '// keep comment without a trailing comma',
                                    '/* empty config */'):
                        if comment in text:
                            self.assertIn(comment, updated)
                    if '\r\n' in text:
                        self.assertNotIn('\n', updated.replace('\r\n', ''))

    def test_update_preserves_comments_and_spacing_inside_previous_record(self):
        text = ('{\r\n  "last_analysis": {\r\n    "status" : "PARTIAL", // keep status comment\r\n'
                '    "finished_at": "2026-10-16T12:00:00+00:00",\r\n'
                '    "results_dir": "/previous/run", /* keep path comment */\r\n  },\r\n'
                '  "agent": {"model": "unchanged"},\r\n}\r\n')
        record = PREVIOUS | {'status': 'COMPLETED', 'finished_at': FINISHED, 'results_dir': '/new/run'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.jsonc'
            path.write_bytes(text.encode())
            save_last_analysis(path, record, path.read_bytes())
            expected = text.replace('"PARTIAL"', '"COMPLETED"').replace(PREVIOUS['finished_at'], FINISHED)
            self.assertEqual(path.read_bytes().decode(), expected.replace('/previous/run', '/new/run'))

    def test_external_changes_and_atomic_failure_preserve_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.jsonc'
            original = b'{"last_analysis": null}\n'
            external = original + b'// external edit\n'
            path.write_bytes(external)
            with self.assertRaisesRegex(ValueError, 'configuration changed'):
                save_last_analysis(path, PREVIOUS, original)
            self.assertEqual(path.read_bytes(), external)
            path.write_bytes(original)
            with patch('src.runtime.config_edit.atomic', side_effect=PermissionError('Read-only config')):
                with self.assertRaises(PermissionError):
                    save_last_analysis(path, PREVIOUS, original)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])


class LastAnalysisLifecycleTests(FolderFixture):
    def execute(self, status, code, *, useful=True, report=True, check=False, callback=None, error=None):
        stdout, stderr, events = io.StringIO(), io.StringIO(), []
        reporter = Reporter(mode='json', stdout=stdout, stderr=stderr, progress=False, event_sink=events.append)
        args = Namespace(config=self.config_path, check=check, trust_repository=False)

        def run(runner, check_only=False):
            path = runner.session.run_dir / 'FINAL_REPORT.md'
            if report:
                artifacts.atomic(path, '# Saved result\n')
            manifest = {'status': status, 'exit_code': code, 'finished_at': FINISHED,
                        'has_usable_material': useful, 'final_report': str(path)}
            runner.session.manifest = manifest
            runner.session.analysis_started = not check_only
            artifacts.save_json(runner.session.run_dir / 'manifest.json', manifest)
            if callback:
                callback(runner)
            if error:
                raise error
            return manifest, code

        with patch.object(Runner, 'run', run):
            result_code = run_audit(args, reporter)
        return result_code, json.loads(stdout.getvalue()), stderr.getvalue(), events

    def test_published_results_update_only_metadata_for_all_final_outcomes(self):
        outcomes = [('COMPLETE', 0, 'COMPLETED'), ('COMPLETE_WITH_WARNINGS', 0, 'COMPLETED'),
                    ('PARTIAL', 2, 'PARTIAL'), ('FAILED', 1, 'PARTIAL'), ('FAILED', 130, 'PARTIAL')]
        for status, code, recorded in outcomes:
            self.value['last_analysis'] = PREVIOUS
            self.config()
            before = jsonc(self.config_path.read_text())
            with self.subTest(status=status, code=code):
                result_code, result, _, events = self.execute(status, code)
                after = jsonc(self.config_path.read_text())
                self.assertEqual(result_code, code)
                self.assertEqual(result['status'], status)
                self.assertEqual(after['last_analysis'], {'status': recorded, 'finished_at': FINISHED,
                    'results_dir': str(Path(result['manifest']).parent)})
                self.assertEqual({k: v for k, v in after.items() if k != 'last_analysis'},
                                 {k: v for k, v in before.items() if k != 'last_analysis'})
                self.assertNotIn('last_analysis_warning', [event.event for event in events])

    def test_check_and_runs_without_saved_useful_results_preserve_previous_record(self):
        cases = [('PREFLIGHT_OK', 0, True, True, True), ('FAILED', 1, False, True, False),
                 ('FAILED', 130, False, False, False), ('PARTIAL', 2, True, False, False)]
        for status, code, useful, report, check in cases:
            self.value['last_analysis'] = PREVIOUS
            self.config()
            before = self.config_path.read_bytes()
            with self.subTest(status=status, useful=useful, report=report, check=check):
                self.assertEqual(self.execute(status, code, useful=useful, report=report, check=check)[0], code)
                self.assertEqual(self.config_path.read_bytes(), before)

    def test_caught_error_or_interrupt_keeps_saved_material_as_partial(self):
        for error, code in ((RuntimeError('Stage failed'), 1), (KeyboardInterrupt(), 130)):
            self.config()
            with self.subTest(error=type(error).__name__):
                result_code, result, _, _ = self.execute('PARTIAL', 2, error=error)
                saved = jsonc(self.config_path.read_text())['last_analysis']
                manifest = json.loads(Path(result['manifest']).read_text())
                self.assertEqual(result_code, code)
                self.assertEqual(saved['status'], 'PARTIAL')
                self.assertEqual(saved['finished_at'], manifest['finished_at'])
                self.assertTrue(Path(result['final_report']).is_file())

    def test_metadata_is_written_after_cleanup_and_uses_final_outcome(self):
        self.config()
        repo = Mock()
        repo.close.side_effect = OSError('Cleanup failed')

        def cleanup_failure(runner):
            runner.session.repo = repo

        def save_after_cleanup(path, record, original):
            repo.close.assert_called_once()
            manifest = json.loads((Path(record['results_dir']) / 'manifest.json').read_text())
            self.assertEqual(manifest['status'], 'FAILED')
            save_last_analysis(path, record, original)

        with patch('src.runtime.audit.save_last_analysis', side_effect=save_after_cleanup):
            code, result, _, _ = self.execute('COMPLETE', 0, callback=cleanup_failure)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(jsonc(self.config_path.read_text())['last_analysis']['status'], 'PARTIAL')

    def test_external_edit_and_write_failure_warn_without_changing_analysis_result(self):
        for failure in ('external edit', 'write failure'):
            self.value['last_analysis'] = PREVIOUS
            self.config()
            before = self.config_path.read_bytes()
            external = before + b'\n// external edit\n'
            with self.subTest(failure=failure):
                if failure == 'external edit':
                    code, result, stderr, events = self.execute('COMPLETE', 0,
                        callback=lambda runner: self.config_path.write_bytes(external))
                    self.assertEqual(self.config_path.read_bytes(), external)
                else:
                    with patch('src.runtime.config_edit.atomic', side_effect=PermissionError('Read-only config')):
                        code, result, stderr, events = self.execute('COMPLETE', 0)
                    self.assertEqual(self.config_path.read_bytes(), before)
                self.assertEqual(code, 0)
                self.assertEqual(result['status'], 'COMPLETE')
                self.assertTrue(Path(result['final_report']).is_file())
                self.assertIn('[WARN] Could not save last_analysis', stderr)
                warning = next(event for event in events if event.event == 'last_analysis_warning')
                self.assertEqual(warning.context['code'], 'LAST_ANALYSIS_NOT_SAVED')
