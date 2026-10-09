# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Source filtering precedes reads, inventory, and study assignments."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from explain import AuditError, Folder, Runner, UnsafeRepository
from src.analysis.analysis_plan import build_analysis_plan, verify_analysis_plan
from src.analysis.coverage_plan import build_coverage_plan, inventory_summary
from src.analysis.evidence import resolve_evidence, _read_confined
from src.analysis.git_sources import tracked_paths, ignored_paths, inventory_delta
from src.analysis.source_filter import normalize_source_filter
from test_folder import FolderFixture
import test_git_sources as git_fixtures
from test_source_hashing import hashed_payloads
from test_submodules import RecursiveFixture, CHILD, LEAF


class SourceFilterConfigTests(FolderFixture):
    def test_defaults_and_partial_settings(self):
        self.assertEqual(self.config()['source_filter'], {'follow_gitignore': False, 'exclude_paths': []})
        for settings in ({}, {'follow_gitignore': True}, {'exclude_paths': ['build', 'src/out']}):
            self.value['source_filter'] = settings
            self.assertEqual(self.config()['source_filter'], normalize_source_filter(settings))

    def test_invalid_settings_fail_before_cli_lookup(self):
        invalid = [None, [], True, {'unknown': True}]
        invalid += [{'follow_gitignore': value} for value in (None, 0, 1, 'true', [], {})]
        invalid += [{'exclude_paths': value} for value in (None, '', 'build', {}, True)]
        invalid += [{'exclude_paths': [value]} for value in (
            None, 1, {}, '', ' ', '.', '..', './build', '../build', 'a/../b', 'a/./b',
            '/build', '//server/share', 'C:/build', 'C:build', 'a\\b', 'a//b', 'build/',
            'a\x00b', 'a\nb', 'a\x7fb', '*', '**/build', 'a?', '[ab]', 'a' * 4097)]
        invalid += [{'exclude_paths': ['build', 'build']}]
        for value in invalid:
            self.value['source_filter'] = value
            with self.subTest(value=value), patch('explain.shutil.which') as which:
                with self.assertRaises(AuditError) as caught:
                    self.config()
                self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
                which.assert_not_called()

    def test_all_examples_keep_filters_opt_in(self):
        from src.contracts.contracts import jsonc
        root = Path(__file__).resolve().parents[1]
        for path in root.glob('config*.example.jsonc'):
            self.assertEqual(jsonc(path.read_text())['source_filter'],
                             {'follow_gitignore': False, 'exclude_paths': []}, path.name)


