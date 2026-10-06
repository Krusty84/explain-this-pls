#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""OPT IN ONLY: potentially paid installed XXX. Uses the production validator."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from explain import Runner, load_config

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('EXPLAIN_XXX_COMPACTION_SMOKE') == '1',
                     'not run: installed XXX compaction smoke requires explicit opt-in (may incur cost)')
class InstalledXXXCompactionSmoke(unittest.TestCase):
    def test_auto_compaction_cli_export_and_local_validation(self):
        if sys.platform not in ('linux', 'darwin'):
            self.skipTest('not run: runtime requires Linux or macOS')
        executable = os.environ.get('EXPLAIN_XXX_EXECUTABLE', 'xxx')
        if shutil.which(executable) is None:
            self.skipTest('not run: XXX executable not installed')
        with tempfile.TemporaryDirectory(prefix='explain-xxx-smoke-') as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            # Pure source text. No imports, builds, generators or tests are run.
            # The user's verified native compaction settings/model determine
            # whether this fits the context. No model limits are falsified here.
            for i in range(12):
                (source / f'module_{i:02}.py').write_text(''.join(
                    f'def transform_{i}_{j}(value):\n    return value + {i + j}\n\n' for j in range(60)))
            template = root / 'study.md'
            template.write_text((ROOT / 'prompts/study.md').read_text() +
                '\nThis is a synthetic compatibility smoke. Read every module in ranges and compare '
                'the independent transformations before reporting; do not execute them. '
                'If automatic compaction occurs, continue to the final JSON answer.\n')
            config = root / 'config.json'
            config.write_text(json.dumps({'result_policy': 'strict', 'mode': 'folder',
                'folder_mode': {'path': str(source)}, 'reports_dir': str(root / 'reports'),
                'project_description': 'Small synthetic XXX compatibility smoke, no user source.',
                'agent': {'backend': 'xxx', 'executable': executable,
                          'model': os.environ.get('EXPLAIN_XXX_MODEL')},
                'execution': {'stage_timeout_seconds': 180, 'max_revision_rounds': 0,
                              'structured_output_repair_attempts': 0},
                'prompts': {'study': str(template)}}))
            runner = Runner(load_config(config), root / 'run')
            manifest, code = runner.run()
            # Missing compaction, CLI/export and local schema/identity/semantic
            # errors all FAIL; a normal small successful run is not proof.
            self.assertEqual(code, 0, manifest.get('diagnostics'))
            self.assertTrue(manifest['accepted'])
            attempts = [json.loads(p.read_text()) for p in (root / 'run').rglob('attempt-*/invocation.json')]
            self.assertTrue(any(a.get('compaction', {}).get('completed', 0) > 0 for a in attempts),
                            'No automatic compaction/continuation observed; smoke criterion not met')
            self.assertTrue(all(a.get('completion_source') == 'session_export' and a.get('backend_result_valid') for a in attempts))


if __name__ == '__main__':
    unittest.main()
