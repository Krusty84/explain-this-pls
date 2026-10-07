# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Run-local model bindings. Full identities never depend on model-supplied hashes."""
from __future__ import annotations

import copy
import json
import secrets

from src.contracts.contracts import contract_violation, validate_schema, MODEL_SCHEMAS, MODEL_FOLDER_SCHEMAS
from src.analysis.evidence import canonical, sha
from src.model.model_context import project_model_context

RULE = 'MODEL_BINDING'
HASH_FORMAT = 'canonical-json-utf8'
TARGET_KEYS = ('source_sha256', 'document_sha256', 'registry_sha256', 'plan_sha256')


def _error(path):
    return contract_violation('MODEL_BINDING_IDENTITY_MISMATCH', path,
                              kind='IDENTITY_MISMATCH', layer='identity')


def _target(value):
    if isinstance(value, dict) and all(k in value for k in TARGET_KEYS):
        return {k: value[k] for k in TARGET_KEYS}
    return None


def _folder(value):
    if isinstance(value, dict):
        if 'source_directory' in value and 'source_fingerprint' in value:
            return {'directory': value['source_directory'], 'fingerprint': value['source_fingerprint']}
        if value.get('mode') == 'folder' and 'directory' in value and 'fingerprint' in value:
            return {'directory': value['directory'], 'fingerprint': value['fingerprint']}
    return None


class BindingRegistry:
    """One instance per run; never share bindings between runs or load old IDs."""
    def __init__(self):
        self._ids = {}
        self._used = set()

    def _id(self, role, identity):
        key = (role, canonical(identity))
        if key not in self._ids:
            prefix = 'S-' if role == 'source_snapshot' else 'T-'
            token = prefix + secrets.token_urlsafe(12)
            while token in self._used:
                token = prefix + secrets.token_urlsafe(12)
            self._ids[key] = token
            self._used.add(token)
        return self._ids[key]

    def bind(self, stage, context, mode='git'):
        return Binding(self, stage, context, mode)


