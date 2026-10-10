# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Citation warnings preserve authored bytes and expose safe locator facts."""
import copy
import io
from pathlib import Path
import tempfile
import unittest

from fixtures.ledger_response import response
from src.analysis.ledger import prepare_result
from src.reports.presentation import render_architecture, render_citation_warnings, render_stage
from src.runtime.reporting import Reporter
from src.tui.state import RunState


def warning(**changes):
    return dict(evidence_id='study:E-001', source_id='source-001', path='app.py', start_line=1, end_line=1,
                reason='QUOTE_MISMATCH', claim_ids=['C-001'], action='UNRESOLVED', **changes)


class CitationPresentationTests(unittest.TestCase):
    def test_architecture_appendix_preserves_exact_prefix_and_input(self):
        for text in ('# Authored\nFACT: original claim\n', 'FACT: no final newline', 'FACT: whitespace  \n\n'):
            data = {'report_markdown': text, 'program_checks': {'citation_warnings': [warning()]}}
            original = copy.deepcopy(data)
            rendered = render_architecture(data)
            self.assertTrue(rendered.startswith(text + '\n\n## Citation warnings'))
            self.assertEqual(rendered[:len(text)], text)
            self.assertIn('C-001', rendered)
            self.assertIn('UNRESOLVED', rendered)
            self.assertEqual(data, original)
        self.assertEqual(render_architecture({'report_markdown': 'unchanged'}), 'unchanged')

    def test_warning_tables_are_deterministic_escaped_and_do_not_show_quotes(self):
        first = warning()
        first.update(path='unsafe|<path>', claim_ids=['C-002', 'C-001'], quote='PRIVATE SOURCE TEXT')
        second = warning()
        second.update(evidence_id='study:E-002', action='CORRECTED', original_start_line=8, original_end_line=9,
                      branch='feature')
        data = {'program_checks': {'citation_warnings': [second, first]}}
        rendered = render_citation_warnings(data)
        data['program_checks']['citation_warnings'].reverse()
        self.assertEqual(rendered, render_citation_warnings(data))
        self.assertIn('unsafe&#124;&lt;path&gt;', rendered)
        self.assertIn('C-001, C-002', rendered)
        self.assertIn('8-9', rendered)
        self.assertIn('feature / source-001', rendered)
        self.assertNotIn('PRIVATE SOURCE TEXT', rendered)
        self.assertIn('Предупреждения о цитатах', render_citation_warnings(data, 'Russian'))

    def test_annotated_report_marks_impacted_fact_without_promoting_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'app.py').write_text('def main(): return 42\n')
            context = {'source_directory': str(Path(directory).resolve()), 'source_fingerprint': 'pinned',
                       'result_policy': 'compromise'}
            wire = response(context)
            wire['evidence'][0]['quote'] = 'PRIVATE WRONG QUOTE'
            doc = prepare_result('study', wire, context)
            rendered = render_stage('study', doc)
        self.assertIn('Citation checks required: study:E-001 (UNRESOLVED)', rendered)
        self.assertIn('strict policy checks satisfied: False', rendered)
        self.assertIn('| FACT |', rendered)
        self.assertIn('QUOTE_MISMATCH', rendered)
        self.assertNotIn('PRIVATE WRONG QUOTE', rendered)
        self.assertFalse(doc['program_checks']['policy_satisfied'])
        self.assertTrue(doc['program_checks']['continuation_allowed'])

    def test_cli_warning_is_safe_in_verbose_output_and_completion_is_not_failure(self):
        out, err, events = io.StringIO(), io.StringIO(), []
        reporter = Reporter(stdout=out, stderr=err, progress=False, verbose=True, event_sink=events.append)
        reporter.emit('citation_warning', stage='study', branch='unit', **warning(), quote='PRIVATE QUOTE', text='PRIVATE SOURCE')
        reporter.emit('stage_completed', stage='study', status='COMPLETE_WITH_WARNINGS', elapsed_seconds=1)
        reporter.finish({'status': 'COMPLETE_WITH_WARNINGS', 'exit_code': 0, 'manifest': None}, {},
                        check_only=False, config_path=Path('config.json'))
        reporter.close()
        self.assertIn('source-001 / app.py:1-1', err.getvalue())
        self.assertIn('affected claims: C-001', err.getvalue())
        self.assertIn('complete with citation warnings', out.getvalue())
        self.assertNotIn('[FAIL]', out.getvalue() + err.getvalue())
        self.assertNotIn('PRIVATE', out.getvalue() + err.getvalue())
        self.assertNotIn('quote', events[0].context)
        self.assertNotIn('text', events[0].context)

    def test_tui_warning_does_not_fail_stage_and_survives_completion(self):
        state = RunState(clock=lambda: 1)
        context = {'stage': 'study', 'branch': 'unit', 'revision_id': '001'}
        state.apply('stage_started', context)
        state.apply('citation_warning', context | warning())
        row = state.rows[('unit', 'study', '001')]
        self.assertEqual(row['status'], 'RUNNING')
        state.apply('stage_completed', context | {'status': 'COMPLETE_WITH_WARNINGS', 'elapsed_seconds': 1})
        state.finish({'result': {'status': 'COMPLETE_WITH_WARNINGS', 'exit_code': 0}})
        self.assertEqual(row['status'], 'COMPLETE_WITH_WARNINGS')
        self.assertIn('C-001', row['diagnostics'][0]['message'])
        self.assertIn('citation warnings', row['result'])
        self.assertEqual(len(state.diagnostics), 1)


if __name__ == '__main__':
    unittest.main()
