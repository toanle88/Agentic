#!/usr/bin/env python3
"""Interactive background-job supervisor for Agentic.

This module uses only the Python standard library. Agentic remains the source
of truth for worktrees, tests, reviews, and durable run logs; this shell only
supervises child processes and stores a terminal-output copy for inspection.
"""
from __future__ import annotations

import codecs
from enum import Enum
import os
import re
import shlex
import shutil
import signal
import sys
import threading
import textwrap
import time
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast

from agentic import __version__
from agentic_output import (
    color_enabled,
    paint,
    sanitize_terminal_text,
    strip_ansi,
    style_system_text,
    style_text,
)
if TYPE_CHECKING:
    from agentic_shell import Job


_OUTPUT_LOCK = threading.RLock()
_ACTIVE_INTERACTIVE_OUTPUT: Callable[[str, bool], None] | None = None
_RESIZE_PENDING = False
_ANSI_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)


class TerminalMode(str, Enum):
    """Identify which interactive view currently owns the terminal."""

    COMMAND_READY = "command-ready"
    CHAT = "chat"
    CONFIRMATION = "confirmation"
    LIVE_OUTPUT = "live-output"
    OUTPUT = "output"
    EXIT = "exit"


class _InteractiveOutputState:
    """Retain output across prompt iterations for one interactive shell."""

    def __init__(self) -> None:
        self.history: list[str] = []
        self.cursor_column = 1
        self.cursor_wrap_pending = False
        self._cursor_columns: int | None = None
        self.live_status = False
        self.mode = TerminalMode.OUTPUT

    @property
    def normal_prompt_visible(self) -> bool:
        """Whether the normal command input line may be rendered."""
        return self.mode is TerminalMode.COMMAND_READY

    def set_mode(self, mode: TerminalMode) -> None:
        """Record the current terminal owner without changing scrollback."""
        self.mode = TerminalMode(mode)

    @staticmethod
    def _with_newline(text: str, ensure_newline: bool) -> str:
        if ensure_newline and text and not text.endswith(("\n", "\r")):
            return text + "\n"
        return text

    def append(self, text: str, ensure_newline: bool, columns: int) -> str:
        """Record output and advance the live tail cursor."""
        columns = max(1, columns)
        self.recompute_cursor(columns)
        output_text = self._with_newline(text, ensure_newline)
        self.history.append(output_text)
        for character in strip_ansi(output_text):
            self.cursor_column, self.cursor_wrap_pending = _advance_terminal_cursor(
                self.cursor_column,
                self.cursor_wrap_pending,
                character,
                columns,
            )
        self._cursor_columns = columns
        return output_text

    def recompute_cursor(self, columns: int) -> None:
        """Recalculate the logical tail after a terminal-width change."""
        columns = max(1, columns)
        if self._cursor_columns == columns:
            return
        cursor_column = 1
        cursor_wrap_pending = False
        for character in strip_ansi("".join(self.history)):
            cursor_column, cursor_wrap_pending = _advance_terminal_cursor(
                cursor_column,
                cursor_wrap_pending,
                character,
                columns,
            )
        self.cursor_column = cursor_column
        self.cursor_wrap_pending = cursor_wrap_pending
        self._cursor_columns = columns

    def mark_prompt_boundary(self, columns: int) -> None:
        """Keep output after a completed prompt on a new logical line."""
        columns = max(1, columns)
        self.recompute_cursor(columns)
        retained = strip_ansi("".join(self.history))
        if retained and not retained.endswith(("\n", "\r")):
            self.append("\n", ensure_newline=False, columns=columns)
            return
        self.cursor_column = 1
        self.cursor_wrap_pending = False
        self._cursor_columns = columns

    def write_without_editor(self, text: str, ensure_newline: bool = True) -> None:
        """Retain output while no prompt renderer is currently active."""
        with _OUTPUT_LOCK:
            if self.live_status:
                sys.stdout.write("\r\033[2K")
                self.live_status = False
            columns = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)
            output_text = self.append(text, ensure_newline, columns)
            sys.stdout.write(output_text)
            sys.stdout.flush()

    def write_transient(self, text: str) -> None:
        """Draw one replaceable status row without adding it to scrollback."""
        with _OUTPUT_LOCK:
            if not getattr(sys.stdout, "isatty", lambda: False)():
                self.write_without_editor(text, ensure_newline=True)
                return
            columns = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)
            status = _fit_terminal_text(sanitize_terminal_text(text), columns - 1)
            if self.live_status:
                sys.stdout.write("\r\033[2K")
            sys.stdout.write("\r" + style_text(status, style_status_tokens=False))
            sys.stdout.flush()
            self.live_status = True


