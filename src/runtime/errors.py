# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Audit failures shared by source checks and orchestration."""
from __future__ import annotations
from src.messages import message as ui_message
from src.runtime.reporting import Diagnostic

class AuditError(RuntimeError):
    def __init__(self, message: str = '', *, code=None, snapshot=None, node_path=None,
                 required_commit=None, hint=None, failure_kind=None, failure_layer=None, details=None):
        super().__init__(message)
        self.message = message
        self.code = code or ('UNSAFE_REPOSITORY' if isinstance(self, UnsafeRepository) else 'AUDIT_ERROR')
        self.snapshot, self.node_path = snapshot, node_path
        self.required_commit, self.hint = required_commit, hint
        self.failure_kind, self.failure_layer, self.details = failure_kind, failure_layer, details

    def diagnostic(self):
        return Diagnostic(self.code, self.message, self.snapshot, self.node_path,
                          self.required_commit, self.hint, self.failure_kind, self.failure_layer, self.details)

    def with_context(self, **context):
        fields = vars(self.diagnostic()).copy()
        for key, value in context.items():
            if fields.get(key) is None:
                fields[key] = value
        return type(self)(**fields)

    def __str__(self):
        details = []
        if self.snapshot is not None:
            details.append(ui_message('diagnostic.errors.snapshot', snapshot=self.snapshot))
        if self.node_path is not None:
            details.append(ui_message('diagnostic.errors.path', node_path=self.node_path))
        if self.required_commit is not None:
            details.append(ui_message('diagnostic.errors.required_sha', required_commit=self.required_commit))
        return (', '.join(details) + ': ' if details else '') + self.message

class UnsafeRepository(AuditError):
    pass
