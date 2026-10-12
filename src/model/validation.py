# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Validate, normalize and materialize a bound model response."""
from __future__ import annotations
import contextlib
from dataclasses import asdict
from src.contracts.contracts import ContractError, validate_result, schema_diagnostics, result_diagnostics, validate_schema, document_contract
from src.runtime.reporting import diagnostic
from src.analysis.study_normalization import normalize_evidence, normalization_provenance
from src.analysis.citation_repair import repair_citations
from src.reports.document_rendering import materialize_study
from src.analysis.evidence import SourceChanged
from src.runtime.errors import AuditError, UnsafeRepository

class ResultValidator:
    def __init__(self, session, attempts):
        self.session, self.attempts = session, attempts

    def validate_attempt(self, stage, data, context, attempt, meta, binding, *,
                         expected_files=None, expected_metadata=None):
        candidate = data
        validation = {'valid': False, 'validated_object': 'extracted.json'}
        try:
            self.attempts.pin_attempt_value(attempt, 'extracted.json', data, meta)
            self.attempts.save_binding(binding, data, None, attempt, meta)
            binding.validate_identity(data)
            meta['model_identity_verified'] = True
            candidate = binding.expand(data, allow_invalid=True)
            self.attempts.save_attempt_value(attempt, 'expanded.json', candidate, meta)
            self.attempts.save_binding(binding, data, candidate, attempt, meta)
            validation['validated_object'] = 'expanded.json'
            validate_schema(document_contract(stage, data), binding.schema)
            expanded = candidate
            if stage in ('study', 'review'):
                normalization_input = expanded
                if self.session.compromise:
                    try:
                        normalization_input, corrections = repair_citations(
                            stage, expanded, context, expected_files, expected_metadata=expected_metadata)
                    except SourceChanged as exc:
                        raise UnsafeRepository('Source changed during citation correction.', code='SOURCE_CHANGED',
                            failure_kind='SOURCE_CHANGED', failure_layer='integrity') from exc
                    if corrections.get('diagnostics'):
                        self.attempts.save_attempt_value(attempt, 'quote-diagnostics.json', corrections['diagnostics'], meta)
                    if corrections['changes']:
                        self.attempts.save_attempt_value(attempt, 'quote-corrected.json', normalization_input, meta)
                        self.attempts.save_attempt_value(attempt, 'quote-corrections.json',
                                                {k: v for k, v in corrections.items() if k != 'diagnostics'}, meta)
                        meta['citation_corrections'] = [change | {
                            key: {k: v for k, v in change[key].items() if k != 'quote'}
                            for key in ('original', 'repaired')} for change in corrections['changes']]
                        meta['citation_correction_provenance'] = {k: v for k, v in corrections.items()
                                                                  if k not in ('changes', 'diagnostics')}
                        validation['citation_corrections'] = meta['citation_correction_provenance']
                candidate, changes = normalize_evidence(stage, normalization_input, context, self.session.mode)
                provenance = normalization_provenance(normalization_input, candidate, changes)
                # Required artifacts precede validation/publication; extracted.json
                # remains the adapter's original object, including on failure.
                self.attempts.save_attempt_value(attempt, 'normalized.json', candidate, meta)
                self.attempts.save_attempt_value(attempt, 'normalization.json', provenance | {'changes': changes}, meta)
                meta['normalization_provenance'] = provenance
                validation.update(validated_object='normalized.json', normalization_provenance=provenance)
                if changes:
                    self.session.reporter.emit(stage + '_normalized', **self.session.stage_context(stage, context),
                                       rule=provenance['rule'], replacement_count=len(changes))
            validate_result(stage, candidate, context, self.session.mode)
            validation['valid'] = True
        except BaseException as exc:
            validation.update(result_diagnostics(stage, candidate, context, self.session.mode))
            validation['schema_diagnostics'] = schema_diagnostics(document_contract(stage, data), binding.schema, private=True)
            validation['error'] = asdict(diagnostic(exc))
            meta['local_validation'] = False
            meta['validation_failed'] = True
            with contextlib.suppress(OSError):
                self.attempts.save_attempt_value(attempt, 'validation.json', validation, meta)
            raise
        validation.update(result_diagnostics(stage, candidate, context, self.session.mode))
        meta['local_validation'] = True
        self.attempts.save_attempt_value(attempt, 'validation.json', validation, meta)
        if stage == 'study':
            try:
                candidate = materialize_study(candidate)
            except ContractError as exc:
                raise AuditError('Program document assembly failed its invariant checks.',
                                 code='MATERIALIZATION_ERROR', failure_layer='materialization') from exc
            self.attempts.save_attempt_value(attempt, 'materialized.json', candidate, meta)
            self.attempts.save_attempt_value(attempt, 'provenance.json', {
                'binding': binding.record(data, expanded),
                **({'citation_correction': meta['citation_correction_provenance']}
                   if meta.get('citation_correction_provenance') else {}),
                'normalization': meta['normalization_provenance'],
                'materialization': candidate['materialization_provenance']}, meta)
        return candidate
