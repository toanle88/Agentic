"""OpenRouter API agent loop built on scoped local tools."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_OPENROUTER_BASE_URL,
    DEFAULT_OPENROUTER_COMMAND_TIMEOUT,
    OPENROUTER_MAX_TURNS,
)
from .context import compact_openrouter_request
from .execution import (
    _console_print,
    _message_text,
    _record_provider_usage,
    _short,
)
from .guidance import guidance_phase, guidance_prompt
from .persistence import atomic_write_text
from .openrouter_tools import (
    _openrouter_request,
    _openrouter_tool_call,
    _openrouter_tools,
)
from .policy import default_permission_policy
from .output import is_transient_progress, strip_ansi, style_text
from .security import redact_secrets
from .telemetry import combine_usage, normalize_usage, record_operation


OPENROUTER_RAW_LOG_BYTES = 2_000_000
OPENROUTER_RAW_EVENT_BYTES = 256_000
OPENROUTER_LOG_BYTES = 200_000
OPENROUTER_FINAL_MESSAGE_CHARS = 50_000
_TRUNCATION_MARKER = "\n[agentic_stream_truncated: size limit reached]\n"


def _normalized_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    """Keep only chat-protocol fields needed for later tool turns.

    Provider-specific metadata is external input and is not useful to the
    next request. Dropping it here prevents large unknown fields from
    surviving history compaction and consuming the request budget.
    """
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        content = _message_text(content)
    normalized: dict[str, Any] = {"role": "assistant", "content": content}
    calls: list[dict[str, Any]] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                continue
            arguments = function.get("arguments", "{}")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            if not isinstance(arguments, str):
                arguments = "{}"
            call: dict[str, Any] = {
                "id": raw_call.get("id", "call"),
                "type": "function",
                "function": {"name": function["name"], "arguments": arguments},
            }
            if not isinstance(call["id"], str) or not call["id"]:
                call["id"] = "call"
            calls.append(call)
    if calls:
        normalized["tool_calls"] = calls
    return normalized

def run_openrouter(worktree: Path, prompt: str, *, output: Path,
                   readonly: bool = False, schema: Path | None = None,
                   timeout: int | None = None, model: str | None = None,
                   reasoning_effort: str | None = None,
                   api_key_env: str = "OPENROUTER_API_KEY",
                   base_url: str = DEFAULT_OPENROUTER_BASE_URL,
                   allow_custom_endpoint: bool = False,
                   max_tool_turns: int | None = None,
                   command_timeout: int | None = None,
                   allow_network: bool = False,
                   permission_policy: dict[str, bool] | None = None,
                   check_commands: list[list[str]] | None = None,
                   allow_runner_source: bool = False,
                   json_mode: bool = False,
                   verbose: bool = False,
                   sandbox: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Run an OpenAI-compatible OpenRouter agent with scoped local tools."""
    if not model:
        raise RuntimeError("OpenRouter requires a model, for example openai/gpt-5.4")
    role_policy = default_permission_policy("reviewer" if readonly else "developer")
    if permission_policy is None:
        # Preserve the legacy OpenRouter-only switch for old direct callers.
        role_policy["network"] = bool(allow_network)
    else:
        # Once the common policy is supplied, it is the source of truth. Do
        # not require the legacy flag to repeat permission_policy.network.
        role_policy.update(permission_policy)
    permission_policy = role_policy
    allow_network = bool(permission_policy.get("network"))
    max_tool_turns = max_tool_turns or OPENROUTER_MAX_TURNS
    command_timeout = command_timeout or DEFAULT_OPENROUTER_COMMAND_TIMEOUT
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"OpenRouter API key is missing; set ${api_key_env}")
    redaction_values = [api_key]
    prompt = guidance_prompt(
        worktree, prompt, readonly=readonly, audit_folder=output.parent,
        phase=guidance_phase(output, readonly=readonly),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    log_file = output.with_suffix(".log")
    raw_file = output.with_suffix(".jsonl")
    label = "REVIEW" if readonly else "DEVELOPER"
    messages: list[dict[str, Any]] = [{
        "role": "system",
        "content": (
            "You are running inside an isolated Agentic Git worktree. "
            "Use only the supplied tools and keep all paths relative to the worktree. "
            "Never commit, push, publish, or change protected agent files. "
            + ("Network-capable commands are allowed by explicit configuration. "
               if allow_network and not readonly else "Do not access the network. ")
            + ("This is a read-only review: do not edit files or run project code." if readonly else "You may edit files and run the project's checks.")
        ),
    }, {"role": "user", "content": prompt}]
    tools = _openrouter_tools(readonly, permission_policy)
    started = time.monotonic()
    started_at = datetime.datetime.now(datetime.timezone.utc)
    last_message = ""
    usage_records: list[dict[str, Any]] = []
    atomic_write_text(log_file, "")
    atomic_write_text(raw_file, "")
    raw_bytes = 0
    raw_truncated = False
    log_bytes = 0
    log_truncated = False
    live_status = False
    live_status_enabled = bool(getattr(sys.stdout, "isatty", lambda: False)())

    def bounded_final_message(value: str) -> str:
        if len(value) <= OPENROUTER_FINAL_MESSAGE_CHARS:
            return value
        keep = max(0, OPENROUTER_FINAL_MESSAGE_CHARS - len(_TRUNCATION_MARKER))
        return value[:keep] + _TRUNCATION_MARKER

    def record_raw(event: dict[str, Any]) -> None:
        nonlocal raw_bytes, raw_truncated
        if raw_truncated:
            return
        event_name = str(event.get("event", "unknown"))[:80]
        try:
            serialized = redact_secrets(
                json.dumps(event, ensure_ascii=True, default=str),
                extra_values=redaction_values,
            ) + "\n"
        except (TypeError, ValueError):
            serialized = ""
        if not serialized or len(serialized.encode("utf-8")) > OPENROUTER_RAW_EVENT_BYTES:
            serialized = json.dumps({
                "event": "agentic_stream_truncated",
                "stream": "openrouter_event",
                "reason": "event_size_limit",
                "original_event": event_name,
            }) + "\n"
        encoded = serialized.encode("utf-8")
        marker = json.dumps({
            "event": "agentic_stream_truncated",
            "stream": "openrouter_jsonl",
            "reason": "artifact_size_limit",
        }) + "\n"
        marker_bytes = marker.encode("utf-8")
        if raw_bytes + len(encoded) > OPENROUTER_RAW_LOG_BYTES - len(marker_bytes):
            if raw_bytes + len(marker_bytes) <= OPENROUTER_RAW_LOG_BYTES:
                with raw_file.open("a", encoding="utf-8") as handle:
                    handle.write(marker)
                raw_bytes += len(marker_bytes)
            raw_truncated = True
            return
        with raw_file.open("a", encoding="utf-8") as handle:
            handle.write(serialized)
        raw_bytes += len(encoded)

    def write_log(line: str) -> None:
        nonlocal log_bytes, log_truncated
        if log_truncated:
            return
        marker = _TRUNCATION_MARKER
        encoded = (line + "\n").encode("utf-8")
        marker_bytes = marker.encode("utf-8")
        if log_bytes + len(encoded) > OPENROUTER_LOG_BYTES - len(marker_bytes):
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(marker)
            log_bytes += len(marker_bytes)
            log_truncated = True
            return
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        log_bytes += len(encoded)

    def clear_live_status() -> None:
        nonlocal live_status
        if live_status and live_status_enabled:
            print("\r\033[2K", end="", flush=True)
        live_status = False

    def fit_live_status(value: str) -> str:
        width = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)
        text = strip_ansi(value).replace("\r", " ").replace("\n", " ")
        if len(text) <= width - 1:
            return text
        if width <= 4:
            return text[:width]
        return text[:width - 4] + "..."

    def emit(message: str, *, style_status_tokens: bool = False,
             generated_status: str | None = None,
             transient: bool = False) -> None:
        nonlocal live_status
        now = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{now}] [{label}] {message}"
        transient = transient or is_transient_progress(line)
        if generated_status and message.startswith(generated_status):
            status_start = len(line) - len(message)
            rendered = (
                style_text(line[:status_start], style_status_tokens=False)
                + style_text(generated_status, style_status_tokens=True)
                + style_text(line[status_start + len(generated_status):],
                             style_status_tokens=False)
            )
        else:
            rendered = style_text(line, style_status_tokens=style_status_tokens)
        if transient and live_status_enabled:
            clear_live_status()
            rendered = style_text(
                fit_live_status(line), style_status_tokens=style_status_tokens,
            )
            _console_print("\r" + rendered, end="", flush=True)
            live_status = True
        else:
            clear_live_status()
            _console_print(rendered, flush=True)
        if verbose or not transient:
            write_log(line)

    emit(f"Started (OpenRouter/{model})", style_status_tokens=True)
    try:
        deadline = started + timeout if timeout is not None else None
        for turn in range(max_tool_turns):
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError(f"OpenRouter agent exceeded total timeout of {timeout}s")
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
            }
            compact_openrouter_request(messages, tools)
            if schema is not None and json_mode:
                # Structured output support is provider-endpoint dependent on
                # OpenRouter, so enable this only when the configured model is
                # known to support it. Local validation remains authoritative.
                body["response_format"] = {"type": "json_object"}
            if reasoning_effort:
                body["reasoning_effort"] = reasoning_effort
            remaining = None if deadline is None else max(1, int(deadline - time.monotonic()))
            request_url = base_url.rstrip("/") + "/chat/completions"
            if allow_custom_endpoint:
                response = _openrouter_request(
                    request_url, api_key, body, remaining,
                    allow_custom_endpoint=True,
                )
            else:
                # Keep the historical call shape for integrations that patch
                # the request seam without the newer opt-in keyword.
                response = _openrouter_request(request_url, api_key, body, remaining)
            usage_records.append(normalize_usage(response, provider="openrouter"))
            record_raw({"event": "response", "turn": turn + 1, "response": response})
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise RuntimeError("OpenRouter response did not contain a choice")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise RuntimeError("OpenRouter response did not contain a message")
            normalized_message = _normalized_assistant_message(message)
            messages.append(normalized_message)
            tool_calls = normalized_message.get("tool_calls") or []
            content = _message_text(normalized_message.get("content"))
            if content:
                last_message = bounded_final_message(content)
                emit("Agent: " + _short(
                    redact_secrets(content, extra_values=redaction_values), 350,
                ))
            if not tool_calls:
                break
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") or {}
                name = function.get("name", "")
                raw_arguments = function.get("arguments", "{}")
                try:
                    arguments = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments)
                except (TypeError, json.JSONDecodeError):
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                record_raw({
                    "event": "tool_call", "turn": turn + 1,
                    "name": name, "arguments": arguments,
                })
                emit(f"Using tool: {name}", transient=True)
                tool_timeout = command_timeout
                if deadline is not None:
                    remaining_tool_time = max(1, int(deadline - time.monotonic()))
                    tool_timeout = (
                        min(command_timeout, remaining_tool_time)
                        if command_timeout is not None else remaining_tool_time
                    )
                result = _openrouter_tool_call(
                    worktree, name, arguments, readonly=readonly,
                    command_timeout=tool_timeout, allow_network=allow_network,
                    permission_policy=permission_policy,
                    check_commands=check_commands,
                    sandbox=sandbox, allow_runner_source=allow_runner_source,
                    redaction_values=redaction_values,
                )
                record_raw({
                    "event": "tool_result", "turn": turn + 1,
                    "name": name, "result": result,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", f"call-{turn}"),
                    "content": result,
                })
        else:
            raise RuntimeError(f"OpenRouter agent exceeded {max_tool_turns} tool turns")
    except RuntimeError as exc:
        message = bounded_final_message(
            redact_secrets(last_message or str(exc), extra_values=redaction_values)
        )
        emit("ERROR: " + message, generated_status="ERROR")
        atomic_write_text(output, message)
        usage = combine_usage(usage_records, provider="openrouter")
        _record_provider_usage(
            output, readonly, usage, model=model, prompt_chars=len(prompt),
            completion_chars=len(message), turns=len(usage_records),
            duration_seconds=time.monotonic() - started,
            status="failed", exit_code=1,
        )
        record_operation(
            output.parent, label.lower(), output.stem,
            started_at=started_at.isoformat(timespec="milliseconds"),
            finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            duration_seconds=time.monotonic() - started,
            status="failed", exit_code=1,
        )
        return False, message
    seconds = int(time.monotonic() - started)
    emit(f"Finished in {seconds}s (exit 0)", style_status_tokens=True)
    last_message = bounded_final_message(
        redact_secrets(last_message, extra_values=redaction_values)
    )
    atomic_write_text(output, last_message)
    usage = combine_usage(usage_records, provider="openrouter")
    _record_provider_usage(
        output, readonly, usage, model=model, prompt_chars=len(prompt),
        completion_chars=len(last_message), turns=len(usage_records),
        duration_seconds=time.monotonic() - started,
        status="completed", exit_code=0,
    )
    record_operation(
        output.parent, label.lower(), output.stem,
        started_at=started_at.isoformat(timespec="milliseconds"),
        finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="milliseconds"
        ),
        duration_seconds=time.monotonic() - started,
        status="completed", exit_code=0,
    )
    return True, last_message
