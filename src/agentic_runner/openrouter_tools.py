"""Scoped OpenRouter tool policy and agent execution."""
from __future__ import annotations

import codecs
from collections import OrderedDict
import http.client
import json
import os
import queue
import signal
import subprocess
import threading
import time
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .execution import _short
from .config import validate_openrouter_base_url
from .file_boundaries import (
    iter_safe_regular_files,
    open_relative_regular,
    revalidate_single_link_regular,
)
from .sandbox import sandbox_command
from .security import redact_secrets
from .policy import default_permission_policy, forbidden, sensitive_read

_DEFAULT_URL_OPEN = urllib.request.urlopen

OPENROUTER_READ_FILE_CHARS = 12_000
OPENROUTER_READ_CHUNK_CHARS = 8_192
OPENROUTER_COMMAND_OUTPUT_CHARS = 6_000
OPENROUTER_COMMAND_READ_CHUNK_BYTES = 4_096
OPENROUTER_COMMAND_DRAIN_TIMEOUT_SECONDS = 1.0
OPENROUTER_LIST_FILES_PAGE_SIZE = 300
# Keep page-number traversal bounded while retaining the existing page API.
OPENROUTER_LIST_FILES_MAX_PAGE = 100
OPENROUTER_LIST_FILES_CACHE_ENTRIES = 8
OPENROUTER_GIT_OBJECT_CHECK_TIMEOUT_SECONDS = 1.0
OPENROUTER_HTTP_RESPONSE_BYTES = 2_000_000
NETWORK_COMMANDS = {"curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "netcat", "telnet", "gh"}
ALWAYS_BLOCKED_COMMANDS = {"gh", "ssh", "scp", "sftp"}
# These programs do not perform a useful check or inspection themselves. They
# can launch another executable with a different policy, so treating their
# argv[0] as the command being authorized is a security-boundary bypass.
LAUNCHER_EXECUTABLES = {
    "busybox", "chrt", "command", "doas", "docker", "env", "exec", "flock",
    "flatpak", "ionice", "nice", "nohup", "nsenter", "podman", "runuser",
    "setsid", "stdbuf", "sudo", "su", "taskset", "timeout", "unshare", "xargs",
}
READONLY_GIT_COMMANDS = {"status", "diff", "grep", "log", "show"}
# Retained as a compatibility-facing import for older integrations. Branch
# inspection is intentionally no longer part of the permission allowlist.
READONLY_BRANCH_OPTIONS: set[str] = set()
READONLY_INSPECTION_COMMANDS = {"rg", "grep", "ls", "pwd", "file"}
# Read-only command policy accepts only bare executable names for allowlisted
# inspection commands. This keeps the name authorized by the policy equal to
# the executable selected by the argv-only process runner.
READONLY_ALLOWLISTED_EXECUTABLES = READONLY_INSPECTION_COMMANDS | {"git"}
INTERPRETER_EXECUTABLES = {
    "node", "nodejs", "perl", "php", "python", "python3", "py", "ruby",
}
INTERPRETER_EVAL_OPTIONS = {"-c", "--command", "-e", "--eval", "-exec"}
GIT_UNSAFE_OPTIONS = {
    "-c", "--config-env", "--exec-path", "--git-dir", "--work-tree", "-C",
    "--output", "-o", "--ext-diff", "--textconv", "--no-index",
}
READONLY_RG_TRAVERSAL_OPTIONS = {
    "--hidden", "--follow", "--no-ignore", "--no-ignore-vcs",
    "--no-ignore-parent", "--no-ignore-dot", "--no-ignore-global",
    "--no-ignore-files", "--glob", "--iglob", "--files-from",
}
READONLY_RECURSIVE_OPTIONS = {
    "-r", "-R", "--recursive", "--dereference-recursive",
}
RG_OPTIONS_WITH_VALUES = {
    "-A", "-B", "-C", "-e", "-f", "-j", "-m", "-M", "-t", "-T",
    "--after-context", "--before-context", "--colors", "--context",
    "--encoding", "--engine", "--file", "--files-from", "--glob", "--iglob",
    "--ignore-file",
    "--max-columns", "--max-count", "--max-depth", "--max-filesize",
    "--path-separator", "--pre", "--pre-glob", "--regexp", "--replace",
    "--sort", "--sortr", "--threads", "--type", "--type-add",
    "--type-clear", "--type-not",
}
RG_PATTERN_OPTIONS = {"-e", "-f", "--regexp", "--file"}
RG_FILE_OPTIONS = {"-f", "--file", "--ignore-file", "--files-from"}
GREP_FILE_OPTIONS = {"-f", "--file", "--exclude-from"}
GIT_PATCH_OPTIONS = {
    "-p", "-u", "--patch", "--full-diff", "-c", "--cc",
    "--patch-with-stat", "--patch-with-raw",
}

def _safe_worktree_path(worktree: Path, relative: str, *, write: bool = False) -> Path:
    """Resolve a model-supplied path without allowing worktree escape."""
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError("path must be a non-empty worktree-relative string")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("path must stay inside the worktree and may not be absolute")
    root = worktree.resolve()
    target = root / candidate
    # The descriptor-based open below is the authoritative race-safe check.
    # Keep this early check for a useful error on an already-visible link.
    current = root
    for part in candidate.parts:
        current /= part
        if current.is_symlink():
            if write:
                raise ValueError("symbolic links are not allowed for worktree writes")
            raise ValueError("path escapes the worktree")
    if write and (target == root / ".git" or ".git" in candidate.parts):
        raise ValueError("writes inside .git are not allowed")
    return target


def _safe_read_worktree_path(
    worktree: Path, relative: str, *, protected_error: str,
) -> Path:
    """Resolve a readable path and apply the API protected-file boundary."""
    root = worktree.resolve()
    if isinstance(relative, str):
        current = root
        for part in Path(relative).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("symbolic links are not allowed for worktree reads")
    target = _safe_worktree_path(worktree, relative)
    normalized = target.relative_to(root).as_posix()
    if sensitive_read(normalized):
        raise ValueError(protected_error)
    return target


def _inspection_path_operands(argv: list[str]) -> list[str]:
    """Return non-option operands that an inspection command may read."""
    executable = Path(argv[0]).name.lower()
    if executable != "git":
        operands: list[str] = []
        after_separator = False
        for part in argv[1:]:
            if part == "--":
                after_separator = True
                continue
            if after_separator or not part.startswith("-"):
                operands.append(part)
        return operands

    subcommand = argv[1] if len(argv) > 1 else ""
    separator = next((index for index, part in enumerate(argv[2:], 2) if part == "--"), None)
    if separator is not None and subcommand in READONLY_GIT_COMMANDS:
        # These commands treat everything after ``--`` as a pathspec.  Keep
        # the operands intact so path resolution also covers names beginning
        # with a dash.  ``git show`` and other Git commands can also receive
        # revision:path expressions before the separator; preserve those
        # expressions for the same protected-file and regular-file checks.
        operands = list(argv[separator + 1:])
        operands.extend(
            part.split(":", 1)[1]
            for part in argv[2:separator]
            if ":" in part and not part.startswith(":") and not part.startswith("-")
        )
        return operands

    # Git's first argument after the executable is its inspection subcommand.
    # A revision:path expression names a tree path rather than a filesystem
    # path, but it can still expose a protected path through ``git show``.
    operands: list[str] = []
    for part in argv[2:]:
        if part.startswith("-"):
            continue
        if part.startswith(":"):
            # Preserve Git pathspec syntax for the authorization layer.  It
            # must not be reduced to a filesystem-looking literal by the
            # revision:path handling below.
            operands.append(part)
            continue
        operands.append(part.split(":", 1)[1] if ":" in part else part)
    return operands


def _rg_path_operands(argv: list[str]) -> list[str] | None:
    """Parse rg paths without mistaking option values for a path operand."""
    operands: list[str] = []
    pattern_seen = False
    files_mode = "--files" in argv[1:]
    index = 1
    after_separator = False
    while index < len(argv):
        part = argv[index]
        if after_separator:
            if not pattern_seen:
                if files_mode:
                    operands.append(part)
                else:
                    pattern_seen = True
            else:
                operands.append(part)
            index += 1
            continue
        if part == "--":
            after_separator = True
            index += 1
            continue
        if part.startswith("-"):
            option = part.split("=", 1)[0]
            if option in RG_OPTIONS_WITH_VALUES:
                if option in RG_PATTERN_OPTIONS:
                    pattern_seen = True
                if "=" not in part:
                    if index + 1 >= len(argv):
                        return None
                    index += 2
                else:
                    index += 1
                continue
            if (part.startswith("-e") and len(part) > 2) or (
                part.startswith("-f") and len(part) > 2
            ):
                pattern_seen = True
            index += 1
            continue
        if files_mode or pattern_seen:
            operands.append(part)
        else:
            pattern_seen = True
        index += 1
    return operands if pattern_seen or files_mode else None


def _option_file_operands(
    argv: list[str], options: set[str], attached_short_options: set[str],
) -> list[str] | None:
    """Return file paths supplied as option values, including short forms."""
    operands: list[str] = []
    index = 1
    while index < len(argv):
        part = argv[index]
        option = part.split("=", 1)[0]
        if option in options:
            if "=" in part:
                value = part.split("=", 1)[1]
            elif index + 1 < len(argv):
                value = argv[index + 1]
                index += 1
            else:
                return None
            if not value:
                return None
            operands.append(value)
        elif any(
            part.startswith(short_option) and len(part) > len(short_option)
            for short_option in attached_short_options
        ):
            operands.append(part[2:])
        index += 1
    return operands


def _git_patch_option(part: str) -> bool:
    """Identify Git options that can make a log command emit file contents."""
    option = part.split("=", 1)[0]
    if option in GIT_PATCH_OPTIONS:
        return True
    # Git accepts combined short options such as ``-pu``.  Keep the check
    # limited to lowercase patch flags so unrelated options are not treated
    # as content-producing forms.
    return option.startswith("-") and not option.startswith("--") and any(
        flag in option[1:] for flag in "puc"
    )


def _git_show_path_is_directory(worktree: Path, argv: list[str], relative: str) -> bool:
    """Reject ``git show REV:path`` when Git resolves the path to a tree."""
    if len(argv) < 3 or argv[1] != "show":
        return False
    specs = [
        part for part in argv[2:]
        if ":" in part and not part.startswith(":") and not part.startswith("-")
    ]
    if not any(spec.split(":", 1)[1] == relative for spec in specs):
        return False
    try:
        for spec in specs:
            if spec.split(":", 1)[1] != relative:
                continue
            object_type = subprocess.run(
                ["git", "cat-file", "-t", spec],
                cwd=worktree,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=OPENROUTER_GIT_OBJECT_CHECK_TIMEOUT_SECONDS,
            )
            if object_type.returncode != 0:
                return True
            if object_type.stdout.strip() != "blob":
                return True
    except (OSError, subprocess.TimeoutExpired):
        return True
    return False


def _readonly_command_paths(
    argv: list[str], worktree: Path | None,
) -> tuple[bool, str]:
    """Apply resolved worktree and protected-read checks to command operands."""
    executable = Path(argv[0]).name.lower()
    operands = _rg_path_operands(argv) if executable == "rg" else _inspection_path_operands(argv)
    if executable == "rg" and (operands is None or not operands):
        return False, "rg must name an explicit file path during read-only review"
    file_options = RG_FILE_OPTIONS if executable == "rg" else GREP_FILE_OPTIONS if executable == "grep" else set()
    option_operands = _option_file_operands(argv, file_options, {"-f"})
    if option_operands is None:
        return False, "inspection command file options must name a path"
    operands = [*(operands or []), *option_operands]
    for part in operands or []:
        # Git pathspec magic (for example ``:(top).env`` or ``:!secret``)
        # changes the meaning of the operand before Git resolves it.  The
        # filesystem resolver cannot safely authorize those expressions, so
        # reject Git's colon-prefixed pathspec syntax rather than treating it
        # as a literal worktree-relative filename.
        if Path(argv[0]).name.lower() == "git" and part.startswith(":"):
            return False, "Git pathspec syntax is not allowed during read-only review"
        if Path(argv[0]).name.lower() == "git" and any(
            marker in part for marker in ("*", "?", "[")
        ):
            return False, "Git wildcard pathspecs are not allowed during read-only review"
        if worktree is None:
            candidate = Path(part)
            if candidate.is_absolute() or ".." in candidate.parts:
                return False, "inspection paths must stay inside the worktree"
            if executable == "rg" and candidate in {Path("."), Path("./")}:
                return False, "rg directory scopes are not allowed during read-only review"
            if sensitive_read(candidate.as_posix()):
                return False, "protected files cannot be inspected"
            continue
        try:
            target = _safe_read_worktree_path(
                worktree, part, protected_error="protected files cannot be inspected",
            )
        except ValueError as exc:
            return False, str(exc)
        git_content_output = (
            executable == "git"
            and (
                argv[1] in {"diff", "grep", "show"}
                or (
                    argv[1] == "log"
                    and any(_git_patch_option(part) for part in argv[2:])
                )
            )
        )
        if executable == "rg" and target.is_dir():
            return False, "rg directory scopes are not allowed during read-only review"
        if git_content_output and not target.is_file():
            return False, "Git pathspecs must resolve to a regular file during read-only review"
        if git_content_output and _git_show_path_is_directory(worktree, argv, part):
            return False, "Git pathspecs may not name historical directories during read-only review"
    return True, ""


def _readonly_git_command_allowed(
    argv: list[str], worktree: Path | None = None,
) -> tuple[bool, str]:
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
    separator = next((index for index, part in enumerate(argv[2:], 2) if part == "--"), None)
    if subcommand == "diff" and (separator is None or not argv[separator + 1:]):
        return False, "git diff must name explicit paths during read-only review"
    if subcommand == "grep" and (separator is None or not argv[separator + 1:]):
        return False, "git grep must name explicit paths during read-only review"
    if subcommand == "show":
        explicit_show_path = any(
            ":" in part and not part.startswith(":")
            for part in argv[2:] if not part.startswith("-")
        ) or bool(separator is not None and argv[separator + 1:])
        if not explicit_show_path:
            return False, "git show must name an explicit path during read-only review"
    if subcommand == "log" and any(_git_patch_option(part) for part in argv[2:]) \
            and (separator is None or not argv[separator + 1:]):
        return False, "git log patch output must name explicit paths during read-only review"
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
    return _readonly_command_paths(argv, worktree)


def _readonly_inspection_command_allowed(
    argv: list[str], worktree: Path | None = None,
) -> tuple[bool, str]:
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
        option = part.split("=", 1)[0]
        if executable == "rg":
            short_flags = option[1:] if option.startswith("-") and not option.startswith("--") else ""
            if (
                option in READONLY_RG_TRAVERSAL_OPTIONS
                or option.startswith(("--hidden=", "--follow=", "--no-ignore-"))
                or any(flag in short_flags for flag in ("u", "L"))
                or short_flags.startswith("g")
            ):
                return False, f"{executable} traversal options are not allowed during read-only review"
        elif executable == "grep":
            short_flags = option[1:] if option.startswith("-") and not option.startswith("--") else ""
            if (
                option in READONLY_RECURSIVE_OPTIONS
                or option in {"-d", "--directories", "--include", "--exclude", "--exclude-dir"}
                or option.startswith(("--directories=", "--include=", "--exclude=", "--exclude-dir="))
                or "r" in short_flags.lower()
            ):
                return False, f"{executable} recursive options are not allowed during read-only review"
        elif executable == "ls":
            short_flags = option[1:] if option.startswith("-") and not option.startswith("--") else ""
            if option == "--recursive" or "R" in short_flags or any(
                flag in short_flags for flag in ("H", "L")
            ):
                return False, f"{executable} traversal options are not allowed during read-only review"
        elif executable == "file" and option in {"-L", "--dereference", "-f", "--files-from"}:
            return False, f"{executable} traversal options are not allowed during read-only review"
    allowed, reason = _readonly_command_paths(argv, worktree)
    if not allowed:
        return False, reason
    return True, ""


def _developer_inspection_command_allowed(
    argv: list[str], worktree: Path | None,
) -> tuple[bool, str]:
    """Apply the same worktree operand boundary to developer inspection."""
    allowed, reason = _readonly_inspection_command_allowed(argv, worktree)
    if not allowed:
        return False, reason
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
                        "maximum": OPENROUTER_LIST_FILES_MAX_PAGE,
                        "description": (
                            "1-based page number, at most "
                            f"{OPENROUTER_LIST_FILES_MAX_PAGE}; request the next page "
                            "when the result says more files exist."
                        ),
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
    path_qualified_executable = (
        Path(argv[0]).name != argv[0]
        or "/" in argv[0]
        or "\\" in argv[0]
    )
    policy = default_permission_policy("reviewer" if readonly else "developer")
    if permission_policy is None:
        policy["network"] = bool(allow_network)
    else:
        policy.update(permission_policy)
        allow_network = bool(policy.get("network"))
    if path_qualified_executable and executable in READONLY_ALLOWLISTED_EXECUTABLES:
        return False, (
            "path-qualified inspection executables are not allowed; "
            "provide a bare trusted command name"
        )
    if executable in LAUNCHER_EXECUTABLES:
        return False, f"launcher executable {executable} is not allowed from an OpenRouter tool call"
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
        return _readonly_git_command_allowed(argv, worktree)
    if readonly:
        if executable in READONLY_INSPECTION_COMMANDS and not policy.get("search_files"):
            return False, "file search is disabled by the permission policy"
        return _readonly_inspection_command_allowed(argv, worktree)
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


def _open_regular_text_stream(root: Path, relative: str):
    """Open a single-link regular file without following a raced symlink."""
    try:
        flags = os.O_RDONLY
        # A nonblocking open prevents a FIFO from hanging before fstat can
        # reject it if the path changes between the lstat and open calls.
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor, expected_identity = open_relative_regular(
            root, relative, flags, description="read_file target",
        )
        try:
            revalidate_single_link_regular(
                descriptor, expected_identity, description="read_file target",
            )
            stream = os.fdopen(descriptor, "r", encoding="utf-8", errors="replace")
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except OSError as exc:
        raise ValueError(f"read_file could not open a regular file: {exc}") from exc
    return stream


def _bounded_file_text(root: Path, relative: str, *, start_line: int | None = None,
                       end_line: int | None = None) -> str:
    """Read only the requested lines and output budget from a text file."""
    marker = "\n...[truncated; use start_line/end_line to read a smaller range]"
    chunks: list[str] = []
    used = 0
    truncated = False
    with _open_regular_text_stream(root, relative) as stream:
        if start_line is None and end_line is None:
            value = stream.read(OPENROUTER_READ_FILE_CHARS + 1)
            truncated = len(value) > OPENROUTER_READ_FILE_CHARS
            text = value[:OPENROUTER_READ_FILE_CHARS]
            return (
                text[:OPENROUTER_READ_FILE_CHARS - len(marker)] + marker
                if truncated else text
            )
        assert start_line is not None and end_line is not None
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


@dataclass
class _FileListIndex:
    """Lazily materialized, protected file listing for one worktree path."""

    target: Path
    root: Path
    paths: list[str] = field(default_factory=list)
    metadata: list[tuple[str, int, int]] = field(default_factory=list)
    walker: Iterator[tuple[str, str, os.stat_result]] | None = None
    complete: bool = False

    def __post_init__(self) -> None:
        # Windows may spell the same temporary directory with an 8.3 path in
        # one caller and its long path in another.  Normalize both roots
        # before resolving children and computing relative paths.
        self.root = self.root.resolve()

    def _advance(self, limit: int) -> None:
        while not self.complete and len(self.paths) < limit:
            if self.walker is None:
                relative = self.target.relative_to(self.root).as_posix() or "."
                self.walker = iter_safe_regular_files(
                    self.root, relative, skip=sensitive_read,
                    max_entries=OPENROUTER_LIST_FILES_MAX_PAGE * OPENROUTER_LIST_FILES_PAGE_SIZE,
                    max_directories=OPENROUTER_LIST_FILES_MAX_PAGE * OPENROUTER_LIST_FILES_PAGE_SIZE,
                )
            try:
                kind, relative, metadata = next(self.walker)
            except StopIteration:
                self.complete = True
                return
            if kind == "directory":
                self.metadata.append((relative, metadata.st_mtime_ns, metadata.st_size))
                continue
            self.paths.append(relative)

    def ensure(self, count: int) -> None:
        """Observe enough entries to answer a page and its continuation flag."""
        while len(self.paths) < count and not self.complete:
            self._advance(count)

    def is_stale(self) -> bool:
        """Check only directories observed by this bounded index so far."""
        for relative, modified, size in self.metadata:
            try:
                current = (self.root / relative).stat()
            except OSError:
                return True
            if current.st_mtime_ns != modified or current.st_size != size:
                return True
        return False


_FILE_LIST_CACHE: OrderedDict[tuple[str, str], _FileListIndex] = OrderedDict()


def _build_file_list_index(
    target: Path, root: Path,
) -> _FileListIndex:
    """Create a lazy, sorted, protected-path-filtered file index."""
    return _FileListIndex(target, root)


def _list_files_page(target: Path, root: Path, page: int) -> tuple[list[str], bool]:
    """Return one deterministic page using a bounded metadata-aware index cache."""
    if page < 1 or page > OPENROUTER_LIST_FILES_MAX_PAGE:
        raise ValueError(
            f"page must be between 1 and {OPENROUTER_LIST_FILES_MAX_PAGE}"
        )
    root = root.resolve()
    target = root / target.relative_to(root)
    key = (str(root), str(target))
    cached = _FILE_LIST_CACHE.get(key)
    if cached is None or cached.is_stale():
        cached = _build_file_list_index(target, root)
        _FILE_LIST_CACHE[key] = cached
        _FILE_LIST_CACHE.move_to_end(key)
        while len(_FILE_LIST_CACHE) > OPENROUTER_LIST_FILES_CACHE_ENTRIES:
            _FILE_LIST_CACHE.popitem(last=False)
    else:
        _FILE_LIST_CACHE.move_to_end(key)
    offset = (page - 1) * OPENROUTER_LIST_FILES_PAGE_SIZE
    end = offset + OPENROUTER_LIST_FILES_PAGE_SIZE
    cached.ensure(end + 1)
    return cached.paths[offset:end], end < len(cached.paths)


def _stop_command_process(process: subprocess.Popen[Any]) -> None:
    """Stop a command and its descendants without leaving the output pipe open."""
    if os.name != "nt":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    else:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False,
            )
        except OSError:
            pass
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass


