# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from src.reports.document_rendering import materialize_study, recover_sections
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.contracts.contracts import ContractError, FOLDER_SCHEMAS, SCHEMAS, MODEL_SCHEMAS, MODEL_FOLDER_SCHEMAS, jsonc, validate_result
from explain import AuditError, Folder, Runner, load_config, repository_lock
from test_explain import review
from fixtures.ledger_response import response as ledger_response

ROOT = Path(__file__).resolve().parents[1]


class JSONCTests(unittest.TestCase):
    def test_comments_trailing_commas_and_strings(self):
        value = jsonc(r'''{
          // Human-readable settings.
          "url": "https://example.invalid/a//b/*c*/",
          "quote": "say \"hi\" // text",
          "backslash": "\\",
          "array": [1, 2, /* explanation */],
          "object": {"enabled": true,},
        } // end''')
        self.assertEqual(value, {'url': 'https://example.invalid/a//b/*c*/',
            'quote': 'say "hi" // text', 'backslash': '\\',
            'array': [1, 2], 'object': {'enabled': True}})

    def test_malformed_json_is_not_repaired(self):
        for text in ('[,]', '{,}', '[1,,]', '{"x":,}', '[1/* gap */2]',
                     '{"x": "unterminated}', '{/* unclosed'):
            with self.subTest(text=text), self.assertRaises(ContractError):
                jsonc(text)

    def test_duplicate_keys_and_nonfinite_values_stay_invalid(self):
        for text in ('{"x":1, /* duplicate */ "x":2,}', '[NaN,]', '[Infinity,]', '[1e9999,]'):
            with self.subTest(text=text), self.assertRaises(ContractError):
                jsonc(text)

    def test_error_locations_match_original_file(self):
        with self.assertRaisesRegex(ContractError, 'line 3 column 8'):
            jsonc('// heading\n{\n  "x": ,\n}')
        with self.assertRaisesRegex(ContractError, 'line 2 column 2'):
            jsonc('{\n /* unfinished\n comment')


class FolderFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / 'app.py').write_text('def main(): return 42\n')
        self.config_path = self.base / 'config.jsonc'
        self.value = {'mode': 'folder', 'folder_mode': {'path': './source'},
            'reports_dir': './reports', 'project_description': 'ERP-система 1995 года.',
            'agent': {'backend': 'codex', 'executable': sys.executable}}

    def config(self):
        # Existing fixtures exercise the retained strict publication contract.
        self.value.setdefault('result_policy', 'strict')
        self.config_path.write_text(json.dumps(self.value))
        return load_config(self.config_path)


