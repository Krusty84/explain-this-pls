#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Replay selected saved attempts without invoking a backend or reading sources."""
import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.runtime.tool_observer import CompletedTool, ToolObserver


def replay(path, backend, *, context=None):
    observer = ToolObserver(backend, clock=lambda: 0.0, context=context)
    fragments = (context or {}).get('source_context', {}).get('fragments', [])
    if fragments:
        observer.seed_prompt(fragments)
    timing, native_start, records = 'unavailable', None, 0
    missing_native_time = False
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            observer.feed(line + '\n')
            continue
        records += 1
        if isinstance(event, dict) and event.get('format') == 'tool-observer-v1':
            raw = event.get('call')
            timestamp = event.get('observed_at_seconds')
            if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
                observer._invalid('Malformed normalized replay record.')
                continue
            timing = 'monotonic-recorded'
            kind = event.get('kind')
            if kind == 'prompt':
                if not fragments:
                    observer.seed_prompt(event.get('fragments', []))
            elif kind == 'invalid':
                observer._invalid(str(event.get('reason', 'Invalid native event.')))
            elif kind == 'terminal':
                observer.terminal = True
            elif kind == 'model_request' and isinstance(event.get('identity'), list):
                observer._request(tuple(event['identity']))
            elif kind == 'pending' and isinstance(event.get('call_id'), str):
                observer.pending[(event.get('session_id', ''), event['call_id'])] = ('unknown', None)
            elif isinstance(raw, dict):
                try:
                    observer.observe(CompletedTool(**raw), observed_at=timestamp)
                except (TypeError, ValueError):
                    observer._invalid('Malformed normalized tool result.')
            else:
                observer._invalid('Unknown normalized replay record.')
        elif isinstance(event, dict):
            # OpenCode CLI timestamps are epoch milliseconds. They are not
            # monotonic; replay explicitly reports that weaker clock evidence.
            timestamp = event.get('timestamp')
            native_timed = backend in ('opencode', 'xxx') and type(timestamp) in (int, float) and math.isfinite(timestamp)
            if native_timed:
                if native_start is None:
                    native_start = timestamp
                timing = 'native-wall-clock'
                elapsed = (timestamp - native_start) / 1000
                observer.clock = lambda: elapsed
            elif backend in ('opencode', 'xxx'):
                observer.clock = lambda: 0.0
            completed_before = observer.counts['calls']
            try:
                observer.event(event)
            except (TypeError, ValueError):
                observer._invalid('Malformed native replay event.')
            if not native_timed and backend in ('opencode', 'xxx') and observer.counts['calls'] > completed_before:
                observer._invalid('Completed native tool result has no reliable observation timestamp.')
                missing_native_time = True
        else:
            observer._invalid('Malformed native replay event.')
        if observer.triggered:
            break
    metrics = observer.finish()
    if timing == 'unavailable':
        metrics['observation']['limitations'].append('No per-call timestamps; stop time cannot be established.')
        metrics['observation']['coverage'] = 'partial' if metrics['calls'] else 'unavailable'
    elif timing == 'native-wall-clock':
        metrics['observation']['limitations'].append('Native wall-clock timestamps are weaker than recorded monotonic times.')
        metrics['observation']['coverage'] = 'partial'
        if missing_native_time:
            timing = 'native-wall-clock-partial'
    return {'log': str(path), 'backend': backend, 'records_processed': records, 'timing': timing,
            'possible_stop': metrics['guard']['diagnostic'], 'metrics': metrics}


def markdown(report):
    rows = ['# Tool observation replay', '', 'No live model calls were made.', '',
            '| Attempt | Backend | Calls | Possible stop | Repeat streak | Seconds | Timing |',
            '|---|---|---:|---:|---:|---:|---|']
    for attempt in report['attempts']:
        stop, metrics = attempt['possible_stop'], attempt['metrics']
        label = attempt['log'].replace('|', '\\|')
        rows.append(f"| {label} | {attempt['backend']} | {metrics['calls']} | "
                    f"{stop['call'] if stop else 'unavailable' if attempt['timing'] == 'unavailable' else 'none'} | "
                    f"{metrics['max_repeat_streak']} | {metrics['max_repeat_duration_seconds']} | {attempt['timing']} |")
    rows += ['', '## Observation limits', '']
    for attempt in report['attempts']:
        rows.extend(f"- {attempt['log']}: {reason}" for reason in attempt['metrics']['observation']['limitations'])
    return '\n'.join(rows) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs', type=Path, help='Directory containing saved attempt logs.')
    parser.add_argument('--attempt', action='append', required=True,
                        help='Relative attempt directory or log file; repeat to select more attempts.')
    parser.add_argument('--backend', choices=('codex', 'claude-code', 'opencode', 'xxx'), required=True)
    parser.add_argument('--context', type=Path, help='Optional saved source context for native path ownership.')
    parser.add_argument('--output', type=Path, required=True, help='Output prefix; writes .json and .md.')
    args = parser.parse_args(argv)
    context = json.loads(args.context.read_text(encoding='utf-8')) if args.context else None
    attempts = []
    root = args.logs.resolve()
    for selected in args.attempt:
        path = (root / selected).resolve()
        if not path.is_relative_to(root):
            parser.error('--attempt must remain inside the logs directory.')
        if path.is_dir():
            normalized = path / 'tool-events.jsonl'
            path = normalized if normalized.exists() else path / 'stdout.log'
        if not path.is_file():
            parser.error('Selected attempt log does not exist: ' + selected)
        attempts.append(replay(path, args.backend, context=context))
    report = {'format': 'tool-observer-replay-v1', 'live_executed': False, 'attempts': attempts}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    args.output.with_suffix('.md').write_text(markdown(report), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
