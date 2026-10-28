"""TTY-only presentation helpers shared by the runner and interactive shell."""
from __future__ import annotations

import datetime
import os
import re
import sys
from typing import Any


_ANSI_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)
_TERMINAL_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SYSTEM_PREFIX_MARKER = "\x1eAGENTIC_SYSTEM_PREFIX:"
_SYSTEM_PREFIX_MARKER_END = "\x1f"
_SYSTEM_PREFIX_MARKER_RE = re.compile(
    rf"^{re.escape(_SYSTEM_PREFIX_MARKER)}([A-Za-z][A-Za-z0-9 _-]{{0,39}})"
    rf"{re.escape(_SYSTEM_PREFIX_MARKER_END)}"
)


def strip_ansi(value: Any) -> str:
    """Remove terminal control sequences before text is persisted or replayed."""
    return _ANSI_SEQUENCE.sub("", str(value))


def sanitize_terminal_text(value: Any) -> str:
    """Remove terminal controls while keeping status text readable."""
    text = strip_ansi(value)
    return _TERMINAL_CONTROL.sub(
        lambda match: " " if match.group() in "\t\n\r\v\f" else "",
        text,
    )


def tag_system_prefix(value: Any, prefix: str) -> str:
    """Tag generated output for a cooperating foreground shell renderer."""
    return f"{_SYSTEM_PREFIX_MARKER}{prefix}{_SYSTEM_PREFIX_MARKER_END}{value}"


def untag_system_prefix(value: Any) -> tuple[str, str | None]:
    """Remove an internal generated-prefix tag and return its category."""
    text = str(value)
    match = _SYSTEM_PREFIX_MARKER_RE.match(text)
    if not match:
        return text, None
    return text[match.end():], match.group(1).strip().upper()


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


