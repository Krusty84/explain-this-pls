# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Root policy regressions and CLI integration with real Git and offline agents.

Mocked EUID tests exercise main(), not OS privileges. Separate tests below require
an actual root process and explicitly skip when that prerequisite is unavailable.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_explain as fixtures
from explain import AuditError, Repository, main

ROOT = Path(__file__).resolve().parents[1]
ROOT_WARNING = '[WARN] Running with administrator privileges.'
OWNERSHIP_HINT = ('Git rejected the repository because of an ownership mismatch.\n'
                  'Run with the checkout owner\'s UID, or use --trust-repository\n'
                  'only if you trust this specific checkout.')


class RepositoryTrustTests(unittest.TestCase):
    setUp = fixtures.RepoFixture.setUp
    tearDown = fixtures.RepoFixture.tearDown
    git = fixtures.RepoFixture.git

    def test_all_commands_keep_protections_and_trust_is_independent_of_mocked_euid(self):
        path = self.base / 'checkout with spaces'
        self.repo_path.rename(path)
        alias = self.base / 'checkout alias'; alias.symlink_to(path, target_is_directory=True)
        inherited = {'PATH': os.environ.get('PATH', os.defpath), 'LANG': 'C', 'LC_ALL': 'C',
            'HOME': str(self.base), 'SUDO_UID': '1234', 'GIT_DIR': '/unwanted',
            'GIT_WORK_TREE': '/unwanted', 'GIT_CONFIG_SYSTEM': '/unwanted',
            'GIT_CONFIG_GLOBAL': '/unwanted', 'GIT_CONFIG_COUNT': '1',
            'GIT_CONFIG_KEY_0': 'safe.directory', 'GIT_CONFIG_VALUE_0': '*',
            'GIT_CONFIG_PARAMETERS': "'safe.directory=*'", 'GIT_TEST_ASSUME_DIFFERENT_OWNER': '1'}
        protected = ['core.hooksPath=/dev/null', 'core.fsmonitor=',
                     'core.untrackedCache=false', 'submodule.recurse=false', 'core.pager=cat']
        for euid in (0, 1000):
            for trust in (False, True):
                with self.subTest(mocked_euid=euid, trust=trust), \
                        patch('explain.os.geteuid', return_value=euid), \
                        patch.object(Repository, 'check_ownership'), \
                        patch.dict(os.environ, inherited, clear=True), \
                        patch('explain.subprocess.run', wraps=subprocess.run) as run:
                    repo = Repository(alias, trust_repository=trust)
                    self.addCleanup(repo.close)
                    pins = repo.preflight(['master', 'test01'])
                    repo.assert_expected()
                    repo.delta(pins['master'], pins['test01'])
                    values = repo.git('config', '--get-all', 'safe.directory', allowed=(0, 1))
                    self.assertEqual(values, ('\n' + str(path) + '\n').encode() if trust else b'')
                    for call in run.call_args_list:
                        command = call.args[0]
                        self.assertIsInstance(command, list)
                        self.assertFalse(call.kwargs.get('shell', False))
                        self.assertEqual(command[command.index('-C') + 1], str(path))
                        settings = [command[i + 1] for i, arg in enumerate(command) if arg == '-c']
                        self.assertEqual(settings, protected)
                        self.assertEqual(call.kwargs['env'], {'PATH': inherited['PATH'],
                            'LANG': 'C', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
                            'GIT_CONFIG_GLOBAL': repo.env['GIT_CONFIG_GLOBAL'], 'GIT_TERMINAL_PROMPT': '0',
                            'GIT_OPTIONAL_LOCKS': '0', 'GIT_PAGER': 'cat',
                            'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': '', 'GIT_NO_REPLACE_OBJECTS': '1'})
                    self.assertEqual(dict(os.environ), inherited)

    def test_only_dubious_ownership_gets_hint_and_never_retries(self):
        errors = ["fatal: detected dubious ownership in repository at '/checkout'\noriginal detail",
                  "fatal: unsafe repository ('/checkout' is owned by someone else)",
                  "fatal: cannot open '.git/HEAD': Permission denied",
                  'fatal: Needed a single revision',
                  "fatal: ambiguous argument 'detected dubious ownership in repository': unknown revision"]
        for error in errors:
            for trust in (False, True):
                repo = Repository(self.repo_path, trust_repository=trust)
                self.addCleanup(repo.close)
                with self.subTest(error=error, trust=trust), patch('explain.subprocess.run') as run:
                    run.return_value = subprocess.CompletedProcess([], 128, b'', error.encode())
                    with self.assertRaises(AuditError) as caught:
                        repo.head()
                    expected = 'git rev-parse failed: ' + error
                    if error in errors[:2]:
                        expected += '\n' + OWNERSHIP_HINT
                    self.assertEqual(str(caught.exception), expected)
                    run.assert_called_once()


