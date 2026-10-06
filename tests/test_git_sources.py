# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline regression tests for actual working bytes and read-only Git inputs."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from explain import Repository, Folder, UnsafeRepository, Runner, load_config
from src.analysis.git_sources import GitSources
from src.analysis.evidence import resolve_evidence

ROOT = Path(__file__).resolve().parents[1]


class GitSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.path = self.base / 'repo'
        self.path.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.write('app.py', 'HEAD_SENTINEL\n')
        self.write('deleted.txt', 'DELETE_SENTINEL\n')
        self.write('rename.txt', 'RENAME_SENTINEL\n')
        self.write('tracked.env', 'TRACKED_SENTINEL\n')
        self.git('add', '.')
        self.git('commit', '-m', 'base')
        self.git('branch', 'alias')
        self.repo = Repository(self.path)
        self.addCleanup(self.repo.close)
        self.home = self.base / 'home'
        self.home.mkdir()
        (self.home / 'audit-profile.json').write_text('{"model":"configured-model"}')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.path), *args], stderr=subprocess.PIPE).decode().strip()

    def write(self, name, data):
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)

    def state(self):
        return {str(p.relative_to(self.path)): p.read_bytes() for p in self.path.rglob('*')
                if p.is_file() and not p.is_symlink()}

    def prepare(self, branches=('main', 'alias')):
        pins = self.repo.preflight(list(branches))
        sources = GitSources(self.repo, Folder, UnsafeRepository, self.base)
        self.addCleanup(sources.close)
        return sources, pins

    def dirty(self):
        self.write('app.py', 'INDEX_SENTINEL\n')
        self.git('add', 'app.py')
        self.write('app.py', 'WORKING_SENTINEL\n')
        self.write('new/deep/.hidden', 'UNTRACKED_SENTINEL\n')
        self.write('.gitignore', '*.env\nbuild/\nnode_modules/\n')
        for path in ('.env', 'build/secret', 'node_modules/secret'):
            self.write(path, 'IGNORED_SECRET_SENTINEL\n')

    def test_disk_bytes_index_metadata_and_independent_copies(self):
        for kind in ('unstaged', 'staged', 'mixed'):
            with self.subTest(kind=kind):
                self.write('app.py', 'INDEX_SENTINEL\n' if kind == 'mixed' else 'WORKING_SENTINEL\n')
                if kind != 'unstaged': self.git('add', 'app.py')
                self.write('app.py', 'WORKING_SENTINEL\n')
                before = self.state()
                sources, _ = self.prepare()
                item = sources.working('main')
                self.assertEqual((item['path'] / 'app.py').read_text(), 'WORKING_SENTINEL\n')
                self.assertNotEqual((item['path'] / 'app.py').stat().st_ino, (self.path / 'app.py').stat().st_ino)
                self.assertEqual(item['git_state']['.']['index']['app.py'][1], self.git('rev-parse', ':app.py'))
                self.assertEqual(before, self.state())

    def test_add_delete_rename_hidden_untracked_and_ignore_semantics(self):
        self.dirty()
        self.git('rm', 'deleted.txt')
        self.git('mv', 'rename.txt', 'renamed.txt')
        self.write('new-staged', 'ADDED_SENTINEL\n')
        self.git('add', 'new-staged')
        self.write('new/.gitignore', '*.tmp\n!keep.tmp\n')
        self.write('new/hide.tmp', 'IGNORED_SECRET_SENTINEL\n')
        self.write('new/keep.tmp', 'KEEP_SENTINEL\n')
        self.write('info-hidden', 'IGNORED_SECRET_SENTINEL\n')
        with (self.path / '.git/info/exclude').open('a') as stream: stream.write('\ninfo-hidden\n')
        sources, pins = self.prepare()
        before = self.state()
        item = sources.working('main')
        files = {str(p.relative_to(item['path'])) for p in item['path'].rglob('*') if p.is_file()}
        self.assertTrue({'app.py', 'new/deep/.hidden', 'tracked.env', 'renamed.txt', 'new-staged', 'new/keep.tmp'} <= files)
        self.assertFalse({'.env', 'deleted.txt', 'rename.txt', 'new/hide.tmp', 'info-hidden'} & files)
        self.assertFalse(any('.git' in p.split('/') for p in files))
        self.assertNotIn('IGNORED_SECRET_SENTINEL', ''.join(p.read_text() for p in item['path'].rglob('*') if p.is_file()))
        sources.commit('alias', pins['alias'])
        delta = sources.delta('alias', 'main')
        changes = {c['path']: c['status'] for c in delta['changes']}
        self.assertEqual(changes['deleted.txt'], 'D')
        self.assertEqual(changes['new/deep/.hidden'], 'A')
        self.assertEqual(before, self.state())

    def test_global_ignores_and_current_working_rules(self):
        excludes = self.home / 'ignore'
        excludes.write_text('global-secret\n')
        (self.home / '.gitconfig').write_text('[core]\n excludesFile = ' + str(excludes) + '\n[filter "danger"]\n smudge = false\n')
        self.write('global-secret', 'IGNORED_SECRET_SENTINEL\n')
        self.write('.gitignore', 'current-secret\n')
        self.write('current-secret', 'IGNORED_SECRET_SENTINEL\n')
        with patch.dict(os.environ, {'HOME': str(self.home)}):
            sources, _ = self.prepare()
            item = sources.working('main')
        self.assertNotIn('global-secret', item['entries'])
        self.assertNotIn('current-secret', item['entries'])

    def test_xdg_global_default_and_conditional_config_include(self):
        xdg = self.home / 'xdg'
        (xdg / 'git').mkdir(parents=True)
        (xdg / 'git/ignore').write_text('xdg-secret\n')
        self.write('xdg-secret', 'IGNORED_SECRET_SENTINEL\n')
        self.write('conditional-secret', 'IGNORED_SECRET_SENTINEL\n')
        with patch.dict(os.environ, {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(xdg)}):
            sources, _ = self.prepare()
            self.assertNotIn('xdg-secret', sources.working('default')['entries'])
            includes = self.home / 'included.config'
            excludes = self.home / 'conditional.ignore'
            excludes.write_text('conditional-secret\n')
            includes.write_text('[core]\n excludesFile = ' + str(excludes) + '\n')
            (self.home / '.gitconfig').write_text('[includeIf "gitdir:' + str(self.path / '.git') + '"]\n path = ' + str(includes) + '\n')
            self.assertNotIn('conditional-secret', sources.working('conditional')['entries'])

    def test_symlinks_are_metadata_and_excluded_evidence_is_never_opened(self):
        self.dirty()
        outside = self.base / 'outside-secret'
        outside.write_text('OUTSIDE_SECRET_SENTINEL\n')
        (self.path / 'link-ignored').symlink_to('.env')
        (self.path / 'link-outside').symlink_to(outside)
        sources, _ = self.prepare()
        item = sources.working('main')
        self.assertEqual(item['entries']['link-ignored']['type'], 'symlink')
        self.assertFalse((item['path'] / 'link-ignored').exists())
        context = {'branch': 'main', 'source_commit': self.repo.head(), 'repository': str(item['path']),
                   'source_snapshot': item['source_snapshot']}
        pointer = {'id': 'E1', 'source_id': 'source-001', 'path': '.env', 'start_line': 1, 'end_line': 1}
        with patch('src.analysis.evidence._read_confined', side_effect=AssertionError('Excluded read')):
            result = resolve_evidence('study', [pointer], context, {'app.py': 'unused'})
        self.assertEqual(result[0]['status'], 'NOT_FOUND')

    def test_concurrent_allowed_source_index_and_rules_changes_fail_preparation(self):
        for name in ('app.py', '.gitignore', 'index'):
            with self.subTest(name=name):
                sources, _ = self.prepare()
                scan = sources.working_scan
                def change(destination=None):
                    result = scan(destination)
                    if destination is not None:
                        if name == 'index': self.git('update-index', '--assume-unchanged', 'app.py')
                        else: self.write(name, 'changed-during-copy\n')
                    return result
                with patch.object(sources, 'working_scan', side_effect=change):
                    with self.assertRaises(UnsafeRepository): sources.working('main')
                if name == 'index': self.git('update-index', '--no-assume-unchanged', 'app.py')

    def test_ignored_mutation_does_not_change_fingerprint_but_snapshot_mutation_fails(self):
        self.dirty()
        sources, _ = self.prepare()
        first = sources.working('main')
        self.write('.env', 'another excluded secret\n')
        self.write('build/new-file', 'excluded\n')
        second = sources.working('again')
        self.assertEqual(first['source_snapshot']['fingerprint'], second['source_snapshot']['fingerprint'])
        self.write('app.py', 'later original edit\n')
        sources.assert_intact()
        (first['path'] / 'app.py').write_text('snapshot corruption\n')
        with self.assertRaisesRegex(UnsafeRepository, 'snapshot changed'): sources.assert_intact()

    def test_ignored_creation_during_copy_and_working_eol_bytes(self):
        self.dirty()
        sources, _ = self.prepare()
        scan = sources.working_scan
        def change(destination=None):
            result = scan(destination)
            if destination is not None: self.write('build/created-during-copy', 'excluded\n')
            return result
        with patch.object(sources, 'working_scan', side_effect=change):
            item = sources.working('main')
        self.assertNotIn('build/created-during-copy', item['entries'])
        self.git('config', 'core.autocrlf', 'true')
        (self.path / 'app.py').write_bytes(b'WORKING_SENTINEL\r\n')
        sources, _ = self.prepare()
        item = sources.working('eol')
        self.assertEqual((item['path'] / 'app.py').read_bytes(), b'WORKING_SENTINEL\r\n')

    def test_conflicted_index_and_unfinished_operations_remain_rejected(self):
        self.git('switch', 'alias')
        self.write('app.py', 'OTHER\n')
        self.git('commit', '-am', 'other')
        self.git('switch', 'main')
        self.write('app.py', 'MAIN\n')
        self.git('commit', '-am', 'main')
        subprocess.run(['git', '-C', str(self.path), 'merge', 'alias'], capture_output=True)
        with self.assertRaisesRegex(UnsafeRepository, 'Unfinished Git operation'): self.prepare()
        (self.path / '.git/MERGE_HEAD').unlink()
        with self.assertRaisesRegex(UnsafeRepository, 'Conflicted Git index'): self.prepare()

    def run_cli(self, backend, branches=('main', 'alias'), check=False, action=None, partial_branches=(), policy='compromise'):
        cli = self.base / ('fake-' + backend)
        fixture = 'fake_xxx.py' if backend == 'xxx' else 'fake_cli.py'
        cli.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path(' + repr(str(ROOT / 'tests/fixtures' / fixture)) + ', run_name="__main__")\n')
        cli.chmod(0o700)
        calls = self.base / ('calls-' + backend + '.jsonl')
        calls.write_text('')
        config = {'mode': 'git', 'result_policy': policy,
                  'git_mode': {'repository': str(self.path), 'branches': list(branches), 'baseline_branch': branches[0]},
                  'reports_dir': str(self.base / 'reports'), 'agent': {'backend': backend, 'executable': str(cli)},
                  'execution': {'stage_timeout_seconds': 1 if action == 'wait' else 30}, 'continue_on_error': False}
        config_path = self.base / 'config.json'
        config_path.write_text(json.dumps(config))
        env = {'PATH': os.environ['PATH'], 'HOME': str(self.home), 'AUDIT_TEST_CALL_LOG': str(calls),
               'AUDIT_FAKE_CALLS': str(calls), 'AUDIT_FAKE_BACKEND': backend, 'AUDIT_FAKE_CAPTURE_SOURCES': '1'}
        env['AUDIT_TEST_PARTIAL_BRANCHES'] = json.dumps(partial_branches)
        if backend == 'opencode': env['AUDIT_TEST_CLI_VERSION'] = 'opencode v2.0.23'
        if action: env['AUDIT_TEST_ACTION'] = json.dumps({'stage': 'study', 'kind': action, 'seconds': 3})
        command = [sys.executable, '-B', str(ROOT / 'explain.py'), '--config', str(config_path)]
        if check: command.append('--check')
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=90)
        summary = json.loads(result.stdout)
        manifest = json.loads(Path(summary['manifest']).read_text())
        records = [json.loads(line) for line in calls.read_text().splitlines()]
        return result, manifest, records

    def test_cli_agents_read_working_and_untracked_bytes_without_ignored_content(self):
        self.dirty()
        before = self.state()
        for backend in ('codex', 'claude-code', 'xxx', 'opencode'):
            with self.subTest(backend=backend):
                result, manifest, calls = self.run_cli(backend)
                self.assertEqual(result.returncode, 0, result.stderr)
                observed = [c for c in calls if 'source_files' in c and 'app.py' in c['source_files']]
                self.assertEqual(len(observed), 6)
                for call in observed[:3]:
                    self.assertEqual(call['source_files']['app.py'], 'WORKING_SENTINEL\n')
                    self.assertEqual(call['source_files']['new/deep/.hidden'], 'UNTRACKED_SENTINEL\n')
                    self.assertNotIn('IGNORED_SECRET_SENTINEL', json.dumps(call))
                    if backend == 'xxx': self.assertEqual(call['permissions']['external_directory'], 'deny')
                    if backend == 'claude-code': self.assertIn('Read(/' + str(self.path) + '/**)', call['args'])
                for call in observed[3:]:
                    self.assertEqual(call['source_files']['app.py'], 'HEAD_SENTINEL\n')
                    self.assertNotIn('new/deep/.hidden', call['source_files'])
                self.assertEqual(manifest['branches'][0]['source_snapshot']['source_type'], 'working_tree')
                self.assertEqual(manifest['branches'][1]['source_snapshot']['source_type'], 'commit')
                evidence = manifest['branches'][0]['study']['program_checks']['evidence'][0]
                self.assertEqual(evidence['file_sha256'], hashlib.sha256(b'WORKING_SENTINEL\n').hexdigest())
                self.assertEqual(evidence['source_identity'], dict(mode='git', **manifest['branches'][0]['source_snapshot']))
                inputs = json.loads((Path(manifest['final_report']).parent / 'comparison/inputs.json').read_text())
                delta = inputs['git_deltas']['alias']
                self.assertFalse(delta['identical_trees'])
                self.assertEqual(next(c['status'] for c in delta['changes'] if c['path'] == 'new/deep/.hidden'), 'D')
                self.assertEqual(manifest['pins']['main'], manifest['pins']['alias'])
                self.assertEqual(before, self.state())
                self.assertTrue(all(not Path(c['cwd']).exists() for c in observed))

    def test_extra_revision_for_unselected_or_detached_working_tree(self):
        self.dirty()
        for detached in (False, True):
            if detached: self.git('switch', '--detach', 'HEAD')
            result, manifest, calls = self.run_cli('codex', ('alias',))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(manifest['branches']), 2)
            extra = manifest['branches'][1]
            self.assertTrue(extra['additional_revision'])
            self.assertEqual(extra['source_snapshot']['branch'], None if detached else 'main')
            self.assertEqual(manifest['baseline_branch'], 'alias')
            report = Path(manifest['final_report']).read_text()
            self.assertIn(extra['branch'], report)
            self.assertIn(extra['source_snapshot']['snapshot_id'], report)

    def test_check_and_failures_leave_all_original_bytes_unchanged(self):
        self.dirty()
        # A user's stash is part of the preservation assertion.
        stash = self.git('stash', 'create')
        self.git('stash', 'store', '-m', 'user stash', stash)
        before = self.state()
        result, manifest, calls = self.run_cli('codex', check=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('context' in c for c in calls))
        for backend in ('codex', 'opencode'):
            for action in ('error', 'wait', 'interrupt'):
                with self.subTest(backend=backend, action=action):
                    result, manifest, calls = self.run_cli(backend, action=action)
                    self.assertEqual(result.returncode, 130 if action == 'interrupt' else 1)
                    self.assertEqual(before, self.state())
                    self.assertTrue(manifest['temporary_sources_removed'])
                    self.assertTrue(all(not Path(c['cwd']).exists() for c in calls if 'context' in c))

    def test_additional_revision_does_not_replace_selected_comparison(self):
        self.git('branch', 'second')
        self.dirty()
        result, manifest, _ = self.run_cli('codex', ('alias', 'second'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(manifest['branches']), 3)
        self.assertEqual(manifest['baseline_branch'], 'alias')
        self.assertEqual(manifest['comparison']['compared_branches'], ['second'])
        bundle = json.loads((Path(manifest['final_report']).parent / 'comparison/inputs.json').read_text())
        self.assertEqual(bundle['requested_branches'], ['alias', 'second'])
        self.assertEqual([b['branch'] for b in bundle['branches']], ['alias', 'second'])

    def test_ignored_only_does_not_add_revision(self):
        self.write('.git/info/exclude', 'only-ignored\n')
        self.write('only-ignored', 'IGNORED_SECRET_SENTINEL\n')
        result, manifest, _ = self.run_cli('codex', ('alias',))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(manifest['branches']), 1)

    def test_accepted_extra_revision_does_not_enable_unaccepted_selected_comparison(self):
        self.git('branch', 'second')
        self.dirty()
        result, manifest, calls = self.run_cli('codex', ('alias', 'second'),
                                             partial_branches=('alias', 'second'), policy='strict')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual([b['accepted'] for b in manifest['branches']], [False, False, True])
        self.assertEqual(manifest['comparison']['completion_status'], 'BLOCKED')
        self.assertEqual(manifest['comparison']['unresolved_branches'], ['alias', 'second'])
        self.assertFalse(any(c.get('stage') == 'compare' for c in calls))
