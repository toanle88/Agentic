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
from .guidance import guidance_phase, guidance_prompt_sections
from .persistence import BoundedAppendWriter, atomic_write_text
from .openrouter_tools import (
    OPENROUTER_READ_FILE_SCAN_LIMIT_MARKER,
    OPENROUTER_READ_FILE_TRUNCATION_MARKER,
    OPENROUTER_MALFORMED_TOOL_CALL_ERROR_KEY,
    _openrouter_request,
    _openrouter_tool_call,
    _openrouter_tools,
    _validate_tool_call_shape,
)
from .policy import default_permission_policy
from .output import is_transient_progress, strip_ansi, style_text
from .security import redact_secrets, restricted_test_environment
from .telemetry import combine_usage, normalize_usage, record_operation


OPENROUTER_RAW_LOG_BYTES = 2_000_000
OPENROUTER_RAW_EVENT_BYTES = 256_000
OPENROUTER_LOG_BYTES = 200_000
OPENROUTER_FINAL_MESSAGE_CHARS = 50_000
OPENROUTER_IDENTICAL_TOOL_CALL_LIMIT = 4
OPENROUTER_IDENTICAL_TOOL_CALL_HARD_LIMIT = 6
OPENROUTER_EXPLORATION_TOOLS = frozenset({"list_files", "read_file", "search_files"})
_TRUNCATION_MARKER = "\n[agentic_stream_truncated: size limit reached]\n"
_MALFORMED_TOOL_CALL_NAME = "read_file"


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
                error = "ERROR: tool call must be an object"
                call_id = "call"
                name = _MALFORMED_TOOL_CALL_NAME
                arguments = json.dumps(
                    {OPENROUTER_MALFORMED_TOOL_CALL_ERROR_KEY: error},
                    ensure_ascii=False, separators=(",", ":"),
                )
            else:
                call_id = raw_call.get("id", "call")
                function = raw_call.get("function")
                if not isinstance(function, dict):
                    name = None
                    arguments: Any = None
                else:
                    name = function.get("name")
                    arguments = function.get("arguments", "{}")
                parsed_arguments = arguments
                if isinstance(arguments, (str, bytes, bytearray)):
                    try:
                        parsed_arguments = json.loads(arguments)
                    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                        parsed_arguments = arguments
                error = _validate_tool_call_shape(name, parsed_arguments)
                if error is None:
                    if isinstance(arguments, dict):
                        arguments = json.dumps(
                            arguments, ensure_ascii=False, separators=(",", ":"),
                        )
                    elif isinstance(arguments, (bytes, bytearray)):
                        arguments = json.dumps(
                            parsed_arguments, ensure_ascii=False, separators=(",", ":"),
                        )
                else:
                    arguments = json.dumps(
                        {OPENROUTER_MALFORMED_TOOL_CALL_ERROR_KEY: error},
                        ensure_ascii=False, separators=(",", ":"),
                    )
                    name = _MALFORMED_TOOL_CALL_NAME
            call: dict[str, Any] = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            if not isinstance(call["id"], str) or not call["id"]:
                call["id"] = "call"
            calls.append(call)
    if calls:
        normalized["tool_calls"] = calls
    return normalized