class StartupCLIIntegrationTests(unittest.TestCase):
    tearDown = fixtures.RepoFixture.tearDown
    git = fixtures.RepoFixture.git

    def setUp(self):
        fixtures.RepoFixture.setUp(self)
        path = self.base / 'checkout with spaces'
        self.repo_path.rename(path)
        self.repo_path = path
        self.repo = Repository(path)
        self.alias = self.base / 'checkout alias'
        self.alias.symlink_to(path, target_is_directory=True)
        self.folder = self.base / 'source folder'; self.folder.mkdir()
        (self.folder / 'app.py').write_text('print(42)\n')
        self.home = self.base / 'home'; self.home.mkdir()
        (self.home / 'audit-profile.json').write_text(json.dumps({'model': 'configured-model'}))
        self.cli = self.base / 'fake-cli'
        self.cli.write_text('#!' + sys.executable + '\nimport sys; sys.path.insert(0, ' +
                           repr(str(ROOT / 'tests/fixtures')) + ')\n' + (ROOT / 'tests/fixtures/fake_cli.py').read_text())
        self.cli.chmod(0o700)
        self.calls = self.base / 'calls.jsonl'
        self.config_path = self.base / 'config.jsonc'
        self.reports = self.base / 'results'
        self.env = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(self.home),
            'LANG': 'C', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONIOENCODING': 'utf-8', 'AUDIT_TEST_CALL_LOG': str(self.calls)}

    def prepare(self, mode, check=False, trust=False):
        self.calls.write_text('')
        value = {'mode': mode, 'git_mode': {'repository': str(self.alias),
                 'branches': ['master', 'test01'], 'baseline_branch': 'master'},
                 'folder_mode': {'path': str(self.folder)}, 'reports_dir': str(self.reports),
                 'project_description': 'Offline integration fixture',
                 'agent': {'backend': 'codex', 'executable': str(self.cli),
                           'expected_version': 'fixture-cli 1.0'}}
        self.config_path.write_text('// Read this configuration through the entry point.\n' + json.dumps(value))
        args = ['--config', str(self.config_path)]
        if check:
            args.append('--check')
        if trust:
            args.append('--trust-repository')
        return args

    def execute(self, mode, check=False, trust=False):
        return subprocess.run([sys.executable, '-B', str(ROOT / 'explain.py'),
            *self.prepare(mode, check, trust)], cwd=self.base, env=self.env,
            capture_output=True, text=True, timeout=30)

    def execute_with_mocked_euid(self, euid, mode, check=False, trust=False):
        args = self.prepare(mode, check, trust)
        out, err = io.StringIO(), io.StringIO()
        old_umask = os.umask(0o077)
        old_handler = signal.getsignal(signal.SIGTERM)
        try:
            with patch('sys.argv', ['explain.py', *args]), \
                    patch('explain.os.geteuid', return_value=euid), \
                    patch.object(Repository, 'check_ownership'), \
                    patch.dict(os.environ, self.env, clear=True), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main()
        finally:
            os.umask(old_umask)
            signal.signal(signal.SIGTERM, old_handler)
        return subprocess.CompletedProcess(args, code, out.getvalue(), err.getvalue())

    def assert_completed(self, result, mode, check, warning_euid):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.splitlines().count(ROOT_WARNING), int(warning_euid == 0))
        output = json.loads(result.stdout)
        self.assertEqual(output['status'], 'PREFLIGHT_OK' if check else 'COMPLETE')
        manifest_path = Path(output['manifest'])
        manifest = json.loads(manifest_path.read_text())
        snapshot = json.loads((manifest_path.parent / 'config.snapshot.json').read_text())
        self.assertEqual(snapshot['mode'], mode)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 2 if check else 9 if mode == 'git' else 5)
        self.assertIn('--version', calls[0]['args'])
        self.assertIn('--help', calls[1]['args'])
        stages = [call for call in calls if 'context' in call]
        self.assertEqual(len(stages), 0 if check else 7 if mode == 'git' else 3)
        for call in calls:
            self.assertEqual(call['euid'], os.geteuid())
            self.assertEqual(call['git_config_env'], {})
            self.assertFalse(any('safe.directory' in arg for arg in call['args']))
        for call in stages:
            self.assertEqual(call['args'][call['args'].index('--sandbox') + 1], 'read-only')
        if mode == 'git':
            self.assertEqual(snapshot['git_mode']['repository'], str(self.repo_path))
            self.assertEqual(self.repo.symbolic(), 'master')
            self.assertEqual(self.repo.head(), self.master)
            self.repo.clean()
            if not check:
                self.assertTrue(manifest['temporary_sources_removed'])

    def test_main_with_mocked_euids_completes_real_pipeline_in_both_modes(self):
        for euid in (0, 1000):
            for mode in ('git', 'folder'):
                for check in (False, True):
                    for trust in ((False, True) if mode == 'git' else (False,)):
                        with self.subTest(mocked_euid=euid, mode=mode, check=check, trust=trust), \
                                patch('explain.subprocess.run', wraps=subprocess.run) as run:
                            result = self.execute_with_mocked_euid(euid, mode, check, trust)
                            # Inspect calls made through main -> Runner -> Repository.
                            commands = [call.args[0] for call in run.call_args_list]
                            self.assertEqual(bool(commands), mode == 'git')
                            for command in commands:
                                settings = [arg for arg in command if arg.startswith('safe.directory=')]
                                self.assertEqual(settings, [])
                            self.assert_completed(result, mode, check, euid)

    def test_mocked_root_still_validates_cli_configuration(self):
        self.cli.unlink()
        for mode in ('git', 'folder'):
            for check in (False, True):
                with self.subTest(mode=mode, check=check):
                    result = self.execute_with_mocked_euid(0, mode, check)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('executable was not found', result.stderr)
                    self.assertIsNone(json.loads(result.stdout)['manifest'])
                self.assertFalse(self.reports.exists())

    def assert_actual_uid_matrix(self):
        for mode in ('git', 'folder'):
            for check in (False, True):
                with self.subTest(actual_euid=os.geteuid(), mode=mode, check=check):
                    self.assert_completed(self.execute(mode, check), mode, check, os.geteuid())

    @unittest.skipUnless(os.geteuid() == 0, 'Actual UID 0 required; no privilege escalation is attempted.')
    def test_cli_under_actual_root(self):
        self.assert_actual_uid_matrix()

    @unittest.skipIf(os.geteuid() == 0, 'Requires an actual non-root process.')
    def test_cli_under_actual_non_root(self):
        self.assert_actual_uid_matrix()

    def test_trusted_git_pipeline_preserves_config_files_and_agent_environment(self):
        # These are isolated fixture configs, never the user's real Git configs.
        global_config = self.home / '.gitconfig'
        system_config = self.base / 'system.gitconfig'
        global_config.write_text('[safe]\n\tdirectory = /unrelated/global\n')
        system_config.write_text('[safe]\n\tdirectory = /unrelated/system\n')
        self.env['GIT_CONFIG_SYSTEM'] = str(system_config)
        paths = [global_config, system_config, self.repo_path / '.git/config']
        before = {path: path.read_bytes() for path in paths}
        for check in (False, True):
            with self.subTest(check=check):
                result = self.execute('git', check, trust=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['status'], 'PREFLIGHT_OK' if check else 'COMPLETE')
                self.assertEqual(self.repo.symbolic(), 'master')
                self.assertEqual(self.repo.head(), self.master)
                self.repo.clean()
                self.assertEqual({path: path.read_bytes() for path in paths}, before)
                calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
                self.assertEqual(len(calls), 2 if check else 9)
                for call in calls:
                    self.assertEqual(call['git_config_env'], {'GIT_CONFIG_SYSTEM': str(system_config)})
                    self.assertFalse(any('safe.directory' in arg for arg in call['args']))

    def test_folder_rejects_trust_before_results_are_created(self):
        for euid in (0, 1000):
            for check in (False, True):
                with self.subTest(mocked_euid=euid, check=check):
                    result = self.execute_with_mocked_euid(euid, 'folder', check, trust=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('--trust-repository requires git mode', result.stderr)
                    self.assertIsNone(json.loads(result.stdout)['manifest'])
                    self.assertFalse(self.reports.exists())
                    self.assertEqual(self.calls.read_text(), '')
        result = self.execute('folder', trust=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('--trust-repository requires git mode', result.stderr)
        self.assertFalse(self.reports.exists())

    def test_trust_accepts_dirty_checkout(self):
        (self.repo_path / 'app.py').write_text('uncommitted change\n')
        for check in (False, True):
            result = self.execute('git', check, trust=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(json.loads(result.stdout)['status'], ('COMPLETE', 'PREFLIGHT_OK'))
            self.assertIn('working changes included', result.stderr)
            self.assertNotIn(OWNERSHIP_HINT, result.stderr)
            self.assertEqual(self.repo.symbolic(), 'master')
            self.assertEqual((self.repo_path / 'app.py').read_text(), 'uncommitted change\n')

    def test_help_has_no_root_permission_option(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'explain.py'), '--help'],
            env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('--allow-root', result.stdout)
        self.assertIn('--trust-repository', result.stdout)

    @unittest.skipUnless(os.geteuid() == 0, 'Real foreign-owned checkout requires UID 0 to create it as another UID; no sudo/chown is used.')
    def test_actual_root_rejects_foreign_owner_until_explicit_trust(self):
        try:
            owner = pwd.getpwnam('nobody')
        except KeyError:
            self.skipTest('No nobody account available to create a real foreign-owned fixture.')
        # The child creates its own checkout. No ownership or mode changes are used.
        script = '''import json, os, pathlib, subprocess, tempfile
base = pathlib.Path(tempfile.mkdtemp(prefix='audit-foreign-', dir='/tmp')).resolve()
repo = base / 'foreign checkout with spaces'
repo.mkdir()
def git(*args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE).decode().strip()
git('init', '-b', 'master')
git('config', 'user.email', 'test@example.invalid')
git('config', 'user.name', 'Fixture')
(repo / 'app.py').write_text('print(42)\\n')
git('add', 'app.py')
git('commit', '-m', 'base')
git('branch', 'test01')
print(json.dumps({'base': str(base), 'repo': str(repo), 'commit': git('rev-parse', 'HEAD'), 'uid': os.geteuid()}))
'''
        try:
            created = subprocess.run([sys.executable, '-B', '-c', script], cwd='/tmp',
                env={'PATH': self.env['PATH'], 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'},
                user=owner.pw_uid, group=owner.pw_gid, extra_groups=[],
                capture_output=True, text=True, timeout=30)
        except PermissionError as exc:
            self.skipTest(f'Cannot create checkout as a different UID: {exc}')
        self.assertEqual(created.returncode, 0, created.stderr)
        fixture = json.loads(created.stdout)
        self.addCleanup(shutil.rmtree, fixture['base'])
        self.repo_path = Path(fixture['repo'])
        self.assertNotEqual(self.repo_path.stat().st_uid, os.geteuid())
        self.assertEqual(fixture['uid'], owner.pw_uid)
        self.alias.unlink()
        self.alias.symlink_to(self.repo_path, target_is_directory=True)
        self.repo = Repository(self.repo_path, trust_repository=True)
        self.master = fixture['commit']
        for check in (False, True):
            with self.subTest(check=check):
                rejected = self.execute('git', check)
                self.assertEqual(rejected.returncode, 1, rejected.stderr)
                self.assertIn('ownership mismatch', rejected.stderr)
                self.assertIn('--trust-repository', rejected.stderr)
                self.assertEqual(self.calls.read_text(), '')
                self.assert_completed(self.execute('git', check, trust=True), 'git', check, 0)


if __name__ == '__main__':
    unittest.main()
