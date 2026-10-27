# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Codex's model catalog and explicit ChatGPT access checks (stdlib only)."""
import asyncio
import contextlib
import os
from pathlib import Path
import signal
import tempfile

from src.messages import message as ui_message
from src.contracts.contracts import ContractError, strict_json


async def _command(executable, arguments, directory):
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        executable, *arguments, cwd=directory,
        env=os.environ | {'PWD': str(directory), 'NO_COLOR': '1', 'GIT_TERMINAL_PROMPT': '0'},
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True))
    child = None
    try:
        child = await asyncio.shield(spawn)
        stdout, stderr = await child.communicate()
    except BaseException:
        child = child if child is not None else await spawn
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        await child.communicate()
        raise
    return child.returncode, stdout.decode('utf-8', errors='replace'), stderr.decode('utf-8', errors='replace')


def _failure(stdout, stderr):
    return (stderr.strip() or stdout.strip() or ui_message('diagnostic.codex_models.no_diagnostic'))[:1500]


async def catalog(executable, *, timeout=30):
    try:
        async with asyncio.timeout(timeout):
            with tempfile.TemporaryDirectory(prefix='explain-models-') as directory:
                code, stdout, stderr = await _command(executable, ['debug', 'models'], Path(directory).resolve())
    except TimeoutError:
        raise ValueError(ui_message(
            'diagnostic.codex_models.codex_model_catalog_exceeded_its_second_time_limit',
            timeout=timeout,
        )) from None
    if code:
        raise ValueError(ui_message('diagnostic.codex_models.cannot_load_the_codex_model_catalog', value=_failure(stdout, stderr)))
    try:
        value = strict_json(stdout)
    except ContractError:
        raise ValueError(ui_message('diagnostic.codex_models.codex_returned_an_invalid_model_catalog_json_document')) from None
    if not isinstance(value, dict) or not isinstance(value.get('models'), list):
        raise ValueError(ui_message('diagnostic.codex_models.codex_model_catalog_must_contain_a_models_array'))
    models, slugs = [], set()
    for item in value['models']:
        if not isinstance(item, dict) or not isinstance(item.get('slug'), str) or not item['slug'].strip():
            raise ValueError(ui_message('diagnostic.codex_models.codex_model_catalog_contains_an_invalid_model_slug'))
        row = {key: item.get(key, default) for key, default in
               (('slug', None), ('display_name', item['slug']), ('visibility', 'unknown'), ('supported_in_api', None))}
        if (not isinstance(row['display_name'], str) or not isinstance(row['visibility'], str) or
                row['supported_in_api'] is not None and type(row['supported_in_api']) is not bool or
                row['slug'] in slugs):
            raise ValueError(ui_message('diagnostic.codex_models.codex_model_catalog_contains_invalid_or_duplicate_model'))
        slugs.add(row['slug'])
        models.append(row)
    return [model for model in models if model['visibility'] != 'hide']


async def check_model(executable, model, *, timeout=60):
    result = {'status': 'unverified', 'message': ''}
    try:
        async with asyncio.timeout(timeout):
            with tempfile.TemporaryDirectory(prefix='explain-model-check-') as directory:
                directory = Path(directory).resolve()
                code, stdout, stderr = await _command(executable, ['login', 'status'], directory)
                if code or 'logged in using chatgpt' not in (stdout + '\n' + stderr).casefold():
                    return dict(result, message=ui_message('diagnostic.codex_models.sign_in_with_chatgpt_using_codex_login_then'))
                code, stdout, stderr = await _command(executable, [
                    'exec', '--ephemeral', '--sandbox', 'read-only', '--skip-git-repo-check',
                    '--model', model, '--json', '--color', 'never', '-c', 'approval_policy="never"',
                    'Ответь только OK. Не используй инструменты.'], directory)
    except TimeoutError:
        return dict(result, message=ui_message(
            'diagnostic.codex_models.the_model_check_exceeded_its_second_time_limit',
            timeout=timeout,
        ))
    except OSError as exc:
        return dict(result, message=str(exc))
    events, malformed = [], False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = strict_json(line)
            if not isinstance(event, dict):
                malformed = True
            else:
                events.append(event)
        except ContractError:
            malformed = True
    errors = [event for event in events if event.get('type') in ('error', 'turn.failed')]
    if code or errors:
        if 'is not supported when using codex with a chatgpt account' in (stdout + '\n' + stderr).casefold():
            return {'status': 'unavailable', 'message': ui_message(
                'diagnostic.codex_models.this_model_is_not_supported_with_your_codex',
            )}
        return dict(result, message=ui_message('diagnostic.codex_models.the_model_check_failed', value=_failure(stdout, stderr)))
    replies = [event.get('item', {}).get('text') for event in events
               if event.get('type') == 'item.completed' and isinstance(event.get('item'), dict)
               and event['item'].get('type') == 'agent_message']
    if (not malformed and any(event.get('type') == 'turn.completed' for event in events)
            and any(isinstance(reply, str) and reply.strip() for reply in replies)):
        return {'status': 'available', 'message': ui_message(
            'diagnostic.codex_models.the_model_returned_a_completed_response_using_chatgpt',
        )}
    return dict(result, message=ui_message('diagnostic.codex_models.codex_did_not_return_a_completed_nonempty_model'))
