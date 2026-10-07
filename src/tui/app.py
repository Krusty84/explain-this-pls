# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Config selection and a persistent table of audit results."""
from argparse import Namespace
import asyncio
import json
from pathlib import Path
import signal

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, DirectoryTree, Footer, Header, Input, Static, TabbedContent, TabPane

from explain import ROOT, load_config
from src.contracts.contracts import ContractError
from src.runtime.reporting import Reporter, diagnostic, duration, metric_value, safe_text
from src.tui.settings import config_directory, config_files, read_settings, save_settings
from src.tui.state import RunState
from src.tui.worker import AuditProcess


class FolderTree(DirectoryTree):
    def filter_paths(self, paths):
        return [path for path in paths if path.is_dir()]

    def render_label(self, node, base_style, style):
        label = super().render_label(node, base_style, style)
        return Text(safe_text(label.plain), style=label.style)


class FolderChooser(ModalScreen):
    BINDINGS = [('escape', 'dismiss(None)', 'Cancel')]
    DEFAULT_CSS = """
    FolderChooser { align: center middle; }
    #folder-dialog { width: 90%; height: 90%; border: round $accent; background: $surface; padding: 1; }
    #folder-message { height: auto; max-height: 4; }
    #folder-tree { height: 1fr; }
    #folder-actions { height: 3; }
    #folder-actions Button { min-width: 10; margin-right: 1; }
    """

    def __init__(self, settings_path, directory, message='Choose the folder containing your JSON/JSONC configs.'):
        super().__init__()
        self.settings_path, self.directory, self.message = settings_path, directory, message

    def compose(self) -> ComposeResult:
        with Vertical(id='folder-dialog'):
            yield Static(Text(self.message), id='folder-message')
            yield Input(str(self.directory), placeholder='Config folder path', id='folder-path')
            yield FolderTree(self.directory, id='folder-tree')
            with Horizontal(id='folder-actions'):
                yield Button('Up', id='folder-up')
                yield Button('Go', id='folder-go')
                yield Button('Use folder', variant='primary', id='folder-save')
                yield Button('Cancel', id='folder-cancel')

    def on_directory_tree_directory_selected(self, event):
        self.query_one('#folder-path', Input).value = str(event.path)

    def on_input_submitted(self, event):
        self.navigate()

    def navigate(self, parent=False):
        try:
            path = Path(self.query_one('#folder-path', Input).value).expanduser()
            self.directory = config_directory(path.parent if parent else path)
            self.query_one('#folder-path', Input).value = str(self.directory)
            self.query_one('#folder-tree', DirectoryTree).path = self.directory
            self.query_one('#folder-message', Static).update('Select a folder, then choose Use folder.')
        except (OSError, ValueError) as exc:
            self.show_error(exc)

    def show_error(self, exc):
        self.query_one('#folder-message', Static).update(Text(self.app.presenter.display(exc), style='red'))

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'folder-cancel':
            self.dismiss(None)
        elif event.button.id in ('folder-up', 'folder-go'):
            self.navigate(parent=event.button.id == 'folder-up')
        elif event.button.id == 'folder-save':
            try:
                directory = save_settings(self.settings_path, self.query_one('#folder-path', Input).value)
            except (OSError, ValueError) as exc:
                self.show_error(exc)
            else:
                self.dismiss(directory)


class WorkerMessage(Message):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload


