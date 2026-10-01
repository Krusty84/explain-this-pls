# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Real, offline recursive checkouts; CLI tests run the public entry point."""
from __future__ import annotations
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from explain import AuditError, Repository, Runner, UnsafeRepository, load_config

ROOT = Path(__file__).resolve().parents[1]
CHILD = 'vendor/модуль with spaces'
LEAF = CHILD + '/nested/深い child'


def git(path, *args):
    env = {k: os.environ[k] for k in ('PATH', 'LANG') if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
    return subprocess.check_output(['git', '-C', str(path), *args], env=env, stderr=subprocess.PIPE).decode().strip()


def init(path):
    path.mkdir(parents=True)
    git(path, 'init', '-b', 'master')
    identity(path)
    (path / 'app.py').write_text('original\n')
    (path / '.gitignore').write_text('ignored.txt\n')
    git(path, 'add', '.')
    git(path, 'commit', '-m', 'initial')


def identity(path):
    git(path, 'config', 'user.name', 'Fixture')
    git(path, 'config', 'user.email', 'fixture@example.invalid')


class RecursiveFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        leaf_source, child_source = self.base / 'leaf-origin', self.base / 'child-origin'
        init(leaf_source)
        init(child_source)
        git(child_source, '-c', 'protocol.file.allow=always', 'submodule', 'add', '--name',
            'nested logical name', str(leaf_source), 'nested/深い child')
        git(child_source, 'commit', '-am', 'nested gitlink')
        self.path = self.base / 'project'
        init(self.path)
        git(self.path, '-c', 'protocol.file.allow=always', 'submodule', 'add', '--name',
            'logical module', str(child_source), CHILD)
        git(self.path, '-c', 'protocol.file.allow=always', 'submodule', 'update', '--init', '--recursive')
        git(self.path, 'commit', '-am', 'submodule gitlink')
        self.paths = {'.': self.path, CHILD: self.path / CHILD, LEAF: self.path / LEAF}
        for relative, path in self.paths.items():
            identity(path)
            if relative != '.':
                git(path, 'switch', '-c', 'original/start')
        self.expected = {'master': self.contents()}
        for relative in (LEAF, CHILD, '.'):
            path = self.paths[relative]
            git(path, 'switch', '-c', 'topic')
            (path / 'app.py').write_text(f'topic: {relative}\n')
            git(path, 'add', '.')
            git(path, 'commit', '-m', 'new snapshot')
        self.expected['topic'] = self.contents()
        for relative, path in self.paths.items():
            git(path, 'switch', 'master' if relative == '.' else 'original/start')
        self.repo = Repository(self.path)
        self.home = self.base / 'home'
        self.home.mkdir()
        (self.home / 'audit-profile.json').write_text('{"model":"configured-model"}')
        self.cli = self.base / 'fake-cli'
        self.cli.write_text('#!' + sys.executable + '\n' + ('import sys; sys.path.insert(0, ' + repr(str(ROOT / 'tests/fixtures')) + ')' + '\n' + (ROOT / 'tests/fixtures/fake_cli.py').read_text()))
        self.cli.chmod(0o700)
        self.calls = self.base / 'calls.jsonl'
        self.expected_file = self.base / 'expected.json'
        self.expected_file.write_text(json.dumps(self.expected))
        self.config_path = self.base / 'config.jsonc'
        self.config = {'repository': str(self.path), 'branches': ['master', 'topic'],
            'baseline_branch': 'master', 'reports_dir': str(self.base / 'reports'),
            'project_description': 'Recursive fixture',
            'agent': {'backend': 'codex', 'executable': str(self.cli)}, 'continue_on_error': False}
        self.env = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(self.home),
            'PYTHONIOENCODING': 'utf-8', 'AUDIT_TEST_CALL_LOG': str(self.calls),
            'AUDIT_TEST_EXPECTED': str(self.expected_file)}

    def contents(self):
        return {p: {'commit': git(path, 'rev-parse', 'HEAD'), 'content': (path / 'app.py').read_text()}
                for p, path in self.paths.items()}

    def state(self):
        result = {}
        for relative, path in self.paths.items():
            gd = Path(git(path, 'rev-parse', '--absolute-git-dir'))
            result[relative] = {'commit': git(path, 'rev-parse', 'HEAD'),
                'gitfile': (path / '.git').read_bytes() if (path / '.git').is_file() else None,
                'files': {str(p.relative_to(gd)): p.read_bytes() for p in gd.rglob('*') if p.is_file()},
                'source': {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*')
                           if p.is_file() and '.git' not in p.relative_to(path).parts}}
        return result

    def write_config(self):
        self.config_path.write_text('// Full public CLI configuration\n' + json.dumps(self.config))

    def execute(self, check=False, trust=False):
        self.write_config()
        self.calls.write_text('')
        args = [sys.executable, '-B', str(ROOT / 'explain.py'), '--config', str(self.config_path)]
        if check:
            args.append('--check')
        if trust:
            args.append('--trust-repository')
        result = subprocess.run(args, cwd=self.base, env=self.env, capture_output=True, text=True, timeout=90)
        summary = json.loads(result.stdout)
        manifest = json.loads(Path(summary['manifest']).read_text())
        return result, manifest

    def assert_original(self, before):
        for relative, path in self.paths.items():
            self.assertEqual(git(path, 'rev-parse', 'HEAD'), before[relative]['commit'])
            gd = Path(git(path, 'rev-parse', '--absolute-git-dir'))
            self.assertEqual((gd / 'HEAD').read_bytes(), before[relative]['files']['HEAD'])
            self.assertEqual((path / 'app.py').read_bytes(), before[relative]['source']['app.py'])
            self.assertEqual((gd / 'config').read_bytes(), before[relative]['files']['config'])


