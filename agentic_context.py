"""Bounded provider context formatting helpers.

These helpers keep repeated issue, feedback, and tool-result context small
without requiring a tokenizer or persisting the original prompt text.
"""
from __future__ import annotations

import json
from typing import Any


# Keep repeated task context small. These are character limits rather than
# token limits because the runner intentionally has no tokenizer dependency.
ISSUE_CONTEXT_CHARS = 4_000
FEEDBACK_CONTEXT_CHARS = 3_500
REPORT_CONTEXT_CHARS = 3_000
OPENROUTER_HISTORY_CHARS = 30_000
OPENROUTER_RECENT_TOOL_MESSAGES = 4
ISSUE_REQUIREMENT_MARKERS = (
    "acceptance", "criteria", "requirement", "must", "should", "expected",
    "definition of done", "non-goal", "scope", "test",
)


def _bounded_text(value: Any, limit: int, *, marker: str = "...[truncated]...") -> str:
    """Bound context while preserving both the beginning and end of a report."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        return marker[:limit]
    available = limit - len(marker)
    head = max(1, int(available * 0.72))
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _issue_context(value: Any, limit: int = ISSUE_CONTEXT_CHARS) -> str:
    """Bound issue text while retaining likely requirements from the middle."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    selected: list[int] = []
    for index, line in enumerate(lines):
        lowered = line.casefold()
        if any(marker in lowered for marker in ISSUE_REQUIREMENT_MARKERS):
            selected.extend(range(max(0, index - 1), min(len(lines), index + 2)))
    selected = list(dict.fromkeys(selected))
    if not selected:
        return _bounded_text(text, limit)
    requirement_lines = []
    for index in selected:
        line = lines[index].strip()
        if not line:
            continue
        lowered = line.casefold()
        line_limit = 500 if any(marker in lowered for marker in ISSUE_REQUIREMENT_MARKERS) else 240
        requirement_lines.append(f"- {_bounded_text(line, line_limit)}")
    requirements = "\n".join(requirement_lines)
    label = "\nImportant requirements and acceptance context:\n"
    separator = "\n...\n"
    available = max(1, limit - len(label) - (2 * len(separator)))
    requirement_budget = min(max(600, limit // 3), available)
    remaining = available - requirement_budget
    head_budget = max(1, int(remaining * 0.58))
    tail_budget = max(1, remaining - head_budget)
    return (
        _bounded_text(text, head_budget) + separator + label.strip("\n") + "\n"
        + _bounded_text(requirements, requirement_budget) + separator
        + _bounded_text(text, tail_budget)
    )


def _compact_feedback(feedback: dict[str, Any], *, limit: int | None = FEEDBACK_CONTEXT_CHARS,
                      max_findings: int | None = 6) -> str:
    """Render bounded investigation/review feedback without dropping its header."""
    lines: list[str] = []
    summary = str(feedback.get("summary", "")).strip()
    if summary:
        lines.append(f"Summary: {_bounded_text(summary, 1_000)}")
    findings = feedback.get("findings", [])
    if isinstance(findings, list) and findings:
        lines.append("Findings:")
        visible_findings = findings if max_findings is None else findings[:max_findings]
        for finding in visible_findings:
            lines.append(f"- {_bounded_text(finding, 500)}")
        if max_findings is not None and len(findings) > max_findings:
            lines.append(f"- ... and {len(findings) - max_findings} more; inspect the recorded result")
    skipped = feedback.get("skipped", [])
    if isinstance(skipped, list) and skipped:
        lines.append("Skipped validation:")
        for item in skipped[:4]:
            lines.append(f"- {_bounded_text(item, 400)}")
    rendered = "\n".join(lines)
    return rendered if limit is None else _bounded_text(rendered, limit)


def _message_context_size(message: dict[str, Any]) -> int:
    size = len(str(message.get("content", "")))
    tool_calls = message.get("tool_calls")
    if tool_calls:
        try:
            size += len(json.dumps(tool_calls, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            size += len(str(tool_calls))
    return size


def _compact_openrouter_history(messages: list[dict[str, Any]]) -> None:
    """Replace old tool output and arguments with reread hints before requests."""
    total = sum(_message_context_size(message) for message in messages)
    if total <= OPENROUTER_HISTORY_CHARS:
        return
    # Tool-call arguments can contain complete files from write_file. Keep the
    # call identity/name for API pairing, but do not resend old file contents.
    for message in messages:
        if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
            continue
        before = _message_context_size(message)
        for tool_call in message["tool_calls"]:
            function = tool_call.get("function") if isinstance(tool_call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                function["arguments"] = "{}"
        total -= before - _message_context_size(message)
        if total <= OPENROUTER_HISTORY_CHARS:
            return
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    replacement = "[Earlier tool output omitted to control context size; call the tool again if needed.]"
    for message in tool_messages[:-OPENROUTER_RECENT_TOOL_MESSAGES]:
        content = message.get("content")
        if isinstance(content, str) and len(content) > len(replacement):
            before = _message_context_size(message)
            message["content"] = replacement
            total -= before - _message_context_size(message)
            if total <= OPENROUTER_HISTORY_CHARS:
                return
    # A single large recent result can still exceed the budget. Truncate the
    # oldest remaining tool messages while preserving the API's call/result
    # pairing and the most recent evidence.
    for message in tool_messages[-OPENROUTER_RECENT_TOOL_MESSAGES:]:
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= len(replacement):
            continue
        before = _message_context_size(message)
        overflow = total - OPENROUTER_HISTORY_CHARS
        keep = max(0, len(content) - overflow - len(replacement))
        message["content"] = content[:keep] + replacement
        total -= before - _message_context_size(message)
        if total <= OPENROUTER_HISTORY_CHARS:
            return