class _ForegroundOutputState:
    """Keep a child prompt in the last terminal rows while streaming output."""

    def __init__(
        self,
        output_state: _InteractiveOutputState,
        mode: TerminalMode = TerminalMode.OUTPUT,
    ) -> None:
        self.output_state = output_state
        self.mode = TerminalMode(mode)
        self.prompt = ""
        self.prompt_prefix: str | None = None
        self.prompt_prefix_text: str | None = None
        self.input_text = ""
        self.input_cursor = 0
        self.finished = False
        self.live_status: str | None = None
        self.output_scroll_lines = 0

    @staticmethod
    def _sanitize_prompt(prompt: str) -> str:
        safe = strip_ansi(prompt).replace("\r", "\n")
        return "\n".join(sanitize_terminal_text(line) for line in safe.split("\n"))

    @classmethod
    def _prompt_lines(
        cls, prompt: str, columns: int, *, system_prefix: str | None = None,
        system_prefix_text: str | None = None,
    ) -> list[str]:
        safe_prompt = cls._sanitize_prompt(prompt)
        lines = _output_history_lines(safe_prompt, columns) or [""]
        if system_prefix is not None and lines:
            lines[0] = style_system_text(
                lines[0],
                system_prefix=system_prefix,
                system_prefix_text=system_prefix_text,
                style_status_tokens=False,
            )
        return lines

    @staticmethod
    def _cursor_position(text: str, cursor: int, columns: int) -> tuple[int, int]:
        """Return the wrapped row and column for an editable cursor."""
        row = 0
        width = 0
        cursor = max(0, min(cursor, len(text)))
        for character in text[:cursor]:
            if character in {"\n", "\r"}:
                row += 1
                width = 0
                continue
            cell_width = _terminal_cell_width(character)
            if cell_width == 0:
                continue
            if width and width + cell_width > columns:
                row += 1
                width = 0
            width = min(columns, width + cell_width)
        return row, max(1, min(columns, width + 1))

    def _prompt_text(self) -> str:
        return self.prompt + self.input_text

    def _render(self) -> None:
        with _OUTPUT_LOCK:
            size = shutil.get_terminal_size(fallback=(80, 24))
            columns = max(1, size.columns)
            rows = max(1, size.lines)
            prompt_text = self._prompt_text()
            prompt_lines = self._prompt_lines(
                prompt_text,
                columns,
                system_prefix=self.prompt_prefix,
                system_prefix_text=self.prompt_prefix_text,
            )
            prompt_rows = min(rows, max(1, len(prompt_lines)))
            output_rows = max(0, rows - prompt_rows)

            output_text = "".join(self.output_state.history)
            if self.live_status:
                if output_text and not output_text.endswith(("\n", "\r")):
                    output_text += "\n"
                output_text += self.live_status
            output_lines = _output_history_lines(output_text, columns)
            max_scroll = max(0, len(output_lines) - output_rows)
            self.output_scroll_lines = min(self.output_scroll_lines, max_scroll)
            output_end = len(output_lines) - self.output_scroll_lines
            output_start = max(0, output_end - output_rows)
            visible_output = output_lines[output_start:output_end] if output_rows else []

            if output_rows:
                sys.stdout.write(f"\033[1;{output_rows}r")
                for index in range(output_rows):
                    sys.stdout.write(f"\033[{index + 1};1H\033[2K")
                    if index < len(visible_output):
                        sys.stdout.write(visible_output[index])
                sys.stdout.write("\033[r")

            prompt_start = output_rows + 1
            prompt_start_index = max(0, len(prompt_lines) - prompt_rows)
            visible_prompt_lines = prompt_lines[prompt_start_index:]
            for index in range(prompt_rows):
                sys.stdout.write(f"\033[{prompt_start + index};1H\033[2K")
                if index < len(visible_prompt_lines):
                    sys.stdout.write(visible_prompt_lines[index])
            cursor_row, cursor_column = self._cursor_position(
                prompt_text,
                len(self.prompt) + self.input_cursor,
                columns,
            )
            cursor_row = max(0, min(prompt_rows - 1, cursor_row - prompt_start_index))
            sys.stdout.write(
                f"\033[{prompt_start + cursor_row};{max(1, cursor_column)}H"
            )
            sys.stdout.flush()

    def write(self, text: str, ensure_newline: bool = True) -> None:
        """Append output above the child prompt and redraw the reserved rows."""
        self.live_status = None
        columns = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)
        self.output_state.append(text, ensure_newline, columns)
        self._render()

    def commit_prompt(self) -> None:
        """Move the completed child prompt into scrollback before new output."""
        if not (self.prompt or self.input_text):
            return
        with _OUTPUT_LOCK:
            columns = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)
            self.output_state.append(
                self._prompt_text(), ensure_newline=True, columns=columns
            )
            self.prompt = ""
            self.prompt_prefix = None
            self.prompt_prefix_text = None
            self.input_text = ""
            self.input_cursor = 0
            self._render()

    def set_prompt(
        self,
        text: str,
        *,
        system_prefix: str | None = None,
        system_prefix_text: str | None = None,
    ) -> None:
        """Show recognized child prompt text in the reserved bottom area."""
        if self.finished:
            return
        self.live_status = None
        # Input can arrive as soon as the forwarder starts, before the child
        # prompt has made it through the stdout reader. Keep that early input
        # attached to the first prompt instead of committing it to scrollback.
        if self.prompt:
            self.commit_prompt()
        self.prompt = self._sanitize_prompt(text)
        self.prompt_prefix = system_prefix
        self.prompt_prefix_text = system_prefix_text
        self._render()

    def set_input(self, text: str, cursor: int | None = None) -> None:
        """Redraw the child prompt with the shell-owned input buffer."""
        if self.finished:
            return
        self.input_text = self._sanitize_prompt(text)
        self.input_cursor = len(self.input_text) if cursor is None else max(
            0, min(cursor, len(self.input_text))
        )
        self._render()

    def clear_input(self) -> None:
        """Discard unsubmitted child input before restoring the shell prompt."""
        if self.finished or not self.input_text:
            return
        self.input_text = ""
        self.input_cursor = 0
        self._render()

    def write_transient(self, text: str) -> None:
        """Show replaceable progress above the child prompt."""
        self.live_status = sanitize_terminal_text(text)
        self._render()

    def clear_transient(self) -> None:
        if self.live_status is not None:
            self.live_status = None
            self._render()

    def refresh(self) -> None:
        """Redraw after a terminal resize while the child is waiting."""
        self._render()

    def scroll(self, delta: int) -> None:
        """Move the output viewport without moving the active child prompt."""
        with _OUTPUT_LOCK:
            size = shutil.get_terminal_size(fallback=(80, 24))
            columns = max(1, size.columns)
            rows = max(1, size.lines)
            prompt_rows = min(
                rows,
                max(1, len(self._prompt_lines(
                    self._prompt_text(),
                    columns,
                    system_prefix=self.prompt_prefix,
                    system_prefix_text=self.prompt_prefix_text,
                ))),
            )
            output_rows = max(0, rows - prompt_rows)
            output_lines = _output_history_lines(
                "".join(self.output_state.history), columns
            )
            max_scroll = max(0, len(output_lines) - output_rows)
            page = max(1, output_rows - 1)
            self.output_scroll_lines = max(
                0,
                min(max_scroll, self.output_scroll_lines + delta * page),
            )
            self._render()

    def finish(self) -> None:
        """Restore the normal terminal scroll region after the child exits."""
        with _OUTPUT_LOCK:
            self.finished = True
            size = shutil.get_terminal_size(fallback=(80, 24))
            columns = max(1, size.columns)
            if self.prompt or self.input_text:
                self.output_state.append(
                    self._prompt_text(), ensure_newline=True, columns=columns
                )
            self.prompt = ""
            self.prompt_prefix = None
            self.prompt_prefix_text = None
            self.input_text = ""
            self.input_cursor = 0
            self.live_status = None
            self.output_scroll_lines = 0
            self.mode = TerminalMode.OUTPUT
            self.output_state.set_mode(TerminalMode.OUTPUT)
            sys.stdout.write("\033[r")
            sys.stdout.write(f"\033[{max(1, size.lines)};1H")
            sys.stdout.flush()


def _begin_foreground_output(
    output_state: _InteractiveOutputState | None,
    mode: TerminalMode = TerminalMode.OUTPUT,
) -> tuple[_ForegroundOutputState | None, Callable[[str, bool], None] | None]:
    """Reserve the terminal bottom for a foreground child prompt."""
    global _ACTIVE_INTERACTIVE_OUTPUT
    with _OUTPUT_LOCK:
        if output_state is None:
            owner = getattr(_ACTIVE_INTERACTIVE_OUTPUT, "__self__", None)
            if isinstance(owner, _InteractiveOutputState):
                output_state = owner
        if output_state is None:
            return None, None
        previous = _ACTIVE_INTERACTIVE_OUTPUT
        foreground = _ForegroundOutputState(output_state, mode)
        output_state.set_mode(mode)
        _ACTIVE_INTERACTIVE_OUTPUT = foreground.write
        foreground._render()
        return foreground, previous