class RecursiveCLITests(RecursiveFixture, unittest.TestCase):
    def test_full_cli_reads_each_snapshot_for_every_backend_and_restores(self):
        before = self.state()
        for backend in ('codex', 'claude-code', 'opencode'):
            with self.subTest(backend=backend):
                self.config['agent']['backend'] = backend
                self.env['OPENCODE_CONFIG_CONTENT'] = '{"provider":{"custom":{"options":{"baseURL":"https://example.invalid"}}}}'
                result, manifest = self.execute()
                if backend == 'opencode':
                    # Prompt-only transport was retired, so this legacy CLI
                    # fixture must stop before reading any source snapshot.
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(manifest['status'], 'FAILED')
                    self.assert_original(before)
                    calls = [json.loads(s) for s in self.calls.read_text().splitlines()]
                    self.assertFalse(any('context' in c for c in calls))
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(manifest['status'], 'COMPLETE')
                self.assertTrue(manifest['restoration']['restored'])
                self.assert_original(before)
                calls = [json.loads(s) for s in self.calls.read_text().splitlines()]
                stages = [c for c in calls if 'context' in c]
                self.assertEqual(len(stages), 5)
                for call in stages[:-1]:
                    self.assertEqual(call['observed'], self.expected[call['context']['branch']])
                delta = stages[-1]['context']['git_deltas']['topic']['submodule_changes']
                self.assertEqual({d['path'] for d in delta}, {CHILD, LEAF})
                for item in delta:
                    self.assertEqual(item['baseline_commit'], self.expected['master'][item['path']]['commit'])
                    self.assertEqual(item['branch_commit'], self.expected['topic'][item['path']]['commit'])

    def test_check_is_byte_for_byte_read_only_and_does_not_call_model(self):
        before = self.state()
        result, manifest = self.execute(check=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(manifest['status'], 'PREFLIGHT_OK')
        self.assertEqual(before, self.state())
        self.assertEqual(len(manifest['snapshot_plans']['topic']['submodules']), 2)
        self.assertEqual(manifest['switch_journal'], [])
        self.assertTrue(all('context' not in json.loads(s) for s in self.calls.read_text().splitlines()))

    def test_detached_original_heads_and_agent_failure_restore(self):
        for path in self.paths.values():
            git(path, 'switch', '--detach', 'HEAD')
        before = self.state()
        result, manifest = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_original(before)
        for kind, code in (('error', 2), ('interrupt', 130)):
            with self.subTest(kind=kind):
                self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'review', 'kind': kind, 'path': str(self.path)})
                result, manifest = self.execute()
                self.assertEqual(result.returncode, code, result.stderr)
                summary = json.loads(result.stdout)
                self.assertEqual(summary['final_report'], manifest['final_report'])
                self.assertTrue(summary['has_usable_material'])
                self.assertTrue(manifest['restoration']['restored'])
                self.assert_original(before)

    def test_integrity_failure_is_fatal_even_with_continue_on_error(self):
        self.config['continue_on_error'] = True
        self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'study', 'kind': 'file', 'path': str(self.paths[LEAF])})
        result, manifest = self.execute()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(manifest['status'], 'FAILED')
        self.assertFalse(manifest['restoration']['restored'])
        self.assertEqual(manifest['restoration']['node'], LEAF)
        self.assertEqual(len(manifest['branches']), 1)
        self.assertEqual((self.paths[LEAF] / 'app.py').read_text(), 'external modification\n')

    def test_same_sha_attached_head_is_detected(self):
        self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'review', 'kind': 'attach', 'path': str(self.paths[CHILD])})
        result, manifest = self.execute()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(manifest['restoration']['node'], CHILD)
        self.assertEqual(git(self.paths[CHILD], 'symbolic-ref', 'HEAD'), 'refs/heads/external-branch')

    def test_compare_modification_of_nested_metadata_is_preserved(self):
        self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'compare', 'kind': 'metadata', 'path': str(self.paths[LEAF])})
        result, manifest = self.execute()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(manifest['restoration']['node'], LEAF)
        self.assertEqual(git(self.paths[LEAF], 'config', '--get', 'external.changed'), 'true')

    def test_cli_check_cannot_modify_nested_sources(self):
        self.env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'check', 'kind': 'file', 'path': str(self.paths[LEAF])})
        result, manifest = self.execute(check=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(manifest['status'], 'FAILED')
        self.assertEqual(manifest['switch_journal'], [])
        self.assertIn(LEAF, ' '.join(manifest['errors']))

    def test_hooks_and_custom_update_are_never_executed(self):
        sentinel = self.base / 'hook-ran'
        for relative, path in self.paths.items():
            gd = Path(git(path, 'rev-parse', '--absolute-git-dir'))
            hook = gd / 'hooks/post-checkout'
            hook.write_text(f'#!/bin/sh\ntouch "{sentinel}"\n')
            hook.chmod(0o700)
            name = 'logical module' if relative == '.' else 'nested logical name'
            git(path, 'config', f'submodule.{name}.update', f'!touch "{sentinel}"')
        result, manifest = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists())

    def test_embedded_git_directory_is_supported(self):
        path = self.paths[LEAF]
        gd = Path(git(path, 'rev-parse', '--absolute-git-dir'))
        (path / '.git').unlink()
        shutil.move(str(gd), str(path / '.git'))
        git(path, '--git-dir', str(path / '.git'), 'config', '--unset', 'core.worktree')
        result, manifest = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(manifest['original_hierarchy'][LEAF]['git_dir'], str(path / '.git'))

    def test_embedded_parent_with_gitfile_child_is_supported(self):
        parent, leaf = self.paths[CHILD], self.paths[LEAF]
        gd = Path(git(parent, 'rev-parse', '--absolute-git-dir'))
        (parent / '.git').unlink()
        shutil.move(str(gd), str(parent / '.git'))
        git(parent, '--git-dir', str(parent / '.git'), 'config', '--unset', 'core.worktree')
        leaf_gd = parent / '.git/modules/nested logical name'
        (leaf / '.git').write_text('gitdir: ' + str(leaf_gd) + '\n')
        git(leaf, '--git-dir', str(leaf_gd), 'config', 'core.worktree', str(leaf))
        result, manifest = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(manifest['original_hierarchy'][LEAF]['git_dir'], str(leaf_gd))

    def test_cli_partial_switch_failure_and_signal_restore_and_record_journal(self):
        before = self.state()
        shim_dir = self.base / 'git-shim'
        shim_dir.mkdir()
        real_git = shutil.which('git')
        self.env['PATH'] = str(shim_dir) + os.pathsep + self.env['PATH']
        for relative, after in ((CHILD, False), (LEAF, True)):
            with self.subTest(node=relative, after=after):
                shim = shim_dir / 'git'
                shim.write_text('#!' + sys.executable + '\n' + f'''import os, signal, subprocess, sys
args = sys.argv[1:]
if ('switch' in args and args[args.index('-C') + 1] == {str(self.paths[relative])!r}
        and {self.expected['topic'][relative]['commit']!r} in args):
    if {after!r}:
        subprocess.run([{real_git!r}, *args], check=True)
        os.kill(os.getppid(), signal.SIGTERM)
        sys.exit(0)
    print('injected partial switch failure', file=sys.stderr)
    sys.exit(19)
os.execv({real_git!r}, [{real_git!r}, *args])
''')
                shim.chmod(0o700)
                result, manifest = self.execute()
                self.assertEqual(result.returncode, 130 if after else 1, result.stderr)
                self.assertTrue(manifest['restoration']['restored'])
                self.assert_original(before)
                attempts = [item for item in manifest['switch_journal'] if item['path'] == relative
                            and item['target']['commit'] == self.expected['topic'][relative]['commit']]
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0]['status'], 'COMPLETED' if after else 'UNCHANGED')


