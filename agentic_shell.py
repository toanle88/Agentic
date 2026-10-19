#!/usr/bin/env python3
"""Interactive background-job supervisor for Agentic.

This module uses only the Python standard library. Agentic remains the source
of truth for worktrees, tests, reviews, and durable run logs; this shell only
supervises child processes and stores a terminal-output copy for inspection.
"""
from __future__ import annotations

import argparse
import codecs
import json
import os
import shlex
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO, cast

from agentic import __version__


HELP = """Commands:

  Run
    /run ISSUE [--profile fast|slow] [--review|--publish|--mark-done|--new]
                                      [--reason "why"] with --mark-done
                                      start a background job
    /run [--profile fast|slow] [--review|--publish|--mark-done|--new]
                                      local mode: paste a task, finish with /done
    /run --task-file PATH [OPTIONS]    local mode: use an existing task file

  Inspect
    /ps [--all]                         show current or persisted jobs
    /logs JOB|PID [LINES]               follow or show captured terminal output
    /list                               list open GitHub issues
    /runs [--json]                      list existing Agentic worktrees
    /status [--json]                    show Agentic worktree status
    /dashboard [OUTPUT]                 generate and open the HTML dashboard

  Setup and cleanup
    /init [OPTIONS]                     initialize target-repository configuration
    /doctor [--json]                    diagnose local prerequisites
    /cleanup --issue N [OPTIONS]        preview or remove clean worktrees
    /version                            show the Agentic version

  Session
    /stop JOB                           stop a running job
    /help                               show this help
    /quit                               leave the shell; running jobs continue

Notes:
  Jobs have no supervisor timeout. Configured Agentic test/Codex timeouts,
  if any, still apply inside the child runner.

  Type / to open autocomplete. Use Up/Down to navigate, Enter or Tab to
  select, and Esc to dismiss. Options and known job IDs are completed too.
"""


HELP_SECTION_COLORS = {
    "Commands:": "1;36",
    "Run": "36",
    "Inspect": "36",
    "Session": "36",
    "Notes:": "1;33",
}


SHELL_COMMANDS = (
    "/run",
    "/ps",
    "/logs",
    "/list",
    "/runs",
    "/stop",
    "/status",
    "/dashboard",
    "/init",
    "/doctor",
    "/cleanup",
    "/version",
    "/help",
    "/quit",
    "/exit",
    "/?",
)
SHELL_OPTIONS = {
    "/run": ("--profile", "--review", "--publish", "--mark-done", "--reason", "--new", "--task-file"),
    "/ps": ("--all",),
    "/runs": ("--json",),
    "/status": ("--json",),
    "/doctor": ("--json",),
    "/cleanup": ("--issue", "--all", "--yes", "--remove-logs"),
    "/init": ("--provider", "--reviewer-provider", "--security-mode", "--sandbox-image", "--agents", "--force"),
}
COMMAND_DESCRIPTIONS: dict[str, str] = {
    "/run": "start a background job or local task",
    "/ps": "show running or persisted background jobs",
    "/logs": "follow or show captured terminal output",
    "/list": "list open GitHub issues",
    "/runs": "list existing Agentic worktrees",
    "/stop": "stop a running background job",
    "/status": "show Agentic worktree and git status",
    "/dashboard": "generate and open the HTML dashboard",
    "/init": "initialize target-repository configuration",
    "/doctor": "diagnose local prerequisites",
    "/cleanup": "preview or remove clean worktrees",
    "/version": "show the Agentic version",
    "/help": "show available commands and usage",
    "/quit": "leave the shell; running jobs continue",
    "/exit": "leave the shell; running jobs continue",
    "/?": "show available commands and usage",
}
OPTION_DESCRIPTIONS: dict[str, str] = {
    "--profile": "execution profile (fast or slow)",
    "--review": "request read-only review",
    "--publish": "open draft PR on success",
    "--mark-done": "record human completion for the current patch",
    "--reason": "reason recorded with manual completion",
    "--new": "start fresh worktree, ignoring cached state",
    "--all": "show jobs across all shell sessions",
    "--json": "output status in JSON format",
    "--task-file": "use an existing local task file",
    "--issue": "select one issue worktree",
    "--remove-logs": "also remove matching run logs",
    "--yes": "confirm cleanup changes",
    "--provider": "developer provider for initialization",
    "--reviewer-provider": "independent reviewer provider",
    "--security-mode": "trusted or isolated execution",
    "--sandbox-image": "container image for isolated execution",
    "--agents": "create a starter AGENTS.md",
    "--force": "replace generated setup files",
    "fast": "run the configured fast checks",
    "slow": "run fast checks plus configured slow checks",
}



def _completion_candidates(line: str, cursor: int | None = None,
                           job_numbers: tuple[int, ...] = (),
                           jobs: dict[int, Any] | None = None) -> list[str]:
    """Return safe slash-command completions for the current input buffer."""
    cursor = len(line) if cursor is None else max(0, min(cursor, len(line)))
    before_cursor = line[:cursor]
    stripped = before_cursor.lstrip()
    if not stripped.startswith("/"):
        return []

    trailing_space = bool(before_cursor and before_cursor[-1].isspace())
    try:
        tokens = shlex.split(before_cursor)
    except ValueError:
        # An unfinished quote should not make completion fail. The shell will
        # still report the syntax error when the user submits the line.
        tokens = before_cursor.split()
    if not tokens:
        return list(SHELL_COMMANDS)

    command = tokens[0].lower()
    if len(tokens) == 1 and not trailing_space:
        partial = tokens[0].lower()
        return [candidate for candidate in SHELL_COMMANDS if candidate.startswith(partial)]

    current = "" if trailing_space else tokens[-1]
    previous = tokens[-1] if trailing_space else (tokens[-2] if len(tokens) > 1 else "")
    if command == "/run" and previous == "--profile":
        values = ("fast", "slow")
        return [value for value in values if value.startswith(current)]

    if command in {"/logs", "/stop"}:
        numbers = sorted(
            job_numbers,
            key=lambda number: (
                not bool(jobs and number in jobs and jobs[number].running), number,
            ),
        )
        values = [str(number) for number in numbers]
        return [value for value in values if value.startswith(current)]

    options = SHELL_OPTIONS.get(command, ())
    return [option for option in options if option.startswith(current)]


def _replace_current_token(line: str, replacement: str) -> str:
    """Replace the whitespace-delimited token at the cursor's end."""
    start = _token_start(line, len(line))
    return line[:start] + replacement


def _token_start(line: str, cursor: int) -> int:
    """Return the start of the token immediately before the cursor."""
    start = max(0, min(cursor, len(line)))
    while start and not line[start - 1].isspace():
        start -= 1
    return start


