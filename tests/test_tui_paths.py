# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Path layout, tooltips and single-click editor navigation."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from rich.cells import cell_len
from rich.console import Console
from textual.geometry import Region
from textual.widgets import DataTable, Static, TabbedContent, Tooltip

from src.tui.app import ExplainApp
from src.tui.paths import PathText, compact_path
from src.tui.state import RunState
from test_tui import options


class PathRenderingTests(unittest.TestCase):
    def test_compact_paths_fit_cell_width_and_keep_names_and_extensions(self):
        path = '/Users/проект 深い [bold]/' + 'parent/' * 20 + 'FINAL_REPORT.md'
        for width in range(121):
            with self.subTest(width=width):
                value = compact_path(path, width)
                self.assertLessEqual(cell_len(value), width)
                if width >= cell_len('.../FINAL_REPORT.md'):
                    self.assertTrue(value.endswith('/FINAL_REPORT.md'))
                if width >= 4:
                    self.assertIn('...', value)
        for path in ('/tmp/a.md', '/tmp/深い/report.md'):
            self.assertEqual(compact_path(path, cell_len(path)), path)
        path = '/tmp/' + 'очень_длинное_имя_深い' * 10 + '.md'
        for width in (10, 30, 60):
            value = compact_path(path, width)
            self.assertLessEqual(cell_len(value), width)
            self.assertTrue(value.endswith('.md'))
            self.assertIn('...', value)

    def test_path_rendering_keeps_the_full_target_and_literal_characters(self):
        path = '/Users/имя 深い [bold] #100%/' + 'parent/' * 12 + 'FINAL_REPORT.md'
        content = PathText(path, 'zed', prefix='Result: Complete\nReport: ')
        console = Console(width=60)
        for width in (30, 60, 120):
            lines = console.render_lines(content, console.options.update_width(width), pad=False)
            self.assertEqual(len(lines), 2)
            self.assertTrue(''.join(segment.text for segment in lines[-1]).endswith('FINAL_REPORT.md'))
            targets = [segment.style.meta for segment in lines[-1]
                       if segment.style and 'path' in segment.style.meta]
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0]['path'], path)
            self.assertIn('app.open_path(', targets[0]['@click'])
        self.assertEqual(content.plain, 'Result: Complete\nReport: ' + path)


class TuiPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.results = self.base / ("проект 深い [bold] #100% 'draft' " * 3) / ('parent_' * 15) / 'saved-results'
        self.results.mkdir(parents=True)
        self.report = self.results / 'FINAL_REPORT.md'
        self.report.write_text('Report\n')
        self.log = self.results / 'run.log'
        self.log.write_text('Log\n')
        self.config = self.base / 'config.json'

    def app(self, editor, *, analysis=True):
        value = {'mode': 'folder', 'folder_mode': {'path': str(self.base / 'source')},
                 'reports_dir': str(self.base / 'reports'),
                 'agent': {'backend': 'codex', 'executable': sys.executable}, 'your_editor': editor}
        if analysis:
            value['last_analysis'] = {'status': 'COMPLETED', 'finished_at': '2026-11-04T09:30:00+00:00',
                                      'results_dir': str(self.results)}
        self.config.write_text(json.dumps(value))
        app = ExplainApp(options(self.config, no_progress=True), settings_path=self.base / 'explain.config')
        app.TOOLTIP_DELAY = .1
        return app

    def position(self, widget, path):
        matches = []
        for y, strip in enumerate(widget.render_lines(Region(0, 0, *widget.size))):
            x = 0
            for segment in strip:
                if segment.style and segment.style.meta.get('path') == str(path):
                    matches.append((x, y, segment.text))
                x += segment.cell_length
        self.assertTrue(matches, (widget.id, str(path)))
        self.assertEqual(len({y for _, y, _ in matches}), 1)
        x, y, _ = matches[0]
        return (x, y), ''.join(text for _, _, text in matches)

    async def tooltip_and_click(self, app, pilot, widget, path, editor):
        await pilot.hover('Header')
        if widget.id == 'config-preview':
            widget.parent.scroll_end(animate=False)
        await pilot.pause()
        offset, text = self.position(widget, path)
        self.assertIn('...', text)
        self.assertTrue(await pilot.hover(widget, offset=offset))
        await pilot.pause(.2)
        self.assertEqual(widget.tooltip.plain, str(path))
        self.assertTrue(app.screen.query_one(Tooltip).display)
        with patch.object(app, 'open_url') as opener:
            self.assertTrue(await pilot.click(widget, offset=offset))
            await pilot.pause()
        opener.assert_called_once()
        url = urlsplit(opener.call_args.args[0])
        self.assertEqual((url.scheme, url.netloc), (editor, 'file'))
        self.assertEqual(unquote(url.path), str(path) + ('/' if path.is_dir() else ''))
        self.assertFalse(url.query or url.fragment)
        self.assertIn('%23', opener.call_args.args[0])
        self.assertIn('%25', opener.call_args.args[0])

    async def test_picker_paths_resize_show_full_tooltips_and_open_directories(self):
        for editor in ('vscode', 'zed'):
            app = self.app(editor)
            async with app.run_test(size=(80, 30), tooltips=True) as pilot:
                preview = app.query_one('#config-preview', Static)
                self.assertNotIn('Reports:', str(preview.content.renderables[-1].columns[0]._cells[0]))
                for width in (60, 120, 80):
                    await pilot.resize_terminal(width, 30)
                    await pilot.pause()
                    await self.tooltip_and_click(app, pilot, preview, self.results, editor)
                preview.parent.scroll_home(animate=False)
                await pilot.pause()
                self.assertTrue(await pilot.hover(preview, offset=(0, 0)))
                await pilot.pause()
                self.assertIsNone(preview.tooltip)

    async def test_picker_hides_results_before_the_first_analysis(self):
        app = self.app('vscode', analysis=False)
        async with app.run_test() as pilot:
            preview = app.query_one('#config-preview', Static)
            details = preview.content.renderables[-1]
            self.assertNotIn('Reports:', details.columns[0]._cells[0].plain)
            self.assertEqual(details.columns[1]._cells[0].plain, '')

    async def test_step_and_summary_paths_open_on_first_click_after_resizing(self):
        for editor in ('vscode', 'zed'):
            app = self.app(editor)
            async with app.run_test(size=(80, 30), tooltips=True) as pilot:
                app.state = RunState()
                app.query_one('#picker').display = False
                app.query_one('#dashboard').display = True
                app.state.apply('stage_completed', {'branch': 'folder', 'stage': 'study', 'revision_id': '001',
                                                   'status': 'COMPLETE', 'report_path': str(self.report)})
                app.render_stages()
                stages = app.query_one('#stages', DataTable)
                stages.move_cursor(row=1)
                await pilot.pause()
                details = app.query_one('#step-details', Static)
                for width in (60, 120, 80):
                    await pilot.resize_terminal(width, 30)
                    await pilot.pause()
                    await self.tooltip_and_click(app, pilot, details, self.report, editor)
                payload = {'result': {'status': 'COMPLETE', 'exit_code': 0},
                           'reviews': [('', 'Accepted')],
                           'paths': [('Final report', str(self.report)), ('Technical log', str(self.log))],
                           'diagnostics': [{'message': 'Warning ' + str(index)} for index in range(30)]}
                app.complete(payload)
                await pilot.pause()
                self.assertEqual(app.query_one('#run-tabs', TabbedContent).active, 'summary-tab')
                summary = app.query_one('#summary', DataTable)
                for width in (60, 120, 80):
                    await pilot.resize_terminal(width, 30)
                    await pilot.pause()
                    self.assertEqual(summary.max_scroll_x, 0)
                    self.assertGreater(summary.max_scroll_y, 0)
                    for path in (self.report, self.log):
                        summary.move_cursor(row=0)
                        await pilot.pause()
                        await self.tooltip_and_click(app, pilot, summary, path, editor)
                        self.assertTrue(all(row.height == 1 for row in summary.rows.values()))
                summary.move_cursor(row=8)
                await pilot.resize_terminal(100, 30)
                self.assertEqual(summary.cursor_row, 8)
                column = list(summary.columns)[1]
                row = list(summary.rows)[1]
                summary.update_cell(row, column, app.literal('Long detail ' * 30 + 'end-of-detail'), update_width=True)
                summary.move_cursor(row=0)
                await pilot.pause()
                self.assertGreater(summary.max_scroll_x, 0)
                for path in (self.report, self.log):
                    offset, text = self.position(summary, path)
                    self.assertLessEqual(offset[0] + cell_len(text), summary.scrollable_content_region.width)
                summary.scroll_to(x=summary.max_scroll_x, animate=False)
                await pilot.pause()
                rendered = ''.join(strip.text for strip in summary.render_lines(Region(0, 0, *summary.size)))
                self.assertIn('end-of-detail', rendered)

    async def test_deleted_targets_notify_without_opening_an_editor(self):
        app = self.app('zed')
        async with app.run_test() as pilot:
            self.report.unlink()
            with patch.object(app, 'open_url') as opener, patch.object(app, 'notify') as notify:
                app.action_open_path(str(self.report), 'zed')
                opener.assert_not_called()
                self.assertIn(str(self.report), notify.call_args.args[0])
                self.assertEqual(notify.call_args.kwargs['severity'], 'warning')


if __name__ == '__main__':
    unittest.main()
