"""Scoped OpenRouter tool policy and agent execution."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agentic_execution import _short
from agentic_sandbox import sandbox_command
from agentic_security import redact_secrets
from agentic_policy import default_permission_policy, forbidden, sensitive_read

OPENROUTER_READ_FILE_CHARS = 12_000
OPENROUTER_READ_CHUNK_CHARS = 8_192
OPENROUTER_COMMAND_OUTPUT_CHARS = 6_000
OPENROUTER_LIST_FILES_PAGE_SIZE = 300
NETWORK_COMMANDS = {"curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "netcat", "telnet", "gh"}
ALWAYS_BLOCKED_COMMANDS = {"gh", "ssh", "scp", "sftp"}
READONLY_GIT_COMMANDS = {"status", "diff", "log", "show"}
# Retained as a compatibility-facing import for older integrations. Branch
# inspection is intentionally no longer part of the permission allowlist.
READONLY_BRANCH_OPTIONS: set[str] = set()
READONLY_INSPECTION_COMMANDS = {"rg", "grep", "ls", "pwd", "file"}
INTERPRETER_EXECUTABLES = {
    "node", "nodejs", "perl", "php", "python", "python3", "py", "ruby",
}
INTERPRETER_EVAL_OPTIONS = {"-c", "--command", "-e", "--eval", "-exec"}
GIT_UNSAFE_OPTIONS = {
    "-c", "--config-env", "--exec-path", "--git-dir", "--work-tree", "-C",
    "--output", "-o", "--ext-diff", "--textconv", "--no-index",
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
    """Allow only inspection-oriented Git commands in a worktree.

    The command is run with ``cwd=worktree``.  Options that can redirect Git
    to another repository, execute an external diff, or write output are
    rejected even though the process is not started through a shell.
    """
    if len(argv) < 2 or argv[1].startswith("-"):
        return False, "review Git commands must name an inspection subcommand"
    subcommand = argv[1]
    if subcommand not in READONLY_GIT_COMMANDS:
        return False, f"git {subcommand} is not allowed during read-only review"
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


def _developer_inspection_command_allowed(
    argv: list[str], worktree: Path | None,
) -> tuple[bool, str]:
    """Apply the same worktree operand boundary to developer inspection."""
    allowed, reason = _readonly_inspection_command_allowed(argv)
    if not allowed:
        return False, reason
    if worktree is None:
        return True, ""
    for part in argv[1:]:
        if part.startswith("-"):
            continue
        try:
            _safe_worktree_path(worktree, part)
        except ValueError:
            return False, "inspection paths must stay inside the worktree"
    return True, ""


def _safe_readonly_git_argv(argv: list[str]) -> list[str]:
    """Disable Git helpers before running native read-only inspection."""
    if len(argv) >= 2 and argv[1] in {"diff", "show"}:
        return [argv[0], argv[1], "--no-ext-diff", "--no-textconv", *argv[2:]]
    return list(argv)


def _openrouter_tools(
    readonly: bool, permission_policy: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    policy = permission_policy or default_permission_policy("reviewer" if readonly else "developer")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files below a relative directory in the current worktree.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "Relative directory, default '.'."},
                    "page": {
                        "type": "integer", "minimum": 1,
                        "description": "1-based page number; request the next page when the result says more files exist.",
                    },
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
    if not readonly and (policy.get("edit_files") or policy.get("create_files")):
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


def _openrouter_command_allowed(
    argv: Any, readonly: bool, allow_network: bool = False,
    permission_policy: dict[str, bool] | None = None,
    check_commands: list[list[str]] | None = None,
    worktree: Path | None = None,
) -> tuple[bool, str]:
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
        return False, "argv must be a non-empty array of strings"
    executable = Path(argv[0]).name.lower()
    policy = default_permission_policy("reviewer" if readonly else "developer")
    if permission_policy is None:
        policy["network"] = bool(allow_network)
    else:
        policy.update(permission_policy)
        allow_network = bool(policy.get("network"))
    if executable in {"bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh"}:
        return False, "shell commands are not allowed; provide argv directly"
    if executable in INTERPRETER_EXECUTABLES and any(
        part.split("=", 1)[0] in INTERPRETER_EVAL_OPTIONS for part in argv[1:]
    ):
        return False, f"{executable} code-evaluation flags are not allowed from an OpenRouter tool call"
    if executable in ALWAYS_BLOCKED_COMMANDS:
        return False, f"{executable} is not allowed from an OpenRouter tool call"
    if executable in NETWORK_COMMANDS and not (allow_network and policy.get("network")):
        return False, f"{executable} is not allowed from an OpenRouter tool call"
    if executable == "git":
        if not policy.get("git_read"):
            return False, "Git inspection is disabled by the permission policy"
        # ``git_read`` is intentionally an allowlist, not a denylist. A
        # denylist would leave mutating commands such as ``git add`` and
        # ``git config`` executable as soon as a new subcommand is added.
        return _readonly_git_command_allowed(argv)
    if readonly:
        if executable in READONLY_INSPECTION_COMMANDS and not policy.get("search_files"):
            return False, "file search is disabled by the permission policy"
        return _readonly_inspection_command_allowed(argv)
    if executable in READONLY_INSPECTION_COMMANDS:
        if not policy.get("search_files"):
            return False, "file search is disabled by the permission policy"
        return _developer_inspection_command_allowed(argv, worktree)
    if not policy.get("run_checks"):
        return False, "command execution is disabled by the permission policy"
    if check_commands is not None:
        if not isinstance(check_commands, list) or any(
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
            for command in check_commands
        ):
            return False, "configured checks must be non-empty argv arrays"
        if argv not in check_commands:
            return False, "command is not one of the configured checks"
    elif not (policy.get("edit_files") and policy.get("create_files")):
        return False, (
            "command execution requires edit_files and create_files, "
            "or an exact configured check"
        )
    return True, ""


def _bounded_file_text(path: Path, *, start_line: int | None = None,
                       end_line: int | None = None) -> str:
    """Read only the requested lines and output budget from a text file."""
    marker = "\n...[truncated; use start_line/end_line to read a smaller range]"
    chunks: list[str] = []
    used = 0
    truncated = False
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        if start_line is None and end_line is None:
            value = stream.read(OPENROUTER_READ_FILE_CHARS + 1)
            truncated = len(value) > OPENROUTER_READ_FILE_CHARS
            text = value[:OPENROUTER_READ_FILE_CHARS]
            return (
                text[:OPENROUTER_READ_FILE_CHARS - len(marker)] + marker
                if truncated else text
            )
        line_number = 1
        while True:
            block = stream.read(OPENROUTER_READ_CHUNK_CHARS)
            if not block:
                break
            block_start = 0
            while block_start < len(block):
                newline = block.find("\n", block_start)
                block_end = len(block) if newline < 0 else newline + 1
                if start_line <= line_number <= end_line:
                    segment = block[block_start:block_end]
                    remaining = OPENROUTER_READ_FILE_CHARS - used
                    if len(segment) > remaining:
                        if remaining > 0:
                            chunks.append(segment[:remaining])
                        truncated = True
                        break
                    chunks.append(segment)
                    used += len(segment)
                if newline < 0:
                    break
                line_number += 1
                if line_number > end_line:
                    break
                block_start = block_end
            if truncated or line_number > end_line:
                break
    text = "".join(chunks)
    if truncated:
        text = text[:OPENROUTER_READ_FILE_CHARS - len(marker)] + marker
    return text


def _list_files_page(target: Path, root: Path, page: int) -> tuple[list[str], bool]:
    """Return one deterministic file-list page and whether another exists."""
    offset = (page - 1) * OPENROUTER_LIST_FILES_PAGE_SIZE
    paths: list[str] = []
    seen = 0
    has_more = False
    for current, directories, filenames in os.walk(target, topdown=True, followlinks=False):
        directories[:] = [directory for directory in directories if not sensitive_read(
            (Path(current) / directory).relative_to(root).as_posix()
        )]
        directories.sort()
        filenames.sort()
        current_path = Path(current)
        for filename in filenames:
            item = current_path / filename
            try:
                resolved_item = item.resolve()
                relative_item = resolved_item.relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            if sensitive_read(relative_item) or not resolved_item.is_file():
                continue
            if seen < offset:
                seen += 1
                continue
            if len(paths) == OPENROUTER_LIST_FILES_PAGE_SIZE:
                has_more = True
                break
            paths.append(relative_item)
            seen += 1
        if has_more:
            break
    return paths, has_more


def _openrouter_tool_call(worktree: Path, name: str, arguments: dict[str, Any],
                          *, readonly: bool, command_timeout: int | None = None,
                          allow_network: bool = False,
                          permission_policy: dict[str, bool] | None = None,
                          check_commands: list[list[str]] | None = None,
                          sandbox: dict[str, Any] | None = None,
                          allow_runner_source: bool = False) -> str:
    try:
        policy = default_permission_policy("reviewer" if readonly else "developer")
        if permission_policy is None:
            policy["network"] = bool(allow_network)
        else:
            policy.update(permission_policy)
            allow_network = bool(policy.get("network"))
        if name == "list_files":
            if not policy.get("search_files"):
                return "ERROR: file listing is disabled by the permission policy"
            relative = arguments.get("path", ".")
            target = worktree.resolve() if relative == "." else _safe_worktree_path(worktree, relative)
            if not target.is_dir():
                return f"ERROR: {relative!r} is not a directory"
            page = arguments.get("page", 1)
            if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                return "ERROR: page must be a positive integer"
            root = worktree.resolve()
            paths, has_more = _list_files_page(target, root, page)
            result = "\n".join(paths) or "(no files)"
            if has_more:
                result += f"\n...[more files; call list_files with page={page + 1}]"
            return result
        if name == "read_file":
            if not policy.get("read_files"):
                return "ERROR: file reading is disabled by the permission policy"
            relative = arguments.get("path", "")
            target = _safe_worktree_path(worktree, relative)
            normalized = target.relative_to(worktree.resolve()).as_posix()
            if sensitive_read(normalized):
                return f"ERROR: protected file {relative!r} cannot be read"
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
            return _bounded_file_text(target, start_line=start_line, end_line=end_line)
        if name == "write_file":
            if readonly:
                return "ERROR: write_file is unavailable during review"
            target = _safe_worktree_path(worktree, arguments.get("path", ""), write=True)
            if target.exists() and not policy.get("edit_files"):
                return "ERROR: editing files is disabled by the permission policy"
            if not target.exists() and not policy.get("create_files"):
                return "ERROR: creating files is disabled by the permission policy"
            relative = str(target.relative_to(worktree.resolve()))
            relative_parts = [part.lower() for part in Path(relative).parts]
            self_hosted_ci = (
                len(relative_parts) >= 3
                and relative_parts[:2] == [".github", "workflows"]
            )
            if forbidden(relative) and not (
                allow_runner_source and (relative == "agentic.py" or self_hosted_ci)
            ):
                return f"ERROR: protected file {relative!r} cannot be changed"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(arguments.get("content", ""), encoding="utf-8")
            return f"Wrote {relative}"
        if name == "run_command":
            argv = arguments.get("argv")
            if not isinstance(argv, list):
                return "ERROR: argv must be a non-empty array of strings"
            allowed, reason = _openrouter_command_allowed(
                argv, readonly, allow_network, permission_policy, check_commands, worktree,
            )
            if not allowed:
                return "ERROR: " + reason
            process_argv = sandbox_command(
                _safe_readonly_git_argv(argv), worktree, sandbox, read_only=readonly, pass_env=[],
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
