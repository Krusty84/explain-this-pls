# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Compare a frozen study; model calls require the explicit --live flag."""
import argparse
from pathlib import Path

from src.runtime.context_comparison import apply_assessments, read_json, run_comparison, save_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--baseline', type=Path, help='Directory with frozen inventory and coverage plan.')
    parser.add_argument('--facts', type=Path, help='Preregistered facts and source evidence JSON.')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--live', action='store_true', help='Explicitly permit model calls for all three variants.')
    parser.add_argument('--summarize', type=Path, help='Existing comparison.json; does not call models.')
    parser.add_argument('--assessments', type=Path, help='Human fact judgments to add when summarizing.')
    args = parser.parse_args(argv)
    if args.summarize:
        if args.live or any((args.config, args.baseline, args.facts, args.output)):
            parser.error('--summarize cannot be combined with experiment options.')
        report = read_json(args.summarize)
        if args.assessments:
            report = apply_assessments(report, read_json(args.assessments))
        save_report(args.summarize.resolve().parent, report)
    else:
        if not all((args.config, args.baseline, args.facts, args.output)) or args.assessments:
            parser.error('An experiment requires --config, --baseline, --facts and --output.')
        from src.runtime.config import load_config
        report = run_comparison(load_config(args.config), args.baseline, read_json(args.facts), args.output,
                                repetitions=args.repetitions, live=args.live)
    print(report['status'])
    return 0 if report['status'] in ('planned', 'complete') else 1


if __name__ == '__main__':
    raise SystemExit(main())