class ModeConfigTests(FolderFixture):
    def test_study_agent_and_custom_prompt_in_both_modes(self):
        prompt = self.base / 'custom-study.md'
        prompt.write_text('# Study the source architecture')
        self.value.update(
            git_mode={'repository': './source', 'branches': ['master', 'customer'], 'baseline_branch': 'master'},
            stage_agents={'study': {'model': 'study-model'}},
            prompts={'study': './custom-study.md'})
        for mode in ('git', 'folder'):
            with self.subTest(mode=mode):
                self.value['mode'] = mode
                cfg = self.config()
                self.assertEqual(cfg['_agents']['study']['model'], 'study-model')
                self.assertEqual(cfg['_prompt_paths']['study'], str(prompt))

    def test_document_stage_configuration_is_rejected_in_both_modes(self):
        self.value['git_mode'] = {
            'repository': './source', 'branches': ['master', 'customer'], 'baseline_branch': 'master'}
        for mode in ('git', 'folder'):
            self.value['mode'] = mode
            for key, value, error in (
                ('stage_agents', {}, 'Unknown stage override: document'),
                ('prompts', str(ROOT / 'prompts/study.md'), 'Unknown prompt stage'),
            ):
                with self.subTest(mode=mode, key=key):
                    self.value[key] = {'document': value}
                    with self.assertRaisesRegex(AuditError, error):
                        self.config()
                    del self.value[key]

    def test_folder_ignores_inactive_source_and_compare_settings(self):
        self.value.update(git_mode={'repository': None, 'branches': 'not checked'},
            stage_agents={'compare': {'backend': 'unavailable', 'executable': '/missing/cli'}},
            prompts={'compare': '/missing/prompt'})
        cfg = self.config()
        self.assertEqual(cfg['folder_mode']['path'], str(self.source))
        self.assertEqual(set(cfg['_agents']), {'catalog', 'study', 'review'})
        self.assertEqual(set(cfg['_prompt_paths']), {'catalog', 'study', 'review', 'revise'})

    def test_git_sections_are_required(self):
        git = {'repository': './source', 'branches': ['master', 'customer'], 'baseline_branch': 'master'}
        self.value.update(mode='git', git_mode=git, folder_mode={'path': None})
        grouped = self.config()
        self.assertEqual(Runner(grouped, self.base / 'run').source_path, self.source)
        self.value.pop('mode'); self.value.pop('git_mode'); self.value.pop('folder_mode')
        self.value.update(git)
        with patch('explain.process') as process, self.assertRaisesRegex(AuditError, 'Unknown configuration keys'):
            self.config()
        process.assert_not_called()

    def test_invalid_modes_sections_and_mixed_formats(self):
        original = copy.deepcopy(self.value)
        cases = [dict(mode='auto'), dict(mode=None), dict(folder_mode=[]),
                 dict(folder_mode={}), dict(git_mode=[]), dict(repository='./source'),
                 dict(folder_mode={'path': './source', 'branches': []}),
                 dict(folder_mode={'path': 42}), dict(reports_dir='./source/reports'),
                 dict(prompts={'study': None})]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(AuditError):
                self.value = original | changes
                self.config()
        self.value = copy.deepcopy(original)
        del self.value['mode']
        with self.assertRaisesRegex(AuditError, 'Missing configuration key: mode'):
            self.config()
        self.value = copy.deepcopy(original)
        del self.value['folder_mode']
        with self.assertRaisesRegex(AuditError, 'Missing configuration section'):
            self.config()

    def test_json_stays_strict_and_jsonc_roundtrips_as_json(self):
        text = '// comments are for JSONC only\n' + json.dumps(self.value)
        self.config_path.write_text(text)
        cfg = load_config(self.config_path)
        snapshot = {k: v for k, v in cfg.items() if not k.startswith('_')}
        strict_path = self.base / 'config.json'
        strict_path.write_text(json.dumps(snapshot))
        self.assertEqual(load_config(strict_path), cfg)
        strict_path.write_text(text)
        with self.assertRaises(ContractError):
            load_config(strict_path)

    def test_examples_also_support_switching_to_folder(self):
        for path in ROOT.glob('config*.example.jsonc'):
            with self.subTest(path=path.name):
                self.value = jsonc(path.read_text())
                self.value['mode'] = 'folder'
                self.value['folder_mode']['path'] = str(self.source)
                with patch('explain.shutil.which', return_value=sys.executable):
                    self.assertEqual(set(self.config()['_agents']), {'catalog', 'study', 'review'})


class FolderInventoryTests(FolderFixture):
    def test_inventory_is_deterministic_and_streams_hidden_and_binary_files(self):
        (self.source / '.settings').write_bytes(b'\x00\xff' * 700000)
        (self.source / 'empty').mkdir()
        first = Folder(self.source).snapshot()
        self.assertEqual(first, Folder(self.source).snapshot())
        files = {entry['path']: entry for entry in first['entries']}
        self.assertEqual(files['.settings']['sha256'], hashlib.sha256(b'\x00\xff' * 700000).hexdigest())
        self.assertEqual(files['empty']['type'], 'directory')
        os.utime(self.source / 'app.py', None)
        self.assertEqual(first['source_fingerprint'], Folder(self.source).snapshot()['source_fingerprint'])

    def test_content_names_permissions_and_empty_directories_affect_fingerprint(self):
        folder = Folder(self.source)
        actions = [lambda: (self.source / 'app.py').write_text('def main(): return 43\n'),
                   lambda: (self.source / 'added').write_text('new'),
                   lambda: (self.source / 'added').unlink(),
                   lambda: (self.source / 'app.py').chmod(0o700),
                   lambda: (self.source / 'empty').mkdir(),
                   lambda: (self.source / 'app.py').rename(self.source / 'renamed.py')]
        for action in actions:
            fingerprint = folder.snapshot()['source_fingerprint']
            action()
            with self.assertRaisesRegex(AuditError, 'Source folder changed'):
                folder.assert_snapshot(fingerprint)

    def test_links_are_recorded_without_following_targets_or_cycles(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'data').write_text('first')
        (self.source / 'link').symlink_to(outside, target_is_directory=True)
        (self.source / 'cycle').symlink_to(self.source, target_is_directory=True)
        (self.source / 'broken').symlink_to(self.base / 'missing')
        initial = Folder(self.source).snapshot()
        link = next(e for e in initial['entries'] if e['path'] == 'link')
        self.assertEqual(link['target'], str(outside))
        self.assertEqual(len(initial['entries']), 5)
        (outside / 'data').write_text('changed outside scope')
        Folder(self.source).assert_snapshot(initial['source_fingerprint'])
        (self.source / 'link').unlink()
        (self.source / 'link').symlink_to(self.base / 'another')
        with self.assertRaises(AuditError):
            Folder(self.source).assert_snapshot(initial['source_fingerprint'])

    def test_special_files_are_rejected_without_opening(self):
        os.mkfifo(self.source / 'pipe')
        with self.assertRaisesRegex(AuditError, 'Unsupported special file.*pipe'):
            Folder(self.source).snapshot()

    @unittest.skipIf(os.geteuid() == 0, 'Permission checks require a non-root user.')
    def test_unreadable_file_is_an_error(self):
        path = self.source / 'app.py'
        path.chmod(0)
        try:
            with self.assertRaisesRegex(AuditError, 'Cannot read source folder'):
                Folder(self.source).snapshot()
        finally:
            path.chmod(0o600)

    def test_changes_during_file_read_are_rejected(self):
        real_read = os.read
        changed = False
        def changing_read(fd, size):
            nonlocal changed
            data = real_read(fd, size)
            if data and not changed:
                changed = True
                (self.source / 'app.py').write_text('changed while hashing')
            return data
        with patch('explain.os.read', side_effect=changing_read):
            with self.assertRaisesRegex(AuditError, 'changed while fingerprinting'):
                Folder(self.source).snapshot()

    def test_non_directory_and_missing_source_are_errors(self):
        for path in (self.source / 'missing', self.source / 'app.py'):
            with self.subTest(path=path), self.assertRaisesRegex(AuditError, 'Cannot read source folder'):
                Folder(path).snapshot()

    def test_lock_covers_folder_and_its_alias(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.source, target_is_directory=True)
        with repository_lock(self.source):
            with self.assertRaisesRegex(AuditError, 'holds this repository lock'):
                with repository_lock(alias):
                    pass


