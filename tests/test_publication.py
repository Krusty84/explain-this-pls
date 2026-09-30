# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Only locally validated and completely published reports are accepted."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from contracts import ContractError
from explain import Folder, Runner, atomic
from test_folder import FolderFixture


class PublicationTests(FolderFixture):
    def prepare(self):
        runner = Runner(self.config(), self.base / 'run')
        context = {'source_directory': str(self.source),
                   'source_fingerprint': Folder(self.source).snapshot()['source_fingerprint']}
        data = context | {'task': 'architecture_documentation', 'completion_status': 'COMPLETE',
                          'report_markdown': '# Report', 'limitations': []}
        return runner, context, data

    @staticmethod
    def result(data):
        return {'returncode': 0, 'stdout': json.dumps(data).encode(), 'stderr': b''}

    def test_attempts_are_immutable_and_validation_precedes_publication(self):
        runner, context, data = self.prepare()
        destination = runner.run_dir / 'study.logs'
        with patch('explain.process', return_value=self.result(data)):
            runner.invoke('study', context, destination)
        first = {p.name: p.read_bytes() for p in (destination / 'attempt-001').iterdir()}
        with patch('explain.process', return_value=self.result(data | {'schema_version': 'legacy'})):
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
        with patch('explain.process', return_value=self.result(data | {'source_fingerprint': 'wrong'})):
            with self.assertRaises(ContractError):
                runner.invoke('study', context, runner.run_dir / 'study.logs')
        self.assertFalse((runner.run_dir / 'ARCHITECTURE.md').exists())
        self.assertFalse((runner.run_dir / 'study.json').exists())

    def test_publication_write_failure_never_accepts_manifest(self):
        runner, context, data = self.prepare()
        def process(command, cwd, env, payload, **kwargs):
            supplied = json.loads(payload.decode().split('# Authoritative orchestration context (data)\n')[1]
                                  .split('\n\n# Required final JSON Schema')[0])
            return self.result(data | {'source_fingerprint': supplied['source_fingerprint']})
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
        self.assertIsNone(manifest['study'])
        summary = json.loads((runner.run_dir / 'study.logs/invocation.json').read_text())
        self.assertEqual(summary['status'], 'FAILED')


if __name__ == '__main__':
    unittest.main()