class RecursivePreflightTests(RecursiveFixture, unittest.TestCase):
    def test_trust_uses_only_each_verified_canonical_path_without_config_writes(self):
        before = self.state()
        repo = Repository(self.path, trust_repository=True)
        self.addCleanup(repo.close)
        repo.preflight(['master', 'topic'])
        for node in repo.nodes.values():
            self.assertEqual(node.git('config', '--get-all', 'safe.directory'),
                             ('\n' + str(node.path) + '\n').encode())
            self.assertNotIn('GIT_CONFIG_COUNT', node.env)
        self.assertEqual(before, self.state())

    def reject_unchanged(self, pattern):
        before = self.state()
        with self.assertRaisesRegex(AuditError, pattern):
            self.repo.preflight(['master', 'topic'])
        self.assertEqual(before, self.state())

    def test_dirty_files_in_each_node_including_ignore_all(self):
        for relative, path in self.paths.items():
            git(path, 'config', 'submodule.logical module.ignore', 'all')
        for relative, path in self.paths.items():
            for kind in ('tracked', 'staged', 'untracked', 'ignored'):
                with self.subTest(node=relative, kind=kind):
                    filename = 'app.py' if kind in ('tracked', 'staged') else 'ignored.txt' if kind == 'ignored' else 'new.txt'
                    file = path / filename
                    old = file.read_bytes() if file.exists() else None
                    file.write_text('dirty\n')
                    if kind == 'staged':
                        git(path, 'add', filename)
                    self.reject_unchanged('Working tree must have no modifications')
                    if old is None:
                        file.unlink()
                    else:
                        file.write_bytes(old)
                    if kind == 'staged':
                        git(path, 'add', filename)

    def test_uninitialized_child_has_path_sha_and_no_switch(self):
        (self.paths[LEAF] / '.git').unlink()
        result, manifest = self.execute(check=True)
        self.assertEqual(result.returncode, 1)
        error = ' '.join(manifest['errors'])
        self.assertIn(LEAF, error)
        self.assertIn(self.expected['master'][LEAF]['commit'], error)
        self.assertIn('not initialized', error)
        self.assertEqual(git(self.path, 'symbolic-ref', 'HEAD'), 'refs/heads/master')

    def test_gitlink_index_change_and_initial_head_mismatch_ignore_all(self):
        parent, leaf = self.paths[CHILD], self.paths[LEAF]
        git(parent, 'config', 'submodule.nested logical name.ignore', 'all')
        git(parent, 'update-index', '--cacheinfo', '160000', self.expected['topic'][LEAF]['commit'], 'nested/深い child')
        self.reject_unchanged('Working tree must have no modifications')
        git(parent, 'update-index', '--cacheinfo', '160000', self.expected['master'][LEAF]['commit'], 'nested/深い child')
        git(leaf, 'switch', 'topic')
        self.reject_unchanged('Initial HEAD .* does not match gitlink')

    def test_unsafe_committed_paths_and_reused_gitfile_are_rejected(self):
        parent = self.paths[CHILD]
        git(self.path, 'switch', 'topic')
        git(parent, 'switch', '--detach', 'topic')
        git(parent, 'config', '-f', '.gitmodules', 'submodule.nested logical name.path', '../escape')
        git(parent, 'commit', '-am', 'unsafe committed path')
        git(self.path, 'commit', '-am', 'unsafe child snapshot')
        git(self.path, 'switch', 'master')
        git(parent, 'switch', 'original/start')
        self.reject_unchanged('Unsafe submodule path or name')
        (self.paths[LEAF] / '.git').write_text('gitdir: ' + git(parent, 'rev-parse', '--absolute-git-dir') + '\n')
        with self.assertRaisesRegex(AuditError, 'Unexpected external Git directory'):
            self.repo.preflight(['master'])

    def test_missing_last_branch_commit_is_rejected_without_any_writes(self):
        sha = self.expected['topic'][LEAF]['commit']
        gd = Path(git(self.paths[LEAF], 'rev-parse', '--absolute-git-dir'))
        (gd / 'objects' / sha[:2] / sha[2:]).unlink()
        before = self.state()
        result, manifest = self.execute(check=True)
        self.assertEqual(result.returncode, 1)
        error = ' '.join(manifest['errors'])
        self.assertIn('topic', error)
        self.assertIn(LEAF, error)
        self.assertIn(sha, error)
        self.assertEqual(before, self.state())
        self.assertEqual(self.calls.read_text(), '')

    def test_missing_blob_and_promisor_never_fetch(self):
        path = self.paths[LEAF]
        sha = git(path, 'rev-parse', 'topic:app.py')
        gd = Path(git(path, 'rev-parse', '--absolute-git-dir'))
        (gd / 'objects' / sha[:2] / sha[2:]).unlink()
        sentinel = self.base / 'fetched'
        git(path, 'config', 'remote.origin.promisor', 'true')
        git(path, 'config', 'remote.origin.url', f'ext::touch {sentinel}')
        git(path, 'config', 'protocol.ext.allow', 'always')
        self.reject_unchanged('required SHA')
        self.assertFalse(sentinel.exists())

    def test_structure_changes_in_last_branch_fail_before_switch(self):
        parent = self.paths[CHILD]
        original_topic = git(parent, 'rev-parse', 'topic')
        root_topic = git(self.path, 'rev-parse', 'topic')
        for kind in ('delete', 'rename', 'add', 'move'):
            with self.subTest(kind=kind):
                git(self.path, 'switch', 'topic')
                git(parent, 'switch', '--detach', original_topic)
                relative = 'nested/深い child'
                if kind in ('delete', 'move'):
                    git(parent, 'update-index', '--force-remove', '--', relative)
                    git(parent, 'config', '-f', '.gitmodules', '--remove-section', 'submodule.nested logical name')
                if kind in ('add', 'move'):
                    git(parent, 'config', '-f', '.gitmodules', 'submodule.new.path', 'extra child')
                    git(parent, 'update-index', '--add', '--cacheinfo', '160000', self.expected['topic'][LEAF]['commit'], 'extra child')
                if kind == 'rename':
                    git(parent, 'config', '-f', '.gitmodules', '--rename-section', 'submodule.nested logical name', 'submodule.renamed')
                git(parent, 'add', '.gitmodules')
                git(parent, 'commit', '-m', kind)
                git(self.path, 'add', CHILD)
                git(self.path, 'commit', '-m', kind)
                git(self.path, 'switch', 'master')
                git(parent, 'switch', 'original/start')
                self.reject_unchanged('Unsupported submodule structure change')
                git(self.path, 'update-ref', 'refs/heads/topic', root_topic)

    def test_nested_protection_flags_filters_and_operations(self):
        path = self.paths[LEAF]
        gd = Path(git(path, 'rev-parse', '--absolute-git-dir'))
        for flag in ('assume-unchanged', 'skip-worktree'):
            git(path, 'update-index', '--' + flag, 'app.py')
            self.reject_unchanged('assume-unchanged/skip-worktree')
            git(path, 'update-index', '--no-' + flag, 'app.py')
        for key, value, error in (('filter.bad.clean', 'false', 'Git filters'),
                                  ('core.sparseCheckout', 'true', 'Sparse checkouts')):
            git(path, 'config', key, value)
            self.reject_unchanged(error)
            git(path, 'config', '--unset', key)
        (gd / 'MERGE_HEAD').write_text(git(path, 'rev-parse', 'HEAD'))
        self.reject_unchanged('Unfinished Git operation')

    def test_symlink_worktree_and_external_gitdir_are_rejected(self):
        path = self.paths[LEAF]
        relocated = self.base / 'relocated'
        path.rename(relocated)
        path.symlink_to(relocated, target_is_directory=True)
        with self.assertRaisesRegex(AuditError, 'symbolic link'):
            self.repo.preflight(['master', 'topic'])
        path.unlink()
        relocated.rename(path)
        (path / '.git').write_text('gitdir: ' + str(self.base / 'leaf-origin/.git') + '\n')
        with self.assertRaisesRegex(AuditError, 'Unexpected external Git directory'):
            self.repo.preflight(['master', 'topic'])


