#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Command-line entry point."""
import contextlib
import os
import sys

from src.cli import main


if __name__ == '__main__':
    code = main()
    # Broken redirected streams must not change the exit code during interpreter shutdown.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            with contextlib.suppress(OSError, ValueError), open(os.devnull, 'w') as sink:
                os.dup2(sink.fileno(), stream.fileno())
    raise SystemExit(code)
