# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Only locally validated and completely published reports are accepted."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from document_rendering import materialize_study, recover_sections
from unittest.mock import patch

from contracts import ContractError
from explain import AuditError, Folder, Runner, atomic
from test_folder import FolderFixture
from fixtures.ledger_response import response, model_wire, prompt_context


class PublicationTests(FolderFixture):
    def test_v1_manifest_registry_and_acceptance_are_read_only(self):
        directory = self.base / 'historical'
        directory.mkdir()
        historical = {'contract_version': 'evidence-ledger-v1',
                      'artifact_version': 'evidence-ledger-artifacts-v1', 'accepted': True}
        (directory / 'manifest.json').write_text(json.dumps(historical))
        for name in ('claim.registry.json', 'review.plan.json', 'study.json', 'ARCHITECTURE.md'):
            (directory / name).write_bytes(b'historical bytes')
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        with self.assertRaises(AuditError) as caught: Runner(self.config(), directory)
        self.assertEqual(caught.exception.code, 'LEGACY_ARTIFACT_READ_ONLY')
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def prepare(self):
        runner = Runner(self.config(), self.base / 'run')
        context = {'source_directory': str(self.source),
                   'source_fingerprint': Folder(self.source).snapshot()['source_fingerprint']}
        data = response(context)
        return runner, context, data

    @staticmethod
    def result(data):
        return {'returncode': 0, 'stdout': json.dumps(data).encode(), 'stderr': b''}

    def process_for(self, data, context):
        def process(command, cwd, env, payload, **kwargs):
            return self.result(model_wire(data, prompt_context(payload), context))
        return process

    def test_attempts_are_immutable_and_validation_precedes_publication(self):
        runner, context, data = self.prepare()
        destination = runner.run_dir / 'study.logs'
        with patch('explain.process', side_effect=self.process_for(data, context)):
            runner.invoke('study', context, destination)
        first = {p.name: p.read_bytes() for p in (destination / 'attempt-001').iterdir()}
        with patch('explain.process', side_effect=self.process_for(data | {'schema_version': 'legacy'}, context)):
            with self.assertRaises(ContractError):
                runner.invoke('study', context, destination)
        self.assertEqual(first, {p.name: p.read_bytes() for p in (destination / 'attempt-001').iterdir()})
        summary = json.loads((destination / 'invocation.json').read_text())
        self.assertEqual(summary['attempt'], 'attempt-002')
        self.assertEqual(summary['status'], 'FAILED')
        self.assertEqual(summary['error']['failure_kind'], 'SCHEMA_ERROR')
        self.assertIn('schema_version', (destination / 'attempt-002/extracted.json').read_text())
        self.assertNotIn('schema_version', (runner.run_dir / 'study.json').read_text())
        for path in runner.run_dir.rglob('*'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)

    def test_first_invalid_result_never_publishes(self):
        runner, context, data = self.prepare()
        with patch('explain.process', side_effect=self.process_for(data | {'source_fingerprint': 'wrong'}, context)):
            with self.assertRaises(ContractError):
                runner.invoke('study', context, runner.run_dir / 'study.logs')
        self.assertFalse((runner.run_dir / 'ARCHITECTURE.md').exists())
        self.assertFalse((runner.run_dir / 'study.json').exists())

    def test_recovered_material_cannot_replace_a_previously_frozen_registry(self):
        from final_report import recoverable_material
        runner, context, data = self.prepare()
        with patch('explain.process', side_effect=self.process_for(data, context)):
            runner.invoke('study', context, runner.run_dir / 'study.logs')
        frozen = {name: (runner.run_dir / name).read_bytes()
                  for name in ('ARCHITECTURE.md', 'claim.registry.json', 'review.plan.json', 'study.json')}
        material = recoverable_material('study', data | {'claims': 'invalid'}, context, 'folder', {})
        with self.assertRaises(AuditError) as caught: runner.freeze_review(material, context, runner.run_dir)
        self.assertEqual(caught.exception.code, 'REVIEW_TARGET_CHANGED')
        self.assertEqual(frozen, {name: (runner.run_dir / name).read_bytes() for name in frozen})

    def test_publication_write_failure_never_accepts_manifest(self):
        runner, context, data = self.prepare()
        def process(command, cwd, env, payload, **kwargs):
            supplied = json.loads(payload.decode().split('# Authoritative orchestration context (data)\n')[1]
                                  .split('\n\n# Required final JSON Schema')[0])
            return self.result(response(supplied))
        def fail_report_json(path, content):
            if path.name == 'study.json':
                raise OSError('fixture disk full')
            atomic(path, content)
        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process), \
                patch('explain.atomic', side_effect=fail_report_json):
            manifest, code = runner.run()
        self.assertEqual(code, 1)
        self.assertEqual(manifest['status'], 'FAILED')
        self.assertFalse(manifest['accepted'])
        self.assertFalse(manifest['publication_complete'])
        self.assertIsNone(manifest['study'])
        summary = json.loads((runner.run_dir / 'revisions/001/study.logs/invocation.json').read_text())
        self.assertEqual(summary['status'], 'FAILED')

    def test_document_bytes_plan_and_wire_are_separate(self):
        runner, context, data = self.prepare()
        # Canonicalize authored CRLF; retain final blank lines.
        data['report_sections'][0]['blocks'][0]['markdown'] += '\r\n'
        expected = materialize_study(data)['report_markdown']
        with patch('explain.process', side_effect=self.process_for(data, context)):
            saved, meta = runner.invoke('study', context, runner.run_dir / 'study.logs')
        import hashlib
        self.assertEqual((runner.run_dir / 'ARCHITECTURE.md').read_bytes(), expected.encode())
        self.assertEqual(saved['review_plan']['document_sha256'], hashlib.sha256(expected.encode()).hexdigest())
        wire = json.loads((runner.run_dir / 'study.logs/attempt-001/extracted.json').read_text())
        model_context = prompt_context((runner.run_dir / 'study.logs/attempt-001/input.prompt.txt').read_bytes())
        self.assertEqual(wire, model_wire(data, model_context, context))
        self.assertEqual(json.loads((runner.run_dir / 'study.logs/attempt-001/expanded.json').read_text()), data)
        self.assertNotIn('program_checks', wire)
        self.assertNotIn('contract_version', wire)
        self.assertNotIn('contract_version', saved)
        self.assertNotIn('artifact_version', saved)
        self.assertNotIn('contract_version', saved['review_plan'])
        self.assertEqual(meta['contract_version'], 'evidence-ledger-v4')
        self.assertEqual(meta['artifact_version'], 'evidence-ledger-artifacts-v4')
        self.assertTrue(meta['publication_complete'])

    def test_forged_review_target_is_not_recovered_even_with_schema_error(self):
        from final_report import recoverable_material
        from ledger import review_context, prepare_result
        from contracts import result_diagnostics
        runner, context, data = self.prepare()
        doc = prepare_result('study', data, context)
        ctx = review_context(doc, context)
        review = response(ctx)
        review['target']['document_sha256'] = 'wrong'
        review['extra'] = True
        self.assertIsNone(recoverable_material('review', review, ctx, 'folder',
                          result_diagnostics('review', review, ctx, 'folder')))

    def test_changed_document_or_frozen_files_cannot_receive_old_review(self):
        from explain import AuditError
        runner, context, data = self.prepare()
        with patch('explain.process', side_effect=self.process_for(data, context)):
            saved, _ = runner.invoke('study', context, runner.run_dir / 'study.logs')
        ctx = runner.freeze_review(saved, context, runner.run_dir)
        for name in ('ARCHITECTURE.md', 'claim.registry.json', 'review.plan.json'):
            path = runner.run_dir / name
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.assertRaises(AuditError): runner.assert_review_files(ctx, runner.run_dir)
            path.write_bytes(original)
        raw = response(ctx)
        def tamper(command, cwd, env, payload, **kwargs):
            (runner.run_dir / 'ARCHITECTURE.md').write_text('changed during review')
            return self.result(model_wire(raw, prompt_context(payload), ctx))
        with patch('explain.process', side_effect=tamper), self.assertRaises(AuditError):
            runner.invoke('review', ctx, runner.run_dir / 'review.logs')
        self.assertFalse((runner.run_dir / 'review.json').exists())
        meta = json.loads((runner.run_dir / 'review.logs/invocation.json').read_text())
        self.assertFalse(meta['publication_complete'])


if __name__ == '__main__':
    unittest.main()