def _apply_completion(line: str, cursor: int, replacement: str,
                      *, add_space: bool = False) -> tuple[str, int]:
    """Replace the token at the cursor and return the new line and cursor."""
    start = _token_start(line, cursor)
    updated = line[:start] + replacement + line[cursor:]
    new_cursor = start + len(replacement)
    if add_space and new_cursor == len(updated) and not updated.endswith(" "):
        updated += " "
        new_cursor += 1
    return updated, new_cursor


def _describe_candidate(candidate: str, command: str = "",
                        jobs: dict[int, Job] | None = None) -> str:
    """Return a concise description for a command, option, or job candidate."""
    if candidate in COMMAND_DESCRIPTIONS:
        return COMMAND_DESCRIPTIONS[candidate]
    if candidate in OPTION_DESCRIPTIONS:
        return OPTION_DESCRIPTIONS[candidate]
    if candidate.isdigit() and jobs and int(candidate) in jobs:
        job = jobs[int(candidate)]
        state = "RUNNING" if job.running else f"EXIT {job.exit_code if job.exit_code is not None else '?'}"
        return f"{state} (PID {job.pid})"
    return ""


def _completion_items(line: str, cursor: int | None = None,
                      job_numbers: tuple[int, ...] = (),
                      jobs: dict[int, Job] | None = None) -> list[tuple[str, str]]:
    """Return candidates paired with descriptions for the dropdown menu."""
    candidates = _completion_candidates(line, cursor, job_numbers, jobs=jobs)
    tokens = line[:cursor].split() if cursor is not None else line.split()
    command = tokens[0].lower() if tokens else ""
    return [
        (candidate, _describe_candidate(candidate, command, jobs))
        for candidate in candidates
    ]


def _is_mocked_input() -> bool:
    """Return whether builtins.input is currently mocked or replaced."""
    builtins = sys.modules.get("builtins")
    inp = getattr(builtins, "input", None)
    return inp is not None and type(inp).__name__ in {"MagicMock", "Mock"}


def _is_interactive_terminal() -> bool:
    """Return whether stdin and stdout are attached to an interactive terminal."""
    if os.environ.get("AGENTIC_PLAIN") or _is_mocked_input():
        return False
    stdin_tty = getattr(sys.stdin, "isatty", lambda: False)()
    stdout_tty = getattr(sys.stdout, "isatty", lambda: False)()
    return bool(stdin_tty and stdout_tty)


