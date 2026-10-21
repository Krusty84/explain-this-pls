# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Large sources collect evidence before authoring the initial document."""
import json
import subprocess

from fixtures.study_runner import run_case
from test_folder import FolderFixture


class SingleStudyTests(FolderFixture):
    run_case = run_case

    def large_source(self):
        for index in range(1, 302):
            path = 'app.py' if index == 1 else f'part{index}.py'
            (self.source / path).write_text('def main(): return 42\n' + '# source\n' * 120)

    def assert_large_study(self):
        backend = 'codex'
        with self.subTest(backend=backend):
            manifest, code = self.run_case(backend=backend,
                subsystem_paths=['.', 'part2.py', 'part3.py', 'part4.py', 'part5.py'])
            self.assertEqual(code, 0, manifest.get('diagnostics'))
            self.assertEqual([c['context']['phase'] for c in self.calls], ['collect', 'write'])
            document = manifest if self.value['mode'] == 'folder' else manifest['branches'][0]
            self.assertEqual(document['coverage_plan']['counts']['files'], 301)
            self.assertNotIn('coverage', document['study'])
            self.assertTrue(document['study']['program_checks']['policy_satisfied'])
            self.assertFalse(document['study']['program_checks']['coverage']['completeness_measured'])
            self.assertFalse(document['study_invocation']['source_tools_enabled'])
            self.assertEqual(self.calls[0]['cwd'], self.source)
            self.assertNotEqual(self.calls[-1]['cwd'], self.source)
            inventory = json.loads(next(self.runner.session.run_dir.rglob('source.inventory.json')).read_text())
            self.assertGreater(sum(e.get('size', 0) for e in inventory['entries']), 262144)
            self.assertEqual({p.name for p in self.runner.session.run_dir.rglob('*.plan.json')},
                             {'coverage.plan.json', 'review.plan.json'})
            self.assertTrue(all(row['stage'] in ('study', 'review')
                                for row in manifest['metrics']['stages']))
            self.assertNotIn('features.shell_tool=false', self.calls[0]['command'])
            self.assertIn('features.shell_tool=false', self.calls[-1]['command'])

    def test_large_folder(self):
        self.large_source()
        self.assert_large_study()

    def test_large_git_branch(self):
        self.large_source()
        for arguments in (['init', '-b', 'master'], ['add', '.'],
                          ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                           'commit', '-m', 'Large source']):
            subprocess.run(['git', '-C', str(self.source), *arguments], check=True, capture_output=True)
        self.value.update(mode='git', git_mode={'repository': './source', 'branches': ['master'], 'baseline_branch': 'master'})
        self.assert_large_study()
