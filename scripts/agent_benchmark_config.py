"""Validation and path handling for benchmark corpora.

This module owns the declarative benchmark format.  Scenario execution and
metric aggregation stay outside it so a validation-only command can inspect an
untrusted corpus without starting any configured process.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Iterable


SCHEMA_VERSION = 1
RUNNERS = ("agent-alone", "agentic")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PLACEHOLDER_NAMES = {
    "benchmark_root", "repo", "reviewer", "result_file", "scenario", "state_dir",
    "task_file", "task_id", "repetition",
}
MAX_TASKS = 100
MAX_CHECKS_PER_TASK = 100
MAX_DEFECTS_PER_TASK = 100
MAX_REVIEWERS = 20
MAX_SCENARIO_RECORDS = 100_000


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
    if len(tasks_raw) > MAX_TASKS:
        raise ValueError(f"benchmark.tasks must contain at most {MAX_TASKS} tasks")
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
        if len(checks_raw) > MAX_CHECKS_PER_TASK:
            raise ValueError(
                f"tasks[{index}].checks must contain at most {MAX_CHECKS_PER_TASK} commands"
            )
        checks = [_argv(command, f"tasks[{index}].checks[{check_index}]")
                  for check_index, command in enumerate(checks_raw, 1)]
        defects = _defects(item.get("defects"), f"tasks[{index}].defects")
        if len(defects) > MAX_DEFECTS_PER_TASK:
            raise ValueError(
                f"tasks[{index}].defects must contain at most {MAX_DEFECTS_PER_TASK} items"
            )
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
            "defects": defects,
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
    if len(reviewers_raw) > MAX_REVIEWERS:
        raise ValueError(
            f"benchmark.reviewers must contain at most {MAX_REVIEWERS} providers"
        )
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
    scenario_count = len(RUNNERS) + len(reviewers)
    record_count = len(tasks) * repetitions * scenario_count
    if record_count > MAX_SCENARIO_RECORDS:
        raise ValueError(
            "benchmark corpus expands to too many scenario records: "
            f"{record_count} > {MAX_SCENARIO_RECORDS}"
        )
    return {
        "path": path.resolve(), "tasks": tasks, "runners": runners,
        "reviewers": reviewers, "repetitions": repetitions,
    }


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
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must resolve inside the benchmark directory") from exc
    return candidate


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
