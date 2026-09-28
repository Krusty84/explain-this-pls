# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Compatibility regressions; the CI matrix supplies real Git builds.

Version shims test parsing/selection only, never claim to emulate an old Git.
Races and permission failures below are injected explicitly; root tests use OS UIDs.
"""
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_explain as fixtures
import test_startup as startup
import test_submodules as recursive
from explain import AuditError, GitRuntime, Repository, UnsafeRepository


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.real_git = str(Path(shutil.which('git')).resolve())

    def shim(self, version):
        target = self.base / 'checked-git'
        target.write_text('#!' + sys.executable + '\n' + f'''import json, os, sys
from pathlib import Path
with open({str(self.base / 'calls')!r}, 'a') as stream:
    stream.write(json.dumps({{'args': sys.argv[1:], 'cwd': os.getcwd(),
                             'env': dict(os.environ)}}) + '\\n')
if sys.argv[1:] == ['--version']:
    print({version!r})
else:
    os.execv({self.real_git!r}, [{self.real_git!r}, *sys.argv[1:]])
''')
        target.chmod(0o700)
        link = self.base / 'git'
        if not link.exists():
            link.symlink_to(target)
        return target

    def test_numeric_minimum_suffixes_and_unknown_versions(self):
        for version, accepted in [('2.34.1', True), ('2.34.1-1ubuntu1.15', True),
                ('2.34.1 (Apple Git-137.1)', True), ('2.54.0', True), ('2.100.0', True),
                ('2.34.0', False), ('2.9.9', False), ('2.33.99', False),
                ('unknown', False), ('2.34', False), ('2.34.1garbage', False)]:
            with self.subTest(version=version):
                target = self.shim('git version ' + version)
                with patch.dict(os.environ, {'PATH': str(self.base)}):
                    if accepted:
                        runtime = GitRuntime()
                        self.assertEqual(runtime.executable, str(target))
                        self.assertEqual(runtime.version_string, 'git version ' + version)
                    else:
                        with self.assertRaises(AuditError) as caught:
                            GitRuntime()
                        self.assertIn('2.34.1 is required', str(caught.exception))
                        self.assertIn(str(target), str(caught.exception))
                        self.assertIn(version, str(caught.exception))

    def test_checked_executable_is_pinned_and_version_probe_is_neutral(self):
        checkout = self.base / 'repo'
        recursive.init(checkout)
        target = self.shim('git version 2.34.1-test')
        poison = {'PATH': str(self.base) + os.pathsep + os.environ['PATH'],
                  'GIT_DIR': str(checkout / '.git'), 'GIT_CONFIG_COUNT': '1',
                  'GIT_CONFIG_KEY_0': 'safe.directory', 'GIT_CONFIG_VALUE_0': '*',
                  'SUDO_UID': '123', 'TMPDIR': str(checkout)}
        with patch.dict(os.environ, poison):
            repo = Repository(checkout)
        link = self.base / 'git'
        link.unlink()
        link.symlink_to('/nonexistent/git')
        with patch.dict(os.environ, {'PATH': '/nonexistent'}):
            repo.preflight(['master'])
        calls = [json.loads(line) for line in (self.base / 'calls').read_text().splitlines()]
        self.assertEqual(calls[0]['args'], ['--version'])
        self.assertEqual(sum(c['args'] == ['--version'] for c in calls), 1)
        self.assertNotIn(str(checkout), calls[0]['cwd'])
        self.assertFalse(Path(calls[0]['cwd']).exists())
        for call in calls:
            self.assertNotIn('GIT_DIR', call['env'])
            self.assertNotIn('GIT_CONFIG_COUNT', call['env'])
            self.assertNotIn('SUDO_UID', call['env'])
        self.assertEqual(repo.prefix[0], str(target))

    def test_trust_cannot_turn_a_literal_star_directory_into_a_wildcard(self):
        runtime = GitRuntime()
        self.addCleanup(runtime.close)
        with self.assertRaisesRegex(UnsafeRepository, 'exact safe.directory'):
            runtime.trust_config(self.base / '*')
        self.assertIsNone(runtime.temporary)


class HeadTests(unittest.TestCase):
    setUp = fixtures.RepoFixture.setUp
    tearDown = fixtures.RepoFixture.tearDown
    git = fixtures.RepoFixture.git

    def test_immediate_target_and_detached_commit(self):
        self.assertEqual(self.repo.symbolic_ref(), 'refs/heads/master')
        self.git('symbolic-ref', 'refs/heads/alias', 'refs/heads/master')
        self.git('symbolic-ref', 'HEAD', 'refs/heads/alias')
        self.assertEqual(self.git('symbolic-ref', 'HEAD').strip(), 'refs/heads/master')
        self.assertEqual(self.repo.symbolic_ref(), 'refs/heads/alias')
        self.assertEqual(self.repo.symbolic(), 'alias')
        refs = {str(p): p.read_bytes() for p in (self.repo_path / '.git/refs').rglob('*') if p.is_file()}
        pins = self.repo.preflight(['master', 'test01'])
        self.repo.checkout(pins['test01'])
        self.assertIsNone(self.repo.symbolic_ref())
        self.repo.restore('alias', self.master)
        self.assertEqual(self.repo.symbolic_ref(), 'refs/heads/alias')
        self.assertEqual(refs, {str(p): p.read_bytes() for p in (self.repo_path / '.git/refs').rglob('*') if p.is_file()})

    def test_invalid_missing_oversized_symlink_and_special_head(self):
        self.repo.preflight(['master'])
        head = self.repo.git_dir / 'HEAD'
        original = head.read_bytes()
        for raw in (b'', b'garbage\n', b'1234\n', b'0' * 40 + b'\n',
                    b'ref: ../escape\n', b'ref: refs/heads/x\nextra\n', b'ref: refs/heads/x\0y\n', b'x' * 4097):
            with self.subTest(raw=raw[:50]):
                head.write_bytes(raw)
                with self.assertRaisesRegex(AuditError, 'HEAD'):
                    self.repo.symbolic_ref()
        head.unlink()
        with self.assertRaisesRegex(UnsafeRepository, 'Cannot read HEAD'):
            self.repo.symbolic_ref()
        target = self.base / 'head-target'
        target.write_bytes(original)
        head.symlink_to(target)
        with self.assertRaisesRegex(UnsafeRepository, 'regular file'):
            self.repo.symbolic_ref()
        head.unlink()
        os.mkfifo(head)
        with self.assertRaisesRegex(UnsafeRepository, 'regular file'):
            self.repo.symbolic_ref()
        head.unlink()
        head.write_bytes(original)

    def test_head_replacement_metadata_race_and_unreadable_file(self):
        self.repo.preflight(['master'])
        head = self.repo.git_dir / 'HEAD'
        original = head.read_bytes()
        real_open, real_fstat = os.open, os.fstat
        def substitute(path, *args, **kwargs):
            if path == head:
                head.unlink()
                head.write_bytes(original)
            return real_open(path, *args, **kwargs)
        with patch('explain.os.open', side_effect=substitute):
            with self.assertRaisesRegex(UnsafeRepository, 'HEAD changed while opening'):
                self.repo.symbolic_ref()
        def mutate(fd):
            info = real_fstat(fd)
            head.write_bytes(original)
            return info
        with patch('explain.os.fstat', side_effect=mutate):
            with self.assertRaisesRegex(UnsafeRepository, 'HEAD changed while reading'):
                self.repo.symbolic_ref()
        with patch('explain.os.open', side_effect=PermissionError('injected permission denial')):
            with self.assertRaisesRegex(UnsafeRepository, 'Cannot read HEAD.*permission denial'):
                self.repo.symbolic_ref()

    def test_sha256_detached_head_and_restore(self):
        path = self.base / 'sha256'
        path.mkdir()
        recursive.git(path, 'init', '-b', 'master', '--object-format=sha256')
        recursive.identity(path)
        (path / 'app.py').write_text('sha256\n')
        recursive.git(path, 'add', '.')
        recursive.git(path, 'commit', '-m', 'sha256')
        recursive.git(path, 'switch', '--detach', 'HEAD')
        repo = Repository(path)
        pins = repo.preflight(['master'])
        self.assertEqual(len(repo.head()), 64)
        self.assertIsNone(repo.symbolic_ref())
        repo.checkout(pins['master'])
        self.assertTrue(repo.restore(None, pins['master'])['restored'])

    def test_transport_denial_without_relying_on_no_lazy_fetch(self):
        sentinel = self.base / 'transport-called'
        helper = self.base / 'local-transport'
        helper.write_text('#!' + sys.executable + '\nfrom pathlib import Path\n' +
                          f'Path({str(sentinel)!r}).touch()\n')
        helper.chmod(0o700)
        self.git('config', 'protocol.ext.allow', 'always')
        self.git('config', 'protocol.file.allow', 'always')
        self.git('config', 'protocol.ssh.allow', 'always')
        self.git('config', 'core.sshCommand', str(helper))
        self.repo.env.pop('GIT_NO_LAZY_FETCH')
        # These test probes use a local sentinel or local path, never a network
        # helper. They exercise the transport gate independently of preflight.
        for url in (f'ext::{helper}', str(self.repo_path), 'ssh://example.invalid/repo'):
            with self.subTest(url=url):
                with self.assertRaisesRegex(AuditError, 'transport .* not allowed'):
                    self.repo.git('ls-remote', url)
                self.assertFalse(sentinel.exists())


class CompatibilityCLI(unittest.TestCase):
    setUp = startup.StartupCLIIntegrationTests.setUp
    tearDown = fixtures.RepoFixture.tearDown
    git = fixtures.RepoFixture.git
    prepare = startup.StartupCLIIntegrationTests.prepare
    execute = startup.StartupCLIIntegrationTests.execute

    def test_fsmonitor_hooks_and_boolean_commands_never_run_and_stale_index_is_ignored(self):
        sentinel = self.base / 'fsmonitor-called'
        hook = self.base / 'fsmonitor-hook'
        hook.write_text('#!' + sys.executable + '\n' +
            f'from pathlib import Path\nimport sys\nPath({str(sentinel)!r}).touch()\n'
            'sys.stdout.buffer.write(b"unchanged-token\\0")\n')
        hook.chmod(0o700)
        for name in ('false', 'true'):
            shutil.copy(hook, self.base / name)
        self.env['PATH'] = str(self.base) + os.pathsep + self.env['PATH']
        self.git('config', 'core.fsmonitor', str(hook))
        self.git('config', 'core.fsmonitorHookVersion', '2')
        # Seed a real FSMN extension whose hook claims every tracked file is valid.
        self.git('update-index', '--fsmonitor')
        self.git('update-index', '--fsmonitor-valid', 'app.py')
        self.assertIn(b'FSMN', (self.repo_path / '.git/index').read_bytes())
        self.assertTrue(sentinel.exists(), 'The test hook must work before testing its suppression')
        sentinel.unlink()
        original = (self.repo_path / 'app.py').read_bytes()
        for value in (str(hook), 'false', 'true'):
            with self.subTest(configured_fsmonitor=value):
                self.git('config', 'core.fsmonitor', value)
                index = (self.repo_path / '.git/index').read_bytes()
                config = (self.repo_path / '.git/config').read_bytes()
                self.assertEqual(self.execute('git', check=True).returncode, 0)
                (self.repo_path / 'app.py').write_bytes(b'changed despite the cached monitor state\n')
                result = self.execute('git', check=True)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('Working tree must have no modifications', result.stderr)
                self.assertFalse(sentinel.exists())
                self.assertEqual(index, (self.repo_path / '.git/index').read_bytes())
                self.assertEqual(config, (self.repo_path / '.git/config').read_bytes())
                (self.repo_path / 'app.py').write_bytes(original)

    def test_old_and_unparseable_versions_fail_before_checkout_inspection(self):
        shim_dir = self.base / 'bin'
        shim_dir.mkdir()
        shim = shim_dir / 'git'
        head = self.repo_path / '.git/HEAD'
        head.write_text('broken HEAD\n')
        self.env['PATH'] = str(shim_dir)
        for version in ('git version 2.34.0', 'not a git version'):
            shim.write_text('#!' + sys.executable + '\nimport sys\n'
                           'assert sys.argv[1:] == ["--version"]\n' + f'print({version!r})\n')
            shim.chmod(0o700)
            result = self.execute('git', check=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn(version, result.stderr)
            self.assertIn(str(shim), result.stderr)
            self.assertNotIn('broken HEAD', result.stderr)
            self.assertFalse(self.reports.exists())


class RecursiveCompatibilityCLI(recursive.RecursiveFixture, unittest.TestCase):
    def test_missing_local_objects_and_promisors_fail_without_fetch_or_config_writes(self):
        path = self.paths[recursive.LEAF]
        sha = recursive.git(path, 'rev-parse', 'topic:app.py')
        gd = Path(recursive.git(path, 'rev-parse', '--absolute-git-dir'))
        (gd / 'objects' / sha[:2] / sha[2:]).unlink()
        sentinel = self.base / 'transport-called'
        helper = self.base / 'transport'
        helper.write_text('#!' + sys.executable + '\nfrom pathlib import Path\n' +
                          f'Path({str(sentinel)!r}).touch()\n')
        helper.chmod(0o700)
        for setting in (None, 'remote.origin.promisor', 'remote.origin.partialclonefilter', 'extensions.partialclone'):
            with self.subTest(setting=setting):
                if setting:
                    recursive.git(path, 'config', setting, 'true' if setting.endswith('.promisor') else
                                  'blob:none' if setting.endswith('filter') else 'origin')
                recursive.git(path, 'config', 'remote.origin.url', f'ext::{helper}')
                recursive.git(path, 'config', 'protocol.ext.allow', 'always')
                before = self.state()
                for check in (True, False):
                    result, manifest = self.execute(check=check)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(recursive.LEAF, result.stderr)
                    self.assertIn('Required commit:', result.stderr)
                    if setting:
                        self.assertIn('Partial-clone/promisor', result.stderr)
                    self.assertFalse(sentinel.exists())
                    self.assertEqual(before, self.state())
                    self.assertEqual(self.calls.read_text(), '')
                if setting:
                    recursive.git(path, 'config', '--unset', setting)

    def test_symbolic_chain_restores_all_nodes_without_moving_refs(self):
        for relative, path in self.paths.items():
            target = 'master' if relative == '.' else 'original/start'
            recursive.git(path, 'symbolic-ref', 'refs/heads/alias', 'refs/heads/' + target)
            recursive.git(path, 'symbolic-ref', 'HEAD', 'refs/heads/alias')
        before = self.state()
        for failure in (None, 'error', 'interrupt'):
            with self.subTest(failure=failure):
                if failure:
                    self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'review', 'path': str(self.path), 'kind': failure})
                result, manifest = self.execute()
                self.assertEqual(result.returncode, 0 if failure is None else 130 if failure == 'interrupt' else 1, result.stderr)
                self.assert_original(before)
                self.assertTrue(manifest['restoration']['restored'])
                self.assertEqual(manifest['original_checkout']['branch'], 'alias')
                for relative, state in manifest['original_hierarchy'].items():
                    self.assertEqual(state['ref'], 'refs/heads/alias')
                    after = self.state()[relative]['files']
                    self.assertEqual({k: v for k, v in before[relative]['files'].items() if k.startswith('refs/')},
                                     {k: v for k, v in after.items() if k.startswith('refs/')})

    def test_private_trust_configs_are_scoped_and_removed_on_success_and_failure(self):
        real_git = shutil.which('git')
        bindir = self.base / 'bin'
        bindir.mkdir()
        log = self.base / 'git-calls'
        shim = bindir / 'git'
        shim.write_text('#!' + sys.executable + '\n' + f'''import json, os, stat, sys
from pathlib import Path
config = os.environ.get('GIT_CONFIG_GLOBAL')
if config and 'archaudit-git-trust-' in config:
    path = Path(config)
    with open({str(log)!r}, 'a') as stream:
        stream.write(json.dumps({{'args': sys.argv[1:], 'file': str(path), 'data': path.read_text(),
            'file_mode': stat.S_IMODE(path.stat().st_mode),
            'dir_mode': stat.S_IMODE(path.parent.stat().st_mode)}}) + '\\n')
os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
''')
        shim.chmod(0o700)
        self.env['PATH'] = str(bindir) + os.pathsep + self.env['PATH']
        before = self.state()
        for failure in (False, True):
            if failure:
                self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'study', 'kind': 'error', 'path': str(self.path)})
            result, manifest = self.execute(trust=True)
            self.assertEqual(result.returncode, 1 if failure else 0, result.stderr)
            self.assert_original(before)
            self.assertEqual(manifest['git']['executable'], str(shim))
            for call in map(json.loads, log.read_text().splitlines()):
                self.assertEqual((call['file_mode'], call['dir_mode']), (0o600, 0o700))
                self.assertFalse(Path(call['file']).parent.exists())
                path = call['args'][call['args'].index('-C') + 1]
                self.assertEqual(call['data'], '[safe]\n\tdirectory =\n\tdirectory = "' + path + '"\n')
            calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
            self.assertTrue(all(c['git_config_env'] == {} for c in calls))

    def test_trust_config_escapes_paths_and_child_runtime_is_shared(self):
        renamed = self.base / 'проект "quoted" \\ tab\t #'
        self.path.rename(renamed)
        repo = Repository(renamed, trust_repository=True)
        self.addCleanup(repo.close)
        # This name is a checkout name, not a submodule path (.gitmodules has
        # stricter path rules). All children continue to use validated gitfiles.
        repo.preflight(['master', 'topic'])
        for node in repo.nodes.values():
            self.assertIs(node.runtime, repo.runtime)
            self.assertEqual(node.prefix[0], repo.runtime.executable)
            self.assertEqual(node.git('config', '--null', '--get-all', 'safe.directory'),
                             b'\0' + str(node.path).encode() + b'\0')
            config = Path(node.env['GIT_CONFIG_GLOBAL'])
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config.parent.stat().st_mode), 0o700)
            self.assertFalse(config.is_relative_to(renamed))


@unittest.skipUnless(os.geteuid() == 0, 'Actual UID 0 required; isolated CI container only, no mocked owners.')
class RootOwnershipCompatibility(recursive.RecursiveFixture, unittest.TestCase):
    def foreign_copy(self, source):
        try:
            owner = pwd.getpwnam('nobody')
        except KeyError:
            self.skipTest('No nobody account to create foreign-owned fixture.')
        payload = {'file': source.is_file(), 'entries': {}}
        if source.is_file():
            payload['entries']['.'] = source.read_bytes().hex()
        else:
            for path in source.rglob('*'):
                payload['entries'][str(path.relative_to(source))] = path.read_bytes().hex() if path.is_file() else None
        script = '''import json, pathlib, sys, tempfile
data = json.load(sys.stdin)
base = pathlib.Path(tempfile.mkdtemp(prefix='audit-foreign-', dir='/tmp'))
target = base / 'copy'
if data['file']:
    target.write_bytes(bytes.fromhex(data['entries']['.']))
else:
    target.mkdir()
    for name, value in data['entries'].items():
        path = target / name
        if value is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes.fromhex(value))
print(base)
'''
        try:
            result = subprocess.run([sys.executable, '-B', '-c', script], cwd='/tmp',
                input=json.dumps(payload), text=True, capture_output=True, timeout=30,
                user=owner.pw_uid, group=owner.pw_gid, extra_groups=[], env={'PATH': self.env['PATH']})
        except PermissionError as exc:
            self.skipTest(f'Cannot create fixture as another UID: {exc}')
        self.assertEqual(result.returncode, 0, result.stderr)
        base = Path(result.stdout.strip())
        self.addCleanup(shutil.rmtree, base)
        self.assertEqual((base / 'copy').stat().st_uid, owner.pw_uid)
        return base / 'copy'

    def test_root_checks_worktree_git_entry_and_actual_gitdir_owners_before_git(self):
        child = self.paths[recursive.CHILD]
        child_gitdir = Path(recursive.git(child, 'rev-parse', '--absolute-git-dir'))
        for target in (self.path, self.path / '.git', child, child / '.git', child_gitdir):
            with self.subTest(foreign_path=target):
                foreign = self.foreign_copy(target)
                original = self.base / 'saved-owner-fixture'
                target.rename(original)
                foreign.rename(target)
                try:
                    before = {str(p): p.read_bytes() for p in self.path.rglob('*') if p.is_file()}
                    for check in (True, False):
                        # The real runner is always UID 0; it never changes user.
                        with patch('explain.subprocess.run', wraps=subprocess.run) as commands:
                            with self.assertRaisesRegex(AuditError, 'ownership mismatch'):
                                Repository(self.path).preflight(['master', 'topic'])
                        denied_checkout = self.path if target in (self.path, self.path / '.git') else child
                        self.assertFalse(any(call.args[0][call.args[0].index('-C') + 1] == str(denied_checkout)
                                             for call in commands.call_args_list))
                        result, manifest = self.execute(check=check)
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn('ownership mismatch', result.stderr)
                        self.assertIn(str(target), result.stderr)
                        self.assertEqual(self.calls.read_text(), '')
                        self.assertEqual(before, {str(p): p.read_bytes() for p in self.path.rglob('*') if p.is_file()})
                        result, manifest = self.execute(check=check, trust=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        if check:
                            self.assertEqual(before, {str(p): p.read_bytes() for p in self.path.rglob('*') if p.is_file()})
                        self.assertEqual((target.stat().st_uid), pwd.getpwnam('nobody').pw_uid)
                finally:
                    target.rename(foreign)
                    original.rename(target)

    @unittest.skipUnless(os.environ.get('AUDIT_GIT_BUILD'), 'Behavioral package probe is enabled by the isolated CI Git matrix.')
    def test_actual_build_safe_directory_behavior_and_scope(self):
        foreign = self.foreign_copy(self.path)
        # Embedded root .git supports discovery after moving the fixture.
        runtime = GitRuntime()
        self.addCleanup(runtime.close)
        env = runtime.env.copy()
        def probe(path, *options):
            return subprocess.run([runtime.executable, *options, '-C', str(path), 'rev-parse', '--show-toplevel'],
                                  env=env, capture_output=True, text=True, timeout=30)
        untrusted = probe(foreign)
        command_scope = probe(foreign, '-c', 'safe.directory=' + str(foreign))
        kind = os.environ['AUDIT_GIT_BUILD']
        if kind == 'upstream-2.34.1':
            self.assertEqual(untrusted.returncode, 0)
        else:
            self.assertRegex(untrusted.stderr, 'dubious ownership|unsafe repository')
        if kind == 'jammy-command-scope-broken':
            self.assertNotEqual(command_scope.returncode, 0)
            self.assertRegex(command_scope.stderr, 'dubious ownership|unsafe repository')
        else:
            self.assertEqual(command_scope.returncode, 0, command_scope.stderr)
        env['GIT_CONFIG_GLOBAL'] = runtime.trust_config(foreign)
        trusted = probe(foreign)
        self.assertEqual(trusted.returncode, 0, trusted.stderr)
        other = self.foreign_copy(self.path)
        if kind != 'upstream-2.34.1':
            self.assertRegex(probe(other).stderr, 'dubious ownership|unsafe repository')
        # Runner's check is mandatory even on upstream without native protection.
        with self.assertRaisesRegex(AuditError, 'ownership mismatch'):
            Repository(other).head()
        print(json.dumps({'git_build': kind, 'executable': runtime.executable,
                          'version': runtime.version_string, 'untrusted_rc': untrusted.returncode,
                          'command_scope_rc': command_scope.returncode, 'global_scope_rc': trusted.returncode}))


if __name__ == '__main__':
    unittest.main()
