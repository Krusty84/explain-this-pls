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
from src.messages import message as ui_message
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
        raise KeyboardInterrupt(ui_message('cli.received_signal', signum=signum))

    try:
        signal.signal(signal.SIGTERM, interrupted)
        config = load_config(args.config)
        groups = {}
        for stage, agent in config['_agents'].items():
            if agent['backend'] == 'codex':
                group = groups.setdefault(agent['executable'], {'executable': agent['executable'], 'stages': []})
                group['stages'].append(stage)
        if not groups:
            raise AuditError(ui_message('cli.the_configuration_has_no_active_codex_agents'))

        async def fetch():
            return [dict(group, models=await codex_models.catalog(group['executable']))
                    for group in groups.values()]

        catalogs = asyncio.run(fetch())
        if reporter.mode == 'json':
            reporter.stdout.write(json.dumps({'catalogs': catalogs}, ensure_ascii=False, indent=2) + '\n')
        else:
            lines = [ui_message('cli.codex_model_catalogs_account_access_has_not_been')]
            for group in catalogs:
                lines += ['', ui_message(
                    'cli.stages',
                    executable=reporter.display(group['executable']),
                    value=', '.join(group['stages']),
                )]
                rows = [[ui_message('cli.model'), ui_message('cli.name'), ui_message('cli.visibility'), ui_message('cli.supported_in_api')]]
                rows += [[reporter.display(model[key]) for key in ('slug', 'display_name', 'visibility')]
                         + [ui_message(
                             'cli.unknown',
                         ) if model['supported_in_api'] is None else str(model['supported_in_api']).lower()]
                         for model in group['models']]
                widths = [max(len(row[index]) for row in rows) for index in range(4)]
                lines += ['  '.join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows]
                if not group['models']:
                    lines.append(ui_message('cli.no_models_returned'))
            reporter.stdout.write('\n'.join(lines) + '\n')
        return 0
    except KeyboardInterrupt:
        reporter.stderr.write(ui_message('cli.model_catalog_request_interrupted'))
        return 130
    except (OSError, ValueError, RuntimeError, ContractError) as exc:
        reporter.stderr.write(ui_message('cli.cannot_list_codex_models', exc=reporter.display(exc)))
        return 1
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        reporter.close()

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=ui_message('cli.description'), epilog=
        ui_message('cli.in_command_line_mode_stdout_contains_one_result'))
    parser.add_argument('--config', type=Path, help=ui_message('cli.path_to_a_json_or_jsonc_configuration_required'))
    parser.add_argument('--tui', action='store_true', help=ui_message('cli.open_the_interactive_config_picker_and_dashboard_also'))
    parser.add_argument('--check', action='store_true', help=ui_message(
        'cli.check_configuration_source_state_and_cli_capabilities_no',
    ))
    parser.add_argument('--models', action='store_true', help=ui_message('cli.list_codex_model_catalogs_for_the_configured_active'))
    parser.add_argument('--trust-repository', action='store_true', help=ui_message(
        'cli.trust_only_the_configured_git_checkout_and_verified',
    ))
    parser.add_argument('--output', choices=('auto', 'text', 'json'), default='auto',
                        help=ui_message('cli.result_format_on_stdout_text_is_human_readable'))
    parser.add_argument('--verbose', action='store_true', help=ui_message('cli.add_technical_event_details_to_stderr_never_print'))
    parser.add_argument('--no-progress', action='store_true', help=ui_message(
        'cli.disable_the_spinner_and_periodic_waiting_messages_keep',
    ))
    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    if args.models and (args.check or args.tui):
        parser.error(ui_message('cli.models_cannot_be_combined_with_check_or_tui'))
    if not argv or args.tui:
        if args.output != 'auto':
            parser.error(ui_message('cli.tui_cannot_be_combined_with_output_text_or'))
        if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get('TERM') == 'dumb':
            parser.error(ui_message('cli.the_tui_needs_an_interactive_terminal_use_config'))
        try:
            from src.tui.app import launch
        except ModuleNotFoundError as exc:
            if exc.name not in ('textual', 'rich'):
                raise
            parser.error(ui_message('cli.install_the_required_tui_dependency_with_python_m', value=str(ROOT / 'requirements.txt')))
        return launch(args)
    if args.config is None:
        parser.error(ui_message('cli.config_is_required_unless_using_tui'))
    reporter = Reporter(mode=output_mode(args.output, sys.stdout), verbose=args.verbose, progress=not args.no_progress)
    if args.models:
        return run_models(args, reporter)
    return run_audit(args, reporter)