def _activate_interactive_output(
    state: _InteractiveOutputState,
) -> Callable[[str, bool], None] | None:
    """Route shell output to a persistent viewport until the shell exits."""
    global _ACTIVE_INTERACTIVE_OUTPUT
    with _OUTPUT_LOCK:
        previous = _ACTIVE_INTERACTIVE_OUTPUT
        _ACTIVE_INTERACTIVE_OUTPUT = state.write_without_editor
        return previous


def _restore_interactive_output(
    previous: Callable[[str, bool], None] | None,
) -> None:
    global _ACTIVE_INTERACTIVE_OUTPUT
    with _OUTPUT_LOCK:
        _ACTIVE_INTERACTIVE_OUTPUT = previous


def _mark_resize_pending(_signum: int, _frame: Any) -> None:
    """Record a terminal resize without doing work from the signal handler."""
    global _RESIZE_PENDING
    _RESIZE_PENDING = True


def _take_resize_pending() -> bool:
    global _RESIZE_PENDING
    if not _RESIZE_PENDING:
        return False
    _RESIZE_PENDING = False
    return True


def _terminal_cell_width(character: str) -> int:
    """Return the terminal display-cell width of one printable character."""
    category = unicodedata.category(character)
    if unicodedata.combining(character) or category in {"Cc", "Cf", "Mn", "Me"}:
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _terminal_text_width(text: str) -> int:
    """Return the terminal display-cell width of text without ANSI styling."""
    return sum(_terminal_cell_width(character) for character in strip_ansi(text))


def _advance_terminal_cursor(
    column: int,
    wrap_pending: bool,
    character: str,
    columns: int,
) -> tuple[int, bool]:
    """Advance a terminal cursor using display cells, not Python characters."""
    if character in {"\n", "\r"}:
        return 1, False
    if character == "\b":
        if wrap_pending:
            column = columns
            wrap_pending = False
        return max(1, column - 1), False
    if wrap_pending:
        # Terminals defer the wrap after writing the last cell until the next
        # printable character arrives. The next character therefore starts on
        # the following row (which is the bottom row of our scroll region).
        column = 1
        wrap_pending = False
    if character == "\t":
        next_tab_stop = ((column - 1) // 8 + 1) * 8 + 1
        return min(columns, next_tab_stop), False

    cell_width = _terminal_cell_width(character)
    if cell_width == 0:
        # Zero-cell marks do not consume the deferred wrap state left by a
        # printable character at the right margin.
        return column, wrap_pending
    if cell_width > columns - column + 1:
        column = 1
    last_cell = column + cell_width - 1
    if last_cell >= columns:
        return columns, True
    return last_cell + 1, False


def _output_history_lines(history: str, columns: int) -> list[str]:
    """Wrap retained output into display rows for the output viewport."""
    width_limit = max(1, columns)
    lines: list[str] = []
    current: list[str] = []
    current_width = 0

    def finish_line() -> None:
        nonlocal current, current_width
        lines.append("".join(current))
        current = []
        current_width = 0

    def add_character(character: str) -> None:
        nonlocal current_width
        if character in {"\n", "\r"}:
            finish_line()
            return
        if character in {"\b", "\x7f"}:
            for index in range(len(current) - 1, -1, -1):
                previous = current[index]
                if _ANSI_SEQUENCE.fullmatch(previous):
                    continue
                del current[index]
                current_width = max(0, current_width - 1)
                break
            return
        if character == "\t":
            cell_width = 8 - (current_width % 8)
            character = " " * cell_width
        else:
            cell_width = _terminal_cell_width(character)
            if cell_width == 0:
                current.append(character)
                return

        if current and current_width + cell_width > width_limit:
            finish_line()
        if cell_width > width_limit:
            # A wide character on a one-cell terminal still occupies the
            # available row rather than being lost from the viewport.
            current.append(character)
            current_width = width_limit
            finish_line()
            return
        current.append(character)
        current_width += cell_width
        if current_width == width_limit:
            # Defer the row break until another character or newline arrives,
            # matching the terminal's pending-wrap behavior.
            return

    position = 0
    for match in _ANSI_SEQUENCE.finditer(history):
        for character in history[position:match.start()]:
            add_character(character)
        current.append(match.group())
        position = match.end()
    for character in history[position:]:
        add_character(character)

    if current or not lines:
        finish_line()
    return lines


HELP_SECTION_COLORS = {
    "Commands:": "1;36",
    "Run": "36",
    "Inspect": "36",
    "Setup and cleanup": "36",
    "Session": "36",
    "Notes:": "1;33",
}


_HELP_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Run",
        (
            (
                '/run ISSUE [--profile fast|slow] [--instruction "text"] '
                '[--review|--publish|--mark-done|--new] [--reason "why"] '
                "with --mark-done",
                "start a background job",
            ),
            (
                '/run [--profile fast|slow] [--instruction "text"] '
                '[--review|--publish|--mark-done|--new]',
                "local mode: paste a task, finish with /done",
            ),
            (
                "/run --task-file PATH [OPTIONS]",
                "local mode: use an existing task file",
            ),
        ),
    ),
    (
        "Inspect",
        (
            ("/ps [--all]", "show current or persisted jobs"),
            ("/logs JOB|PID [LINES]", "follow or show captured terminal output"),
            ("/list", "list open GitHub issues"),
            ("/runs [--json]", "list existing Agentic worktrees"),
            ("/status [--json]", "show Agentic worktree status"),
            ("/dashboard [OUTPUT]", "generate and open the HTML dashboard"),
        ),
    ),
    (
        "Setup and cleanup",
        (
            ("/init [OPTIONS]", "initialize target-repository configuration"),
            (
                "/doctor [--json] [--commands]",
                "diagnose prerequisites; optionally run checks",
            ),
            ("/cleanup --issue N [OPTIONS]", "preview or remove clean worktrees"),
            ("/version", "show the Agentic version"),
        ),
    ),
    (
        "Session",
        (
            (
                "/chat ISSUE",
                "open a conversational session in an existing run",
            ),
            (
                "/chat pr PR",
                "repair an existing pull request after CI failure",
            ),
            ("/chat ISSUE MESSAGE", "run an issue with a one-shot instruction"),
            (
                "/chat MESSAGE",
                "start a local one-shot task from chat style (local mode)",
            ),
            ("/stop JOB|--all", "stop one or all running jobs"),
            ("/help", "show this help"),
            ("/quit", "leave the shell; running jobs continue"),
        ),
    ),
)

