"""Shared bounded process execution for the benchmark harnesses."""
from __future__ import annotations

from dataclasses import dataclass
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BenchmarkCommandResult:
    """Bounded result from one benchmark command."""

    returncode: int
    timed_out: bool
    output: str = ""


def process_group_kwargs() -> dict[str, Any]:
    """Start a benchmark command in a group that can be stopped together."""
    if os.name != "nt":
        return {"start_new_session": True}
    return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}


def terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """Terminate a timed-out benchmark command and all of its descendants."""
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    else:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
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
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _bounded_tail(stream: Any, limit: int) -> str:
    stream.flush()
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(max(0, size - max(1, limit * 4)))
    return stream.read().decode("utf-8", errors="replace")[-limit:]


def run_command(
    command: list[str], *, cwd: Path, timeout: int,
    env: dict[str, str] | None = None, capture_output: bool = False,
    output_limit: int = 6_000,
) -> BenchmarkCommandResult:
    """Run an argv command with bounded output and complete timeout cleanup."""
    output_file = tempfile.TemporaryFile(mode="w+b") if capture_output else None
    try:
        process = subprocess.Popen(
            command, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL,
            stdout=output_file if output_file is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            **process_group_kwargs(),
        )
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_tree(process)
        output = _bounded_tail(output_file, output_limit) if output_file is not None else ""
        return BenchmarkCommandResult(
            124 if timed_out else process.returncode, timed_out, output,
        )
    finally:
        if output_file is not None:
            output_file.close()
