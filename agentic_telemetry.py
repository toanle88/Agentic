"""Small, dependency-free usage telemetry helpers.

Telemetry is deliberately best effort: providers may omit usage or cost data,
and missing values are represented as ``None`` rather than guessed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


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
        if isinstance(value, dict) and ("usage" in value or "input_tokens" in value or "output_tokens" in value):
            records.append(normalize_usage(value, provider=provider))
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


def record_usage(folder: Path, role: str, usage: dict[str, Any]) -> None:
    """Add one provider invocation to the backward-compatible run record."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "run.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schema_version", 1)
    by_role = data.setdefault("usage_by_role", {})
    if not isinstance(by_role, dict):
        by_role = {}
        data["usage_by_role"] = by_role
    existing = by_role.get(role)
    by_role[role] = combine_usage([existing or {}, usage], provider=usage.get("provider"))
    records = [value for value in by_role.values() if isinstance(value, dict)]
    data["usage"] = combine_usage(records)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def read_run_usage(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return combine_usage([])
    return data.get("usage", combine_usage([])) if isinstance(data, dict) else combine_usage([])
