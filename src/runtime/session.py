# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""One shared set of configuration, source guards, events and run metrics."""
from __future__ import annotations
from pathlib import Path
from src.contracts.contracts import MODEL_FOLDER_SCHEMAS, MODEL_SCHEMAS, ContractError, strict_json, CONTRACT_ID, ARTIFACT_FORMAT
from src.runtime.reporting import NullReporter, existing_file
from src.runtime.execution import execution_settings
from src.runtime.metrics import RunMetrics, measurement, usage, FIELDS
from src.analysis.ledger import continuation_allowed
from src.model.model_boundary import BindingRegistry
from src.analysis.source_context import SourceContextBuilder
from src.analysis.git_sources import tracked_paths, ignored_paths
from src.runtime.errors import AuditError
from src.analysis.repository import Repository
from src.analysis.folder import Folder

class RunSession:
    def __init__(self, config: dict, run_dir: Path, trust_repository: bool = False, reporter=None):
        self.reporter = reporter or NullReporter()
        self.metrics = RunMetrics(self.reporter.clock)
        self.metrics.started = getattr(self.reporter, 'started', self.metrics.started)
        self.reported_errors = []
        self.analysis_started = False
        self.active_stage = {}
        self.cfg, self.run_dir = config, run_dir.resolve()
        existing_manifest = self.run_dir / 'manifest.json'
        if existing_manifest.exists():
            previous = strict_json(existing_manifest.read_text(encoding='utf-8'))
            if (previous.get('contract_id') != CONTRACT_ID or
                    previous.get('artifact_format') != ARTIFACT_FORMAT):
                raise AuditError('Unsupported artifact format; select a new run directory.',
                                 code='UNSUPPORTED_ARTIFACT_FORMAT', failure_layer='publication')
        self.compromise = config.get('result_policy', 'compromise') == 'compromise'
        self.critical_failure = False
        self.execution = execution_settings(config.get('execution'))
        self.mode = config['mode']
        if trust_repository and self.mode != 'git':
            raise AuditError('--trust-repository requires git mode; it cannot be used in folder mode.')
        self.source = config[self.mode + '_mode']
        self.source_path = Path(self.source['path' if self.mode == 'folder' else 'repository'])
        self.folder = Folder(self.source_path, source_filter=config.get('source_filter')) if self.mode == 'folder' else None
        self.repo = Repository(self.source_path, trust_repository=trust_repository,
                               reporter=self.reporter) if self.mode == 'git' else None
        self.schemas = MODEL_FOLDER_SCHEMAS if self.mode == 'folder' else MODEL_SCHEMAS
        self.bindings = BindingRegistry()
        self.source_context = SourceContextBuilder(config.get('source_context'))
        self.versions: dict[str, str] = {}

    def assert_source(self):
        if self.repo:
            self.repo.assert_expected()
            if getattr(self, '_source_folder', None) is not None:
                self._source_folder.assert_snapshot(self._source_folder.inventory['source_fingerprint'])

    def source_inventory(self):
        if self.folder:
            folder = self.folder
        else:
            if getattr(self, '_source_folder', None) is None:
                settings = dict(self.cfg.get('source_filter', {}), follow_gitignore=False)
                self._source_folder = Folder(self.source_path, canonical=True, source_filter=settings,
                                             paths=tracked_paths(self.repo))
            folder = self._source_folder
        if folder.inventory is None:
            folder.snapshot()
            if self.repo:
                folder.inventory['source_filter']['exclusions'].extend(ignored_paths(self.repo, folder.source_filter))
        folder.assert_snapshot(folder.inventory['source_fingerprint'])
        return folder.inventory, folder.metadata

    def record_error(self, manifest, exc, *, phase='run', **context):
        self.reporter.stop_progress()
        recoverable = isinstance(exc, ContractError) or (isinstance(exc, AuditError)
            and (exc.code == 'NO_PROGRESS' or exc.code == 'CLI_FAILED' and exc.failure_layer == 'backend'))
        if (not recoverable or phase in ('preflight', 'cleanup')
                or getattr(exc, 'failure_layer', None) == 'cleanup' or getattr(exc, 'cleanup_failed', False)):
            self.critical_failure = True
        if isinstance(exc, KeyboardInterrupt):
            self.reporter.stop_requested()
        if not any(exc is seen for seen in self.reported_errors):
            self.reported_errors.append(exc)
            if phase in ('run', 'stage') and self.active_stage:
                phase = 'stage'
                context = self.active_stage | context
            if phase == 'stage' and context.get('stage'):
                context['metrics'] = self.metrics.finish_stage(context, 'FAILED')
            detail = self.reporter.error(exc, phase=phase, analysis_started=self.analysis_started,
                switches_performed=bool(getattr(self.repo, 'journal', [])), **context)
            manifest.setdefault('diagnostics', []).append(detail | {'phase': phase} | context)

    def stage_context(self, stage, context):
        return {'branch': context.get('branch', 'all branches' if stage == 'compare' else 'folder'),
                **({'source_name': self.source_path.name or str(self.source_path)} if self.mode == 'folder' else {}),
                'commit': context.get('source_commit'),
                'stage': context.get('prompt_variant', stage) if stage == 'study' else stage,
                'revision_id': context.get('revision_id'),
                **({'phase': context['phase']} if context.get('phase') else {}),
                **({'generated_by': context['generated_by']} if context.get('generated_by') else {}),
                'backend': None if context.get('generated_by') == 'orchestrator' else
                    self.cfg['_agents'].get(stage, {}).get('backend')}

    def stage_started(self, stage, context):
        started = self.reporter.clock()
        self.active_stage = self.stage_context(stage, context)
        self.metrics.start(self.active_stage)
        self.reporter.emit('stage_started', _started=started, **self.active_stage)
        return started

    def stage_finished(self, stage, context, data, started, *, report_path=None):
        status = data['completion_status']
        if stage == 'review' and data['verdict'] != 'PASS':
            status = 'PARTIAL'
        if stage == 'compare' and status == 'COMPLETE' and not data.get('program_checks', {}).get('policy_satisfied'):
            status = 'PARTIAL'
        checks = data.get('program_checks', {})
        if checks.get('citation_warnings') and continuation_allowed(checks):
            status = 'COMPLETE_WITH_WARNINGS'
        self.reporter.emit('stage_completed', **self.stage_context(stage, context), status=status,
                           elapsed_seconds=self.reporter.clock() - started,
                           metrics=self.metrics.finish_stage(self.stage_context(stage, context), status),
                           **({'report_path': report_path} if existing_file(report_path) else {}))
        self.active_stage = {}

    def record_attempt_metrics(self, meta, context, started):
        if 'metrics' not in meta:
            measured = usage() if meta.get('prompt_sent') else usage({key: 0 for key in FIELDS}, source='not_sent')
            meta['metrics'] = measurement(meta['backend'], meta['model_requested'], meta['model_actual'], measured)
        meta['metrics'].update(duration_seconds=round(self.reporter.clock() - started, 3), attempts=1)
        if 'tool_observation' in meta:
            meta['metrics']['tool_observation'] = meta['tool_observation']
        models = list(dict.fromkeys(entry['model_actual'] for entry in meta['metrics']['by_model']
                                    if entry.get('model_actual')))
        if models:
            meta['models_reported'] = models
            meta['model_actual_source'] = 'backend usage metadata; may include multiple models'
            stage_models = list(dict.fromkeys(entry['model_actual'] for entry in meta['metrics']['by_model']
                if entry.get('model_actual')))
            if len(stage_models) == 1:
                meta['model_actual'] = stage_models[0]
        self.metrics.record(self.stage_context(meta['stage'], context), meta)
