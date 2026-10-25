#!/usr/bin/env python3
"""Measure dashboard collection with reproducible cold and warm samples.

The command is offline and provider-free.  Pass an existing run corpus with
``--logs`` or create the documented synthetic corpus with ``--generate``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # pragma: no cover - native Windows has no resource module.
    resource = None

try:
    from agentic_runner import dashboard, dashboard_data, openrouter, worktrees
except ModuleNotFoundError:  # Direct source-checkout execution.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from agentic_runner import dashboard, dashboard_data, openrouter, worktrees


def percentile(values: list[float], fraction: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def peak_rss_bytes() -> int | None:
    if resource is None:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB; macOS reports bytes.
    return int(value * 1024 if sys.platform.startswith("linux") else value)


def generate_corpus(
    root: Path,
    *,
    runs: int,
    artifacts: int,
    developer_bytes: int,
) -> None:
    """Create the bounded historical-style dashboard corpus without providers."""
    if root.exists():
        raise ValueError(f"refusing to overwrite existing corpus: {root}")
    if min(runs, artifacts, developer_bytes) < 1:
        raise ValueError("corpus dimensions must be positive")
    payload = b"x" * min(developer_bytes, 1024 * 1024)
    root.mkdir(parents=True)
    for run_number in range(runs):
        folder = root / f"agent-issue-{run_number + 1}-benchmark{run_number:04d}"
        folder.mkdir()
        remaining = developer_bytes
        with (folder / "developer-001.txt").open("wb") as stream:
            while remaining:
                chunk = payload[:min(remaining, len(payload))]
                stream.write(chunk)
                remaining -= len(chunk)
        for artifact_number in range(2, artifacts + 1):
            (folder / f"artifact-{artifact_number:03d}.log").write_bytes(b"x\n")


def sample(logs: Path, *, cold: bool, baseline_peak: int | None = None) -> tuple[float, int | None]:
    if cold:
        with dashboard_data._RUN_SNAPSHOT_CACHE_LOCK:
            dashboard_data._RUN_SNAPSHOT_CACHE.clear()
    started = time.perf_counter()
    dashboard_data.collect_runs(logs)
    peak = peak_rss_bytes()
    if peak is not None and baseline_peak is not None:
        # ru_maxrss is a process high-water mark. Subtract the value observed
        # before this benchmark so repeated samples do not report unrelated
        # memory retained by earlier work in the harness process.
        peak = max(0, peak - baseline_peak)
    return time.perf_counter() - started, peak


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50_seconds": percentile(values, 0.50),
        "p95_seconds": percentile(values, 0.95),
        "p99_seconds": percentile(values, 0.99),
    }


def _prepare_git_fixture(root: Path) -> Path | None:
    """Create a disposable repository for local, provider-free Git samples."""
    if shutil.which("git") is None:
        return None
    root.mkdir()
    result = subprocess.run(
        ["git", "init", "-q"], cwd=root, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("could not initialize the benchmark Git fixture")
    for index in range(12):
        (root / f"tracked-{index:02d}.txt").write_text(
            f"benchmark content {index}\n", encoding="utf-8",
        )
    result = subprocess.run(
        ["git", "add", "."], cwd=root, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("could not stage the benchmark Git fixture")
    return root


def _sample_openrouter_tools(worktree: Path) -> None:
    openrouter._openrouter_tool_call(
        worktree, "list_files", {"path": "."}, readonly=True,
    )
    openrouter._openrouter_tool_call(
        worktree, "read_file", {"path": "notes.txt"}, readonly=True,
    )


def _sample_git_operations(repo: Path) -> None:
    worktrees.git(repo, "status", "--short")
    worktrees.git(repo, "diff", "--cached", "--name-only")
    worktrees.git(repo, "ls-files")


def measure(logs: Path, repetitions: int) -> dict[str, Any]:
    cold: list[float] = []
    warm: list[float] = []
    peak_values: list[int] = []
    baseline_peak = peak_rss_bytes()
    case_samples: dict[str, dict[str, list[float]]] = {
        name: {"cold": [], "warm": []}
        for name in (
            "dashboard_collection", "dashboard_rendering",
            "openrouter_file_tools", "git_operations",
        )
    }
    with tempfile.TemporaryDirectory(prefix="agentic-dashboard-benchmark-") as temp:
        fixture_root = Path(temp)
        tool_root = fixture_root / "openrouter-worktree"
        tool_root.mkdir()
        (tool_root / "notes.txt").write_text(
            "provider-free benchmark fixture\n", encoding="utf-8",
        )
        (tool_root / "nested").mkdir()
        (tool_root / "nested" / "detail.txt").write_text("detail\n", encoding="utf-8")
        git_repo = _prepare_git_fixture(fixture_root / "git-worktree")
        for _ in range(repetitions):
            for phase, is_cold in (("cold", True), ("warm", False)):
                collection_seconds, collection_peak = sample(
                    logs, cold=is_cold, baseline_peak=baseline_peak,
                )
                case_samples["dashboard_collection"][phase].append(collection_seconds)
                if phase == "cold":
                    cold.append(collection_seconds)
                else:
                    warm.append(collection_seconds)
                peak_values.extend(
                    value for value in (collection_peak,) if value is not None
                )

                runs = dashboard_data.collect_runs(logs)
                started = time.perf_counter()
                dashboard.render_dashboard(runs, "benchmark", logs.name)
                case_samples["dashboard_rendering"][phase].append(
                    time.perf_counter() - started,
                )

                started = time.perf_counter()
                _sample_openrouter_tools(tool_root)
                case_samples["openrouter_file_tools"][phase].append(
                    time.perf_counter() - started,
                )

                if git_repo is not None:
                    started = time.perf_counter()
                    _sample_git_operations(git_repo)
                    case_samples["git_operations"][phase].append(
                        time.perf_counter() - started,
                    )

    cases: dict[str, dict[str, Any]] = {}
    for name, values in case_samples.items():
        if values["cold"]:
            cases[name] = {
                "status": "measured",
                "cold": _summary(values["cold"]),
                "warm": _summary(values["warm"]),
            }
        else:
            cases[name] = {
                "status": "unavailable",
                "reason": "Git executable unavailable",
            }

    return {
        "repetitions": repetitions,
        "cold": _summary(cold),
        "warm": _summary(warm),
        "cases": cases,
        "peak_resident_memory_bytes": max(peak_values) if peak_values else None,
        "host": {
            "python": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "filesystem": "not identified by the standard library",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--runs", type=int, default=500)
    parser.add_argument("--artifacts", type=int, default=24)
    parser.add_argument("--developer-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 3 <= args.repetitions <= 20:
        parser.error("--repetitions must be between 3 and 20")
    logs = args.logs.resolve()
    try:
        if args.generate:
            generate_corpus(
                logs, runs=args.runs, artifacts=args.artifacts,
                developer_bytes=args.developer_bytes,
            )
        if not logs.is_dir():
            raise ValueError(f"logs directory does not exist: {logs}")
        payload = {
            "schema_version": 1,
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "logs": str(logs),
            "measurement": measure(logs, args.repetitions),
        }
    except ValueError as exc:
        parser.error(str(exc))
    text = json.dumps(payload, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
