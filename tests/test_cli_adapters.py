# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline compatibility checks for the standalone CLI adapters."""
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import claude_code
import codex
from contracts import ContractError, FOLDER_SCHEMAS, SCHEMAS
from explain import AuditError, Runner


ADAPTERS = {'codex': codex, 'claude-code': claude_code}
REQUIRED_FLAGS = {
    'codex': ['--ephemeral', '--output-schema', '--sandbox'],
    'claude-code': ['--no-session-persistence', '--json-schema', '--tools',
                    '--allowedTools', '--disallowedTools', '--permission-mode'],
}


class CLIAdapterTests(unittest.TestCase):
    def test_commands_preserve_permissions_schema_and_model_selection(self):
        schema_path = Path('/private output/schema.json')
        for backend, adapter in ADAPTERS.items():
            for mode, schemas in (('git', SCHEMAS), ('folder', FOLDER_SCHEMAS)):
                for stage, schema in schemas.items():
                    for model in (None, '', 'chosen-model'):
                        with self.subTest(backend=backend, mode=mode, stage=stage, model=model):
                            agent = {'executable': '/configured cli/agent', 'model': model}
                            cmd = adapter.build_command(agent, stage, mode, schema, schema_path)
                            self.assertEqual(cmd[0], agent['executable'])
                            if model:
                                self.assertEqual(cmd[cmd.index('--model') + 1], model)
                            else:
                                self.assertNotIn('--model', cmd)
                            if backend == 'codex':
                                self.assertEqual(cmd[1], 'exec')
                                self.assertEqual(cmd[-1], '-')
                                self.assertIn('--ephemeral', cmd)
                                self.assertEqual(cmd[cmd.index('--sandbox') + 1], 'read-only')
                                self.assertEqual(cmd[cmd.index('--output-schema') + 1], str(schema_path))
                                self.assertIn('approval_policy="never"', cmd)
                                self.assertIn('web_search="disabled"', cmd)
                                self.assertEqual('--skip-git-repo-check' in cmd,
                                                 mode == 'folder' or stage == 'compare')
                                self.assertEqual('features.shell_tool=false' in cmd, stage == 'compare')
                            else:
                                self.assertEqual(cmd[1], '-p')
                                self.assertIn('--no-session-persistence', cmd)
                                self.assertEqual(cmd[cmd.index('--output-format') + 1], 'json')
                                self.assertEqual(json.loads(cmd[cmd.index('--json-schema') + 1]), schema)
                                self.assertEqual(cmd[cmd.index('--permission-mode') + 1], 'dontAsk')
                                self.assertEqual(cmd[cmd.index('--disallowedTools') + 1], 'mcp__*')
                                self.assertEqual(cmd[cmd.index('--tools') + 1],
                                                 '' if stage == 'compare' else 'Read,Glob,Grep')
                                self.assertEqual('--allowedTools' in cmd, stage != 'compare')
                                if stage != 'compare':
                                    self.assertEqual(cmd[cmd.index('--allowedTools') + 1], 'Read,Glob,Grep')

    def test_help_commands_and_required_flags_are_independent_between_calls(self):
        self.assertEqual(codex.help_command('/configured/codex'), ['/configured/codex', 'exec', '--help'])
        self.assertEqual(claude_code.help_command('/configured/claude'), ['/configured/claude', '--help'])
        for backend, adapter in ADAPTERS.items():
            for mode in ('git', 'folder'):
                with self.subTest(backend=backend, mode=mode):
                    expected = REQUIRED_FLAGS[backend] + (
                        ['--skip-git-repo-check'] if backend == 'codex' and mode == 'folder' else [])
                    flags = adapter.required_flags(mode)
                    self.assertEqual(flags, expected)
                    flags.clear()
                    self.assertEqual(adapter.required_flags(mode), expected)

    def test_parsers_preserve_results_and_metadata(self):
        data = {'report_markdown': '# Report', 'completion_status': 'COMPLETE'}
        metadata = {'session_id': 'session', 'total_cost_usd': 0.25,
                    'usage': {'input_tokens': 10}, 'modelUsage': {'chosen-model': {}}}
        outputs = {
            'codex': (data, {}),
            'claude-code': ({'is_error': False, 'structured_output': data,
                             'result': 'ignored ordinary text', **metadata}, metadata),
        }
        for backend, (envelope, expected_metadata) in outputs.items():
            with self.subTest(backend=backend):
                output = '\n ' + json.dumps(envelope) + ' \n'
                self.assertEqual(ADAPTERS[backend].parse_output(output), (data, expected_metadata))

    def test_parsers_preserve_failure_classification(self):
        cases = [
            ('codex', '```json\n{}\n```', 'INVALID_JSON', 'result'),
            ('codex', '{"x": 1, "x": 2}', 'INVALID_JSON', 'result'),
            ('claude-code', '{', 'TRANSPORT_ERROR', 'transport'),
            ('claude-code', '[]', 'TRANSPORT_ERROR', 'transport'),
            ('claude-code', '{"is_error": 0}', 'TRANSPORT_ERROR', 'transport'),
            ('claude-code', '{"is_error": true}', 'BACKEND_ERROR', 'backend'),
            ('claude-code', '{"is_error": false}', 'INCOMPLETE_OUTPUT', 'result'),
            ('claude-code', '{"is_error": false, "structured_output": []}', 'INCOMPLETE_OUTPUT', 'result'),
        ]
        for backend, output, kind, layer in cases:
            with self.subTest(backend=backend, output=output), self.assertRaises(ContractError) as caught:
                ADAPTERS[backend].parse_output(output)
            self.assertEqual(caught.exception.failure_kind, kind)
            self.assertEqual(caught.exception.failure_layer, layer)

    def test_adapters_import_without_orchestrator_in_any_order(self):
        script = '''
import importlib
import sys
for name in sys.argv[1:]:
    importlib.import_module(name)
import codex
import claude_code
assert codex.parse_output('{}') == ({}, {})
assert claude_code.parse_output('{"is_error": false, "structured_output": {}}') == ({}, {})
assert 'explain' not in sys.modules
'''
        for order in itertools.permutations(('codex', 'claude_code', 'contracts')):
            with self.subTest(order=order):
                result = subprocess.run([sys.executable, '-B', '-c', script, *order],
                                        cwd=ROOT, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)


class CLIPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / 'source'
        self.source.mkdir()

    def runner(self, backend):
        agent = {'backend': backend, 'executable': '/configured/agent', 'expected_version': 'fixture 1.0'}
        return Runner({'mode': 'folder', 'folder_mode': {'path': str(self.source)},
                       '_agents': {'study': agent, 'review': dict(agent)}}, self.base / 'reports')

    def test_checks_use_adapter_help_and_preserve_version_and_deduplication(self):
        for backend in ADAPTERS:
            with self.subTest(backend=backend):
                runner = self.runner(backend)
                flags = REQUIRED_FLAGS[backend] + (['--skip-git-repo-check'] if backend == 'codex' else [])
                responses = [
                    {'returncode': 0, 'stdout': b'fixture 1.0\n', 'stderr': b'startup warning'},
                    {'returncode': 0, 'stdout': b'CLI help\n', 'stderr': ' '.join(flags).encode()},
                ]
                with patch('explain.process', side_effect=responses) as process:
                    result = runner.check_cli()
                self.assertEqual(process.call_count, 2)
                self.assertEqual(process.call_args_list[0].args[0], ['/configured/agent', '--version'])
                self.assertEqual(process.call_args_list[1].args[0],
                                 ['/configured/agent', *(['exec'] if backend == 'codex' else []), '--help'])
                key = backend + ':/configured/agent'
                self.assertEqual(result, {key: {'version': 'fixture 1.0', 'required_flags': flags}})
                self.assertEqual(runner.versions, {key: 'fixture 1.0'})

    def test_missing_required_options_still_fail_before_invocation(self):
        for backend in ADAPTERS:
            flags = REQUIRED_FLAGS[backend] + (['--skip-git-repo-check'] if backend == 'codex' else [])
            for missing in flags:
                with self.subTest(backend=backend, missing=missing):
                    runner = self.runner(backend)
                    responses = [
                        {'returncode': 0, 'stdout': b'fixture 1.0', 'stderr': b''},
                        {'returncode': 0, 'stdout': ' '.join(f for f in flags if f != missing).encode(),
                         'stderr': b''},
                    ]
                    with patch('explain.process', side_effect=responses) as process, \
                            self.assertRaises(AuditError) as caught:
                        runner.check_cli()
                    self.assertEqual(process.call_count, 2)
                    self.assertEqual(caught.exception.code, 'BACKEND_INCOMPATIBLE')
                    self.assertEqual(caught.exception.failure_kind, 'BACKEND_INCOMPATIBLE')
                    self.assertEqual(caught.exception.failure_layer, 'compatibility')
                    self.assertEqual(runner.versions, {})


if __name__ == '__main__':
    unittest.main()