def _bounded_command_capture(
    process_argv: list[str], worktree: Path, timeout: int | None,
) -> tuple[int, str, bool, bool]:
    """Run a command while retaining only a bounded decoded output tail."""
    popen_kwargs: dict[str, Any] = {
        "cwd": worktree,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "bufsize": 0,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(process_argv, **popen_kwargs)
    if process.stdout is None:
        raise OSError("command output pipe was not created")

    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=1)
    stop_reader = threading.Event()
    stream = process.stdout

    def reader() -> None:
        try:
            while not stop_reader.is_set():
                chunk = stream.read(OPENROUTER_COMMAND_READ_CHUNK_BYTES)
                if not chunk:
                    break
                while not stop_reader.is_set():
                    try:
                        chunks.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError):
            pass
        finally:
            while not stop_reader.is_set():
                try:
                    chunks.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    output_tail = ""
    output_chars = 0
    truncated = False
    timed_out = False
    terminated = False
    reader_done = False
    deadline = time.monotonic() + timeout if timeout is not None else None
    drain_deadline: float | None = None

    def append_output(data: bytes, *, final: bool = False) -> None:
        nonlocal output_tail, output_chars, truncated
        decoded = decoder.decode(data, final=final)
        if not decoded:
            return
        output_chars = min(
            OPENROUTER_COMMAND_OUTPUT_CHARS + 1,
            output_chars + len(decoded),
        )
        if output_chars > OPENROUTER_COMMAND_OUTPUT_CHARS:
            truncated = True
        output_tail = (output_tail + decoded)[-OPENROUTER_COMMAND_OUTPUT_CHARS:]

    try:
        while True:
            now = time.monotonic()
            if not terminated and deadline is not None and now >= deadline:
                timed_out = True
                terminated = True
                drain_deadline = now + OPENROUTER_COMMAND_DRAIN_TIMEOUT_SECONDS
                _stop_command_process(process)
            if terminated and reader_done:
                break
            if terminated and drain_deadline is not None and now >= drain_deadline:
                break
            if not terminated and reader_done and process.poll() is not None:
                break
            wait_for = 0.1
            if deadline is not None and not terminated:
                wait_for = max(0.01, min(wait_for, deadline - now))
            if terminated and drain_deadline is not None:
                wait_for = max(0.01, min(wait_for, drain_deadline - now))
            try:
                chunk = chunks.get(timeout=wait_for)
            except queue.Empty:
                continue
            if chunk is None:
                reader_done = True
                continue
            append_output(chunk)
            if truncated and not terminated:
                terminated = True
                drain_deadline = time.monotonic() + OPENROUTER_COMMAND_DRAIN_TIMEOUT_SECONDS
                _stop_command_process(process)
        if not terminated:
            process.wait()
        else:
            try:
                process.wait(timeout=OPENROUTER_COMMAND_DRAIN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                _stop_command_process(process)
                process.wait()
        if not timed_out:
            append_output(b"", final=True)
    finally:
        # Capture errors must not leave a write-capable command running after
        # the tool call has failed.  The normal overflow/timeout paths have
        # already stopped and waited for the process, but this also covers
        # unexpected pipe or wait failures.
        if process.poll() is None:
            _stop_command_process(process)
        try:
            process.wait(timeout=OPENROUTER_COMMAND_DRAIN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _stop_command_process(process)
            process.wait()
        stop_reader.set()
        stream.close()
        reader_thread.join(timeout=OPENROUTER_COMMAND_DRAIN_TIMEOUT_SECONDS)
    return process.returncode, output_tail, truncated, timed_out


def _openrouter_tool_call(worktree: Path, name: str, arguments: dict[str, Any],
                          *, readonly: bool, command_timeout: int | None = None,
                          allow_network: bool = False,
                          permission_policy: dict[str, bool] | None = None,
                          check_commands: list[list[str]] | None = None,
                          sandbox: dict[str, Any] | None = None,
                          allow_runner_source: bool = False,
                          redaction_values: list[str] | None = None) -> str:
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
            page = arguments.get("page", 1)
            if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                return "ERROR: page must be a positive integer"
            if page > OPENROUTER_LIST_FILES_MAX_PAGE:
                return (
                    "ERROR: page must be at most "
                    f"{OPENROUTER_LIST_FILES_MAX_PAGE}"
                )
            root = worktree.resolve()
            try:
                paths, has_more = _list_files_page(target, root, page)
            except (OSError, RuntimeError, ValueError) as exc:
                return f"ERROR: could not safely list {relative!r}: {exc}"
            result = "\n".join(paths) or "(no files)"
            if has_more:
                result += f"\n...[more files; call list_files with page={page + 1}]"
            return result
        if name == "read_file":
            if not policy.get("read_files"):
                return "ERROR: file reading is disabled by the permission policy"
            relative = arguments.get("path", "")
            try:
                target = _safe_read_worktree_path(
                    worktree, relative, protected_error="protected file cannot be read",
                )
            except ValueError as exc:
                if str(exc) == "protected file cannot be read":
                    return f"ERROR: protected file {relative!r} cannot be read"
                raise
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
            relative_path = target.relative_to(worktree.resolve()).as_posix()
            try:
                return _bounded_file_text(
                    worktree.resolve(), relative_path,
                    start_line=start_line, end_line=end_line,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                return f"ERROR: could not safely read {relative!r}: {exc}"
        if name == "write_file":
            if readonly:
                return "ERROR: write_file is unavailable during review"
            target = _safe_worktree_path(worktree, arguments.get("path", ""), write=True)
            relative = target.relative_to(worktree.resolve()).as_posix()
            relative_parts = [part.lower() for part in Path(relative).parts]
            self_hosted_ci = (
                len(relative_parts) >= 3
                and relative_parts[:2] == [".github", "workflows"]
            )
            if forbidden(relative) and not (
                allow_runner_source and (relative == "agentic.py" or self_hosted_ci)
            ):
                return f"ERROR: protected file {relative!r} cannot be changed"
            content = arguments.get("content", "")
            if not isinstance(content, str):
                return "ERROR: file content must be a string"
            exists = target.exists()
            if exists and not policy.get("edit_files"):
                return "ERROR: editing files is disabled by the permission policy"
            if not exists and not policy.get("create_files"):
                return "ERROR: creating files is disabled by the permission policy"
            flags = os.O_WRONLY
            if policy.get("create_files"):
                flags |= os.O_CREAT
                if not policy.get("edit_files"):
                    flags |= os.O_EXCL
            try:
                descriptor, expected_identity = open_relative_regular(
                    worktree.resolve(), relative, flags, 0o644,
                    create_parents=True, description="write_file target",
                )
            except (OSError, RuntimeError, ValueError) as exc:
                return f"ERROR: could not open {relative!r} for writing: {exc}"
            try:
                revalidate_single_link_regular(
                    descriptor, expected_identity, description="write_file target",
                )
                os.ftruncate(descriptor, 0)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    descriptor = -1
                    revalidate_single_link_regular(
                        stream.fileno(), expected_identity, description="write_file target",
                    )
                    stream.write(content)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
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
                _safe_readonly_git_argv(argv), worktree, sandbox, read_only=readonly,
                pass_env=[], restricted=not allow_network,
            )
            returncode, output, truncated, timed_out = _bounded_command_capture(
                process_argv, worktree, command_timeout,
            )
            if timed_out:
                return f"ERROR: command timed out after {command_timeout}s"
            output = redact_secrets(output, extra_values=redaction_values or ())
            if truncated or len(output) > OPENROUTER_COMMAND_OUTPUT_CHARS:
                marker = "...[command output truncated; showing the tail]\n"
                output = marker + output[-(OPENROUTER_COMMAND_OUTPUT_CHARS - len(marker)):]
            return f"exit {returncode}\n{output}"
        return f"ERROR: unknown tool {name!r}"
    except (OSError, UnicodeError, ValueError) as exc:
        return f"ERROR: {redact_secrets(str(exc), extra_values=redaction_values or ())}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward an Authorization header to a redirected endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, new_url):
        return None


def _read_bounded_response(stream: Any, limit: int) -> tuple[bytes, bool]:
    """Read at most ``limit`` bytes and report whether more were available."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while total <= limit:
        chunk = stream.read(min(65_536, limit + 1 - total))
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise ValueError("OpenRouter response was not a byte stream")
        remaining = limit - total
        if len(chunk) > remaining:
            chunks.append(bytes(chunk[:remaining]))
            truncated = True
            break
        chunks.append(bytes(chunk))
        total += len(chunk)
        if total == limit:
            # One bounded probe distinguishes an exact-size body from an
            # oversized one without ever asking the transport for an
            # unbounded read.
            continue
    return b"".join(chunks), truncated


def _openrouter_request(url: str, api_key: str, body: dict[str, Any],
                        timeout: int | None,
                        redaction_values: list[str] | None = None,
                        *, allow_custom_endpoint: bool = False) -> dict[str, Any]:
    validate_openrouter_base_url(
        url, "OpenRouter request URL", allow_custom_endpoint=allow_custom_endpoint,
    )
    redaction_values = [api_key, *(redaction_values or [])]
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
        # Preserve the historical module-level transport seam for integrations
        # that inject a test/local transport. The real urllib transport uses a
        # no-redirect opener so Authorization cannot cross endpoint boundaries.
        if urllib.request.urlopen is not _DEFAULT_URL_OPEN:
            response_context = (
                urllib.request.urlopen(request, timeout=timeout)
                if timeout else urllib.request.urlopen(request)
            )
        else:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            response_context = opener.open(request, timeout=timeout) if timeout else opener.open(request)
        with response_context as response:
            response_body, truncated = _read_bounded_response(
                response, OPENROUTER_HTTP_RESPONSE_BYTES,
            )
            if truncated:
                raise RuntimeError(
                    f"OpenRouter response exceeded {OPENROUTER_HTTP_RESPONSE_BYTES} bytes"
                )
            value = json.loads(response_body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error_body, truncated = _read_bounded_response(
                exc, OPENROUTER_HTTP_RESPONSE_BYTES,
            )
            detail = error_body.decode("utf-8", errors="replace")[-1000:]
        except (OSError, TypeError, ValueError, http.client.HTTPException):
            detail = "(error body unavailable)"
            truncated = False
        detail = redact_secrets(detail, extra_values=redaction_values or ())
        if truncated:
            detail += " [response body truncated]"
        raise RuntimeError(f"OpenRouter request failed ({exc.code}): {detail}") from exc
    except (
        urllib.error.URLError,
        OSError,
        socket.timeout,
        TimeoutError,
        http.client.HTTPException,
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise RuntimeError(
            "OpenRouter request failed: "
            + redact_secrets(str(exc), extra_values=redaction_values or ())
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("OpenRouter returned a non-object response")
    if value.get("error"):
        raise RuntimeError(f"OpenRouter returned an error: {_short(value['error'], 1000)}")
    return value