_HELP_NOTES = (
    "Jobs have no supervisor timeout. Configured Agentic test/Codex timeouts, "
    "if any, still apply inside the child runner.",
    "Type / to open autocomplete. Use Up/Down to navigate, Enter or Tab to "
    "select, and Esc to dismiss. Options and known job IDs are completed too.",
)


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
    "/chat": "open a conversational session in an existing issue run",
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
    cursor = max(0, min(cursor, len(line)))
    total_width = _terminal_text_width(line)
    cursor_width = _terminal_text_width(line[:cursor])
    if total_width <= available:
        return line, cursor_width

    # Choose a display-cell window around the cursor, then move its edges to
    # character boundaries so a wide glyph is never split by clipping.
    start_width = min(max(0, cursor_width - available), total_width - available)
    start = 0
    consumed_width = 0
    while start < len(line):
        character_width = _terminal_cell_width(line[start])
        if consumed_width + character_width > start_width:
            if character_width and consumed_width < start_width:
                # The target falls inside a wide glyph. Skip that glyph
                # instead of starting in the middle of its two cells.
                start += 1
            break
        consumed_width += character_width
        start += 1

    end = start
    visible_width = 0
    while end < len(line):
        character_width = _terminal_cell_width(line[end])
        if character_width and visible_width + character_width > available:
            break
        visible_width += character_width
        end += 1

    visible = line[start:end]
    visible_cursor = min(max(cursor, start), end)
    return visible, _terminal_text_width(line[start:visible_cursor])


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
        if value in {"5~", "5;2~"}:
            return "PAGE_UP", ""
        if value in {"6~", "6;2~"}:
            return "PAGE_DOWN", ""
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
    _ANSI_BYTE_TIMEOUT = 0.05

    def __init__(self, *, raw: bool = False) -> None:
        self._old_settings = None
        self._posix_fd: int | None = None
        self._posix_decoder: Any = None
        self._terminal_size: os.terminal_size | None = None
        self._raw = raw
        # Keep an incomplete escape sequence across read_key calls. A PTY can
        # deliver the bytes of one CSI sequence in separate reads, especially
        # when input is forwarded by another process.
        self._pending_ansi_sequence: str | None = None
        # An isolated Escape is returned before its possible delayed CSI/SS3
        # tail. Keep that state separate from a sequence which is genuinely
        # in progress, and replay a delayed non-sequence as ordinary input.
        self._pending_ansi_escape_reported = False
        self._pending_posix_chars: list[str] = []

    def __enter__(self) -> "_KeyReader":
        self._terminal_size = shutil.get_terminal_size(fallback=(80, 24))
        if os.name != "nt":
            try:
                import termios
                import tty
                fd = sys.stdin.fileno()
                self._posix_fd = fd
                self._old_settings = termios.tcgetattr(fd)
                # The foreground child uses raw mode so Ctrl-C is read as a
                # key and forwarded to the child's process group. cbreak
                # leaves ISIG enabled, which would deliver SIGINT to this
                # shell's main thread instead of reaching read_key().
                if self._raw:
                    tty.setraw(fd)
                else:
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

    def _terminal_resized(self) -> bool:
        current = shutil.get_terminal_size(fallback=(80, 24))
        if self._terminal_size is None:
            self._terminal_size = current
            return False
        if current == self._terminal_size:
            return False
        self._terminal_size = current
        return True

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._old_settings is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass

    def _read_posix_char(self) -> str:
        """Read one character without allowing TextIOWrapper to buffer input."""
        if self._pending_posix_chars:
            return self._pending_posix_chars.pop(0)
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

    @staticmethod
    def _classify_posix_char(char: str) -> tuple[str, str]:
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
        return "CHAR", char

    @staticmethod
    def _decode_complete_ansi(sequence: str) -> tuple[str, str] | None:
        """Decode a buffered sequence, or return None while it is incomplete."""
        if not sequence:
            return None
        if sequence[0] == "[":
            if len(sequence) == 1:
                return None
            # CSI sequences end at a parameter-final letter or '~'.
            if sequence[-1] != "~" and not sequence[-1].isalpha():
                return None
        elif sequence[0] == "O":
            if len(sequence) == 1:
                return None
            if sequence[1] in {"1", "3", "4"} and len(sequence) == 2:
                return None
        else:
            # The ESC has already been handled. The next ordinary character
            # is independent input, not an escape sequence byte.
            return "CHAR", sequence[0]

        iterator = iter(sequence)
        result = _parse_ansi_sequence(lambda: next(iterator, None))
        return result

    def _read_posix_char_if_ready(self, fd: int) -> str | None:
        import select

        if self._pending_posix_chars:
            return self._read_posix_char()
        deadline = time.monotonic() + self._ANSI_BYTE_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                readable, _, _ = select.select([fd], [], [], remaining)
            except InterruptedError:
                continue
            if not readable:
                return None
            return self._read_posix_char()

    def _read_posix_ansi(
        self, fd: int, *, initial: bool, stop_event: threading.Event | None,
    ) -> tuple[str, str]:
        sequence = self._pending_ansi_sequence or ""
        escape_reported = self._pending_ansi_escape_reported
        self._pending_ansi_sequence = None
        self._pending_ansi_escape_reported = False
        while True:
            decoded = self._decode_complete_ansi(sequence)
            if decoded is not None:
                if escape_reported and decoded[0] == "ESCAPE" and sequence:
                    # The Escape which could have introduced this sequence was
                    # already returned on an earlier read. If the delayed
                    # candidate is not a known terminal key, preserve it as
                    # literal input and replay everything after its first byte.
                    self._pending_posix_chars.extend(sequence[1:])
                    return self._classify_posix_char(sequence[0])
                return decoded
            if stop_event is not None and stop_event.is_set():
                self._pending_ansi_sequence = sequence
                self._pending_ansi_escape_reported = escape_reported
                return "STOP", ""
            char = self._read_posix_char_if_ready(fd)
            if char is None:
                if _take_resize_pending() or self._terminal_resized():
                    self._pending_ansi_sequence = sequence
                    self._pending_ansi_escape_reported = escape_reported
                    return "RESIZE", ""
                self._pending_ansi_sequence = sequence
                self._pending_ansi_escape_reported = escape_reported or initial
                # The first ESC should behave like a normal Escape key. Once
                # reported, subsequent idle polls must not redraw the editor.
                return ("ESCAPE", "\x1b") if initial else ("IDLE", "")
            if not char:
                self._pending_ansi_sequence = None
                self._pending_ansi_escape_reported = False
                return "CTRL_D", ""
            if char == "\x1b":
                if sequence and escape_reported:
                    self._pending_posix_chars.extend(sequence[1:])
                    self._pending_posix_chars.append(char)
                    self._pending_ansi_sequence = None
                    self._pending_ansi_escape_reported = False
                    return self._classify_posix_char(sequence[0])
                self._pending_ansi_sequence = ""
                self._pending_ansi_escape_reported = True
                return "ESCAPE", "\x1b"
            if not sequence and char not in {"[", "O"}:
                # An ordinary byte after Escape is not part of an ANSI
                # sequence. Return the Escape first and replay the byte so
                # rapidly typed literal input is not lost.
                self._pending_posix_chars.append(char)
                self._pending_ansi_sequence = None
                self._pending_ansi_escape_reported = False
                return "ESCAPE", "\x1b"
            sequence += char

    def read_key(self, stop_event: threading.Event | None = None) -> tuple[str, str]:
        if os.name == "nt":
            import msvcrt
            getwch = cast(Callable[[], str], getattr(msvcrt, "getwch"))
            while not msvcrt.kbhit():
                if stop_event is not None and stop_event.is_set():
                    return "STOP", ""
                if _take_resize_pending() or self._terminal_resized():
                    return "RESIZE", ""
                time.sleep(0.05)
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
                if code == "I":
                    return "PAGE_UP", ""
                if code == "Q":
                    return "PAGE_DOWN", ""
                if code == "\x0f":
                    return "UP", ""
                return "SPECIAL", code
            return "CHAR", char

        import select
        fd = self._posix_fd if self._posix_fd is not None else sys.stdin.fileno()
        if self._pending_ansi_sequence is not None:
            key, value = self._read_posix_ansi(
                fd, initial=False, stop_event=stop_event,
            )
            if key == "PASTE_START":
                return "PASTE", self._read_paste()
            return key, value
        if self._pending_posix_chars:
            char = self._read_posix_char()
            if char == "\x1b":
                key, value = self._read_posix_ansi(
                    fd, initial=True, stop_event=stop_event,
                )
                if key == "PASTE_START":
                    return "PASTE", self._read_paste()
                return key, value
            return self._classify_posix_char(char)
        while True:
            if stop_event is not None and stop_event.is_set():
                return "STOP", ""
            try:
                readable, _, _ = select.select([fd], [], [], 0.1)
            except InterruptedError:
                readable = []
            if readable:
                break
            if _take_resize_pending() or self._terminal_resized():
                return "RESIZE", ""
        if stop_event is not None and stop_event.is_set():
            return "STOP", ""
        char = self._read_posix_char()
        if char == "\x1b":
            key, value = self._read_posix_ansi(
                fd, initial=True, stop_event=stop_event,
            )
            if key == "PASTE_START":
                return "PASTE", self._read_paste()
            return key, value
        return self._classify_posix_char(char)



