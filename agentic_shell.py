#!/usr/bin/env python3
"""Interactive background-job supervisor for Agentic.

This module uses only the Python standard library. Agentic remains the source
of truth for worktrees, tests, reviews, and durable run logs; this shell only
supervises child processes and stores a terminal-output copy for inspection.
"""
from __future__ import annotations

import argparse
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


HELP = """Commands:
  /run ISSUE [--profile fast|slow]       start a background job
       [--review|--publish|--new]
  /run [--profile fast|slow]             local mode: paste a task, finish with /done
       [--review|--publish|--new]
  /ps [--all]                            show current or persisted jobs
  /logs JOB [LINES]                      show the latest captured terminal output
  /stop JOB                              stop a running job
  /status [--json]                       show Agentic worktree status
  /dashboard [OUTPUT]                    generate and open the HTML dashboard
  /help                                  show this help
  /quit                                  leave the shell; running jobs continue

Jobs have no supervisor timeout. The configured Agentic test/Codex timeouts,
if any, still apply inside the child runner.
"""


def _shell_paint(text: str, code: str) -> str:
    """Add terminal color without leaking escape codes into captured output."""
    if (os.environ.get("NO_COLOR") or os.environ.get("AGENTIC_PLAIN")
            or not getattr(sys.stdout, "isatty", lambda: False)()):
        return text
    return f"\033[{code}m{text}\033[0m"


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
        print(_shell_paint("AGENTIC", "1;36"))
        print(f"Interactive coding workflow - {_fit_terminal_text(repo.name, max(columns - 34, 8))}")
        print(f"{active_jobs} running job(s); use /help for commands.")
        if previous_jobs:
            print(f"{previous_jobs} previous job(s); use /ps --all to inspect.")
        print("Use /help for commands. Jobs continue after /quit.")
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
    print(_shell_paint(row(f"{marker}  A G E N T I C"), "1;36"))
    print(_shell_paint(row("   Interactive issue-driven coding workflow"), "36"))
    print(_shell_paint(divider + horizontal * inner + ("┤" if unicode_banner else "+"), "36"))
    print(row(f"Repository  {repo}"))
    print(row(f"State       {state_text}"))
    print(row(f"Config      {config_text}"))
    print(row(f"Jobs        {job_text}"))
    print(_shell_paint(row("Commands    /run ISSUE   /ps   /logs JOB   /help   /quit"), "2"))
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
        raise ValueError("usage: /run ISSUE [--profile fast|slow] [--review|--publish|--new]")
    allowed = {"--review", "--publish", "--new", "--profile", "fast", "slow"}
    unknown = [flag for flag in flags if flag not in allowed]
    if unknown:
        raise ValueError(f"unknown /run option: {unknown[0]}")
    if "--profile" not in flags and any(flag in {"fast", "slow"} for flag in flags):
        raise ValueError("usage: /run ISSUE [--profile fast|slow] [--review|--publish|--new]")
    if "--profile" in flags:
        index = flags.index("--profile")
        if index + 1 >= len(flags) or flags[index + 1] not in {"fast", "slow"}:
            raise ValueError("usage: /run ISSUE [--profile fast|slow] [--review|--publish|--new]")
        if any(flag in {"fast", "slow"} for flag in flags[:index]):
            raise ValueError("usage: /run ISSUE [--profile fast|slow] [--review|--publish|--new]")
        if any(
            flag in {"fast", "slow"}
            for flag in flags[index + 2:]
            if flag not in {"--review", "--publish", "--new"}
        ):
            raise ValueError("usage: /run ISSUE [--profile fast|slow] [--review|--publish|--new]")
    if flags.count("--profile") > 1:
        raise ValueError("/run accepts --profile only once")
    if "--review" in flags and "--publish" in flags:
        raise ValueError("/run cannot combine --review and --publish")
    return issue, flags


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
        if issue is None:
            if self._task_source() != "local":
                raise ValueError("GitHub mode requires /run ISSUE; set task_source to local to paste tasks")
            task_file = self._capture_local_task()
        argv = self._runner_args(issue, flags)
        if task_file:
            argv.extend(["--task-file", str(task_file)])
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
        for job in jobs:
            status = "RUNNING" if job.running else f"EXIT {job.exit_code if job.exit_code not in (None, -1) else '?'}"
            session = "current" if job.session_id == self.session_id else job.session_id[:8]
            print(f"{job.number:<3} {status:<10} {job.pid:<7} {job.elapsed:>3}s  {session:<8} {_display_command(job.argv)}")

    def show_logs(self, parts: list[str]) -> None:
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError("usage: /logs JOB [LINES]")
        job = self.jobs.get(int(parts[1]))
        if job is None:
            raise ValueError(f"unknown job: {parts[1]}")
        lines = 40
        if len(parts) > 3 or (len(parts) == 3 and (not parts[2].isdigit() or int(parts[2]) < 1)):
            raise ValueError("usage: /logs JOB [LINES]")
        if len(parts) == 3:
            lines = min(int(parts[2]), 500)
        print(f"--- job {job.number}: {_display_command(job.argv)} ---")
        print(_tail(job.log_path, lines))
        print(f"--- end; full log: {job.log_path} ---")

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

    def execute(self, line: str) -> bool:
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
                print(HELP)
            elif command == "/run":
                self.run_issue(parts)
            elif command == "/ps":
                self.show_processes(parts)
            elif command == "/logs":
                self.show_logs(parts)
            elif command == "/stop":
                self.stop(parts)
            elif command == "/status":
                self.show_status(parts)
            elif command == "/dashboard":
                self.run_dashboard(parts)
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
        print("Available commands:")
        print(HELP.rstrip())
        while True:
            self.refresh()
            try:
                line = input("agentic> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not self.execute(line):
                break
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
