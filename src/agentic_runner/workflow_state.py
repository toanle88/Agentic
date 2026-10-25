"""Durable state helpers for the developer/reviewer workflow.

This module owns bounded run evidence and approval markers.  Workflow
orchestration remains in :mod:`agentic_workflow`; the compatibility facade
re-exports these helpers so older integrations keep their import paths.
"""
from __future__ import annotations

import datetime
import json
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .execution import _short
from .models import Issue, Run
from .output import strip_ansi
from .persistence import (
    atomic_write_json,
    atomic_write_text,
    read_text_bounded,
    read_text_tail,
)
from .security import redact_secrets
from .worktrees import issue_fingerprint

MANUAL_APPROVAL_FILENAME = "manual-approval.json"
REVIEW_APPROVAL_FILENAME = "review-approval.json"
APPROVAL_EVENTS_FILENAME = "approval-events.json"
MAX_DURABLE_JSON_BYTES = 256_000
MAX_DEVELOPER_REPORT_BYTES = 24_000
MAX_APPROVAL_EVENTS = 50


def next_log(folder: Path, kind: str, ext: str = "txt") -> Path:
    """Return the next numbered durable log path for a run."""
    numbers = []
    for path in folder.glob(f"{kind}-*.{ext}"):
        suffix = path.stem.removeprefix(f"{kind}-")
        if suffix.isdigit():
            numbers.append(int(suffix))
    return folder / f"{kind}-{max(numbers, default=0) + 1:03d}.{ext}"


def latest_numbered_log(folder: Path, kind: str, ext: str) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    for path in folder.glob(f"{kind}-*.{ext}"):
        suffix = path.stem.removeprefix(f"{kind}-")
        if suffix.isdigit():
            candidates.append((int(suffix), path))
    return max(candidates, default=(0, None))[1]


def load_latest_developer_report(folder: Path, limit: int = 6_000) -> str:
    """Load the latest bounded developer handoff for standalone review."""
    path = latest_numbered_log(folder, "developer", "txt")
    if path is None:
        return ""
    limit = max(0, min(limit, 6_000))
    if limit == 0:
        return ""
    return redact_secrets(
        read_text_tail(path, min(MAX_DEVELOPER_REPORT_BYTES, limit * 4))
    )[-limit:]


def manual_approval_path(folder: Path) -> Path:
    return folder / MANUAL_APPROVAL_FILENAME


def review_approval_path(folder: Path) -> Path:
    return folder / REVIEW_APPROVAL_FILENAME


def approval_events_path(folder: Path) -> Path:
    return folder / APPROVAL_EVENTS_FILENAME


def _blocked_path(folder: Path) -> Path:
    return folder / "blocked.json"


def _result_path(folder: Path) -> Path:
    return folder / "result.json"


def result_text(value: Any, limit: int = 1_200) -> str:
    """Bound and redact untrusted evidence before showing or persisting it."""
    text = strip_ansi(str(value)).strip()
    if not text:
        return ""
    return _short(redact_secrets(text), limit)


def developer_attempts(folder: Path) -> int:
    return len(list(folder.glob("developer-*.txt")))


