"""Scoped OpenRouter tool policy and agent execution."""
from __future__ import annotations

import json
import os
import datetime
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agentic_config import (
    DEFAULT_OPENROUTER_BASE_URL,
    DEFAULT_OPENROUTER_COMMAND_TIMEOUT,
    OPENROUTER_MAX_TURNS,
)
from agentic_context import _compact_openrouter_history
from agentic_execution import (
    _console_print,
    _message_text,
    _record_provider_usage,
    _short,
)
from agentic_sandbox import sandbox_command
from agentic_security import redact_secrets
from agentic_telemetry import combine_usage, normalize_usage, record_operation
from agentic_policy import forbidden, sensitive_read

OPENROUTER_READ_FILE_CHARS = 12_000
OPENROUTER_COMMAND_OUTPUT_CHARS = 6_000
NETWORK_COMMANDS = {"curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "netcat", "telnet", "gh"}
ALWAYS_BLOCKED_COMMANDS = {"gh", "ssh", "scp", "sftp"}
READONLY_GIT_COMMANDS = {"status", "diff", "log", "show", "branch", "rev-parse"}
READONLY_INSPECTION_COMMANDS = {"rg", "grep", "ls", "pwd", "file"}
INTERPRETER_EXECUTABLES = {
    "node", "nodejs", "perl", "php", "python", "python3", "py", "ruby",
}
INTERPRETER_EVAL_OPTIONS = {"-c", "--command", "-e", "--eval", "-exec"}
GIT_UNSAFE_OPTIONS = {
    "-c", "--config-env", "--exec-path", "--git-dir", "--work-tree", "-C",
    "--output", "-o", "--ext-diff", "--textconv", "--no-index",
}
READONLY_BRANCH_OPTIONS = {
    "--list", "-l", "--show-current", "-r", "--remotes", "-a", "--all",
    "-v", "-vv", "--verbose",
}

def _safe_worktree_path(worktree: Path, relative: str, *, write: bool = False) -> Path:
    """Resolve a model-supplied path without allowing worktree escape."""
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError("path must be a non-empty worktree-relative string")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("path must stay inside the worktree and may not be absolute")
    root = worktree.resolve()
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise ValueError("path escapes the worktree")
    if write and (target == root / ".git" or ".git" in target.relative_to(root).parts):
        raise ValueError("writes inside .git are not allowed")
    return target


def _readonly_git_command_allowed(argv: list[str]) -> tuple[bool, str]:
    """Allow only inspection-oriented Git commands in a review worktree.

    The command is run with ``cwd=worktree``.  Options that can redirect Git
    to another repository, execute an external diff, or write output are
    rejected even though the process is not started through a shell.
    """
    if len(argv) < 2 or argv[1].startswith("-"):
        return False, "review Git commands must name an inspection subcommand"
    subcommand = argv[1]
    if subcommand not in READONLY_GIT_COMMANDS:
        return False, f"git {subcommand} is not allowed during read-only review"
    if subcommand == "branch" and any(part not in READONLY_BRANCH_OPTIONS for part in argv[2:]):
        return False, "only inspection-oriented git branch options are allowed during read-only review"
    for index, part in enumerate(argv[2:], 2):
        option = part.split("=", 1)[0]
        if option in GIT_UNSAFE_OPTIONS or option.startswith(("--git-dir", "--work-tree", "--output")):
            return False, f"git option {part} is not allowed during read-only review"
        if part in {"--upload-pack", "--receive-pack"} or part.startswith(("--upload-pack=", "--receive-pack=")):
            return False, f"git option {part} is not allowed during read-only review"
        if part.startswith("-") and subcommand == "status" and part not in {
            "--short", "--porcelain", "--branch", "--untracked-files=no",
        }:
            return False, f"git option {part} is not allowed during read-only review"
        if index == 2 and subcommand == "rev-parse" and part in {"--show-toplevel", "--git-dir", "--git-common-dir"}:
            return False, f"git option {part} is not allowed during read-only review"
    return True, ""


def _readonly_inspection_command_allowed(argv: list[str]) -> tuple[bool, str]:
    """Allow bounded, non-mutating text inspection without path escapes."""
    executable = Path(argv[0]).name.lower()
    if executable not in READONLY_INSPECTION_COMMANDS:
        return False, f"{executable} is not allowed during read-only review"
    if any(
        part in {"--pre", "--pre-glob", "--hostname-bin"}
        or part.startswith(("--pre=", "--pre-glob=", "--hostname-bin="))
        for part in argv[1:]
    ):
        return False, f"{executable} execution hooks are not allowed during read-only review"
    for part in argv[1:]:
        if part.startswith("-"):
            continue
        candidate = Path(part)
        if candidate.is_absolute() or ".." in candidate.parts:
            return False, "inspection paths must stay inside the worktree"
    return True, ""


def _openrouter_tools(readonly: bool) -> list[dict[str, Any]]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files below a relative directory in the current worktree.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "Relative directory, default '.'."},
                }},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a UTF-8 text file from the current worktree.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                }, "required": ["path"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "Run a non-interactive argv command in the current worktree; do not use a shell.",
                "parameters": {"type": "object", "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}},
                }, "required": ["argv"]},
            },
        },
    ]
    if not readonly:
        tools.append({
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write complete UTF-8 file content inside the current worktree.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                }, "required": ["path", "content"]},
            },
        })
    return tools


