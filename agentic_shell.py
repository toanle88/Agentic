#!/usr/bin/env python3
"""Interactive background-job supervisor for Agentic.

The input/editor and presentation boundary lives in agentic_shell_input;
this facade retains job lifecycle, command dispatch, and public CLI behavior.
"""
from __future__ import annotations

import argparse
from collections.abc import Sequence
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
from typing import Callable, TextIO

from agentic import __version__  # noqa: F401
from agentic_shell_input import (  # noqa: F401
    COMMAND_DESCRIPTIONS,
    HELP,
    HELP_SECTION_COLORS,
    OPTION_DESCRIPTIONS,
    SHELL_COMMANDS,
    SHELL_OPTIONS,
    _KeyReader,
    _apply_completion,
    _clip_input_line,
    _completion_candidates,
    _completion_items,
    _describe_candidate,
    _enable_windows_ansi,
    _fit_terminal_text,
    _help_line,
    _interactive_input,
    _is_interactive_terminal,
    _parse_ansi_sequence,
    _print_help,
    _readline_module,
    _replace_current_token,
    _shell_paint,
    _shell_print,
    _shell_print_raw,
    _shell_write_log,
    _supports_banner_unicode,
    _token_start,
    _welcome_banner,
    _windows_input,
)
from agentic_shell_commands import (
    has_job_flag as _has_job_flag,
    option_value as _option_value,
    parse_job_command as _parse_job_command,
    parse_options as _parse_options,
)

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
                _shell_print(f"[DASHBOARD] Opened {path}")
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
            _shell_print(f"[DASHBOARD] Opened {path}")
            return
        except OSError:
            continue
    _shell_print(f"[DASHBOARD] Ready: {path} (open it manually; no desktop opener found)")


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
        _shell_print(f"[JOB {number}] started (PID {process.pid})")
        _shell_print(f"[JOB {number}] log: {log_path}")
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
            _shell_print(f"[DASHBOARD] Could not open dashboard: {exc}")

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
            _shell_print(f"\n[JOB {job.number}] {state} after {job.elapsed}s; use /logs {job.number}")

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
        # Use argparse's inline form for instruction values. This keeps
        # option-like and empty instruction text in the value itself when the
        # child runner parses the argv array.
        index = 0
        while index < len(flags):
            if flags[index] == "--instruction" and index + 1 < len(flags):
                argv.append(f"--instruction={flags[index + 1]}")
                index += 2
                continue
            argv.append(flags[index])
            index += 1
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
            if "--json" in argv:
                _shell_print_raw(result.stdout.rstrip())
            else:
                _shell_print(result.stdout.rstrip())
        if result.returncode:
            raise RuntimeError(f"command failed with exit {result.returncode}")

    def _subcommand_args(self, command: str, flags: Sequence[str] = ()) -> list[str]:
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
        _shell_print("Paste the local task. Finish with a line containing /done; use /cancel to abort.")
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
        _shell_print(f"Local task saved: {path}")
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
                if task_file_value is not None
                else self._capture_local_task().expanduser().resolve()
            )
        runner_flags = list(flags)
        if task_file is not None and task_file_value is not None:
            task_file_index = runner_flags.index("--task-file")
            runner_flags[task_file_index + 1] = str(task_file)
        argv = self._runner_args(issue, runner_flags)
        if task_file and task_file_value is None:
            argv.extend(["--task-file", str(task_file)])
        if _has_job_flag(flags, "--publish"):
            _shell_print("[PUBLISH] Running in the foreground so publication confirmation can be entered")
            self._run_foreground(argv)
        else:
            self._launch(argv)

    def run_dashboard(self, parts: list[str]) -> None:
        dashboard = self.runner.with_name("agentic_dashboard.py")
        if not dashboard.is_file():
            _shell_print(f"Dashboard script not found: {dashboard}")
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
                _shell_print(f"[DASHBOARD] Generation failed (exit {code}); use /logs for details")

        self._launch(argv, on_complete=open_when_ready)

    def show_processes(self, parts: list[str] | None = None) -> None:
        parts = parts or ["/ps"]
        if len(parts) > 2 or (len(parts) == 2 and parts[1] != "--all"):
            raise ValueError("usage: /ps [--all]")
        show_all = len(parts) == 2
        self.refresh()
        jobs = [job for job in self.jobs.values() if show_all or job.session_id == self.session_id]
        if not jobs:
            _shell_print("No matching jobs. Use /ps --all to include previous shell sessions.")
            return
        _shell_print("ID  STATUS      PID     AGE  SESSION      COMMAND")
        states = [(job, job.running) for job in jobs]
        for job, running in sorted(states, key=lambda item: (not item[1], item[0].pid, item[0].number)):
            status = "RUNNING" if running else f"EXIT {job.exit_code if job.exit_code not in (None, -1) else '?'}"
            session = "current" if job.session_id == self.session_id else job.session_id[:8]
            _shell_print(f"{job.number:<3} {status:<10} {job.pid:<7} {job.elapsed:>3}s  {session:<8} {_display_command(job.argv)}")

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
        _shell_print(
            f"--- job {job.number}: {_display_command(job.argv)} ---",
            style_status_tokens=False,
        )
        if job.running:
            _shell_print("--- following live output; press Ctrl-C to stop following ---")
            self._follow_log(job, lines)
        else:
            _shell_print(_tail(job.log_path, lines), style_status_tokens=False)
        _shell_print(
            f"--- end; full log: {job.log_path} ---",
            style_status_tokens=False,
        )

    def _follow_log(self, job: Job, lines: int) -> None:
        """Print a running job's existing tail and stream appended output."""
        try:
            with job.log_path.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(0, os.SEEK_END)
                initial_end = stream.tell()
                initial_start = max(0, initial_end - 200_000)
                stream.seek(initial_start)
                initial = stream.read(initial_end - initial_start)
                _shell_print(_tail_from_text(initial, lines), style_status_tokens=False)
                # Keep the position at the end of the initial snapshot rather
                # than seeking to the current EOF, so a concurrent write is
                # still included in the first live read.
                stream.seek(initial_end)

                while job.running:
                    self._refresh(job)
                    chunk = stream.read()
                    if chunk:
                        _shell_write_log(chunk)
                    if not job.running:
                        break
                    time.sleep(0.1)

                # The child may have flushed its last bytes just before the
                # process status changed. Drain once after observing exit.
                self._refresh(job)
                chunk = stream.read()
                if chunk:
                    _shell_write_log(chunk)
        except KeyboardInterrupt:
            _shell_print("\n--- live output follow stopped; job continues running ---")
        except OSError as exc:
            _shell_print(f"Could not read {job.log_path}: {exc}")

    def _request_stop(self, job: Job) -> bool:
        """Request termination for one running job and report the result."""
        self._refresh(job)
        if not job.running:
            return False
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
        _shell_print(f"[JOB {job.number}] stop requested")
        return True

    def stop(self, parts: list[str]) -> None:
        if len(parts) != 2 or (parts[1] != "--all" and not parts[1].isdigit()):
            raise ValueError("usage: /stop JOB|--all")
        if parts[1] == "--all":
            requested = sum(self._request_stop(job) for job in self.jobs.values())
            if requested == 0:
                _shell_print("[STOP] no running jobs")
            else:
                _shell_print(f"[STOP] stop requested for {requested} job(s)")
            return
        job = self.jobs.get(int(parts[1]))
        if job is None:
            raise ValueError(f"unknown job: {parts[1]}")
        if not self._request_stop(job):
            _shell_print(f"[JOB {job.number}] already finished with exit {job.exit_code}")

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
        if len(parts) == 2:
            _shell_print_raw(result.stdout.rstrip())
        else:
            _shell_print(result.stdout.rstrip())
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
        _shell_print(result.stdout.rstrip())
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
            parts,
            usage="/doctor [--json] [--commands] [--profile fast|slow] [--worktree PATH]",
            value_options={"--profile", "--worktree"},
            boolean_options={"--json", "--commands"},
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
            _shell_print(f"Input error: {exc}")
            return True
        if not parts:
            return True
        if not parts[0].startswith("/"):
            _shell_print("Commands must start with '/'. Use /help for available commands.")
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
                _shell_print(f"Unknown command: {command}. Use /help.")
        except (OSError, ValueError, RuntimeError) as exc:
            _shell_print(f"Error: {exc}")
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
            _shell_print(f"Leaving shell; jobs still running: {', '.join(map(str, running))}")
            _shell_print("Their logs and job records remain in the session directory; use /ps --all later.")
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
