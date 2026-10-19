"""Aggregation of normalized benchmark scenario records."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def aggregate_metrics(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Reduce scenario records into stable, provider-neutral benchmark metrics."""
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
    token_values = [
        item["usage"]["total_tokens"] for item in items
        if item.get("usage", {}).get("total_tokens") is not None
    ]
    cost_values = [
        item["usage"]["estimated_cost_usd"] for item in items
        if item.get("usage", {}).get("estimated_cost_usd") is not None
    ]
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
        "average_duration_seconds": round(
            sum(float(item["duration_seconds"]) for item in items) / total, 3
        ) if total else None,
        "average_attempts": round(
            sum(int(item["attempts"]) for item in items) / total, 3
        ) if total else None,
        "interventions": sum(int(item["interventions"]) for item in items),
        "recovery_success_rate": _rate(
            sum(item.get("recovered") is True for item in known_recovery),
            len(known_recovery),
        ),
        "total_tokens_observed": sum(token_values) if token_values else None,
        "total_cost_usd_observed": round(sum(cost_values), 6) if cost_values else None,
    }
