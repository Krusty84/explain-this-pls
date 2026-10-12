# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Sequential study, optional review and bounded document revisions."""
from __future__ import annotations
import contextlib
import json
from dataclasses import asdict
from src.contracts.contracts import ContractError, accepted, workflow_satisfied, processing_satisfied, strict_json, validate_result
from src.runtime.reporting import diagnostic
from src.model.structured_output import blocked_comparison
from src.reports.final_report import stage_document, usable_study
from src.analysis.ledger import prepare_result
from src.analysis.coverage_plan import inventory_summary, build_coverage_plan
from src.analysis.revisions import revision_inputs, choose_revision, completed_pair
from src.runtime.errors import AuditError
from src.runtime.artifacts import now, digest, private_directory
from src.reports.publication import ARTIFACTS
from src.runtime import artifacts

class SourcePipeline:
    def __init__(self, session, publisher):
        self.session, self.publisher = session, publisher

    def store_stage(self, item, stage, data, meta, context):
        item[stage], item[stage + '_invocation'] = data, meta
        material = meta.get('usable_material')
        if material is not None:
            item[stage + '_material'] = material
            detail = meta['error'] | self.session.stage_context(stage, context) | {'phase': 'stage'}
            self.session.manifest.setdefault('diagnostics', []).append(detail)
            self.session.reporter.emit('stage_recovered', level=30, **self.session.stage_context(stage, context),
                               message=material['contract_failure']['message'] +
                               ' Text retained; policy checks not completed. Agent self-assessment: ' +
                               (material['completion_status'] or 'n/a') + '.')
        item[stage + '_usable'] = usable_study(item) if stage == 'study' else bool(stage_document(item, stage))
        if material is not None and not self.session.cfg['continue_on_error']:
            raise ContractError('Stopped after retaining unvalidated material (continue_on_error=false).')

    def select_source_revision(self, item, directory, *, publish):
        self.publisher.assert_revision_files(item)
        selected = choose_revision(item.get('revisions', []), continuation=self.session.compromise)
        item['selected_revision'] = selected['revision_id'] if selected else None
        item['revision_history'] = [{
            'revision_id': revision['revision_id'], 'directory': revision['directory'],
            'study_status': (stage_document(revision, 'study') or {}).get('completion_status'),
            'review_status': 'SKIPPED' if revision.get('review_skipped') == 'disabled_by_config'
                             else (revision.get('review') or {}).get('completion_status'),
            'review_enabled': revision.get('review_enabled', True),
            'review_skipped': revision.get('review_skipped'),
            'workflow_satisfied': workflow_satisfied(revision),
            'processing_completed': processing_satisfied(revision),
            'review_complete': completed_pair(revision), 'accepted': accepted(revision),
            'errors': list(revision['errors'])} for revision in item.get('revisions', [])]
        for stage in ('study', 'review'):
            for suffix in ('', '_material', '_invocation', '_usable'):
                item.pop(stage + suffix, None)
            item[stage] = None
        if selected:
            for stage in ('study', 'review'):
                if stage == 'review' and not completed_pair(selected):
                    continue
                for suffix in ('', '_material', '_invocation', '_usable'):
                    if stage + suffix in selected:
                        item[stage + suffix] = selected[stage + suffix]
            if publish:
                self.publisher.publish_revision(selected, directory)
                item['selection_publication_complete'] = True
        item['accepted'] = accepted(item)
        item['workflow_satisfied'] = workflow_satisfied(item)
        item['processing_completed'] = processing_satisfied(item)

    def run_source(self, item, context, directory, persist, *, invoke):
        """One pinned source and at most two independent document pairs."""
        review_enabled = self.session.execution['review_enabled']
        item['review_enabled'] = review_enabled
        item['workflow_satisfied'] = False
        if not review_enabled:
            item['review_skipped'] = 'disabled_by_config'
        context = dict(context, review_enabled=review_enabled, source_decoding=self.session.cfg.get('source_decoding', {'rules': []}),
                       source_decoding_access='Decoding rules apply to program evidence checks and, when source_context is enabled, prepared source fragments; additional file reading depends on the CLI. No UTF-8 file copies are created on disk.')
        item['revisions'] = []

        def guard(stage_context):
            if self.session.folder:
                self.session.folder.assert_snapshot(context['source_fingerprint'])
            else:
                self.session.assert_source()
            self.publisher.assert_coverage_file(stage_context)
            self.publisher.assert_revision_files(item)

        def run_stage(stage, state, stage_context, stage_dir):
            started = self.session.stage_started(stage, stage_context)
            destination = stage_dir / (stage + '.logs')
            try:
                guard(stage_context)
                data, meta = invoke(stage, stage_context, destination)
                self.store_stage(state, stage, data, meta, stage_context)
                if data is not None:
                    self.session.stage_finished(stage, stage_context, data, started,
                        report_path=stage_dir / ARTIFACTS[stage] if meta.get('publication_complete') and stage in ARTIFACTS else None)
                else:
                    self.session.reporter.emit('stage_completed', **self.session.stage_context(stage, stage_context),
                                       status='PARTIAL', elapsed_seconds=self.session.reporter.clock() - started,
                                       metrics=self.session.metrics.finish_stage(self.session.stage_context(stage, stage_context), 'PARTIAL'))
                    self.session.active_stage = {}
                return data
            except (AuditError, ContractError, OSError, UnicodeError) as exc:
                message = f'{stage}: {exc}'
                state.setdefault('errors', []).append(message)
                if state is not item:
                    item['errors'].append(message)
                self.session.record_error(self.session.manifest, exc, phase='stage', **self.session.stage_context(stage, stage_context))
                invocation = destination / 'invocation.json'
                if invocation.is_file():
                    with contextlib.suppress(OSError, ContractError):
                        state[stage + '_invocation'] = strict_json(invocation.read_text())
                if self.session.critical_failure or not self.session.cfg['continue_on_error']:
                    raise
                return None
            finally:
                try:
                    guard(stage_context)
                except BaseException:
                    self.session.critical_failure = True
                    state[stage] = None
                    state.pop(stage + '_material', None)
                    state[stage + '_usable'] = False
                    raise
                finally:
                    persist()

        try:
            guard(context)
            inventory, _ = self.session.source_inventory()
            guard(context)
            artifacts.save_json(directory / 'source.inventory.json', inventory)
            context['_inventory'] = inventory
            context['inventory_summary'] = inventory_summary(inventory)
            plan = build_coverage_plan(inventory, context)
            self.publisher.publish_coverage_plan(plan, directory)
            item['coverage_plan'] = plan
            context.update(coverage_plan=plan, _coverage_plan_path=str(directory / 'coverage.plan.json'))
            previous = None
            revision_rounds = self.session.execution['max_revision_rounds'] if review_enabled else 0
            for number in range(1, revision_rounds + 2):
                revision_id = f'{number:03d}'
                revision_dir = directory / 'revisions' / revision_id
                revision = {key: item[key] for key in ('branch', 'source_commit', 'source_directory', 'source_fingerprint') if key in item}
                revision.update(revision_id=revision_id, directory=str(revision_dir.relative_to(self.session.run_dir)),
                                study=None, review=None, errors=[], review_enabled=review_enabled, workflow_satisfied=False)
                if not review_enabled:
                    revision['review_skipped'] = 'disabled_by_config'
                item['revisions'].append(revision)
                current = context | {'revision_id': revision_id}
                if previous:
                    current.update(revision_inputs(previous))
                    current['prompt_variant'] = 'revise'
                run_stage('study', revision, current, revision_dir)
                if not review_enabled:
                    skip_context = self.session.stage_context('review', current)
                    self.session.reporter.emit('stage_skipped', **skip_context, reason='disabled_by_config',
                                       metrics=self.session.metrics.finish_stage(skip_context, 'SKIPPED'))
                elif usable_study(revision) and (previous is None or revision.get('study') is not None):
                    current.pop('prompt_variant', None)
                    if previous and revision.get('study'):
                        current['registry_diff'] = revision['study']['registry_diff']
                    review_input = self.publisher.freeze_review(stage_document(revision, 'study'), current, revision_dir)
                    if revision.get('study_material') is not None:
                        meta = revision['study_invocation']
                        for name, value in (('claim.registry.json', review_input['claim_registry']),
                                            ('review.plan.json', review_input['review_plan'])):
                            content = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
                            meta['material_hashes'][name] = {'sha256': digest(content), 'bytes': len(content)}
                        artifacts.save_json(revision_dir / 'study.logs' / 'invocation.json', meta)
                    run_stage('review', revision, review_input, revision_dir)
                else:
                    revision['review_skipped'] = 'No usable strictly valid revised study.' if previous else 'No usable architecture document.'
                    self.session.metrics.finish_stage(self.session.stage_context('review', current), 'SKIPPED')
                    self.session.reporter.emit('stage_skipped', **self.session.stage_context('review', current))
                    if self.session.repo and not usable_study(revision):
                        item['errors'].append('Review skipped: no usable architecture document.')
                revision['accepted'] = accepted(revision)
                revision['workflow_satisfied'] = workflow_satisfied(revision)
                persist()
                if not (review_enabled and completed_pair(revision) and revision.get('study') and
                        any(f['severity'] in ('HIGH', 'MEDIUM') for f in revision['review']['findings'])):
                    break
                previous = revision
        except BaseException as exc:
            self.session.record_error(self.session.manifest, exc, phase='stage' if self.session.active_stage else 'run')
            raise
        finally:
            # Even a failed second stage leaves the first published pair intact.
            self.select_source_revision(item, directory, publish=not self.session.critical_failure)
            persist()

    def publish_blocked_comparison(self, context, destination):
        data = blocked_comparison(context)
        meta = {'stage': 'compare', 'generated_by': 'orchestrator',
                'reason': 'no_comparable_studies' if self.session.compromise else 'no_accepted_branches',
                'started_at': now(), 'status': 'RUNNING'}
        private_directory(destination)
        try:
            self.session.assert_source()
            validate_result('compare', data, context)
            meta['local_validation'] = True
            self.session.assert_source()
            data = prepare_result('compare', data, context | {'generated_by': 'orchestrator'})
            meta['report_sha256'] = self.publisher.publish_result('compare', data, destination)
            meta.update(status='SUCCEEDED', finished_at=now(), publication_complete=True)
            artifacts.save_json(destination / 'invocation.json', meta)
            return data, meta
        except BaseException as exc:
            meta.update(status='FAILED', error=asdict(diagnostic(exc)), finished_at=now(), publication_complete=False)
            with contextlib.suppress(OSError):
                artifacts.save_json(destination / 'invocation.json', meta)
            raise
