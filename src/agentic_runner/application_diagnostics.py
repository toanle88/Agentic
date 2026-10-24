"""Best-effort, bounded diagnostics for Agentic application failures.

This log is intentionally separate from workflow, provider, test, and shell
job artifacts.  It records bounded structural context only; issue text,
repository contents, environment variables, and raw provider output do not
belong in this file.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import platform
import re
import sys
import threading
import traceback
from collections.abc import Callable, Iterable
from typing import Any

from . import __version__
from .security import redact_secrets


DIAGNOSTIC_FILENAME = "agentic.log"
DIAGNOSTIC_MAX_BYTES = 256_000
DIAGNOSTIC_BACKUPS = 3
_SAFE_TEXT_LIMIT = 1_000
_SAFE_CATEGORY = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
_SAFE_ACTION = re.compile(r"^/?[a-z][a-z0-9_-]{0,40}$", re.IGNORECASE)
_OPTION_WITH_VALUE = {
    "--config", "--discovery-timeout", "--issue", "--output", "--port",
    "--profile", "--repo", "--runner", "--sandbox-context", "--sandbox-dockerfile",
    "--sandbox-image", "--sandbox-language", "--session-dir", "--state-dir",
    "--task", "--task-file", "--worktree", "--instruction", "--reason",
}
_KNOWN_ACTIONS = {
    "audit", "cleanup", "dashboard", "doctor", "help", "init", "runs", "shell", "status",
}
_LOG_LOCK = threading.Lock()


def _safe_text(value: Any, limit: int = _SAFE_TEXT_LIMIT) -> str:
    """Keep exception text bounded, plain, and credential-redacted."""
    text = redact_secrets(str(value))
    text = "".join(character if character in "\t\n" or ord(character) >= 32 else " " for character in text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _safe_exception_type(exc: BaseException) -> str:
    return _safe_text(f"{type(exc).__module__}.{type(exc).__name__}", 200)


def _safe_traceback(exc: BaseException) -> str:
    """Render stack structure without source lines or arbitrary local values."""
    sections: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(sections) < 4:
        seen.add(id(current))
        frames = traceback.extract_tb(current.__traceback__)
        frame_lines = []
        for frame in frames[-20:]:
            filename = os.path.basename(str(frame.filename)) or "<unknown>"
            function = _safe_text(frame.name, 120) or "<unknown>"
            frame_lines.append(f'  File "{filename}", line {frame.lineno}, in {function}')
        label = _safe_exception_type(current)
        sections.append("\n".join([f"{label} traceback:", *frame_lines]))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return "\nDuring handling of the above exception:\n".join(sections)


def _option_value(argv: list[str], option: str) -> str | None:
    for index, token in enumerate(argv):
        if token == option and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith(option + "="):
            return token.split("=", 1)[1]
    return None


def _path_option(argv: list[str], option: str) -> Path | None:
    raw = _option_value(argv, option)
    if raw is None:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return None


def safe_command_context(
    argv: Iterable[str] = (), *, entrypoint: str = "cli", action: str | None = None,
) -> dict[str, Any]:
    """Return command names and option names without persisting argument values."""
    values = [str(value) for value in argv]
    options: list[str] = []
    positional: list[str] = []
    skip_value = False
    for token in values:
        if skip_value:
            skip_value = False
            continue
        if token.startswith("-"):
            option = token.split("=", 1)[0]
            if re.fullmatch(r"--?[A-Za-z0-9][A-Za-z0-9-]*", option):
                options.append(option)
            if option in _OPTION_WITH_VALUE and "=" not in token:
                skip_value = True
            continue
        positional.append(token)
    known_action = next((token for token in positional if token in _KNOWN_ACTIONS), None)
    safe_action = _SAFE_ACTION.fullmatch(action or "")
    return {
        "entrypoint": _safe_text(entrypoint, 80) or "unknown",
        "action": (safe_action.group(0).lstrip("/").lower() if safe_action else known_action or "runner"),
        "options": sorted(set(options)),
    }


def diagnostic_path(repo: Path | None = None, state_dir: Path | None = None) -> Path:
    """Resolve the project-scoped diagnostic path, with an unscoped fallback."""
    from .worktrees import default_state_dir, state_paths

    repository = (repo or Path.cwd()).expanduser().resolve()
    try:
        paths = state_paths(repository, state_dir)
        root = paths.diagnostics if paths.diagnostics is not None else paths.root / "diagnostics"
    except (OSError, TypeError, ValueError, RuntimeError):
        root = (state_dir or default_state_dir()).expanduser().resolve() / "unknown-repository" / "diagnostics"
    return root / DIAGNOSTIC_FILENAME


def _rotate(path: Path) -> None:
    oldest = path.with_name(f"{path.name}.{DIAGNOSTIC_BACKUPS}")
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass
    for index in range(DIAGNOSTIC_BACKUPS - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        target = path.with_name(f"{path.name}.{index + 1}")
        if source.exists():
            os.replace(source, target)
    if path.exists():
        os.replace(path, path.with_name(f"{path.name}.1"))


def _append_line(path: Path, line: str) -> None:
    payload = (line.rstrip("\n") + "\n").encode("utf-8", errors="replace")
    if len(payload) > DIAGNOSTIC_MAX_BYTES:
        payload = json.dumps(
            {"version": __version__, "category": "diagnostic_record_truncated", "record_truncated": True},
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size + len(payload) > DIAGNOSTIC_MAX_BYTES:
            _rotate(path)
        with path.open("ab") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())


def record_diagnostic(
    category: str, *, exc: BaseException | None = None,
    argv: Iterable[str] = (), entrypoint: str = "cli", action: str | None = None,
    repo: Path | None = None, state_dir: Path | None = None,
) -> Path | None:
    """Persist one diagnostic event; never raise if diagnostic persistence fails."""
    try:
        values = list(argv)
        resolved_repo = repo or _path_option(values, "--repo")
        resolved_state_dir = state_dir or _path_option(values, "--state-dir")
        safe_category = category if _SAFE_CATEGORY.fullmatch(category) else "unknown"
        event: dict[str, Any] = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            "version": __version__,
            "category": safe_category,
            "command": safe_command_context(values, entrypoint=entrypoint, action=action),
            "runtime": {
                "python": platform.python_version(),
                "platform": sys.platform,
                "os": os.name,
            },
        }
        if exc is not None:
            event["exception"] = {
                "type": _safe_exception_type(exc),
                "traceback": _safe_traceback(exc),
            }
        path = diagnostic_path(resolved_repo, resolved_state_dir)
        _append_line(path, json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        return path
    except BaseException:
        return None


def run_with_diagnostics(
    callback: Callable[[], int], *, argv: Iterable[str] = (), entrypoint: str = "cli",
    repo: Path | None = None, state_dir: Path | None = None,
) -> int:
    """Run an entrypoint and record uncaught failures before re-raising them."""
    values = list(argv)
    resolved_repo = repo or _path_option(values, "--repo")
    resolved_state_dir = state_dir or _path_option(values, "--state-dir")
    try:
        return callback()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            record_diagnostic(
                "handled_error", exc=exc, argv=values, entrypoint=entrypoint,
                repo=resolved_repo, state_dir=resolved_state_dir,
            )
        raise
    except KeyboardInterrupt as exc:
        record_diagnostic(
            "interrupt", exc=exc, argv=values, entrypoint=entrypoint,
            repo=resolved_repo, state_dir=resolved_state_dir,
        )
        raise
    except BaseException as exc:
        record_diagnostic(
            "crash", exc=exc, argv=values, entrypoint=entrypoint,
            repo=resolved_repo, state_dir=resolved_state_dir,
        )
        raise
