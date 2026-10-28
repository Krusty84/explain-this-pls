#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Install Python requirements and a private PlantUML launcher in .venv."""
import argparse
import hashlib
from pathlib import Path
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
from urllib.request import urlopen
from src.messages import message as ui_message

ROOT = Path(__file__).resolve().parent
PLANTUML_VERSION = '1.2026.9'
PLANTUML_URL = f'https://github.com/plantuml/plantuml/releases/download/v{PLANTUML_VERSION}/plantuml-{PLANTUML_VERSION}.jar'
PLANTUML_SHA256 = 'aa4a1d06f8911c4b842beff4828dd955be431692b631a312d04c0e9fab9581de'


def install_plantuml(environment):
    java = shutil.which('java')
    if java is None:
        raise RuntimeError(ui_message('installer.java_required'))
    try:
        subprocess.run([java, '-version'], check=True, capture_output=True, timeout=30)
    except subprocess.SubprocessError as exc:
        raise RuntimeError(ui_message('installer.java_required')) from exc
    jar = environment / 'share' / 'plantuml' / 'plantuml.jar'
    if jar.is_file() and hashlib.sha256(jar.read_bytes()).hexdigest() == PLANTUML_SHA256:
        print(ui_message('installer.reusing_plantuml', version=PLANTUML_VERSION), flush=True)
    else:
        print(ui_message('installer.installing_plantuml', version=PLANTUML_VERSION), flush=True)
        jar.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='download-', dir=jar.parent) as raw:
            downloaded = Path(raw) / 'plantuml.jar'
            context = None
            if PLANTUML_URL.startswith('https:'):
                certificates = subprocess.run([str(environment / 'bin' / 'python'), '-m', 'certifi'],
                    check=True, capture_output=True, text=True, timeout=30)
                context = ssl.create_default_context()
                context.load_verify_locations(certificates.stdout.strip())
            with urlopen(PLANTUML_URL, timeout=60, context=context) as response, downloaded.open('wb') as output:
                shutil.copyfileobj(response, output)
            if hashlib.sha256(downloaded.read_bytes()).hexdigest() != PLANTUML_SHA256:
                raise RuntimeError(ui_message('installer.plantuml_checksum_mismatch'))
            downloaded.replace(jar)
    launcher = environment / 'bin' / 'plantuml'
    with tempfile.TemporaryDirectory(prefix='plantuml-launcher-', dir=environment) as raw:
        prepared = Path(raw) / 'plantuml'
        prepared.write_text('#!/bin/sh\nexec ' + shlex.quote(java) +
            ' -Djava.awt.headless=true -jar ' + shlex.quote(str(jar)) + ' "$@"\n', encoding='utf-8')
        prepared.chmod(0o755)
        prepared.replace(launcher)
    result = subprocess.run([str(launcher), '-charset', 'UTF-8', '-syntax'],
        input='@startuml\nA -> B : data\n@enduml\n', text=True, capture_output=True, timeout=30)
    if result.returncode or not result.stdout.startswith('SEQUENCE\n'):
        raise RuntimeError(ui_message('installer.plantuml_check_failed'))


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
        install_plantuml(environment)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
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
