"""Bounded reads of repository-controlled metadata used by discovery."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .persistence import read_text_bounded


MAX_REPOSITORY_METADATA_BYTES = 256_000


@dataclass(frozen=True)
class RepositoryMetadata:
    """Common metadata snapshot shared by deterministic and assisted discovery."""

    package_json: dict[str, Any]
    makefile_text: str


def read_repository_metadata(repo: Path) -> RepositoryMetadata:
    """Read common metadata with a complete-file byte bound.

    Missing, malformed, unreadable, and oversized files are treated as absent.
    Callers retain their own candidate-selection policies while sharing the
    bounded file reads.
    """
    package: dict[str, Any] = {}
    package_path = repo / "package.json"
    if package_path.is_file():
        raw = read_text_bounded(package_path, MAX_REPOSITORY_METADATA_BYTES)
        if raw is not None:
            try:
                value = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError):
                value = {}
            if isinstance(value, dict):
                package = value

    makefile_text = ""
    makefile = next(
        (repo / name for name in ("Makefile", "makefile", "GNUmakefile")
         if (repo / name).is_file()),
        None,
    )
    if makefile is not None:
        bounded = read_text_bounded(makefile, MAX_REPOSITORY_METADATA_BYTES)
        if bounded is not None:
            makefile_text = bounded
    return RepositoryMetadata(package_json=package, makefile_text=makefile_text)