def _enable_windows_ansi() -> None:
    """Enable virtual terminal processing on Windows consoles if supported."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _parse_ansi_sequence(read_next: Callable[[], str | None]) -> tuple[str, str]:
    seq = read_next()
    if seq in {"[", "O"}:
        code = read_next()
        if code == "A":
            return "UP", ""
        if code == "B":
            return "DOWN", ""
        if code == "C":
            return "RIGHT", ""
        if code == "D":
            return "LEFT", ""
        if code == "H":
            return "HOME", ""
        if code == "F":
            return "END", ""
        if code == "Z":
            return "UP", ""
        if code in {"1", "3", "4"}:
            read_next()  # consume trailing '~' if present
            if code == "3":
                return "DELETE", ""
            if code == "1":
                return "HOME", ""
            if code == "4":
                return "END", ""
    return "ESCAPE", "\x1b"


class _KeyReader:
    def __init__(self) -> None:
        self._old_settings = None
        self._posix_fd: int | None = None
        self._posix_decoder: Any = None

    def __enter__(self) -> "_KeyReader":
        if os.name != "nt":
            try:
                import termios
                import tty
                fd = sys.stdin.fileno()
                self._posix_fd = fd
                self._old_settings = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                encoding = getattr(sys.stdin, "encoding", None) or "utf-8"
                self._posix_decoder = codecs.getincrementaldecoder(encoding)(
                    errors="replace"
                )
            except Exception:
                self._old_settings = None
        else:
            self._old_settings = None
            _enable_windows_ansi()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._old_settings is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass

    def _read_posix_char(self) -> str:
        """Read one character without allowing TextIOWrapper to buffer input."""
        fd = self._posix_fd if self._posix_fd is not None else sys.stdin.fileno()
        decoder = self._posix_decoder
        if decoder is None:
            encoding = getattr(sys.stdin, "encoding", None) or "utf-8"
            decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
            self._posix_decoder = decoder

        while True:
            raw = os.read(fd, 1)
            if not raw:
                return decoder.decode(b"", final=True)
            char = decoder.decode(raw, final=False)
            if char:
                return char

    def read_key(self) -> tuple[str, str]:
        if os.name == "nt":
            import msvcrt
            getwch = cast(Callable[[], str], getattr(msvcrt, "getwch"))
            char = getwch()
            if char in {"\r", "\n"}:
                return "ENTER", char
            if char == "\003":
                return "CTRL_C", char
            if char == "\004":
                return "CTRL_D", char
            if char in {"\b", "\x08"}:
                return "BACKSPACE", char
            if char == "\t":
                return "TAB", char
            if char == "\x1b":
                def _read_win() -> str | None:
                    if msvcrt.kbhit():
                        return getwch()
                    for _ in range(10):
                        time.sleep(0.005)
                        if msvcrt.kbhit():
                            return getwch()
                    return None
                return _parse_ansi_sequence(_read_win)
            if char == "\x01":
                return "CTRL_A", char
            if char == "\x05":
                return "CTRL_E", char
            if char == "\x0b":
                return "CTRL_K", char
            if char == "\x15":
                return "CTRL_U", char
            if char == "\x17":
                return "CTRL_W", char
            if char in {"\x00", "\xe0"}:
                code = getwch()
                if code == "H":
                    return "UP", ""
                if code == "P":
                    return "DOWN", ""
                if code == "K":
                    return "LEFT", ""
                if code == "M":
                    return "RIGHT", ""
                if code == "G":
                    return "HOME", ""
                if code == "O":
                    return "END", ""
                if code == "S":
                    return "DELETE", ""
                if code == "\x0f":
                    return "UP", ""
                return "SPECIAL", code
            return "CHAR", char

        import select
        fd = self._posix_fd if self._posix_fd is not None else sys.stdin.fileno()
        char = self._read_posix_char()
        if not char:
            return "CTRL_D", ""
        if char in {"\r", "\n"}:
            return "ENTER", char
        if char == "\003":
            return "CTRL_C", char
        if char == "\004":
            return "CTRL_D", char
        if char in {"\b", "\x7f", "\x08"}:
            return "BACKSPACE", char
        if char == "\t":
            return "TAB", char
        if char == "\x01":
            return "CTRL_A", char
        if char == "\x05":
            return "CTRL_E", char
        if char == "\x0b":
            return "CTRL_K", char
        if char == "\x15":
            return "CTRL_U", char
        if char == "\x17":
            return "CTRL_W", char
        if char == "\x1b":
            def _read_posix() -> str | None:
                r, _, _ = select.select([fd], [], [], 0.05)
                return self._read_posix_char() if r else None
            return _parse_ansi_sequence(_read_posix)
        return "CHAR", char



def _interactive_input(
    prompt: str,
    completer: Callable[[str, int | None], list[tuple[str, str]]],
    history: list[str] | None = None,
    key_reader_factory: Callable[[], Any] | None = None,
) -> str:
    """Read a shell line with live dropdown autocomplete, colors, and navigation."""
    line = ""
    cursor_pos = 0
    selected_index = 0
    navigated = False
    dismissed = False
    prev_dropdown_lines = 0
    candidates: list[tuple[str, str]] = []

    hist_entries = list(history) if history else []
    hist_index = len(hist_entries)
    saved_input = ""

    def render() -> None:
        nonlocal prev_dropdown_lines, selected_index, navigated, candidates
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        lines_avail = shutil.get_terminal_size(fallback=(80, 24)).lines

        if not dismissed and line.lstrip().startswith("/"):
            try:
                candidates = completer(line, cursor_pos)
            except Exception:
                candidates = []
        else:
            candidates = []

        if not candidates:
            selected_index = 0
            navigated = False
            visible_items: list[tuple[str, str]] = []
            visible_selected = -1
        else:
            selected_index = max(0, min(selected_index, len(candidates) - 1))
            max_visible = min(8, max(2, lines_avail - 4), len(candidates))
            window_start = max(0, min(selected_index - max_visible // 2, len(candidates) - max_visible))
            visible_items = candidates[window_start : window_start + max_visible]
            visible_selected = selected_index - window_start

        new_dropdown_lines = len(visible_items)
        use_color = not (os.environ.get("NO_COLOR") or os.environ.get("AGENTIC_PLAIN"))

        dropdown_strings: list[str] = []
        if visible_items:
            cmd_col = min(24, max((len(c) for c, _ in visible_items), default=12) + 2)
            max_content_len = 0
            for idx, (cmd, desc) in enumerate(visible_items):
                is_active = (idx == visible_selected)
                prefix = "> " if is_active else "  "
                raw_len = len(prefix) + cmd_col + 2 + len(desc)
                if raw_len > max_content_len:
                    max_content_len = raw_len

            banner_width = min(columns - 2, max(50, max_content_len + 2))

            for idx, (cmd, desc) in enumerate(visible_items):
                is_active = (idx == visible_selected)
                prefix = "> " if is_active else "  "
                desc_col = max(10, columns - len(prefix) - cmd_col - 4)
                desc_text = desc if len(desc) <= desc_col else (desc[:desc_col - 3] + "...")

                if use_color:
                    if is_active:
                        content = f"{prefix}{cmd:<{cmd_col}}  {desc_text}".ljust(banner_width)
                        if len(content) > columns - 1:
                            content = content[:columns - 1]
                        dropdown_strings.append(f"\033[44;97m{content}\033[0m")
                    else:
                        line_str = f"{prefix}\033[97m{cmd:<{cmd_col}}\033[0m  \033[90m{desc_text}\033[0m"
                        dropdown_strings.append(line_str)
                else:
                    content = f"{prefix}{cmd:<{cmd_col}}  {desc_text}".ljust(banner_width)
                    if len(content) > columns - 1:
                        content = content[:columns - 1]
                    dropdown_strings.append(content)

        buf: list[str] = []
        display_prompt = _shell_paint(prompt, "1;36")
        buf.append(f"\r\033[2K{display_prompt}{line}")
        for s in dropdown_strings:
            buf.append(f"\n\033[2K{s}")
        if prev_dropdown_lines > new_dropdown_lines:
            for _ in range(prev_dropdown_lines - new_dropdown_lines):
                buf.append("\n\033[2K")
        total_down = max(new_dropdown_lines, prev_dropdown_lines)
        if total_down > 0:
            buf.append(f"\033[{total_down}A")
        cursor_col = len(prompt) + cursor_pos + 1
        buf.append(f"\033[{cursor_col}G")

        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        prev_dropdown_lines = new_dropdown_lines

    def clear_dropdown() -> None:
        nonlocal prev_dropdown_lines
        if prev_dropdown_lines > 0:
            buf = []
            for _ in range(prev_dropdown_lines):
                buf.append("\n\033[2K")
            buf.append(f"\033[{prev_dropdown_lines}A")
            display_prompt = _shell_paint(prompt, "1;36")
            buf.append(f"\r\033[2K{display_prompt}{line}")
            cursor_col = len(prompt) + cursor_pos + 1
            buf.append(f"\033[{cursor_col}G")
            sys.stdout.write("".join(buf))
            sys.stdout.flush()
            prev_dropdown_lines = 0

    reader_factory = key_reader_factory or _KeyReader
    with reader_factory() as reader:
        render()
        try:
            while True:
                key, char = reader.read_key()

                if key == "ENTER":
                    current_token = line[_token_start(line, cursor_pos):cursor_pos]
                    if candidates and (
                        navigated
                        or (current_token and current_token != candidates[selected_index][0])
                    ):
                        candidate = candidates[selected_index][0]
                        line, cursor_pos = _apply_completion(
                            line,
                            cursor_pos,
                            candidate,
                            add_space=candidate in {"/run", "/logs", "/stop", "--profile"},
                        )
                        navigated = False
                        render()
                        continue
                    clear_dropdown()
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return line

                if key == "TAB":
                    if candidates:
                        candidate = candidates[selected_index][0]
                        line, cursor_pos = _apply_completion(
                            line, cursor_pos, candidate, add_space=True
                        )
                        navigated = False
                        render()
                        continue
                    render()
                    continue

                if key == "DOWN":
                    if candidates:
                        selected_index = (selected_index + 1) % len(candidates)
                        navigated = True
                    elif hist_entries:
                        if hist_index < len(hist_entries) - 1:
                            hist_index += 1
                            line = hist_entries[hist_index]
                            cursor_pos = len(line)
                        elif hist_index == len(hist_entries) - 1:
                            hist_index = len(hist_entries)
                            line = saved_input
                            cursor_pos = len(line)
                    render()
                    continue

                if key == "UP":
                    if candidates:
                        selected_index = (selected_index - 1) % len(candidates)
                        navigated = True
                    elif hist_entries:
                        if hist_index == len(hist_entries):
                            saved_input = line
                        if hist_index > 0:
                            hist_index -= 1
                            line = hist_entries[hist_index]
                            cursor_pos = len(line)
                    render()
                    continue

                if key == "LEFT":
                    if cursor_pos > 0:
                        cursor_pos -= 1
                    render()
                    continue

                if key == "RIGHT":
                    if cursor_pos < len(line):
                        cursor_pos += 1
                    render()
                    continue

                if key in {"HOME", "CTRL_A"}:
                    cursor_pos = 0
                    render()
                    continue

                if key in {"END", "CTRL_E"}:
                    cursor_pos = len(line)
                    render()
                    continue

                if key == "BACKSPACE":
                    if cursor_pos > 0:
                        line = line[:cursor_pos - 1] + line[cursor_pos:]
                        cursor_pos -= 1
                        dismissed = False
                        selected_index = 0
                        navigated = False
                    render()
                    continue

                if key == "DELETE":
                    if cursor_pos < len(line):
                        line = line[:cursor_pos] + line[cursor_pos + 1:]
                        dismissed = False
                        selected_index = 0
                        navigated = False
                    render()
                    continue

                if key == "ESCAPE":
                    if candidates:
                        dismissed = True
                        clear_dropdown()
                    render()
                    continue

                if key == "CTRL_C":
                    clear_dropdown()
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    raise KeyboardInterrupt

                if key == "CTRL_D":
                    if not line:
                        clear_dropdown()
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        raise EOFError
                    render()
                    continue

                if key == "CTRL_U":
                    line = line[cursor_pos:]
                    cursor_pos = 0
                    dismissed = False
                    selected_index = 0
                    navigated = False
                    render()
                    continue

                if key == "CTRL_K":
                    line = line[:cursor_pos]
                    dismissed = False
                    selected_index = 0
                    navigated = False
                    render()
                    continue

                if key == "CTRL_W":
                    if cursor_pos > 0:
                        end = cursor_pos
                        while end > 0 and line[end - 1].isspace():
                            end -= 1
                        start = end
                        while start > 0 and not line[start - 1].isspace():
                            start -= 1
                        line = line[:start] + line[cursor_pos:]
                        cursor_pos = start
                        dismissed = False
                        selected_index = 0
                        navigated = False
                        render()
                    continue

                if key == "CHAR":
                    line = line[:cursor_pos] + char + line[cursor_pos:]
                    cursor_pos += len(char)
                    dismissed = False
                    selected_index = 0
                    navigated = False
                    render()
                    continue

        finally:
            clear_dropdown()



def _windows_input(prompt: str, completer: Callable[[str, int], list[str]]) -> str:
    """Read one editable line with Tab completion on native Windows consoles."""
    import msvcrt

    # Pyright's bundled msvcrt stubs do not expose getwch, although it is
    # available in the Windows runtime. Keep the dynamic lookup local to the
    # Windows-only fallback and give the call its actual signature.
    getwch = cast(Callable[[], str], getattr(msvcrt, "getwch"))
    line = ""
    print(prompt, end="", flush=True)
    while True:
        character = getwch()
        if character in {"\r", "\n"}:
            print()
            return line
        if character == "\003":
            print()
            raise KeyboardInterrupt
        if character == "\b":
            if line:
                line = line[:-1]
                print("\b \b", end="", flush=True)
            continue
        if character == "\t":
            matches = completer(line, len(line))
            if not matches:
                continue
            current_start = len(line)
            while current_start and not line[current_start - 1].isspace():
                current_start -= 1
            current = line[current_start:]
            common = matches[0]
            for match in matches[1:]:
                common_length = 0
                for left, right in zip(common, match):
                    if left != right:
                        break
                    common_length += 1
                common = common[:common_length]
            if len(common) > len(current):
                line = _replace_current_token(line, common)
                print("\r" + prompt + line, end="", flush=True)
            else:
                print("\n" + "  ".join(matches), end="\n" + prompt + line, flush=True)
            continue
        if character in {"\x00", "\xe0"}:
            # Consume the second byte of a Windows special key (arrows,
            # function keys, etc.) without corrupting the current line.
            getwch()
            continue
        line += character
        print(character, end="", flush=True)


def _readline_module():
    """Load the optional stdlib readline module where the platform provides it."""
    try:
        import readline
    except ImportError:
        return None
    return readline


def _shell_paint(text: str, code: str) -> str:
    """Add terminal color without leaking escape codes into captured output."""
    if (os.environ.get("NO_COLOR") or os.environ.get("AGENTIC_PLAIN")
            or not getattr(sys.stdout, "isatty", lambda: False)()):
        return text
    return f"\033[{code}m{text}\033[0m"


def _help_line(line: str) -> str:
    """Color one help line while preserving its plain-text layout."""
    stripped = line.strip()
    section_code = HELP_SECTION_COLORS.get(stripped)
    if section_code:
        return _shell_paint(line, section_code)
    if stripped.startswith("/"):
        indent = line[:len(line) - len(line.lstrip())]
        command_end = line.find(" ", len(indent))
        if command_end < 0:
            return _shell_paint(line, "1;37")
        return (
            indent
            + _shell_paint(line[len(indent):command_end], "1;37")
            + _shell_paint(line[command_end:], "37")
        )
    if stripped.startswith("Jobs have") or stripped.startswith("if any"):
        return _shell_paint(line, "33")
    if stripped.startswith("Type /") or stripped.startswith("select,"):
        return _shell_paint(line, "90")
    return _shell_paint(line, "90")


def _print_help() -> None:
    """Print command help with a colorful terminal-only presentation."""
    for line in HELP.rstrip().splitlines():
        print(_help_line(line))


def _fit_terminal_text(text: str, width: int) -> str:
    """Keep a welcome line inside its frame, including a small ellipsis."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[:width - 3] + "..."


