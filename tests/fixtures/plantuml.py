# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Explicit offline PlantUML executable for transport and orchestration fixtures."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


def install_plantuml(test):
    temporary = tempfile.TemporaryDirectory(prefix='fixture-plantuml-')
    test.addCleanup(temporary.cleanup)
    executable = Path(temporary.name) / 'plantuml'
    executable.write_text('#!' + sys.executable + '\n' + '''import os, sys
if '-version' in sys.argv:
    print('PlantUML version 1.2026.9 (fixture)')
else:
    assert sys.argv[1:] == ['-charset', 'UTF-8', '-syntax']
    assert os.environ['PLANTUML_SECURITY_PROFILE'] == 'SANDBOX'
    source = sys.stdin.buffer.read().decode('utf-8')
    if 'aass' in source or 'BROKEN_SYNTAX' in source:
        print('ERROR\\n1\\nSyntax Error?')
    else:
        print('DESCRIPTION\\n(2 entities)')
''', encoding='utf-8')
    executable.chmod(0o700)
    environment = patch.dict(os.environ, PATH=str(executable.parent) + os.pathsep + os.environ.get('PATH', ''))
    environment.start()
    test.addCleanup(environment.stop)
    runtime = patch('src.reports.plantuml.sys', SimpleNamespace(executable=str(executable.with_name('python'))))
    runtime.start()
    test.addCleanup(runtime.stop)
    return executable