_TIMESTAMP_PREFIX = r"\[(?:\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]"
_TIMESTAMP_RE = re.compile(rf"^\s*({_TIMESTAMP_PREFIX})")
_PREFIX_RE = re.compile(
    rf"^\s*(?:{_TIMESTAMP_PREFIX}\s*)?"
    r"(\[(?:DEVELOPER|REVIEW|TESTS|RETRY|GIT|PATCH|ERROR|RESULT|SUMMARY|INVESTIGATION|AUDIT|BASELINE|"
    r"PUBLISH|SKIP|INIT|CLEAN|SYSTEM|COST|NO_CHANGES|CANCEL|DOCTOR|"
    r"JOB [^\]]+|DASHBOARD|MANUAL|"
    r"TIMEOUT|BLOCKED|STOP|RESUME|DRY RUN)\])"
    r"(?=\s|$)",
    re.IGNORECASE,
)
_SYSTEM_PREFIX_RE = re.compile(
    rf"^\s*(?:{_TIMESTAMP_PREFIX}\s*)?"
    r"(\[[A-Za-z][A-Za-z0-9 _-]{0,39}\])(?=\s|$)"
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
_DIFF_STAT_LINE_RE = re.compile(
    r"^.+?\s+\|\s+\d+(?P<graph>\s+[+-]+)?\s*$"
)
_DIFF_STAT_TOTAL_LINE_RE = re.compile(
    r"^\s*\d+\s+.*(?:\(\+\)|\(-\))\s*$"
)
_DIFF_STAT_TOTAL_TOKEN_RE = re.compile(
    r"\b\d+\s+[^\d,()]+?\((?P<sign>[+-])\)"
)
_PATCH_SUMMARY_PREFIX = "[PATCH] Proposed changes:"
_PATCH_SUMMARY_HEADER_RE = re.compile(
    rf"^\s*(?:{_TIMESTAMP_PREFIX}\s*)?"
    r"\[PATCH\]\s+Proposed changes:\s*$"
)


def timestamped_status(value: Any, *, now: datetime.datetime | None = None) -> str:
    """Prefix every human-facing status line with the local date and time.

    Existing full-date prefixes are preserved so shell replay does not add a
    second timestamp. Legacy time-only prefixes are upgraded to the full form.
    """
    timestamp = (now or datetime.datetime.now()).strftime("[%Y-%m-%d %H:%M:%S]")
    text = str(value)
    lines = text.splitlines(keepends=True)
    if not lines:
        return f"{timestamp} {text}"
    rendered: list[str] = []
    for line in lines:
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        existing = _TIMESTAMP_RE.match(body)
        if existing is None:
            visible_body = strip_ansi(body)
            visible_existing = _TIMESTAMP_RE.match(visible_body)
            if visible_existing and len(visible_existing.group(1)) == len(
                "[2026-10-28 10:36:18]"
            ):
                rendered.append(f"{body}{ending}")
                continue
        if existing:
            prefix = existing.group(1)
            if len(prefix) == len("[2026-10-28 10:36:18]"):
                rendered.append(f"{body}{ending}")
                continue
            body = body[existing.end():].lstrip()
        rendered.append(f"{timestamp} {body}{ending}")
    return "".join(rendered)

_PREFIX_COLORS = {
    "DEVELOPER": "36",
    "REVIEW": "35",
    "TESTS": "34",
    "RETRY": "33",
    "GIT": "33",
    "PATCH": "95",
    "ERROR": "31",
    "RESULT": "32",
    "SUMMARY": "36",
    "COST": "90",
    "OK": "32",
    "PASSED": "32",
    "WARN": "33",
    "FAIL": "31",
    "FAILED": "31",
    "NOT-STARTED": "31",
    "NO_CHANGES": "33",
    "CANCEL": "33",
    "DOCTOR": "36",
    "INVESTIGATION": "95",
    "AUDIT": "95",
    "BASELINE": "90",
    "DASHBOARD": "36",
    "MANUAL": "36",
    "INIT": "36",
    "CLEAN": "32",
    "SYSTEM": "90",
    "RESUME": "33",
    "DRY RUN": "90",
    "TIMEOUT": "33",
    "BLOCKED": "33",
    "STOP": "33",
    "PUBLISH": "33",
    "SKIP": "33",
}


def _contains_failure(text: str) -> bool:
    text = re.sub(r"(?:[A-Za-z]:)?[^\s]*[\\/][^\s]*", " ", text)
    return bool(re.search(
        r"\b(?:ERROR|FAIL(?:ED|URE)?|REJECTED|BLOCKED|UNAVAILABLE|"
        r"TIM(?:E)?OUT|TIMED\s+OUT|EXIT\s+-?[1-9]\d*)\b",
        text,
        re.IGNORECASE,
    ))


def _contains_success(text: str) -> bool:
    text = re.sub(r"(?:[A-Za-z]:)?[^\s]*[\\/][^\s]*", " ", text)
    return bool(re.search(
        r"\b(?:PASSED|APPROVED|VALIDATED|COMPLETED|SUCCESS|FINISHED|"
        r"CLEAN|EXIT\s+0)\b",
        text,
        re.IGNORECASE,
    ))


def _prefix_color(label: str, line: str) -> str:
    """Choose a stage color without applying it to the message body."""
    if label == "TESTS":
        if _contains_failure(line):
            return "31"
        if _contains_success(line):
            return "32"
    elif label == "GIT":
        if _contains_failure(line):
            return "31"
    elif label == "RESULT":
        return "32" if _contains_success(line) and not _contains_failure(line) else "31"
    elif label == "ERROR":
        return "31"
    return _PREFIX_COLORS.get(label, "90")


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


def _styled_spans(
    line: str,
    *,
    style_status_tokens: bool = True,
    system_prefix: str | None = None,
    system_prefix_text: str | None = None,
) -> list[tuple[int, int, str]]:
    """Find terminal-only spans without styling message bodies or paths."""
    spans: list[tuple[int, int, str]] = []
    timestamp = _TIMESTAMP_RE.match(line)
    if timestamp:
        spans.append((timestamp.start(1), timestamp.end(1), "90"))

    prefix = _PREFIX_RE.match(line) if system_prefix is None else None
    explicit_label = None
    if system_prefix_text is not None and system_prefix is not None:
        expected_text = str(system_prefix_text)
        if expected_text:
            explicit = re.compile(
                rf"^\s*(?:{_TIMESTAMP_PREFIX}\s*)?"
                rf"({re.escape(expected_text)})",
            ).match(line)
            if explicit:
                prefix = explicit
                explicit_label = system_prefix.strip("[]").strip().upper()
    elif system_prefix == "":
        prefix = _SYSTEM_PREFIX_RE.match(line)
    elif system_prefix is not None:
        expected = system_prefix.strip()
        if expected.startswith("[") and expected.endswith("]"):
            expected = expected[1:-1]
        if expected:
            explicit = re.compile(
                rf"^\s*(?:{_TIMESTAMP_PREFIX}\s*)?"
                rf"(\[{re.escape(expected)}\s*\])(?=\s|$)",
                re.IGNORECASE,
            ).match(line)
            if explicit:
                prefix = explicit
    if prefix:
        label = explicit_label or prefix.group(1)[1:-1].strip().upper()
        code = _prefix_color(label, line)
        spans.append((prefix.start(1), prefix.end(1), code))

    if style_status_tokens:
        for prefix in _STRUCTURED_SUFFIX_RE.finditer(line):
            label = prefix.group(1)[1:-1].upper()
            code = _prefix_color(label, line)
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
    return _render_spans(line, spans, stream)


def _render_spans(
    line: str,
    spans: list[tuple[int, int, str]],
    stream: Any = None,
) -> str:
    """Render non-overlapping terminal-only spans in a plain line."""
    output: list[str] = []
    cursor = 0
    for start, end, code in spans:
        output.append(line[cursor:start])
        output.append(paint(line[start:end], code, stream))
        cursor = end
    output.append(line[cursor:])
    return "".join(output)


def _diff_stat_spans(line: str) -> list[tuple[int, int, str]]:
    """Find additions and removals in a recognized Git diff-stat line."""
    spans: list[tuple[int, int, str]] = []
    stat = _DIFF_STAT_LINE_RE.match(line)
    if stat and stat.group("graph"):
        graph_start = stat.start("graph")
        for marker in re.finditer(r"\++|-+", stat.group("graph")):
            start = graph_start + marker.start()
            end = graph_start + marker.end()
            spans.append((start, end, "32" if marker.group().startswith("+") else "31"))

    if _DIFF_STAT_TOTAL_LINE_RE.match(line):
        for total in _DIFF_STAT_TOTAL_TOKEN_RE.finditer(line):
            spans.append((
                total.start(), total.end(),
                "32" if total.group("sign") == "+" else "31",
            ))
    return spans


def _align_diff_stat_line(line: str) -> str:
    """Keep diff-stat rows aligned when an upstream producer drops padding."""
    body = line.rstrip("\r\n")
    if body and not body[0].isspace() and (
        _DIFF_STAT_LINE_RE.match(body) or _DIFF_STAT_TOTAL_LINE_RE.match(body)
    ):
        return " " + line
    return line


def style_patch_summary(value: Any, stream: Any = None) -> str:
    """Style a ``[PATCH]`` message and its Git diff-stat additions/removals."""
    text = strip_ansi(value)
    output: list[str] = []
    for index, line in enumerate(text.splitlines(keepends=True)):
        line = _align_diff_stat_line(line)
        if not color_enabled(stream):
            output.append(line)
            continue
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        spans = _diff_stat_spans(body)
        if index == 0:
            spans.extend(_styled_spans(body, style_status_tokens=False))
        spans.sort()
        output.append(_render_spans(body, spans, stream) + ending)
    return "".join(output)


def _is_diff_stat_line(line: str) -> bool:
    """Return whether a line belongs to a Git diff-stat summary."""
    body = line.rstrip("\r\n")
    return bool(_DIFF_STAT_LINE_RE.match(body) or _DIFF_STAT_TOTAL_LINE_RE.match(body))


def _is_patch_header_prefix(value: str) -> bool:
    """Return whether an incomplete line can still become a patch header."""
    candidate = value.lstrip()
    if _PATCH_SUMMARY_PREFIX.startswith(candidate):
        return True
    timestamp_patterns = (
        ("[dd:dd:dd]", r"\[\d{2}:\d{2}:\d{2}\]"),
        ("[dddd-dd-dd dd:dd:dd]", r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]"),
    )
    for timestamp_shape, timestamp_regex in timestamp_patterns:
        if len(candidate) <= len(timestamp_shape):
            if all(
                character == expected or expected == "d" and character.isdigit()
                for character, expected in zip(candidate, timestamp_shape)
            ):
                return True
            continue
        timestamp = candidate[:len(timestamp_shape)]
        if re.fullmatch(timestamp_regex, timestamp):
            return _PATCH_SUMMARY_PREFIX.startswith(candidate[len(timestamp_shape):].lstrip())
    return False


class PatchSummaryStream:
    """Style patch summaries while preserving arbitrary output chunking."""

    def __init__(self, stream: Any = None, *, style_status_tokens: bool = False) -> None:
        self.stream = stream
        self.style_status_tokens = style_status_tokens
        self._active = False
        self._pending = ""

    def _render_line(self, line: str) -> str:
        line = _align_diff_stat_line(line)
        body = line.rstrip("\r\n")
        if _PATCH_SUMMARY_HEADER_RE.match(body):
            self._active = True
            return style_patch_summary(line, self.stream)
        if self._active and _is_diff_stat_line(body):
            return style_patch_summary(line, self.stream)
        self._active = False
        return style_text(
            line,
            self.stream,
            style_status_tokens=self.style_status_tokens,
        )

    def feed(self, value: Any) -> str:
        """Render one output chunk, retaining only incomplete patch lines."""
        text = self._pending + strip_ansi(value)
        self._pending = ""
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending = lines.pop()

        rendered = "".join(self._render_line(line) for line in lines)
        if self._pending and (
            self._active
            or _is_patch_header_prefix(self._pending)
        ):
            return rendered
        if self._pending:
            rendered += style_text(
                self._pending,
                self.stream,
                style_status_tokens=self.style_status_tokens,
            )
            self._pending = ""
        return rendered

    def flush(self) -> str:
        """Render an incomplete final line, if any."""
        if not self._pending:
            return ""
        pending = self._pending
        self._pending = ""
        return (
            style_patch_summary(pending, self.stream)
            if self._active or _PATCH_SUMMARY_HEADER_RE.match(pending)
            else style_text(
                pending,
                self.stream,
                style_status_tokens=self.style_status_tokens,
            )
        )


def style_shell_text(value: Any, stream: Any = None, *, style_status_tokens: bool = False) -> str:
    """Style shell-replayed output, including complete patch-stat blocks."""
    renderer = PatchSummaryStream(stream, style_status_tokens=style_status_tokens)
    rendered = renderer.feed(value)
    return rendered + renderer.flush()


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


def style_system_text(
    value: Any,
    stream: Any = None,
    *,
    system_prefix: str | None = None,
    system_prefix_text: str | None = None,
    style_status_tokens: bool = True,
) -> str:
    """Style a known system message without classifying its message body.

    ``style_text`` deliberately recognizes only its compatibility allowlist.
    Callers that own a generated prefix can use this helper for a new category
    by supplying ``system_prefix``; the prefix is matched only at the start of
    each rendered line and receives the neutral fallback when no palette entry
    exists.  ``system_prefix_text`` can identify an exact generated prompt when
    its visible text is not bracketed, without classifying arbitrary user text.
    Without an explicit prefix, only the first line accepts a new system label.
    This keeps provider responses and user text after a prefix out of the
    classification boundary.
    """
    text = strip_ansi(value)
    if not color_enabled(stream):
        return text
    output: list[str] = []
    for index, line in enumerate(text.splitlines(keepends=True)):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        if index > 0 and system_prefix is None:
            output.append(body + ending)
            continue
        generated_prefix = system_prefix if system_prefix is not None else ""
        spans = _styled_spans(
            body,
            style_status_tokens=style_status_tokens,
            system_prefix=generated_prefix,
            system_prefix_text=system_prefix_text,
        )
        if not spans:
            output.append(body + ending)
            continue
        rendered: list[str] = []
        cursor = 0
        for start, end, code in sorted(spans):
            rendered.append(body[cursor:start])
            rendered.append(paint(body[start:end], code, stream))
            cursor = end
        rendered.append(body[cursor:])
        output.append("".join(rendered) + ending)
    return "".join(output)


_TRANSIENT_PROGRESS_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}:\d{2}\]\s*)?(?:\[[^\]]+\]\s*)?(?:"
    r"(?:Codex|Gemini|Claude|Copilot) session started|"
    r"Agent is working|Still running \(|Running command:|Using tool:"
    r")",
    re.IGNORECASE,
)
_TRANSIENT_COMPLETION_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}:\d{2}\]\s*)?(?:\[[^\]]+\]\s*)?(?:"
    r"Agent finished its turn(?:\s+\([^)]*\))?|"
    r"(?:Codex|Gemini|Claude|Copilot) agent finished|"
    r"Command finished(?:\s+\(exit 0\))?"
    r")\s*$",
    re.IGNORECASE,
)
_ROUTINE_LOG_START_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}:\d{2}\]\s*)?(?:\[[^\]]+\]\s*)?"
    r"(?:Started \(PID \d+\); progress log: .+|Started \(OpenRouter/.+\))$",
    re.IGNORECASE,
)
_MODIFIED_FILES_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}:\d{2}\]\s*)?(?:\[[^\]]+\]\s*)?"
    r"Modified files:\s*(.*)$",
    re.IGNORECASE,
)
_MODIFIED_COUNT_RE = re.compile(r"\b(\d+)\s+files?\s+changed\b", re.IGNORECASE)
_TESTS_PREFIX_RE = re.compile(
    r"^\s*(?:\[\d{2}:\d{2}:\d{2}\]\s*)?\[TESTS\](?:\s+(?P<body>.*))?$",
    re.IGNORECASE,
)
_TEST_FINISHED_RE = re.compile(
    r"^Finished in \d+(?:\.\d+)?s \(exit (?P<code>-?\d+)\)"
    r"(?: \[TIMEOUT\])?\s*$",
    re.IGNORECASE,
)
_TEST_FAILURE_STATUS_RE = re.compile(
    r"^FAILED(?: \(exit -?\d+\))?\s*$", re.IGNORECASE,
)
_TEST_SUCCESS_STATUS_RE = re.compile(r"^PASSED\s*$", re.IGNORECASE)
_TEST_STRUCTURAL_RE = re.compile(
    r"^(?:Running:|Finished in |PASSED$|FAILED(?: \(exit -?\d+\))?$|"
    r"Overall task deadline reached$|Time limit reached \(|"
    r"All configured checks passed$|git diff --check |Removed extra blank line at EOF:)",
    re.IGNORECASE,
)