def _finding_requirement(value: Any) -> str:
    """Return the concise requirement from a legacy or structured finding."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("requirement", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return ""


def review_reason(verdict: dict[str, Any]) -> str:
    summary = result_text(verdict.get("summary", "Reviewer rejected the patch."), 700)
    findings = [
        result_text(_finding_requirement(item), 240)
        for item in verdict.get("findings", [])
        if _finding_requirement(item).strip()
    ]
    if findings:
        summary += " Findings: " + "; ".join(findings[:3])
    return "Reviewer rejected the patch: " + (summary or "no summary provided")


def local_resume_task_file(issue: Issue, folder: Path) -> Path:
    """Materialize inline local-task text behind a safe resume path."""
    path = folder / "resume-task.md"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Cannot preserve local task for resume: {path}") from exc
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"Cannot preserve local task for resume: {path}")
    if not path.exists():
        try:
            atomic_write_text(path, issue.body)
        except OSError as exc:
            raise RuntimeError(f"Cannot preserve local task for resume: {path}") from exc
    return path


def resume_action(issue: Issue, folder: Path) -> str:
    if issue.is_audit:
        target = shlex.join(["python", "agentic.py", "audit", "--resume"])
    elif issue.is_local:
        task_path = local_resume_task_file(issue, folder)
        target = shlex.join([
            "python", "agentic.py", "--task-file", str(task_path.resolve()),
        ])
    else:
        target = shlex.join(["python", "agentic.py", "--issue", str(issue.number)])
    return f"inspect the run logs at {folder}, modify the worktree, then {target}"


def record_result(folder: Path, status: str, *, attempts: int,
                  max_retries: int, reason: str, worktree: Path,
                  next_action: str) -> None:
    """Persist the latest user-facing terminal result for the dashboard."""
    atomic_write_json(_result_path(folder), {
        "status": status,
        "attempts": attempts,
        "max_retries": max_retries,
        "reason": result_text(reason),
        "worktree": str(worktree),
        "logs_path": str(folder),
        "next_action": next_action,
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


def clear_result(folder: Path) -> None:
    _result_path(folder).unlink(missing_ok=True)


def load_blocked(folder: Path) -> dict[str, Any] | None:
    try:
        raw = read_text_bounded(_blocked_path(folder), MAX_DURABLE_JSON_BYTES)
        if raw is None:
            return None
        value = json.loads(redact_secrets(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or value.get("status") != "blocked":
        return None
    return value if isinstance(value.get("patch_digest"), str) else None


def record_blocked(folder: Path, patch_digest: str, findings: list[str], *,
                   attempts: int | None = None, max_retries: int | None = None,
                   summary: str = "", issue_digest: str = "") -> None:
    safe_findings = [
        bounded for item in findings
        if (bounded := result_text(item, 1_000))
    ]
    atomic_write_json(_blocked_path(folder), {
        "status": "blocked",
        "result_status": "BLOCKED",
        "reason": "Repeated review blocker with no patch change",
        "summary": result_text(summary or (findings[0] if findings else "")),
        "patch_digest": patch_digest,
        "issue_digest": issue_digest,
        "findings": safe_findings,
        "attempts": attempts,
        "max_retries": max_retries,
        "logs_path": str(folder),
        "blocked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


def record_no_code_changes_blocked(folder: Path, developer_report: str, *,
                                   attempts: int | None = None,
                                   max_retries: int | None = None,
                                   issue_digest: str = "") -> None:
    developer_log = latest_numbered_log(folder, "developer", "txt")
    atomic_write_json(_blocked_path(folder), {
        "status": "blocked",
        "result_status": "NO_CHANGES",
        "kind": "no-code-changes",
        "reason": "Developer reported a blocker without producing code changes",
        "patch_digest": "",
        "issue_digest": issue_digest,
        "developer_summary": result_text(developer_report, 2_000)
        or "No developer blocker summary was provided.",
        "developer_log": developer_log.name if developer_log else "",
        "attempts": attempts,
        "max_retries": max_retries,
        "logs_path": str(folder),
        "blocked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


def record_test_failure_blocked(folder: Path, failure_digest: str, report: str, *,
                                patch_digest: str = "",
                                attempts: int | None = None,
                                max_retries: int | None = None,
                                issue_digest: str = "") -> None:
    """Persist a terminal blocker caused by the same test failure repeating."""
    diagnostic = result_text(report, 2_000)
    atomic_write_json(_blocked_path(folder), {
        "status": "blocked",
        "result_status": "BLOCKED",
        "kind": "test-failure",
        "reason": "Repeated identical test failure",
        "summary": (
            "Repeated identical test failure. "
            f"Diagnostic: {diagnostic}"
            if diagnostic else "Repeated identical test failure; see the test logs."
        ),
        "patch_digest": patch_digest,
        "issue_digest": issue_digest,
        "failure_digest": failure_digest,
        "attempts": attempts,
        "max_retries": max_retries,
        "logs_path": str(folder),
        "blocked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


def clear_blocked(folder: Path) -> None:
    _blocked_path(folder).unlink(missing_ok=True)


def _normalize_approval(value: dict[str, Any], path: Path, label: str) -> dict[str, Any]:
    """Normalize optional reviewer evidence before it reaches callers."""
    if "summary" in value and not isinstance(value["summary"], str):
        raise RuntimeError(f"{label} is invalid: {path}")
    skipped = value.get("skipped")
    if skipped is not None and (
        not isinstance(skipped, list)
        or any(not isinstance(item, str) for item in skipped)
    ):
        raise RuntimeError(f"{label} is invalid: {path}")
    if "summary" in value:
        value["summary"] = result_text(value["summary"], 2_000)
    if skipped is not None:
        value["skipped"] = [
            bounded for item in skipped
            if (bounded := result_text(item, 1_000))
        ]
    return value


def _load_approval(path: Path, required: tuple[str, ...], label: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = read_text_bounded(path, MAX_DURABLE_JSON_BYTES)
        if raw is None:
            raise ValueError("missing, unreadable, or oversized artifact")
        value = json.loads(redact_secrets(raw))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("status") != "approved":
        raise RuntimeError(f"{label} is invalid: {path}")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
        raise RuntimeError(f"{label} is incomplete: {path}")
    return _normalize_approval(value, path, label)


def load_manual_approval(folder: Path) -> dict[str, Any] | None:
    """Load a human completion record, rejecting corrupt approval state."""
    return _load_approval(
        manual_approval_path(folder),
        ("branch", "issue_digest", "base_sha", "patch_digest", "reason"),
        "Manual completion record",
    )


def load_review_approval(folder: Path) -> dict[str, Any] | None:
    """Load an automated review record, rejecting corrupt approval state."""
    return _load_approval(
        review_approval_path(folder),
        ("branch", "issue_digest", "base_sha", "patch_digest"),
        "Automated review approval",
    )


def load_publication_approval(
    folder: Path, *,
    manual_loader: Callable[[Path], dict[str, Any] | None] | None = None,
    review_loader: Callable[[Path], dict[str, Any] | None] | None = None,
) -> dict[str, Any] | None:
    """Load the newest human or automated approval available for publication."""
    candidates: list[tuple[float, dict[str, Any]]] = []
    manual_loader = manual_loader or load_manual_approval
    review_loader = review_loader or load_review_approval
    manual_path = manual_approval_path(folder)
    manual = manual_loader(folder)
    if manual is not None:
        manual["kind"] = "manual"
        candidates.append((manual_path.stat().st_mtime, manual))
    review_path = review_approval_path(folder)
    review_approval = review_loader(folder)
    if review_approval is not None:
        candidates.append((review_path.stat().st_mtime, review_approval))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def invalidate_publication_approvals(folder: Path, *, previous_digest: str = "",
                                     current_digest: str = "",
                                     reason: str = "Issue requirements changed") -> list[str]:
    """Remove approvals made for an earlier issue and preserve the evidence."""
    invalidated: list[dict[str, Any]] = []
    for kind, path in (
        ("manual", manual_approval_path(folder)),
        ("automated", review_approval_path(folder)),
    ):
        if not path.is_file():
            continue
        entry: dict[str, Any] = {"kind": kind, "file": path.name}
        try:
            raw = read_text_bounded(path, MAX_DURABLE_JSON_BYTES)
            value = json.loads(redact_secrets(raw)) if raw is not None else None
        except (OSError, TypeError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict):
            for key in ("issue_digest", "patch_digest", "branch", "marked_at", "approved_at"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    entry[key] = result_text(candidate, 256)
        invalidated.append(entry)

    if not invalidated:
        return []
    event = {
        "event": "publication_approvals_invalidated",
        "reason": result_text(reason, 500),
        "previous_issue_digest": result_text(previous_digest, 128),
        "current_issue_digest": result_text(current_digest, 128),
        "approvals": invalidated,
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        raw_events = read_text_bounded(approval_events_path(folder), MAX_DURABLE_JSON_BYTES)
        existing = json.loads(redact_secrets(raw_events)) if raw_events is not None else []
    except (OSError, TypeError, json.JSONDecodeError):
        existing = []
    events = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    events.append(event)
    atomic_write_json(approval_events_path(folder), events[-MAX_APPROVAL_EVENTS:])
    for item in invalidated:
        filename = item.get("file")
        if filename == MANUAL_APPROVAL_FILENAME:
            manual_approval_path(folder).unlink(missing_ok=True)
        elif filename == REVIEW_APPROVAL_FILENAME:
            review_approval_path(folder).unlink(missing_ok=True)
    return [str(item["kind"]) for item in invalidated]


def record_manual_approval(run: Run, issue: Issue, base: str, base_sha: str,
                           patch_digest: str, folder: Path, reason: str) -> dict[str, Any]:
    """Persist a human completion decision tied to the exact current patch."""
    approval = {
        "status": "approved",
        "issue": issue.reference,
        "issue_digest": issue_fingerprint(issue),
        "branch": run.branch,
        "base": base,
        "base_sha": base_sha,
        "patch_digest": patch_digest,
        "reason": result_text(reason, 2_000) or "Manually marked done by user",
        "marked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    atomic_write_json(manual_approval_path(folder), approval)
    return approval


def record_review_approval(run: Run, issue: Issue, base: str, base_sha: str,
                           patch_digest: str, verdict: dict[str, Any],
                           config: dict[str, Any], folder: Path) -> dict[str, Any]:
    approval = {
        "status": "approved",
        "kind": "automated",
        "issue": issue.reference,
        "issue_digest": issue_fingerprint(issue),
        "branch": run.branch,
        "base": base,
        "base_sha": base_sha,
        "patch_digest": patch_digest,
        "summary": result_text(verdict.get("summary", ""), 2_000),
        "skipped": [
            bounded for item in verdict.get("skipped", [])
            if isinstance(item, str) and (bounded := result_text(item, 1_000))
        ],
        "reviewer_provider": config.get("reviewer_agent", {}).get("provider", "codex"),
        "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    atomic_write_json(review_approval_path(folder), approval)
    return approval


__all__ = [
    "clear_blocked", "clear_result", "developer_attempts", "latest_numbered_log",
    "load_blocked", "load_latest_developer_report", "load_manual_approval",
    "load_publication_approval", "load_review_approval",
    "local_resume_task_file", "next_log", "record_blocked", "record_no_code_changes_blocked",
    "record_manual_approval", "record_result", "record_review_approval", "result_text",
    "manual_approval_path", "review_approval_path", "approval_events_path",
    "invalidate_publication_approvals",
    "review_reason", "resume_action",
]