def _interactive_input(
    prompt: str,
    completer: Callable[[str, int | None], list[tuple[str, str]]],
    history: list[str] | None = None,
    key_reader_factory: Callable[[], Any] | None = None,
    status_line: str | None = None,
    output_state: _InteractiveOutputState | None = None,
    terminal_mode: TerminalMode = TerminalMode.COMMAND_READY,
    system_prefix: str | None = None,
    system_prefix_text: str | None = None,
) -> str:
    """Read one or more shell lines with autocomplete, paste, and navigation."""
    line = ""
    cursor_pos = 0
    selected_index = 0
    navigated = False
    dismissed = False
    prev_dropdown_lines = 0
    prev_editor_lines = 0
    prev_cursor_row = 0
    candidates: list[tuple[str, str]] = []
    output_state = output_state or _InteractiveOutputState()
    output_state.set_mode(terminal_mode)
    output_history = output_state.history
    output_scroll_lines = 0

    hist_entries = list(history) if history else []
    hist_index = len(hist_entries)
    saved_input = ""

    def _clear_previous_editor(buf: list[str]) -> None:
        """Clear the old bottom-anchored region from its current cursor."""
        if not prev_editor_lines:
            return
        buf.append("\r")
        if prev_cursor_row:
            buf.append(f"\033[{prev_cursor_row}A")
        for index in range(prev_editor_lines):
            buf.append("\033[2K")
            if index < prev_editor_lines - 1:
                buf.append("\n")
        if prev_editor_lines > 1:
            buf.append(f"\033[{prev_editor_lines - 1}A\r")

    def render() -> None:
        nonlocal prev_dropdown_lines, prev_editor_lines, prev_cursor_row
        nonlocal selected_index, navigated, candidates, output_scroll_lines
        with _OUTPUT_LOCK:
            size = shutil.get_terminal_size(fallback=(80, 24))
            columns = max(1, size.columns)
            rows = max(1, size.lines)
            output_state.recompute_cursor(columns)

            if not dismissed and line.lstrip().startswith("/"):
                try:
                    candidates = completer(line, cursor_pos)
                except Exception:
                    candidates = []
            else:
                candidates = []

            input_parts = line.split("\n")
            cursor_row = line[:cursor_pos].count("\n")
            cursor_column = cursor_pos - (line.rfind("\n", 0, cursor_pos) + 1)
            status_strings: list[str] = []
            if status_line is not None:
                status = _fit_terminal_text(sanitize_terminal_text(status_line), columns - 1)
                status_strings.append(_shell_paint(status, "90"))

            # Keep the prompt visible on very short terminals. The status line
            # is useful, but it must not push the editable row below the
            # viewport.
            if rows <= len(status_strings):
                status_strings = []

            # The input region normally has one blank row above and below the
            # editable rows. On short terminals, preserve the prompt, one
            # output row when history exists, and a small completion viewport
            # before spending rows on padding.
            output_history_text = "".join(output_history)
            output_lines = _output_history_lines(output_history_text, columns)
            output_reserve = min(1, len(output_lines)) if output_history_text else 0
            available_rows = max(0, rows - len(status_strings))
            completion_reserve = min(
                2,
                len(candidates),
                max(0, available_rows - 1 - output_reserve),
            )
            padding_budget = max(
                0,
                available_rows - 1 - completion_reserve - output_reserve,
            )
            top_padding_rows = min(1, padding_budget)
            bottom_padding_rows = min(
                1, max(0, padding_budget - top_padding_rows),
            )
            max_input_lines = max(
                1,
                available_rows
                - completion_reserve
                - top_padding_rows
                - bottom_padding_rows
                - output_reserve,
            )

            # Keep the cursor visible when a pasted buffer is taller than the
            # terminal. The remaining rows are then available to completion.
            input_start = max(0, min(
                cursor_row - max_input_lines + 1,
                len(input_parts) - max_input_lines,
            ))
            input_end = min(len(input_parts), input_start + max_input_lines)
            visible_parts = input_parts[input_start:input_end]
            visible_cursor_row = cursor_row - input_start

            if not candidates:
                selected_index = 0
                navigated = False
                visible_items: list[tuple[str, str]] = []
                visible_selected = -1
            else:
                selected_index = max(0, min(selected_index, len(candidates) - 1))
                available_dropdown = max(
                    0,
                    rows
                    - len(status_strings)
                    - top_padding_rows
                    - bottom_padding_rows
                    - output_reserve
                    - len(visible_parts),
                )
                max_visible = min(8, available_dropdown, len(candidates))
                if max_visible:
                    window_start = max(
                        0,
                        min(selected_index - max_visible // 2,
                            len(candidates) - max_visible),
                    )
                    visible_items = candidates[window_start:window_start + max_visible]
                    visible_selected = selected_index - window_start
                else:
                    visible_items = []
                    visible_selected = -1

            use_color = not (os.environ.get("NO_COLOR") or os.environ.get("AGENTIC_PLAIN"))
            dropdown_strings: list[str] = []
            if visible_items:
                cmd_col = min(24, max((len(c) for c, _ in visible_items), default=12) + 2)
                max_content_len = max(
                    len("> " if idx == visible_selected else "  ") + cmd_col + 2 + len(desc)
                    for idx, (_, desc) in enumerate(visible_items)
                )
                banner_width = max(1, min(columns - 1, max_content_len + 2))
                for idx, (cmd, desc) in enumerate(visible_items):
                    is_active = idx == visible_selected
                    prefix = "> " if is_active else "  "
                    desc_col = max(1, columns - len(prefix) - cmd_col - 4)
                    if len(desc) > desc_col:
                        desc = desc[:max(0, desc_col - 3)] + "..." if desc_col >= 4 else desc[:desc_col]
                    content = f"{prefix}{cmd:<{cmd_col}}  {desc}".ljust(banner_width)
                    content = content[:max(1, columns - 1)]
                    if use_color and is_active:
                        dropdown_strings.append(f"\033[44;97m{content}\033[0m")
                    elif use_color:
                        dropdown_strings.append(
                            f"{prefix}\033[97m{cmd:<{cmd_col}}\033[0m  \033[90m{desc}\033[0m"
                        )
                    else:
                        dropdown_strings.append(content)

            display_lines: list[str] = []
            cursor_display = 0
            for offset, part in enumerate(visible_parts):
                original_row = input_start + offset
                row_prompt = prompt if original_row == 0 else "  "
                row_cursor = cursor_column if original_row == cursor_row else len(part)
                visible, visible_cursor = _clip_input_line(
                    part, row_cursor, _terminal_text_width(row_prompt), columns,
                )
                if original_row == cursor_row:
                    cursor_display = visible_cursor
                display_lines.append(_input_row(
                    row_prompt,
                    visible,
                    columns,
                    system_prefix=system_prefix if original_row == 0 else None,
                    system_prefix_text=system_prefix_text if original_row == 0 else None,
                ))

            input_padding = [
                _input_row("", "", columns)
                for _ in range(top_padding_rows)
            ]
            input_padding.extend(
                _input_row("", "", columns)
                for _ in range(bottom_padding_rows)
            )
            editor_rows = (
                status_strings
                + dropdown_strings
                + input_padding[:top_padding_rows]
                + display_lines
                + input_padding[top_padding_rows:]
            )
            editor_lines = len(editor_rows)
            output_rows = max(0, rows - editor_lines)
            max_output_scroll = max(0, len(output_lines) - output_rows)
            output_scroll_lines = min(output_scroll_lines, max_output_scroll)
            top_row = max(1, rows - editor_lines + 1)
            cursor_from_top = (
                len(status_strings)
                + len(dropdown_strings)
                + top_padding_rows
                + visible_cursor_row
            )
            buf: list[str] = []
            _clear_previous_editor(buf)
            buf.append("\033[r")
            if output_rows and output_history:
                output_end = len(output_lines) - output_scroll_lines
                output_start = max(0, output_end - output_rows)
                visible_output = output_lines[output_start:output_end]
                buf.append(f"\033[1;{output_rows}r\033[1;1H")
                for index in range(output_rows):
                    buf.append("\033[2K")
                    if index < len(visible_output):
                        buf.append(visible_output[index])
                    if index < output_rows - 1:
                        buf.append("\n")
                buf.append("\033[r")
            buf.append(f"\033[{top_row};1H")
            if prev_editor_lines:
                for index in range(editor_lines):
                    buf.append("\033[2K")
                    if index < editor_lines - 1:
                        buf.append("\n")
                if editor_lines > 1:
                    buf.append(f"\033[{editor_lines - 1}A\r")
            for index, editor_row in enumerate(editor_rows):
                if index:
                    buf.append("\n")
                buf.append(editor_row)
            cursor_up = editor_lines - 1 - cursor_from_top
            if cursor_up:
                buf.append(f"\033[{cursor_up}A")
            cursor_col = (
                _terminal_text_width(prompt) + cursor_display + 1
                if cursor_row == 0
                else _terminal_text_width("  ") + cursor_display + 1
            )
            buf.append(f"\033[{max(1, cursor_col)}G")
            sys.stdout.write("".join(buf))
            sys.stdout.flush()
            prev_dropdown_lines = len(dropdown_strings)
            prev_editor_lines = editor_lines
            prev_cursor_row = cursor_from_top

    def write_output(text: str, ensure_newline: bool = True) -> None:
        """Write output above the editor and restore the editor below it."""
        nonlocal prev_editor_lines
        with _OUTPUT_LOCK:
            size = shutil.get_terminal_size(fallback=(80, 24))
            columns = max(1, size.columns)
            rows = max(1, size.lines)
            output_state.recompute_cursor(columns)
            old_editor_lines = prev_editor_lines
            clear: list[str] = []
            _clear_previous_editor(clear)
            if clear:
                sys.stdout.write("".join(clear))

            # The editor occupies the bottom rows. Scroll only the rows above
            # it so streamed output cannot overwrite the status or prompt.
            output_bottom = max(1, rows - old_editor_lines)
            output_state.cursor_column = min(max(1, output_state.cursor_column), columns)
            if output_state.cursor_wrap_pending and text:
                output_state.cursor_column = 1
                output_state.cursor_wrap_pending = False
            sys.stdout.write(f"\033[1;{output_bottom}r")
            sys.stdout.write(f"\033[{output_bottom};{output_state.cursor_column}H")
            prev_editor_lines = 0
            output_text = output_state.append(text, ensure_newline, columns)

            if output_scroll_lines:
                # A user reviewing older output should not be pulled back to
                # the live tail by an arriving log chunk.
                prev_editor_lines = 0
                sys.stdout.write("\033[r")
                sys.stdout.flush()
                render()
                return

            sys.stdout.write(output_text)

            sys.stdout.write("\033[r")
            sys.stdout.flush()
            render()

    def scroll_output(delta: int) -> None:
        """Move the retained output viewport without moving the editor."""
        nonlocal output_scroll_lines
        with _OUTPUT_LOCK:
            size = shutil.get_terminal_size(fallback=(80, 24))
            columns = max(1, size.columns)
            rows = max(1, size.lines)
            output_rows = max(0, rows - prev_editor_lines)
            if not output_rows:
                return
            output_lines = _output_history_lines("".join(output_history), columns)
            max_scroll = max(0, len(output_lines) - output_rows)
            page = max(1, output_rows - 1)
            output_scroll_lines = max(
                0, min(max_scroll, output_scroll_lines + delta * page)
            )
        render()

    def clear_dropdown() -> None:
        nonlocal dismissed
        if prev_dropdown_lines > 0:
            dismissed = True
            render()

    def finish_input() -> None:
        """Commit a submitted line as plain transcript output."""
        with _OUTPUT_LOCK:
            size = shutil.get_terminal_size(fallback=(80, 24))
            columns = max(1, size.columns)
            output_state.mark_prompt_boundary(columns)
            if line:
                clear: list[str] = []
                _clear_previous_editor(clear)
                if clear:
                    sys.stdout.write("".join(clear))
                submitted = sanitize_terminal_text(prompt + line)
                output_text = output_state.append(
                    submitted, ensure_newline=True, columns=columns
                )
                sys.stdout.write(output_text)
            else:
                if prev_cursor_row < prev_editor_lines - 1:
                    sys.stdout.write(f"\033[{prev_editor_lines - 1 - prev_cursor_row}B")
                sys.stdout.write("\r\n")
            sys.stdout.flush()

    reader_factory = key_reader_factory or _KeyReader
    resize_handler_previous = None
    resize_handler_installed = False
    if os.name != "nt" and hasattr(signal, "SIGWINCH"):
        try:
            resize_handler_previous = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, _mark_resize_pending)
            resize_handler_installed = True
        except (OSError, ValueError):
            # Embedded callers and non-main threads cannot install signal
            # handlers; the reader's size polling remains a safe fallback.
            resize_handler_previous = None
    global _ACTIVE_INTERACTIVE_OUTPUT
    try:
        with reader_factory() as reader:
            with _OUTPUT_LOCK:
                previous_output = _ACTIVE_INTERACTIVE_OUTPUT
                _ACTIVE_INTERACTIVE_OUTPUT = write_output
                sys.stdout.write("\033[?2004h")
                render()
            try:
                while True:
                    key, char = reader.read_key()

                    if key == "RESIZE":
                        render()
                        continue

                    if key == "PAGE_UP":
                        scroll_output(1)
                        continue

                    if key == "PAGE_DOWN":
                        scroll_output(-1)
                        continue

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
                with _OUTPUT_LOCK:
                    if _ACTIVE_INTERACTIVE_OUTPUT is write_output:
                        _ACTIVE_INTERACTIVE_OUTPUT = previous_output
                    output_state.set_mode(TerminalMode.OUTPUT)
                    clear_dropdown()
                    sys.stdout.write("\033[?2004l")
                    sys.stdout.flush()
    finally:
        with _OUTPUT_LOCK:
            output_state.set_mode(TerminalMode.OUTPUT)
        if resize_handler_installed:
            try:
                signal.signal(signal.SIGWINCH, resize_handler_previous)
            except (OSError, ValueError):
                pass


