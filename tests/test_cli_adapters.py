# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline compatibility checks for the standalone CLI adapters."""
import itertools
import json
from fnmatch import fnmatchcase
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backends import codex, opencode_cli, preflight, xxx
from src.contracts.contracts import ContractError, FOLDER_SCHEMAS, SCHEMAS
from src.runtime.errors import AuditError
from src.runtime.runner import Runner


ADAPTERS = {'codex': codex}
REQUIRED_FLAGS = {
    'codex': ['--ephemeral', '--output-schema', '--sandbox', '--json', '--output-last-message'],
}


class CLIAdapterTests(unittest.TestCase):
    def test_source_map_permissions_resolve_symlinks_without_allowing_sibling_logs(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / 'attempt/source-maps'
            directory.mkdir(parents=True)
            alias = Path(raw) / 'maps-link'
            alias.symlink_to(directory, target_is_directory=True)
            expected = directory.resolve().as_posix() + '/*'
            for adapter in (opencode_cli, xxx):
                with self.subTest(adapter=adapter.__name__):
                    env = {}
                    name = adapter.prepare_environment(env, 'study', source_maps_directory=alias)
                    config = json.loads(env['OPENCODE_CONFIG_CONTENT'])
                    if adapter is xxx:
                        patterns = [pattern for pattern, effect in
                            config['agent'][name]['permission']['external_directory'].items() if effect == 'allow']
                    else:
                        patterns = [rule['resource'] for rule in config['agents'][name]['permissions']
                            if rule['action'] == 'external_directory' and rule['effect'] == 'allow']
                    self.assertEqual(patterns, [expected])
                    self.assertTrue(fnmatchcase((directory / 'M-000001.json').as_posix(), expected))
                    self.assertFalse(fnmatchcase((directory.parent / 'stdout.log').as_posix(), expected))
                    self.assertFalse(fnmatchcase((directory.parent / 'source-maps-other/M-000001.json').as_posix(), expected))

    def test_source_map_directory_reaches_only_external_read_permissions(self):
        session = SimpleNamespace(mode='git', schemas={'study': {}})
        state, schema_path = Path('/state'), Path('/state/schema.json')
        directory = Path('/private output/source-maps')
        for backend, adapter in (('opencode', opencode_cli), ('xxx', xxx)):
            for reports_only in (False, True):
                with self.subTest(backend=backend, reports_only=reports_only):
                    env = {}
                    with patch.object(adapter, 'prepare_environment', return_value='audit') as prepare:
                        preflight.command(session, 'study', state, {'backend': backend, 'executable': 'cli'},
                            schema_path, env, reports_only=reports_only, source_maps_directory=directory)
                    prepare.assert_called_once_with(env, 'compare' if reports_only else 'study',
                        source_maps_directory=directory)
        command = preflight.command(session, 'study', state, {'backend': 'codex', 'executable': 'cli'},
            schema_path, {}, source_maps_directory=directory)
        self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
        self.assertNotIn('--add-dir', command)

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
                            self.assertEqual(cmd[1], 'exec')
                            self.assertEqual(cmd[-1], '-')
                            self.assertIn('--ephemeral', cmd)
                            self.assertEqual(cmd[cmd.index('--sandbox') + 1], 'read-only')
                            self.assertEqual(cmd[cmd.index('--output-schema') + 1], str(schema_path))
                            self.assertIn('--json', cmd)
                            self.assertEqual(cmd[cmd.index('--output-last-message') + 1],
                                             str(schema_path.with_name('final.response.json')))
                            self.assertIn('approval_policy="never"', cmd)
                            self.assertIn('web_search="disabled"', cmd)
                            self.assertEqual('--skip-git-repo-check' in cmd, True)
                            self.assertEqual('features.shell_tool=false' in cmd, stage == 'compare')

    def test_help_commands_and_required_flags_are_independent_between_calls(self):
        self.assertEqual(codex.help_command('/configured/codex'), ['/configured/codex', 'exec', '--help'])
        for backend, adapter in ADAPTERS.items():
            for mode in ('git', 'folder'):
                with self.subTest(backend=backend, mode=mode):
                    expected = REQUIRED_FLAGS[backend] + ['--skip-git-repo-check']
                    flags = adapter.required_flags(mode)
                    self.assertEqual(flags, expected)
                    flags.clear()
                    self.assertEqual(adapter.required_flags(mode), expected)

    def test_codex_reasoning_effort_is_a_quoted_config_override(self):
        agent = {'executable': 'codex', 'model': 'chosen-model', 'reasoning_effort': 'high'}
        command = codex.build_command(agent, 'compare', 'git', {}, Path('/state/schema.json'))
        self.assertEqual(command, ['codex', 'exec', '--ephemeral', '--color', 'never',
            '--sandbox', 'read-only', '--output-schema', '/state/schema.json', '--json',
            '--output-last-message', '/state/final.response.json', '-c', 'approval_policy="never"',
            '-c', 'web_search="disabled"', '--skip-git-repo-check', '-c', 'features.shell_tool=false',
            '--model', 'chosen-model', '-c', 'model_reasoning_effort="high"', '-'])
        for model in (None, 'chosen-model'):
            agent = {'executable': 'codex', 'model': model}
            original = codex.build_command(agent, 'study', 'folder', {}, Path('/state/schema.json'))
            self.assertEqual(codex.build_command(agent | {'reasoning_effort': None}, 'study',
                'folder', {}, Path('/state/schema.json')), original)
            with_effort = codex.build_command(agent | {'reasoning_effort': 'xhigh'}, 'study',
                'folder', {}, Path('/state/schema.json'))
            self.assertEqual(with_effort, original[:-1] + ['-c', 'model_reasoning_effort="xhigh"', '-'])

    def test_parsers_preserve_results_and_metadata(self):
        data = {'report_markdown': '# Report', 'completion_status': 'COMPLETE'}
        outputs = {
            'codex': (data, {}),
        }
        for backend, (envelope, expected_metadata) in outputs.items():
            with self.subTest(backend=backend):
                output = '\n ' + json.dumps(envelope) + ' \n'
                self.assertEqual(ADAPTERS[backend].parse_output(output), (data, expected_metadata))

    def test_parsers_preserve_failure_classification(self):
        cases = [
            ('codex', '```json\n{}\n```', 'INVALID_JSON', 'result'),
            ('codex', '{"x": 1, "x": 2}', 'INVALID_JSON', 'result'),
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
from src.backends import codex
assert codex.parse_output('{}') == ({}, {})
assert 'explain' not in sys.modules
'''
        for order in itertools.permutations(('src.backends.codex', 'src.contracts.contracts')):
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
                with patch('src.runtime.process.process', side_effect=responses) as process:
                    result = runner.check_cli()
                self.assertEqual(process.call_count, 2)
                self.assertEqual(process.call_args_list[0].args[0], ['/configured/agent', '--version'])
                self.assertEqual(process.call_args_list[1].args[0],
                                 ['/configured/agent', *(['exec'] if backend == 'codex' else []), '--help'])
                key = backend + ':/configured/agent'
                self.assertEqual(result, {key: {'version': 'fixture 1.0', 'required_flags': flags}})
                self.assertEqual(runner.session.versions, {key: 'fixture 1.0'})

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
                    with patch('src.runtime.process.process', side_effect=responses) as process, \
                            self.assertRaises(AuditError) as caught:
                        runner.check_cli()
                    self.assertEqual(process.call_count, 2)
                    self.assertEqual(caught.exception.code, 'BACKEND_INCOMPATIBLE')
                    self.assertEqual(caught.exception.failure_kind, 'BACKEND_INCOMPATIBLE')
                    self.assertEqual(caught.exception.failure_layer, 'compatibility')
                    self.assertEqual(runner.session.versions, {})

    def test_xxx_requires_variant_if_only_a_later_stage_sets_effort(self):
        for effort, supported in ((None, False), ('high', False), ('high', True)):
            with self.subTest(effort=effort, supported=supported):
                runner = self.runner('xxx')
                runner.session.cfg['_agents']['review']['reasoning_effort'] = effort
                flags = xxx.required_flags('folder') + (['--variant'] if supported else [])
                responses = [
                    {'returncode': 0, 'stdout': b'fixture 1.0', 'stderr': b''},
                    {'returncode': 0, 'stdout': ' '.join(flags).encode(), 'stderr': b''},
                    {'returncode': 0, 'stdout': b'sessionID', 'stderr': b''},
                    {'returncode': 0, 'stdout': b'sessionID', 'stderr': b''},
                ]
                with patch('src.runtime.process.process', side_effect=responses) as process:
                    if effort is not None and not supported:
                        with self.assertRaises(AuditError) as caught:
                            runner.check_cli()
                        self.assertEqual(caught.exception.code, 'BACKEND_INCOMPATIBLE')
                        self.assertEqual(process.call_count, 2)
                    else:
                        result = runner.check_cli()
                        self.assertEqual(process.call_count, 4)
                        self.assertEqual(result['xxx:/configured/agent']['required_flags'],
                            xxx.required_flags('folder') + (['--variant'] if effort is not None else []))
                self.assertNotIn('--variant', xxx.required_flags('folder'))

    def test_xxx_variant_requirement_is_specific_to_the_executable(self):
        runner = self.runner('xxx')
        runner.session.cfg['_agents']['review'].update(executable='/configured/other', reasoning_effort='high')
        responses = []
        for extra in ([], ['--variant']):
            responses += [
                {'returncode': 0, 'stdout': b'fixture 1.0', 'stderr': b''},
                {'returncode': 0, 'stdout': ' '.join(xxx.required_flags('folder') + extra).encode(), 'stderr': b''},
                {'returncode': 0, 'stdout': b'sessionID', 'stderr': b''},
                {'returncode': 0, 'stdout': b'sessionID', 'stderr': b''},
            ]
        with patch('src.runtime.process.process', side_effect=responses) as process:
            result = runner.check_cli()
        self.assertEqual(process.call_count, 8)
        self.assertNotIn('--variant', result['xxx:/configured/agent']['required_flags'])
        self.assertIn('--variant', result['xxx:/configured/other']['required_flags'])


if __name__ == '__main__':
    unittest.main()