class FolderContractTests(unittest.TestCase):
    def test_identity_is_checked(self):
        from src.analysis.ledger import review_context
        context = {'source_directory': '/source', 'source_fingerprint': 'abc'}
        context = review_context(materialize_study(ledger_response(context)), context)
        data = ledger_response(context)
        validate_result('review', data, context, 'folder')
        for changed in ({'source_directory': '/other'}, {'source_fingerprint': 'wrong'}):
            with self.subTest(changed=changed), self.assertRaises(ContractError):
                validate_result('review', data | changed, context, 'folder')
        with self.assertRaises(ContractError):
            validate_result('review', data | {'branch': 'invented'}, context, 'folder')

    def test_checked_in_schemas_match_runtime(self):
        for prefix, schemas in (('', MODEL_SCHEMAS), ('folder-', MODEL_FOLDER_SCHEMAS)):
            for stage, schema in schemas.items():
                self.assertEqual(json.loads((ROOT / f'schemas/{prefix}{stage}.schema.json').read_text()), schema)


class FolderPipelineTests(FolderFixture):
    def run_pipeline(self, doc_status='COMPLETE', fail_stage=None, mutate_review=False):
        runner = Runner(self.config(), self.base / 'run')
        stages = []
        def response(command, cwd, env, payload, **options):
            context = json.loads(payload.decode().split('# Authoritative orchestration context (data)\n')[1]
                                 .split('\n\n# Required final JSON Schema')[0])
            stage = context.get('stage') or ('review' if 'architecture_document' in context else 'study')
            stages.append(stage)
            if stage == fail_stage:
                return {'returncode': 1, 'duration_seconds': 0,
                        'stdout': b'', 'stderr': b'failed'}
            data = ledger_response(context)
            if stage == 'study':
                data.update(completion_status=doc_status,
                    limitations=[] if doc_status == 'COMPLETE' else ['Investigation incomplete.'])
            data.pop('branch', None); data.pop('source_commit', None)
            if stage == 'review' and mutate_review:
                (self.source / 'app.py').unlink()
            return {'returncode': 0, 'duration_seconds': 0,
                    'stdout': json.dumps(data).encode(), 'stderr': b''}
        with patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=response):
            manifest, code = runner.run()
        return manifest, code, stages

    def test_blocked_document_skips_review_without_operational_failure(self):
        manifest, code, stages = self.run_pipeline(doc_status='BLOCKED')
        self.assertEqual(code, 2)
        self.assertEqual(manifest['status'], 'PARTIAL')
        self.assertEqual(stages, ['catalog', 'study'])
        self.assertEqual(manifest['errors'], [])

    def test_partial_document_is_reviewed_but_not_accepted(self):
        manifest, code, stages = self.run_pipeline(doc_status='PARTIAL')
        self.assertEqual(code, 2)
        self.assertFalse(manifest['accepted'])
        self.assertEqual(stages, ['catalog', 'study', 'review'])

    def test_stage_failure_stops_even_with_continue_on_error(self):
        for stage in ('study', 'review'):
            with self.subTest(stage=stage):
                manifest, code, stages = self.run_pipeline(fail_stage=stage)
                self.assertEqual(code, 1)
                self.assertEqual(manifest['status'], 'FAILED')
                self.assertIsNone(manifest[stage])
                self.assertEqual(stages[-1], stage)

    def test_mutation_during_review_prevents_acceptance_and_review_publication(self):
        manifest, code, stages = self.run_pipeline(mutate_review=True)
        self.assertEqual(code, 1)
        self.assertIsNotNone(manifest['study'])
        self.assertIsNone(manifest['review'])
        self.assertFalse(manifest['accepted'])
        self.assertFalse((self.base / 'run/ARCHITECTURE_REVIEW.md').exists())

    def test_change_before_invocation_prevents_model_call(self):
        runner = Runner(self.config(), self.base / 'run')
        fingerprint = runner.folder.snapshot()['source_fingerprint']
        (self.source / 'app.py').write_text('changed before stage')
        context = {'source_directory': str(self.source), 'source_fingerprint': fingerprint}
        with patch('explain.process') as process:
            with self.assertRaisesRegex(AuditError, 'Source folder changed'):
                runner.invoke('study', context, self.base / 'run/study.logs')
            process.assert_not_called()