def _interactive_line(
    prompt: str,
    *,
    history: list[str] | None = None,
    completer: Callable[[str, int | None], list[tuple[str, str]]] | None = None,
    system_prefix: str | None = None,
    system_prefix_text: str | None = None,
) -> str:
    """Read one edited line on a TTY and retain plain-input behavior elsewhere."""
    if _is_interactive_terminal():
        return _interactive_input(
            prompt,
            completer or (lambda _line, _cursor: []),
            history=history,
            system_prefix=system_prefix,
            system_prefix_text=system_prefix_text,
        )
    return input(prompt)



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


def _input_row(
    prompt: str,
    text: str,
    columns: int,
    *,
    system_prefix: str | None = None,
    system_prefix_text: str | None = None,
) -> str:
    """Render one full-width editable row with a resettable TTY background."""
    safe_prompt = sanitize_terminal_text(prompt)
    safe_text = sanitize_terminal_text(text)
    prompt_display = (
        style_system_text(
            safe_prompt,
            system_prefix=system_prefix,
            system_prefix_text=system_prefix_text,
            style_status_tokens=False,
        )
        if system_prefix is not None else safe_prompt
    )
    content = safe_prompt + safe_text
    padding = " " * max(0, columns - _terminal_text_width(content))
    if not color_enabled():
        return content + padding
    return (
        "\033[48;5;236m"
        f"\033[1;36m{prompt_display}"
        f"\033[97m{safe_text}{padding}"
        "\033[0m"
    )


