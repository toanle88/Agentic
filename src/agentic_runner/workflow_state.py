"""Durable state helpers for the developer/reviewer workflow.

This module owns bounded run evidence and approval markers.  Workflow
orchestration remains in :mod:`agentic_workflow`; the compatibility facade
re-exports these helpers so older integrations keep their import paths.
"""
from __future__ import annotations

import datetime
import json
import shlex
from pathlib import Path
from typing import Any

from .execution import _short
from .models import Issue, Run
from .output import strip_ansi
from .persistence import atomic_write_json, atomic_write_text
from .security import redact_secrets
from .worktrees import issue_fingerprint


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
    try:
        return redact_secrets(path.read_text(encoding="utf-8", errors="replace"))[-limit:]
    except OSError:
        return ""


def review_approval_path(folder: Path) -> Path:
    return folder / "review-approval.json"


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
        target = shlex.join(["python", "agentic.py", "audit"])
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
        value = json.loads(_blocked_path(folder).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("status") != "blocked":
        return None
    return value if isinstance(value.get("patch_digest"), str) else None


def record_blocked(folder: Path, patch_digest: str, findings: list[str], *,
                   attempts: int | None = None, max_retries: int | None = None,
                   summary: str = "", issue_digest: str = "") -> None:
    atomic_write_json(_blocked_path(folder), {
        "status": "blocked",
        "result_status": "BLOCKED",
        "reason": "Repeated review blocker with no patch change",
        "summary": result_text(summary or (findings[0] if findings else "")),
        "patch_digest": patch_digest,
        "issue_digest": issue_digest,
        "findings": findings,
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


def clear_blocked(folder: Path) -> None:
    _blocked_path(folder).unlink(missing_ok=True)


def _load_approval(path: Path, required: tuple[str, ...]) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Approval record is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("status") != "approved":
        raise RuntimeError(f"Approval record is invalid: {path}")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
        raise RuntimeError(f"Approval record is incomplete: {path}")
    return value


def load_publication_approval(folder: Path) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    manual_path = folder / "manual-approval.json"
    manual = _load_approval(
        manual_path, ("branch", "issue_digest", "base_sha", "patch_digest", "reason"),
    )
    if manual is not None:
        manual["kind"] = "manual"
        candidates.append((manual_path.stat().st_mtime, manual))
    review_path = review_approval_path(folder)
    review_approval = _load_approval(
        review_path, ("branch", "issue_digest", "base_sha", "patch_digest"),
    )
    if review_approval is not None:
        candidates.append((review_path.stat().st_mtime, review_approval))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


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
        "summary": str(verdict.get("summary", "")),
        "skipped": [item for item in verdict.get("skipped", []) if isinstance(item, str)],
        "reviewer_provider": config.get("reviewer_agent", {}).get("provider", "codex"),
        "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    atomic_write_json(review_approval_path(folder), approval)
    return approval


__all__ = [
    "clear_blocked", "clear_result", "developer_attempts", "latest_numbered_log",
    "load_blocked", "load_latest_developer_report", "load_publication_approval",
    "local_resume_task_file", "next_log", "record_blocked", "record_no_code_changes_blocked",
    "record_result", "record_review_approval", "result_text", "review_approval_path",
    "review_reason", "resume_action",
]
