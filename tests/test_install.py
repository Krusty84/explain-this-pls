# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Installer checks with no downloads or changes to the developer environment."""
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import certifi

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
        jar_source = self.root / 'download.jar'
        jar_source.write_bytes(b'offline PlantUML fixture')
        contents = (ROOT / 'install-explain.py').read_text()
        script.write_text(contents.replace("if __name__ == '__main__':",
            'PLANTUML_URL = ' + repr(jar_source.as_uri()) + '\nPLANTUML_SHA256 = ' +
            repr(hashlib.sha256(jar_source.read_bytes()).hexdigest()) + "\nif __name__ == '__main__':"))
        package = self.root / 'src'
        package.mkdir()
        for name in ('__init__.py', 'messages.py'):
            shutil.copyfile(ROOT / 'src' / name, package / name)
        java = self.root.parent / 'java with spaces' / 'java'
        java.parent.mkdir()
        java.write_text('#!/bin/sh\nif [ "$1" != "-version" ]; then\n'
                        '  [ -f "$3" ] || exit 1\n'
                        "  printf 'SEQUENCE\\n(2 participants)\\n'\nfi\n")
        java.chmod(0o755)
        env = os.environ | {'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1',
                            'PIP_CONFIG_FILE': os.devnull, 'PATH': str(java.parent)}
        command = [sys.executable, '-B', str(script)]
        first = subprocess.run(command, cwd=self.root.parent, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        python = self.root / '.venv' / 'bin' / 'python'
        self.assertTrue(python.is_file())
        jar = self.root / '.venv' / 'share' / 'plantuml' / 'plantuml.jar'
        self.assertEqual(jar.read_bytes(), jar_source.read_bytes())
        self.assertTrue(os.access(self.root / '.venv' / 'bin' / 'plantuml', os.X_OK))
        self.assertIn('Creating ', first.stdout)
        marker = self.root / '.venv' / 'keep-me'
        marker.write_text('existing environment')
        jar_source.unlink()
        second = subprocess.run(command, cwd=self.root.parent, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('Reusing ', second.stdout)
        self.assertIn('Reusing PlantUML ', second.stdout)
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

    def test_child_python_check_receives_its_message_as_an_argument(self):
        with patch.object(installer.subprocess, 'run') as run, \
                patch.object(installer, 'install_plantuml') as plantuml:
            self.assertEqual(self.install(), 0)
        plantuml.assert_called_once_with(self.root / '.venv')
        command = run.call_args_list[1].args[0]
        self.assertIn('sys.exit(sys.argv[1])', command[2])
        self.assertEqual(command[3],
                         'The existing .venv needs Python 3.11+. Move it aside and rerun the installer.')

    def test_plantuml_failure_never_reports_installation_complete(self):
        with patch.object(installer.subprocess, 'run'), \
                patch.object(installer, 'install_plantuml', side_effect=RuntimeError('PlantUML failed')):
            self.assertEqual(self.install(), 1)
        self.assertIn('PlantUML failed', self.stderr.getvalue())
        self.assertNotIn('Installation complete', self.stdout.getvalue())


class PlantUMLInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.environment = Path(temporary.name) / 'environment with spaces'
        (self.environment / 'bin').mkdir(parents=True)
        self.jar = self.environment / 'share' / 'plantuml' / 'plantuml.jar'
        self.content = b'PlantUML fixture'
        for target, options in (
                ('PLANTUML_SHA256', {'new': hashlib.sha256(self.content).hexdigest()}),
                ('urlopen', {'return_value': io.BytesIO(self.content)})):
            patcher = patch.object(installer, target, **options)
            value = patcher.start()
            if target == 'urlopen':
                self.download = value
            self.addCleanup(patcher.stop)
        which = patch.object(installer.shutil, 'which', return_value='/java with spaces/bin/java')
        self.which = which.start()
        self.addCleanup(which.stop)
        def run(command, **kwargs):
            output = certifi.where() if command[-2:] == ['-m', 'certifi'] else 'SEQUENCE\n(2 participants)\n'
            return subprocess.CompletedProcess(command, 0, stdout=output)
        process = patch.object(installer.subprocess, 'run', side_effect=run)
        self.process = process.start()
        self.addCleanup(process.stop)

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install_plantuml(self.environment)

    def test_download_is_verified_and_launcher_preserves_paths_and_arguments(self):
        self.install()
        self.download.assert_called_once()
        context = self.download.call_args.kwargs['context']
        self.assertEqual(self.download.call_args.args, (installer.PLANTUML_URL,))
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(self.jar.read_bytes(), self.content)
        launcher = self.environment / 'bin' / 'plantuml'
        self.assertTrue(os.access(launcher, os.X_OK))
        command = shlex.split(launcher.read_text().splitlines()[1])
        self.assertEqual(command, ['exec', '/java with spaces/bin/java', '-Djava.awt.headless=true',
                                  '-jar', str(self.jar), '$@'])
        self.assertEqual(self.process.call_args.args[0], [str(launcher), '-charset', 'UTF-8', '-syntax'])

    def test_verified_download_is_reused_without_network(self):
        self.jar.parent.mkdir(parents=True)
        self.jar.write_bytes(self.content)
        self.install()
        self.download.assert_not_called()

    def test_corrupted_cache_is_replaced_by_a_verified_download(self):
        self.jar.parent.mkdir(parents=True)
        self.jar.write_bytes(b'corrupted cache')
        self.install()
        self.assertEqual(self.jar.read_bytes(), self.content)
        self.download.assert_called_once()

    def test_checksum_failure_preserves_existing_files_and_removes_partial_download(self):
        self.jar.parent.mkdir(parents=True)
        self.jar.write_bytes(b'existing file')
        launcher = self.environment / 'bin' / 'plantuml'
        launcher.write_text('existing launcher')
        self.download.return_value = io.BytesIO(b'wrong download')
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            self.install()
        self.assertEqual(self.jar.read_bytes(), b'existing file')
        self.assertEqual(launcher.read_text(), 'existing launcher')
        self.assertEqual(list(self.jar.parent.iterdir()), [self.jar])

    def test_missing_or_unusable_java_fails_without_download(self):
        self.which.return_value = None
        with self.assertRaisesRegex(RuntimeError, r'Java 11\+'):
            self.install()
        self.which.return_value = '/java'
        self.process.side_effect = subprocess.CalledProcessError(1, ['/java', '-version'])
        with self.assertRaisesRegex(RuntimeError, r'Java 11\+'):
            self.install()
        self.download.assert_not_called()

    def test_failed_launch_is_not_accepted(self):
        self.jar.parent.mkdir(parents=True)
        self.jar.write_bytes(self.content)
        for result in (subprocess.CompletedProcess([], 1, stdout=''),
                       subprocess.CompletedProcess([], 0, stdout='ERROR\n1\nSyntax Error?\n')):
            with self.subTest(result=result.returncode):
                self.process.side_effect = [subprocess.CompletedProcess([], 0), result]
                self.download.return_value = io.BytesIO(self.content)
                with self.assertRaisesRegex(RuntimeError, 'failed its syntax check'):
                    self.install()


if __name__ == '__main__':
    unittest.main()