class ExplainApp(App[int]):
    TITLE = 'explain-this-pls'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding('ctrl+c', 'cancel', 'Cancel', priority=True),
                Binding('q,ctrl+q', 'quit', 'Quit')]
    CSS = """
    #picker, #dashboard { height: 1fr; padding: 0 1; }
    #dashboard { display: none; }
    #config-location, #run-status { height: auto; max-height: 3; margin: 1 0; }
    #configs { height: 1fr; min-height: 4; }
    #preview-scroll { height: 8; border-top: solid $primary; }
    #config-preview, #step-details { height: auto; }
    .actions { height: 3; margin-top: 1; }
    .actions Button { min-width: 8; margin-right: 1; }
    #run-tabs { height: 1fr; }
    TabPane { padding: 0; }
    #stages, #summary { height: 1fr; min-height: 3; }
    #detail-scroll { height: 8; border-top: solid $primary; }
    """

    def __init__(self, args, *, settings_path=None):
        super().__init__()
        self.args = args
        self.settings_path = settings_path or ROOT / 'explain.config'
        self.presenter = Reporter(progress=False)
        self.directory = None
        self.selected = args.config.resolve() if args.config else None
        self.explicit_config = self.selected
        self.configs = {}
        self.state = None
        self.audit = None
        self.running = False
        self.pending_result = None
        self.exit_when_finished = False
        self.last_exit_code = 0

    def literal(self, value):
        return Text(self.presenter.display(value))

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id='picker'):
            yield Static('', id='config-location')
            yield DataTable(id='configs', cursor_type='row', zebra_stripes=True)
            with VerticalScroll(id='preview-scroll'):
                yield Static('Select a configuration.', id='config-preview')
            with Horizontal(classes='actions'):
                yield Button('Run', id='run', variant='primary', disabled=True)
                yield Button('Check setup', id='check', disabled=True)
                yield Button('Refresh', id='refresh')
                yield Button('Change folder', id='change-folder')
                yield Button('Quit', id='quit-picker')
        with Vertical(id='dashboard'):
            yield Static('Preparing analysis…', id='run-status')
            with TabbedContent(id='run-tabs'):
                with TabPane('Steps', id='steps-tab'):
                    yield DataTable(id='stages', cursor_type='row', zebra_stripes=True, fixed_columns=2)
                    with VerticalScroll(id='detail-scroll'):
                        yield Static('', id='step-details')
                with TabPane('Final result', id='summary-tab'):
                    yield DataTable(id='summary', cursor_type='row')
            with Horizontal(classes='actions'):
                yield Button('Cancel', id='cancel-run', variant='warning')
                yield Button('Configs', id='back', disabled=True)
                yield Button('Quit', id='quit-run')
        yield Footer()

    def on_mount(self):
        self.query_one('#run', Button).display = not self.args.check
        self.query_one('#configs', DataTable).add_columns('Config', 'Mode', 'Source / validation')
        table = self.query_one('#stages', DataTable)
        for name, width in (('Source', 20), ('Step/revision', 18), ('Status', 12), ('Elapsed', 8),
                            ('Attempts', 8), ('Tokens', 22), ('Estimated cost', 22), ('Result', 55)):
            table.add_column(name, width=width)
        self.query_one('#summary', DataTable).add_columns('Item', 'Result')
        self.query_one('#run-tabs', TabbedContent).disable_tab('summary-tab')
        if not self.args.no_progress:
            self.set_interval(1, self.tick)
        if self.explicit_config:
            self.refresh_configs()
        else:
            try:
                self.directory = read_settings(self.settings_path)
            except FileNotFoundError:
                self.choose_folder()
            except (OSError, ValueError) as exc:
                self.choose_folder('Cannot use saved settings: ' + self.presenter.display(exc))
            else:
                self.refresh_configs()

    def choose_folder(self, message=None):
        self.push_screen(FolderChooser(self.settings_path, self.directory or Path.cwd(),
                                      message or 'Choose the folder containing your JSON/JSONC configs.'), self.folder_chosen)

    def folder_chosen(self, directory):
        if directory is None:
            if self.directory is None and self.explicit_config is None:
                self.exit(self.last_exit_code)
            return
        self.directory, self.explicit_config = directory, None
        self.refresh_configs()

    def refresh_configs(self):
        self.configs.clear()
        table = self.query_one('#configs', DataTable)
        table.clear()
        try:
            paths = [self.explicit_config] if self.explicit_config else config_files(self.directory)
        except OSError as exc:
            self.choose_folder('Cannot read the config folder: ' + self.presenter.display(exc))
            return
        self.query_one('#config-location', Static).update(self.literal(
            'Config: ' + str(self.explicit_config) if self.explicit_config else 'Config folder: ' + str(self.directory)))
        for path in paths:
            try:
                config = load_config(path)
                mode = config['mode']
                source = config[mode + '_mode']['path' if mode == 'folder' else 'repository']
                error = ''
            except (OSError, ValueError, RuntimeError, ContractError) as exc:
                config, mode, source = None, 'Invalid', self.presenter.display(diagnostic(exc).message)
                error = source
            self.configs[str(path)] = (path, config, error)
            table.add_row(self.literal(path.name), self.literal(mode), self.literal(source), key=str(path))
        selected = str(self.selected) if str(self.selected) in self.configs else next(iter(self.configs), None)
        if selected:
            table.move_cursor(row=list(self.configs).index(selected))
            self.select_config(selected)
        else:
            self.selected = None
            self.query_one('#config-preview', Static).update(
                'No configs found. Copy an example config into this folder, edit its paths, then Refresh.')
            self.enable_start(False)
        table.focus()

    def enable_start(self, enabled):
        for name in ('run', 'check'):
            self.query_one('#' + name, Button).disabled = not enabled

    def select_config(self, key):
        if key not in self.configs:
            return
        self.selected, config, error = self.configs[key]
        self.enable_start(config is not None)
        s = self.presenter.display
        lines = ['File: ' + s(self.selected)]
        if config is None:
            lines += ['Cannot start: ' + error]
        else:
            source = config[config['mode'] + '_mode']
            lines += ['Mode: ' + s(config['mode']), 'Source: ' + s(source.get('path', source.get('repository')))]
            if config['mode'] == 'git':
                lines += ['Branches: ' + s(', '.join(source['branches'])) + ' | Baseline: ' + s(source['baseline_branch'])]
            agents = '; '.join(stage + ': ' + agent['backend'] + ' / ' + (agent.get('model') or 'CLI default')
                               for stage, agent in config['_agents'].items())
            lines += ['Agents: ' + s(agents), 'Review: ' + ('enabled' if config['execution']['review_enabled'] else 'disabled'),
                      'Reports: ' + s(config['reports_dir'])]
        self.query_one('#config-preview', Static).update(Text('\n'.join(lines)))

    def on_data_table_row_highlighted(self, event):
        if event.data_table.id == 'configs':
            self.select_config(event.row_key.value)
        elif event.data_table.id == 'stages':
            self.show_step_details()

    def on_button_pressed(self, event):
        name = event.button.id
        if name in ('run', 'check'):
            self.start_audit(check_only=name == 'check' or self.args.check)
        elif name == 'refresh':
            self.refresh_configs()
        elif name == 'change-folder':
            self.choose_folder()
        elif name == 'back' and not self.running:
            self.query_one('#dashboard').display = False
            self.query_one('#picker').display = True
            self.refresh_configs()
        elif name == 'cancel-run':
            self.action_cancel()
        elif name in ('quit-picker', 'quit-run'):
            self.action_quit()

    def start_audit(self, *, check_only=False):
        if self.running or self.selected is None:
            return
        # Revalidate changes made in an external editor since the last selection.
        try:
            config, error = load_config(self.selected), ''
        except (OSError, ValueError, RuntimeError, ContractError) as exc:
            config, error = None, self.presenter.display(diagnostic(exc).message)
        self.configs[str(self.selected)] = (self.selected, config, error)
        self.select_config(str(self.selected))
        if config is None:
            return
        args = Namespace(**(vars(self.args) | {'config': self.selected, 'check': check_only}))
        self.state, self.pending_result = RunState(), None
        self.query_one('#stages', DataTable).clear()
        self.query_one('#summary', DataTable).clear()
        tabs = self.query_one('#run-tabs', TabbedContent)
        tabs.active = 'steps-tab'
        tabs.disable_tab('summary-tab')
        self.query_one('#picker').display = False
        self.query_one('#dashboard').display = True
        self.query_one('#back', Button).disabled = True
        self.query_one('#cancel-run', Button).disabled = False
        self.query_one('#run-status', Static).update(self.literal('Preparing ' + self.selected.name + '…'))
        self.render_stages()
        self.query_one('#stages', DataTable).focus()
        self.audit = AuditProcess(args)
        try:
            self.audit.start()
        except (OSError, ValueError) as exc:
            self.complete({'type': 'result', 'result': {'status': 'FAILED', 'exit_code': 1},
                           'diagnostics': [{'message': 'Could not start analysis: ' + self.presenter.display(exc)}]})
            self.audit = None
            return
        self.running = True
        self.receive_audit(self.audit)

    @work(thread=True, exit_on_error=False)
    def receive_audit(self, audit):
        for payload in audit.messages():
            self.post_message(WorkerMessage(payload))

    def on_worker_message(self, message):
        payload = message.payload
        if payload['type'] == 'event':
            name, context = payload['event'], payload['context']
            self.state.apply(name, context)
            if name == 'stage_started':
                self.query_one('#run-status', Static).update(self.literal(
                    self.selected.name + ' · ' + str(context.get('source_name') or context.get('branch') or '') +
                    ' · ' + context['stage']))
            elif name == 'stop_requested':
                self.query_one('#run-status', Static).update('Stopping analysis; waiting for cleanup…')
            self.render_stages()
        elif payload['type'] == 'result':
            self.pending_result = payload
        elif payload['type'] == 'exit':
            self.running = False
            self.audit.close()
            self.audit = None
            result = self.pending_result
            if result is None or payload['exit_code'] != result['result']['exit_code']:
                result = {'result': {'status': 'FAILED', 'exit_code': 1},
                          'diagnostics': [{'message': 'The audit worker exited unexpectedly; inspect any saved run files.'}]}
            self.complete(result)

    def tick(self):
        if self.running:
            self.render_stages()

    def render_stages(self):
        table = self.query_one('#stages', DataTable)
        columns = list(table.columns)
        for index, row in enumerate(self.state.rows.values()):
            metrics = row['metrics']
            usage = metrics.get('usage', {})
            values = [row.get('source_name') or row.get('branch') or 'Run',
                      row['stage'] + (' #' + row['revision_id'] if row.get('revision_id') else '')
                      + (' ' + row['shard_id'] if row.get('shard_id') else ''),
                      row['status'], duration(self.state.elapsed(row)), metrics.get('attempts', '—'),
                      metric_value(usage, 'total_tokens'), metric_value(usage, 'cost_usd'), row['result']]
            cells = [self.literal(value) for value in values]
            cells[2].stylize({'COMPLETE': 'green', 'FAILED': 'red', 'PARTIAL': 'yellow', 'BLOCKED': 'yellow',
                              'INTERRUPTED': 'yellow', 'SKIPPED': 'dim', 'RUNNING': 'cyan'}.get(row['status'], ''))
            key = str(index)
            if index >= table.row_count:
                table.add_row(*cells, key=key)
            else:
                for column, cell in zip(columns, cells):
                    table.update_cell(key, column, cell)
        self.show_step_details()

    def diagnostic_lines(self, item):
        fields = ('message', 'hint')
        if self.args.verbose:
            fields += ('code', 'node_path', 'failure_kind', 'failure_layer', 'details')
        return [key.replace('_', ' ').title() + ': ' + self.presenter.display(
            json.dumps(item[key], ensure_ascii=False) if isinstance(item[key], dict) else item[key])
                for key in fields if item.get(key)]

    def show_step_details(self):
        if not self.state:
            return
        rows = list(self.state.rows.values())
        index = self.query_one('#stages', DataTable).cursor_row
        if index >= len(rows):
            return
        row, s = rows[index], self.presenter.display
        metrics = row['metrics']
        lines = ['Source: ' + s(row.get('source_name') or row.get('branch') or 'Run'),
                 'Step: ' + s(row['stage'] + (' #' + row['revision_id'] if row.get('revision_id') else '')
                              + (' ' + row['shard_id'] if row.get('shard_id') else '')),
                 'Result: ' + s(row['result']),
                 'Agent / model: ' + (self.presenter.metric_models(metrics) if metrics else s(row.get('backend') or 'Not reported yet')),
                 'Report: ' + s(row.get('report_path') or 'No report available')]
        if metrics:
            lines += self.presenter.metric_lines(metrics)
        for item in row['diagnostics']:
            lines += self.diagnostic_lines(item)
        self.query_one('#step-details', Static).update(Text('\n'.join(lines)))

    def complete(self, payload):
        self.pending_result = payload
        result = payload['result']
        self.last_exit_code = result['exit_code']
        self.state.finish(payload)
        self.render_stages()
        outcome = 'INTERRUPTED' if self.last_exit_code == 130 else result['status']
        self.query_one('#run-status', Static).update(self.literal(self.selected.name + ' · ' + outcome))
        metrics = result.get('metrics', {})
        values = [('Outcome', outcome), ('Meaning', result.get('status_meaning', 'See diagnostics.')),
                  ('Elapsed', duration(metrics['duration_seconds']) if metrics else 'unavailable'),
                  ('Attempts', metrics.get('attempts', 'unavailable')),
                  ('Tokens', metric_value(metrics.get('usage', {}), 'total_tokens')),
                  ('Estimated cost', metric_value(metrics.get('usage', {}), 'cost_usd')),
                  ('Review', 'Not performed (setup check).' if payload.get('check_only') else
                   'Enabled; see review results.' if result.get('review_enabled') else 'Disabled; no separate review performed.'),
                  *payload.get('paths', [])]
        values += [('Diagnostic ' + str(i + 1), '\n'.join(self.diagnostic_lines(item)))
                   for i, item in enumerate(self.state.diagnostics)]
        table = self.query_one('#summary', DataTable)
        table.clear()
        for label, value in values:
            table.add_row(self.literal(label), self.literal(value))
        tabs = self.query_one('#run-tabs', TabbedContent)
        tabs.enable_tab('summary-tab')
        self.query_one('#cancel-run', Button).disabled = True
        self.query_one('#back', Button).disabled = False
        # Enabling a tab also enables its pane through a queued Textual message.
        # Wait for that transition before moving focus out of the steps table.
        self.call_after_refresh(self.show_summary)
        if self.exit_when_finished:
            self.exit(self.last_exit_code)

    def show_summary(self):
        self.query_one('#run-tabs', TabbedContent).active = 'summary-tab'
        self.call_after_refresh(self.query_one('#summary', DataTable).focus)

    def action_cancel(self):
        if not self.running:
            self.exit(self.last_exit_code)
            return
        self.audit.cancel()
        self.query_one('#cancel-run', Button).disabled = True
        self.query_one('#run-status', Static).update('Stopping analysis; waiting for cleanup…')

    def action_quit(self):
        if self.running:
            self.exit_when_finished = True
            self.action_cancel()
        else:
            self.exit(self.last_exit_code)

    async def on_unmount(self):
        if self.audit is not None and self.running:
            self.audit.cancel()
            await asyncio.to_thread(self.audit.process.join)
        self.presenter.close()


def launch(args):
    app = ExplainApp(args)
    old_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda signum, frame: app.call_later(app.action_quit))
    try:
        return app.run() or 0
    finally:
        signal.signal(signal.SIGTERM, old_handler)
