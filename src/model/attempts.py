# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Pinned private attempt values and model binding files."""
from __future__ import annotations
import json
from src.messages import message as ui_message
from src.contracts.contracts import ContractError, strict_json
from src.analysis.evidence import SourceChanged, read_confined
from src.runtime.errors import AuditError
from src.runtime.artifacts import digest
from src.runtime import artifacts

class AttemptStore:
    def __init__(self, session):
        self.session = session

    def pin_attempt_value(self, attempt, name, value, meta, *, compact=False):
        content = (json.dumps(value) if compact else json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        relative = str((attempt / name).relative_to(attempt.parent.parent))
        meta['attempt_hashes'][relative] = {'sha256': digest(content), 'bytes': len(content)}

    def save_attempt_value(self, attempt, name, value, meta):
        # Pin the program's value, not a reread of bytes writable by a subprocess.
        artifacts.save_json(attempt / name, value)
        self.pin_attempt_value(attempt, name, value, meta)

    def read_attempt_value(self, attempt, name, hashes):
        relative = str((attempt / name).relative_to(attempt.parent.parent))
        expected = hashes[relative]
        try:
            content = read_confined(self.session.run_dir, str((attempt / name).relative_to(self.session.run_dir)), expected['bytes'])
            if len(content) != expected['bytes'] or digest(content) != expected['sha256']:
                raise ValueError('changed')
        except (OSError, ValueError, SourceChanged) as exc:
            self.session.critical_failure = True
            raise AuditError(ui_message('diagnostic.attempts.private_attempt_artifact_changed'), code='MODEL_BINDING_CHANGED',
                             failure_layer='integrity') from exc
        return strict_json(content.decode('utf-8'))

    def save_binding(self, binding, raw, expanded, attempt, meta):
        record = binding.record(raw, expanded)
        content = (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        path = attempt / 'binding.json'
        artifacts.atomic(path, content)
        relative = str(path.relative_to(attempt.parent.parent))
        meta['binding_hashes'][relative] = {'sha256': digest(content), 'bytes': len(content)}

    def assert_binding_files(self, binding, context, directory, hashes):
        try:
            binding.assert_unchanged(context)
            for name, expected in hashes.items():
                relative = str((directory / name).relative_to(self.session.run_dir))
                content = read_confined(self.session.run_dir, relative, expected['bytes'])
                if len(content) != expected['bytes'] or digest(content) != expected['sha256']:
                    raise ValueError('changed')
        except (ContractError, OSError, ValueError, SourceChanged) as exc:
            self.session.critical_failure = True
            raise AuditError(ui_message(
                'diagnostic.attempts.model_binding_changed_during_invocation',
            ), code='MODEL_BINDING_CHANGED',
                             failure_layer='integrity') from exc
