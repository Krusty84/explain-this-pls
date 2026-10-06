# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Installer checks with no downloads or changes to the developer environment."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('install_explain', ROOT / 'install-explain.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'project with spaces'
        self.root.mkdir()
        (self.root / 'requirements.txt').write_text('')
        root_patch = patch.object(installer, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def install(self):
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return installer.main([])

    def test_real_install_and_rerun_work_from_another_directory_offline(self):
        script = self.root / 'install-explain.py'
        shutil.copyfile(ROOT / 'install-explain.py', script)
        env = os.environ | {'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1',
                            'PIP_CONFIG_FILE': os.devnull}
        command = [sys.executable, '-B', str(script)]
        first = subprocess.run(command, cwd=self.root.parent, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        python = self.root / '.venv' / 'bin' / 'python'
        self.assertTrue(python.is_file())
        self.assertIn('Creating ', first.stdout)
        marker = self.root / '.venv' / 'keep-me'
        marker.write_text('existing environment')
        second = subprocess.run(command, cwd=self.root.parent, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('Reusing ', second.stdout)
        self.assertEqual(marker.read_text(), 'existing environment')
        self.assertEqual(shlex.split(second.stdout.strip().splitlines()[-1]),
                         [str(python), str(self.root / 'explain.py')])

    def test_unsupported_python_or_platform_does_not_create_environment(self):
        for platform, version in [('win32', (3, 13)), ('linux', (3, 10))]:
            with self.subTest(platform=platform, version=version), \
                    patch.object(installer.sys, 'platform', platform), \
                    patch.object(installer.sys, 'version_info', version), \
                    patch.object(installer.subprocess, 'run') as run:
                self.assertEqual(self.install(), 1)
                run.assert_not_called()
        self.assertFalse((self.root / '.venv').exists())
        self.assertIn('Python 3.11+', self.stderr.getvalue())

    def test_missing_requirements_fails_before_creating_environment(self):
        (self.root / 'requirements.txt').unlink()
        with patch.object(installer.subprocess, 'run') as run:
            self.assertEqual(self.install(), 1)
            run.assert_not_called()
        self.assertIn('Requirements file is missing', self.stderr.getvalue())

    def test_incomplete_existing_environment_is_preserved(self):
        environment = self.root / '.venv'
        environment.mkdir()
        marker = environment / 'keep-me'
        marker.write_text('existing data')
        with patch.object(installer.subprocess, 'run') as run:
            self.assertEqual(self.install(), 1)
            run.assert_not_called()
        self.assertEqual(marker.read_text(), 'existing data')
        self.assertIn('Move it aside', self.stderr.getvalue())

    def test_creation_and_dependency_failures_never_report_success(self):
        failure = subprocess.CalledProcessError(1, ['failed-command'])
        for outcomes in ([failure], [None, failure], [None, None, failure], [OSError('permission denied')]):
            with self.subTest(outcomes=outcomes), \
                    patch.object(installer.subprocess, 'run', side_effect=outcomes):
                self.assertEqual(self.install(), 1)
        self.assertIn('Installation failed', self.stderr.getvalue())
        self.assertNotIn('Installation complete', self.stdout.getvalue())

    def test_interrupt_returns_130(self):
        with patch.object(installer.subprocess, 'run', side_effect=KeyboardInterrupt):
            self.assertEqual(self.install(), 130)
        self.assertIn('Installation interrupted', self.stderr.getvalue())
        self.assertNotIn('Installation complete', self.stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
