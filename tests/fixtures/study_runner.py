# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline ordinary studies through each backend's transport contract."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from explain import Runner, process as real_process
from fixtures.cli_response import cli_result
from fixtures.ledger_response import response, prompt_context
from fixtures.xxx_protocol import transcript


def run_case(self, *, backend='codex', large=True, change=None, review=False,
             policy='strict', keep_going=True, catalog_paths=None, reporter=None):
    paths = ['app.py'] + [f'part{i}.py' for i in range(2, 10)] if large else ['app.py']
    for path in paths:
        if not (self.source / path).exists():
            (self.source / path).write_text('def main(): return 42\n')
    self.value.update(result_policy=policy, continue_on_error=keep_going,
                      execution={'review_enabled': review}, stage_agents={'study': {'model': 'fixture/study-model'}})
    self.value['agent']['backend'] = backend
    cfg = self.config()
    runner = Runner(cfg, self.base / f'run-{len(list(self.base.glob("run-*")))}', reporter=reporter)
    runner.versions[backend + ':' + cfg['_agents']['study']['executable']] = '2.0.0' if backend == 'opencode' else 'fixture'
    calls, exports = [], {}
    selectors = catalog_paths or paths

    def process(command, cwd, env, payload=b'', **kwargs):
        if Path(command[0]).name == 'git':
            return real_process(command, cwd, env, payload, **kwargs)
        if backend == 'xxx' and command[1] == 'export':
            return dict(returncode=0, stdout=json.dumps(exports[command[2]]).encode(), stderr=b'')
        if backend == 'xxx' and command[1:3] == ['session', 'delete']:
            exports.pop(command[3])
            return dict(returncode=0, stdout=b'', stderr=b'')
        context = prompt_context(payload)
        calls.append(dict(context=context, command=command, cwd=cwd, env=env.copy(), prompt=payload.decode()))
        data = response(context)
        if context['stage'] == 'study':
            data['evidence'] = [dict(id=f'E-{i + 1:03d}', source_id='source-001',
                path='app.py' if path == '.' else path, start_line=1, end_line=1, quote='')
                for i, path in enumerate(selectors)]
            for i, area in enumerate(data['coverage'], 1):
                area['evidence_ids'] = [f'study:E-{i:03d}']
        if change:
            change(runner, context, data)
        if backend == 'xxx':
            sid = f'ses_study{len(calls)}'
            output, exported = transcript(data, session=sid, agent=command[command.index('--agent') + 1],
                prompt=payload.decode(), cwd=str(cwd), model='fixture/study-model')
            exports[sid] = exported
            return dict(returncode=0, stdout=output.encode(), stderr=b'')
        return cli_result(command, data)

    with patch.dict(os.environ, {'AUDIT_TEST_SUBSYSTEM_PATHS': json.dumps(selectors)}), \
            patch.object(runner, 'check_cli', return_value={}), patch('explain.process', side_effect=process):
        manifest, code = runner.run()
    self.runner, self.calls, self.manifest = runner, calls, manifest
    return manifest, code
