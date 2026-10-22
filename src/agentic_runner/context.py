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
INSTRUCTION_CONTEXT_CHARS = 2_000
OPENROUTER_HISTORY_CHARS = 30_000
OPENROUTER_REQUEST_CHARS = 48_000
OPENROUTER_SYSTEM_CHARS = 6_000
OPENROUTER_TASK_CHARS = 12_000
OPENROUTER_TOOL_SCHEMA_CHARS = 8_000
OPENROUTER_RESPONSE_RESERVE_CHARS = 8_000
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


def _finding_parts(finding: Any, index: int) -> tuple[str, str, str]:
    """Return an ID, concise requirement, and action target for one finding."""
    if isinstance(finding, dict):
        identifier = _bounded_text(finding.get("id", ""), 64) or f"F-{index}"
        requirement = str(finding.get("requirement", finding.get("text", "")))
        action_target = str(finding.get("action_target", requirement))
        return identifier, requirement, action_target
    text = str(finding or "")
    return f"F-{index}", text, text


def _compact_finding(finding: Any, index: int, limit: int | None = None) -> str:
    """Render a finding while retaining its identity and action target."""
    if not isinstance(finding, dict):
        text = str(finding or "")
        rendered = "- " + _bounded_text(text, 500)
        if limit is None or len(rendered) <= limit:
            return rendered
        return "- " + text[:max(1, limit - 2)]
    identifier, requirement, action_target = _finding_parts(finding, index)
    prefix = f"- {identifier}: "
    suffix = " (action: "
    closing = ")"
    if limit is None:
        return (
            prefix + _bounded_text(requirement, 400) + suffix
            + _bounded_text(action_target, 220) + closing
        )
    # Stable IDs are mandatory context. If a caller supplies an unusually
    # small budget, retain the full identifier and let the finding block
    # exceed that advisory budget rather than silently changing its identity.
    if limit <= len(prefix):
        return prefix
    detail_limit = limit - len(prefix)
    if detail_limit <= len(suffix) + len(closing):
        return prefix + requirement[:max(1, detail_limit)]
    content_limit = detail_limit - len(suffix) - len(closing)
    requirement_limit = max(1, content_limit // 2)
    action_limit = max(1, content_limit - requirement_limit)
    return (
        prefix + requirement[:requirement_limit]
        + suffix + action_target[:action_limit] + closing
    )


def _minimum_finding_limit(finding: Any, index: int) -> int:
    """Reserve enough room for an ID and one character of each required field."""
    if not isinstance(finding, dict):
        return 3
    identifier, requirement, action_target = _finding_parts(finding, index)
    minimum = len(f"- {identifier}: ")
    if requirement:
        minimum += 1
    if action_target:
        minimum += len(" (action: ") + 1 + len(")")
    return minimum


def _compact_feedback(feedback: dict[str, Any], *, limit: int | None = FEEDBACK_CONTEXT_CHARS) -> str:
    """Render bounded feedback while preserving every finding's identity."""
    lines: list[str] = []
    summary = str(feedback.get("summary", "")).strip()
    findings = feedback.get("findings", [])
    finding_values = findings if isinstance(findings, list) else []
    minimum_limits = [
        _minimum_finding_limit(finding, index)
        for index, finding in enumerate(finding_values, 1)
    ]
    if summary:
        if limit is None:
            lines.append(f"Summary: {_bounded_text(summary, 1_000)}")
        elif not finding_values:
            lines.append(f"Summary: {_bounded_text(summary, min(1_000, max(1, limit)))}")
        else:
            minimum_block = (
                len("Findings:\n") + sum(minimum_limits)
                + max(0, len(minimum_limits) - 1)
            )
            summary_limit = min(1_000, limit - minimum_block - len("Summary: ") - 1)
            if summary_limit > 0:
                lines.append(f"Summary: {_bounded_text(summary, summary_limit)}")
    if finding_values:
        lines.append("Findings:")
        if limit is None:
            lines.extend(
                _compact_finding(finding, index)
                for index, finding in enumerate(finding_values, 1)
            )
        else:
            # Reserve the remaining budget across every finding. The final
            # rendered block is never globally truncated, which keeps the
            # last finding visible when descriptions are large.
            prefix_length = len("\n".join(lines)) + 1
            available = max(0, limit - prefix_length)
            separators = max(0, len(finding_values) - 1)
            minimum_total = sum(minimum_limits) + separators
            if available < minimum_total:
                # There is no way to fit all mandatory IDs and fields in the
                # nominal bound. Preserve the complete finding list and omit
                # optional summary/skip context instead of dropping findings.
                allocations = minimum_limits
            else:
                extra = available - minimum_total
                allocations = [
                    minimum + extra // len(finding_values)
                    + (1 if index <= extra % len(finding_values) else 0)
                    for index, minimum in enumerate(minimum_limits, 1)
                ]
            lines.extend(
                _compact_finding(finding, index, allocations[index - 1])
                for index, finding in enumerate(finding_values, 1)
            )
    skipped = feedback.get("skipped", [])
    if isinstance(skipped, list) and skipped:
        lines.append("Skipped validation:")
        for item in skipped[:4]:
            lines.append(f"- {_bounded_text(item, 400)}")
    rendered = "\n".join(lines)
    if limit is None or len(rendered) <= limit:
        return rendered
    # Findings already received an individual allocation above. If optional
    # summary/skip text consumed the remainder, return the complete finding
    # block and omit only optional context rather than cutting a finding.
    finding_marker = "Findings:\n"
    finding_start = rendered.find(finding_marker)
    if finding_start >= 0:
        finding_end = rendered.find("\nSkipped validation:", finding_start)
        if finding_end < 0:
            finding_end = len(rendered)
        return rendered[:finding_end]
    return _bounded_text(rendered, limit)


def _message_context_size(message: dict[str, Any]) -> int:
    try:
        return len(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return len(str(message))


def _serialized_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return len(str(value))


def _compact_openrouter_tools(
    tools: list[dict[str, Any]], limit: int,
) -> int:
    """Bound serialized tool definitions while retaining callable names."""
    original_size = _serialized_size(tools)
    if original_size <= limit:
        return original_size

    compacted: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            continue
        name = _bounded_text(function.get("name", "tool"), 128)
        candidate = {
            "type": "function",
            "function": {
                "name": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        description = function.get("description")
        if isinstance(description, str) and description:
            candidate["function"]["description"] = _bounded_text(description, 256)
        if _serialized_size(compacted + [candidate]) > limit:
            candidate = {
                "type": "function",
                "function": {
                    "name": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        if _serialized_size(compacted + [candidate]) > limit:
            break
        compacted.append(candidate)
    tools[:] = compacted
    return _serialized_size(tools)


def _compact_openrouter_history(
    messages: list[dict[str, Any]], budget: int = OPENROUTER_HISTORY_CHARS,
) -> None:
    """Compact messages while retaining every assistant/tool pairing."""
    total = sum(_message_context_size(message) for message in messages)
    if total <= budget:
        return
    # Tool-call arguments can contain complete files from write_file. Keep the
    # call identity/name for API pairing, but do not resend old file contents.
    for message in messages:
        if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
            continue
        before = _message_context_size(message)
        for tool_call in message["tool_calls"]:
            function = tool_call.get("function") if isinstance(tool_call, dict) else None
            if not isinstance(tool_call, dict) or not isinstance(function, dict):
                continue
            # Retain only the protocol fields needed to pair this assistant
            # message with its tool result. Provider extensions and oversized
            # arguments are history, not callable state for the next request.
            tool_call["type"] = "function"
            function["name"] = _bounded_text(function.get("name", "tool"), 128)
            function["arguments"] = "{}"
            for key in tuple(tool_call):
                if key not in {"id", "type", "function"}:
                    del tool_call[key]
        total -= before - _message_context_size(message)
        if total <= budget:
            return
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    replacement = "[Earlier tool output omitted to control context size; call the tool again if needed.]"
    for message in tool_messages[:-OPENROUTER_RECENT_TOOL_MESSAGES]:
        content = message.get("content")
        if isinstance(content, str) and len(content) > len(replacement):
            before = _message_context_size(message)
            message["content"] = replacement
            total -= before - _message_context_size(message)
            if total <= budget:
                return
    # A single large recent result can still exceed the budget. Truncate the
    # oldest remaining tool messages while preserving the API's call/result
    # pairing and the most recent evidence.
    for message in tool_messages[-OPENROUTER_RECENT_TOOL_MESSAGES:]:
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= len(replacement):
            continue
        before = _message_context_size(message)
        overflow = total - budget
        keep = max(0, len(content) - overflow - len(replacement))
        message["content"] = content[:keep] + replacement
        total -= before - _message_context_size(message)
        if total <= budget:
            return
    # System/task text can exceed the history target without any tool output.
    # Preserve message order and protocol fields, but bound textual content as
    # the final fallback so callers can rely on the invariant.
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or not content:
            continue
        before = _message_context_size(message)
        overflow = total - budget
        keep = max(0, len(content) - overflow)
        message["content"] = _bounded_text(content, keep) if keep else ""
        total -= before - _message_context_size(message)
        if total <= budget:
            return


def compact_openrouter_request(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
    *, budget: int = OPENROUTER_REQUEST_CHARS,
) -> dict[str, int]:
    """Apply a predictable request budget before an OpenRouter call.

    Character budgets are deliberately used instead of a tokenizer. The
    system/task allocation is retained first, recent tool evidence next, and
    older history is compacted last. Message identities and tool-call/result
    pairing are never removed.
    """
    tool_list = tools if tools is not None else []
    tool_budget = min(max(0, budget), OPENROUTER_TOOL_SCHEMA_CHARS)
    tool_chars = _compact_openrouter_tools(tool_list, tool_budget)
    history_budget = max(
        1_000,
        budget - min(tool_chars, OPENROUTER_TOOL_SCHEMA_CHARS)
        - OPENROUTER_RESPONSE_RESERVE_CHARS,
    )
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if role == "system":
            message["content"] = _bounded_text(content, OPENROUTER_SYSTEM_CHARS)
        elif role == "user":
            message["content"] = _bounded_text(content, OPENROUTER_TASK_CHARS)
    _compact_openrouter_history(messages, budget=history_budget)
    request_chars = _serialized_size(messages) + _serialized_size(tool_list)
    if request_chars > budget:
        # A system/task prompt can exceed the normal history target on its own.
        # Trim the oldest textual evidence without deleting protocol messages.
        overflow = request_chars - budget
        for message in messages:
            content = message.get("content")
            if not isinstance(content, str) or not content:
                continue
            before = _message_context_size(message)
            keep = max(0, len(content) - overflow)
            message["content"] = _bounded_text(content, keep) if keep else ""
            request_chars -= before - _message_context_size(message)
            overflow = request_chars - budget
            if overflow <= 0:
                break
    # Message protocol fields are not normally attacker-sized, but a custom
    # provider may return oversized tool-call metadata. Compact once more so
    # the reported request budget remains true without deleting pairings.
    if request_chars > budget:
        for message in messages:
            if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
                continue
            before = _message_context_size(message)
            compacted_calls = []
            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                compacted_calls.append({
                    "id": call.get("id", "call"),
                    "type": "function",
                    "function": {
                        "name": _bounded_text(function.get("name", "tool"), 64),
                        "arguments": "{}",
                    },
                })
            message["tool_calls"] = compacted_calls
            request_chars -= before - _message_context_size(message)
            if request_chars <= budget:
                break
    return {
        "request_chars": max(0, request_chars),
        "message_chars": sum(_message_context_size(message) for message in messages),
        "tool_schema_chars": tool_chars,
        "budget": budget,
    }
