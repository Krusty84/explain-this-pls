# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Audit lifecycle shared by command-line and interactive callers."""
from __future__ import annotations
import datetime as dt
import os
import signal
import sys
import uuid
from pathlib import Path
from src.contracts.contracts import ContractError
from src.runtime.reporting import existing_file
from src.runtime.metrics import RunMetrics
from src.runtime.errors import AuditError
from src.runtime.artifacts import now
from src.runtime import artifacts
from src.runtime.execution import repository_lock
from src.runtime.config import load_config
from src.runtime.config_edit import save_last_analysis
from src.runtime.runner import Runner

def run_audit(args, reporter) -> int:
    """Run one audit with the same lifecycle for CLI and interactive callers."""
    run_id = run_dir = runner = None
    run_created = False
    manifest, code = {}, 1
    header_shown = False
    config = None
    original = None
    phase = 'configuration'
    old_umask = os.umask(0o077)
    old_handler = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'Received signal {signum}')

    def header():
        nonlocal header_shown
        if header_shown:
            return
        header_shown = True
        mode = config['mode'] if config else None
        source = config[mode + '_mode'] if config else {}
        reporter.emit('run_started', check_only=args.check, output=reporter.mode, mode=mode,
                      source=source.get('path' if mode == 'folder' else 'repository'))
        if os.geteuid() == 0:
            reporter.emit('root_warning')

    try:
        signal.signal(signal.SIGTERM, interrupted)
        if sys.platform not in ('darwin', 'linux') or sys.version_info < (3, 11):
            raise AuditError('This implementation requires macOS or Linux and Python 3.11+.')
        try:
            config_path = args.config.resolve()
            original = config_path.read_bytes()
            config = load_config(config_path, text=original.decode('utf-8'))
        except ContractError as exc:
            raise AuditError('The configuration is not valid JSON/JSONC.', code='CONFIGURATION_ERROR',
                             hint='Check the configuration syntax and run --check again.') from exc
        reports = Path(config['_reports_dir'])
        run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:10]
        run_dir = reports / run_id
        reporter.run_id = run_id
        header()
        mode = config['mode']
        source = config[mode + '_mode']
        source_path = Path(source['path' if mode == 'folder' else 'repository'])
        repository_name = source_path.name or str(source_path)
        phase = 'preflight'
        try:
            runner = Runner(config, run_dir, trust_repository=args.trust_repository, reporter=reporter)
            if runner.session.repo:
                repository_name = runner.session.repo.display_name()
        finally:
            reporter.emit('configuration_loaded', repository_name=repository_name, branches=source.get('branches', []),
                          agents={stage: agent['backend'] for stage, agent in config['_agents'].items()})
            reporter.emit('preflight_completed', check='configuration')
        reports.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_dir.mkdir(mode=0o700)
        run_created = True
        reporter.attach_log(run_dir)
        snapshot = {k: v for k, v in config.items() if not k.startswith('_')}
        artifacts.save_json(run_dir / 'config.snapshot.json', snapshot)
        with repository_lock(runner.session.source_path):
            manifest, code = runner.run(check_only=args.check)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            reporter.stop_requested()
        if isinstance(exc, AuditError) and phase == 'configuration' and exc.code == 'AUDIT_ERROR':
            exc.code = 'CONFIGURATION_ERROR'
        header()
        manifest = getattr(runner.session, 'manifest', manifest) if runner else manifest
        code = 130 if isinstance(exc, KeyboardInterrupt) or manifest.get('exit_code') == 130 else 1
        manifest.update(run_id=run_id, status='FAILED', exit_code=code, finished_at=now(), publication_complete=False,
                        processing_completed=False, workflow_satisfied=False)
        if 'accepted' in manifest:
            manifest['accepted'] = False
        manifest.setdefault('errors', []).append(str(exc) or type(exc).__name__)
        if runner:
            runner.session.record_error(manifest, exc, phase='run' if runner.session.analysis_started else 'preflight')
            manifest['critical_failure'] = runner.session.critical_failure
            manifest['metrics'] = runner.session.metrics.snapshot(finish=True)
        else:
            manifest.setdefault('diagnostics', []).append(reporter.error(exc, phase='preflight',
                analysis_started=False, switches_performed=False))
        if run_created:
            try:
                artifacts.save_json(run_dir / 'manifest.json', manifest)
            except OSError as save_error:
                reporter.error(save_error)
    finally:
        # Presentation failures never bypass Git runtime cleanup.
        if runner and runner.session.repo:
            try:
                runner.session.repo.close()
            except OSError as close_error:
                reporter.error(close_error)
                code = 130 if code == 130 else 1
                manifest.update(status='FAILED', exit_code=code, publication_complete=False, critical_failure=True,
                                processing_completed=False, workflow_satisfied=False)
                if 'accepted' in manifest:
                    manifest['accepted'] = False
                if run_created:
                    try:
                        artifacts.save_json(run_dir / 'manifest.json', manifest)
                    except OSError as save_error:
                        reporter.error(save_error)
        if (not args.check and config is not None and manifest.get('has_usable_material')
                and manifest.get('final_report') and existing_file(Path(manifest['final_report']))):
            analysis = {
                'status': 'COMPLETED' if code == 0 and manifest.get('status') in
                    ('COMPLETE', 'COMPLETE_WITH_WARNINGS') else 'PARTIAL',
                'finished_at': manifest['finished_at'],
                'results_dir': str(run_dir.resolve()),
            }
            try:
                save_last_analysis(config_path, analysis, original)
            except (OSError, ValueError, ContractError) as exc:
                reporter.emit('last_analysis_warning', code='LAST_ANALYSIS_NOT_SAVED',
                              message='Could not save last_analysis in the configuration: ' + str(exc))
        signal.signal(signal.SIGTERM, old_handler)
        os.umask(old_umask)
        manifest_path = run_dir / 'manifest.json' if run_dir else None
        result = {'run_id': run_id, 'status': manifest.get('status', 'FAILED'),
                  'manifest': str(manifest_path) if existing_file(manifest_path) else None,
                  'final_report': manifest.get('final_report'),
                  'has_usable_material': manifest.get('has_usable_material', False),
                  'exit_code': code}
        result['status_meaning'] = ('Local launch prerequisites checked. Source analysis was not performed; '
            'model availability and provider authorization were not tested.' if result['status'] == 'PREFLIGHT_OK' else
            'Enabled-stage policy checks satisfied; factual correctness is not established.' if result['status'] == 'COMPLETE' else
            'Document processing completed with citation warnings; unresolved citations require human review.'
                if result['status'] == 'COMPLETE_WITH_WARNINGS' else
            'Processing may be incomplete or evidence may be insufficient; consult limitations and diagnostics.')
        result['review_quality'] = 'NOT_MEASURED'
        result['review_enabled'] = manifest.get('review_enabled', (config or {}).get('execution', {}).get('review_enabled', False))
        result['workflow_satisfied'] = manifest.get('workflow_satisfied', False)
        result['processing_completed'] = manifest.get('processing_completed', False)
        if 'metrics' in manifest:
            result['metrics'] = manifest['metrics']
        else:
            metrics = RunMetrics(reporter.clock)
            metrics.started = reporter.started
            result['metrics'] = metrics.snapshot(finish=True)
        try:
            reporter.finish(result, manifest, check_only=args.check, config_path=args.config,
                            run_dir=run_dir, trust=args.trust_repository)
        finally:
            reporter.close()
    return code
