#!/usr/bin/env python3
"""Run an opt-in, argv-only benchmark for real coding-agent scenarios.

The benchmark deliberately knows nothing about provider CLIs. Scenario adapters
receive a disposable repository and task file, run the selected workflow, and
write a small normalized result JSON file. This keeps provider credentials and
full prompts out of the aggregate benchmark artifact.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Iterable


SCHEMA_VERSION = 1
RUNNERS = ("agent-alone", "agentic")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PLACEHOLDER_NAMES = {
    "benchmark_root", "repo", "reviewer", "result_file", "scenario", "state_dir",
    "task_file", "task_id", "repetition",
}
SUCCESS_STATUSES = {"success", "completed", "passed", "approved"}
REPORTED_STATUSES = SUCCESS_STATUSES | {"failed", "timeout", "runner-error"}
MAX_ERROR = 500


def _argv(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(part, str) and part for part in value)
    ):
        raise ValueError(f"{label} must be a nonempty argv array")
    return list(value)


def _relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must stay inside the benchmark directory")
    return value


def _defects(value: Any, label: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value, 1):
        if isinstance(item, str):
            defect_id, description = item, ""
        elif isinstance(item, dict):
            defect_id = item.get("id")
            description = item.get("description", "")
        else:
            raise ValueError(f"{label}[{index}] must be a string or object")
        if not isinstance(defect_id, str) or not TASK_ID.fullmatch(defect_id):
            raise ValueError(f"{label}[{index}].id must be a short identifier")
        if not isinstance(description, str):
            raise ValueError(f"{label}[{index}].description must be a string")
        if defect_id in seen:
            raise ValueError(f"{label} contains duplicate id {defect_id!r}")
        seen.add(defect_id)
        result.append({"id": defect_id, "description": description})
    return result


def _validate_placeholders(command: list[str], label: str) -> None:
    for part in command:
        for name in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", part):
            if name not in PLACEHOLDER_NAMES:
                raise ValueError(f"{label} uses unsupported placeholder {{{name}}}")


def load_benchmark(path: Path) -> dict[str, Any]:
    """Load and validate a benchmark corpus plus its scenario adapters."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read benchmark: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"benchmark is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("benchmark must be a JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"benchmark.schema_version must be {SCHEMA_VERSION}")

    tasks_raw = raw.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise ValueError("benchmark.tasks must be a nonempty list")
    tasks: list[dict[str, Any]] = []
    seen_tasks: set[str] = set()
    for index, item in enumerate(tasks_raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"tasks[{index}] must be an object")
        task_id = item.get("id")
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ValueError(f"tasks[{index}].id must be a short identifier")
        if task_id in seen_tasks:
            raise ValueError(f"duplicate task id {task_id!r}")
        seen_tasks.add(task_id)
        checks_raw = item.get("checks")
        if not isinstance(checks_raw, list) or not checks_raw:
            raise ValueError(f"tasks[{index}].checks must be a nonempty list")
        checks = [_argv(command, f"tasks[{index}].checks[{check_index}]")
                  for check_index, command in enumerate(checks_raw, 1)]
        tasks.append({
            "id": task_id,
            "name": item.get("name", task_id),
            "repository": _relative_path(item.get("repository"), f"tasks[{index}].repository"),
            "task_file": _relative_path(item.get("task_file"), f"tasks[{index}].task_file"),
            "review_repository": (
                _relative_path(item.get("review_repository"), f"tasks[{index}].review_repository")
                if item.get("review_repository") is not None else None
            ),
            "checks": checks,
            "defects": _defects(item.get("defects"), f"tasks[{index}].defects"),
        })
        if not isinstance(tasks[-1]["name"], str) or not tasks[-1]["name"].strip():
            raise ValueError(f"tasks[{index}].name must be a nonempty string")

    runners_raw = raw.get("runners")
    if not isinstance(runners_raw, dict):
        raise ValueError("benchmark.runners must be an object")
    runners: dict[str, list[str]] = {}
    for runner in RUNNERS:
        command = _argv(runners_raw.get(runner), f"runners.{runner}")
        _validate_placeholders(command, f"runners.{runner}")
        runners[runner] = command

    reviewers_raw = raw.get("reviewers", {})
    if not isinstance(reviewers_raw, dict):
        raise ValueError("benchmark.reviewers must be an object")
    if reviewers_raw and len(reviewers_raw) < 2:
        raise ValueError("benchmark.reviewers must contain at least two providers")
    reviewers: dict[str, list[str]] = {}
    for reviewer, value in reviewers_raw.items():
        if not isinstance(reviewer, str) or not TASK_ID.fullmatch(reviewer):
            raise ValueError("reviewer names must be short identifiers")
        command = _argv(value, f"reviewers.{reviewer}")
        _validate_placeholders(command, f"reviewers.{reviewer}")
        reviewers[reviewer] = command
    repetitions = raw.get("repetitions", 1)
    if type(repetitions) is not int or not 1 <= repetitions <= 20:
        raise ValueError("benchmark.repetitions must be an integer from 1 to 20")
    return {
        "path": path.resolve(), "tasks": tasks, "runners": runners,
        "reviewers": reviewers, "repetitions": repetitions,
    }


def validate_benchmark_files(config: dict[str, Any]) -> None:
    """Verify that fixture and task files exist without executing them."""
    root = Path(config["path"]).parent.resolve()
    for task in config["tasks"]:
        repository = _path_under_root(root, task["repository"], f"task {task['id']} repository")
        task_file = _path_under_root(root, task["task_file"], f"task {task['id']} task file")
        if not repository.is_dir():
            raise ValueError(f"task {task['id']} repository fixture does not exist: {repository}")
        if not task_file.is_file():
            raise ValueError(f"task {task['id']} task file does not exist: {task_file}")
        if config["reviewers"]:
            review_repository_value = task.get("review_repository")
            if not review_repository_value:
                raise ValueError(
                    f"task {task['id']} requires review_repository when reviewers are configured"
                )
            review_repository = _path_under_root(
                root, review_repository_value, f"task {task['id']} review repository"
            )
            if not review_repository.is_dir():
                raise ValueError(
                    f"task {task['id']} review repository fixture does not exist: {review_repository}"
                )


def _path_under_root(root: Path, relative: str, label: str) -> Path:
    root = root.resolve()
    raw = root / relative
    current = root
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} must not use symlinks")
    candidate = raw.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} must resolve inside the benchmark directory") from exc
    return candidate


