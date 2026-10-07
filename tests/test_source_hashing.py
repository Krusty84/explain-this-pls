# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Source digests are calculated once; subsequent checks inspect metadata."""
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
import unittest
from unittest.mock import Mock, patch

from explain import AuditError, Folder, Runner, UnsafeRepository, load_config
from src.analysis.evidence import SourceChanged, resolve_evidence
from src.contracts.contracts import validate_schema
from src.contracts.saved_contracts import RESOLUTION
from fixtures.cli_response import cli_result
from fixtures.ledger_response import response
from test_folder import FolderFixture
import test_git_sources as git_fixtures
from test_revision_pipeline import initial_finding
from test_submodules import RecursiveFixture


@contextmanager
def hashed_payloads():
    """Observe both streamed and immediate digests, including accidental rehashes."""
    original, instances, payloads = hashlib.sha256, [], Counter()

    def create(data=b''):
        instance = Mock(wraps=original(data))
        instances.append((bytes(data), instance))
        return instance

    with patch('hashlib.sha256', side_effect=create):
        yield payloads
    for initial, instance in instances:
        payloads[initial + b''.join(call.args[0] for call in instance.update.call_args_list)] += 1


class FolderHashingTests(FolderFixture):
    def test_one_pass_for_preflight_stages_and_revisions(self):
        blob = (self.source / 'app.py').read_bytes()
        for check, review, revise in ((True, False, False), (False, False, False),
                                      (False, True, False), (False, True, True)):
            with self.subTest(check=check, review=review, revise=revise):
                self.value['execution'] = {'review_enabled': review, 'max_revision_rounds': int(revise)}
                runner = Runner(self.config(), self.base / f'run-{check}-{review}-{revise}')
                stages = []

                def process(command, cwd, env, payload, **kwargs):
                    context = json.loads(payload.decode().split('# Authoritative orchestration context (data)\n')[1]
                                         .split('\n\n# Required final JSON Schema')[0])
                    stages.append(context['stage'])
                    data = response(context)
                    if revise:
                        initial_finding(context['stage'], context, data)
                    return cli_result(command, data)

                with hashed_payloads() as hashes, patch.object(runner, 'check_cli', return_value={}), \
                        patch('explain.process', side_effect=process), \
                        patch.object(Folder, 'snapshot', autospec=True, side_effect=Folder.snapshot) as snapshots:
                    manifest, code = runner.run(check_only=check)
                self.assertEqual(code, 0, manifest['errors'])
                self.assertEqual(snapshots.call_count, 1)
                self.assertEqual(hashes[blob], 1)
                self.assertEqual(len(stages), 0 if check else 5 if revise else 3 if review else 2)
                for revision in manifest.get('revisions', []):
                    for stage in ('study', 'review'):
                        for evidence in (revision.get(stage) or {}).get('program_checks', {}).get('evidence', []):
                            self.assertNotIn('fragment_sha256', evidence)

    def test_metadata_changes_fail_without_content_reads_or_hashing(self):
        mutations = ('contents', 'restored_mtime', 'timestamp', 'added', 'deleted', 'renamed',
                     'permissions', 'directory', 'directory_replacement', 'file_replacement',
                     'symlink', 'root_replacement')
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                root = self.base / mutation
                (root / 'src').mkdir(parents=True)
                path = root / 'src/app.py'
                path.write_bytes(b'AAAA\n')
                folder = Folder(root)
                fingerprint = folder.snapshot()['source_fingerprint']
                before = path.stat()
                if mutation in ('contents', 'restored_mtime'):
                    path.write_bytes(b'BBBB\n')
                    if mutation == 'restored_mtime':
                        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                elif mutation == 'timestamp':
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
                elif mutation == 'added':
                    (root / 'new').write_text('new')
                elif mutation == 'deleted':
                    path.unlink()
                elif mutation == 'renamed':
                    path.rename(root / 'renamed')
                elif mutation == 'permissions':
                    path.chmod(0o700)
                elif mutation == 'directory':
                    (root / 'empty').mkdir()
                elif mutation == 'directory_replacement':
                    (root / 'src').rename(root / 'old-src')
                    (root / 'src').mkdir()
                    os.link(root / 'old-src/app.py', path)
                elif mutation == 'root_replacement':
                    root.rename(self.base / 'old-root')
                    root.mkdir()
                else:
                    path.rename(root / 'old-file')
                    if mutation == 'symlink':
                        path.symlink_to(root / 'old-file')
                    else:
                        path.write_bytes(b'AAAA\n')
                with patch('os.read', side_effect=AssertionError('Guard read file contents')), \
                        patch('hashlib.sha256', side_effect=AssertionError('Guard recalculated a digest')), \
                        self.assertRaises(AuditError):
                    folder.assert_snapshot(fingerprint)

    def test_evidence_reuses_pins_and_legacy_snippet_hash_remains_readable(self):
        folder = Folder(self.source)
        inventory = folder.snapshot()
        files = {e['path']: e['sha256'] for e in inventory['entries'] if e['type'] == 'file'}
        context = {'source_directory': str(self.source), 'source_fingerprint': inventory['source_fingerprint']}
        pointer = dict(id='E-001', source_id='source-001', path='app.py', start_line=1, end_line=1, quote='')
        with patch('src.analysis.evidence.sha', side_effect=AssertionError('Evidence recalculated a digest')):
            for stage in ('study', 'review'):
                result = resolve_evidence(stage, [pointer, pointer | {'id': 'E-002'}], context, files,
                                          expected_metadata=folder.metadata)[0]
                self.assertEqual(result['status'], 'RESOLVED')
                self.assertEqual(result['file_sha256'], files['app.py'])
                self.assertNotIn('fragment_sha256', result)
                validate_schema(result, RESOLUTION)
                validate_schema(result | {'fragment_sha256': 'legacy'}, RESOLUTION)
            with patch('src.analysis.evidence.MAX_FILE_BYTES', 1):
                limited = resolve_evidence('study', [pointer], context, files, expected_metadata=folder.metadata)
            self.assertEqual(limited[0]['status'], 'LIMIT_EXCEEDED')
            (self.source / 'app.py').write_text('changed before evidence\n')
            with self.assertRaises(SourceChanged):
                resolve_evidence('study', [pointer], context, files, expected_metadata=folder.metadata)

    def test_changes_during_first_pinned_evidence_read_are_fatal(self):
        folder = Folder(self.source)
        inventory = folder.snapshot()
        files = {e['path']: e['sha256'] for e in inventory['entries'] if e['type'] == 'file'}
        context = {'source_directory': str(self.source), 'source_fingerprint': inventory['source_fingerprint']}
        pointer = dict(id='E-001', source_id='source-001', path='app.py', start_line=1, end_line=1, quote='')
        original = os.read

        def change(fd, count):
            data = original(fd, count)
            if data:
                (self.source / 'app.py').write_bytes(b'x' * len(data))
            return data

        with patch('os.read', side_effect=change), \
                patch('src.analysis.evidence.sha', side_effect=AssertionError('Evidence recalculated a digest')), \
                self.assertRaises(SourceChanged):
            resolve_evidence('study', [pointer], context, files, expected_metadata=folder.metadata)

    def test_standalone_decode_failures_hash_a_file_once(self):
        (self.source / 'app.py').write_bytes(b'\xff')
        context = {'source_directory': str(self.source), 'source_fingerprint': 'standalone'}
        pointer = dict(id='E-001', source_id='source-001', path='app.py', start_line=1, end_line=1, quote='')
        with hashed_payloads() as hashes:
            results = resolve_evidence('study', [pointer, pointer | {'id': 'E-002'}], context)
        self.assertEqual([r['status'] for r in results], ['DECODE_ERROR', 'DECODE_ERROR'])
        self.assertEqual(hashes[b'\xff'], 1)


