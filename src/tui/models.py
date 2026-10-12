# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Explicit catalog loading, model access checks and config updates."""
import asyncio

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Select, Static

from src.runtime.config import load_config, resolve_agent
from src.backends import codex_models
from src.contracts.contracts import ContractError
from src.tui.config_edit import save_model


def model_targets(config, path):
    targets = {}
    if config['agent'].get('backend') == 'codex':
        targets['agent.model'] = resolve_agent(config['agent'], path)
    targets.update({'stage_agents.' + stage + '.model': agent for stage, agent in config['_agents'].items()
                    if agent['backend'] == 'codex'})
    return targets


class ModelChooser(ModalScreen):
    BINDINGS = [('escape', 'close', 'Cancel')]
    DEFAULT_CSS = """
    ModelChooser { align: center middle; }
    #model-dialog { width: 95%; height: 90%; max-width: 120; border: round $accent; background: $surface; padding: 0 1; }
    #model-file, #model-current { height: auto; max-height: 2; }
    #model-table { height: 1fr; min-height: 3; }
    #model-status { height: 3; }
    #model-actions { height: 3; }
    #model-actions Button { min-width: 8; margin-right: 1; }
    """

    def __init__(self, path, config, original):
        super().__init__()
        self.path, self.original = path.resolve(), original
        self.targets = model_targets(config, self.path)
        if not self.targets:
            raise ValueError('The configuration has no Codex model fields.')
        self.target = next(iter(self.targets))
        self.models, self.verifications = [], {}
        self.selected = None
        self.busy, self.generation = False, 0
        self.tasks = set()

    def literal(self, value):
        return Text(self.app.presenter.display(value))

    def compose(self):
        with Vertical(id='model-dialog'):
            yield Static(self.literal('File: ' + str(self.path)), id='model-file')
            yield Select([(field, field) for field in self.targets], value=self.target,
                         allow_blank=False, id='model-target')
            yield Static('', id='model-current')
            yield DataTable(id='model-table', cursor_type='row', zebra_stripes=True)
            yield Static('', id='model-status')
            with Horizontal(id='model-actions'):
                yield Button('Check model', id='model-check', disabled=True)
                yield Button('Apply', id='model-apply', variant='primary', disabled=True)
                yield Button('Refresh', id='model-refresh')
                yield Button('Cancel', id='model-close')

    def on_mount(self):
        self.query_one('#model-table', DataTable).add_columns('Model', 'Name', 'Visibility', 'API', 'Access')
        self.load_catalog()

    def update_buttons(self):
        self.query_one('#model-table', DataTable).disabled = self.busy
        self.query_one('#model-check', Button).disabled = self.busy or self.selected is None
        self.query_one('#model-apply', Button).disabled = (
            self.busy or self.verifications.get(self.selected, {}).get('status') != 'available')

    def cancel_operations(self):
        self.generation += 1
        for task in tuple(self.tasks):
            if not task.cancelling():
                task.cancel()

    def begin(self, operation):
        self.cancel_operations()
        self.busy = True
        self.update_buttons()
        task = asyncio.create_task(operation(self.generation))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def show_error(self, exc):
        self.query_one('#model-status', Static).update(self.literal(exc))
        self.update_buttons()

    def load_catalog(self):
        self.models, self.verifications, self.selected = [], {}, None
        self.query_one('#model-table', DataTable).clear()
        agent = self.targets[self.target]
        self.query_one('#model-current', Static).update(self.literal(
            'Current: ' + (agent.get('model') or 'CLI default') + ' | Executable: ' + agent['executable']))
        self.query_one('#model-status', Static).update('Loading Codex model catalog…')
        self.begin(self.fetch_catalog)

    async def fetch_catalog(self, generation):
        agent = self.targets[self.target]
        try:
            models = await codex_models.catalog(agent['executable'])
        except (OSError, ValueError, RuntimeError, ContractError) as exc:
            if generation == self.generation:
                self.busy = False
                self.show_error(exc)
            return
        if generation != self.generation:
            return
        self.models, self.busy = models, False
        table = self.query_one('#model-table', DataTable)
        for model in models:
            api = 'unknown' if model['supported_in_api'] is None else str(model['supported_in_api']).lower()
            table.add_row(*(self.literal(model[key]) for key in ('slug', 'display_name', 'visibility')),
                          api, 'Not checked', key=model['slug'])
        if models:
            index = next((i for i, model in enumerate(models) if model['slug'] == agent.get('model')), 0)
            table.move_cursor(row=index)
            self.select_model(models[index]['slug'])
            table.focus()
        else:
            self.query_one('#model-status', Static).update('No models returned.')
            self.update_buttons()

    def select_model(self, slug):
        self.selected = slug
        result = self.verifications.get(slug)
        self.query_one('#model-status', Static).update(self.literal(
            result['message'] if result else 'Not checked. Check model sends a request and uses your Codex allowance.'))
        self.update_buttons()

    def on_data_table_row_highlighted(self, event):
        if event.data_table.id == 'model-table' and any(model['slug'] == event.row_key.value for model in self.models):
            self.select_model(event.row_key.value)

    def on_select_changed(self, event):
        if event.select.id == 'model-target' and event.value in self.targets and event.value != self.target:
            self.target = event.value
            self.load_catalog()

    async def verify_model(self, generation):
        slug, executable = self.selected, self.targets[self.target]['executable']
        result = await codex_models.check_model(executable, slug)
        if generation != self.generation:
            return
        self.busy = False
        self.verifications[slug] = result
        table = self.query_one('#model-table', DataTable)
        table.update_cell(slug, list(table.columns)[-1], {
            'available': 'Available', 'unavailable': 'Unavailable', 'unverified': 'Not verified'}[result['status']])
        self.select_model(slug)

    def refresh_models(self):
        self.cancel_operations()
        self.verifications, self.selected, self.busy = {}, None, False
        self.models = []
        self.query_one('#model-table', DataTable).clear()
        try:
            original = self.path.read_bytes()
            targets = model_targets(load_config(self.path), self.path)
            if not targets:
                raise ValueError('The configuration has no Codex model fields.')
        except (OSError, ValueError, RuntimeError, ContractError) as exc:
            self.show_error(exc)
            return
        self.original, self.targets = original, targets
        self.target = self.target if self.target in targets else next(iter(targets))
        select = self.query_one('#model-target', Select)
        select.set_options([(field, field) for field in targets])
        select.value = self.target
        self.load_catalog()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'model-close':
            self.action_close()
        elif event.button.id == 'model-refresh':
            self.refresh_models()
        elif event.button.id == 'model-check' and self.selected is not None and not self.busy:
            self.verifications.pop(self.selected, None)
            table = self.query_one('#model-table', DataTable)
            table.update_cell(self.selected, list(table.columns)[-1], 'Checking…')
            self.query_one('#model-status', Static).update('Checking model access with ChatGPT…')
            self.begin(self.verify_model)
        elif event.button.id == 'model-apply' and not self.busy:
            if self.verifications.get(self.selected, {}).get('status') != 'available':
                return
            try:
                save_model(self.path, self.target, self.selected, self.original)
            except (OSError, ValueError, ContractError) as exc:
                self.verifications.clear()
                self.show_error(exc)
            else:
                self.dismiss(True)

    def action_close(self):
        self.cancel_operations()
        self.dismiss(False)

    async def on_unmount(self):
        self.cancel_operations()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
