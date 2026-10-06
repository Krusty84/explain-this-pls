# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Actual model payloads and private binding guards, with no model requests."""
import json
from fixtures.cli_response import cli_result
from pathlib import Path
import unittest
from unittest.mock import patch

from src.analysis.evidence import canonical, sha
from explain import Runner
from fixtures.ledger_response import response, prompt_context
from test_folder import FolderFixture
from test_revision_pipeline import initial_finding
import test_structured_output as native_fixtures


def service_hashes(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if (key in ('sha256', 'fingerprint', 'source_fingerprint') or key.endswith('_sha256')) and isinstance(item, str):
                yield item
            else:
                yield from service_hashes(item)
    elif isinstance(value, list):
        for item in value:
            yield from service_hashes(item)


class HashlessPipelineTests(FolderFixture):
    def execute_case(self, change=None):
        self.value.update(result_policy='compromise', output_language='English')
        runner = Runner(self.config(), self.base / 'hashless-run')
        calls = []
        def process(command, cwd, env, payload, **kwargs):
            context = prompt_context(payload)
            data = response(context)
            if change:
                data = change(runner, context, data)
            calls.append((context, payload.decode(), data))
            return cli_result(command, data)
        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process):
            manifest, code = runner.run()
        return runner, manifest, code, calls

    def test_all_revision_prompts_omit_service_hashes_and_preserve_private_chain(self):
        runner, manifest, code, calls = self.execute_case(lambda r, c, d: initial_finding(c['stage'], c, d))
        self.assertEqual(code, 0)
        self.assertEqual([c['stage'] for c, _, _ in calls], ['catalog', 'study', 'review', 'study', 'review'])
        self.assertEqual(len({c['source_snapshot_id'] for c, _, _ in calls}), 1)
        targets = [c['review_target_id'] for c, _, _ in calls if c['stage'] == 'review']
        self.assertEqual(len(set(targets)), 2)
        self.assertEqual(calls[3][0]['previous_revision']['review']['review_target_id'], targets[0])
        hashes = set(service_hashes(manifest))
        self.assertTrue(hashes)
        for context, prompt, raw in calls:
            self.assertEqual(list(service_hashes(context)), [])
            self.assertNotIn('source_fingerprint', prompt)
            for value in hashes:
                self.assertNotIn(value, prompt)
            self.assertNotIn('source_fingerprint', raw)
            if context['stage'] == 'review':
                self.assertNotIn('target', raw)
        attempts = list(runner.run_dir.rglob('attempt-001'))
        self.assertEqual(len(attempts), 5)
        for attempt in attempts:
            raw = json.loads((attempt / 'extracted.json').read_text())
            expanded = json.loads((attempt / 'expanded.json').read_text())
            binding = json.loads((attempt / 'binding.json').read_text())
            meta = json.loads((attempt / 'invocation.json').read_text())
            self.assertEqual(meta['context_format'], 'compact-context')
            self.assertEqual(binding['wire_sha256'], sha(canonical(raw)))
            self.assertEqual(binding['expanded_sha256'], sha(canonical(expanded)))
            self.assertEqual(expanded['source_fingerprint'], manifest['source_fingerprint'])
            self.assertEqual(meta['prompt_sha256'], sha((attempt / 'input.prompt.txt').read_bytes()))
            self.assertEqual(list(service_hashes(json.loads((attempt / 'schema.json').read_text()))), [])
            if raw['task'] != 'architecture_catalog':
                normalized = json.loads((attempt / 'normalized.json').read_text())
                provenance = meta['normalization_provenance']
                self.assertEqual(provenance['input_sha256'], sha(canonical(expanded)))
                self.assertEqual(provenance['normalized_sha256'], sha(canonical(normalized)))
        self.assertTrue(manifest['study']['program_checks']['evidence'][0]['file_sha256'])
        final = Path(manifest['final_report']).read_text()
        self.assertNotIn('File SHA-256', final)
        self.assertNotIn('Fragment SHA-256', final)

    def test_model_cannot_change_its_private_binding_file(self):
        def change(runner, context, data):
            if context['stage'] == 'study':
                path = runner.run_dir / 'revisions/001/study.logs/attempt-001/binding.json'
                path.write_bytes(path.read_bytes() + b'\nchanged\n')
            return data
        runner, manifest, code, calls = self.execute_case(change)
        self.assertEqual((code, manifest['status'], manifest['critical_failure']), (1, 'FAILED', True))
        self.assertFalse(manifest['publication_complete'])
        self.assertFalse((runner.run_dir / 'ARCHITECTURE.md').exists())
        self.assertEqual([c['stage'] for c, _, _ in calls], ['catalog', 'study'])
        self.assertTrue(any(d.get('failure_layer') == 'integrity' for d in manifest['diagnostics']))

    def test_wrong_snapshot_id_never_becomes_recovered_material(self):
        def change(runner, context, data):
            if context['stage'] == 'study':
                data['source_snapshot_id'] = 'S-abcdefghijklmnop'
                data['unexpected'] = True
            return data
        runner, manifest, code, calls = self.execute_case(change)
        self.assertNotEqual(code, 0)
        self.assertIsNone(manifest['study'])
        self.assertNotIn('study_material', manifest['revisions'][0])
        self.assertEqual([c['stage'] for c, _, _ in calls], ['catalog', 'study'])
        self.assertFalse((runner.run_dir / 'revisions/001/study.material.json').exists())


class HashlessXXXTests(unittest.TestCase):
    setUp = native_fixtures.XXXOutputTests.setUp
    config = native_fixtures.XXXOutputTests.config
    stage = native_fixtures.XXXOutputTests.stage
    recorded = native_fixtures.XXXOutputTests.recorded
    prompts = native_fixtures.XXXOutputTests.prompts

    def test_invalid_service_hashes_are_saved_but_never_sent_for_repair(self):
        from src.contracts.contracts import ContractError
        with self.assertRaises(ContractError):
            self.stage('xxx', 'schema-extra', repairs=2)
        attempt = self.destination / 'attempt-001'
        raw = json.loads((attempt / 'extracted.json').read_text())
        binding = json.loads((attempt / 'binding.json').read_text())
        self.assertIn('extra_private_key', raw)
        self.assertEqual(binding['wire_sha256'], sha(canonical(raw)))
        self.assertEqual(len(self.prompts()), 1)
        self.assertFalse((self.destination / 'attempt-002').exists())