class GitHashingTests(unittest.TestCase):
    setUp = git_fixtures.GitSourceTests.setUp
    git = git_fixtures.GitSourceTests.git
    write = git_fixtures.GitSourceTests.write
    prepare = git_fixtures.GitSourceTests.prepare
    dirty = git_fixtures.GitSourceTests.dirty

    def test_preparation_and_guards_reuse_digests_including_ignore_rules(self):
        self.dirty()
        self.git('config', 'core.excludesFile', str(self.path / '.gitignore'))
        sources, pins = self.prepare()
        rules = (self.path / '.gitignore').read_bytes()
        with hashed_payloads() as hashes:
            working = sources.working('main')
            committed = sources.commit('alias', pins['alias'])
            for _ in range(3):
                sources.assert_intact()
        self.assertEqual(hashes[b'WORKING_SENTINEL\n'], 1)
        self.assertEqual(hashes[b'HEAD_SENTINEL\n'], 1)
        self.assertEqual(hashes[rules], 1)
        self.assertFalse(any('_stamp' in e for e in working['inventory']['entries']))
        self.assertEqual(working['entries']['app.py']['sha256'],
                         next(e['sha256'] for e in working['inventory']['entries'] if e['path'] == 'app.py'))
        (committed['path'] / 'app.py').write_bytes(b'corrupt\n')
        with patch('hashlib.sha256', side_effect=AssertionError('Guard recalculated a digest')), \
                self.assertRaises(UnsafeRepository):
            sources.assert_intact()

    def test_copy_changes_fail_without_rehashing_original(self):
        sources, _ = self.prepare()
        scan = sources.working_scan

        def change(destination=None):
            result = scan(destination)
            if destination is not None:
                self.write('app.py', 'CHANGED_SENTINEL\n')
            return result

        with hashed_payloads() as hashes, patch.object(sources, 'working_scan', side_effect=change):
            with self.assertRaises(UnsafeRepository):
                sources.working('main')
        self.assertEqual(hashes[b'HEAD_SENTINEL\n'], 1)
        self.assertEqual(hashes[b'CHANGED_SENTINEL\n'], 0)

    def test_prepared_copy_changes_cannot_receive_a_new_baseline(self):
        sources, _ = self.prepare()
        scan = sources.working_scan

        def change(destination=None):
            result = scan(destination)
            if destination is not None:
                (destination / 'app.py').write_bytes(b'COPY_SENTINEL\n')
            return result

        with hashed_payloads() as hashes, patch.object(sources, 'working_scan', side_effect=change):
            with self.assertRaises(AuditError) as caught:
                sources.working('main')
        self.assertEqual(caught.exception.code, 'SOURCE_CHANGED')
        self.assertEqual(hashes[b'HEAD_SENTINEL\n'], 1)
        self.assertEqual(hashes[b'COPY_SENTINEL\n'], 0)


