"""Crash-safe helpers for Agentic's durable state files.

Run metadata is read by later invocations and by the dashboard while a job may
still be running.  Replacing a complete temporary file keeps readers from
observing half-written JSON after a process interruption.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .security import redact_secrets


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Redact, then replace *path* only after the complete write succeeds.

    A failed write or replacement leaves an existing destination untouched and
    removes the same-directory temporary file. This is the persistence
    boundary for durable text as well as JSON serialized through
    :func:`atomic_write_json`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = redact_secrets(text)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding=encoding, dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass


def atomic_write_json(path: Path, value: Any, *, indent: int = 2) -> None:
    """Serialize JSON and persist it atomically with a trailing newline."""
    atomic_write_text(path, json.dumps(value, indent=indent) + "\n")