class RecursiveTransitionTests(RecursiveFixture, unittest.TestCase):
    def test_packed_original_refs_restore_without_rewriting_them(self):
        for path in self.paths.values():
            git(path, 'pack-refs', '--all')
        before = self.state()
        pins = self.repo.preflight(['master', 'topic'])
        self.repo.checkout(pins['topic'])
        self.repo.restore('master', pins['master'])
        self.assert_original(before)
        for relative, path in self.paths.items():
            gd = Path(git(path, 'rev-parse', '--absolute-git-dir'))
            self.assertEqual((gd / 'packed-refs').read_bytes(), before[relative]['files']['packed-refs'])

    def test_partial_switch_error_and_interrupt_at_every_level(self):
        before = self.state()
        for relative in self.paths:
            for after in (False, True):
                for failure in (AuditError, KeyboardInterrupt):
                    with self.subTest(node=relative, after=after, error=failure):
                        repo = Repository(self.path)
                        pins = repo.preflight(['master', 'topic'])
                        node = repo.nodes[relative]
                        original_git = node.git
                        def fail(*args, **kwargs):
                            if args[0] == 'switch':
                                if after:
                                    original_git(*args, **kwargs)
                                raise failure('injected switch failure')
                            return original_git(*args, **kwargs)
                        with patch.object(node, 'git', side_effect=fail):
                            with self.assertRaises(failure):
                                repo.checkout(pins['topic'])
                        restored = repo.restore('master', pins['master'])
                        self.assertTrue(restored['restored'])
                        self.assert_original(before)

    def test_original_ref_movement_and_directory_substitution_refuse_restoration(self):
        pins = self.repo.preflight(['master', 'topic'])
        self.repo.checkout(pins['topic'])
        path = self.paths[LEAF]
        git(path, 'update-ref', 'refs/heads/original/start', self.expected['topic'][LEAF]['commit'])
        with self.assertRaisesRegex(UnsafeRepository, 'Original branch moved'):
            self.repo.restore('master', pins['master'])
        git(path, 'update-ref', 'refs/heads/original/start', self.expected['master'][LEAF]['commit'])
        gd = self.repo.nodes[LEAF].git_dir
        moved = gd.with_name(gd.name + '-preserved')
        gd.rename(moved)
        shutil.copytree(moved, gd)
        with self.assertRaisesRegex(UnsafeRepository, 'metadata changed'):
            self.repo.restore('master', pins['master'])

    def test_primary_failure_survives_restoration_error_in_manifest(self):
        self.write_config()
        runner = Runner(load_config(self.config_path), self.base / 'run')
        def fail_invoke(*args):
            (self.paths[LEAF] / 'app.py').write_text('external change')
            raise AuditError('PRIMARY agent failure')
        runner.invoke = fail_invoke
        runner.check_cli = lambda: {}
        manifest, code = runner.run()
        self.assertEqual(code, 1)
        self.assertIn('PRIMARY agent failure', str(manifest['branches']))
        self.assertEqual(manifest['restoration']['node'], LEAF)
        self.assertFalse(manifest['restoration']['restored'])

    def test_restoration_git_error_does_not_replace_primary_error(self):
        self.write_config()
        runner = Runner(load_config(self.config_path), self.base / 'run')
        original_git = runner.repo.git
        def fail_restore(*args, **kwargs):
            if args[0] == 'switch' and '--no-guess' in args:
                raise AuditError('SECONDARY restoration Git failure')
            return original_git(*args, **kwargs)
        runner.check_cli = lambda: {}
        with patch.object(runner.repo, 'git', side_effect=fail_restore), \
                patch.object(runner, 'invoke', side_effect=AuditError('PRIMARY agent failure')):
            manifest, code = runner.run()
        self.assertEqual(code, 1)
        self.assertIn('PRIMARY agent failure', manifest['errors'])
        self.assertIn('SECONDARY restoration Git failure', manifest['restoration']['error'])
        self.assertEqual(manifest['restoration']['node'], '.')


