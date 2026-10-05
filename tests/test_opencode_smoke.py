# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Opt-in installed OpenCode V2 smoke with a local fake provider, no paid model."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from explain import Runner, load_config
from fixtures.ledger_response import prompt_context, response


@unittest.skipUnless(os.environ.get('EXPLAIN_OPENCODE_SMOKE') == '1',
                     'installed OpenCode smoke requires EXPLAIN_OPENCODE_SMOKE=1; uses only a local fake provider')
class InstalledOpenCodeSmoke(unittest.TestCase):
    def test_check_and_folder_pipeline_with_local_provider(self):
        executable = shutil.which('opencode')
        if executable is None:
            self.skipTest('OpenCode is not installed')
        stages = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                body = json.dumps({'object': 'list', 'data': [{'id': 'model', 'object': 'model'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                prompts = []
                for message in request['messages']:
                    content = message.get('content', '')
                    text = content if isinstance(content, str) else '\n'.join(
                        part.get('text', '') for part in content if isinstance(part, dict))
                    if message['role'] == 'user' and '# Authoritative orchestration context (data)\n' in text:
                        prompts.append(text)
                result = 'Fixture title'
                title = any(m['role'] == 'system' and 'You are a title generator.' in str(m.get('content', ''))
                            for m in request['messages'])
                if not title and prompts:
                    context = prompt_context(prompts[-1].encode())
                    stages.append(context['stage'])
                    result = json.dumps(response(context))
                base = {'id': 'chatcmpl_fixture', 'object': 'chat.completion.chunk',
                        'created': 1, 'model': 'model'}
                chunks = [base | {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': result},
                                               'finish_reason': None}]},
                          base | {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                                  'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}]
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='explain-opencode-smoke-') as directory:
                root = Path(directory)
                source = root / 'source'; source.mkdir()
                (source / 'app.py').write_text('print(1)\n')
                # Isolate test configuration/history; never load credentials or change the user's profile.
                env = {k: v for k, v in os.environ.items()
                       if k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'TMPDIR', 'SHELL', 'SYSTEMROOT')}
                for name in ('CONFIG', 'DATA', 'CACHE', 'STATE'):
                    env['XDG_' + name + '_HOME'] = str(root / name.lower())
                env.update(OPENCODE_DISABLE_MODELS_FETCH='1', OPENCODE_CONFIG_PROJECT_DISABLE='1',
                           OPENCODE_CONFIG_CONTENT=json.dumps({
                               'model': 'fixture/model', 'update': 'disable',
                               'providers': {'fixture': {'package': '@opencode/ai/providers/openai-compatible',
                                   'settings': {'baseURL': f'http://127.0.0.1:{server.server_port}/v1', 'apiKey': 'local-fixture'},
                                   'models': {'model': {'modelID': 'model',
                                       'capabilities': {'tools': True, 'input': ['text'], 'output': ['text']},
                                       'limit': {'context': 128000, 'output': 32000}}}}}}))
                config = root / 'config.json'
                config.write_text(json.dumps({'mode': 'folder', 'result_policy': 'strict',
                    'project_description': 'Synthetic local provider smoke.',
                    'folder_mode': {'path': str(source)}, 'reports_dir': str(root / 'reports'),
                    'agent': {'backend': 'opencode', 'executable': executable},
                    'execution': {'stage_timeout_seconds': 60, 'max_revision_rounds': 0}}))
                with patch.dict(os.environ, env, clear=True):
                    checked, code = Runner(load_config(config), root / 'check').run(check_only=True)
                    self.assertEqual(code, 0, checked.get('diagnostics'))
                    self.assertEqual(stages, [])
                    manifest, code = Runner(load_config(config), root / 'run').run()
                logs = {str(p.relative_to(root)): p.read_text() for p in (root / 'run').rglob('*.log')
                        if p.name in ('stdout.log', 'stderr.log')}
                self.assertEqual(code, 0, {'diagnostics': manifest.get('diagnostics'), 'logs': logs})
                self.assertTrue(manifest['accepted'])
                self.assertEqual(stages, ['catalog', 'study', 'review'])
                attempts = list((root / 'run').rglob('attempt-*/invocation.json'))
                self.assertEqual(len(attempts), 3)
                for path in attempts:
                    meta = json.loads(path.read_text())
                    self.assertTrue(meta['local_validation'])
                    self.assertTrue(meta['publication_complete'])
                    self.assertIsNone(meta['model_actual'])
                    self.assertGreater(meta['metrics']['usage']['total_tokens'], 0)
                self.assertEqual((source / 'app.py').read_text(), 'print(1)\n')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