class RecursiveHashingTests(RecursiveFixture, unittest.TestCase):
    def test_submodules_stages_revisions_and_comparison_share_initial_hashes(self):
        self.addCleanup(self.repo.close)
        self.config['execution']['max_revision_rounds'] = 1
        self.write_config()
        runner = Runner(load_config(self.config_path), self.base / 'hash-run')
        stages = []

        def process(command, cwd, env, payload, **kwargs):
            context = json.loads(payload.decode().split('# Authoritative orchestration context (data)\n')[1]
                                 .split('\n\n# Required final JSON Schema')[0])
            stages.append(context['stage'])
            data = response(context)
            if context.get('branch') == 'master':
                initial_finding(context['stage'], context, data)
            return cli_result(command, data)

        with hashed_payloads() as hashes, patch.object(runner, 'check_cli', return_value={}), \
                patch('explain.process', side_effect=process):
            manifest, code = runner.run()
        self.assertEqual(code, 0, manifest['errors'])
        self.assertIn('compare', stages)
        self.assertEqual(stages.count('study'), 3)
        self.assertEqual(hashes[b'original\n'], 3)  # One file in each working submodule/root.
        for path in self.paths:
            self.assertEqual(hashes[f'topic: {path}\n'.encode()], 1)
        for path in (self.path, self.path / 'vendor/модуль with spaces'):
            self.assertEqual(hashes[(path / '.gitmodules').read_bytes()], 2)  # Working and commit snapshots.


if __name__ == '__main__':
    unittest.main()
