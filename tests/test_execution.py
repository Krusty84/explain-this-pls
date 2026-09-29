"""Offline execution budgets: deterministic time plus real owned child groups."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from contracts import ContractError
from execution import Budget, execution_settings
from explain import cli_env, process
from unittest.mock import patch
from contracts import response_error
from explain import Runner
import test_explain as fixtures


class ExecutionTests(unittest.TestCase):
    def test_defaults_and_invalid_values(self):
        self.assertEqual(execution_settings(), {'stage_timeout_seconds': 3600,
            'idle_timeout_seconds': None, 'opencode_format_retries': 2})
        for key in ('stage_timeout_seconds', 'idle_timeout_seconds'):
            for value in (True, False, 0, -1, float('nan'), float('inf'), 10 ** 1000, '3', [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    execution_settings({key: value})
        for value in (True, False, -1, 3, 1.0, None):
            with self.assertRaises(ValueError):
                execution_settings({'opencode_format_retries': value})
        for value in (0, 1, 2):
            self.assertEqual(execution_settings({'opencode_format_retries': value})['opencode_format_retries'], value)

    def test_total_deadline_survives_activity(self):
        current = [0.0]
        budget = Budget(10, 3, clock=lambda: current[0])
        for i in range(1, 10):
            current[0] = i
            budget.activity()
        current[0] = 10
        with self.assertRaises(ContractError) as caught:
            budget.activity()
        self.assertEqual(caught.exception.failure_kind, 'STAGE_TIMEOUT')

    def test_idle_clock_does_not_reset_on_checks(self):
        current = [0.0]
        budget = Budget(10, 2, clock=lambda: current[0])
        current[0] = 1
        budget.check()
        current[0] = 2
        with self.assertRaises(ContractError) as caught:
            budget.check()
        self.assertEqual(caught.exception.failure_kind, 'IDLE_TIMEOUT')

    def test_pipe_holding_descendant_and_active_output_cannot_evade_deadline(self):
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            for script in ("import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])",
                           "import time\nwhile True: print('activity',flush=True); time.sleep(.01)"):
                started = time.monotonic()
                with self.assertRaises(ContractError) as caught:
                    process([sys.executable, '-c', script], cwd, cli_env(cwd), budget=Budget(.3))
                self.assertEqual(caught.exception.failure_kind, 'STAGE_TIMEOUT')
                self.assertLess(time.monotonic() - started, 3)


class TimeoutRestorationTests(unittest.TestCase):
    setUp = fixtures.RepoFixture.setUp
    tearDown = fixtures.RepoFixture.tearDown
    git = fixtures.RepoFixture.git
    config = fixtures.RepoFixture.config

    def test_git_restoration_runs_after_agent_deadline_expires(self):
        config = self.config()
        config.update(branches=['master'], continue_on_error=False)
        runner = Runner(config, self.base / 'timeout-run')
        self.addCleanup(runner.repo.close)
        with patch.object(runner, 'check_cli', return_value={}), \
                patch('explain.process', side_effect=response_error('STAGE_TIMEOUT', 'execution', 'Expired.')):
            manifest, code = runner.run()
        self.assertEqual(code, 1)
        self.assertEqual(manifest['status'], 'FAILED')
        self.assertTrue(manifest['restoration']['restored'])
        self.assertEqual(self.repo.symbolic(), 'master')
        self.assertEqual(self.repo.head(), self.master)
        self.assertEqual(manifest['diagnostics'][0]['failure_kind'], 'STAGE_TIMEOUT')
        self.repo.clean()


if __name__ == '__main__':
    unittest.main()
