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
from .persistence import BoundedAppendWriter, rotate_bounded_log
from .security import (
    configured_environment_values,
    redact_secrets,
    sensitive_environment_values,
)


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


def _safe_text(
    value: Any, limit: int = _SAFE_TEXT_LIMIT, *, redaction_values: Iterable[str] = (),
) -> str:
    """Keep exception text bounded, plain, and credential-redacted."""
    text = redact_secrets(str(value), extra_values=redaction_values)
    text = "".join(character if character in "\t\n" or ord(character) >= 32 else " " for character in text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _safe_exception_type(exc: BaseException, *, redaction_values: Iterable[str] = ()) -> str:
    return _safe_text(
        f"{type(exc).__module__}.{type(exc).__name__}", 200,
        redaction_values=redaction_values,
    )


def _safe_traceback(exc: BaseException, *, redaction_values: Iterable[str] = ()) -> str:
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
            function = _safe_text(
                frame.name, 120, redaction_values=redaction_values,
            ) or "<unknown>"
            frame_lines.append(f'  File "{filename}", line {frame.lineno}, in {function}')
        label = _safe_exception_type(current, redaction_values=redaction_values)
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


def _diagnostic_redaction_values(
    argv: Iterable[str], extra_values: Iterable[str] = (),
) -> list[str]:
    """Collect semantic and explicitly configured credential values."""
    values = list(sensitive_environment_values())
    values.extend(value for value in extra_values if isinstance(value, str))
    arguments = list(argv)
    config_path = _path_option(arguments, "--config")
    if config_path:
        try:
            data = json.loads(config_path.read_bytes()[:256_000].decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            names: set[str] = set()
            for section in (
                "developer", "reviewer", "investigator",
                "developer_agent", "reviewer_agent", "investigator_agent",
            ):
                settings = data.get(section)
                if isinstance(settings, dict) and isinstance(settings.get("api_key_env"), str):
                    names.add(settings["api_key_env"])
            sandbox = data.get("sandbox")
            if isinstance(sandbox, dict):
                names.update(
                    name for name in sandbox.get("pass_env", ()) if isinstance(name, str)
                )
            values.extend(configured_environment_values(os.environ, names))
    return sorted(set(values), key=len, reverse=True)


def safe_command_context(
    argv: Iterable[str] = (), *, entrypoint: str = "cli", action: str | None = None,
    redaction_values: Iterable[str] = (),
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
        "entrypoint": _safe_text(
            entrypoint, 80, redaction_values=redaction_values,
        ) or "unknown",
        "action": (safe_action.group(0).lstrip("/").lower() if safe_action else known_action or "runner"),
        "options": sorted(set(options)),
    }


def diagnostic_path(repo: Path | None = None, state_dir: Path | None = None) -> Path:
    """Resolve the project-scoped diagnostic path and fail closed on unsafe state."""
    from .worktrees import state_paths

    repository = (repo or Path.cwd()).expanduser().resolve()
    paths = state_paths(repository, state_dir)
    root = paths.diagnostics if paths.diagnostics is not None else paths.root / "diagnostics"
    return root / DIAGNOSTIC_FILENAME


def _append_line(
    path: Path, line: str, *, redaction_values: Iterable[str] = (),
) -> None:
    payload = (line.rstrip("\n") + "\n").encode("utf-8", errors="replace")
    if len(payload) > DIAGNOSTIC_MAX_BYTES:
        payload = json.dumps(
            {"version": __version__, "category": "diagnostic_record_truncated", "record_truncated": True},
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    with _LOG_LOCK:
        with BoundedAppendWriter(
            path, max_bytes=DIAGNOSTIC_MAX_BYTES, truncate=False,
        ) as existing:
            size = existing.written_bytes
        safe_payload = redact_secrets(
            payload.decode("utf-8", errors="replace"),
            extra_values=redaction_values,
        ).encode("utf-8")
        if size + len(safe_payload) > DIAGNOSTIC_MAX_BYTES:
            rotate_bounded_log(path, DIAGNOSTIC_BACKUPS)
        writer = BoundedAppendWriter(
            path, max_bytes=DIAGNOSTIC_MAX_BYTES,
            truncation_marker=json.dumps({
                "version": __version__,
                "category": "diagnostic_record_truncated",
                "record_truncated": True,
            }, separators=(",", ":")) + "\n",
            redaction_values=redaction_values,
            truncate=False,
        )
        try:
            writer.write(payload)
            writer.flush(sync=True)
        finally:
            writer.close()


def record_diagnostic(
    category: str, *, exc: BaseException | None = None,
    argv: Iterable[str] = (), entrypoint: str = "cli", action: str | None = None,
    repo: Path | None = None, state_dir: Path | None = None,
    redaction_values: Iterable[str] = (),
) -> Path | None:
    """Persist one diagnostic event; never raise if diagnostic persistence fails."""
    try:
        values = list(argv)
        configured_values = _diagnostic_redaction_values(values, redaction_values)
        resolved_repo = repo or _path_option(values, "--repo")
        diagnostic_repo = (
            Path(resolved_repo).expanduser().resolve()
            if resolved_repo is not None else Path.cwd().resolve()
        )
        resolved_state_dir = state_dir or _path_option(values, "--state-dir")
        safe_category = category if _SAFE_CATEGORY.fullmatch(category) else "unknown"
        event: dict[str, Any] = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            "version": __version__,
            "category": safe_category,
            "command": safe_command_context(
                values, entrypoint=entrypoint, action=action,
                redaction_values=configured_values,
            ),
            "runtime": {
                "python": platform.python_version(),
                "platform": sys.platform,
                "os": os.name,
            },
        }
        if exc is not None:
            event["exception"] = {
                "type": _safe_exception_type(exc, redaction_values=configured_values),
                "traceback": _safe_traceback(exc, redaction_values=configured_values),
            }
        from .worktrees import ensure_state_paths
        ensure_state_paths(diagnostic_repo, resolved_state_dir)
        path = diagnostic_path(diagnostic_repo, resolved_state_dir)
        _append_line(
            path, json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            redaction_values=configured_values,
        )
        return path
    except BaseException:
        return None


def run_with_diagnostics(
    callback: Callable[[], int], *, argv: Iterable[str] = (), entrypoint: str = "cli",
    repo: Path | None = None, state_dir: Path | None = None,
    redaction_values: Iterable[str] = (),
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
                redaction_values=redaction_values,
            )
        raise
    except KeyboardInterrupt as exc:
        record_diagnostic(
            "interrupt", exc=exc, argv=values, entrypoint=entrypoint,
            repo=resolved_repo, state_dir=resolved_state_dir,
            redaction_values=redaction_values,
        )
        raise
    except BaseException as exc:
        record_diagnostic(
            "crash", exc=exc, argv=values, entrypoint=entrypoint,
            repo=resolved_repo, state_dir=resolved_state_dir,
            redaction_values=redaction_values,
        )
        raise
