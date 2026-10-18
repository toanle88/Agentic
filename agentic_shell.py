#!/usr/bin/env python3
"""Interactive background-job supervisor for Agentic.

This module uses only the Python standard library. Agentic remains the source
of truth for worktrees, tests, reviews, and durable run logs; this shell only
supervises child processes and stores a terminal-output copy for inspection.
"""
from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


HELP = """Commands:
  /run ISSUE [--review|--publish|--new]  start an Agentic job in the background
  /ps                                    show jobs, PIDs, status, and elapsed time
  /logs JOB [LINES]                      show the latest captured terminal output
  /stop JOB                              stop a running job
  /dashboard [OUTPUT]                    generate the HTML run dashboard
  /help                                  show this help
  /quit                                  leave the shell; running jobs continue

Jobs have no supervisor timeout. The configured Agentic test/Codex timeouts,
if any, still apply inside the child runner.
"""


@dataclass
class Job:
    number: int
    argv: list[str]
    log_path: Path
    process: subprocess.Popen[bytes]
    started: float
    log_handle: object
    finished: float | None = None
    exit_code: int | None = None
    notified: bool = False

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    @property
    def elapsed(self) -> int:
        end = self.finished or time.monotonic()
        return max(0, int(end - self.started))


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


def _parse_job_command(parts: list[str]) -> tuple[str, list[str]]:
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError("usage: /run ISSUE [--review|--publish|--new]")
    flags = parts[2:]
    allowed = {"--review", "--publish", "--new"}
    unknown = [flag for flag in flags if flag not in allowed]
    if unknown:
        raise ValueError(f"unknown /run option: {unknown[0]}")
    if "--review" in flags and "--publish" in flags:
        raise ValueError("/run cannot combine --review and --publish")
    return parts[1], flags


class AgenticShell:
    def __init__(self, repo: Path, runner: Path, config: Path | None = None,
                 session_dir: Path | None = None) -> None:
        self.repo = repo.resolve()
        self.runner = runner.resolve()
        self.config = config.resolve() if config else None
        self.session_dir = (session_dir or self.repo.parent / f".{self.repo.name}-agentic-shell").resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[int, Job] = {}
        self.next_number = 1

    def _launch(self, argv: list[str]) -> Job:
        number = self.next_number
        self.next_number += 1
        log_path = self.session_dir / f"job-{number:03d}.log"
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
        job = Job(number, argv, log_path, process, time.monotonic(), log_handle)
        self.jobs[number] = job
        print(f"[JOB {number}] started (PID {process.pid})")
        print(f"[JOB {number}] log: {log_path}")
        return job

    def _refresh(self, job: Job) -> None:
        if job.finished is not None:
            return
        code = job.process.poll()
        if code is None:
            return
        job.finished = time.monotonic()
        job.exit_code = code
        try:
            job.log_handle.close()
        except OSError:
            pass
        if not job.notified:
            job.notified = True
            state = "finished" if code == 0 else f"failed (exit {code})"
            print(f"\n[JOB {job.number}] {state} after {job.elapsed}s; use /logs {job.number}")

    def refresh(self) -> None:
        for job in self.jobs.values():
            self._refresh(job)

    def _runner_args(self, issue: str, flags: list[str]) -> list[str]:
        argv = [sys.executable, "-u", str(self.runner), "--repo", str(self.repo), "--issue", issue]
        if self.config:
            argv.extend(["--config", str(self.config)])
        argv.extend(flags)
        return argv

    def run_issue(self, parts: list[str]) -> None:
        issue, flags = _parse_job_command(parts)
        self._launch(self._runner_args(issue, flags))

    def run_dashboard(self, parts: list[str]) -> None:
        dashboard = self.runner.with_name("agentic_dashboard.py")
        if not dashboard.is_file():
            print(f"Dashboard script not found: {dashboard}")
            return
        argv = [sys.executable, "-u", str(dashboard), "--repo", str(self.repo)]
        if len(parts) > 2:
            raise ValueError("usage: /dashboard [OUTPUT]")
        if len(parts) == 2:
            argv.extend(["--output", str(Path(parts[1]).expanduser().resolve())])
        self._launch(argv)

    def show_processes(self) -> None:
        self.refresh()
        if not self.jobs:
            print("No jobs in this shell session.")
            return
        print("ID  STATUS      PID     AGE  COMMAND")
        for job in self.jobs.values():
            status = "RUNNING" if job.running else f"EXIT {job.exit_code}"
            print(f"{job.number:<3} {status:<10} {job.process.pid:<7} {job.elapsed:>3}s  {_display_command(job.argv)}")

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
                os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            job.process.terminate()
        print(f"[JOB {job.number}] stop requested")

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
                self.show_processes()
            elif command == "/logs":
                self.show_logs(parts)
            elif command == "/stop":
                self.stop(parts)
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
        print("Agentic interactive shell")
        print(f"Repository: {self.repo}")
        print("Background jobs have no supervisor timeout. Use /help for commands.")
        while True:
            self.refresh()
            try:
                line = input("agentic> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not self.execute(line):
                break
        running = [job.number for job in self.jobs.values() if job.running]
        if running:
            print(f"Leaving shell; jobs still running: {', '.join(map(str, running))}")
            print("Their logs remain in the session directory.")
        return 0


def run_shell(repo: Path, runner: Path, config: Path | None = None,
              session_dir: Path | None = None) -> int:
    return AgenticShell(repo, runner, config, session_dir).run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Target repository root")
    parser.add_argument("--runner", type=Path, default=Path(__file__).with_name("agentic.py"), help="Agentic runner script")
    parser.add_argument("--config", type=Path, help="Configuration file passed to child runs")
    parser.add_argument("--session-dir", type=Path, help="Directory for interactive-shell logs")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return run_shell(args.repo, args.runner, args.config, args.session_dir)


if __name__ == "__main__":
    raise SystemExit(main())