def _write_shell_output(text: str, *, ensure_newline: bool = True) -> None:
    """Write shell output through the active editor when one is present."""
    with _OUTPUT_LOCK:
        if _ACTIVE_INTERACTIVE_OUTPUT is not None:
            _ACTIVE_INTERACTIVE_OUTPUT(text, ensure_newline)
            return
        sys.stdout.write(text)


def _shell_print(message: Any = "", *, end: str = "\n", flush: bool = False,
                 style_status_tokens: bool = True,
                 system_prefix: str | None = None) -> None:
    """Print shell status or replayed log text with terminal-only styling."""
    renderer = style_system_text if system_prefix is not None else style_text
    kwargs = {"system_prefix": system_prefix} if system_prefix is not None else {}
    text = renderer(message, style_status_tokens=style_status_tokens, **kwargs)
    _write_shell_output(text + end)
    if flush:
        sys.stdout.flush()


def _shell_print_raw(message: Any = "", *, end: str = "\n", flush: bool = False) -> None:
    """Print machine-oriented child output without adding terminal decoration."""
    _write_shell_output(strip_ansi(message) + end)
    if flush:
        sys.stdout.flush()


def _shell_write_log(text: str) -> None:
    """Write appended log text without bypassing the TTY styling boundary."""
    _write_shell_output(
        style_text(text, style_status_tokens=False),
        ensure_newline=False,
    )
    sys.stdout.flush()


