#!/usr/bin/env python3
"""Shared helpers for opt-in, provider-specific benchmark adapters.

The adapter contract deliberately keeps provider commands outside the
repository.  A caller supplies an argv array as JSON in an environment
variable; this module renders only documented placeholders and never invokes
a shell. Provider-specific wrappers can therefore be checked in without
storing credentials, model choices, or local installation paths.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


PLACEHOLDERS = {
    "repo": "AGENTIC_BENCHMARK_REPO",
    "task_file": "AGENTIC_BENCHMARK_TASK_FILE",
    "result_file": "AGENTIC_BENCHMARK_RESULT_FILE",
    "reviewer": "AGENTIC_BENCHMARK_REVIEWER",
    "scenario": "AGENTIC_BENCHMARK_SCENARIO",
    "task_id": "AGENTIC_BENCHMARK_TASK_ID",
}
DEFAULT_TIMEOUT = 1800


def _load_command(name: str) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        raise RuntimeError(
            f"{name} is required; provide a JSON argv array for the local provider setup"
        )
    try:
        command = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must contain valid JSON: {exc}") from exc
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise RuntimeError(f"{name} must contain a nonempty JSON argv array")
    return command


def _render(command: list[str]) -> list[str]:
    values = {name: os.environ.get(env_name, "") for name, env_name in PLACEHOLDERS.items()}
    rendered: list[str] = []
    for part in command:
        value = part
        for name, replacement in values.items():
            if "{" + name + "}" in value and not replacement:
                raise RuntimeError(
                    f"{PLACEHOLDERS[name]} is required when the command uses {{{name}}}"
                )
            value = value.replace("{" + name + "}", replacement)
        rendered.append(value)
    return rendered


def _write_default_result(path: Path, *, success: bool, error: str | None = None) -> None:
    result: dict[str, Any] = {
        "status": "success" if success else "failed",
        "completed": success,
        "attempts": 1,
        "interventions": 0,
        "recovered": False,
    }
    if error:
        result["error"] = "adapter command failed"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def run_configured_adapter(command_env: str) -> int:
    """Run a caller-supplied provider command using the benchmark contract."""
    result_file = Path(os.environ["AGENTIC_BENCHMARK_RESULT_FILE"])
    command = _render(_load_command(command_env))
    try:
        timeout = int(os.environ.get("AGENTIC_BENCHMARK_ADAPTER_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError as exc:
        raise RuntimeError("AGENTIC_BENCHMARK_ADAPTER_TIMEOUT must be an integer") from exc
    if timeout < 1:
        raise RuntimeError("AGENTIC_BENCHMARK_ADAPTER_TIMEOUT must be positive")

    try:
        completed = subprocess.run(
            command,
            cwd=os.environ["AGENTIC_BENCHMARK_REPO"],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _write_default_result(result_file, success=False, error="timeout")
        return 124
    except OSError as exc:
        _write_default_result(result_file, success=False, error=str(exc))
        return 127

    # A provider-aware command may write richer, normalized telemetry.  The
    # fallback keeps the adapter useful for simple commands while ensuring the
    # benchmark runner always receives a result file.
    if not result_file.is_file():
        _write_default_result(
            result_file,
            success=completed.returncode == 0,
            error=completed.stdout[-500:] if completed.returncode else None,
        )
    return completed.returncode


def main(command_env: str) -> int:
    try:
        return run_configured_adapter(command_env)
    except (KeyError, RuntimeError) as exc:
        print(f"benchmark adapter error: {exc}", file=sys.stderr)
        result_name = os.environ.get("AGENTIC_BENCHMARK_RESULT_FILE")
        if result_name:
            _write_default_result(Path(result_name), success=False, error=str(exc))
        return 2
