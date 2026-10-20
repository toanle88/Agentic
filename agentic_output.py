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


_TIMESTAMP_RE = re.compile(r"^\s*(\[\d{2}:\d{2}:\d{2}\])")
_PREFIX_RE = re.compile(
    r"^\s*(?:\[\d{2}:\d{2}:\d{2}\]\s*)?"
    r"(\[(?:DEVELOPER|REVIEW|TESTS|RETRY|GIT|PATCH|INVESTIGATION|BASELINE|"
    r"PUBLISH|SKIP|INIT|CLEAN|SYSTEM|COST|JOB [^\]]+|DASHBOARD|MANUAL|"
    r"TIMEOUT|BLOCKED|STOP|RESUME|DRY RUN)\])"
    r"(?=\s|$)",
    re.IGNORECASE,
)
_STRUCTURED_SUFFIX_RE = re.compile(
    r"(?<![\w./\\-])(\[(?:RETRY|TIMEOUT|BLOCKED|PUBLISH|SKIP)\])(?=\s*$)",
    re.IGNORECASE,
)
_BRACKETED_STATUS_RE = re.compile(
    r"(?<![\w./\\-])\[(?P<token>WARN(?:ING)?|FAIL(?:ED|URE)?|ERROR)\s*\]",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(
    r"(?<![\w./\\-])(?:STOP REQUESTED|NO MATCHING|TIMED OUT|STILL RUNNING|TIMEOUT|"
    r"WARNING|WARN|RETRY|PENDING|BLOCKED|SKIPPED|CANCEL(?:LED|ED)?|DEADLINE|"
    r"ERROR|FAILED|FAILURE|REJECTED|UNAVAILABLE|COULD NOT|"
    r"EXIT\s+(?:-[1-9]\d*|[1-9]\d*)|PASSED|APPROVED|VALIDATED|COMPLETED|MARKED COMPLETE|"
    r"EXIT\s+0|SUCCESS|FINISHED|RUNNING|STARTED)(?![\w./\\-])",
    re.IGNORECASE,
)

_PREFIX_COLORS = {
    "DEVELOPER": "36",
    "REVIEW": "35",
    "TESTS": "34",
    "RETRY": "33",
    "TIMEOUT": "33",
    "BLOCKED": "33",
    "STOP": "33",
    "PUBLISH": "33",
    "SKIP": "33",
}


def _status_color(token: str) -> str:
    """Return the color for a bounded, explicit status token."""
    upper = token.upper()
    if upper in {"WARNING", "WARN", "RETRY", "TIMEOUT", "TIMED OUT", "PENDING",
                 "BLOCKED", "SKIPPED", "CANCEL", "CANCELLED", "CANCELED", "DEADLINE",
                 "STILL RUNNING", "STOP REQUESTED", "NO MATCHING"}:
        return "33"
    if upper == "RUNNING" or upper == "STARTED":
        return "36"
    if upper in {"PASSED", "APPROVED", "VALIDATED", "COMPLETED", "MARKED COMPLETE",
                 "EXIT 0", "SUCCESS", "FINISHED"}:
        return "32"
    return "31"


def _is_path_bracketed_status(line: str, start: int, end: int) -> bool:
    """Return whether a bracketed status token is part of a path component."""
    if start < len(line) and line[start] == "[":
        if end <= start or line[end - 1] != "]":
            return False
        bracket_start = start
    else:
        if start == 0 or end >= len(line) or line[start - 1] != "[":
            return False
        closing = end
        while closing < len(line) and line[closing].isspace():
            closing += 1
        if closing >= len(line) or line[closing] != "]":
            return False
        bracket_start = start - 1
    return "/" in line[:bracket_start] or "\\" in line[:bracket_start]


def _styled_spans(line: str, *, style_status_tokens: bool = True) -> list[tuple[int, int, str]]:
    """Find terminal-only spans without styling message bodies or paths."""
    spans: list[tuple[int, int, str]] = []
    timestamp = _TIMESTAMP_RE.match(line)
    if timestamp:
        spans.append((timestamp.start(1), timestamp.end(1), "90"))

    prefix = _PREFIX_RE.match(line)
    if prefix:
        label = prefix.group(1)[1:-1].upper()
        code = _PREFIX_COLORS.get(label, "90")
        spans.append((prefix.start(1), prefix.end(1), code))

    if style_status_tokens:
        for prefix in _STRUCTURED_SUFFIX_RE.finditer(line):
            label = prefix.group(1)[1:-1].upper()
            code = _PREFIX_COLORS.get(label, "90")
            span = (prefix.start(1), prefix.end(1), code)
            if _is_path_bracketed_status(line, span[0], span[1]):
                continue
            if not any(span[0] < existing_end and span[1] > existing_start
                       for existing_start, existing_end, _ in spans):
                spans.append(span)

        for status in _BRACKETED_STATUS_RE.finditer(line):
            span = (status.start(), status.end(), _status_color(status.group("token")))
            if _is_path_bracketed_status(line, span[0], span[1]):
                continue
            if not any(span[0] < existing_end and span[1] > existing_start
                       for existing_start, existing_end, _ in spans):
                spans.append(span)

    if style_status_tokens and (
        timestamp or prefix or _STATUS_RE.match(line) or _BRACKETED_STATUS_RE.match(line)
    ):
        for match in _STATUS_RE.finditer(line):
            start, end = match.span()
            if any(start < existing_end and end > existing_start
                   for existing_start, existing_end, _ in spans):
                continue
            if _is_path_bracketed_status(line, start, end):
                continue
            spans.append((start, end, _status_color(match.group())))
    return sorted(spans)


def _style_line(line: str, stream: Any = None, *, style_status_tokens: bool = True) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith(("{", "[\"")):
        return line
    spans = _styled_spans(line, style_status_tokens=style_status_tokens)
    if not spans:
        return line
    output: list[str] = []
    cursor = 0
    for start, end, code in spans:
        output.append(line[cursor:start])
        output.append(paint(line[start:end], code, stream))
        cursor = end
    output.append(line[cursor:])
    return "".join(output)


def style_text(value: Any, stream: Any = None, *, style_status_tokens: bool = True) -> str:
    """Style structured prefixes and optional generated status tokens."""
    text = strip_ansi(value)
    if not color_enabled(stream):
        return text
    output: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        output.append(_style_line(body, stream, style_status_tokens=style_status_tokens) + ending)
    return "".join(output)
