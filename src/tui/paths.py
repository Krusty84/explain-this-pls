# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Single-line local paths with their full click targets."""
from pathlib import Path

from rich.cells import cell_len, get_character_cell_size
from rich.measure import Measurement
from rich.text import Text
from textual.widgets import Static

from src.runtime.reporting import safe_text


class PathTooltip:
    def on_mouse_move(self, event):
        path = event.style.meta.get('path')
        self.tooltip = Text(safe_text(path)) if path is not None else None

    def on_leave(self):
        self.tooltip = None


class PathStatic(PathTooltip, Static):
    pass


def _take_cells(value, width, *, reverse=False):
    chars = []
    for char in reversed(value) if reverse else value:
        width -= get_character_cell_size(char)
        if width < 0:
            break
        chars.append(char)
    return ''.join(reversed(chars) if reverse else chars)


def compact_path(value, width):
    if width <= 0:
        return ''
    if cell_len(value) <= width:
        return value
    if width <= 3:
        return '.' * width
    available = width - 3
    name = value.rsplit('/', 1)[-1]
    if cell_len(name) > available:
        extension = Path(name).suffix
        tail = (extension if extension and cell_len(extension) <= available else
                _take_cells(name, available // 2, reverse=True))
        return _take_cells(name, available - cell_len(tail)) + '...' + tail
    tail_width = min(available, max(cell_len(name) + 1, (available + 1) // 2))
    head = _take_cells(value, available - tail_width)
    tail = _take_cells(value, available - cell_len(head), reverse=True)
    return head + '...' + tail


class PathText:
    def __init__(self, path, editor, *, prefix=''):
        self.path, self.editor, self.prefix = str(path), editor, prefix
        self.max_width = None

    @property
    def plain(self):
        return self.prefix + safe_text(self.path)

    def __str__(self):
        return self.plain

    def __rich_measure__(self, console, options):
        return Measurement(0, min(options.max_width, self.max_width or options.max_width))

    def __rich_console__(self, console, options):
        heading, separator, label = self.prefix.rpartition('\n')
        if separator:
            yield Text(heading)
        width = max(0, min(options.max_width, self.max_width or options.max_width) - cell_len(label))
        path = Text(compact_path(safe_text(self.path), width), style='underline')
        path.on(meta={'path': self.path}, click=f'app.open_path({self.path!r}, {self.editor!r})')
        yield Text.assemble(label, path, no_wrap=True, overflow='crop')
