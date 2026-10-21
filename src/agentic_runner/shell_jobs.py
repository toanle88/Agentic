"""Process and durable-job primitives used by the interactive shell.

The shell facade owns command dispatch and terminal-mode transitions.  This
module owns the independent job representation, process-tree cancellation,
bounded log observation, and foreground input forwarding boundary.
"""
from __future__ import annotations

from collections.abc import Iterable
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from .output import ConciseLogStream
from .shell_input import _KeyReader


@dataclass
class Job:
    number: int
    argv: list[str]
    log_path: Path
    process: subprocess.Popen[Any] | None
    started: float
    log_handle: TextIO | None
    record_path: Path
    session_id: str
    persisted_pid: int = 0
    finished: float | None = None
    exit_code: int | None = None
    notified: bool = False
    completion_callback: Callable[[int], None] | None = None
    stop_requested: bool = False

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


def job_for_issue(jobs: Iterable[Job], issue: int) -> Job | None:
    """Find a persisted or active job whose runner targets an issue."""
    issue_text = str(issue)
    for job in jobs:
        for index, argument in enumerate(job.argv):
            if argument == "--issue" and index + 1 < len(job.argv):
                if job.argv[index + 1] == issue_text:
                    return job
            elif argument == f"--issue={issue_text}":
                return job
    return None


def tail(path: Path, lines: int, *, verbose: bool = False) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read {path}: {exc}"
    text = text[-200_000:]
    if not verbose:
        concise = ConciseLogStream()
        text = concise.feed(text) + concise.flush()
    selected = text.splitlines()[-lines:]
    return "\n".join(selected) or "(log is empty)"


def tail_from_text(text: str, lines: int, *, verbose: bool = False) -> str:
    """Return a bounded tail from text already read from an open log."""
    text = text[-200_000:]
    if not verbose:
        concise = ConciseLogStream()
        text = concise.feed(text) + concise.flush()
    selected = text.splitlines()[-lines:]
    return "\n".join(selected) or "(log is empty)"


def pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def signal_process_tree(process: subprocess.Popen[Any], signum: int) -> None:
    """Signal a foreground process and descendants in its isolated group."""
    if os.name != "nt":
        try:
            pid = int(process.pid)
            process_group = os.getpgid(pid)
            # Foreground children are started in a new session. Keep this
            # guard so a test double or an unexpected caller cannot signal
            # the shell's own process group.
            if process_group > 0 and process_group != os.getpgrp():
                os.killpg(process_group, signum)
                return
        except (AttributeError, OSError, ProcessLookupError, TypeError, ValueError):
            pass
    try:
        process.send_signal(signum)
    except (AttributeError, OSError, ProcessLookupError, TypeError, ValueError):
        try:
            process.terminate()
        except (AttributeError, OSError, ProcessLookupError, TypeError, ValueError):
            pass


def is_foreground_prompt(text: str) -> bool:
    """Recognize the child prompts that are safe to make editable."""
    return text in FOREGROUND_PROMPT_PREFIXES


FOREGROUND_PROMPT_PREFIXES = {
    "[RESUME] Waiting for worktree selection. Enter a number or 'q' to cancel: ": "RESUME",
    "[PUBLISH] Waiting for worktree selection. Enter a number or 'q' to cancel: ": "PUBLISH",
    "[PUBLISH] Publish this patch? [y/N]: ": "PUBLISH",
    "[PUBLISH] Waiting for publication confirmation. Type 'publish' to continue or 'q' to cancel: ": "PUBLISH",
    # Retain recognition for logs and older runner children.
    "[PUBLISH] Publish this patch? Type 'publish' to approve: ": "PUBLISH",
}


def foreground_prompt_prefix(text: str) -> str | None:
    """Return styling metadata for a recognized child prompt."""
    return FOREGROUND_PROMPT_PREFIXES.get(text)


def display_escape(character: str) -> str:
    """Decode one safe shell escape for terminal display only."""
    return {
        "a": "<BEL>",
        "b": "<BS>",
        "f": "<FF>",
        "n": "\n",
        "r": "\n",
        "t": "    ",
        "v": "<VT>",
        "\\": "\\",
        "'": "'",
        '"': '"',
    }.get(character, "\\" + character)


def normalize_foreground_output(text: str) -> str:
    """Make recognized shell-escaped command output readable."""
    normalized: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("$'", index):
            decoded: list[str] = []
            cursor = index + 2
            closed = False
            while cursor < len(text):
                character = text[cursor]
                if character == "'":
                    cursor += 1
                    closed = True
                    break
                if character == "\\" and cursor + 1 < len(text):
                    decoded.append(display_escape(text[cursor + 1]))
                    cursor += 2
                    continue
                decoded.append(character)
                cursor += 1
            if closed:
                normalized.extend(decoded)
                index = cursor
                continue
        normalized.append(text[index])
        index += 1
    return "".join(normalized)