TEST_DIAGNOSTIC_MAX_LINES = 80
TEST_DIAGNOSTIC_MAX_CHARS = 12_000
_TEST_DIAGNOSTIC_HEADER = (
    "[TESTS] Failure diagnostics (bounded; use --verbose for full output):\n"
)


def is_transient_progress(value: Any) -> bool:
    """Return whether a provider line can be replaced by live terminal status."""
    text = strip_ansi(value).replace("\r", " ").replace("\n", " ").strip()
    return bool(_TRANSIENT_PROGRESS_RE.match(text) or _TRANSIENT_COMPLETION_RE.match(text))


def is_routine_log_progress(value: Any) -> bool:
    """Return whether a human log line is safe to omit in concise replay."""
    text = strip_ansi(value).replace("\r", " ").replace("\n", " ").strip()
    return bool(is_transient_progress(text) or _ROUTINE_LOG_START_RE.match(text))


class ConciseLogStream:
    """Filter routine runner progress while preserving line-oriented output."""

    def __init__(self) -> None:
        self._pending = ""
        self._modified_paths: set[str] = set()
        self._modified_count = 0
        self._first_transient: str | None = None
        self._last_transient: str | None = None
        self._test_diagnostics: list[str] = []
        self._test_diagnostic_chars = 0
        self._test_diagnostics_truncated = False
        self._test_failure_pending = False

    @staticmethod
    def _test_body(line: str) -> str | None:
        match = _TESTS_PREFIX_RE.match(line.strip())
        if match is None:
            return None
        body = match.group("body")
        return body.strip() if body is not None else ""

    def _remember_test_output(self, line: str) -> None:
        """Keep only a bounded tail for a later failed test command."""
        if len(line) > TEST_DIAGNOSTIC_MAX_CHARS:
            line = line[-TEST_DIAGNOSTIC_MAX_CHARS:]
            self._test_diagnostics_truncated = True
        self._test_diagnostics.append(line)
        self._test_diagnostic_chars += len(line)
        while (
            len(self._test_diagnostics) > TEST_DIAGNOSTIC_MAX_LINES
            or self._test_diagnostic_chars > TEST_DIAGNOSTIC_MAX_CHARS
        ):
            removed = self._test_diagnostics.pop(0)
            self._test_diagnostic_chars -= len(removed)
            self._test_diagnostics_truncated = True

    def _clear_test_output(self) -> None:
        self._test_diagnostics.clear()
        self._test_diagnostic_chars = 0
        self._test_diagnostics_truncated = False
        self._test_failure_pending = False

    def _failure_diagnostics(self) -> str:
        if not self._test_diagnostics:
            self._clear_test_output()
            return ""
        output = _TEST_DIAGNOSTIC_HEADER
        if self._test_diagnostics_truncated:
            output += "[TESTS] Earlier diagnostics omitted by concise replay.\n"
        output += "".join(self._test_diagnostics)
        if not output.endswith(("\n", "\r")):
            output += "\n"
        self._clear_test_output()
        return output

    def _filter_test_line(self, line: str) -> str | None:
        """Filter raw test output while keeping lifecycle and result rows."""
        body = self._test_body(line)
        if body is None:
            return None
        finished = _TEST_FINISHED_RE.match(body)
        if finished:
            failed = int(finished.group("code")) != 0
            if failed:
                self._test_failure_pending = True
                return line
            self._clear_test_output()
            return line
        if _TEST_FAILURE_STATUS_RE.match(body):
            return line + ("" if line.endswith(("\n", "\r")) else "\n") + self._failure_diagnostics()
        if _TEST_SUCCESS_STATUS_RE.match(body):
            self._clear_test_output()
            return line
        if self._test_failure_pending:
            diagnostics = self._failure_diagnostics()
            if _TEST_STRUCTURAL_RE.match(body):
                return diagnostics + line
            return diagnostics
        if _TEST_STRUCTURAL_RE.match(body):
            return line
        self._remember_test_output(line)
        return ""

    def _record_modified_files(self, line: str) -> None:
        match = _MODIFIED_FILES_RE.match(line.strip())
        if not match:
            return
        details = match.group(1).strip()
        count_match = _MODIFIED_COUNT_RE.search(details)
        if count_match:
            self._modified_count = max(self._modified_count, int(count_match.group(1)))
            return
        if details.lower() == "patch applied":
            self._modified_count += 1
            return
        paths = {item.strip() for item in details.split(",") if item.strip()}
        self._modified_paths.update(paths)
        self._modified_count = max(self._modified_count, len(self._modified_paths), 1)

    def _filter_line(self, line: str) -> str:
        body = line.rstrip("\r\n")
        if self._test_body(body) is not None:
            return self._filter_test_line(line) or ""
        diagnostics = self._failure_diagnostics() if self._test_failure_pending else ""
        if _MODIFIED_FILES_RE.match(body.strip()):
            self._record_modified_files(body)
            return diagnostics
        if is_routine_log_progress(body):
            if self._first_transient is None:
                self._first_transient = body.strip()
            self._last_transient = body.strip()
            return diagnostics
        return diagnostics + line

    def take_transient(self) -> str | None:
        """Return the latest routine status observed since the previous call."""
        transient = self._last_transient
        self._first_transient = None
        self._last_transient = None
        return transient

    def take_first_transient(self) -> str | None:
        """Return the first routine status observed since the previous call."""
        transient = self._first_transient
        self._first_transient = None
        self._last_transient = None
        return transient

    def feed(self, value: Any) -> str:
        """Filter complete lines and retain an incomplete trailing line."""
        text = self._pending + strip_ansi(value)
        self._pending = ""
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending = lines.pop()
        return "".join(self._filter_line(line) for line in lines)

    def flush(self) -> str:
        """Flush an incomplete line and emit one file-change summary."""
        output = self._filter_line(self._pending) if self._pending else ""
        self._pending = ""
        if self._test_failure_pending:
            output += self._failure_diagnostics()
        count = max(self._modified_count, len(self._modified_paths))
        if count:
            if output and not output.endswith(("\n", "\r")):
                output += "\n"
            output += f"Modified files: {count} file{'s' if count != 1 else ''} changed\n"
        self._modified_paths.clear()
        self._modified_count = 0
        self._clear_test_output()
        return output
