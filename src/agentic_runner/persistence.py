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


def _fsync_parent_directory(path: Path) -> None:
    """Durably publish a replacement on POSIX; Windows has no dir-fsync API."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8",
                      redact: bool = True) -> None:
    """Optionally redact, then replace *path* after the complete write succeeds.

    A failed write or replacement leaves an existing destination untouched and
    removes the same-directory temporary file. This is the persistence
    boundary for durable text as well as JSON serialized through
    :func:`atomic_write_json`. Generated HTML may disable the final text pass
    when its dynamic data has already been safely encoded and redacted; running
    the credential regex over JavaScript source can turn expressions such as
    ``key=value`` into invalid code.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if redact:
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
        _fsync_parent_directory(path)
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
