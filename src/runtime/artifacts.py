# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Private artifact files, hashes and run directory helpers."""
from __future__ import annotations
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any
from src.messages import message as ui_message
from src.runtime.errors import AuditError

def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents

def overlap(a: Path, b: Path) -> bool:
    return inside(a, b) or inside(b, a)

def private_directory(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700, exist_ok=True)

def neutral_temporary_base(source: Path) -> Path:
    for candidate in (Path('/tmp'), Path('/var/tmp')):
        candidate = candidate.resolve()
        if candidate.is_dir() and not inside(candidate, source.resolve()):
            return candidate
    raise AuditError(ui_message('diagnostic.artifacts.no_temporary_directory_outside_the_source_tree_is'))

def atomic(path: Path, data: str | bytes) -> None:
    private_directory(path.parent)
    blob = data.encode('utf-8') if isinstance(data, str) else data
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()

def save_json(path: Path, value: Any) -> None:
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def append_observation(path, event):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + '\n')

def slug(branch: str) -> str:
    text = re.sub(r'[^A-Za-z0-9._-]+', '_', branch).strip('._-')[:55] or 'branch'
    return f'{text}--{digest(branch.encode())[:12]}'