def _tool_call_signature(name: str, arguments: dict[str, Any]) -> str:
    """Return a stable identity for detecting repeated no-progress calls."""
    return json.dumps(
        {"name": name, "arguments": arguments},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _tool_path_key(arguments: dict[str, Any]) -> str | None:
    """Normalize a relative tool path for session-local safety checks."""
    path = arguments.get("path")
    if not isinstance(path, str) or not path:
        return None
    return os.path.normpath(path).replace("\\", "/")


def _configured_command_key(argv: Any) -> tuple[str, ...] | None:
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        return None
    return tuple(argv)


def _phase_tool_error(
    name: str, arguments: dict[str, Any], *, edited: bool, repair_mode: bool,
    check_commands: list[list[str]] | None,
) -> str | None:
    """Keep the developer in an inspect/edit/validate lifecycle."""
    if not edited or repair_mode:
        return None
    if name in OPENROUTER_EXPLORATION_TOOLS:
        return (
            "ERROR: the worktree is in the validate phase after a successful edit. "
            "Run a configured check, make another already-identified edit, or call finish. "
            "Repository exploration is available again only after a failed check."
        )
    if name == "run_command" and check_commands:
        argv_key = _configured_command_key(arguments.get("argv"))
        configured = {_configured_command_key(command) for command in check_commands}
        if argv_key not in configured:
            return (
                "ERROR: after editing, run only one of the configured validation commands "
                "or call finish"
            )
    return None


def _command_succeeded(result: str) -> bool:
    return result.startswith("exit 0\n") or result == "exit 0"


def _command_failed(result: str) -> bool:
    if not result.startswith("exit "):
        return False
    return not result.startswith("exit 0\n") and result != "exit 0"

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
                   credential_names: list[str] | None = None,
                   allow_runner_source: bool = False,
                   json_mode: bool = False,
                   verbose: bool = False,
                   sandbox: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Run an OpenAI-compatible OpenRouter agent with scoped local tools."""
    if not model:
        raise RuntimeError("OpenRouter requires a model, for example openai/gpt-6-luna")
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
    sandbox_credential_names = list((sandbox or {}).get("pass_env", []))
    restricted_environment, removed_values = restricted_test_environment(
        blocked_names=(api_key_env, *(credential_names or ()), *sandbox_credential_names),
    )
    redaction_values = sorted(
        set([api_key, *removed_values]), key=len, reverse=True,
    )
    task_prompt, guidance = guidance_prompt_sections(
        worktree, prompt, readonly=readonly, audit_folder=output.parent,
        phase=guidance_phase(output, readonly=readonly),
    )
    prompt = task_prompt if guidance is None else guidance + "\n\n" + task_prompt
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
    }, {"role": "user", "content": task_prompt}]
    if guidance is not None:
        # Keep repository guidance in a separate bounded message.  Otherwise
        # the combined guidance plus task text can be truncated as one user
        # message, discarding the actual issue before it reaches the model.
        messages.append({"role": "user", "content": guidance})
    tools = _openrouter_tools(readonly, permission_policy)
    started = time.monotonic()
    started_at = datetime.datetime.now(datetime.timezone.utc)
    last_message = ""
    usage_records: list[dict[str, Any]] = []
    mutation_state = {"bytes": 0}
    identical_tool_calls: dict[str, int] = {}
    truncated_read_paths: set[str] = set()
    force_final_response = False
    edited = False
    repair_mode = False
    raw_writer = BoundedAppendWriter(
        raw_file, max_bytes=OPENROUTER_RAW_LOG_BYTES,
        truncation_marker=json.dumps({
            "event": "agentic_stream_truncated",
            "stream": "openrouter_jsonl",
            "reason": "artifact_size_limit",
        }) + "\n",
        redaction_values=redaction_values,
    )
    try:
        log_writer = BoundedAppendWriter(
            log_file, max_bytes=OPENROUTER_LOG_BYTES,
            truncation_marker=_TRUNCATION_MARKER,
            redaction_values=redaction_values,
        )
    except BaseException:
        raw_writer.close()
        raise
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
                raw_writer.write(marker)
                raw_bytes = raw_writer.written_bytes
            raw_truncated = True
            return
        raw_writer.write(serialized)
        raw_bytes = raw_writer.written_bytes

    def write_log(line: str) -> None:
        nonlocal log_bytes, log_truncated
        if log_truncated:
            return
        log_writer.write(line + "\n")
        log_bytes = log_writer.written_bytes
        log_truncated = log_writer.truncated

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

    try:
        emit(f"Started (OpenRouter/{model})", style_status_tokens=True)
        deadline = started + timeout if timeout is not None else None
        for turn in range(max_tool_turns):
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError(f"OpenRouter agent exceeded total timeout of {timeout}s")
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "tools": [] if force_final_response else tools,
                "tool_choice": "none" if force_final_response else "auto",
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
            finished = False
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    name = None
                    raw_arguments = None
                else:
                    name = function.get("name")
                    raw_arguments: Any = function.get("arguments", "{}")
                try:
                    if isinstance(raw_arguments, dict):
                        arguments = raw_arguments
                    elif isinstance(raw_arguments, (str, bytes, bytearray)):
                        arguments = json.loads(raw_arguments)
                    else:
                        arguments = raw_arguments
                except json.JSONDecodeError:
                    arguments = raw_arguments
                shape_error = _validate_tool_call_shape(name, arguments)
                typed_name = name if isinstance(name, str) else ""
                typed_arguments = arguments if isinstance(arguments, dict) else {}
                repeated_tool_call = False
                if shape_error:
                    # Malformed provider input is still sent through the tool
                    # boundary so it produces a bounded result and remains in
                    # the assistant/tool history for the next turn.
                    pass
                elif name in {"write_file", "edit_file"}:
                    # A successful write changes repository state, so earlier
                    # read repetition is no longer evidence of a stuck session.
                    identical_tool_calls.clear()
                else:
                    signature = _tool_call_signature(typed_name, typed_arguments)
                    identical_tool_calls[signature] = identical_tool_calls.get(signature, 0) + 1
                    repeated_count = identical_tool_calls[signature]
                    if repeated_count >= OPENROUTER_IDENTICAL_TOOL_CALL_HARD_LIMIT:
                        if readonly and schema is not None:
                            force_final_response = True
                        else:
                            raise RuntimeError(
                                "OpenRouter agent repeated the same tool call "
                                f"{OPENROUTER_IDENTICAL_TOOL_CALL_LIMIT} times without progress: {name}"
                            )
                    if readonly and schema is not None and repeated_count > OPENROUTER_IDENTICAL_TOOL_CALL_LIMIT:
                        force_final_response = True
                    if force_final_response:
                        repeated_tool_call = True
                    else:
                        repeated_tool_call = repeated_count > OPENROUTER_IDENTICAL_TOOL_CALL_LIMIT
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
                if repeated_tool_call:
                    result = (
                        "ERROR: this exact tool call has already returned a result. "
                        "Stop using tools and return the required final JSON now."
                        if force_final_response else
                        "ERROR: this exact tool call has already returned a result "
                        f"{OPENROUTER_IDENTICAL_TOOL_CALL_LIMIT} times. "
                        "Use a narrower read range, choose a different file, or proceed "
                        "with the evidence already collected."
                    )
                else:
                    if shape_error:
                        phase_error = None
                    else:
                        # Validation above guarantees these provider values have
                        # the concrete shapes required by the phase policy, but
                        # the validator intentionally accepts Any so malformed
                        # calls can be converted into bounded tool errors.
                        assert isinstance(name, str)
                        assert isinstance(arguments, dict)
                        phase_error = _phase_tool_error(
                            name, arguments, edited=edited, repair_mode=repair_mode,
                            check_commands=check_commands,
                        )
                    path_key = _tool_path_key(arguments) if isinstance(arguments, dict) else None
                    if (
                        not phase_error
                        and name == "write_file"
                        and path_key in truncated_read_paths
                    ):
                        result = (
                            "ERROR: read_file returned truncated content for this path. "
                            "Use read_file with start_line/end_line, then use edit_file; "
                            "do not replace the whole file."
                        )
                    elif shape_error:
                        result = _openrouter_tool_call(
                            worktree, name, arguments, readonly=readonly,
                            command_timeout=tool_timeout, allow_network=allow_network,
                            permission_policy=permission_policy,
                            check_commands=check_commands,
                            sandbox=sandbox, allow_runner_source=allow_runner_source,
                            redaction_values=redaction_values,
                            env=restricted_environment, mutation_state=mutation_state,
                        )
                    else:
                        result = phase_error or _openrouter_tool_call(
                            worktree, name, arguments, readonly=readonly,
                            command_timeout=tool_timeout, allow_network=allow_network,
                            permission_policy=permission_policy,
                            check_commands=check_commands,
                            sandbox=sandbox, allow_runner_source=allow_runner_source,
                            redaction_values=redaction_values,
                            env=restricted_environment, mutation_state=mutation_state,
                        )
                    if name == "read_file" and path_key is not None:
                        if (
                            OPENROUTER_READ_FILE_TRUNCATION_MARKER in result
                            or OPENROUTER_READ_FILE_SCAN_LIMIT_MARKER in result
                        ):
                            truncated_read_paths.add(path_key)
                        elif not result.startswith("ERROR:"):
                            truncated_read_paths.discard(path_key)
                record_raw({
                    "event": "tool_result", "turn": turn + 1,
                    "name": name, "result": result,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", f"call-{turn}"),
                    "content": result,
                })
                if name == "write_file" and result.startswith("Wrote "):
                    edited = True
                    repair_mode = False
                elif name == "edit_file" and result.startswith("Edited "):
                    edited = True
                    repair_mode = False
                elif isinstance(name, str) and name in {"write_file", "edit_file"} and result.startswith("ERROR:"):
                    # A failed mutation needs another inspection/repair pass.
                    # Keeping the validate phase here would prevent the agent
                    # from reading the exact context needed to correct an
                    # ambiguous edit_file match.
                    repair_mode = True
                elif name == "run_command" and _command_succeeded(result):
                    repair_mode = False
                elif name == "run_command" and _command_failed(result):
                    repair_mode = True
                if name == "finish" and result.startswith("FINISH: "):
                    last_message = bounded_final_message(result.removeprefix("FINISH: "))
                    emit("Agent: " + _short(
                        redact_secrets(last_message, extra_values=redaction_values), 350,
                    ))
                    finished = True
                    break
            if finished:
                break
        else:
            raise RuntimeError(f"OpenRouter agent exceeded {max_tool_turns} tool turns")
    except RuntimeError as exc:
        error_message = redact_secrets(str(exc), extra_values=redaction_values)
        if last_message:
            error_message += "\nLast assistant message: " + redact_secrets(
                last_message, extra_values=redaction_values,
            )
        message = bounded_final_message(error_message)
        emit("ERROR: " + message, generated_status="ERROR")
        atomic_write_text(output, message, extra_values=redaction_values)
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
        raw_writer.close()
        log_writer.close()
        return False, message
    except BaseException:
        raw_writer.close()
        log_writer.close()
        raise
    seconds = int(time.monotonic() - started)
    emit(f"Finished in {seconds}s (exit 0)", style_status_tokens=True)
    last_message = bounded_final_message(
        redact_secrets(last_message, extra_values=redaction_values)
    )
    atomic_write_text(output, last_message, extra_values=redaction_values)
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
    raw_writer.close()
    log_writer.close()
    return True, last_message
