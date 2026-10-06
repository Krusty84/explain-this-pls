#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Verify the versioned native patch in a disposable pinned OpenCode checkout.

No model requests or global installs. --install prepares only the runtime's
workspace dependencies; run the verification itself in a network namespace.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HASHES = {
    'packages/opencode/src/session/prompt.ts': '4c13fea470c7418d53b688fb976770d16ae843e66ba6c7238d9c1f960251c6be',
    'packages/opencode/src/session/compaction.ts': '74d1549b49cc1b66e88310b31e881778cbaff4e283182e679aad22dcb8ca93b4',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--bun', required=True, type=Path)
    parser.add_argument('--install', action='store_true', help='Prepare dependencies only; requires network')
    args = parser.parse_args()
    source, bun = args.source.resolve(), args.bun.resolve()
    for name, digest in HASHES.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            parser.error(f'{name}: not the pristine pinned 4ee426ba source')
    if json.loads((source / 'packages/opencode/package.json').read_text())['version'] != '1.2.27':
        parser.error('Expected OpenCode 1.2.27')
    env = os.environ | {'PATH': str(bun.parent) + os.pathsep + os.environ['PATH'],
                        'BUN_INSTALL_CACHE_DIR': str(source.parent / 'bun-cache')}
    if args.install:
        # Bun resolves unrelated web workspaces even with --filter. Keep the
        # runtime's dependency versions/catalog and upstream lock; exclude web.
        manifest = source / 'package.json'
        original = manifest.read_bytes()
        package = json.loads(original)
        workspaces = ['packages/opencode', 'packages/plugin', 'packages/util', 'packages/script', 'packages/sdk/js']
        catalog = set()
        for directory in workspaces:
            child = json.loads((source / directory / 'package.json').read_text())
            for section in ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'):
                catalog.update(name for name, version in child.get(section, {}).items() if version == 'catalog:')
        package['workspaces']['packages'] = workspaces
        package['workspaces']['catalog'] = {key: value for key, value in package['workspaces']['catalog'].items() if key in catalog}
        package['dependencies'] = {}
        package['devDependencies'] = {}
        try:
            manifest.write_text(json.dumps(package, indent=2) + '\n')
            subprocess.run([str(bun), 'install', '--ignore-scripts'], cwd=source, env=env, check=True)
        finally:
            manifest.write_bytes(original)
        print('Runtime dependencies prepared; package.json restored. Run again without --install, offline.')
        return
    package = source / 'packages/opencode'
    test = package / 'test/session/compaction-format.test.ts'
    if test.exists():
        parser.error(f'Refusing to replace existing test: {test}')
    originals = {name: (source / name).read_bytes() for name in HASHES}
    patch = ROOT / 'patches/opencode-v1.2.27-compaction-format.patch'
    with tempfile.TemporaryDirectory(prefix='native-compaction-') as directory:
        env.update(NATIVE_COMPACTION_ARTIFACT_DIR=directory,
                   OPENCODE_DISABLE_DEFAULT_PLUGINS='1', OPENCODE_DISABLE_MODELS_FETCH='1',
                   OPENCODE_VERSION='1.2.27', OPENCODE_CHANNEL='test')
        try:
            shutil.copyfile(ROOT / 'tests/native/compaction-format.test.ts', test)
            command = [str(bun), 'test', '--timeout', '30000', str(test)]
            subprocess.run(command, cwd=package, env=env | {'NATIVE_COMPACTION_BASELINE': '1'}, check=True)
            subprocess.run(['git', 'apply', '--check', str(patch)], cwd=source, check=True)
            subprocess.run(['git', 'apply', str(patch)], cwd=source, check=True)
            subprocess.run(command, cwd=package, env=env | {'NATIVE_COMPACTION_BASELINE': '0'}, check=True)
            subprocess.run([sys.executable, '-B', str(ROOT / 'tests/native/validate_compaction.py'), directory],
                           cwd=ROOT, check=True)
            subprocess.run([str(bun), 'test', '--timeout', '30000', 'test/session/compaction.test.ts',
                            'test/session/structured-output.test.ts'], cwd=package, env=env, check=True)
            subprocess.run([str(bun), 'run', 'typecheck'], cwd=package, env=env, check=True)
        finally:
            for name, data in originals.items():
                (source / name).write_bytes(data)
            test.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