class FolderFilterTests(FolderFixture):
    def write(self, path, text='source\n'):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def scan(self, follow=True, exclude=()):
        folder = Folder(self.source, source_filter={'follow_gitignore': follow, 'exclude_paths': list(exclude)})
        inventory = folder.snapshot()
        return folder, inventory, {e['path'] for e in inventory['entries'] if e['type'] == 'file'}

    def test_root_nested_wildcards_and_negation_without_repository(self):
        self.write('.gitignore', '*.log\n/root-only\ncache/\n**/generated/**\nblocked/\n!blocked/keep.py\nopen/*\n!open/keep/\n')
        self.write('pkg/.gitignore', '!keep.log\n/local.txt\n')
        self.write('blocked/.gitignore', '!keep.py\n')
        excluded = ['a.log', 'root-only', 'pkg/a.log', 'pkg/local.txt', 'cache/a',
                    'pkg/cache/a', 'pkg/generated/deep/x', 'blocked/keep.py', 'open/drop/a']
        kept = ['pkg/keep.log', 'pkg/root-only', 'pkg/deep/local.txt', 'open/keep/a', 'cache.txt']
        for path in excluded + kept:
            self.write(path)
        self.assertFalse((self.source / '.git').exists())
        folder, inventory, files = self.scan()
        self.assertTrue(set(kept) <= files)
        self.assertFalse(set(excluded) & files)
        self.assertEqual([r['path'] for r in inventory['source_filter']['rule_files']], ['.gitignore', 'pkg/.gitignore'])
        self.assertIn({'path': 'blocked', 'origin': 'GITIGNORE', 'rule_file': '.gitignore', 'line': 5},
                      inventory['source_filter']['exclusions'])
        folder.assert_snapshot(inventory['source_fingerprint'])
        # Compare file selection with Git, after the Folder-only acceptance check.
        subprocess.run(['git', 'init', '-q', str(self.source)], check=True)
        raw = subprocess.check_output(['git', '-C', str(self.source), 'ls-files', '--others', '--exclude-standard', '-z'])
        self.assertEqual(files, {os.fsdecode(p) for p in raw.split(b'\0') if p})

    def test_explicit_paths_are_root_relative_and_override_negation(self):
        self.write('.gitignore', '*.log\n!keep.log\n')
        for path in ('build/a', 'builder/a', 'packages/web/build/a', 'packages/web/node_modules/a',
                     'node_modules/a', 'src/private.txt', 'src/private.txt.bak', 'src/public.txt', 'keep.log', 'drop.log'):
            self.write(path)
        _, inventory, files = self.scan(exclude=['build', 'src/private.txt', 'node_modules', 'keep.log'])
        self.assertTrue({'builder/a', 'packages/web/build/a', 'packages/web/node_modules/a', 'src/private.txt.bak'} <= files)
        self.assertFalse({'build/a', 'src/private.txt', 'node_modules/a', 'keep.log', 'drop.log'} & files)
        self.assertEqual({e['origin'] for e in inventory['source_filter']['exclusions']}, {'CONFIG', 'GITIGNORE'})
        self.assertNotIn('packages/web/node_modules/a', self.scan(exclude=['packages/web/node_modules'])[2])

    def test_disabled_or_absent_rules_preserve_files(self):
        self.write('build/app.py')
        self.assertIn('build/app.py', self.scan()[2])
        self.write('.gitignore', 'build/\n')
        self.assertIn('build/app.py', self.scan(follow=False)[2])
        self.assertNotIn('build/app.py', self.scan(follow=False, exclude=['build'])[2])
        self.assertIn('build/app.py', self.scan(exclude=['.gitignore'])[2])
        self.assertEqual(Folder(self.source).snapshot()['entries'], self.scan(follow=False)[1]['entries'])

    def test_excluded_files_are_never_opened_hashed_or_descended(self):
        self.write('.gitignore', 'ignored/\n*.tmp\n')
        for path in ('ignored/.gitignore', 'ignored/data', 'build/data', 'secret.txt', 'data.tmp'):
            self.write(path, 'EXCLUDED_SENTINEL\n')
        original = os.open

        def guarded(name, *args, **kwargs):
            if str(name) in ('ignored', 'build', 'secret.txt', 'data.tmp'):
                self.fail('Opened excluded path: ' + str(name))
            return original(name, *args, **kwargs)

        with patch('os.open', side_effect=guarded), hashed_payloads() as hashes:
            folder, inventory, _ = self.scan(exclude=['build', 'secret.txt'])
            folder.assert_snapshot(inventory['source_fingerprint'])
        self.assertEqual(hashes[b'EXCLUDED_SENTINEL\n'], 0)
        self.assertEqual(hashes[b'ignored/\n*.tmp\n'], 1)

    def test_active_rule_changes_fail_even_if_ignored_or_selection_unchanged(self):
        self.write('.gitignore', '.gitignore\n*.tmp\n')
        for kind in ('edit', 'touch', 'replace', 'delete'):
            self.write('pkg/.gitignore', '# no matching files\n')
            folder, inventory, files = self.scan()
            self.assertNotIn('.gitignore', files)
            path = self.source / 'pkg/.gitignore'
            if kind == 'edit':
                path.write_text('# different comment\n')
            elif kind == 'touch':
                before = path.stat()
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            elif kind == 'replace':
                path.unlink()
                path.write_text('# no matching files\n')
            else:
                path.unlink()
            with self.subTest(kind=kind), patch('os.read', side_effect=AssertionError('Guard read contents')):
                with self.assertRaisesRegex(AuditError, 'Source folder changed'):
                    folder.assert_snapshot(inventory['source_fingerprint'])
        folder, inventory, _ = self.scan()
        self.write('new/.gitignore', '# newly active\n')
        with self.assertRaises(AuditError):
            folder.assert_snapshot(inventory['source_fingerprint'])

    def test_excluded_changes_do_not_invalidate_snapshot_or_evidence(self):
        self.write('.gitignore', '*.tmp\nignored/\n')
        self.write('ignored/.gitignore', '*\n')
        self.write('build/data')
        self.write('old.tmp')
        folder, inventory, _ = self.scan(exclude=['build'])
        self.write('build/data', 'changed')
        self.write('ignored/.gitignore', '!everything\n')
        self.write('new.tmp', 'new')
        (self.source / 'old.tmp').unlink()
        with patch('os.read', side_effect=AssertionError('Guard read contents')):
            folder.assert_snapshot(inventory['source_fingerprint'])
        self.assertEqual(inventory['source_fingerprint'], self.scan(exclude=['build'])[1]['source_fingerprint'])
        context = {'source_directory': str(self.source), 'source_fingerprint': inventory['source_fingerprint']}
        pointer = {'id': 'E1', 'source_id': 'source-001', 'path': 'app.py', 'start_line': 1, 'end_line': 1}
        pins = {e['path']: e['sha256'] for e in inventory['entries'] if e['type'] == 'file'}
        result = resolve_evidence('study', [pointer], context, pins, expected_metadata=folder.metadata)
        self.assertEqual(result[0]['status'], 'RESOLVED')
        self.write('new.py')
        with self.assertRaises(AuditError):
            folder.assert_snapshot(inventory['source_fingerprint'])

    def test_symlink_rule_files_and_directory_targets_are_not_followed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / '.gitignore').write_text('*\n')
        (outside / 'secret').write_text('outside\n')
        (self.source / '.gitignore').symlink_to(outside / '.gitignore')
        (self.source / 'link').symlink_to(outside, target_is_directory=True)
        folder, inventory, files = self.scan()
        self.assertEqual(files, {'app.py'})
        self.assertEqual(inventory['source_filter']['rule_files'], [])
        self.assertEqual({e['path'] for e in inventory['entries'] if e['type'] == 'symlink'}, {'.gitignore', 'link'})
        (outside / '.gitignore').write_text('app.py\n')
        folder.assert_snapshot(inventory['source_fingerprint'])

    def test_active_rules_cannot_change_during_initial_hashing(self):
        self.write('.gitignore', '.gitignore\n*.tmp\n')
        original = os.read

        def read(fd, count):
            data = original(fd, count)
            if data == b'def main(): return 42\n':
                self.write('.gitignore', '.gitignore\n*.tmp\n# changed\n')
            return data

        with patch('os.read', side_effect=read), self.assertRaises(AuditError):
            self.scan()

    def test_pinned_filter_settings_cannot_change(self):
        folder, inventory, _ = self.scan(exclude=['build'])
        folder.source_filter['exclude_paths'].append('other')
        with self.assertRaisesRegex(AuditError, 'Source filter changed'):
            folder.assert_snapshot(inventory['source_fingerprint'])

    def test_git_pattern_edge_cases_match_git_file_selection(self):
        cases = [
            '*\n!*/\n!*.py\n',
            'foo/*\n!foo/bar/\nfoo/bar/*\n!foo/bar/keep.txt\n',
            'foo/\n!foo/bar/keep.txt\n',
            'foo/**\n!foo/bar/\n!foo/bar/keep.txt\n',
            '/foo\n!foo/\n',
            '*.txt\n!foo/bar/keep.txt\n',
            '**/bar/\n!foo/bar/keep.txt\n',
            '\\#name\n\\!name\nspace\\ \n[ab].txt\n',
            '/root.txt\nfoo/**/drop.py\n',
            '*\n!foo/**\n',
            'foo\n!foo/\n*.txt\n!foo/bar/keep.txt\n',
            '**\n!*/\n!*.py\n',
            'foo/*\n!foo/bar/\n',
            '!**/foo/**\nfoo/\n',
            '*.txt\n!*.txt/\n',
            'foo/**/\n!*.py\n',
            '/foo\n!foo/**/\n',
            'foo/**/\n!foo/**\n',
            'foo/**/**/ \n',
        ]
        for index, patterns in enumerate(cases):
            root = self.base / ('case-' + str(index))
            for path in ('root.txt', 'a.txt', 'b.txt', 'c.txt', '#name', '!name', 'space ',
                         'foo/top.py', 'foo/bar/keep.txt', 'foo/bar/drop.py', 'other/root.txt'):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('source\n')
            (root / '.gitignore').write_text(patterns)
            inventory = Folder(root, source_filter={'follow_gitignore': True}).snapshot()
            files = {e['path'] for e in inventory['entries'] if e['type'] == 'file'}
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            raw = subprocess.check_output(['git', '-C', str(root), 'ls-files', '--others', '--exclude-standard', '-z'])
            with self.subTest(patterns=patterns):
                self.assertEqual(files, {os.fsdecode(p) for p in raw.split(b'\0') if p})

    def test_ecosystem_examples_and_similar_sources(self):
        roots = ['target', 'android/app/build', '.gradle', 'node_modules', '.next', '.angular/cache',
                 'CMakeFiles', 'CMakeCache.txt', 'cmake-build-debug', 'src-tauri/target', '.rustc_info.json',
                 'coverage.out', '__debug_bin', '.venv', '__pycache__', '.build', 'DerivedData', 'Carthage/Build', 'ios/build']
        important = ['pom.xml', 'build.gradle', 'package.json', 'tsconfig.json', 'CMakeLists.txt',
                     'Cargo.toml', 'go.mod', 'pyproject.toml', 'Package.swift', 'build.py', 'builder/a',
                     'targeting/a', 'vendor/modified.py', 'bin/script', 'Pods/source.m', 'Saved/resource',
                     'packages/web/node_modules/modified.js', 'src/target/important.rs']
        for root in roots:
            self.write(root if root in ('CMakeCache.txt', '.rustc_info.json', 'coverage.out', '__debug_bin') else root + '/artifact')
        for path in important:
            self.write(path)
        self.assertTrue(set(important) <= self.scan(follow=False, exclude=roots)[2])
        self.assertEqual(self.scan(follow=False, exclude=roots)[2], set(important) | {'app.py'})
        self.assertGreater(len(self.scan(follow=False)[2]), len(important) + 1)

    def test_inventory_catalog_and_study_plans_are_deterministic(self):
        self.write('.gitignore', 'build/\n')
        self.write('build/generated.py')
        self.write('secret.py')
        self.write('pkg/source.py')
        _, first, files = self.scan(exclude=['secret.py'])
        second = self.scan(exclude=['secret.py'])[1]
        self.assertEqual(first, second)
        self.assertEqual(inventory_summary(first)['files'], len(files))
        context = {'source_directory': str(self.source), 'source_fingerprint': first['source_fingerprint']}
        coverage = build_coverage_plan(None, first, context, fallback=True)
        self.assertEqual(coverage['exclusions'], [])
        catalog = dict(task='architecture_catalog', **context, completion_status='COMPLETE',
                       limitations=[], exclusions=[], subsystems=[])
        unclassified = build_coverage_plan(catalog, first, context)
        self.assertEqual(unclassified['areas'][0]['id'], 'UNCLASSIFIED')
        self.assertEqual(set(unclassified['areas'][0]['file_paths']), files)
        plan = build_analysis_plan(first, coverage, max_source_bytes_per_session=10)
        self.assertEqual(plan, build_analysis_plan(second, coverage, max_source_bytes_per_session=10))
        verify_analysis_plan(plan, first, coverage, max_source_bytes_per_session=10)
        self.assertEqual({p for shard in plan['shards'] for p in shard['primary_file_paths']}, files)
        self.value['source_filter'] = {'follow_gitignore': True, 'exclude_paths': ['secret.py']}
        runner = Runner(self.config(), self.base / 'run')
        with patch.object(runner, 'check_cli', return_value={}):
            manifest, code = runner.run(check_only=True)
        self.assertEqual(code, 0, manifest['errors'])
        self.assertEqual(json.loads((self.base / 'run/source.inventory.json').read_text()), first)


