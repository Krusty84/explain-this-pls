# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Frozen stage artifacts, coverage plans and review inputs."""
from __future__ import annotations
import json
from pathlib import Path
from src.messages import message as ui_message
from src.contracts.contracts import ContractError, validate_schema
from src.analysis.ledger import review_context
from src.reports.document_rendering import validate_materialized, validate_published_architecture
from src.analysis.evidence import SourceChanged, read_confined
from src.contracts.saved_contracts import SAVED_SCHEMAS, SAVED_FOLDER_SCHEMAS
from src.reports.presentation import render_stage, render_architecture
from src.reports.review import render_review
from src.reports.comparison import render_comparison
from src.analysis.coverage_plan import verify_coverage_plan
from src.analysis.revisions import completed_pair
from src.runtime.errors import AuditError
from src.runtime.artifacts import digest
from src.runtime import artifacts

ARTIFACTS = {'study': 'ARCHITECTURE.md', 'review': 'ARCHITECTURE_REVIEW.md',
             'compare': 'BRANCH_COMPARISON.md'}

class ReportPublisher:
    def __init__(self, session):
        self.session = session

    def publish_coverage_plan(self, plan, directory):
        verify_coverage_plan(plan)
        content = (json.dumps(plan, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        path = directory / 'coverage.plan.json'
        if path.exists() or path.is_symlink():
            self.assert_coverage_file({'coverage_plan': plan, '_coverage_plan_path': str(path)})
            return
        artifacts.atomic(path, content)
        self.assert_coverage_file({'coverage_plan': plan, '_coverage_plan_path': str(path)})

    def assert_coverage_file(self, context):
        if not context.get('_coverage_plan_path'):
            return
        try:
            verify_coverage_plan(context['coverage_plan'])
            expected = (json.dumps(context['coverage_plan'], ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            relative = str(Path(context['_coverage_plan_path']).relative_to(self.session.run_dir))
            if read_confined(self.session.run_dir, relative, len(expected)) != expected:
                raise ValueError('changed')
        except (OSError, ValueError, SourceChanged) as exc:
            raise AuditError(ui_message(
                'diagnostic.coverage_plan.frozen_coverage_plan_changed',
            ), code='COVERAGE_PLAN_CHANGED', failure_layer='integrity') from exc

    def assert_revision_files(self, item):
        states = list(item.get('revisions', []))
        directory = item.get('directory', '.')
        if item.get('coverage_plan'):
            self.assert_coverage_file({'coverage_plan': item['coverage_plan'],
                                       '_coverage_plan_path': str(self.session.run_dir / directory / 'coverage.plan.json')})
        if item.get('selection_publication_complete'):
            states.append(item | {'directory': directory, 'selected_aliases': True})
        for revision in states:
            for stage in ('study', 'review'):
                meta = revision.get(stage + '_invocation') or {}
                hashes = dict(meta.get('artifact_hashes', {})) if meta.get('publication_complete') else {}
                if meta.get('material_retained'):
                    hashes.update(meta.get('material_hashes', {}))
                if not revision.get('selected_aliases'):
                    hashes.update(meta.get('binding_hashes', {}))
                    hashes.update(meta.get('attempt_hashes', {}))
                for name, expected in hashes.items():
                    relative = str(Path(revision['directory']) / name)
                    try:
                        blob = read_confined(self.session.run_dir, relative, expected['bytes'])
                        if len(blob) != expected['bytes'] or digest(blob) != expected['sha256']:
                            raise ValueError('changed')
                    except (OSError, ValueError, SourceChanged) as exc:
                        self.session.critical_failure = True
                        raise AuditError(ui_message(
                            'diagnostic.publication.published_revision_artifact_changed',
                        ), code='REVIEW_TARGET_CHANGED',
                                         failure_layer='integrity') from exc

    def publish_result(self, stage, data, destination, *, context=None):
        try:
            if stage == 'study':
                validate_materialized(data)
            validate_schema(data, (SAVED_FOLDER_SCHEMAS if self.session.mode == 'folder' else SAVED_SCHEMAS)[stage])
        except ContractError as exc:
            raise AuditError(ui_message(
                'diagnostic.publication.computed_artifact_failed_local_validation',
            ), code='ARTIFACT_CONTRACT_ERROR',
                             failure_layer='publication') from exc
        contents = self.artifact_contents(stage, data, context=context)
        # Published revision files are immutable, including JSON and annotations.
        for name, content in contents.items():
            path = destination.parent / name
            if path.exists() or path.is_symlink():
                try:
                    actual = read_confined(self.session.run_dir, str(path.relative_to(self.session.run_dir)), len(content))
                    if actual != content:
                        raise ValueError('changed')
                except (OSError, ValueError, SourceChanged) as exc:
                    raise AuditError(ui_message('diagnostic.publication.frozen_stage_artifacts_cannot_be_replaced_select_a'),
                                     code='REVIEW_TARGET_CHANGED', failure_layer='publication') from exc
        for name, content in contents.items():
            path = destination.parent / name
            if not path.exists():
                artifacts.atomic(path, content)
        return digest(contents[ARTIFACTS[stage]])

    def artifact_contents(self, stage, data, *, context=None):
        def encoded(value):
            return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        language = self.session.cfg.get('output_language')
        study = (context or {}).get('architecture_document')
        if stage == 'study':
            report = render_architecture(data, language)
        elif stage == 'review':
            report = render_review(data, language, study)
        else:
            report = render_comparison(data, language, context)
        if stage == 'study':
            validate_published_architecture(report, self.session.cfg.get('output_language'), canonical=data)
        contents = {ARTIFACTS[stage]: report.encode('utf-8'),
                    stage + '.original.md': data['report_markdown'].encode('utf-8'),
                    stage + '.json': encoded(data)}
        if stage == 'study':
            if data['review_plan']['coverage_plan'] is not None:
                contents['coverage.plan.json'] = encoded(data['review_plan']['coverage_plan'])
            contents.update({'study.annotated.md': render_stage(stage, data, self.session.cfg.get('output_language')).encode('utf-8'),
                             'claim.registry.json': encoded(data['claims']), 'review.plan.json': encoded(data['review_plan'])})
            if data.get('registry_diff') is not None:
                contents['registry.diff.json'] = encoded(data['registry_diff'])
        elif stage == 'review':
            contents['review.annotated.md'] = render_stage(stage, data, language, study=study).encode('utf-8')
        return contents

    def assert_review_files(self, context, directory):
        expected = {'claim.registry.json': (json.dumps(context['claim_registry'], ensure_ascii=False, indent=2) + '\n').encode(),
            'review.plan.json': (json.dumps(context['review_plan'], ensure_ascii=False, indent=2) + '\n').encode()}
        if context.get('document_strictly_valid'):
            report = render_architecture(context['architecture_document'], self.session.cfg.get('output_language'))
            validate_published_architecture(report, self.session.cfg.get('output_language'),
                                            canonical=context['architecture_document'])
            expected['ARCHITECTURE.md'] = report.encode('utf-8')
        for name, content in expected.items():
            try:
                actual = read_confined(self.session.run_dir, str((directory / name).relative_to(self.session.run_dir)), len(content))
                if actual != content:
                    raise ValueError('changed')
            except (OSError, ValueError, SourceChanged) as exc:
                raise AuditError(ui_message('diagnostic.publication.published_document_registry_or_review_plan_changed'),
                                 code='REVIEW_TARGET_CHANGED', failure_layer='integrity') from exc

    def freeze_review(self, doc, context, directory):
        result = review_context(doc, context)
        if result['document_strictly_valid']:
            self.assert_review_files(result, directory)
        else:
            pending = {'claim.registry.json': result['claim_registry'], 'review.plan.json': result['review_plan']}
            for name, value in pending.items():
                path = directory / name
                expected = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
                if path.exists() or path.is_symlink():
                    try:
                        if read_confined(self.session.run_dir, str(path.relative_to(self.session.run_dir)), len(expected)) != expected:
                            raise ValueError('changed')
                    except (OSError, ValueError, SourceChanged) as exc:
                        raise AuditError(ui_message(
                            'diagnostic.publication.recovered_material_cannot_replace_an_existing_frozen_registry',
                        ),
                                         code='REVIEW_TARGET_CHANGED', failure_layer='publication') from exc
            for name, value in pending.items():
                if not (directory / name).exists():
                    artifacts.save_json(directory / name, value)
        return result

    def publish_revision(self, selected, directory):
        source = directory / 'revisions' / selected['revision_id']
        names = ['study.json', 'study.material.json', 'study.original.md', 'study.annotated.md',
                 'ARCHITECTURE.md', 'claim.registry.json', 'review.plan.json', 'registry.diff.json']
        if completed_pair(selected):
            names += ['review.json', 'review.original.md', 'review.annotated.md', 'ARCHITECTURE_REVIEW.md']
        expected_files = {}
        for stage in ('study', 'review'):
            meta = selected.get(stage + '_invocation') or {}
            expected_files.update(meta.get('artifact_hashes', {}))
            expected_files.update(meta.get('material_hashes', {}))
        for name in names:
            path = source / name
            if name in expected_files or path.exists():
                expected = expected_files.get(name)
                blob = read_confined(self.session.run_dir, str(path.relative_to(self.session.run_dir)),
                                     expected['bytes'] if expected else path.stat().st_size)
                if expected and (len(blob) != expected['bytes'] or digest(blob) != expected['sha256']):
                    raise AuditError(ui_message('diagnostic.publication.selected_revision_artifact_changed_during_publication'),
                                     code='REVIEW_TARGET_CHANGED', failure_layer='integrity')
                artifacts.atomic(directory / name, blob)