def _shell_write_transient(text: str) -> None:
    """Draw provider progress as one live row when the shell owns the TTY."""
    callback = _ACTIVE_INTERACTIVE_OUTPUT
    owner = getattr(callback, "__self__", None)
    if isinstance(owner, (_InteractiveOutputState, _ForegroundOutputState)):
        owner.write_transient(text)
        return
    _shell_print(text, style_status_tokens=False, flush=True)


def _shell_clear_transient() -> None:
    """Remove a live provider status before returning to the shell prompt."""
    callback = _ACTIVE_INTERACTIVE_OUTPUT
    owner = getattr(callback, "__self__", None)
    if isinstance(owner, _ForegroundOutputState):
        owner.clear_transient()
        return
    if isinstance(owner, _InteractiveOutputState) and owner.live_status:
        with _OUTPUT_LOCK:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()
            owner.live_status = False


def _wrap_help_text(
    text: str,
    width: int,
    *,
    initial_indent: str = "",
    subsequent_indent: str = "",
) -> list[str]:
    """Wrap help text without allowing indentation to exceed the viewport."""
    width = max(1, width)
    initial_indent = initial_indent[:max(0, width - 1)]
    subsequent_indent = subsequent_indent[:max(0, width - 1)]
    return textwrap.wrap(
        text,
        width=width,
        initial_indent=initial_indent,
        subsequent_indent=subsequent_indent,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [initial_indent]


def _help_heading(text: str, columns: int, indent: str = "  ") -> list[str]:
    """Render a heading at a safe indentation for very narrow terminals."""
    return _wrap_help_text(
        text,
        columns,
        initial_indent=indent,
        subsequent_indent=indent,
    )


def _help_two_column_entry(
    syntax: str,
    description: str,
    columns: int,
    description_column: int,
) -> list[str]:
    """Render one command with syntax on the left and text on the right."""
    left_indent = "    "
    left_width = max(1, description_column - len(left_indent) - 1)
    syntax_lines = _wrap_help_text(
        syntax,
        left_width,
        subsequent_indent="  ",
    )
    description_lines = _wrap_help_text(
        description,
        max(1, columns - description_column),
    )
    rendered: list[str] = []
    first_description = description_lines[0]
    first_syntax = left_indent + syntax_lines[0]
    rendered.append(
        first_syntax
        + " " * max(1, description_column - len(first_syntax))
        + first_description
    )
    for index in range(1, max(len(syntax_lines), len(description_lines))):
        syntax_line = (
            left_indent + syntax_lines[index]
            if index < len(syntax_lines)
            else ""
        )
        if index < len(description_lines):
            description_text = description_lines[index]
            if syntax_line:
                rendered.append(
                    syntax_line
                    + " " * max(1, description_column - len(syntax_line))
                    + description_text
                )
            else:
                rendered.append((" " * description_column) + description_text)
        else:
            rendered.append(syntax_line)
    return rendered


def _help_stacked_entry(syntax: str, description: str, columns: int) -> list[str]:
    """Render help vertically when two columns would be too cramped."""
    syntax_lines = _wrap_help_text(
        syntax,
        max(1, columns - 4),
        initial_indent="    ",
        subsequent_indent="      ",
    )
    description_lines = _wrap_help_text(
        description,
        columns,
        initial_indent="      ",
        subsequent_indent="      ",
    )
    return syntax_lines + description_lines


def _render_help_lines(columns: int = 80) -> list[str]:
    """Build the interactive help screen for the current terminal width."""
    columns = max(1, columns)
    lines = _help_heading("Commands:", columns, indent="")
    lines.append("")

    # Keep the description column stable on normal terminals. Below this
    # threshold, a stacked layout keeps both syntax and descriptions readable
    # instead of forcing a two-column layout into a handful of cells.
    two_column = columns >= 64
    description_column = min(42, max(24, columns // 2))
    for section, entries in _HELP_SECTIONS:
        lines.extend(_help_heading(section, columns))
        for syntax, description in entries:
            if two_column:
                lines.extend(
                    _help_two_column_entry(
                        syntax,
                        description,
                        columns,
                        description_column,
                    )
                )
            else:
                lines.extend(_help_stacked_entry(syntax, description, columns))
        lines.append("")

    lines.extend(_help_heading("Notes:", columns, indent=""))
    for index, note in enumerate(_HELP_NOTES):
        lines.extend(
            _wrap_help_text(
                note,
                columns,
                initial_indent="  ",
                subsequent_indent="  ",
            )
        )
        if index < len(_HELP_NOTES) - 1:
            lines.append("")
    return lines


# Keep the historical public constant available to integrations. The actual
# renderer uses the live terminal width so help remains useful after resizing.
HELP = "\n".join(_render_help_lines()) + "\n"


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
    """Print width-aware command help with terminal-only presentation."""
    columns = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)
    for line in _render_help_lines(columns):
        _write_shell_output(_help_line(line) + "\n")


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
    def write_line(text: str = "") -> None:
        """Send banner lines through the retained interactive output path."""
        _write_shell_output(text + "\n")

    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    # A framed banner is useful on normal terminals, but should not wrap on
    # split panes or small CI terminals.
    if columns < 62:
        write_line(_shell_paint(f"AGENTIC v{__version__}", "1;36"))
        write_line(_shell_paint(
            f"Interactive coding workflow - {_fit_terminal_text(repo.name, max(columns - 34, 8))}",
            "36",
        ))
        write_line(f"{active_jobs} running job(s); use /help for commands.")
        if previous_jobs:
            write_line(_shell_paint(f"{previous_jobs} previous job(s); use /ps --all to inspect.", "33"))
        write_line(_shell_paint("Use /help for commands. Jobs continue after /quit.", "90"))
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

    write_line()
    write_line(_shell_paint(border(left, right), "36"))
    write_line(_shell_paint(row(f"{marker}  A G E N T I C  v{__version__}"), "1;36"))
    write_line(_shell_paint(row("   Interactive issue-driven coding workflow"), "36"))
    write_line(_shell_paint(divider + horizontal * inner + ("┤" if unicode_banner else "+"), "36"))
    write_line(row(f"Repository  {repo}"))
    write_line(row(f"State       {state_text}"))
    write_line(row(f"Config      {config_text}"))
    write_line(row(f"Jobs        {job_text}"))
    write_line(_shell_paint(row("Commands    /run ISSUE   /ps   /logs JOB|PID   /help   /quit"), "36"))
    write_line(_shell_paint(border(bottom_left, bottom_right), "36"))
    if previous_jobs:
        write_line(_shell_paint("Tip: use /ps --all to inspect jobs from previous sessions.", "33"))
    write_line()


# Explicitly export private compatibility helpers to the facade.
__all__ = [name for name in globals() if not name.startswith("__")]
# End of shell input boundary.