def _supports_banner_unicode() -> bool:
    """Return whether the current output encoding can render the banner glyphs."""
    if os.environ.get("AGENTIC_ASCII") or os.environ.get("AGENTIC_PLAIN"):
        return False
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "╭─╮│◈╰╯".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _welcome_banner(repo: Path, state_dir: Path | None = None,
                    config: Path | None = None, active_jobs: int = 0,
                    previous_jobs: int = 0) -> None:
    """Print the interactive shell welcome screen."""
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    # A framed banner is useful on normal terminals, but should not wrap on
    # split panes or small CI terminals.
    if columns < 62:
        print(_shell_paint(f"AGENTIC v{__version__}", "1;36"))
        print(_shell_paint(
            f"Interactive coding workflow - {_fit_terminal_text(repo.name, max(columns - 34, 8))}",
            "36",
        ))
        print(f"{active_jobs} running job(s); use /help for commands.")
        if previous_jobs:
            print(_shell_paint(f"{previous_jobs} previous job(s); use /ps --all to inspect.", "33"))
        print(_shell_paint("Use /help for commands. Jobs continue after /quit.", "90"))
        return

    width = min(columns, 88)
    inner = width - 2

    unicode_banner = _supports_banner_unicode()
    if not unicode_banner:
        horizontal, left, right, bottom_left, bottom_right, divider = "-", "+", "+", "+", "+", "+"
        vertical = "|"
        marker = "*"
    else:
        horizontal, left, right, bottom_left, bottom_right, divider = "─", "╭", "╮", "╰", "╯", "├"
        vertical = "│"
        marker = "◈"

    def row(text: str = "") -> str:
        return vertical + " " + _fit_terminal_text(text, inner - 2).ljust(inner - 2) + " " + vertical

    def border(start: str, end: str) -> str:
        return start + horizontal * inner + end

    state_text = str(state_dir) if state_dir else "default"
    config_text = str(config) if config else "auto-detected"
    job_text = f"{active_jobs} running job(s)"
    if previous_jobs:
        job_text += f" · {previous_jobs} previous"

    print()
    print(_shell_paint(border(left, right), "36"))
    print(_shell_paint(row(f"{marker}  A G E N T I C  v{__version__}"), "1;36"))
    print(_shell_paint(row("   Interactive issue-driven coding workflow"), "36"))
    print(_shell_paint(divider + horizontal * inner + ("┤" if unicode_banner else "+"), "36"))
    print(row(f"Repository  {repo}"))
    print(row(f"State       {state_text}"))
    print(row(f"Config      {config_text}"))
    print(row(f"Jobs        {job_text}"))
    print(_shell_paint(row("Commands    /run ISSUE   /ps   /logs JOB|PID   /help   /quit"), "36"))
    print(_shell_paint(border(bottom_left, bottom_right), "36"))
    if previous_jobs:
        print(_shell_paint("Tip: use /ps --all to inspect jobs from previous sessions.", "33"))
    print()