class GitFilterTests(unittest.TestCase):
    setUp = git_fixtures.GitSourceTests.setUp
    git = git_fixtures.GitSourceTests.git
    write = git_fixtures.GitSourceTests.write

    def test_excluded_tracked_git_rules_still_have_integrity_guards(self):
        self.write('.gitignore', '*.env\n')
        self.git('add', '.gitignore')
        self.git('commit', '-m', 'ignore rules')
        self.write('.env', 'ignored\n')
        self.repo.preflight(['main'])
        folder = Folder(self.path, paths=tracked_paths(self.repo), source_filter={'exclude_paths': ['.gitignore']})
        with hashed_payloads() as hashes:
            inventory = folder.snapshot(exclude_git=True)
        self.assertEqual(hashes[b'*.env\n'], 0)
        self.assertNotIn('.gitignore', folder.files)
        self.assertNotIn('.env', folder.files)
        self.assertIn('tracked.env', folder.files)
        self.write('.gitignore', '*.env\n# changed\n')
        with self.assertRaises(UnsafeRepository): self.repo.assert_expected()

    def test_tracked_files_and_explicit_exclusions_across_checkouts(self):
        self.write('build/generated.py', 'EXCLUDED_BUILD\n')
        self.write('pkg/build/keep.py', 'NESTED_SOURCE\n')
        self.write('.gitignore', '*.env\n')
        self.git('add', '.')
        self.git('commit', '-m', 'build fixture')
        self.git('branch', '-f', 'alias')
        self.write('.env', 'IGNORED_SECRET\n')
        pins = self.repo.preflight(['main', 'alias'])
        inventories = {}
        settings = {'exclude_paths': ['build', 'app.py']}
        try:
            with hashed_payloads() as hashes:
                for branch in ('main', 'alias'):
                    self.repo.checkout(pins[branch])
                    folder = Folder(self.path, paths=tracked_paths(self.repo), source_filter=settings)
                    inventories[branch] = folder.snapshot(exclude_git=True)
                    folder.assert_snapshot(inventories[branch]['source_fingerprint'])
                    self.assertTrue({'tracked.env', 'pkg/build/keep.py'} <= folder.files.keys())
                    self.assertFalse({'app.py', 'build/generated.py', '.env'} & folder.files.keys())
                    self.assertIn({'path': '.env', 'origin': 'GITIGNORE'}, ignored_paths(self.repo, folder.source_filter))
            self.assertEqual(hashes[b'EXCLUDED_BUILD\n'], 0)
            self.assertEqual(hashes[b'HEAD_SENTINEL\n'], 0)
            self.assertEqual(hashes[b'IGNORED_SECRET\n'], 0)
            self.assertEqual(inventory_delta('main', 'alias', inventories, pins, self.repo.plans)['changes'], [])
        finally:
            self.assertTrue(self.repo.restore()['restored'])


class SubmoduleFilterTests(RecursiveFixture, unittest.TestCase):
    def test_paths_use_common_root_and_keep_source_identities(self):
        self.addCleanup(self.repo.close)
        pins = self.repo.preflight(['master', 'topic'])
        try:
            for branch in ('master', 'topic'):
                self.repo.checkout(pins[branch])
                folder = Folder(self.path, paths=tracked_paths(self.repo),
                                source_filter={'exclude_paths': [CHILD + '/app.py', LEAF]})
                folder.snapshot(exclude_git=True)
                self.assertIn('app.py', folder.files)
                self.assertNotIn(CHILD + '/app.py', folder.files)
                self.assertFalse(any(p.startswith(LEAF + '/') for p in folder.files))
                self.assertEqual({s['path'] for s in self.repo.submodules(pins[branch], verified=True)}, {CHILD, LEAF})
        finally:
            self.assertTrue(self.repo.restore()['restored'])


if __name__ == '__main__':
    unittest.main()