def _openrouter_command_allowed(argv: Any, readonly: bool,
                                allow_network: bool = False) -> tuple[bool, str]:
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
        return False, "argv must be a non-empty array of strings"
    executable = Path(argv[0]).name.lower()
    if executable in {"bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh"}:
        return False, "shell commands are not allowed; provide argv directly"
    if executable in INTERPRETER_EXECUTABLES and any(
        part.split("=", 1)[0] in INTERPRETER_EVAL_OPTIONS for part in argv[1:]
    ):
        return False, f"{executable} code-evaluation flags are not allowed from an OpenRouter tool call"
    if executable in ALWAYS_BLOCKED_COMMANDS:
        return False, f"{executable} is not allowed from an OpenRouter tool call"
    if executable in NETWORK_COMMANDS and not allow_network:
        return False, f"{executable} is not allowed from an OpenRouter tool call"
    if executable == "git":
        if readonly:
            return _readonly_git_command_allowed(argv)
        for part in argv[1:]:
            option = part.split("=", 1)[0]
            if option in GIT_UNSAFE_OPTIONS or option.startswith(("--git-dir", "--work-tree", "--exec-path")):
                return False, f"git option {part} is not allowed"
            if part in {"--upload-pack", "--receive-pack"} or part.startswith(("--upload-pack=", "--receive-pack=")):
                return False, f"git option {part} is not allowed"
        subcommand = argv[1] if len(argv) > 1 else ""
        if subcommand in {"commit", "push", "reset", "clean", "checkout", "switch", "worktree"}:
            return False, f"git {subcommand} is not allowed"
    if readonly:
        return _readonly_inspection_command_allowed(argv)
    return True, ""


def _openrouter_tool_call(worktree: Path, name: str, arguments: dict[str, Any],
                          *, readonly: bool, command_timeout: int | None = None,
                          allow_network: bool = False,
                          sandbox: dict[str, Any] | None = None) -> str:
    try:
        if name == "list_files":
            relative = arguments.get("path", ".")
            target = worktree.resolve() if relative == "." else _safe_worktree_path(worktree, relative)
            if not target.is_dir():
                return f"ERROR: {relative!r} is not a directory"
            paths = []
            for item in sorted(target.rglob("*")):
                try:
                    resolved_item = item.resolve()
                    relative_item = resolved_item.relative_to(worktree.resolve()).as_posix()
                except ValueError:
                    continue
                if sensitive_read(relative_item):
                    continue
                if resolved_item.is_file():
                    paths.append(relative_item)
                if len(paths) >= 300:
                    break
            return "\n".join(paths) or "(no files)"
        if name == "read_file":
            relative = arguments.get("path", "")
            target = _safe_worktree_path(worktree, relative)
            normalized = target.relative_to(worktree.resolve()).as_posix()
            if sensitive_read(normalized):
                return f"ERROR: protected file {relative!r} cannot be read"
            text = target.read_text(encoding="utf-8", errors="replace")
            start_line = arguments.get("start_line")
            end_line = arguments.get("end_line")
            if start_line is not None or end_line is not None:
                if (
                    not isinstance(start_line, int) or isinstance(start_line, bool)
                    or start_line < 1
                    or not isinstance(end_line, int) or isinstance(end_line, bool)
                    or end_line < start_line
                ):
                    return "ERROR: start_line and end_line must be integers with 1 <= start_line <= end_line"
                lines = text.splitlines(keepends=True)
                text = "".join(lines[start_line - 1:end_line])
            if len(text) > OPENROUTER_READ_FILE_CHARS:
                marker = "\n...[truncated; use start_line/end_line to read a smaller range]"
                text = (
                    text[:OPENROUTER_READ_FILE_CHARS - len(marker)] + marker
                )
            return text
        if name == "write_file":
            if readonly:
                return "ERROR: write_file is unavailable during review"
            target = _safe_worktree_path(worktree, arguments.get("path", ""), write=True)
            relative = str(target.relative_to(worktree.resolve()))
            if forbidden(relative):
                return f"ERROR: protected file {relative!r} cannot be changed"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(arguments.get("content", ""), encoding="utf-8")
            return f"Wrote {relative}"
        if name == "run_command":
            argv = arguments.get("argv")
            if not isinstance(argv, list):
                return "ERROR: argv must be a non-empty array of strings"
            allowed, reason = _openrouter_command_allowed(argv, readonly, allow_network)
            if not allowed:
                return "ERROR: " + reason
            process_argv = sandbox_command(
                argv, worktree, sandbox, read_only=readonly, pass_env=[],
            )
            try:
                result = subprocess.run(
                    process_argv, cwd=worktree, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", check=False,
                    timeout=command_timeout,
                )
            except subprocess.TimeoutExpired:
                return f"ERROR: command timed out after {command_timeout}s"
            output = redact_secrets(result.stdout)
            if len(output) > OPENROUTER_COMMAND_OUTPUT_CHARS:
                marker = "...[command output truncated; showing the tail]\n"
                output = (
                    marker + output[-(OPENROUTER_COMMAND_OUTPUT_CHARS - len(marker)):]
                )
            return f"exit {result.returncode}\n{output}"
        return f"ERROR: unknown tool {name!r}"
    except (OSError, UnicodeError, ValueError) as exc:
        return f"ERROR: {redact_secrets(str(exc))}"


