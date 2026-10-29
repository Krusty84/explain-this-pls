# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Account quota labels shared by Models and the stage table."""
import datetime as dt

from src.messages import message as ui_message


def limit_rows(snapshot, *, minutes=None, compact=False):
    rows = []
    for window in (snapshot or {}).get('windows', []):
        if minutes is not None and window['window_minutes'] != minutes:
            continue
        identifier, name = window['limit_id'], window['limit_name']
        label = f'{name} ({identifier})' if name and name != identifier else identifier
        duration = window['window_minutes']
        period = (f'{duration // 1440}d' if duration % 1440 == 0 else
                  f'{duration // 60}h' if duration % 60 == 0 else f'{duration}m') if duration else window['window']
        used = window['used_percent']
        remaining = f'{max(0, 100 - used):g}%' if used is not None else ui_message('tui.n_a')
        resets = ui_message('tui.n_a')
        if window['resets_at'] is not None:
            try:
                value = dt.datetime.fromtimestamp(window['resets_at'], dt.timezone.utc).astimezone()
                resets = value.strftime('%d.%m %H:%M' if compact else '%Y-%m-%d %H:%M')
            except (OverflowError, OSError, ValueError):
                pass
        rows.append((label + ' / ' + period, remaining, resets))
    return rows