class FolderCLIIntegrationTests(FolderFixture):
    def setUp(self):
        super().setUp()
        home = self.base / 'home'; home.mkdir()
        (home / 'audit-profile.json').write_text(json.dumps({'model': 'configured-model'}))
        self.cli = self.base / 'cli'
        self.cli.write_text('#!' + sys.executable + '\n' + ('import sys; sys.path.insert(0, ' + repr(str(ROOT / 'tests/fixtures')) + ')' + '\n' + (ROOT / 'tests/fixtures/fake_cli.py').read_text()))
        self.cli.chmod(0o700)
        self.calls = self.base / 'calls.jsonl'
        # An empty PATH makes any accidental Git invocation fail.
        self.env = {'HOME': str(home), 'PATH': '', 'PYTHONDONTWRITEBYTECODE': '1',
            'AUDIT_TEST_CALL_LOG': str(self.calls), 'PYTHONIOENCODING': 'utf-8',
            'OPENCODE_CONFIG_CONTENT': json.dumps({'provider': {'custom': {'options': {
                'baseURL': 'https://example.invalid'}}}, 'plugin': ['auth-plugin']})}
        self.value['agent']['executable'] = str(self.cli)
        self.value.update(git_mode={'repository': '/missing/repository'},
            stage_agents={'compare': {'backend': 'missing', 'executable': '/missing/cli'}},
            prompts={'compare': '/missing/prompt'})

    def execute(self, check=False):
        self.config_path.write_text('// configuration with comments\n' + json.dumps(self.value))
        cmd = [sys.executable, '-B', str(ROOT / 'explain.py'), '--config', str(self.config_path)]
        if check:
            cmd.append('--check')
        return subprocess.run(cmd, cwd=self.base, env=self.env, capture_output=True, text=True, timeout=30)

    def test_all_backends_check_and_run_without_git(self):
        before = Folder(self.source).snapshot()
        for backend in ('codex', 'claude-code', 'opencode'):
            for check in (True, False):
                with self.subTest(backend=backend, check=check):
                    self.calls.write_text('')
                    self.value['agent']['backend'] = backend
                    self.value['project_description'] = '' if check else 'ERP-система 1995 года.'
                    result = self.execute(check)
                    if backend == 'opencode':
                        # This fixture lacks the required HTTP interface; the pipeline is
                        # covered separately, with its upstream capability limit explicit.
                        self.assertEqual(result.returncode, 1, result.stderr)
                        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
                        self.assertFalse(any('context' in call for call in calls))
                        self.assertEqual(Folder(self.source).snapshot(), before)
                        continue
                    self.assertEqual(result.returncode, 0, result.stderr)
                    output = json.loads(result.stdout)
                    self.assertEqual(output['status'], 'PREFLIGHT_OK' if check else 'COMPLETE')
                    self.assertEqual(result.stderr.count('Project description is missing.'), int(check))
                    run = Path(output['manifest']).parent
                    manifest = json.loads((run / 'manifest.json').read_text())
                    self.assertEqual(manifest['contract_id'], 'evidence-ledger')
                    self.assertEqual(manifest['artifact_format'], 'evidence-ledger-artifacts')
                    self.assertEqual(manifest['source_fingerprint'], before['source_fingerprint'])
                    self.assertEqual(json.loads((run / 'source.inventory.json').read_text()), before)
                    snapshot = json.loads((run / 'config.snapshot.json').read_text())
                    self.assertEqual(snapshot['folder_mode']['path'], str(self.source))
                    calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
                    invocations = [c for c in calls if 'context' in c]
                    self.assertEqual(len(calls), 2 if check else 5)
                    self.assertEqual(len(invocations), 0 if check else 3)
                    self.assertFalse((run / 'comparison').exists())
                    self.assertFalse((self.source / '.git').exists())
                    for call in invocations:
                        context, args = call['context'], call['args']
                        for field in ('contract_id', 'artifact_format', 'context_format'):
                            self.assertNotIn(field, call['schema']['properties'])
                            self.assertNotIn(field, call['schema']['required'])
                        self.assertEqual(call['cwd'], str(self.source))
                        self.assertEqual(context['project_description'], self.value['project_description'])
                        self.assertNotIn('entries', context)
                        self.assertNotIn('branch', context)
                        if backend == 'codex':
                            self.assertIn('--skip-git-repo-check', args)
                            self.assertNotIn('features.shell_tool=false', args)
                        elif backend == 'claude-code':
                            self.assertEqual(args[args.index('--tools') + 1], 'Read,Glob,Grep')
                        else:
                            self.assertEqual(call['permissions'], {'*': 'deny', 'read': 'allow',
                                'glob': 'allow', 'grep': 'allow', 'list': 'allow'})
                    if not check:
                        self.assertNotIn('architecture_document', invocations[0]['context'])
                        self.assertEqual(invocations[2]['context']['architecture_document']['report_markdown'], manifest['study']['report_markdown'])
                        review_context = invocations[2]['context']
                        self.assertNotIn('document_sha256', review_context)
                        self.assertNotIn('source_fingerprint', review_context)
                        bindings = json.loads((run / 'revisions/001/review.logs/attempt-001/binding.json').read_text())
                        target = next(mapping['identity'] for mapping in bindings['mappings']
                                      if mapping['id'] == review_context['review_target_id'])
                        self.assertEqual(target['document_sha256'], manifest['study_invocation']['report_sha256'])
                        self.assertEqual(len({call['context']['source_snapshot_id'] for call in invocations}), 1)
                        self.assertEqual(manifest['study_invocation']['stage'], 'study')
                        self.assertNotIn('document', manifest)
                        self.assertNotIn('document_invocation', manifest)
                        self.assertFalse((run / 'document.json').exists())
                        self.assertFalse((run / 'document.logs').exists())
                        self.assertTrue((run / 'ARCHITECTURE.md').is_file())
                        self.assertTrue((run / 'ARCHITECTURE_REVIEW.md').is_file())
                        for stage in ('study', 'review'):
                            data = json.loads((run / f'{stage}.json').read_text())
                            self.assertEqual(data, manifest[stage])
                            meta = json.loads((run / 'revisions/001' / f'{stage}.logs/invocation.json').read_text())
                            self.assertEqual(meta['stage'], stage)
                            for field in ('contract_id', 'artifact_format', 'context_format'):
                                self.assertNotIn(field, data)
                            self.assertEqual(data['source_fingerprint'], before['source_fingerprint'])
                    self.assertEqual(Folder(self.source).snapshot(), before)

    def test_mutation_stops_before_publishing_or_review_and_is_not_restored(self):
        self.env['AUDIT_TEST_MUTATE_SOURCE'] = '1'
        result = self.execute()
        self.assertEqual(result.returncode, 1, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['status'], 'FAILED')
        run = Path(output['manifest']).parent
        self.assertTrue((self.source / 'modified.txt').exists())
        self.assertFalse((run / 'ARCHITECTURE.md').exists())
        manifest = json.loads((run / 'manifest.json').read_text())
        self.assertIsNone(manifest['study'])
        self.assertIsNone(manifest['review'])
        meta = json.loads((run / 'catalog.logs/invocation.json').read_text())
        self.assertEqual(meta['status'], 'FAILED')
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len([c for c in calls if 'context' in c]), 1)

    def test_preflight_checks_skip_git_capability(self):
        self.cli.write_text(self.cli.read_text().replace('--skip-git-repo-check', ''))
        result = self.execute(check=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('--skip-git-repo-check', result.stderr)
        self.assertFalse(any('context' in json.loads(line) for line in self.calls.read_text().splitlines()))


if __name__ == '__main__':
    unittest.main()
