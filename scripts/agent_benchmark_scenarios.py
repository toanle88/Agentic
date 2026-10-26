"""Disposable repository and scenario execution for Agentic benchmarks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Thread
import time
from typing import Any

try:
    from scripts.agent_benchmark_config import _path_under_root, render_command
    from scripts.benchmark_execution import (
        process_group_kwargs, run_command as _run_shared_command,
        terminate_process_tree,
    )
except ModuleNotFoundError:  # Direct ``python scripts/agent_benchmark.py`` use.
    from agent_benchmark_config import _path_under_root, render_command
    from benchmark_execution import (
        process_group_kwargs, run_command as _run_shared_command,
        terminate_process_tree,
    )

try:
    from agentic_runner.file_boundaries import open_single_link_regular
except ModuleNotFoundError:  # Direct source-checkout use without installation.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from agentic_runner.file_boundaries import open_single_link_regular


SUCCESS_STATUSES = {"success", "completed", "passed", "approved"}
REPORTED_STATUSES = SUCCESS_STATUSES | {"failed", "timeout", "runner-error"}
MAX_ERROR = 500
MAX_RESULT_BYTES = 256_000
MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024
GIT_COMMAND_TIMEOUT_SECONDS = 10
_BENCHMARK_BASE_ENV = frozenset({
    "PATH", "HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR", "SYSTEMROOT",
    "PATHEXT", "LANG", "LC_ALL", "LC_CTYPE",
})


def _benchmark_environment(values: dict[str, str]) -> dict[str, str]:
    """Build a minimal child environment with explicit credential opt-in."""
    names = {
        item.strip() for item in os.environ.get(
            "AGENTIC_BENCHMARK_PASSTHROUGH_ENV", ""
        ).split(",") if item.strip()
    }
    environment = {
        name: value for name, value in os.environ.items()
        if name in _BENCHMARK_BASE_ENV or name in names
    }
    environment.update(values)
    return environment


def _run_benchmark_command(
    command: list[str], *, cwd: Path, env: dict[str, str] | None = None,
    timeout: int,
) -> tuple[int, bool]:
    """Run one argv command and return (exit code, timed out)."""
    result = _run_shared_command(command, cwd=cwd, env=env, timeout=timeout)
    return result.returncode, result.timed_out


def _run_bounded_git_command(
    repo: Path, environment: dict[str, str], args: list[str],
) -> tuple[int, str]:
    """Run Git without materializing output beyond the safety limit."""
    process = subprocess.Popen(
        ["git", *args],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=environment, **process_group_kwargs(),
    )
    chunks: list[bytes] = []
    overflow = False
    read_error: BaseException | None = None
    total = 0

    def read_output() -> None:
        nonlocal overflow, read_error, total
        stream = process.stdout
        if stream is None:
            return
        try:
            while True:
                read = getattr(stream, "read1", stream.read)
                chunk = read(64 * 1024)
                if not chunk:
                    return
                total += len(chunk)
                if total > MAX_GIT_OUTPUT_BYTES:
                    overflow = True
                    try:
                        terminate_process_tree(process)
                    except OSError:
                        pass
                    return
                chunks.append(chunk)
        except BaseException as exc:  # pragma: no cover - OS-specific pipe failures
            read_error = exc

    reader = Thread(target=read_output, daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=GIT_COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        try:
            terminate_process_tree(process)
        except OSError:
            pass
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
        raise RuntimeError("Timed out running benchmark Git command") from exc
    finally:
        reader.join(timeout=1)
        if process.stdout is not None:
            process.stdout.close()
    if overflow:
        raise RuntimeError("Git command output exceeded its safety limit")
    if read_error is not None:
        raise RuntimeError("Could not read benchmark Git output") from read_error
    return returncode, b"".join(chunks).decode("utf-8", errors="replace")


def _run_git(repo: Path, *args: str) -> None:
    environment = _benchmark_environment({})
    for name in list(environment):
        if name.startswith("GIT_CONFIG_"):
            environment.pop(name, None)
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    if (repo / ".git").exists():
        filter_returncode, filter_output = _run_bounded_git_command(
            repo, environment,
            ["config", "--local", "--name-only", "--get-regexp",
             r"^filter\..+\.(clean|smudge|process|required)$"],
        )
        if filter_returncode not in (0, 1) or filter_output.strip():
            raise RuntimeError("benchmark fixture uses unsupported Git filter drivers")
    returncode, output = _run_bounded_git_command(
        repo, environment, list(args),
    )
    if returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {output[-MAX_ERROR:]}")


def prepare_repository(source: Path, destination: Path) -> None:
    """Copy a fixture into an isolated repository and initialize Git if needed."""
    if not source.is_dir():
        raise ValueError(f"repository fixture does not exist: {source}")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError(f"repository fixture contains unsupported symlinks: {source}")
    shutil.copytree(source, destination, symlinks=True)
    if not (destination / ".git").exists():
        _run_git(destination, "init", "--initial-branch", "main")
        _run_git(destination, "add", "-A")
        _run_git(
            destination, "-c", "user.name=Agentic Benchmark",
            "-c", "user.email=agentic-benchmark@example.invalid",
            "commit", "-m", "benchmark fixture",
        )


def _run_checks(
    repo: Path, checks: list[list[str]], timeout: int,
    *, env: dict[str, str] | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    if env is None:
        env = _benchmark_environment({})
    records: list[dict[str, Any]] = []
    all_passed = True
    for command in checks:
        started = time.monotonic()
        try:
            returncode, timed_out = _run_benchmark_command(
                command, cwd=repo, env=env, timeout=timeout,
            )
            status = "timeout" if timed_out else "passed" if returncode == 0 else "failed"
            records.append({
                "status": status,
                "returncode": returncode,
                "duration_seconds": round(time.monotonic() - started, 3),
            })
            all_passed = all_passed and returncode == 0 and not timed_out
        except subprocess.TimeoutExpired:
            records.append({
                "status": "timeout",
                "returncode": 124,
                "duration_seconds": round(time.monotonic() - started, 3),
            })
            all_passed = False
        except OSError:
            records.append({
                "status": "unavailable",
                "returncode": 127,
                "duration_seconds": round(time.monotonic() - started, 3),
            })
            all_passed = False
    return all_passed, records


def _read_result(path: Path) -> dict[str, Any]:
    descriptor: int | None = None
    try:
        descriptor, _ = open_single_link_regular(
            path,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
            description="scenario result",
        )
        payload = os.read(descriptor, MAX_RESULT_BYTES + 1)
    except FileNotFoundError:
        return {"status": "runner-error", "error": "scenario did not write result_file"}
    except (OSError, ValueError) as exc:
        return {"status": "runner-error", "error": f"invalid scenario result: {exc}"}
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > MAX_RESULT_BYTES:
        return {
            "status": "runner-error",
            "error": f"scenario result exceeds {MAX_RESULT_BYTES}-byte limit",
        }
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"status": "runner-error", "error": f"invalid scenario result: {exc}"}
    if not isinstance(value, dict):
        return {"status": "runner-error", "error": "scenario result must be a JSON object"}
    return value


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value) if integer else value


def _finding_ids(result: dict[str, Any]) -> list[str]:
    review = result.get("review")
    values: Any = review.get("finding_ids") if isinstance(review, dict) else result.get("finding_ids")
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(item for item in values if isinstance(item, str)))


def _scenario_record(
    task: dict[str, Any], runner: str, raw: dict[str, Any], command_exit: int,
    duration: float, checks_passed: bool, checks: list[dict[str, Any]],
    workspace: Path | None, repetition: int,
) -> dict[str, Any]:
    status = raw.get("status")
    reported_status = status if isinstance(status, str) else "invalid"
    runner_ok = command_exit == 0 and reported_status in SUCCESS_STATUSES
    completed = raw.get("completed") if isinstance(raw.get("completed"), bool) else runner_ok
    if completed is False:
        runner_ok = False
    review = raw.get("review") if isinstance(raw.get("review"), dict) else {}
    approved = raw.get("approved")
    if not isinstance(approved, bool):
        approved = review.get("approved") if isinstance(review.get("approved"), bool) else None
    attempts = _number(raw.get("attempts", 1), integer=True) or 1
    interventions = _number(raw.get("interventions", 0), integer=True) or 0
    recovered = raw.get("recovered") if isinstance(raw.get("recovered"), bool) else None
    findings = _finding_ids(raw)
    defect_ids = {item["id"] for item in task["defects"]}
    caught = sorted(defect_ids.intersection(findings))
    false_positives = sorted(set(findings).difference(defect_ids)) if findings else []
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    record: dict[str, Any] = {
        "task_id": task["id"], "task_name": task["name"], "runner": runner,
        "repetition": repetition,
        "status": "passed" if runner_ok and checks_passed else "failed",
        "runner_status": reported_status if reported_status in REPORTED_STATUSES else "invalid",
        "runner_returncode": command_exit, "completed": completed,
        "checks_passed": checks_passed, "checks": checks, "approved": approved,
        "first_pass_approved": None if approved is None else approved is True and attempts == 1,
        "attempts": attempts, "interventions": interventions, "recovered": recovered,
        "duration_seconds": round(duration, 3), "defects_total": len(defect_ids),
        "defects_caught": len(caught), "finding_total": len(findings),
        "false_positives": len(false_positives),
        "usage": {
            "total_tokens": _number(usage.get("total_tokens"), integer=True),
            "estimated_cost_usd": _number(usage.get("estimated_cost_usd")),
        },
    }
    if workspace is not None:
        record["workspace"] = str(workspace)
    if raw.get("error"):
        record["error"] = "scenario reported an error"
    return record


def run_scenario(
    root: Path, task: dict[str, Any], runner: str, command: list[str],
    workspace_root: Path, timeout: int, keep_workspace: bool = False,
    source_repository: str | None = None, repetition: int = 1,
) -> dict[str, Any]:
    """Run one adapter in a disposable repository and normalize its result."""
    scenario_directory = runner.replace(":", "-")
    run_root = workspace_root / scenario_directory / task["id"] / f"repetition-{repetition:03d}"
    run_root.mkdir(parents=True, exist_ok=True)
    repo = run_root / "repo"
    source = _path_under_root(
        root, source_repository or task["repository"], f"task {task['id']} source repository"
    )
    task_source = _path_under_root(root, task["task_file"], f"task {task['id']} task file")
    prepare_repository(source, repo)
    task_file = run_root / Path(task["task_file"]).name
    shutil.copy2(task_source, task_file)
    result_file = run_root / "result.json"
    state_dir = run_root / "state"
    state_dir.mkdir()
    values = {
        "benchmark_root": str(root), "repo": str(repo),
        "reviewer": runner.removeprefix("reviewer:"), "task_file": str(task_file),
        "result_file": str(result_file), "scenario": runner, "state_dir": str(state_dir),
        "task_id": task["id"], "repetition": str(repetition),
    }
    rendered = render_command(command, values)
    environment = _benchmark_environment({
        "AGENTIC_BENCHMARK_ROOT": str(root),
        "AGENTIC_BENCHMARK_REVIEWER": runner.removeprefix("reviewer:"),
        "AGENTIC_BENCHMARK_SCENARIO": runner, "AGENTIC_BENCHMARK_TASK_ID": task["id"],
        "AGENTIC_BENCHMARK_REPO": str(repo), "AGENTIC_BENCHMARK_TASK_FILE": str(task_file),
        "AGENTIC_BENCHMARK_RESULT_FILE": str(result_file),
        "AGENTIC_BENCHMARK_STATE_DIR": str(state_dir),
        "AGENTIC_BENCHMARK_REPETITION": str(repetition),
    })
    started = time.monotonic()
    command_exit = 127
    raw: dict[str, Any]
    try:
        command_exit, timed_out = _run_benchmark_command(
            rendered, cwd=repo, env=environment, timeout=timeout,
        )
        if timed_out:
            raw = {"status": "timeout", "error": "scenario exceeded its timeout"}
        else:
            raw = _read_result(result_file)
    except OSError as exc:
        raw = {"status": "runner-error", "error": str(exc)}
    checks_passed, checks = _run_checks(
        repo, task["checks"], timeout, env=environment,
    )
    keep_path = run_root if keep_workspace else None
    record = _scenario_record(
        task, runner, raw, command_exit, time.monotonic() - started,
        checks_passed, checks, keep_path, repetition,
    )
    if not keep_workspace:
        shutil.rmtree(run_root, ignore_errors=True)
    return record