class ForegroundInputForwarder:
    """Own a child line buffer while keeping the terminal echo under control."""

    def __init__(self, process: subprocess.Popen[str], foreground) -> None:
        self.process = process
        self.foreground = foreground
        self.reader = _KeyReader(raw=True)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.buffer = ""
        self.cursor = 0
        self.cancel_reason: str | None = None

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run,
            name="agentic-foreground-input",
            daemon=True,
        )
        self.thread.start()

    def _set_noecho(self) -> None:
        if os.name == "nt":
            return
        try:
            import termios

            fd = self.reader._posix_fd
            if fd is None:
                return
            settings = termios.tcgetattr(fd)
            settings[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, settings)
        except (OSError, ValueError):
            return

    def _send_line(self) -> None:
        if self.process.stdin is None:
            return
        if not self.buffer and not getattr(self.foreground, "prompt", ""):
            return
        self.foreground.commit_prompt()
        try:
            self.process.stdin.write(self.buffer + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.stop_event.set()
        self.buffer = ""
        self.cursor = 0
        self.foreground.set_input("", 0)

    def _cancel(self, reason: str, signum: int) -> None:
        """Cancel the child and stop reading keys after reporting the reason."""
        self.cancel_reason = reason
        signal_process_tree(self.process, signum)
        self.buffer = ""
        self.cursor = 0
        self.foreground.set_input("", 0)
        self.stop_event.set()

    def _read_key(self) -> tuple[str, str]:
        if isinstance(self.reader, _KeyReader):
            return self.reader.read_key(self.stop_event)
        return self.reader.read_key()

    def _insert(self, text: str) -> None:
        self.buffer = self.buffer[:self.cursor] + text + self.buffer[self.cursor:]
        self.cursor += len(text)
        self.foreground.set_input(self.buffer, self.cursor)

    def _run(self) -> None:
        try:
            self.reader.__enter__()
            self._set_noecho()
            self.foreground.set_input("", 0)
            while not self.stop_event.is_set():
                key, char = self._read_key()
                if key == "STOP":
                    break
                if key == "RESIZE":
                    self.foreground.refresh()
                elif key in {"CHAR", "NEWLINE", "PASTE"}:
                    self._insert(char if key != "NEWLINE" else "\n")
                elif key == "LEFT":
                    self.cursor = max(0, self.cursor - 1)
                    self.foreground.set_input(self.buffer, self.cursor)
                elif key == "RIGHT":
                    self.cursor = min(len(self.buffer), self.cursor + 1)
                    self.foreground.set_input(self.buffer, self.cursor)
                elif key == "HOME":
                    self.cursor = 0
                    self.foreground.set_input(self.buffer, self.cursor)
                elif key == "END":
                    self.cursor = len(self.buffer)
                    self.foreground.set_input(self.buffer, self.cursor)
                elif key == "BACKSPACE":
                    if self.cursor:
                        self.buffer = self.buffer[:self.cursor - 1] + self.buffer[self.cursor:]
                        self.cursor -= 1
                        self.foreground.set_input(self.buffer, self.cursor)
                elif key == "DELETE":
                    if self.cursor < len(self.buffer):
                        self.buffer = self.buffer[:self.cursor] + self.buffer[self.cursor + 1:]
                        self.foreground.set_input(self.buffer, self.cursor)
                elif key == "CTRL_U":
                    self.buffer = self.buffer[self.cursor:]
                    self.cursor = 0
                    self.foreground.set_input(self.buffer, self.cursor)
                elif key == "CTRL_C":
                    self._cancel("Ctrl-C", signal.SIGINT)
                elif key == "CTRL_D":
                    if not self.buffer and self.process.stdin is not None:
                        try:
                            self.process.stdin.close()
                        except (OSError, ValueError):
                            pass
                        self.stop_event.set()
                elif key == "PAGE_UP":
                    self.foreground.scroll(1)
                elif key == "PAGE_DOWN":
                    self.foreground.scroll(-1)
                elif key == "ENTER":
                    if self.buffer.strip() == "/stop":
                        self._cancel("/stop", signal.SIGTERM)
                    else:
                        self._send_line()
        finally:
            try:
                self.reader.__exit__(None, None, None)
            except Exception:
                pass

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1.0)


__all__ = [
    "FOREGROUND_PROMPT_PREFIXES", "ForegroundInputForwarder", "Job",
    "display_escape", "foreground_prompt_prefix", "is_foreground_prompt",
    "job_for_issue", "normalize_foreground_output", "pid_running",
    "signal_process_tree", "tail", "tail_from_text",
]
