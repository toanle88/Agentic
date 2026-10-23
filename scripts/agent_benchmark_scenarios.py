"""Disposable repository and scenario execution for Agentic benchmarks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

try:
    from scripts.agent_benchmark_config import _path_under_root, render_command
    from scripts.benchmark_execution import (
        run_command as _run_shared_command,
    )
except ModuleNotFoundError:  # Direct ``python scripts/agent_benchmark.py`` use.
    from agent_benchmark_config import _path_under_root, render_command
    from benchmark_execution import (
        run_command as _run_shared_command,
    )


SUCCESS_STATUSES = {"success", "completed", "passed", "approved"}
REPORTED_STATUSES = SUCCESS_STATUSES | {"failed", "timeout", "runner-error"}
MAX_ERROR = 500


def _run_benchmark_command(
    command: list[str], *, cwd: Path, env: dict[str, str] | None = None,
    timeout: int,
) -> tuple[int, bool]:
    """Run one argv command and return (exit code, timed out)."""
    result = _run_shared_command(command, cwd=cwd, env=env, timeout=timeout)
    return result.returncode, result.timed_out


def _run_git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stdout[-MAX_ERROR:]}")


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


def _run_checks(repo: Path, checks: list[list[str]], timeout: int) -> tuple[bool, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    all_passed = True
    for command in checks:
        started = time.monotonic()
        try:
            returncode, timed_out = _run_benchmark_command(
                command, cwd=repo, timeout=timeout,
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
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "runner-error", "error": "scenario did not write result_file"}
    except (OSError, json.JSONDecodeError) as exc:
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
    environment = os.environ.copy()
    environment.update({
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
    checks_passed, checks = _run_checks(repo, task["checks"], timeout)
    keep_path = run_root if keep_workspace else None
    record = _scenario_record(
        task, runner, raw, command_exit, time.monotonic() - started,
        checks_passed, checks, keep_path, repetition,
    )
    if not keep_workspace:
        shutil.rmtree(run_root, ignore_errors=True)
    return record
