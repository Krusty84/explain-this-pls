# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Core modules load independently without the executable or process side effects."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    'src.runtime.errors', 'src.runtime.artifacts', 'src.runtime.process',
    'src.runtime.execution', 'src.runtime.config', 'src.analysis.repository',
    'src.analysis.folder', 'src.backends.preflight', 'src.runtime.session',
    'src.model.attempts', 'src.model.validation', 'src.reports.publication',
    'src.model.invocation', 'src.runtime.pipeline', 'src.runtime.runner',
    'src.runtime.audit', 'src.runtime.context_comparison', 'src.cli',
    'src.tui.config_edit', 'src.tui.settings', 'src.tui.models',
    'src.tui.worker', 'src.tui.app',
)


class ModuleImportTests(unittest.TestCase):
    def test_modules_import_without_entry_point_or_processes_from_another_directory(self):
        script = '''
import importlib
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
with patch('subprocess.Popen', side_effect=AssertionError('Import started a process')):
    importlib.import_module(sys.argv[2])
assert 'explain' not in sys.modules
if not sys.argv[2].startswith('src.tui.'):
    assert 'src.tui.app' not in sys.modules
'''
        with tempfile.TemporaryDirectory() as directory:
            for module in MODULES:
                with self.subTest(module=module):
                    result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(ROOT), module],
                                            cwd=directory, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