def _openrouter_request(url: str, api_key: str, body: dict[str, Any],
                        timeout: int | None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Agentic",
        },
        method="POST",
    )
    try:
        response_context = urllib.request.urlopen(request, timeout=timeout) if timeout else urllib.request.urlopen(request)
        with response_context as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = redact_secrets(exc.read().decode("utf-8", errors="replace")[-1000:])
        raise RuntimeError(f"OpenRouter request failed ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"OpenRouter request failed: {redact_secrets(str(exc))}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("OpenRouter returned a non-object response")
    if value.get("error"):
        raise RuntimeError(f"OpenRouter returned an error: {_short(value['error'], 1000)}")
    return value


def run_openrouter(worktree: Path, prompt: str, *, output: Path,
                   readonly: bool = False, schema: Path | None = None,
                   timeout: int | None = None, model: str | None = None,
                   reasoning_effort: str | None = None,
                   api_key_env: str = "OPENROUTER_API_KEY",
                   base_url: str = DEFAULT_OPENROUTER_BASE_URL,
                   max_tool_turns: int | None = None,
                   command_timeout: int | None = None,
                   allow_network: bool = False,
                   json_mode: bool = False,
                   sandbox: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Run an OpenAI-compatible OpenRouter agent with scoped local tools."""
    if not model:
        raise RuntimeError("OpenRouter requires a model, for example openai/gpt-5.4")
    max_tool_turns = max_tool_turns or OPENROUTER_MAX_TURNS
    command_timeout = command_timeout or DEFAULT_OPENROUTER_COMMAND_TIMEOUT
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"OpenRouter API key is missing; set ${api_key_env}")
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
    tools = _openrouter_tools(readonly)
    started = time.monotonic()
    started_at = datetime.datetime.now(datetime.timezone.utc)
    last_message = ""
    usage_records: list[dict[str, Any]] = []
    log_file.write_text("", encoding="utf-8")
    raw_file.write_text("", encoding="utf-8")

    def emit(message: str) -> None:
        now = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{now}] [{label}] {message}"
        _console_print(line, flush=True)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    emit(f"Started (OpenRouter/{model})")
    try:
        deadline = started + timeout if timeout is not None else None
        for turn in range(max_tool_turns):
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError(f"OpenRouter agent exceeded total timeout of {timeout}s")
            _compact_openrouter_history(messages)
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
            }
            if schema is not None and json_mode:
                # Structured output support is provider-endpoint dependent on
                # OpenRouter, so enable this only when the configured model is
                # known to support it. Local validation remains authoritative.
                body["response_format"] = {"type": "json_object"}
            if reasoning_effort:
                body["reasoning_effort"] = reasoning_effort
            remaining = None if deadline is None else max(1, int(deadline - time.monotonic()))
            response = _openrouter_request(
                base_url.rstrip("/") + "/chat/completions", api_key, body, remaining
            )
            usage_records.append(normalize_usage(response, provider="openrouter"))
            with raw_file.open("a", encoding="utf-8") as handle:
                handle.write(redact_secrets(json.dumps(
                    {"turn": turn + 1, "response": response}, ensure_ascii=False
                )) + "\n")
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise RuntimeError("OpenRouter response did not contain a choice")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise RuntimeError("OpenRouter response did not contain a message")
            messages.append(message)
            tool_calls = message.get("tool_calls") or []
            content = _message_text(message.get("content"))
            if content:
                last_message = content
                emit("Agent: " + _short(redact_secrets(content), 350))
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
                emit(f"Using tool: {name}")
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
                    sandbox=sandbox,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", f"call-{turn}"),
                    "content": result,
                })
        else:
            raise RuntimeError(f"OpenRouter agent exceeded {max_tool_turns} tool turns")
    except RuntimeError as exc:
        message = redact_secrets(last_message or str(exc))
        emit("ERROR: " + message)
        output.write_text(message, encoding="utf-8")
        usage = combine_usage(usage_records, provider="openrouter")
        _record_provider_usage(
            output, readonly, usage, prompt_chars=len(prompt),
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
    emit(f"Finished in {seconds}s (exit 0)")
    last_message = redact_secrets(last_message)
    output.write_text(last_message, encoding="utf-8")
    usage = combine_usage(usage_records, provider="openrouter")
    _record_provider_usage(
        output, readonly, usage, prompt_chars=len(prompt),
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