class Binding:
    def __init__(self, registry, stage, context, mode):
        self._registry = registry
        self._stage, self._mode = stage, mode
        self._context = copy.deepcopy(context)
        self._context_digest = sha(canonical(context))
        self._sources, self._targets = {}, {}
        self._references = {}
        self._review_id = None
        self._snapshot_id = None

        def scan(value):
            if isinstance(value, dict):
                identity = _folder(value)
                if identity:
                    self._sources[canonical(identity)] = registry._id('source_snapshot', identity)
                target = _target(value)
                if target:
                    self._targets[canonical(target)] = registry._id('review_target', target)
                for child in value.values():
                    scan(child)
            elif isinstance(value, list):
                for child in value:
                    scan(child)
        scan(context)
        if mode == 'folder':
            identity = _folder(context)
            if identity is None:
                raise _error('$.source_snapshot_id')
            self._snapshot_id = self._sources[canonical(identity)]
        if stage == 'review':
            target = _target(context.get('review_target'))
            if target is None:
                raise _error('$.review_target_id')
            self._review_id = self._targets[canonical(target)]
        if stage == 'compare':
            for entry in context.get('branches', []):
                study = entry.get('study') or {}
                target = _target(study.get('review_plan'))
                revision = study.get('revision_id')
                selected = entry.get('selected_revision', revision)
                if target is None or revision != selected:
                    continue
                identifier = self._targets[canonical(target)]
                for artifact in ('study', 'review'):
                    document = entry.get(artifact)
                    if not isinstance(document, dict):
                        continue
                    self._references[(entry['branch'], revision, artifact)] = {
                        'review_target_id': identifier, 'target': target,
                        'claim_ids': [claim['id'] for claim in document.get('claims', [])]}
        self._seal = sha(canonical(self._state()))

    def _state(self):
        return {'sources': sorted((key.decode('utf-8'), value) for key, value in self._sources.items()),
                'targets': sorted((key.decode('utf-8'), value) for key, value in self._targets.items()),
                'references': sorted((list(key), value) for key, value in self._references.items()),
                'snapshot_id': self._snapshot_id, 'review_id': self._review_id,
                'stage': self._stage, 'mode': self._mode}

    def assert_unchanged(self, context=None):
        if (sha(canonical(self._state())) != self._seal
                or sha(canonical(self._context if context is None else context)) != self._context_digest):
            raise _error('$.binding')
        for role, mappings in (('source_snapshot', self._sources), ('review_target', self._targets)):
            if any(self._registry._ids.get((role, key)) != value for key, value in mappings.items()):
                raise _error('$.binding')

    def project(self, context=None):
        self.assert_unchanged(context)
        def visit(value):
            if isinstance(value, dict):
                projected = {key: visit(item) for key, item in value.items()}
                source = _folder(value)
                if source:
                    projected['source_snapshot_id'] = self._sources[canonical(source)]
                target = _target(value)
                if target:
                    projected['review_target_id'] = self._targets[canonical(target)]
                for key in ('target', 'review_target'):
                    target = _target(value.get(key))
                    if target:
                        projected['review_target_id'] = self._targets[canonical(target)]
                return projected
            if isinstance(value, list):
                return [visit(item) for item in value]
            return copy.deepcopy(value)
        return project_model_context(self._stage, visit(self._context if context is None else context))

    def validate_identity(self, raw):
        """Check identity even if non-identity records are structurally malformed."""
        self.assert_unchanged()
        if not isinstance(raw, dict):
            raise _error('$')
        if ('source_snapshot_id' in raw and self._mode != 'folder'
                or 'review_target_id' in raw and self._stage != 'review'):
            raise _error('$.binding')
        if self._stage in ('catalog', 'study', 'study-shard', 'review'):
            keys = ('source_directory',) if self._mode == 'folder' else ('branch', 'source_commit')
            for key in keys:
                if raw.get(key) != self._context.get(key):
                    raise _error('$.' + key)
            if self._mode == 'folder' and (raw.get('source_snapshot_id') != self._snapshot_id
                                           or 'source_fingerprint' in raw):
                raise _error('$.source_snapshot_id')
        if self._stage == 'study-shard':
            shard = self._context['analysis_shard']
            if raw.get('shard_id') != shard['id'] or raw.get('assigned_subsystem_ids') != shard['subsystem_ids']:
                raise _error('$.analysis_shard')
        if self._stage == 'review' and (raw.get('review_target_id') != self._review_id or 'target' in raw):
            raise _error('$.review_target_id')
        if self._stage == 'compare':
            for key in ('baseline_branch', 'baseline_commit'):
                if raw.get(key) != self._context.get(key):
                    raise _error('$.' + key)
            differences = raw.get('differences')
            for diff in differences if isinstance(differences, list) else []:
                if not isinstance(diff, dict):
                    continue
                refs = diff.get('evidence_refs')
                for ref in refs if isinstance(refs, list) else []:
                    if not isinstance(ref, dict):
                        continue
                    key = tuple(ref.get(k) for k in ('branch', 'revision_id', 'artifact'))
                    allowed = self._references.get(key) if all(isinstance(k, str) for k in key) else None
                    if (allowed is None or ref.get('review_target_id') != allowed['review_target_id']
                            or ref.get('claim_id') not in allowed['claim_ids']
                            or 'document_sha256' in ref or 'registry_sha256' in ref
                            or ref.get('branch') not in (self._context['baseline_branch'], diff.get('branch'))):
                        raise _error('$.differences[].evidence_refs[]')

    def expand(self, raw, allow_invalid=False):
        self.validate_identity(raw)
        if not allow_invalid:
            validate_schema(raw, (MODEL_FOLDER_SCHEMAS if self._mode == 'folder' else MODEL_SCHEMAS)[self._stage])
        expanded = copy.deepcopy(raw)
        if self._mode == 'folder':
            expanded.pop('source_snapshot_id', None)
            expanded['source_fingerprint'] = self._context['source_fingerprint']
        if self._stage == 'review':
            expanded.pop('review_target_id', None)
            expanded['target'] = copy.deepcopy(self._context['review_target'])
        if self._stage == 'compare':
            differences = expanded.get('differences')
            for diff in differences if isinstance(differences, list) else []:
                refs = diff.get('evidence_refs') if isinstance(diff, dict) else None
                for ref in refs if isinstance(refs, list) else []:
                    if not isinstance(ref, dict):
                        continue
                    target = self._references[tuple(ref[k] for k in ('branch', 'revision_id', 'artifact'))]['target']
                    ref.pop('review_target_id', None)
                    ref.update({key: target[key] for key in ('document_sha256', 'registry_sha256')})
        return expanded

    def record(self, raw, expanded):
        self.assert_unchanged()
        mappings = []
        for role, values in (('source_snapshot', self._sources), ('review_target', self._targets)):
            mappings.extend({'role': role, 'id': identifier, 'identity': json.loads(key)}
                            for key, identifier in sorted(values.items()))
        return {'rule': RULE, 'hash_format': HASH_FORMAT, 'stage': self._stage, 'mode': self._mode,
                'mappings': mappings,
                'reference_scope': [dict(branch=key[0], revision_id=key[1], artifact=key[2], **copy.deepcopy(value))
                                    for key, value in sorted(self._references.items())],
                'wire_sha256': sha(canonical(raw)) if raw is not None else None,
                'expanded_sha256': sha(canonical(expanded)) if expanded is not None else None}
