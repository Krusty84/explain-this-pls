# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Message formatting and independent checks of visible application text."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from src.messages import MESSAGES, message
from src.runtime.reporting import citation_warning_message
from src.tui.state import RunState

ROOT = Path(__file__).resolve().parents[1]


class MessageTests(unittest.TestCase):
    def test_parameters_preserve_paths_quotes_braces_and_number_formats(self):
        path = Path('/tmp/проект {draft}/"source".py')
        self.assertEqual(message('installer.requirements_file_is_missing', requirements=path),
                         'Requirements file is missing: /tmp/проект {draft}/"source".py')
        self.assertEqual(message('diagnostic.codex_models.the_model_check_exceeded_its_second_time_limit',
                                 timeout=2.5),
                         'The model check exceeded its 2.5-second time limit.')
        with patch.dict(MESSAGES, {'test.template': '{{literal}} {path!r} {count:04d}'}):
            self.assertEqual(message('test.template', path='a "quote" {brace}', count=7),
                             '{literal} \'a "quote" {brace}\' 0007')

    def test_unknown_key_and_missing_parameter_raise_key_error(self):
        with self.assertRaises(KeyError):
            message('unknown.message')
        with self.assertRaises(KeyError):
            message('installer.requirements_file_is_missing')

    def test_catalog_import_needs_no_site_packages_or_application_modules(self):
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
from src.messages import message
assert message('tui.preparing_analysis') == 'Preparing analysis…'
assert not any(name.startswith(('textual', 'rich', 'src.runtime')) for name in sys.modules)
'''
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', script, str(ROOT)],
                                cwd=ROOT.parent, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tui_keeps_current_phase_and_outcome_messages(self):
        state = RunState()
        context = dict(branch='main', stage='study', revision_id='001')
        state.apply('stage_started', context)
        row = state.rows[('main', 'study', '001')]
        self.assertEqual(row['result'], 'Analyzing project…')
        for phase, expected in [('collect', 'Collecting claims and source evidence.'),
                                ('write', 'Writing the architecture report.'),
                                ('plantuml_repair', 'Correcting PlantUML syntax.')]:
            state.apply('study_phase_started', dict(context, phase=phase))
            self.assertEqual(row['result'], expected)
        for status, expected in [('COMPLETE', 'Architecture report created.'),
                                 ('PARTIAL', 'See limitations and diagnostics.'),
                                 ('COMPLETE_WITH_WARNINGS',
                                  'Completed with citation warnings. See affected claims.')]:
            state.apply('stage_completed', dict(context, status=status))
            self.assertEqual(row['result'], expected)

    def test_citation_warning_keeps_optional_original_lines_and_punctuation(self):
        warning = dict(evidence_id='study:E-001', source_id='source-001', path='a {draft}.py',
                       start_line=1, end_line=2, reason='QUOTE_MISMATCH', action='UNRESOLVED',
                       claim_ids=['C-001', 'C-002'])
        prefix = ('Citation study:E-001: source-001 / a {draft}.py:1-2; QUOTE_MISMATCH; '
                  'UNRESOLVED; affected claims: C-001, C-002')
        self.assertEqual(citation_warning_message(warning),
                         prefix + '. Check citation support for these claims.')
        warning.update(original_start_line=3, original_end_line=4)
        self.assertEqual(citation_warning_message(warning),
                         prefix + '; original lines: 3-4. Check citation support for these claims.')


if __name__ == '__main__':
    unittest.main()
