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
from pathlib import Path
import tempfile
from typing import Any

try:
    from scripts.agent_benchmark_reporting import aggregate_metrics
    from scripts.agent_benchmark_config import (  # noqa: F401
        PLACEHOLDER_NAMES, RUNNERS, SCHEMA_VERSION, TASK_ID,
        _argv, _defects, _path_under_root, _relative_path,
        _validate_placeholders, load_benchmark, render_command,
        validate_benchmark_files,
    )
    from scripts.agent_benchmark_scenarios import (  # noqa: F401
        MAX_ERROR, REPORTED_STATUSES, SUCCESS_STATUSES,
        _finding_ids, _number, _read_result, _run_checks, _run_git,
        _scenario_record, prepare_repository, run_scenario,
    )
except ModuleNotFoundError:  # Direct ``python scripts/agent_benchmark.py`` use.
    from agent_benchmark_reporting import aggregate_metrics
    from agent_benchmark_config import (  # noqa: F401
        PLACEHOLDER_NAMES, RUNNERS, SCHEMA_VERSION, TASK_ID,
        _argv, _defects, _path_under_root, _relative_path,
        _validate_placeholders, load_benchmark, render_command,
        validate_benchmark_files,
    )
    from agent_benchmark_scenarios import (  # noqa: F401
        MAX_ERROR, REPORTED_STATUSES, SUCCESS_STATUSES,
        _finding_ids, _number, _read_result, _run_checks, _run_git,
        _scenario_record, prepare_repository, run_scenario,
    )



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
