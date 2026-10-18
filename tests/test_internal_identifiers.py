# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Internal metadata uses permanent identifiers; external versions are allowed."""
import ast
import json
from pathlib import Path
import unittest

from src.contracts.saved_contracts import COVERAGE_PLAN, SAVED_SCHEMAS, SAVED_FOLDER_SCHEMAS

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {'contract_id', 'artifact_format', 'context_format', 'index_format',
          'format', 'rule', 'hash_format'}


class InternalIdentifierTests(unittest.TestCase):
    def assert_identifier(self, name, value):
        self.assertNotRegex(name.lower(),
            r'(?:schema|contract|artifact|context|rendering|index|rule|format)_?version$')
        if (name.lower() in FIELDS or name == 'RULE' or name.endswith(('_FORMAT', '_RULE'))):
            if isinstance(value, str):
                self.assertNotRegex(value, r'(?i)(?:^|[-_])v?\d+(?:\.\d+)*(?=$|[-_])')

    def test_source_metadata_uses_permanent_identifiers(self):
        for directory in ('src', 'scripts'):
            for path in sorted((ROOT / directory).rglob('*.py')):
                with self.subTest(path=str(path.relative_to(ROOT))):
                    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                        entries = []
                        if isinstance(node, ast.Dict):
                            entries = [(key.value, value) for key, value in zip(node.keys, node.values)
                                       if isinstance(key, ast.Constant) and isinstance(key.value, str)]
                        elif isinstance(node, ast.Call):
                            entries = [(keyword.arg, keyword.value) for keyword in node.keywords if keyword.arg]
                        elif isinstance(node, ast.Assign):
                            entries = [(target.id, node.value) for target in node.targets if isinstance(target, ast.Name)]
                        for name, value in entries:
                            self.assert_identifier(name, value.value if isinstance(value, ast.Constant) else None)

    def test_schema_metadata_uses_permanent_identifiers(self):
        def visit(value):
            if isinstance(value, dict):
                for name, item in value.items():
                    self.assert_identifier(name, item)
                    if name in FIELDS and isinstance(item, dict):
                        for identifier in item.get('enum', []):
                            self.assert_identifier(name, identifier)
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        for path in sorted((ROOT / 'schemas').glob('*.schema.json')):
            with self.subTest(path=path.name):
                visit(json.loads(path.read_text(encoding='utf-8')))

    def test_saved_schema_files_match_runtime(self):
        schemas = {'coverage-plan': COVERAGE_PLAN}
        for prefix, values in (('saved-', SAVED_SCHEMAS), ('saved-folder-', SAVED_FOLDER_SCHEMAS)):
            schemas.update((prefix + stage, schema) for stage, schema in values.items())
        for name, schema in schemas.items():
            with self.subTest(schema=name):
                self.assertEqual(json.loads((ROOT / 'schemas' / (name + '.schema.json')).read_text(encoding='utf-8')), schema)
