# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Synthetic CLI transport, including Codex's separate final-response file."""
import json
from pathlib import Path


def cli_result(command, data):
    output = data if isinstance(data, bytes) else json.dumps(data).encode()
    if '--output-last-message' in command:
        Path(command[command.index('--output-last-message') + 1]).write_bytes(output)
        events = [{'type': 'turn.started'}, {'type': 'item.completed', 'item': {
            'id': 'item_final', 'type': 'agent_message', 'text': output.decode()}},
            {'type': 'turn.completed', 'usage': {
            'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 20, 'reasoning_output_tokens': 5}}]
        output = ('\n'.join(json.dumps(event) for event in events) + '\n').encode()
    return {'returncode': 0, 'stdout': output, 'stderr': b''}