@dataclass
class Job:
    number: int
    argv: list[str]
    log_path: Path
    process: subprocess.Popen[bytes] | None
    started: float
    log_handle: TextIO | None
    record_path: Path
    session_id: str
    persisted_pid: int = 0
    finished: float | None = None
    exit_code: int | None = None
    notified: bool = False
    completion_callback: Callable[[int], None] | None = None

    @property
    def running(self) -> bool:
        if self.finished is not None:
            return False
        if self.process is not None:
            return self.process.poll() is None
        try:
            os.kill(self.pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    @property
    def pid(self) -> int:
        return self.process.pid if self.process is not None else int(getattr(self, "persisted_pid", 0))

    @property
    def elapsed(self) -> int:
        end = self.finished or time.time()
        return max(0, int(end - self.started))


def _windows_path(path: Path) -> str:
    """Return a Windows spelling when WSL can provide one."""
    wslpath = shutil.which("wslpath")
    if not wslpath:
        return str(path)
    try:
        result = subprocess.run(
            [wslpath, "-w", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return str(path)
    converted = result.stdout.strip()
    return converted if result.returncode == 0 and converted else str(path)


def _open_dashboard(path: Path) -> None:
    """Open a generated dashboard with the host platform's default handler."""
    path = path.resolve()
    if os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if callable(startfile):
            try:
                startfile(str(path))
                print(f"[DASHBOARD] Opened {path}")
                return
            except OSError:
                pass

    # wslview is the most reliable bridge for Linux paths in WSL. explorer.exe
    # is the fallback for minimal WSL installations without wslview.
    openers: list[tuple[str, str]] = []
    if shutil.which("wslview"):
        openers.append(("wslview", str(path)))
    if shutil.which("explorer.exe"):
        openers.append(("explorer.exe", _windows_path(path)))
    if shutil.which("open"):
        openers.append(("open", str(path)))
    if shutil.which("xdg-open"):
        openers.append(("xdg-open", str(path)))

    for opener, target in openers:
        try:
            subprocess.Popen(
                [opener, target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f"[DASHBOARD] Opened {path}")
            return
        except OSError:
            continue
    print(f"[DASHBOARD] Ready: {path} (open it manually; no desktop opener found)")


def _display_command(argv: list[str]) -> str:
    return shlex.join(argv)


def _tail(path: Path, lines: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read {path}: {exc}"
    # Avoid loading an accidentally enormous log just to display its tail.
    text = text[-200_000:]
    selected = text.splitlines()[-lines:]
    return "\n".join(selected) or "(log is empty)"


def _tail_from_text(text: str, lines: int) -> str:
    """Return a bounded tail from text already read from an open log."""
    text = text[-200_000:]
    selected = text.splitlines()[-lines:]
    return "\n".join(selected) or "(log is empty)"


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _parse_job_command(parts: list[str]) -> tuple[str | None, list[str]]:
    issue: str | None = None
    if len(parts) > 1 and parts[1].isdigit():
        issue = parts[1]
        flags = parts[2:]
    elif len(parts) == 1 or parts[1].startswith("--"):
        flags = parts[1:]
    else:
        raise ValueError("usage: /run ISSUE [--profile fast|slow] [--review|--publish|--mark-done|--new]")
    value_options = {"--profile", "--reason", "--task-file"}
    boolean_options = {"--review", "--publish", "--mark-done", "--new"}
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag in value_options:
            if index + 1 >= len(flags) or flags[index + 1].startswith("--"):
                raise ValueError(f"{flag} requires a value")
            if flag == "--profile" and flags[index + 1] not in {"fast", "slow"}:
                raise ValueError("--profile must be fast or slow")
            index += 2
            continue
        if flag not in boolean_options:
            raise ValueError(f"unknown /run option: {flag}")
        index += 1
    if flags.count("--profile") > 1:
        raise ValueError("/run accepts --profile only once")
    if flags.count("--reason") > 1:
        raise ValueError("/run accepts --reason only once")
    if flags.count("--task-file") > 1:
        raise ValueError("/run accepts --task-file only once")
    if "--review" in flags and "--publish" in flags:
        raise ValueError("/run cannot combine --review and --publish")
    if "--mark-done" in flags and "--publish" in flags:
        raise ValueError("/run cannot combine --mark-done and --publish")
    if "--mark-done" not in flags and "--reason" in flags:
        raise ValueError("/run --reason requires --mark-done")
    return issue, flags


def _option_value(flags: list[str], option: str) -> str | None:
    try:
        index = flags.index(option)
    except ValueError:
        return None
    return flags[index + 1]


def _parse_options(parts: list[str], *, usage: str,
                   value_options: set[str], boolean_options: set[str]) -> list[str]:
    flags = parts[1:]
    seen: set[str] = set()
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag in value_options:
            if flag in seen:
                raise ValueError(f"{flag} accepts one value")
            if index + 1 >= len(flags) or flags[index + 1].startswith("--"):
                raise ValueError(f"{flag} requires a value")
            seen.add(flag)
            index += 2
            continue
        if flag not in boolean_options:
            raise ValueError(f"usage: {usage}")
        index += 1
    return flags


class AgenticShell:
    def __init__(self, repo: Path, runner: Path, config: Path | None = None,
                 session_dir: Path | None = None,
                 state_dir: Path | None = None) -> None:
        self.repo = repo.resolve()
        self.runner = runner.resolve()
        self.config = config.resolve() if config else None
        if state_dir is None:
            from agentic import default_state_dir
            state_dir = default_state_dir()
        self.state_dir = state_dir.expanduser().resolve()
        if session_dir is None:
            from agentic import state_paths
            session_dir = state_paths(self.repo, self.state_dir).shell
        self.session_dir = session_dir.resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = uuid.uuid4().hex[:12]
        self.jobs: dict[int, Job] = {}
        self._load_persisted_jobs()
        self.next_number = max(self.jobs, default=0) + 1
        self.history: list[str] = []

    def _completion_matches(self, line: str, cursor: int | None = None) -> list[str]:
        return _completion_candidates(line, cursor, tuple(self.jobs), jobs=self.jobs)

    def _completion_items(self, line: str, cursor: int | None = None) -> list[tuple[str, str]]:
        return _completion_items(line, cursor, tuple(self.jobs), jobs=self.jobs)


    def _readline_completer(self, text: str, state: int) -> str | None:
        """Adapter for GNU/libedit readline's stateful completion API."""
        readline = _readline_module()
        if readline is None:
            return None
        if state == 0:
            self._readline_matches = self._completion_matches(
                readline.get_line_buffer(), readline.get_endidx()
            )
        matches = getattr(self, "_readline_matches", [])
        return matches[state] if state < len(matches) else None

    def _configure_completion(self):
        """Enable Tab completion and return readline state for restoration."""
        readline = _readline_module()
        if readline is None:
            return None
        previous = (
            readline.get_completer(),
            readline.get_completer_delims(),
        )
        readline.set_completer(self._readline_completer)
        # Keep '/' and '-' inside a completion word so both command and option
        # completion replace the full token.
        readline.set_completer_delims(" \t")
        readline.parse_and_bind("tab: complete")
        return readline, previous

    @staticmethod
    def _restore_completion(state) -> None:
        if state is None:
            return
        readline, (completer, delimiters) = state
        readline.set_completer(completer)
        readline.set_completer_delims(delimiters)

    def _input(self, prompt: str) -> str:
        """Read a shell line with completion where the terminal supports it."""
        if _is_interactive_terminal():
            return _interactive_input(
                prompt,
                self._completion_items,
                history=self.history,
            )
        if _readline_module() is not None:
            return input(prompt)
        if os.name == "nt" and getattr(sys.stdin, "isatty", lambda: False)():
            return _windows_input(prompt, self._completion_matches)
        return input(prompt)


    def _record_path(self, number: int) -> Path:
        return self.session_dir / f"job-{number:03d}.json"

    def _write_record(self, job: Job) -> None:
        record = {
            "number": job.number,
            "argv": job.argv,
            "pid": job.pid,
            "started": job.started,
            "finished": job.finished,
            "exit_code": job.exit_code,
            "state": "running" if job.running else "finished",
            "log_path": str(job.log_path),
            "cwd": str(self.repo),
            "session_id": job.session_id,
        }
        temporary = job.record_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        temporary.replace(job.record_path)

    def _load_persisted_jobs(self) -> None:
        for record_path in sorted(self.session_dir.glob("job-*.json")):
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                number = int(record["number"])
                argv = [str(value) for value in record["argv"]]
                pid = int(record.get("pid", 0))
                started = float(record["started"])
                log_path = Path(record["log_path"])
                state = record.get("state", "finished")
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            if state == "running" and _pid_running(pid):
                job = Job(number, argv, log_path, None, started, None,
                          record_path, str(record.get("session_id", "unknown")),
                          persisted_pid=pid, notified=False)
                self.jobs[number] = job
                continue
            finished = record.get("finished")
            job = Job(
                number, argv, log_path, None, started, None, record_path,
                str(record.get("session_id", "unknown")), persisted_pid=pid,
                finished=float(finished) if finished is not None else time.time(),
                exit_code=record.get("exit_code") if state != "running" else -1,
                notified=True,
            )
            self.jobs[number] = job
            if state == "running":
                self._write_record(job)

    def _launch(self, argv: list[str],
                on_complete: Callable[[int], None] | None = None) -> Job:
        number = self.next_number
        self.next_number += 1
        log_path = self.session_dir / f"job-{number:03d}.log"
        record_path = self._record_path(number)
        log_handle = log_path.open("w", encoding="utf-8", buffering=1)
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.repo,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
        except OSError:
            log_handle.close()
            raise
        job = Job(number, argv, log_path, process, time.time(), log_handle,
                  record_path, self.session_id, completion_callback=on_complete)
        self.jobs[number] = job
        self._write_record(job)
        print(f"[JOB {number}] started (PID {process.pid})")
        print(f"[JOB {number}] log: {log_path}")
        if on_complete is not None:
            threading.Thread(
                target=self._wait_for_completion,
                args=(job,),
                name=f"agentic-job-{number}",
                daemon=True,
            ).start()
        return job

    @staticmethod
    def _wait_for_completion(job: Job) -> None:
        """Run a transient completion action without delaying the shell prompt."""
        if job.process is None or job.completion_callback is None:
            return
        code = job.process.wait()
        try:
            job.completion_callback(code)
        except Exception as exc:  # desktop openers must not crash the shell
            print(f"[DASHBOARD] Could not open dashboard: {exc}")

    def _refresh(self, job: Job) -> None:
        if job.finished is not None:
            return
        code = job.process.poll() if job.process is not None else None
        if job.process is None:
            if job.running:
                return
            code = -1
        elif code is None:
            return
        job.finished = time.time()
        job.exit_code = code
        if job.log_handle is not None:
            try:
                job.log_handle.close()
            except OSError:
                pass
        self._write_record(job)
        if not job.notified:
            job.notified = True
            state = "finished" if code == 0 else f"failed (exit {code})"
            print(f"\n[JOB {job.number}] {state} after {job.elapsed}s; use /logs {job.number}")

    def refresh(self) -> None:
        for job in self.jobs.values():
            self._refresh(job)

    def _runner_args(self, issue: str | None, flags: list[str]) -> list[str]:
        argv = [
            sys.executable, "-u", str(self.runner), "--repo", str(self.repo),
            "--state-dir", str(self.state_dir),
        ]
        if issue is not None:
            argv.extend(["--issue", issue])
        if self.config:
            argv.extend(["--config", str(self.config)])
        argv.extend(flags)
        return argv

    def _run_foreground(self, argv: list[str]) -> None:
        """Run a command with the shell's terminal attached for confirmation."""
        result = subprocess.run(argv, cwd=self.repo, check=False)
        if result.returncode:
            raise RuntimeError(f"command failed with exit {result.returncode}")

    def _run_runner_command(self, argv: list[str]) -> None:
        result = subprocess.run(
            argv, cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        if result.stdout:
            print(result.stdout.rstrip())
        if result.returncode:
            raise RuntimeError(f"command failed with exit {result.returncode}")

    def _subcommand_args(self, command: str, flags: list[str] = ()) -> list[str]:
        argv = [
            sys.executable, "-u", str(self.runner), command,
            "--repo", str(self.repo), "--state-dir", str(self.state_dir),
        ]
        if self.config and command == "doctor":
            argv.extend(["--config", str(self.config)])
        argv.extend(flags)
        return argv

    def _task_source(self) -> str:
        config_path = self.config or (self.repo / "agent_config.json")
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "github"
        return data.get("task_source", "github") if isinstance(data, dict) else "github"

    def _capture_local_task(self) -> Path:
        print("Paste the local task. Finish with a line containing /done; use /cancel to abort.")
        lines: list[str] = []
        while True:
            try:
                line = input("task> ")
            except (EOFError, KeyboardInterrupt) as exc:
                print()
                raise ValueError("local task input cancelled") from exc
            if line.strip() == "/done":
                break
            if line.strip() == "/cancel":
                raise ValueError("local task input cancelled")
            lines.append(line)
        if not any(line.strip() for line in lines):
            raise ValueError("local task must not be empty")
        path = self.session_dir / f"task-{uuid.uuid4().hex[:12]}.md"
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        print(f"Local task saved: {path}")
        return path

    def run_issue(self, parts: list[str]) -> None:
        issue, flags = _parse_job_command(parts)
        task_file: Path | None = None
        task_file_value = _option_value(flags, "--task-file")
        if issue is not None and task_file_value is not None:
            raise ValueError("--task-file is only available for local /run tasks")
        if issue is None:
            if self._task_source() != "local":
                raise ValueError("GitHub mode requires /run ISSUE; set task_source to local to paste tasks")
            task_file = (
                Path(task_file_value).expanduser().resolve()
                if task_file_value is not None else self._capture_local_task()
            )
        argv = self._runner_args(issue, flags)
        if task_file and task_file_value is None:
            argv.extend(["--task-file", str(task_file)])
        if "--publish" in flags:
            print("[PUBLISH] Running in the foreground so publication confirmation can be entered")
            self._run_foreground(argv)
        else:
            self._launch(argv)

    def run_dashboard(self, parts: list[str]) -> None:
        dashboard = self.runner.with_name("agentic_dashboard.py")
        if not dashboard.is_file():
            print(f"Dashboard script not found: {dashboard}")
            return
        argv = [sys.executable, "-u", str(dashboard), "--repo", str(self.repo)]
        argv.extend(["--state-dir", str(self.state_dir)])
        if len(parts) > 2:
            raise ValueError("usage: /dashboard [OUTPUT]")
        if len(parts) == 2:
            output = Path(parts[1]).expanduser().resolve()
        else:
            from agentic import state_paths
            output = state_paths(self.repo, self.state_dir).dashboards / "dashboard.html"
        argv.extend(["--output", str(output)])

        def open_when_ready(code: int) -> None:
            if code == 0:
                _open_dashboard(output)
            else:
                print(f"[DASHBOARD] Generation failed (exit {code}); use /logs for details")

        self._launch(argv, on_complete=open_when_ready)

    def show_processes(self, parts: list[str] | None = None) -> None:
        parts = parts or ["/ps"]
        if len(parts) > 2 or (len(parts) == 2 and parts[1] != "--all"):
            raise ValueError("usage: /ps [--all]")
        show_all = len(parts) == 2
        self.refresh()
        jobs = [job for job in self.jobs.values() if show_all or job.session_id == self.session_id]
        if not jobs:
            print("No matching jobs. Use /ps --all to include previous shell sessions.")
            return
        print("ID  STATUS      PID     AGE  SESSION      COMMAND")
        states = [(job, job.running) for job in jobs]
        for job, running in sorted(states, key=lambda item: (not item[1], item[0].pid, item[0].number)):
            status = "RUNNING" if running else f"EXIT {job.exit_code if job.exit_code not in (None, -1) else '?'}"
            session = "current" if job.session_id == self.session_id else job.session_id[:8]
            print(f"{job.number:<3} {status:<10} {job.pid:<7} {job.elapsed:>3}s  {session:<8} {_display_command(job.argv)}")

    def show_logs(self, parts: list[str]) -> None:
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError("usage: /logs JOB|PID [LINES]")
        identifier = int(parts[1])
        job = self.jobs.get(identifier)
        if job is None:
            job = next((candidate for candidate in self.jobs.values()
                        if candidate.pid == identifier), None)
        if job is None:
            raise ValueError(f"unknown job or PID: {parts[1]}")
        lines = 40
        if len(parts) > 3 or (len(parts) == 3 and (not parts[2].isdigit() or int(parts[2]) < 1)):
            raise ValueError("usage: /logs JOB|PID [LINES]")
        if len(parts) == 3:
            lines = min(int(parts[2]), 500)
        self._refresh(job)
        print(f"--- job {job.number}: {_display_command(job.argv)} ---")
        if job.running:
            print("--- following live output; press Ctrl-C to stop following ---")
            self._follow_log(job, lines)
        else:
            print(_tail(job.log_path, lines))
        print(f"--- end; full log: {job.log_path} ---")

    def _follow_log(self, job: Job, lines: int) -> None:
        """Print a running job's existing tail and stream appended output."""
        try:
            with job.log_path.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(0, os.SEEK_END)
                initial_end = stream.tell()
                initial_start = max(0, initial_end - 200_000)
                stream.seek(initial_start)
                initial = stream.read(initial_end - initial_start)
                print(_tail_from_text(initial, lines))
                # Keep the position at the end of the initial snapshot rather
                # than seeking to the current EOF, so a concurrent write is
                # still included in the first live read.
                stream.seek(initial_end)

                while job.running:
                    self._refresh(job)
                    chunk = stream.read()
                    if chunk:
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                    if not job.running:
                        break
                    time.sleep(0.1)

                # The child may have flushed its last bytes just before the
                # process status changed. Drain once after observing exit.
                self._refresh(job)
                chunk = stream.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
        except KeyboardInterrupt:
            print("\n--- live output follow stopped; job continues running ---")
        except OSError as exc:
            print(f"Could not read {job.log_path}: {exc}")

    def stop(self, parts: list[str]) -> None:
        if len(parts) != 2 or not parts[1].isdigit():
            raise ValueError("usage: /stop JOB")
        job = self.jobs.get(int(parts[1]))
        if job is None:
            raise ValueError(f"unknown job: {parts[1]}")
        self._refresh(job)
        if not job.running:
            print(f"[JOB {job.number}] already finished with exit {job.exit_code}")
            return
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(job.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            if job.process is not None:
                job.process.terminate()
            else:
                os.kill(job.pid, signal.SIGTERM)
        print(f"[JOB {job.number}] stop requested")

    def show_status(self, parts: list[str]) -> None:
        if len(parts) > 2 or (len(parts) == 2 and parts[1] != "--json"):
            raise ValueError("usage: /status [--json]")
        argv = [sys.executable, str(self.runner), "status", "--repo", str(self.repo)]
        if len(parts) == 2:
            argv.append("--json")
        result = subprocess.run(
            argv, cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        print(result.stdout.rstrip())
        if result.returncode:
            raise RuntimeError(f"status command failed with exit {result.returncode}")

    def show_issues(self, parts: list[str]) -> None:
        if len(parts) != 1:
            raise ValueError("usage: /list")
        result = subprocess.run(
            self._runner_args(None, ["--list"]), cwd=self.repo,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        print(result.stdout.rstrip())
        if result.returncode:
            raise RuntimeError(f"list command failed with exit {result.returncode}")

    def show_runs(self, parts: list[str]) -> None:
        flags = _parse_options(
            parts, usage="/runs [--json]", value_options=set(), boolean_options={"--json"},
        )
        self._run_runner_command(self._subcommand_args("runs", flags))

    def run_init(self, parts: list[str]) -> None:
        flags = _parse_options(
            parts,
            usage="/init [--provider NAME] [--reviewer-provider NAME] [--security-mode MODE] [--sandbox-image IMAGE] [--agents] [--force]",
            value_options={"--provider", "--reviewer-provider", "--security-mode", "--sandbox-image"},
            boolean_options={"--agents", "--force"},
        )
        self._run_runner_command(self._subcommand_args("init", flags))

    def run_doctor(self, parts: list[str]) -> None:
        flags = _parse_options(
            parts, usage="/doctor [--json]", value_options=set(), boolean_options={"--json"},
        )
        self._run_runner_command(self._subcommand_args("doctor", flags))

    def run_cleanup(self, parts: list[str]) -> None:
        flags = _parse_options(
            parts,
            usage="/cleanup --issue N [--yes] [--remove-logs] | /cleanup --all [--yes] [--remove-logs]",
            value_options={"--issue"},
            boolean_options={"--all", "--yes", "--remove-logs"},
        )
        issue = _option_value(flags, "--issue")
        if issue is None and "--all" not in flags:
            raise ValueError("/cleanup requires --issue N or --all")
        if issue is not None and (not issue.isdigit() or "--all" in flags):
            raise ValueError("/cleanup accepts either --issue N or --all")
        self._run_runner_command(self._subcommand_args("cleanup", flags))

    def show_version(self, parts: list[str]) -> None:
        if len(parts) != 1:
            raise ValueError("usage: /version")
        self._run_runner_command([sys.executable, "-u", str(self.runner), "--version"])

    def execute(self, line: str) -> bool:
        stripped = line.strip()
        if stripped and (not self.history or self.history[-1] != stripped):
            self.history.append(stripped)
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            print(f"Input error: {exc}")
            return True
        if not parts:
            return True
        if not parts[0].startswith("/"):
            print("Commands must start with '/'. Use /help for available commands.")
            return True
        command = parts[0].lower()
        try:
            if command in {"/help", "/?"}:
                _print_help()
            elif command == "/run":
                self.run_issue(parts)
            elif command == "/ps":
                self.show_processes(parts)
            elif command == "/logs":
                self.show_logs(parts)
            elif command == "/stop":
                self.stop(parts)
            elif command == "/list":
                self.show_issues(parts)
            elif command == "/runs":
                self.show_runs(parts)
            elif command == "/status":
                self.show_status(parts)
            elif command == "/dashboard":
                self.run_dashboard(parts)
            elif command == "/init":
                self.run_init(parts)
            elif command == "/doctor":
                self.run_doctor(parts)
            elif command == "/cleanup":
                self.run_cleanup(parts)
            elif command == "/version":
                self.show_version(parts)
            elif command in {"/quit", "/exit"}:
                return False
            else:
                print(f"Unknown command: {command}. Use /help.")
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"Error: {exc}")
        return True

    def run(self) -> int:
        active_jobs = sum(job.running for job in self.jobs.values())
        previous_jobs = sum(job.session_id != self.session_id for job in self.jobs.values())
        _welcome_banner(
            self.repo,
            self.state_dir,
            self.config,
            active_jobs=active_jobs,
            previous_jobs=previous_jobs,
        )
        _print_help()
        completion_state = self._configure_completion()
        try:
            while True:
                self.refresh()
                try:
                    line = self._input("agentic> ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not self.execute(line):
                    break
        finally:
            self._restore_completion(completion_state)
        running = [job.number for job in self.jobs.values()
                   if job.running and job.session_id == self.session_id]
        if running:
            print(f"Leaving shell; jobs still running: {', '.join(map(str, running))}")
            print("Their logs and job records remain in the session directory; use /ps --all later.")
        return 0


def run_shell(repo: Path, runner: Path, config: Path | None = None,
              session_dir: Path | None = None,
              *, state_dir: Path | None = None) -> int:
    return AgenticShell(repo, runner, config, session_dir, state_dir).run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("help",), help="Show this help message and exit")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Target repository root")
    parser.add_argument("--runner", type=Path, default=Path(__file__).with_name("agentic.py"), help="Agentic runner script")
    parser.add_argument("--config", type=Path, help="Configuration file passed to child runs")
    parser.add_argument("--session-dir", type=Path, help="Directory for interactive-shell logs")
    parser.add_argument("--state-dir", type=Path, help="Agentic state root (shared /mnt path recommended for WSL/Windows)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "help":
        parser.print_help()
        return 0
    return run_shell(args.repo, args.runner, args.config, args.session_dir, state_dir=args.state_dir)


if __name__ == "__main__":
    raise SystemExit(main())