def render_command(command: Iterable[str], values: dict[str, str]) -> list[str]:
    """Substitute path placeholders without invoking a shell."""
    rendered: list[str] = []
    for part in command:
        value = part
        for name in PLACEHOLDER_NAMES:
            if name in values:
                value = value.replace("{" + name + "}", values[name])
        rendered.append(value)
    return rendered


def _run_git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stdout[-MAX_ERROR:]}")


def prepare_repository(source: Path, destination: Path) -> None:
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
            result = subprocess.run(
                command, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
            )
            status = "passed" if result.returncode == 0 else "failed"
            records.append({
                "status": status,
                "returncode": result.returncode,
                "duration_seconds": round(time.monotonic() - started, 3),
            })
            all_passed = all_passed and result.returncode == 0
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
        "task_id": task["id"],
        "task_name": task["name"],
        "runner": runner,
        "repetition": repetition,
        "status": "passed" if runner_ok and checks_passed else "failed",
        "runner_status": reported_status if reported_status in REPORTED_STATUSES else "invalid",
        "runner_returncode": command_exit,
        "completed": completed,
        "checks_passed": checks_passed,
        "checks": checks,
        "approved": approved,
        "first_pass_approved": None if approved is None else approved is True and attempts == 1,
        "attempts": attempts,
        "interventions": interventions,
        "recovered": recovered,
        "duration_seconds": round(duration, 3),
        "defects_total": len(defect_ids),
        "defects_caught": len(caught),
        "finding_total": len(findings),
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
        "benchmark_root": str(root),
        "repo": str(repo),
        "reviewer": runner.removeprefix("reviewer:"),
        "task_file": str(task_file),
        "result_file": str(result_file),
        "scenario": runner,
        "state_dir": str(state_dir),
        "task_id": task["id"],
        "repetition": str(repetition),
    }
    rendered = render_command(command, values)
    environment = os.environ.copy()
    environment.update({
        "AGENTIC_BENCHMARK_ROOT": str(root),
        "AGENTIC_BENCHMARK_REVIEWER": runner.removeprefix("reviewer:"),
        "AGENTIC_BENCHMARK_SCENARIO": runner,
        "AGENTIC_BENCHMARK_TASK_ID": task["id"],
        "AGENTIC_BENCHMARK_REPO": str(repo),
        "AGENTIC_BENCHMARK_TASK_FILE": str(task_file),
        "AGENTIC_BENCHMARK_RESULT_FILE": str(result_file),
        "AGENTIC_BENCHMARK_STATE_DIR": str(state_dir),
        "AGENTIC_BENCHMARK_REPETITION": str(repetition),
    })
    started = time.monotonic()
    command_exit = 127
    raw: dict[str, Any]
    try:
        completed = subprocess.run(
            rendered, cwd=repo, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
        command_exit = completed.returncode
        raw = _read_result(result_file)
    except subprocess.TimeoutExpired:
        raw = {"status": "timeout", "error": "scenario exceeded its timeout"}
        command_exit = 124
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


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def aggregate_metrics(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(records)
    total = len(items)
    completed = sum(item.get("completed") is True for item in items)
    checks_passed = sum(item.get("checks_passed") is True for item in items)
    approval_items = [item for item in items if isinstance(item.get("approved"), bool)]
    first_pass_items = [item for item in items if isinstance(item.get("first_pass_approved"), bool)]
    approved = sum(item.get("approved") is True for item in approval_items)
    first_pass = sum(item.get("first_pass_approved") is True for item in first_pass_items)
    defects_total = sum(int(item.get("defects_total", 0)) for item in items)
    defects_caught = sum(int(item.get("defects_caught", 0)) for item in items)
    finding_total = sum(int(item.get("finding_total", 0)) for item in items)
    false_positives = sum(int(item.get("false_positives", 0)) for item in items)
    known_recovery = [item for item in items if isinstance(item.get("recovered"), bool)]
    token_values = [item["usage"]["total_tokens"] for item in items if item.get("usage", {}).get("total_tokens") is not None]
    cost_values = [item["usage"]["estimated_cost_usd"] for item in items if item.get("usage", {}).get("estimated_cost_usd") is not None]
    return {
        "runs": total,
        "completed": completed,
        "completion_rate": _rate(completed, total),
        "checks_passed": checks_passed,
        "checks_pass_rate": _rate(checks_passed, total),
        "approved": approved,
        "approval_rate": _rate(approved, len(approval_items)),
        "first_pass_approved": first_pass,
        "first_pass_approval_rate": _rate(first_pass, len(first_pass_items)),
        "defect_recall": _rate(defects_caught, defects_total),
        "false_positive_rate": _rate(false_positives, finding_total),
        "average_duration_seconds": round(sum(float(item["duration_seconds"]) for item in items) / total, 3) if total else None,
        "average_attempts": round(sum(int(item["attempts"]) for item in items) / total, 3) if total else None,
        "interventions": sum(int(item["interventions"]) for item in items),
        "recovery_success_rate": _rate(sum(item.get("recovered") is True for item in known_recovery), len(known_recovery)),
        "total_tokens_observed": sum(token_values) if token_values else None,
        "total_cost_usd_observed": round(sum(cost_values), 6) if cost_values else None,
    }


def run_benchmark(path: Path, *, timeout: int = 1800, workdir: Path | None = None,
                  keep_workspaces: bool = False) -> dict[str, Any]:
    config = load_benchmark(path)
    validate_benchmark_files(config)
    root = path.resolve().parent
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if workdir is None:
        temporary = tempfile.TemporaryDirectory(prefix="agentic-quality-")
        workspace_root = Path(temporary.name)
    else:
        workspace_root = workdir.resolve()
        workspace_root.mkdir(parents=True, exist_ok=True)
    try:
        records: list[dict[str, Any]] = []
        for task in config["tasks"]:
            for repetition in range(1, config["repetitions"] + 1):
                for runner in RUNNERS:
                    records.append(run_scenario(
                        root, task, runner, config["runners"][runner], workspace_root,
                        timeout, keep_workspace=keep_workspaces, repetition=repetition,
                    ))
                for reviewer, command in config["reviewers"].items():
                    records.append(run_scenario(
                        root, task, f"reviewer:{reviewer}", command, workspace_root,
                        timeout, keep_workspace=keep_workspaces,
                        source_repository=task["review_repository"], repetition=repetition,
                    ))
    finally:
        if temporary is not None:
            temporary.cleanup()
    scenario_names = list(RUNNERS) + [f"reviewer:{name}" for name in config["reviewers"]]
    by_runner = {
        runner: aggregate_metrics(item for item in records if item["runner"] == runner)
        for runner in scenario_names
    }
    reviewer_metrics = {
        reviewer: by_runner[f"reviewer:{reviewer}"]
        for reviewer in config["reviewers"]
    }
    task_metrics = {}
    for task in config["tasks"]:
        task_records = [item for item in records if item["task_id"] == task["id"]]
        task_metrics[task["id"]] = {
            "name": task["name"],
            "scenarios": {
                runner: aggregate_metrics(
                    item for item in task_records if item["runner"] == runner
                )
                for runner in scenario_names
            },
        }
    comparison: dict[str, Any] = {}
    for key, alone_value in by_runner["agent-alone"].items():
        agentic_value = by_runner["agentic"].get(key)
        if isinstance(alone_value, (int, float)) and isinstance(agentic_value, (int, float)):
            comparison[key] = round(agentic_value - alone_value, 4)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "benchmark": path.name,
        "timeout_seconds": timeout,
        "summary": {
            "total": len(records),
            "passed": sum(item["status"] == "passed" for item in records),
            "failed": sum(item["status"] != "passed" for item in records),
        },
        "scenarios": by_runner,
        "reviewer_comparison": {
            "same_candidate_fixture": bool(config["reviewers"]),
            "providers": reviewer_metrics,
        },
        "task_metrics": task_metrics,
        "agentic_minus_agent_alone": comparison,
        "results": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path, help="Benchmark corpus/configuration JSON")
    parser.add_argument("--live", action="store_true", help="Allow scenario commands to invoke real providers")
    parser.add_argument("--validate-only", action="store_true", help="Validate the corpus without running scenarios")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout per scenario and check in seconds")
    parser.add_argument("--workdir", type=Path, help="Workspace root for scenario copies")
    parser.add_argument("--keep-workspaces", action="store_true", help="Preserve disposable repositories for inspection")
    parser.add_argument("--output", type=Path, help="Write sanitized benchmark results to this path")
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    try:
        config = load_benchmark(args.benchmark.resolve())
        validate_benchmark_files(config)
    except ValueError as exc:
        parser.error(str(exc))
    if args.validate_only:
        scenario_count = len(config["runners"]) + len(config["reviewers"])
        print(
            f"Validated {len(config['tasks'])} tasks, {scenario_count} scenario runners, "
            f"and {config['repetitions']} repetition(s)."
        )
        return 0
    if not args.live:
        parser.error("scenario execution requires --live; use --validate-only for a credential-free check")
    payload = run_benchmark(
        args.benchmark.resolve(), timeout=args.timeout, workdir=args.workdir,
        keep_workspaces=args.keep_workspaces,
    )
    text = json.dumps(payload, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if payload["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
