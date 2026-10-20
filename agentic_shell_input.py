#!/usr/bin/env python3
"""Interactive background-job supervisor for Agentic.

This module uses only the Python standard library. Agentic remains the source
of truth for worktrees, tests, reviews, and durable run logs; this shell only
supervises child processes and stores a terminal-output copy for inspection.
"""
from __future__ import annotations

import codecs
import os
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast

from agentic import __version__
from agentic_output import paint, sanitize_terminal_text, strip_ansi, style_text
if TYPE_CHECKING:
    from agentic_shell import Job


HELP = """Commands:

  Run
    /run ISSUE [--profile fast|slow] [--instruction "text"]
                                      [--review|--publish|--mark-done|--new]
                                      [--reason "why"] with --mark-done
                                      start a background job
    /run [--profile fast|slow] [--instruction "text"]
                                      [--review|--publish|--mark-done|--new]
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
    /doctor [--json] [--commands]      diagnose prerequisites; optionally run checks
    /cleanup --issue N [OPTIONS]        preview or remove clean worktrees
    /version                            show the Agentic version

  Session
    /chat ISSUE MESSAGE                  run an issue with a one-off instruction
    /stop JOB|--all                     stop one or all running jobs
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
    "/chat",
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
    "/run": ("--profile", "--instruction", "--review", "--publish", "--mark-done", "--reason", "--new", "--task-file"),
    "/chat": (),
    "/ps": ("--all",),
    "/runs": ("--json",),
    "/status": ("--json",),
    "/doctor": ("--json", "--commands", "--profile", "--worktree"),
    "/cleanup": ("--issue", "--all", "--yes", "--remove-logs"),
    "/stop": ("--all",),
    "/init": ("--provider", "--reviewer-provider", "--security-mode", "--sandbox-image", "--agents", "--force"),
}
COMMAND_DESCRIPTIONS: dict[str, str] = {
    "/run": "start a background job or local task",
    "/chat": "run an issue with a one-off instruction",
    "/ps": "show running or persisted background jobs",
    "/logs": "follow or show captured terminal output",
    "/list": "list open GitHub issues",
    "/runs": "list existing Agentic worktrees",
    "/stop": "stop one or all running background jobs",
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
    "--instruction": "optional instruction preserved for this run",
    "--review": "request read-only review",
    "--publish": "publish the existing approved patch as a draft PR",
    "--mark-done": "record human completion for the current patch",
    "--reason": "reason recorded with manual completion",
    "--new": "start fresh worktree, ignoring cached state",
    "--all": "show jobs across all shell sessions",
    "--json": "output status in JSON format",
    "--commands": "run configured doctor checks (may mutate the worktree)",
    "--worktree": "worktree used by doctor checks",
    "--task-file": "use an existing local task file",
    "--issue": "select one issue worktree",
    "--remove-logs": "also remove matching run logs",
    "--yes": "confirm cleanup changes",
    "--provider": "developer provider for initialization",
    "--reviewer-provider": "independent reviewer provider",
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
    if command in {"/run", "/doctor"} and previous == "--profile":
        values = ("fast", "slow")
        return [value for value in values if value.startswith(current)]

    if command == "/stop" and (current.startswith("-") or not job_numbers):
        options = SHELL_OPTIONS["/stop"]
        return [option for option in options if option.startswith(current)]

    if command in {"/logs", "/stop"}:
        numbers = sorted(
            job_numbers,
            key=lambda number: (
                not bool(jobs and number in jobs and jobs[number].running), number,
            ),
        )
        values = [str(number) for number in numbers]
        if command == "/stop":
            values.insert(0, "--all")
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


def _clip_input_line(line: str, cursor: int, prompt_width: int,
                     columns: int) -> tuple[str, int]:
    """Keep the editable line on one physical terminal row while scrolling."""
    available = max(1, columns - prompt_width - 1)
    if len(line) <= available:
        return line, cursor
    start = min(max(0, cursor - available), len(line) - available)
    visible = line[start:start + available]
    return visible, cursor - start


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
    if seq == "[":
        sequence: list[str] = []
        while True:
            code = read_next()
            if code is None:
                return "ESCAPE", "\x1b"
            sequence.append(code)
            if code == "~" or code.isalpha():
                break
        value = "".join(sequence)
        if value == "200~":
            return "PASTE_START", ""
        if value == "201~":
            return "PASTE_END", ""
        if value in {"A", "Z", "1;2A"}:
            return "UP", ""
        if value == "B":
            return "DOWN", ""
        if value == "C":
            return "RIGHT", ""
        if value == "D":
            return "LEFT", ""
        if value in {"H", "1~", "7~"}:
            return "HOME", ""
        if value in {"F", "4~", "8~"}:
            return "END", ""
        if value == "3~":
            return "DELETE", ""
        # Terminals that support modified keys commonly encode Shift+Enter as
        # CSI 13;2u or CSI 13;2~. Keep it distinct from submit/Enter.
        if value in {"13;2u", "13;2~", "27;2;13~"}:
            return "NEWLINE", "\n"
        return "ESCAPE", "\x1b"
    if seq == "O":
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

    def _read_paste(self) -> str:
        """Read a bracketed paste body, including embedded newlines."""
        terminator = "\x1b[201~"
        buffered = ""
        pasted: list[str] = []
        if os.name == "nt":
            import msvcrt
            getwch = cast(Callable[[], str], getattr(msvcrt, "getwch"))
            read_next = getwch
        else:
            read_next = self._read_posix_char
        while True:
            char = read_next()
            if not char:
                pasted.append(buffered)
                return "".join(pasted)
            buffered += char
            if buffered.endswith(terminator):
                pasted.append(buffered[:-len(terminator)])
                return "".join(pasted)
            # Keep only the possible prefix of the terminator. This avoids
            # swallowing ordinary pasted text when an escape is present.
            while buffered and not terminator.startswith(buffered):
                pasted.append(buffered[0])
                buffered = buffered[1:]

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
                key, value = _parse_ansi_sequence(_read_win)
                if key == "PASTE_START":
                    return "PASTE", self._read_paste()
                return key, value
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
            key, value = _parse_ansi_sequence(_read_posix)
            if key == "PASTE_START":
                return "PASTE", self._read_paste()
            return key, value
        return "CHAR", char



def _interactive_input(
    prompt: str,
    completer: Callable[[str, int | None], list[tuple[str, str]]],
    history: list[str] | None = None,
    key_reader_factory: Callable[[], Any] | None = None,
    status_line: str | None = None,
) -> str:
    """Read one or more shell lines with autocomplete, paste, and navigation."""
    line = ""
    cursor_pos = 0
    selected_index = 0
    navigated = False
    dismissed = False
    prev_dropdown_lines = 0
    prev_input_lines = 1
    prev_cursor_row = 0
    first_render = True
    candidates: list[tuple[str, str]] = []

    hist_entries = list(history) if history else []
    hist_index = len(hist_entries)
    saved_input = ""

    def render() -> None:
        nonlocal prev_dropdown_lines, prev_input_lines, prev_cursor_row
        nonlocal selected_index, navigated, candidates, first_render
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        lines_avail = shutil.get_terminal_size(fallback=(80, 24)).lines

        if first_render and status_line:
            status = sanitize_terminal_text(status_line)
            sys.stdout.write(_shell_paint(_fit_terminal_text(status, columns - 1), "90") + "\n")
            first_render = False
        elif first_render:
            first_render = False

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

        input_parts = line.split("\n")
        cursor_row = line[:cursor_pos].count("\n")
        cursor_column = cursor_pos - (line.rfind("\n", 0, cursor_pos) + 1)
        display_lines: list[str] = []
        cursor_display = 0
        for row, part in enumerate(input_parts):
            row_prompt = prompt if row == 0 else "  "
            row_cursor = cursor_column if row == cursor_row else len(part)
            visible, visible_cursor = _clip_input_line(
                part, row_cursor, len(row_prompt), columns,
            )
            if row == cursor_row:
                cursor_display = visible_cursor
            display_lines.append(
                _shell_paint(row_prompt, "1;36") + _shell_paint(visible, "37")
            )

        buf: list[str] = []
        if prev_input_lines + prev_dropdown_lines:
            if prev_cursor_row:
                buf.append(f"\033[{prev_cursor_row}A")
            buf.append("\r")
            old_lines = prev_input_lines + prev_dropdown_lines
            for index in range(old_lines):
                buf.append("\033[2K")
                if index < old_lines - 1:
                    buf.append("\n")
            if old_lines > 1:
                buf.append(f"\033[{old_lines - 1}A\r")
        buf.append(display_lines[0])
        for display_line in display_lines[1:]:
            buf.append(f"\n\033[2K{display_line}")
        for s in dropdown_strings:
            buf.append(f"\n\033[2K{s}")
        cursor_col = len(prompt) + cursor_display + 1 if cursor_row == 0 else 2 + cursor_display + 1
        if cursor_row + new_dropdown_lines:
            buf.append(f"\033[{cursor_row + new_dropdown_lines}A")
        if cursor_row:
            buf.append(f"\033[{cursor_row}B")
        buf.append(f"\033[{cursor_col}G")

        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        prev_dropdown_lines = new_dropdown_lines
        prev_input_lines = len(display_lines)
        prev_cursor_row = cursor_row

    def clear_dropdown() -> None:
        nonlocal dismissed
        if prev_dropdown_lines > 0:
            dismissed = True
            render()

    def finish_input() -> None:
        """Leave the cursor after the complete editable buffer."""
        if prev_cursor_row < prev_input_lines - 1:
            sys.stdout.write(f"\033[{prev_input_lines - 1 - prev_cursor_row}B")
        sys.stdout.write("\r\n")
        sys.stdout.flush()

    reader_factory = key_reader_factory or _KeyReader
    with reader_factory() as reader:
        sys.stdout.write("\033[?2004h")
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
                    finish_input()
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

                if key in {"NEWLINE", "PASTE"}:
                    inserted = char.replace("\r\n", "\n").replace("\r", "\n")
                    if key == "NEWLINE":
                        inserted = "\n"
                    line = line[:cursor_pos] + inserted + line[cursor_pos:]
                    cursor_pos += len(inserted)
                    dismissed = False
                    selected_index = 0
                    navigated = False
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
            sys.stdout.write("\033[?2004l")
            sys.stdout.flush()



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
    return paint(sanitize_terminal_text(text), code)


def _shell_print(message: Any = "", *, end: str = "\n", flush: bool = False,
                 style_status_tokens: bool = True) -> None:
    """Print shell status or replayed log text with terminal-only styling."""
    text = style_text(message, style_status_tokens=style_status_tokens)
    print(text, end=end, flush=flush)


def _shell_print_raw(message: Any = "", *, end: str = "\n", flush: bool = False) -> None:
    """Print machine-oriented child output without adding terminal decoration."""
    print(strip_ansi(message), end=end, flush=flush)


def _shell_write_log(text: str) -> None:
    """Write appended log text without bypassing the TTY styling boundary."""
    sys.stdout.write(style_text(text, style_status_tokens=False))
    sys.stdout.flush()


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


# Explicitly export private compatibility helpers to the facade.
__all__ = [name for name in globals() if not name.startswith("__")]
# End of shell input boundary.
