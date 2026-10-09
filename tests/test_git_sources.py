# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline integration tests for sequential checkouts in the original repository."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from explain import Repository, Folder, UnsafeRepository, Runner, load_config, run_audit
from src.analysis.git_sources import tracked_paths
from src.analysis.evidence import resolve_evidence
from src.runtime.reporting import Reporter

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
        env = {k: os.environ[k] for k in ('PATH', 'LANG') if k in os.environ}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
        return subprocess.check_output(['git', '-C', str(self.path), *args],
                                       env=env, stderr=subprocess.PIPE).decode().strip()

    def write(self, name, data):
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)

    def state(self):
        return {str(p.relative_to(self.path)): p.read_bytes() for p in self.path.rglob('*')
                if p.is_file() and not p.is_symlink()}

    def prepare(self, branches=('main', 'alias')):
        return self.repo.preflight(list(branches))

    def capture(self):
        folder = Folder(self.path, paths=tracked_paths(self.repo))
        folder.snapshot(exclude_git=True)
        return folder

    def topic(self):
        self.git('switch', '-c', 'topic')
        self.write('app.py', 'TOPIC_SENTINEL\n')
        self.git('commit', '-am', 'topic')
        self.git('switch', 'main')

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
                  'execution': {'review_enabled': True, 'stage_timeout_seconds': 1 if action == 'wait' else 60}, 'continue_on_error': False}
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

    def test_all_backends_analyze_original_repository_sequentially(self):
        self.topic()
        original = self.git('rev-parse', 'HEAD')
        for backend in ('codex', 'claude-code', 'xxx', 'opencode'):
            with self.subTest(backend=backend):
                result, manifest, calls = self.run_cli(backend, ('topic', 'main', 'alias'))
                self.assertEqual(result.returncode, 0, result.stderr)
                observed = [c for c in calls if 'source_files' in c]
                self.assertEqual([c['context']['branch'] for c in observed],
                                 ['topic'] * 3 + ['main'] * 3 + ['alias'] * 3)
                for call in observed:
                    self.assertEqual(Path(call['cwd']), self.path)
                    content = 'TOPIC_SENTINEL\n' if call['context']['branch'] == 'topic' else 'HEAD_SENTINEL\n'
                    self.assertEqual(call['source_files']['app.py'], content)
                    self.assertFalse(any('.git' in p.split('/') for p in call['source_files']))
                    self.assertNotIn('source_snapshot', call['context'])
                for item in manifest['branches']:
                    evidence = item['study']['program_checks']['evidence'][0]
                    self.assertEqual(evidence['source_identity'],
                        {'mode': 'git', 'branch': item['branch'], 'commit': manifest['pins'][item['branch']]})
                    content = b'TOPIC_SENTINEL\n' if item['branch'] == 'topic' else b'HEAD_SENTINEL\n'
                    self.assertEqual(evidence['file_sha256'], hashlib.sha256(content).hexdigest())
                    inventory_path = Path(manifest['final_report']).parent / item['directory'] / 'source.inventory.json'
                    self.assertTrue(inventory_path.is_file())
                    self.assertNotIn(self.path, inventory_path.parents)
                self.assertEqual(manifest['comparison']['completion_status'], 'COMPLETE')
                bundle = json.loads((Path(manifest['final_report']).parent / 'comparison/inputs.json').read_text())
                self.assertEqual(bundle['git_deltas']['main']['changes'][0]['path'], 'app.py')
                self.assertTrue(manifest['restoration']['restored'])
                self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')
                self.assertEqual(self.git('rev-parse', 'HEAD'), original)
                self.assertEqual(self.git('status', '--porcelain'), '')
                self.assertFalse({'source_snapshot', 'snapshot_plans', 'working_tree', 'temporary_sources_removed'} & manifest.keys())

    def test_initial_dirty_and_untracked_states_are_rejected(self):
        for kind in ('tracked', 'staged', 'untracked'):
            with self.subTest(kind=kind):
                filename = 'new.txt' if kind == 'untracked' else 'app.py'
                self.write(filename, 'dirty\n')
                if kind == 'staged': self.git('add', filename)
                before = self.state()
                with self.assertRaisesRegex(UnsafeRepository, 'clean checkout'): self.prepare()
                self.assertEqual(before, self.state())
                if kind == 'untracked': (self.path / filename).unlink()
                else: self.write(filename, 'HEAD_SENTINEL\n')
                if kind == 'staged': self.git('add', filename)

    def test_ignored_files_and_symlinks_are_outside_readable_inventory(self):
        self.write('.gitignore', '*.env\nbuild/\n')
        (self.path / 'link').symlink_to('/outside/source')
        self.git('add', '.gitignore', 'link')
        self.git('commit', '-m', 'ignore rules and link')
        self.write('build/secret', 'IGNORED_SECRET\n')
        self.write('.env', 'IGNORED_SECRET\n')
        self.prepare()
        folder = self.capture()
        files = folder.files
        self.assertIn('tracked.env', files)
        self.assertNotIn('.env', files)
        self.assertNotIn('build/secret', files)
        self.assertNotIn('link', files)
        self.assertFalse(any('.git' in p.split('/') for p in files))
        self.write('build/new', 'ignored\n')
        folder.assert_snapshot(folder.inventory['source_fingerprint'])
        context = dict(branch='main', source_commit=self.repo.head(), repository=str(self.path))
        pointer = dict(id='E1', source_id='source-001', path='.env', start_line=1, end_line=1)
        with patch('src.analysis.evidence._read_confined', side_effect=AssertionError('Excluded read')):
            evidence = resolve_evidence('study', [pointer], context,
                {p: e['sha256'] for p, e in files.items()}, expected_metadata=folder.metadata)
        self.assertEqual(evidence[0]['status'], 'NOT_FOUND')

    def test_global_xdg_and_conditional_ignore_rules_remain_supported(self):
        xdg = self.home / 'xdg'
        (xdg / 'git').mkdir(parents=True)
        (xdg / 'git/ignore').write_text('secret\n')
        self.write('secret', 'ignored\n')
        with patch.dict(os.environ, {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(xdg)}):
            self.prepare()
            self.assertNotIn('secret', self.capture().files)
        includes = self.home / 'included.config'
        excludes = self.home / 'conditional.ignore'
        excludes.write_text('secret\n')
        includes.write_text('[core]\n excludesFile = ' + str(excludes) + '\n')
        (self.home / '.gitconfig').write_text('[includeIf "gitdir:' + str(self.path / '.git') + '"]\n path = ' + str(includes) + '\n')
        with patch.dict(os.environ, {'HOME': str(self.home)}):
            repo = Repository(self.path)
            self.addCleanup(repo.close)
            repo.preflight(['main'])
            excludes.write_text('other\n')
            with self.assertRaises(UnsafeRepository): repo.assert_expected()

    def test_check_does_not_switch_copy_inventory_or_invoke_model(self):
        self.topic()
        before = self.state()
        result, manifest, calls = self.run_cli('codex', ('main', 'topic'), check=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, self.state())
        self.assertEqual(manifest['switch_journal'], [])
        self.assertFalse(any('context' in c for c in calls))
        self.assertFalse(list(Path(manifest['repository']).glob('**/source.inventory.json')))
        run = Path(manifest['final_report']).parent if manifest.get('final_report') else self.base / 'reports'
        self.assertFalse(list(run.rglob('source.inventory.json')))

    def test_ignore_rule_changes_are_detected_and_block_restoration(self):
        self.prepare()
        self.repo.checkout(self.repo.head())
        self.write('.git/info/exclude', 'app.py\n')
        with self.assertRaises(UnsafeRepository):
            self.repo.assert_expected()
        result = self.repo.restore()
        self.assertFalse(result['restored'])
        self.assertIn('Ignore rules changed', result['nodes']['.']['error'])
        self.assertEqual((self.path / '.git/info/exclude').read_text(), 'app.py\n')

    def test_no_source_temporary_directory_or_blob_extraction(self):
        self.run_cli('codex', check=True)
        cfg = load_config(self.base / 'config.json')
        runner = Runner(cfg, self.base / 'no-copies')
        temporary = tempfile.TemporaryDirectory
        def allocate(*args, **kwargs):
            self.assertNotEqual(kwargs.get('prefix'), 'archaudit-sources-')
            return temporary(*args, **kwargs)
        git = Repository.git
        def command(node, *args, **kwargs):
            self.assertNotIn(args[0], ('reset', 'clean', 'stash', 'checkout', 'worktree', 'archive', 'fetch'))
            self.assertFalse(args[:2] == ('cat-file', 'blob'))
            return git(node, *args, **kwargs)
        env = {'HOME': str(self.home), 'PATH': os.environ['PATH'],
               'AUDIT_TEST_CALL_LOG': str(self.base / 'allocation-calls.jsonl')}
        with patch.dict(os.environ, env, clear=True), patch('tempfile.TemporaryDirectory', side_effect=allocate), \
                patch.object(Repository, 'git', command):
            manifest, code = runner.run()
        self.assertEqual(code, 0, manifest['errors'])
        self.assertTrue(manifest['restoration']['restored'])

    def test_detached_unselected_original_is_restored_without_extra_revision(self):
        self.topic()
        self.git('switch', '--detach', 'main')
        original = self.git('rev-parse', 'HEAD')
        result, manifest, _ = self.run_cli('codex', ('topic',))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([b['branch'] for b in manifest['branches']], ['topic'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), original)
        self.assertIsNone(self.repo.symbolic_ref())
        self.assertTrue(manifest['restoration']['restored'])

    def test_analysis_uses_pins_even_if_an_unchecked_branch_ref_moves(self):
        self.topic()
        pins = self.prepare(('main', 'topic'))
        self.git('branch', '-f', 'topic', 'main')
        try:
            self.repo.checkout(pins['topic'])
            self.assertEqual(self.repo.head(), pins['topic'])
            self.assertEqual((self.path / 'app.py').read_text(), 'TOPIC_SENTINEL\n')
        finally:
            self.assertTrue(self.repo.restore()['restored'])

    def test_index_and_original_ref_changes_are_not_accepted(self):
        self.topic()
        pins = self.prepare(('main', 'topic'))
        self.repo.checkout(pins['topic'])
        try:
            self.git('update-index', '--assume-unchanged', 'app.py')
            with self.assertRaises(UnsafeRepository): self.repo.assert_expected()
            self.git('update-index', '--no-assume-unchanged', 'app.py')
            self.git('update-ref', 'refs/heads/main', pins['topic'])
            restoration = self.repo.restore()
            self.assertFalse(restoration['restored'])
            self.assertIn('Original branch moved', restoration['nodes']['.']['error'])
            self.assertEqual(self.git('rev-parse', 'main'), pins['topic'])
        finally:
            self.git('update-ref', 'refs/heads/main', pins['main'])

    def test_interrupt_during_restoration_is_reported(self):
        pins = self.prepare(('alias',))
        self.repo.checkout(pins['alias'])
        with patch.object(self.repo, 'switch_node', side_effect=KeyboardInterrupt()):
            restored = self.repo.restore()
        self.assertFalse(restored['restored'])
        self.assertTrue(restored['interrupted'])
        self.assertTrue(self.repo.restore()['restored'])

    def test_journal_write_failure_does_not_prevent_restoration(self):
        self.topic()
        pins = self.prepare(('topic',))
        self.repo.checkout(pins['topic'])
        def fail():
            raise OSError('journal disk full')
        restored = self.repo.restore(fail)
        self.assertTrue(restored['restored'])
        self.assertEqual(restored['journal_errors'], ['journal disk full'])
        self.assertEqual(self.repo.symbolic(), 'main')

    def test_failure_timeout_and_interrupt_restore_original(self):
        self.topic()
        for kind in ('error', 'wait', 'interrupt'):
            with self.subTest(kind=kind):
                result, manifest, _ = self.run_cli('codex', ('topic', 'main'), action=kind)
                self.assertEqual(result.returncode, 130 if kind == 'interrupt' else 1, result.stderr)
                self.assertTrue(manifest['restoration']['restored'])
                self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')
                self.assertEqual((self.path / 'app.py').read_text(), 'HEAD_SENTINEL\n')

    def test_ui_disconnect_before_restoration_still_restores_and_returns_130(self):
        self.topic()
        self.run_cli('codex', ('topic',), check=True)
        def emit(event):
            if event.event == 'restoration_started':
                raise KeyboardInterrupt('UI disconnected')
        reporter = Reporter(stdout=io.StringIO(), stderr=io.StringIO(), progress=False, event_sink=emit)
        env = {'HOME': str(self.home), 'PATH': os.environ['PATH'],
               'AUDIT_TEST_CALL_LOG': str(self.base / 'disconnect-calls.jsonl')}
        args = SimpleNamespace(config=self.base / 'config.json', check=False, trust_repository=False)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(run_audit(args, reporter), 130)
        manifests = [json.loads(path.read_text()) for path in (self.base / 'reports').glob('*/manifest.json')]
        manifest = next(m for m in manifests if m['status'] != 'PREFLIGHT_OK')
        self.assertTrue(manifest['restoration']['restored'])
        self.assertEqual(manifest['exit_code'], 130)
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')

    def test_source_change_is_fatal_and_blocked_restoration_is_explicit(self):
        self.topic()
        result, manifest, _ = self.run_cli('codex', ('topic', 'main'), action='file')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(manifest['restoration']['restored'])
        self.assertEqual(len(manifest['branches']), 1)
        self.assertIn('RESTORATION_FAILED', {d['code'] for d in manifest['diagnostics']})
        self.assertEqual((self.path / 'app.py').read_text(), 'external modification\n')
        self.assertIn('not fully restored', result.stderr)

    def test_ignored_file_collision_does_not_overwrite_content(self):
        self.git('switch', '-c', 'topic')
        self.write('collision', 'tracked\n')
        self.git('add', 'collision')
        self.git('commit', '-m', 'collision')
        self.git('switch', 'main')
        self.write('.git/info/exclude', 'collision\n')
        self.write('collision', 'ignored original\n')
        result, manifest, _ = self.run_cli('codex', ('topic',))
        self.assertEqual(result.returncode, 1)
        self.assertTrue(manifest['restoration']['restored'])
        self.assertEqual((self.path / 'collision').read_text(), 'ignored original\n')
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')

    def test_timestamp_changes_are_detected_without_refreshing_hashes(self):
        self.prepare()
        folder = self.capture()
        file = self.path / 'app.py'
        st = file.stat()
        os.utime(file, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))
        with self.assertRaisesRegex(Exception, 'changed'):
            folder.assert_snapshot(folder.inventory['source_fingerprint'])
