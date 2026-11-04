# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Static samples for the existing TUI; no audit or backend operations."""
from argparse import Namespace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from textual.widgets import Button, DataTable, TabbedContent

from src.backends.codex_limits import parse_limits
from src.messages import message as ui_message
from src.runtime.metrics import measurement, sum_usage, usage
from src.tui.app import ExplainApp, launch
from src.tui.state import RunState


class DemoApp(ExplainApp):
    TITLE = 'explain-this-pls · DEMO'

    def __init__(self, args, directory):
        self.demo_directory = directory
        super().__init__(Namespace(**(vars(args) | {'config': None, 'check': False})),
                         settings_path=directory / 'explain.config')
        self.prepare_samples()
        self.settings_path.write_text(json.dumps({'config_dir': str(directory / 'configs')}), encoding='utf-8')

    def prepare_samples(self):
        directory = self.demo_directory
        (directory / 'configs').mkdir(exist_ok=True)
        for name, mode, status in (('01-success', 'git', 'COMPLETED'), ('02-partial', 'folder', 'PARTIAL'),
                                   ('03-failure', 'folder', None)):
            source = directory / 'sources' / name
            source.mkdir(parents=True, exist_ok=True)
            reports = directory / 'reports' / name
            reports.mkdir(parents=True, exist_ok=True)
            filenames = ('run.log',) if status is None else (
                'ARCHITECTURE.md', 'ARCHITECTURE_REVIEW.md', 'COMPARISON.md', 'FINAL_REPORT.md', 'run.log')
            for filename in filenames:
                (reports / filename).write_text('DEMO: static sample; no analysis was performed.\n', encoding='utf-8')
            config = {'mode': mode, 'reports_dir': str(directory / 'reports'),
                      'project_description': 'Demo application with an API, background jobs and a shared data store.',
                      'agent': {'backend': 'codex', 'executable': sys.executable,
                                'model': 'demo-model', 'reasoning_effort': 'high'},
                      'execution': {'review_enabled': mode == 'git'}}
            config[mode + '_mode'] = ({'repository': str(source), 'branches': ['main', 'feature/background-job-processing'],
                                       'baseline_branch': 'main'} if mode == 'git' else {'path': str(source)})
            if status:
                config['last_analysis'] = {'status': status, 'finished_at': '2026-11-04T09:30:00+00:00',
                                           'results_dir': str(reports)}
            (directory / 'configs' / (name + '.json')).write_text(json.dumps(config, indent=2), encoding='utf-8')
        (directory / 'configs' / '04-invalid.json').write_text('{}\n', encoding='utf-8')

    def refresh_configs(self):
        self.prepare_samples()
        super().refresh_configs()

    def select_config(self, key):
        super().select_config(key)
        for name in ('check', 'models', 'change-folder'):
            self.query_one('#' + name, Button).disabled = True

    def choose_model(self):
        pass

    def choose_folder(self, message=None):
        pass

    def start_audit(self, *, check_only=False):
        if check_only or self.selected is None:
            return
        self.select_config(str(self.selected))
        if self.configs[str(self.selected)][1] is None:
            return
        self.state = RunState(clock=lambda: 0)
        self.query_one('#stages', DataTable).clear()
        self.query_one('#summary', DataTable).clear()
        self.query_one('#picker').display = False
        self.query_one('#dashboard').display = True
        self.query_one('#run-tabs', TabbedContent).active = 'steps-tab'
        name = self.selected.stem
        reports = self.demo_directory / 'reports' / name
        if name == '01-success':
            steps = [('main', 'study', '001', 'COMPLETE'), ('main', 'review', '001', 'COMPLETE'),
                     ('main', 'align', '002', 'COMPLETE'), ('main', 'review', '002', 'COMPLETE'),
                     ('feature/background-job-processing', 'study', '001', 'COMPLETE'),
                     ('feature/background-job-processing', 'review', '001', 'COMPLETE'),
                     ('all branches', 'compare', None, 'COMPLETE')]
            status, exit_code = 'COMPLETE', 0
        elif name == '02-partial':
            steps = [('folder', 'study', '001', 'COMPLETE_WITH_WARNINGS'),
                     ('folder', 'review', '001', 'SKIPPED')]
            status, exit_code = 'PARTIAL', 2
        else:
            steps = [('folder', 'study', '001', 'FAILED'), ('folder', 'review', '001', 'SKIPPED')]
            status, exit_code = 'FAILED', 1
        stages = []
        for index, (branch, stage, revision, outcome) in enumerate(steps):
            context = dict(branch=branch, stage=stage, revision_id=revision, backend='codex')
            measured = usage({'input_tokens': 12400, 'output_tokens': 1800, 'total_tokens': 14200,
                              'cache_read_tokens': 6400, 'cache_write_tokens': 0, 'reasoning_tokens': 900,
                              'cost_usd': .06}, source='demo') if outcome != 'FAILED' else usage(source='demo')
            metrics = dict(context, status=outcome, duration_seconds=0 if outcome == 'SKIPPED' else 32.5 + index * 4,
                           attempts=0 if outcome == 'SKIPPED' else 2 if outcome == 'FAILED' else 1,
                           **measurement('codex', requested='demo-model', measured=measured))
            if outcome == 'SKIPPED':
                metrics.update(usage=usage({key: 0 for key in measured['coverage']}, source='demo'), by_model=[])
                self.state.apply('stage_skipped', dict(context, reason='disabled_by_config'))
            elif outcome == 'FAILED':
                self.state.apply('error', dict(context, code='DEMO_BACKEND_ERROR', metrics=metrics,
                    message='Demo: the analysis stopped before a report could be created.',
                    hint='This is a static failure sample. Select another demo configuration to inspect its results.'))
            else:
                filename = {'review': 'ARCHITECTURE_REVIEW.md', 'compare': 'COMPARISON.md'}.get(stage, 'ARCHITECTURE.md')
                self.state.apply('stage_completed', dict(context, status=outcome, metrics=metrics,
                    elapsed_seconds=metrics['duration_seconds'], report_path=str(reports / filename)))
                self.state.apply('stage_rate_limits', dict(context, rate_limits=parse_limits({'rateLimits': {
                    'primary': {'usedPercent': 24 + index, 'windowDurationMins': 300, 'resetsAt': 1793818800},
                    'secondary': {'usedPercent': 61 + index, 'windowDurationMins': 10080, 'resetsAt': 1794416400}}})))
            if outcome == 'COMPLETE_WITH_WARNINGS':
                self.state.apply('citation_warning', dict(context, evidence_id='study:E-001', source_id='source-001',
                    path='services/background_jobs/processing/long_module_name.py', start_line=24, end_line=38,
                    reason='QUOTE_MISMATCH', action='UNRESOLVED', claim_ids=['C-001', 'C-004']))
            stages.append(metrics)
        self.state.end_preparation('COMPLETE')
        result = {'status': status, 'exit_code': exit_code, 'review_enabled': name == '01-success',
                  'status_meaning': 'DEMO: static sample; no analysis was performed.',
                  'metrics': {'duration_seconds': sum(stage['duration_seconds'] for stage in stages),
                              'attempts': sum(stage['attempts'] for stage in stages),
                              'usage': sum_usage(stage['usage'] for stage in stages), 'stages': stages}}
        paths = [(ui_message('tui.technical_log'), str(reports / 'run.log'))]
        if status != 'FAILED':
            paths.insert(0, (ui_message('tui.final_report'), str(reports / 'FINAL_REPORT.md')))
        self.complete({'result': result, 'reviews': [('', ui_message('review.summary.accepted.en'))]
                       if name == '01-success' else [], 'paths': paths}, show_summary=False)
        self.last_exit_code = 0
        self.query_one('#stages', DataTable).move_cursor(row=1)
        self.query_one('#stages', DataTable).focus()


def launch_demo(args):
    with TemporaryDirectory(prefix='explain-demo-') as directory:
        return launch(args, app=DemoApp(args, Path(directory).resolve()))
