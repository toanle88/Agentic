"""Single-link regular-file checks for worktree/provider boundaries."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any


def _identity(metadata: Any) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _validate_single_link_regular(metadata: Any, *, description: str) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{description} must be regular files")
    if getattr(metadata, "st_nlink", 1) != 1:
        raise ValueError(f"hard-linked {description} files are not allowed")


def revalidate_single_link_regular(
    descriptor: int,
    expected_identity: tuple[int, int],
    *,
    description: str = "file",
) -> os.stat_result:
    """Recheck an open descriptor immediately before using its contents."""
    metadata = os.fstat(descriptor)
    _validate_single_link_regular(metadata, description=description)
    if _identity(metadata) != expected_identity:
        raise ValueError(f"{description} changed while it was being opened")
    return metadata


def open_single_link_regular(
    path: Path,
    flags: int,
    mode: int = 0o644,
    *,
    description: str = "file",
) -> tuple[int, tuple[int, int]]:
    """Open a regular file only when its path and descriptor identify one inode.

    The caller owns the returned descriptor. ``O_CREAT`` is supported for
    safe creation of new worktree files; the descriptor is still rejected if
    an existing hard-linked target is selected by a race.
    """
    try:
        path_metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        path_metadata = None
    if path_metadata is not None:
        _validate_single_link_regular(path_metadata, description=description)

    open_flags = flags | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, open_flags, mode)
    try:
        descriptor_metadata = os.fstat(descriptor)
        _validate_single_link_regular(descriptor_metadata, description=description)
        if (
            path_metadata is not None
            and _identity(descriptor_metadata) != _identity(path_metadata)
        ):
            raise ValueError(f"{description} changed while it was being opened")
        expected_identity = _identity(descriptor_metadata)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, expected_identity
