"""Small, dependency-free usage telemetry helpers.

Telemetry is deliberately best effort: providers may omit usage or cost data,
and missing values are represented as ``None`` rather than guessed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .context import INSTRUCTION_CONTEXT_CHARS, _bounded_text
from .persistence import atomic_write_json
from .security import redact_secrets


USAGE_KEYS = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
)

# Run metadata is durable and may be appended to by repeated resumes.  Keep
# the useful newest history while leaving aggregate fields available to old
# readers.  The byte limit also bounds legacy reads before JSON parsing.
MAX_RUN_RECORD_BYTES = 256_000
MAX_OPERATION_RECORDS = 100
MAX_INVOCATION_RECORDS = 100
MAX_POLICY_HISTORY_RECORDS = 50
MAX_GUIDANCE_RECORDS = 100
MAX_GUIDANCE_STATS = 100

_RETENTION_LIMITS = {
    "usage_by_invocation": MAX_INVOCATION_RECORDS,
    "security_policy_history": MAX_POLICY_HISTORY_RECORDS,
    "guidance_loads": MAX_GUIDANCE_RECORDS,
    "guidance_stats": MAX_GUIDANCE_STATS,
}
_KNOWN_RUN_FIELDS = {
    "schema_version", "usage_by_role", "usage_by_stage", "usage",
    "usage_by_invocation", "security_policy", "security_policy_history",
    "instruction", "issue_metadata", "metrics", "guidance_loads",
    "guidance_stats", "workflow_metrics",
}


def _timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat(timespec="milliseconds")


def _read_run_record(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_RUN_RECORD_BYTES:
            # An oversized legacy artifact is untrusted input.  Do not read
            # it in full; the next write starts a bounded record instead.
            return {}
        data = json.loads(redact_secrets(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _load_run_record(folder: Path) -> tuple[Path, dict[str, Any]]:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "run.json"
    data = _read_run_record(path)
    data.setdefault("schema_version", 1)
    return path, data


def _json_size(value: dict[str, Any]) -> int:
    return len((json.dumps(value, indent=2) + "\n").encode("utf-8"))


def _compact_run_record(data: dict[str, Any]) -> dict[str, Any]:
    """Retain newest history and enforce the complete record byte budget."""
    unknown_fields = [field for field in data if field not in _KNOWN_RUN_FIELDS]
    for field, limit in _RETENTION_LIMITS.items():
        value = data.get(field)
        if isinstance(value, list):
            data[field] = value[-limit:]
    metrics = data.get("metrics")
    if isinstance(metrics, dict) and isinstance(metrics.get("operations"), list):
        metrics["operations"] = metrics["operations"][-MAX_OPERATION_RECORDS:]
        metrics["operation_count"] = len(metrics["operations"])

    # Normal records fit comfortably after the per-array caps.  For oversized
    # legacy values, progressively discard oldest optional history while
    # preserving the newest entry and aggregate/scalar fields.
    while _json_size(data) > MAX_RUN_RECORD_BYTES:
        candidates: list[tuple[str, list[Any]]] = []
        for field in _RETENTION_LIMITS:
            value = data.get(field)
            if isinstance(value, list) and len(value) > 1:
                candidates.append((field, value))
        if isinstance(metrics, dict) and isinstance(metrics.get("operations"), list):
            operations = metrics["operations"]
            if len(operations) > 1:
                candidates.append(("metrics.operations", operations))
        if not candidates:
            break
        _field, largest = max(candidates, key=lambda item: _json_size({"value": item[1]}))
        retained = largest[-max(1, len(largest) // 2):]
        if _field == "metrics.operations" and isinstance(metrics, dict):
            metrics["operations"] = retained
            metrics["operation_count"] = len(retained)
        else:
            data[_field] = retained
    # Preserve extension fields for normal-sized records.  If a legacy or
    # provider-specific field is what keeps an oversized record above the
    # budget, discard the largest such fields only after known history has
    # been compacted.
    while _json_size(data) > MAX_RUN_RECORD_BYTES and unknown_fields:
        field = max(
            unknown_fields,
            key=lambda name: _json_size({"value": data.get(name)}),
        )
        unknown_fields.remove(field)
        data.pop(field, None)
    if _json_size(data) > MAX_RUN_RECORD_BYTES:
        # A single malformed legacy value can be larger than the budget even
        # after history compaction.  Keep a valid marker rather than allowing
        # the write boundary to recreate an oversized artifact.
        data = {"schema_version": 1, "record_truncated": True}
    return data


def _write_run_record(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, _compact_run_record(data))


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
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    completion_details = completion_details if isinstance(completion_details, dict) else {}
    cache_read = _number(usage.get("cache_read_tokens", usage.get("cache_read_input_tokens")))
    cache_read = cache_read if cache_read is not None else _number(prompt_details.get("cached_tokens"))
    cache_write = _number(usage.get("cache_write_tokens", usage.get("cache_creation_input_tokens")))
    cache_write = cache_write if cache_write is not None else _number(prompt_details.get("cache_write_tokens"))
    reasoning = _number(usage.get("reasoning_tokens"))
    reasoning = reasoning if reasoning is not None else _number(completion_details.get("reasoning_tokens"))
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
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
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
                    "output_tokens", "total_tokens", "cache_read_tokens",
                    "cache_write_tokens", "reasoning_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens",
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
            scope = value.get("usage_scope")
            usage_value = value.get("usage")
            if isinstance(usage_value, dict):
                scope = usage_value.get("scope", usage_value.get("usage_scope", scope))
            records.append((normalize_usage(merged, provider=provider), scope == "cumulative"))
    cumulative = [record for record, is_cumulative in records if is_cumulative]
    if cumulative:
        # Providers that expose cumulative JSONL usage are counted from their
        # final snapshot, not once per event. Per-event records remain additive.
        per_event = [record for record, is_cumulative in records if not is_cumulative]
        records = per_event + [(cumulative[-1], False)]
    return combine_usage([record for record, _ in records], provider=provider)


def combine_usage(records: Iterable[dict[str, Any]], *, provider: str | None = None) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "provider": provider,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
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
    by_stage = data.setdefault("usage_by_stage", {})
    if not isinstance(by_stage, dict):
        by_stage = {}
        data["usage_by_stage"] = by_stage
    stage = role
    if invocation and isinstance(invocation.get("stage"), str):
        stage = invocation["stage"]
    by_stage[stage] = combine_usage(
        [by_stage.get(stage) or {}, usage], provider=usage.get("provider"),
    )
    records = [value for value in by_role.values() if isinstance(value, dict)]
    data["usage"] = combine_usage(records)
    if invocation is not None:
        invocations = data.setdefault("usage_by_invocation", [])
        if not isinstance(invocations, list):
            invocations = []
            data["usage_by_invocation"] = invocations
        entry = {"role": role, **invocation, **usage}
        # Keep usage records order-independent for the dashboard. Older
        # records continue to work through dashboard timestamp fallbacks.
        entry.setdefault("recorded_at", _timestamp())
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
        "permission_policy": {
            "developer": dict(developer.get("permission_policy", {})),
            "reviewer": dict(reviewer.get("permission_policy", {})),
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


def record_run_instruction(folder: Path, instruction: str) -> None:
    """Persist or clear the bounded, user-provided instruction for a run."""
    path, data = _load_run_record(folder)
    bounded = _bounded_text(instruction, INSTRUCTION_CONTEXT_CHARS)
    if bounded.strip():
        data["instruction"] = bounded
    else:
        data.pop("instruction", None)
    _write_run_record(path, data)


def record_issue_metadata(folder: Path, *, number: int | None, title: str,
                          state: str, source: str = "github",
                          body: str = "", audit_key: str | None = None) -> None:
    """Persist bounded issue identity for read-only dashboards and resumes."""
    path, data = _load_run_record(folder)
    data["issue_metadata"] = {
        "number": number,
        "title": _bounded_text(redact_secrets(title), 280),
        "state": _bounded_text(state, 40).upper() or "UNKNOWN",
        "source": _bounded_text(source, 40) or "github",
    }
    if audit_key:
        data["issue_metadata"]["audit_key"] = _bounded_text(audit_key, 32)
    # Keep a bounded, redacted task snapshot so an existing run can be resumed
    # during a GitHub outage.  It is intentionally separate from run.json and
    # excluded from dashboard log listings.
    atomic_write_json(folder / "issue-snapshot.json", {
        "number": number,
        "title": _bounded_text(redact_secrets(title), 280),
        "body": _bounded_text(redact_secrets(body), 12_000),
        "state": _bounded_text(state, 40).upper() or "UNKNOWN",
        "source": _bounded_text(source, 40) or "github",
        **({"audit_key": _bounded_text(audit_key, 32)} if audit_key else {}),
    })
    _write_run_record(path, data)


def read_issue_snapshot(folder: Path) -> dict[str, Any] | None:
    """Read the bounded issue snapshot used by offline run resumes."""
    try:
        value = json.loads(redact_secrets((folder / "issue-snapshot.json").read_text(encoding="utf-8")))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if not isinstance(value.get("body"), str) or not isinstance(value.get("title"), str):
        return None
    return value


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


def record_guidance_loads(folder: Path, loads: list[dict[str, Any]], *,
                          prompt_chars: int | None = None,
                          prompt_chars_with_guidance: int | None = None) -> None:
    """Record sanitized repository-guidance inventory without its contents."""
    if not loads:
        return
    path, data = _load_run_record(folder)
    records = data.setdefault("guidance_loads", [])
    if not isinstance(records, list):
        records = []
        data["guidance_loads"] = records
    for load in loads:
        if not isinstance(load, dict):
            continue
        relative = load.get("path")
        role = load.get("role")
        phase = load.get("phase")
        digest = load.get("sha256")
        if (
            not isinstance(relative, str) or not relative
            or not isinstance(role, str) or not role
            or not isinstance(phase, str) or not phase
            or not isinstance(digest, str) or not digest
        ):
            continue
        try:
            size = max(0, int(load.get("size", 0)))
        except (TypeError, ValueError):
            size = 0
        entry = {
            "path": relative.replace("\\", "/"),
            "role": role,
            "phase": phase,
            "size": size,
            "sha256": digest,
        }
        content_chars = load.get("content_chars")
        if isinstance(content_chars, int) and content_chars >= 0:
            entry["content_chars"] = content_chars
        records.append(entry)
    stats = data.setdefault("guidance_stats", [])
    if not isinstance(stats, list):
        stats = []
        data["guidance_stats"] = stats
    total_chars = sum(
        int(item.get("content_chars", 0))
        for item in loads
        if isinstance(item, dict) and isinstance(item.get("content_chars"), int)
    )
    stat: dict[str, Any] = {
        "role": next((item.get("role") for item in loads if isinstance(item, dict)), "unknown"),
        "phase": next((item.get("phase") for item in loads if isinstance(item, dict)), "unknown"),
        "file_count": len(loads),
        "content_chars": max(0, total_chars),
    }
    if prompt_chars is not None:
        stat["prompt_chars_without_guidance"] = max(0, int(prompt_chars))
        stat["prompt_chars_with_guidance"] = max(
            0,
            int(prompt_chars_with_guidance)
            if prompt_chars_with_guidance is not None
            else int(prompt_chars) + total_chars,
        )
    stats.append(stat)
    _write_run_record(path, data)


def record_workflow_outcome(folder: Path, *, approved: bool, attempts: int,
                            first_pass: bool, review_findings: int = 0) -> None:
    """Persist bounded outcome metrics used to compare workflow efficiency."""
    path, data = _load_run_record(folder)
    metrics = data.setdefault("workflow_metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
        data["workflow_metrics"] = metrics
    metrics.update({
        "approved": bool(approved),
        "attempts": max(0, int(attempts)),
        "retries": max(0, int(attempts) - 1),
        "first_pass_approval": bool(approved and first_pass),
        "review_findings": max(0, int(review_findings)),
    })
    _write_run_record(path, data)


def read_run_usage(folder: Path) -> dict[str, Any]:
    data = _read_run_record(folder / "run.json")
    if not data:
        return combine_usage([])
    usage = data.get("usage", combine_usage([]))
    if not isinstance(usage, dict):
        usage = combine_usage([])
    result = dict(usage)
    if isinstance(data.get("usage_by_role"), dict):
        result["by_role"] = data["usage_by_role"]
    if isinstance(data.get("usage_by_stage"), dict):
        result["by_stage"] = data["usage_by_stage"]
    if isinstance(data.get("usage_by_invocation"), list):
        result["by_invocation"] = data["usage_by_invocation"]
    if isinstance(data.get("metrics"), dict):
        result["metrics"] = data["metrics"]
    if isinstance(data.get("security_policy"), dict):
        result["security_policy"] = data["security_policy"]
    if isinstance(data.get("security_policy_history"), list):
        result["security_policy_history"] = data["security_policy_history"]
    if isinstance(data.get("guidance_loads"), list):
        result["guidance_loads"] = data["guidance_loads"]
    if isinstance(data.get("guidance_stats"), list):
        result["guidance_stats"] = data["guidance_stats"]
    if isinstance(data.get("workflow_metrics"), dict):
        result["workflow_metrics"] = data["workflow_metrics"]
        if (
            data["workflow_metrics"].get("approved") is True
            and isinstance(result.get("total_tokens"), int)
        ):
            result["tokens_per_approved_task"] = result["total_tokens"]
    if isinstance(data.get("issue_metadata"), dict):
        result["issue_metadata"] = data["issue_metadata"]
    instruction = data.get("instruction")
    if isinstance(instruction, str) and instruction.strip():
        result["instruction"] = _bounded_text(instruction, INSTRUCTION_CONTEXT_CHARS)
    return result


def read_run_instruction(folder: Path) -> str | None:
    """Read an optional bounded instruction from a run record."""
    value = read_run_usage(folder).get("instruction")
    return value if isinstance(value, str) else None
