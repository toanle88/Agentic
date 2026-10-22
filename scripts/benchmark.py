#!/usr/bin/env python3
"""Run a deterministic, argv-only benchmark batch for configured checks.

The harness intentionally does not invoke live providers. A case is a JSON
object containing a name and command argv, so CI can benchmark fake providers
or repository fixtures without credentials or shell interpolation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any

try:
    from scripts.benchmark_execution import run_command
except ModuleNotFoundError:  # Direct ``python scripts/benchmark.py`` use.
    from benchmark_execution import run_command

try:
    from agentic_runner.security import redact_secrets
except ModuleNotFoundError:  # Source checkout without an editable install.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from agentic_runner.security import redact_secrets


def load_cases(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ValueError("benchmark cases must be a nonempty JSON array")
    cases: list[dict[str, Any]] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise ValueError(f"case {index} must be an object")
        name = item.get("name", f"case-{index}")
        command = item.get("command")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"case {index}.name must be a nonempty string")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError(f"case {index}.command must be a nonempty argv array")
        cases.append({"name": name, "command": command})
    return cases


def run_case(repo: Path, case: dict[str, Any], timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    command = case["command"]
    safe_command = {
        "executable": Path(command[0]).name,
        "argv_count": len(command),
    }
    try:
        result = run_command(
            command, cwd=repo, timeout=timeout, capture_output=True,
        )
        status = "timeout" if result.timed_out else "passed" if result.returncode == 0 else "failed"
        output = redact_secrets(result.output, extra_values=command)
        return {
            "name": case["name"], "command": safe_command, "status": status,
            "returncode": result.returncode, "duration_seconds": round(time.monotonic() - started, 3),
            "output": output,
        }
    except OSError as exc:
        return {
            "name": case["name"], "command": safe_command, "status": "failed",
            "returncode": 127, "duration_seconds": round(time.monotonic() - started, 3),
            "output": redact_secrets(str(exc), extra_values=command),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--cases", type=Path, required=True, help="JSON array of benchmark cases")
    parser.add_argument("--timeout", type=int, default=300, help="Per-case timeout in seconds")
    parser.add_argument("--output", type=Path, help="Write JSON results to this path")
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    repo = args.repo.resolve()
    cases = load_cases(args.cases.resolve())
    results = [run_case(repo, case, args.timeout) for case in cases]
    payload = {
        "schema_version": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "repo": str(repo),
        "timeout_seconds": args.timeout,
        "summary": {
            "total": len(results),
            "passed": sum(item["status"] == "passed" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
            "timeout": sum(item["status"] == "timeout" for item in results),
        },
        "results": results,
    }
    text = json.dumps(payload, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if payload["summary"]["failed"] == 0 and payload["summary"]["timeout"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
