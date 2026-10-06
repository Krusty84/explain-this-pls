# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Presentation state for the audit's existing structured events."""
import time

from src.runtime.metrics import RunMetrics
from src.runtime.reporting import STAGE_MESSAGES


class RunState:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.rows = {}
        self.diagnostics = []
        self.preparation = self.row({'branch': '', 'stage': 'preparation'})
        self.preparation.update(status='RUNNING', result='Preparing analysis…')

    def row(self, context):
        key = RunMetrics.key(context)
        if key not in self.rows:
            self.rows[key] = dict(context, status='RUNNING', started=self.clock(),
                                  duration_seconds=None, metrics={}, result='', diagnostics=[])
        return self.rows[key]

    def elapsed(self, row):
        return row['duration_seconds'] if row['duration_seconds'] is not None else self.clock() - row['started']

    def end_preparation(self, status='COMPLETE'):
        if self.preparation['status'] == 'RUNNING':
            self.preparation.update(status=status, duration_seconds=self.elapsed(self.preparation),
                                    result='Local setup prepared.' if status == 'COMPLETE' else 'Preparation stopped.')

    def apply(self, name, context):
        if name == 'run_started':
            self.preparation['source_name'] = context.get('source', '')
        elif name in ('preflight_started', 'preflight_completed', 'snapshot_started', 'snapshot_completed'):
            if self.preparation['status'] == 'RUNNING':
                self.preparation['result'] = ('Preparing ' + str(context.get('check') or context.get('snapshot') or 'sources') + '…')
        elif name in ('stage_started', 'stage_completed', 'stage_skipped', 'stage_recovered'):
            self.end_preparation()
            row = self.row(context)
            row.update(context)
            if context.get('metrics'):
                row['duration_seconds'] = context['metrics']['duration_seconds']
            if name == 'stage_started':
                row.update(status='RUNNING', started=self.clock(), duration_seconds=None,
                           result=STAGE_MESSAGES.get(context['stage'], STAGE_MESSAGES['study'])[0])
            elif name == 'stage_completed':
                row['duration_seconds'] = context.get('elapsed_seconds', self.elapsed(row))
                row['result'] = (STAGE_MESSAGES.get(context['stage'], STAGE_MESSAGES['study'])[2]
                                 if row['status'] == 'COMPLETE' else 'See limitations and diagnostics.')
            elif name == 'stage_skipped':
                row.update(status='SKIPPED', duration_seconds=0,
                           result='Review disabled by configuration.' if context.get('reason') == 'disabled_by_config'
                           else 'No architecture report available.')
            else:
                row['result'] = 'Report text recovered; some checks did not pass.'
        elif name == 'error':
            self.diagnostics.append(context)
            row = self.row(context) if context.get('stage') else self.preparation
            row['diagnostics'].append(context)
            if context.get('stage') or row['status'] == 'RUNNING':
                row.update(status='INTERRUPTED' if context.get('code') == 'INTERRUPTED' else 'FAILED',
                           duration_seconds=self.elapsed(row), result=context.get('message', 'Analysis failed.'))
                if context.get('metrics'):
                    row['metrics'] = context['metrics']
                    row['duration_seconds'] = context['metrics']['duration_seconds']
        elif name in ('root_warning', 'description_missing'):
            self.diagnostics.append({'message': 'Running with administrator privileges.' if name == 'root_warning'
                                     else 'Project description is missing.'})

    def finish(self, payload):
        result = payload['result']
        interrupted = result['exit_code'] == 130
        for metrics in result.get('metrics', {}).get('stages', []):
            row = self.row(metrics)
            row.update(metrics=metrics, status=metrics['status'], duration_seconds=metrics['duration_seconds'])
            if interrupted and row['status'] in ('FAILED', 'RUNNING'):
                row['status'] = 'INTERRUPTED'
        self.end_preparation('COMPLETE' if result['exit_code'] in (0, 2) else 'INTERRUPTED' if interrupted else 'FAILED')
        for row in self.rows.values():
            if row['status'] == 'RUNNING':
                row.update(status='INTERRUPTED' if interrupted else 'FAILED', duration_seconds=self.elapsed(row),
                           result='The run stopped before this step completed.')
        for diagnostic in payload.get('diagnostics', []):
            if diagnostic not in self.diagnostics:
                self.diagnostics.append(diagnostic)
