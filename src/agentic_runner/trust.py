"""External repository trust consent for host-side execution.

Trust is deliberately separate from target-repository configuration.  The
record contains only a digest of repository identity, never a path, remote
URL, credential, issue text, or repository content.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Literal

from .persistence import atomic_write_json, read_text_prefix
from .worktrees import (
    ensure_state_paths,
    repository_identity,
    state_paths,
)


TRUST_RECORD_VERSION = 1
TRUST_RECORD_FILENAME = "repository-trust.json"
TRUST_RECORD_MAX_BYTES = 16 * 1024
TrustChoice = Literal["trusted", "isolated"]


def trust_record_path(repo: Path, state_dir: Path | None = None) -> Path:
    """Return the external trust record path for *repo*."""
    return state_paths(repo, state_dir).root / TRUST_RECORD_FILENAME


def is_repository_trusted(
    repo: Path, state_dir: Path | None = None,
    *, identity: str | None = None,
) -> bool:
    """Return true only for a valid record matching the current identity."""
    path = trust_record_path(repo, state_dir)
    raw = read_text_prefix(path, TRUST_RECORD_MAX_BYTES)
    if not raw:
        return False
    try:
        record = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    return (
        record.get("version") == TRUST_RECORD_VERSION
        and record.get("repository_identity") == (
            identity if identity is not None else repository_identity(repo)
        )
    )


def record_repository_trust(
    repo: Path, state_dir: Path | None = None,
    *, identity: str | None = None,
) -> Path:
    """Persist host-execution consent outside the repository."""
    paths = ensure_state_paths(repo, state_dir)
    path = paths.root / TRUST_RECORD_FILENAME
    atomic_write_json(
        path,
        {
            "version": TRUST_RECORD_VERSION,
            "repository_identity": identity or repository_identity(repo),
        },
    )
    return path


def revoke_repository_trust(repo: Path, state_dir: Path | None = None) -> Path:
    """Revoke consent without touching repository-controlled files."""
    paths = ensure_state_paths(repo, state_dir)
    path = paths.root / TRUST_RECORD_FILENAME
    atomic_write_json(path, {"version": TRUST_RECORD_VERSION, "repository_identity": None})
    return path


def _interactive_terminal() -> bool:
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


def request_repository_trust(
    repo: Path,
    state_dir: Path | None = None,
    *,
    input_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], None] | None = None,
    interactive: bool | None = None,
    identity: str | None = None,
) -> TrustChoice:
    """Resolve host consent, failing closed when no safe choice is possible."""
    current_identity = identity or repository_identity(repo)
    if is_repository_trusted(repo, state_dir, identity=current_identity):
        return "trusted"
    if interactive is None:
        interactive = _interactive_terminal()
    if not interactive:
        raise RuntimeError(
            "Repository trust is required before trusted host execution. "
            "Run interactively and choose host trust, configure and explicitly "
            "select isolated mode, or use `agentic trust --revoke` to review consent."
        )
    input_fn = input_fn or input
    output_fn = output_fn or print
    output_fn(
        "This run may execute repository code, configured test commands, and "
        "provider workflows with the host account's permissions. Repository "
        "files and provider output remain untrusted instructions."
    )
    output_fn("Choose an execution mode:")
    output_fn("  1) Trust and run on the host")
    output_fn("  2) Run in isolated mode (requires a configured, available sandbox)")
    output_fn("  3) Cancel")
    try:
        answer = input_fn("Selection [1/2/3, default 3]: ")
    except (EOFError, KeyboardInterrupt) as exc:
        raise RuntimeError("Repository trust prompt was cancelled; no execution started") from exc
    selection = answer.strip().lower()
    if selection in {"1", "trust", "trusted", "host"}:
        record_repository_trust(repo, state_dir, identity=current_identity)
        return "trusted"
    if selection in {"2", "isolated", "isolate"}:
        return "isolated"
    raise RuntimeError("Repository trust was not granted; execution cancelled")
