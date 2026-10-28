# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline ordinary studies through the Codex transport contract."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from src.runtime.runner import Runner
from src.runtime.process import process as real_process
from fixtures.cli_response import cli_result
from fixtures.ledger_response import response, prompt_context


def run_case(self, *, backend='codex', large=True, change=None, review=False,
             policy='strict', keep_going=True, subsystem_paths=None, reporter=None):
    paths = ['app.py'] + [f'part{i}.py' for i in range(2, 10)] if large else ['app.py']
    for path in paths:
        if not (self.source / path).exists():
            (self.source / path).write_text('def main(): return 42\n')
    self.value.update(result_policy=policy, continue_on_error=keep_going,
                      execution=self.value.get('execution', {}) | {'review_enabled': review},
                      stage_agents={'study': {'model': 'fixture/study-model'}})
    self.value['agent']['backend'] = backend
    cfg = self.config()
    runner = Runner(cfg, self.base / f'run-{len(list(self.base.glob("run-*")))}', reporter=reporter)
    calls = []
    selectors = subsystem_paths or paths

    def process(command, cwd, env, payload=b'', **kwargs):
        if Path(command[0]).name == 'git':
            return real_process(command, cwd, env, payload, **kwargs)
        context = prompt_context(payload)
        calls.append(dict(context=context, command=command, cwd=cwd, env=env.copy(), prompt=payload.decode(),
                          budget=kwargs.get('budget')))
        data = response(context)
        if context['stage'] == 'study' and context.get('phase') not in ('write', 'plantuml_repair'):
            data['evidence'] = [dict(id=f'E-{i + 1:03d}', source_id='source-001',
                path='app.py' if path == '.' else path, start_line=1, end_line=1, quote='')
                for i, path in enumerate(selectors)]
        if change:
            change(runner, context, data)
        return cli_result(command, data)

    with patch.dict(os.environ, {'AUDIT_TEST_SUBSYSTEM_PATHS': json.dumps(selectors)}), \
            patch.object(runner, 'check_cli', return_value={}), patch('src.runtime.process.process', side_effect=process):
        manifest, code = runner.run()
    self.runner, self.calls, self.manifest = runner, calls, manifest
    return manifest, code
