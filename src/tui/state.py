# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Presentation state for the audit's existing structured events."""
import time

from src.messages import STAGE_MESSAGES, message as ui_message
from src.runtime.metrics import RunMetrics
from src.runtime.reporting import citation_warning_message


class RunState:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.rows = {}
        self.diagnostics = []
        self.preparation = self.row({'branch': '', 'stage': 'preparation'})
        self.preparation.update(status='RUNNING', result=ui_message('tui.preparing_analysis'))

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
                                    result=ui_message('tui.local_setup_prepared') if status == 'COMPLETE' else ui_message('tui.preparation_stopped'))

    def apply(self, name, context):
        if name == 'run_started':
            self.preparation['source_name'] = context.get('source', '')
        elif name in ('preflight_started', 'preflight_completed', 'snapshot_started', 'snapshot_completed'):
            if self.preparation['status'] == 'RUNNING':
                self.preparation['result'] = (ui_message(
                    'tui.preparing.apply',
                    value=str(context.get('check') or context.get('snapshot') or ui_message('tui.sources')),
                ))
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
                                 if row['status'] == 'COMPLETE' else
                                 ui_message(
                                     'tui.completed_with_citation_warnings_see_affected_claims',
                                 ) if row['status'] == 'COMPLETE_WITH_WARNINGS' else
                                 ui_message('tui.see_limitations_and_diagnostics'))
            elif name == 'stage_skipped':
                row.update(status='SKIPPED', duration_seconds=0,
                           result=ui_message(
                               'tui.review_disabled_by_configuration',
                           ) if context.get('reason') == 'disabled_by_config'
                           else ui_message('tui.no_architecture_report_available'))
            else:
                row['result'] = ui_message('tui.report_text_recovered_some_checks_did_not_pass')
        elif name == 'stage_rate_limits':
            row = self.row(context)
            if row['status'] not in ('SKIPPED', 'INTERRUPTED') and 'rate_limits' not in row:
                row['rate_limits'] = context['rate_limits']
        elif name == 'study_phase_started':
            row = self.row(context)
            row.update(phase=context['phase'], result=ui_message('run.correcting_plantuml_syntax')
                       if context['phase'] == 'plantuml_repair' else ui_message(
                'tui.collecting_claims_and_source_evidence',
            ) if context['phase'] == 'collect'
                       else ui_message('tui.writing_the_architecture_report'))
        elif name == 'citation_warning':
            warning = dict(context, message=citation_warning_message(context))
            row = self.row(context) if context.get('stage') else self.preparation
            row['diagnostics'].append(warning)
            self.diagnostics.append(warning)
        elif name == 'error':
            self.diagnostics.append(context)
            row = self.row(context) if context.get('stage') else self.preparation
            row['diagnostics'].append(context)
            if context.get('stage') or row['status'] == 'RUNNING':
                row.update(status='INTERRUPTED' if context.get('code') == 'INTERRUPTED' else 'FAILED',
                           duration_seconds=self.elapsed(row), result=context.get('message', ui_message('tui.analysis_failed')))
                if context.get('metrics'):
                    row['metrics'] = context['metrics']
                    row['duration_seconds'] = context['metrics']['duration_seconds']
                if row['status'] == 'INTERRUPTED':
                    row.pop('rate_limits', None)
        elif name == 'last_analysis_warning':
            self.diagnostics.append(context)
        elif name in ('root_warning', 'description_missing'):
            self.diagnostics.append({'message': ui_message('tui.running_with_administrator_privileges') if name == 'root_warning'
                                     else ui_message('tui.project_description_is_missing')})

    def finish(self, payload):
        result = payload['result']
        interrupted = result['exit_code'] == 130
        for metrics in result.get('metrics', {}).get('stages', []):
            row = self.row(metrics)
            row.update(metrics=metrics, status=metrics['status'], duration_seconds=metrics['duration_seconds'])
            if interrupted and row['status'] in ('FAILED', 'RUNNING'):
                row['status'] = 'INTERRUPTED'
                row.pop('rate_limits', None)
        self.end_preparation('COMPLETE' if result['exit_code'] in (0, 2) else 'INTERRUPTED' if interrupted else 'FAILED')
        for row in self.rows.values():
            if row['status'] == 'RUNNING':
                row.update(status='INTERRUPTED' if interrupted else 'FAILED', duration_seconds=self.elapsed(row),
                           result=ui_message('tui.the_stopped_before_this_step_completed'))
        for diagnostic in payload.get('diagnostics', []):
            if diagnostic not in self.diagnostics:
                self.diagnostics.append(diagnostic)
