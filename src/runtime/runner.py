# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Coordinate Git or folder runs and commit the final publication record."""
from __future__ import annotations
import sys
from pathlib import Path
from src.contracts.contracts import ContractError, accepted, workflow_satisfied, processing_satisfied, CONTRACT_ID, ARTIFACT_FORMAT
from src.model.structured_output import required_unresolved
from src.reports.final_report import usable_study, report_entries, comparison_possible, render_final_report
from src.analysis.ledger import continuation_allowed
from src.analysis.git_sources import inventory_delta
from src.runtime.errors import AuditError, UnsafeRepository
from src.runtime.artifacts import now, slug
from src.backends.preflight import check_cli
from src.reports.publication import ARTIFACTS, ReportPublisher
from src.runtime.session import RunSession
from src.model.invocation import ModelInvoker
from src.runtime.pipeline import SourcePipeline
from src.runtime import artifacts

class Runner:
    def __init__(self, config: dict, run_dir: Path, trust_repository: bool = False, reporter=None):
        self.session = RunSession(config, run_dir, trust_repository, reporter)
        self.publisher = ReportPublisher(self.session)
        self.invoker = ModelInvoker(self.session, self.publisher)
        self.pipeline = SourcePipeline(self.session, self.publisher)

    def check_cli(self) -> dict:
        return check_cli(self.session)

    def invoke(self, stage: str, context: dict, destination: Path) -> tuple[dict | None, dict]:
        return self.invoker.invoke(stage, context, destination)

    def run_source(self, item, context, directory, persist):
        return self.pipeline.run_source(item, context, directory, persist, invoke=self.invoke)

    def run(self, check_only: bool = False) -> tuple[dict, int]:
        try:
            manifest, code = self.run_folder(check_only) if self.session.folder else self.run_git(check_only)
        finally:
            self.session.reporter.stop_progress()
            if self.session.repo:
                try:
                    self.session.repo.close()
                except OSError as exc:
                    self.session.record_error(self.session.manifest, exc, phase='cleanup')
        manifest['result_policy'] = 'compromise' if self.session.compromise else 'strict'
        manifest.update(contract_id=CONTRACT_ID, artifact_format=ARTIFACT_FORMAT,
            acceptance_meaning='accepted=true means study and review policy checks satisfied; factual correctness is not established.',
            review_quality='NOT_MEASURED', source_check_meaning='MATCHED_AT_BOUNDARIES means pinned source metadata matched at performed checks only.')
        manifest['critical_failure'] = self.session.critical_failure
        if not check_only:
            entries = report_entries(manifest, self.session.source, self.session.mode)
            try:
                for entry in entries:
                    self.publisher.assert_revision_files(entry)
            except (AuditError, ContractError, OSError) as exc:
                self.session.critical_failure = True
                self.session.record_error(manifest, exc, phase='integrity')
            manifest['critical_failure'] = self.session.critical_failure
            if self.session.compromise and code != 130:
                if self.session.critical_failure or not any(usable_study(b) for b in entries):
                    manifest['status'], code = 'FAILED', 1
                elif (all(processing_satisfied(b) for b in entries) and not manifest.get('diagnostics')
                      and (self.session.mode == 'folder' or len(self.session.source['branches']) == 1
                           or (continuation_allowed((manifest.get('comparison') or {}).get('program_checks', {}))
                               and manifest.get('comparison_invocation', {}).get('publication_complete')))):
                    warned = any((b.get(stage) or {}).get('program_checks', {}).get('citation_warnings')
                                 for b in entries for stage in ('study', 'review'))
                    warned = warned or bool((manifest.get('comparison') or {}).get('program_checks', {}).get('citation_warnings'))
                    manifest['status'], code = 'COMPLETE_WITH_WARNINGS' if warned else 'COMPLETE', 0
                else:
                    manifest['status'], code = 'PARTIAL', 2
            elif self.session.critical_failure and code != 130:
                manifest['status'], code = 'FAILED', 1
            try:
                report, useful = render_final_report(manifest, self.session.source, self.session.mode,
                                                     self.session.cfg.get('output_language', 'Russian'))
                path = self.session.run_dir / 'FINAL_REPORT.md'
                artifacts.atomic(path, report)
                manifest.update(final_report=str(path), has_usable_material=useful)
            except OSError as exc:
                self.session.record_error(manifest, exc, phase='publication')
                manifest.update(status='FAILED', critical_failure=True)
                code = 130 if code == 130 else 1
        manifest['processing_completed'] = not check_only and manifest['status'] in ('COMPLETE', 'COMPLETE_WITH_WARNINGS') and code == 0
        manifest['workflow_satisfied'] = (manifest['processing_completed'] and
            all(workflow_satisfied(b) for b in report_entries(manifest, self.session.source, self.session.mode)) and
            (self.session.mode == 'folder' or len(self.session.source['branches']) == 1 or
             (manifest.get('comparison') or {}).get('program_checks', {}).get('policy_satisfied') is True))
        manifest['exit_code'] = code
        # This last manifest write commits the run's publication record. Individual
        # file replacements above are atomic; the group is not a transaction.
        manifest['publication_complete'] = code in (0, 2)
        if self.session.critical_failure and 'accepted' in manifest:
            manifest['accepted'] = False
        manifest['metrics'] = self.session.metrics.snapshot(finish=True)
        artifacts.save_json(self.session.run_dir / 'manifest.json', manifest)
        return manifest, code

    def run_git(self, check_only: bool = False) -> tuple[dict, int]:
        manifest = {'run_id': self.session.run_dir.name, 'started_at': now(), 'repository': str(self.session.repo.path),
            'review_enabled': self.session.execution['review_enabled'],
            'git': self.session.repo.runtime.manifest(), 'baseline_branch': self.session.source['baseline_branch'],
            'status': 'RUNNING', 'isolation': 'in-place-checkout; cli-native-permissions',
            'platform': sys.platform, 'branches': [], 'errors': [], 'switch_journal': [],
            'source_preservation': 'Only the orchestrator switches checkouts; agents have read-only source access.',
            'publication_complete': False}
        self.session.manifest = manifest
        def persist():
            manifest['metrics'] = self.session.metrics.snapshot()
            artifacts.save_json(self.session.run_dir / 'manifest.json', manifest)
        persist()
        inventories = {}
        code = 1
        try:
            if not self.session.cfg['project_description']:
                self.session.reporter.emit('description_missing')
            pins = self.session.repo.preflight(self.session.source['branches'])
            manifest['pins'] = pins
            original_branch, original_commit = self.session.repo.symbolic(), self.session.repo.head()
            manifest['original_checkout'] = {'branch': original_branch, 'commit': original_commit}
            manifest['original_hierarchy'] = self.session.repo.original
            manifest['checkout_plans'] = {branch: dict(self.session.repo.plans[commit]) for branch, commit in pins.items()}
            manifest['switch_journal'] = self.session.repo.journal
            manifest['cli_checks'] = self.check_cli()
            self.session.repo.assert_expected()
            if check_only:
                manifest['status'], code = 'PREFLIGHT_OK', 0
            else:
                for branch in self.session.source['branches']:
                    self.session.assert_source()
                    self.session._source_folder = None
                    commit = pins[branch]
                    self.session.repo.checkout(commit, persist)
                    self.session.analysis_started = True
                    self.session.active_stage = {}
                    self.session.reporter.emit('branch_started', branch=branch, commit=commit)
                    branch_dir = self.session.run_dir / 'branches' / slug(branch)
                    item = {'branch': branch, 'source_commit': commit,
                        'submodules': self.session.repo.submodules(commit, verified=True),
                        'directory': str(branch_dir.relative_to(self.session.run_dir)), 'study': None, 'review': None, 'errors': []}
                    manifest['branches'].append(item)
                    inventories[branch], _ = self.session.source_inventory()
                    persist()
                    context = {'source_mode': 'git', 'branch': branch, 'source_commit': commit,
                        'submodules': item['submodules'],
                        'repository': str(self.session.source_path), 'output_language': self.session.cfg['output_language'],
                        'project_description': self.session.cfg['project_description'],
                        'priority_scenarios': self.session.cfg['priority_scenarios'], 'execution_mode': 'static-only',
                        'source_access': 'Read tracked source files in this repository at the pinned commit. '
                            'Do not read .git, ignored untracked files, or configured exclusions. Do not follow symlinks. '
                            'Do not modify sources or Git state. Native CLI permissions are not full filesystem isolation.'}
                    self.run_source(item, context, branch_dir, persist)
                    item['accepted'] = accepted(item)
                    persist()
                entries = manifest['branches']
                quality_ok = all(workflow_satisfied(b) for b in entries)
                if len(self.session.source['branches']) > 1:
                    baseline = self.session.source['baseline_branch']
                    selected_entries = [b for b in entries if b['branch'] in self.session.source['branches']]
                    bundle = {'baseline_branch': baseline, 'baseline_commit': pins[baseline],
                        'review_enabled': self.session.execution['review_enabled'],
                        'result_policy': 'compromise' if self.session.compromise else 'strict',
                        'requested_branches': self.session.source['branches'], 'output_language': self.session.cfg['output_language'],
                        'project_description': self.session.cfg['project_description'],
                        'scope': 'reports-only comparison; source inspection is outside task scope',
                        'branches': [{k: b.get(k) for k in ('branch', 'source_commit', 'submodules', 'study', 'review',
                                                           'study_material', 'review_material', 'errors', 'selected_revision', 'coverage_plan')} |
                                      {'accepted': accepted(b), 'workflow_satisfied': workflow_satisfied(b),
                                       'review_enabled': self.session.execution['review_enabled']} |
                                     {stage + '_invocation': {k: (b.get(stage + '_invocation') or {}).get(k) for k in
                                        ('publication_complete', 'contract_id', 'artifact_format', 'status', 'model_requested', 'model_actual',
                                         'model_actual_source', 'source_check_status', 'review_quality')}
                                      for stage in ('study', 'review')} for b in entries if b['branch'] in self.session.source['branches']],
                        'git_deltas': {b: inventory_delta(baseline, b, inventories, pins, self.session.repo.plans) for b in self.session.source['branches'] if b != baseline}}
                    bundle['required_unresolved_branches'] = required_unresolved(bundle)
                    comp_dir = self.session.run_dir / 'comparison'
                    artifacts.save_json(comp_dir / 'inputs.json', bundle)
                    started = self.session.stage_started('compare', bundle)
                    try:
                        self.session.assert_source()
                        for entry in entries:
                            self.publisher.assert_revision_files(entry)
                        can_compare = (comparison_possible(selected_entries, baseline) if self.session.compromise
                                       else any(workflow_satisfied(b) for b in selected_entries))
                        if can_compare:
                            comparison, meta = self.invoke('compare', bundle, comp_dir / 'compare.logs')
                        else:
                            comparison, meta = self.pipeline.publish_blocked_comparison(bundle, comp_dir / 'compare.logs')
                    finally:
                        try:
                            self.session.assert_source()
                            for entry in entries:
                                self.publisher.assert_revision_files(entry)
                        except AuditError as exc:
                            self.session.record_error(manifest, exc, phase='integrity')
                            raise
                    manifest['comparison'] = comparison
                    manifest['comparison_invocation'] = meta
                    self.session.stage_finished('compare', bundle, comparison, started,
                        report_path=comp_dir / ARTIFACTS['compare'] if meta.get('publication_complete') else None)
                    quality_ok = quality_ok and comparison['program_checks']['policy_satisfied'] and meta['publication_complete']
                failed = any(b['errors'] for b in entries)
                manifest['status'] = 'FAILED' if failed else 'COMPLETE' if quality_ok else 'PARTIAL'
                code = 1 if failed else 0 if quality_ok else 2
        except BaseException as exc:
            manifest['errors'].append(str(exc) or type(exc).__name__)
            manifest['status'] = 'FAILED'
            code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            self.session.record_error(manifest, exc, phase='run' if self.session.analysis_started else 'preflight')
        finally:
            self.session._source_folder = None
            if getattr(self.session.repo, 'journal', []):
                try:
                    self.session.reporter.emit('restoration_started')
                finally:
                    restoration = self.session.repo.restore(persist)
                    manifest['restoration'] = restoration
                    if not restoration['restored']:
                        manifest['status'] = 'FAILED'
                        code = 130 if code == 130 or restoration.get('interrupted') else 1
                        for path, state in restoration['nodes'].items():
                            if not state['restored']:
                                error = UnsafeRepository('Original checkout restoration failed: ' + state['error'],
                                    code='RESTORATION_FAILED', failure_layer='integrity', node_path=path)
                                manifest['errors'].append(str(error))
                                self.session.record_error(manifest, error, phase='restoration')
                    self.session.reporter.emit('restoration_completed', restored=restoration['restored'])
                    for message in restoration['journal_errors']:
                        error = AuditError('Could not save restoration journal: ' + message,
                                           code='RESTORATION_JOURNAL_FAILED', failure_layer='publication')
                        manifest['errors'].append(str(error))
                        manifest['status'] = 'FAILED'
                        code = 130 if code == 130 else 1
                        self.session.record_error(manifest, error, phase='restoration')
        manifest.update(finished_at=now(), exit_code=code)
        persist()
        return manifest, code

    def run_folder(self, check_only: bool = False) -> tuple[dict, int]:
        manifest = {'mode': 'folder', 'run_id': self.session.run_dir.name,
            'review_enabled': self.session.execution['review_enabled'],
            'started_at': now(), 'source_directory': str(self.session.source_path), 'status': 'RUNNING',
            'isolation': 'cli-native-permissions', 'platform': sys.platform,
            'integrity': 'One initial content fingerprint; metadata checked at stage boundaries. '
                         'Not a backup or continuous immutability guarantee.',
            'study': None, 'review': None, 'accepted': False, 'errors': []}
        manifest['publication_complete'] = False
        self.session.manifest = manifest
        def persist():
            manifest['metrics'] = self.session.metrics.snapshot()
            artifacts.save_json(self.session.run_dir / 'manifest.json', manifest)
        persist()
        try:
            if not self.session.cfg['project_description']:
                self.session.reporter.emit('description_missing')
            self.session.reporter.emit('preflight_started', check='inventory')
            inventory = self.session.folder.snapshot()
            artifacts.save_json(self.session.run_dir / 'source.inventory.json', inventory)
            fingerprint = inventory['source_fingerprint']
            manifest.update(source_fingerprint=fingerprint, inventory='source.inventory.json')
            self.session.reporter.emit('preflight_completed', check='inventory')
            manifest['cli_checks'] = self.check_cli()
            self.session.folder.assert_snapshot(fingerprint)
            self.session.reporter.emit('preflight_completed', check='integrity')
            persist()
            if check_only:
                manifest['status'], code = 'PREFLIGHT_OK', 0
            else:
                self.session.analysis_started = True
                context = {'source_mode': 'folder', 'source_directory': str(self.session.source_path),
                    'source_fingerprint': fingerprint, 'output_language': self.session.cfg['output_language'],
                    'project_description': self.session.cfg['project_description'],
                    'priority_scenarios': self.session.cfg['priority_scenarios'], 'execution_mode': 'static-only',
                    'source_access': 'Read the pinned source inventory, including eligible hidden files. '
                                     'Do not read .git entries or configured exclusions. Do not follow symlinks or use Git. '
                                     'Native CLI permissions; pinned metadata is verified at stage boundaries only.'}
                self.run_source(manifest, context, self.session.run_dir, persist)
                manifest['accepted'] = accepted(manifest)
                manifest['workflow_satisfied'] = workflow_satisfied(manifest)
                manifest['status'] = 'FAILED' if manifest['errors'] else 'COMPLETE' if manifest['workflow_satisfied'] else 'PARTIAL'
                code = 1 if manifest['errors'] else 0 if manifest['workflow_satisfied'] else 2
        except BaseException as exc:
            manifest['errors'].append(str(exc) or type(exc).__name__)
            manifest['status'] = 'FAILED'
            code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            self.session.record_error(manifest, exc, phase='run' if self.session.analysis_started else 'preflight')
        manifest.update(finished_at=now(), exit_code=code)
        persist()
        return manifest, code
