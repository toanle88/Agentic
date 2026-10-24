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


def read_text_tail(path: Path, max_bytes: int, *, encoding: str = "utf-8") -> str:
    """Read at most the final *max_bytes* of a text artifact.

    Durable provider and test logs are untrusted evidence.  Reading their tail
    keeps resume and diagnostic paths bounded even when an older writer created
    an oversized artifact.  ``errors='replace'`` also makes a byte-window cut
    at a UTF-8 boundary harmless to callers.
    """
    if max_bytes <= 0:
        return ""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            payload = stream.read(max_bytes)
    except OSError:
        return ""
    return payload.decode(encoding, errors="replace")


def read_text_bounded(path: Path, max_bytes: int, *, encoding: str = "utf-8") -> str | None:
    """Read a complete text artifact only when it fits the byte budget.

    JSON state must be parsed as a complete document, so tail reads are not a
    safe substitute here.  Returning ``None`` for missing, unreadable, or
    oversized files lets callers fail closed without allocating for an
    attacker-controlled artifact.
    """
    if max_bytes <= 0:
        return "" if max_bytes == 0 else None
    try:
        with path.open("rb") as stream:
            payload = stream.read(max_bytes + 1)
    except OSError:
        return None
    if len(payload) > max_bytes:
        return None
    return payload.decode(encoding, errors="replace")
