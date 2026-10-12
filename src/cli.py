# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Sequential architecture auditing with configured coding-agent CLIs.

macOS or Linux, Python 3.11+, and one or more authenticated coding-agent CLIs.
Git >= 2.34.1 is required only for git mode; folder mode needs no Git.
Textual provides the interactive interface. Git branches are checked out sequentially in place.
The parent publishes reports. The agent receives stdin and returns structured JSON."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from src.contracts.contracts import ContractError
from src.runtime.reporting import Reporter, output_mode
from src.backends import codex_models
from src.runtime.errors import AuditError
from src.runtime.config import ROOT, load_config
from src.runtime.audit import run_audit

def run_models(args, reporter) -> int:
    """List catalogs without entering the audit lifecycle or testing inference."""
    old_handler = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'Received signal {signum}')

    try:
        signal.signal(signal.SIGTERM, interrupted)
        config = load_config(args.config)
        groups = {}
        for stage, agent in config['_agents'].items():
            if agent['backend'] == 'codex':
                group = groups.setdefault(agent['executable'], {'executable': agent['executable'], 'stages': []})
                group['stages'].append(stage)
        if not groups:
            raise AuditError('The configuration has no active Codex agents.')

        async def fetch():
            return [dict(group, models=await codex_models.catalog(group['executable']))
                    for group in groups.values()]

        catalogs = asyncio.run(fetch())
        if reporter.mode == 'json':
            reporter.stdout.write(json.dumps({'catalogs': catalogs}, ensure_ascii=False, indent=2) + '\n')
        else:
            lines = ['Codex model catalogs. Account access has not been checked.']
            for group in catalogs:
                lines += ['', reporter.display(group['executable']) + ' | Stages: ' + ', '.join(group['stages'])]
                rows = [['Model', 'Name', 'Visibility', 'Supported in API']]
                rows += [[reporter.display(model[key]) for key in ('slug', 'display_name', 'visibility')]
                         + ['unknown' if model['supported_in_api'] is None else str(model['supported_in_api']).lower()]
                         for model in group['models']]
                widths = [max(len(row[index]) for row in rows) for index in range(4)]
                lines += ['  '.join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows]
                if not group['models']:
                    lines.append('No models returned.')
            reporter.stdout.write('\n'.join(lines) + '\n')
        return 0
    except KeyboardInterrupt:
        reporter.stderr.write('Model catalog request interrupted.\n')
        return 130
    except (OSError, ValueError, RuntimeError, ContractError) as exc:
        reporter.stderr.write('Cannot list Codex models: ' + reporter.display(exc) + '\n')
        return 1
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        reporter.close()

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=
        'In command-line mode, stdout contains one result. --help and argument syntax errors '
        'use standard argparse output without a run or manifest. Console messages are in English; '
        'output_language only controls generated reports. Labels and statuses are colored automatically '
        'when their output stream is a terminal, unless NO_COLOR is nonempty or TERM=dumb. '
        'JSON is never colored. Disable color with NO_COLOR=1.')
    parser.add_argument('--config', type=Path, help='Path to a JSON or JSONC configuration; required outside the TUI.')
    parser.add_argument('--tui', action='store_true', help='Open the interactive config picker and run dashboard (also the default without arguments).')
    parser.add_argument('--check', action='store_true', help='Check configuration, source state, and CLI capabilities; no branch switch or model calls.')
    parser.add_argument('--models', action='store_true', help='List Codex model catalogs for the configured active agents; no access probes or audit.')
    parser.add_argument('--trust-repository', action='store_true', help='Trust only the configured Git checkout and verified submodules despite an ownership mismatch (Git >= 2.34.1).')
    parser.add_argument('--output', choices=('auto', 'text', 'json'), default='auto',
                        help='Result format on stdout: text is human-readable; auto selects text for a TTY, JSON otherwise (default: auto). Diagnostics use stderr.')
    parser.add_argument('--verbose', action='store_true', help='Add technical event details to stderr; never print prompts, credentials or raw model output.')
    parser.add_argument('--no-progress', action='store_true', help='Disable the spinner and periodic waiting messages; keep stage boundaries, warnings and errors.')
    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    if args.models and (args.check or args.tui):
        parser.error('--models cannot be combined with --check or --tui.')
    if not argv or args.tui:
        if args.output != 'auto':
            parser.error('--tui cannot be combined with --output text or --output json.')
        if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get('TERM') == 'dumb':
            parser.error('The TUI needs an interactive terminal. Use --config FILE for command-line output.')
        try:
            from src.tui.app import launch
        except ModuleNotFoundError as exc:
            if exc.name not in ('textual', 'rich'):
                raise
            parser.error('Install the required TUI dependency with: python3 -m pip install -r ' + str(ROOT / 'requirements.txt'))
        return launch(args)
    if args.config is None:
        parser.error('--config is required unless using --tui.')
    reporter = Reporter(mode=output_mode(args.output, sys.stdout), verbose=args.verbose, progress=not args.no_progress)
    if args.models:
        return run_models(args, reporter)
    return run_audit(args, reporter)
