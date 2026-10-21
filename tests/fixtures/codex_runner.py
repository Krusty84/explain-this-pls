# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Owned Codex subprocess fixture using the ordinary offline CLI transport."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from src.runtime.config import load_config
from src.runtime.runner import Runner

ROOT = Path(__file__).resolve().parents[2]


class CodexFixture:
    def setUp(self):
        environment = patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                               if not key.endswith('_API_KEY')}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'app.py').write_text('print(1)\n')
        home = self.root / 'home'
        home.mkdir()
        (home / 'audit-profile.json').write_text(json.dumps({'model': 'configured-model'}))
        self.cli = self.root / 'codex'
        self.cli.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path(' +
            repr(str(ROOT / 'tests/fixtures/fake_cli.py')) + ', run_name="__main__")\n')
        self.cli.chmod(0o700)
        self.calls = self.root / 'calls.jsonl'
        self.env = {'HOME': str(home), 'AUDIT_TEST_CALL_LOG': str(self.calls)}
        self.value = {'result_policy': 'strict', 'mode': 'folder', 'folder_mode': {'path': str(self.source)},
            'execution': {'review_enabled': True}, 'reports_dir': str(self.root / 'reports'),
            'project_description': 'Synthetic fixture',
            'agent': {'backend': 'codex', 'executable': str(self.cli), 'model': None}}
        self.number = 0

    def run_case(self, scenario='', *, check=False, reporter=None):
        self.number += 1
        self.value.setdefault('execution', {}).setdefault('review_enabled', True)
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.value))
        self.run_dir = self.root / f'run-{self.number}'
        with patch.dict(os.environ, self.env | {'AUDIT_FAKE_CASE': scenario}):
            return Runner(load_config(path), self.run_dir, reporter=reporter).run(check_only=check)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def prompts(self):
        return [call for call in self.recorded() if call['args'][0] == 'exec' and '--help' not in call['args']]

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), *args], stderr=subprocess.STDOUT).decode().strip()

    def init_git(self, branches):
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        self.git('add', '.')
        self.git('commit', '-m', 'fixture')
        if len(branches) > 1:
            self.git('branch', 'other')
        self.value.update(mode='git', git_mode={'repository': str(self.source),
            'branches': branches, 'baseline_branch': 'main'})
