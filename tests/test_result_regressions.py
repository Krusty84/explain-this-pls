# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline regressions for untrusted responses and safe failure reporting."""
import json
import io
from pathlib import Path
import unittest

import claude_code
from contracts import ContractError, FOLDER_SCHEMAS, SCHEMAS, strict_json, validate_result
from reporting import Reporter, diagnostic


class ResultRegressions(unittest.TestCase):
    def test_json_error_retains_safe_positions(self):
        try:
            strict_json('{\n "secret": }')
        except ContractError as exc:
            detail = diagnostic(exc)
            self.assertEqual(detail.failure_kind, 'INVALID_JSON')
            self.assertEqual(detail.details, {'json_error': 'Expecting value', 'line': 2,
                                             'column': 12, 'position': 13})
            self.assertEqual(diagnostic(exc.__cause__).failure_kind, 'INVALID_JSON')
            self.assertNotIn('secret', detail.message)

    def test_schema_diagnostic_does_not_include_model_values(self):
        for value in (None, [], 'secret', {'secret': 'secret'}):
            with self.subTest(value=value), self.assertRaises(ContractError) as caught:
                validate_result('study', value, {})
            detail = diagnostic(caught.exception)
            self.assertEqual(detail.failure_kind, 'SCHEMA_ERROR')
            self.assertEqual(detail.details['path'], '$')
            self.assertNotIn('secret', str(detail))

    def test_claude_untrusted_transport_types(self):
        for value in (None, [], 'secret', {'is_error': []}, {'structured_output': {}}):
            with self.subTest(value=value), self.assertRaises(ContractError) as caught:
                claude_code.parse_output(json.dumps(value))
            self.assertEqual(diagnostic(caught.exception).failure_kind, 'TRANSPORT_ERROR')

    def test_public_schemas_equal_python_source(self):
        from contracts import MODEL_SCHEMAS, MODEL_FOLDER_SCHEMAS
        from saved_contracts import SAVED_SCHEMAS, SAVED_FOLDER_SCHEMAS, COVERAGE_PLAN
        root = Path(__file__).resolve().parents[1]
        for prefix, schemas in (('', MODEL_SCHEMAS), ('folder-', MODEL_FOLDER_SCHEMAS),
                                ('saved-', SAVED_SCHEMAS), ('saved-folder-', SAVED_FOLDER_SCHEMAS)):
            for stage, schema in schemas.items():
                saved = json.loads((root / 'schemas' / f'{prefix}{stage}.schema.json').read_text())
                self.assertEqual(saved, schema)
                for field in ('contract_id', 'artifact_format', 'context_format'):
                    self.assertNotIn(field, saved['properties'])
        self.assertEqual(json.loads((root / 'schemas/coverage-plan.schema.json').read_text()), COVERAGE_PLAN)

    def test_prompts_exclude_orchestrator_format_metadata(self):
        for path in (Path(__file__).resolve().parents[1] / 'prompts').glob('*.md'):
            for field in ('contract_id', 'artifact_format', 'context_format'):
                self.assertNotIn(field, path.read_text())

    def test_json_cause_is_not_logged_as_internal_error_or_response_excerpt(self):
        text = '{"private_key": "DO_NOT_PRINT_REPORT", invalid}'
        out, err = io.StringIO(), io.StringIO()
        reporter = Reporter(stdout=out, stderr=err, verbose=True)
        self.addCleanup(reporter.close)
        try:
            strict_json(text)
        except ContractError as exc:
            detail = reporter.error(exc)
            chain = reporter.exception_chain(exc)
        self.assertNotIn('INTERNAL_ERROR', err.getvalue() + chain)
        self.assertNotIn('DO_NOT_PRINT_REPORT', err.getvalue() + chain)
        self.assertEqual(detail['details']['position'], 39)
        self.assertIn('char 39', err.getvalue())


if __name__ == '__main__':
    unittest.main()
