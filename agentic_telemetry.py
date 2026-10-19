"""Small, dependency-free usage telemetry helpers.

Telemetry is deliberately best effort: providers may omit usage or cost data,
and missing values are represented as ``None`` rather than guessed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat(timespec="milliseconds")


def _load_run_record(folder: Path) -> tuple[Path, dict[str, Any]]:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "run.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schema_version", 1)
    return path, data


def _write_run_record(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    return None


def _cost(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def normalize_usage(raw: Any, *, provider: str | None = None) -> dict[str, Any]:
    """Normalize common provider usage shapes without inventing values."""
    if not isinstance(raw, dict):
        return {"provider": provider, **{key: None for key in USAGE_KEYS}, "estimated_cost_usd": None}
    nested_usage = raw.get("usage")
    usage = nested_usage if isinstance(nested_usage, dict) else raw
    prompt = _number(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = _number(usage.get("completion_tokens", usage.get("output_tokens")))
    total = _number(usage.get("total_tokens"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    cost = _cost(usage.get(
        "cost", usage.get("total_cost", usage.get("estimated_cost_usd", raw.get("cost")))
    ))
    return {
        "provider": provider or raw.get("provider"),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "estimated_cost_usd": cost,
    }


def extract_jsonl_usage(text: str, *, provider: str | None = None) -> dict[str, Any]:
    records = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        candidates = [value]
        for key in ("result", "response", "message"):
            nested = value.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
        merged = None
        for candidate in candidates:
            if not (
                "usage" in candidate
                or any(key in candidate for key in (
                    "prompt_tokens", "completion_tokens", "input_tokens",
                    "output_tokens", "total_tokens",
                ))
            ):
                continue
            normalized = normalize_usage(candidate, provider=provider)
            if merged is None:
                merged = normalized
                continue
            for key in (*USAGE_KEYS, "estimated_cost_usd"):
                if merged.get(key) is None and normalized.get(key) is not None:
                    merged[key] = normalized[key]
        if merged is not None:
            records.append(normalize_usage(merged, provider=provider))
    return combine_usage(records, provider=provider)


def combine_usage(records: Iterable[dict[str, Any]], *, provider: str | None = None) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "provider": provider,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
    }
    seen_tokens = {key: False for key in USAGE_KEYS}
    seen_cost = False
    for record in records:
        normalized = normalize_usage(record, provider=provider or record.get("provider"))
        if normalized.get("provider") and not totals.get("provider"):
            totals["provider"] = normalized["provider"]
        for key in USAGE_KEYS:
            value = normalized.get(key)
            if value is not None:
                totals[key] += value
                seen_tokens[key] = True
        value = normalized.get("estimated_cost_usd")
        if value is not None:
            totals["estimated_cost_usd"] += value
            seen_cost = True
    if not seen_tokens["total_tokens"] and seen_tokens["prompt_tokens"] and seen_tokens["completion_tokens"]:
        totals["total_tokens"] = totals["prompt_tokens"] + totals["completion_tokens"]
        seen_tokens["total_tokens"] = True
    for key in USAGE_KEYS:
        if not seen_tokens[key]:
            totals[key] = None
    if not seen_cost:
        totals["estimated_cost_usd"] = None
    return totals


def record_usage(folder: Path, role: str, usage: dict[str, Any], *,
                 invocation: dict[str, Any] | None = None) -> None:
    """Add one provider invocation to the backward-compatible run record."""
    path, data = _load_run_record(folder)
    by_role = data.setdefault("usage_by_role", {})
    if not isinstance(by_role, dict):
        by_role = {}
        data["usage_by_role"] = by_role
    existing = by_role.get(role)
    by_role[role] = combine_usage([existing or {}, usage], provider=usage.get("provider"))
    records = [value for value in by_role.values() if isinstance(value, dict)]
    data["usage"] = combine_usage(records)
    if invocation is not None:
        invocations = data.setdefault("usage_by_invocation", [])
        if not isinstance(invocations, list):
            invocations = []
            data["usage_by_invocation"] = invocations
        entry = {"role": role, **invocation, **usage}
        invocations.append(entry)
    _write_run_record(path, data)


def record_security_policy(folder: Path, config: dict[str, Any]) -> None:
    """Persist the effective security boundary without secrets or commands."""
    sandbox_value = config.get("sandbox")
    sandbox: dict[str, Any] = sandbox_value if isinstance(sandbox_value, dict) else {}
    developer_value = config.get("developer_agent")
    developer: dict[str, Any] = developer_value if isinstance(developer_value, dict) else {}
    reviewer_value = config.get("reviewer_agent")
    reviewer: dict[str, Any] = reviewer_value if isinstance(reviewer_value, dict) else {}
    enabled = sandbox.get("enabled") is True
    policy = {
        "mode": config.get("security_mode", "trusted"),
        "sandbox_enabled": enabled,
        "engine": sandbox.get("engine") if enabled else None,
        "image": sandbox.get("image") if enabled else None,
        "network": ("bridge" if sandbox.get("allow_network") else "none") if enabled else "host",
        "worktree_mount": "rw" if enabled else "host",
        "git_metadata_mount": "ro" if enabled else "host",
        "passed_environment_names": list(sandbox.get("pass_env", [])) if enabled else [],
        "developer": {
            "provider": developer.get("provider", "codex"),
            "filesystem": "write",
        },
        "reviewer": {
            "provider": reviewer.get("provider", "codex"),
            "filesystem": "read-only",
        },
    }
    path, data = _load_run_record(folder)
    history = data.get("security_policy_history")
    if not isinstance(history, list):
        history = []
        data["security_policy_history"] = history
    if not history or not isinstance(history[-1], dict) or history[-1].get("policy") != policy:
        history.append({"recorded_at": _timestamp(), "policy": policy})
    if not isinstance(data.get("security_policy"), dict):
        data["security_policy"] = policy
    _write_run_record(path, data)


def record_operation(folder: Path, role: str, name: str, *,
                     started_at: str | None = None,
                     finished_at: str | None = None,
                     duration_seconds: float | None = None,
                     status: str = "completed",
                     exit_code: int | None = None) -> None:
    """Persist bounded timing/status metadata for one run operation.

    Operation records intentionally contain no command arguments or prompt
    text.  The aggregate duration spans the first recorded operation through
    the latest completed operation, while each operation keeps its own active
    duration.
    """
    path, data = _load_run_record(folder)
    metrics = data.setdefault("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
        data["metrics"] = metrics
    operations = metrics.setdefault("operations", [])
    if not isinstance(operations, list):
        operations = []
        metrics["operations"] = operations
    started = started_at or _timestamp()
    finished = finished_at or _timestamp()
    try:
        bounded_duration = max(0.0, float(duration_seconds or 0.0))
    except (TypeError, ValueError):
        bounded_duration = 0.0
    entry: dict[str, Any] = {
        "role": role,
        "name": name,
        "started_at": started,
        "finished_at": finished,
        "duration_seconds": round(bounded_duration, 3),
        "status": status,
    }
    if exit_code is not None:
        entry["exit_code"] = exit_code
    operations.append(entry)

    previous_start = metrics.get("started_at")
    previous_finish = metrics.get("finished_at")
    metrics["started_at"] = min(
        [value for value in (previous_start, started) if isinstance(value, str)],
    )
    metrics["finished_at"] = max(
        [value for value in (previous_finish, finished) if isinstance(value, str)],
    )
    try:
        begin = datetime.fromisoformat(metrics["started_at"])
        end = datetime.fromisoformat(metrics["finished_at"])
        metrics["duration_seconds"] = round(max(0.0, (end - begin).total_seconds()), 3)
    except (TypeError, ValueError):
        durations = []
        for item in operations:
            if not isinstance(item, dict):
                continue
            try:
                durations.append(max(0.0, float(item.get("duration_seconds", 0))))
            except (TypeError, ValueError):
                continue
        metrics["duration_seconds"] = round(sum(durations), 3)
    metrics["operation_count"] = len(operations)
    _write_run_record(path, data)


def read_run_usage(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return combine_usage([])
    if not isinstance(data, dict):
        return combine_usage([])
    usage = data.get("usage", combine_usage([]))
    if not isinstance(usage, dict):
        usage = combine_usage([])
    result = dict(usage)
    if isinstance(data.get("usage_by_role"), dict):
        result["by_role"] = data["usage_by_role"]
    if isinstance(data.get("usage_by_invocation"), list):
        result["by_invocation"] = data["usage_by_invocation"]
    if isinstance(data.get("metrics"), dict):
        result["metrics"] = data["metrics"]
    if isinstance(data.get("security_policy"), dict):
        result["security_policy"] = data["security_policy"]
    if isinstance(data.get("security_policy_history"), list):
        result["security_policy_history"] = data["security_policy_history"]
    return result
