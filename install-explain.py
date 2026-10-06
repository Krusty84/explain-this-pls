#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Prepare a local virtual environment and install explain-this-pls requirements."""
import argparse
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    if sys.platform not in ('darwin', 'linux') or sys.version_info < (3, 11):
        print('Installation requires macOS or Linux and Python 3.11+.', file=sys.stderr)
        return 1
    requirements = ROOT / 'requirements.txt'
    if not requirements.is_file():
        print(f'Requirements file is missing: {requirements}', file=sys.stderr)
        return 1
    environment = ROOT / '.venv'
    python = environment / 'bin' / 'python'
    try:
        if environment.exists():
            if not (environment / 'pyvenv.cfg').is_file() or not python.is_file():
                print(f'Cannot reuse {environment}: it is not a complete virtual environment. '
                      'Move it aside and run the installer again.', file=sys.stderr)
                return 1
            print(f'Reusing {environment}', flush=True)
        else:
            print(f'Creating {environment}', flush=True)
            subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True)
        subprocess.run([str(python), '-c',
            'import sys\n'
            'if sys.version_info < (3, 11):\n'
            '    sys.exit("The existing .venv needs Python 3.11+. Move it aside and rerun the installer.")'],
            check=True)
        print(f'Installing requirements from {requirements}', flush=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '--require-virtualenv', '-r', str(requirements)],
                       cwd=ROOT, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f'Installation failed: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nInstallation interrupted. Run the installer again to retry.', file=sys.stderr)
        return 130
    print('\nInstallation complete. Start explain-this-pls with:\n' +
          shlex.join([str(python), str(ROOT / 'explain.py')]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
