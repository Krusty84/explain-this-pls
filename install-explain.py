#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Prepare a local virtual environment and install explain-this-pls requirements."""
import argparse
from pathlib import Path
import shlex
import subprocess
import sys
from src.messages import message as ui_message

ROOT = Path(__file__).resolve().parent


def main(argv=None) -> int:
    argparse.ArgumentParser(description=ui_message('installer.description')).parse_args(argv)
    if sys.platform not in ('darwin', 'linux') or sys.version_info < (3, 11):
        print(ui_message('installer.installation_requires_macos_or_linux_and_python'), file=sys.stderr)
        return 1
    requirements = ROOT / 'requirements.txt'
    if not requirements.is_file():
        print(ui_message('installer.requirements_file_is_missing', requirements=requirements), file=sys.stderr)
        return 1
    environment = ROOT / '.venv'
    python = environment / 'bin' / 'python'
    try:
        if environment.exists():
            if not (environment / 'pyvenv.cfg').is_file() or not python.is_file():
                print(ui_message('installer.cannot_reuse_it_is_not_a_complete_virtual', environment=environment), file=sys.stderr)
                return 1
            print(ui_message('installer.reusing', environment=environment), flush=True)
        else:
            print(ui_message('installer.creating', environment=environment), flush=True)
            subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True)
        subprocess.run([str(python), '-c',
            'import sys\n'
            'if sys.version_info < (3, 11):\n'
            '    sys.exit(sys.argv[1])', ui_message('installer.python_required')],
            check=True)
        print(ui_message('installer.installing_requirements_from', requirements=requirements), flush=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '--require-virtualenv', '-r', str(requirements)],
                       cwd=ROOT, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(ui_message('installer.installation_failed', exc=exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(ui_message('installer.installation_interrupted_the_installer_again_to_retry'), file=sys.stderr)
        return 130
    print(ui_message(
        'installer.installation_complete_start_explain_this_pls_with',
        value=shlex.join([str(python), str(ROOT / 'explain.py')]),
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