@unittest.skipUnless(os.geteuid() == 0, 'Actual UID 0 required; run in the isolated CI container. No mocked ownership.')
class RecursiveRootTests(RecursiveFixture, unittest.TestCase):
    def test_actual_root_needs_no_permission_flag(self):
        for check in (True, False):
            result, manifest = self.execute(check=check)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('[WARN] Running as root', result.stderr)

    def test_foreign_owned_recursive_checkout_requires_scoped_trust(self):
        try:
            owner = pwd.getpwnam('nobody')
        except KeyError:
            self.skipTest('No nobody account to create a real foreign-owned checkout.')
        script = '''import json, sys, unittest
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
from test_submodules import RecursiveFixture
class Fixture(RecursiveFixture, unittest.TestCase): pass
fixture = Fixture()
fixture.setUp()
# Parent owns fixture cleanup after this short-lived unprivileged process exits.
fixture.tmp._finalizer.detach()
print(json.dumps({'base': str(fixture.base), 'path': str(fixture.path), 'expected': fixture.expected}))
'''
        try:
            child = subprocess.run([sys.executable, '-B', '-c', script, str(ROOT), str(ROOT / 'tests')],
                cwd='/tmp', env={'PATH': self.env['PATH']}, user=owner.pw_uid, group=owner.pw_gid,
                extra_groups=[], capture_output=True, text=True, timeout=90)
        except PermissionError as exc:
            self.skipTest(f'Cannot create fixture as another UID: {exc}')
        self.assertEqual(child.returncode, 0, child.stderr)
        fixture = json.loads(child.stdout)
        self.addCleanup(shutil.rmtree, fixture['base'])
        self.path = Path(fixture['path'])
        self.config['repository'] = str(self.path)
        self.expected_file.write_text(json.dumps(fixture['expected']))
        self.assertEqual(self.path.stat().st_uid, owner.pw_uid)
        for check in (True, False):
            result, manifest = self.execute(check=check)
            self.assertEqual(result.returncode, 1)
            self.assertIn('ownership mismatch', result.stderr)
            self.assertEqual(self.calls.read_text(), '')
            result, manifest = self.execute(check=check, trust=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for item in manifest['original_hierarchy'].values():
                self.assertEqual(Path(item['worktree']).stat().st_uid, owner.pw_uid)
            calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
            self.assertTrue(all(call['git_config_env'] == {} for call in calls))
        repo = Repository(self.path, trust_repository=True)
        self.addCleanup(repo.close)
        repo.preflight(['master', 'topic'])
        # Sharing a checked executable with a trusted parent does not authorize
        # a separately created, untrusted child, even on old upstream Git.
        child = Repository(self.path / CHILD, runtime=repo.runtime)
        with self.assertRaisesRegex(AuditError, 'ownership mismatch'):
            child.setup_node(repo, repo, 'logical module')
        for node in repo.nodes.values():
            self.assertEqual(node.git('config', '--get-all', 'safe.directory'),
                             ('\n' + str(node.path) + '\n').encode())


if __name__ == '__main__':
    unittest.main()
