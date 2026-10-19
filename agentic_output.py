"""TTY-only presentation helpers shared by the runner and interactive shell."""
from __future__ import annotations

import os
import re
import sys
from typing import Any


_ANSI_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)


def strip_ansi(value: Any) -> str:
    """Remove terminal control sequences before text is persisted or replayed."""
    return _ANSI_SEQUENCE.sub("", str(value))


def color_enabled(stream: Any = None) -> bool:
    """Return whether ANSI styling is allowed for this output stream."""
    target = stream or sys.stdout
    return not (
        os.environ.get("NO_COLOR")
        or os.environ.get("AGENTIC_PLAIN")
        or not getattr(target, "isatty", lambda: False)()
    )


def paint(text: Any, code: str, stream: Any = None) -> str:
    """Paint text only when the destination is an interactive terminal."""
    value = str(text)
    if not color_enabled(stream):
        return value
    return f"\033[{code}m{value}\033[0m"


def _line_color(line: str) -> str | None:
    """Choose a readable color from explicit, still-readable status text."""
    stripped = line.strip()
    upper = stripped.upper()
    # JSON and blank lines are data, not terminal decoration. Human log
    # records beginning with a timestamp are handled by the rules below.
    if not stripped or stripped.startswith(("{", "[\"")):
        return None
    if stripped.startswith("+") and not stripped.startswith("+++"):
        return "32"
    if stripped.startswith("-") and not stripped.startswith("---"):
        return "31"
    if "DELETION" in upper or "REMOVED" in upper:
        return "31"
    if "INSERTION" in upper or "ADDITION" in upper:
        return "32"
    if any(token in upper for token in (
        "WARNING", "WARN:", "RETRY", "TIMEOUT", "TIMED OUT", "PENDING",
        "BLOCKED", "SKIPPED", "STOP REQUESTED", "NO MATCHING", "CANCEL",
        "DEADLINE", "STILL RUNNING", "[PUBLISH]", "[SKIP]",
    )):
        return "33"
    if re.search(r"\bEXIT\s+-?[1-9]\d*\b", upper):
        return "31"
    if any(token in upper for token in (
        "ERROR", "FAILED", "FAILURE", "REJECTED", "EXIT 1", "EXIT 2",
        "EXIT 3", "EXIT 4", "EXIT 5", "EXIT 6", "EXIT 7", "EXIT 8",
        "EXIT 9", "COMMAND FAILED", "COULD NOT", "UNAVAILABLE",
    )):
        return "31"
    if any(token in upper for token in (
        "PASSED", "APPROVED", "VALIDATED", "COMPLETED", "MARKED COMPLETE",
        "EXIT 0", "ALL CONFIGURED CHECKS PASSED", "SUCCESS", "FINISHED",
    )):
        return "32"
    if stripped.startswith(("$ ", "> ")) or any(token in upper for token in (
        "RUNNING COMMAND:", "RUNNING:", "USING TOOL:", "COMMAND:", "[COMMAND]",
    )):
        return "36"
    if any(token in upper for token in (
        "MODIFIED FILES:", "PROPOSED CHANGES:", "FILE CHANGE", "DIFF --",
    )):
        return "35"
    if "[DEVELOPER]" in upper:
        return "36"
    if "[REVIEW]" in upper:
        return "35"
    if "[TESTS]" in upper:
        return "34"
    if any(token in upper for token in (
        "[INVESTIGATION]", "[BASELINE]", "[PATCH]", "[GIT]", "[INIT]",
        "[CLEAN]", "[SYSTEM]", "[COST]", "[JOB ", "[DASHBOARD]", "[MANUAL]",
    )):
        return "90"
    if "RUNNING" in upper or "STARTED" in upper:
        return "36"
    return None


def style_text(value: Any, stream: Any = None) -> str:
    """Style each line while preserving indentation and line endings."""
    text = strip_ansi(value)
    if not color_enabled(stream):
        return text
    output: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        code = _line_color(body)
        output.append(paint(body, code, stream) + ending if code else line)
    if text and not output:
        code = _line_color(text)
        return paint(text, code, stream) if code else text
    return "".join(output)
