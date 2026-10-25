"""Concise, idempotent completion comments for GitHub issue runs."""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .models import DEFAULT_GITHUB_HOST, Issue, Run
from .output import strip_ansi
from .persistence import atomic_write_json, read_text_tail
from .security import redact_secrets
from .telemetry import read_issue_history, read_run_usage
from .workflow_state import latest_numbered_log
from .worktrees import command

COMMENT_STATE_FILENAME = "github-comment.json"
MAX_RESULT_BYTES = 32_000
MAX_COMMENT_CHARS = 6_000
MAX_ERROR_CHARS = 500

_SAFE_STATUS = re.compile(r"^[A-Z][A-Z0-9_-]{0,39}$")
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")
_SAFE_RESULT_STATUSES = frozenset({
    "approved",
    "blocked",
    "error",
    "failed",
    "not reached",
    "not run",
    "rejected",
    "skipped",
    "unknown",
    "passed",
})

_STATUS_PRESENTATION = {
    "approved": ("✅", "Approved"),
    "blocked": ("⛔", "Blocked"),
    "error": ("❌", "Error"),
    "failed": ("❌", "Failed"),
    "not reached": ("⏭️", "Not reached"),
    "not run": ("⏭️", "Not run"),
    "rejected": ("❌", "Rejected"),
    "skipped": ("⏭️", "Skipped"),
    "unknown": ("❔", "Unknown"),
    "passed": ("✅", "Passed"),
    "RETRY_EXHAUSTED": ("⚠️", "Retry exhausted"),
    "NO_CHANGES": ("ℹ️", "No changes"),
    "FAILED": ("❌", "Failed"),
    "BLOCKED": ("⛔", "Blocked"),
    "APPROVED": ("✅", "Approved"),
    "INVALID": ("❔", "Unknown"),
}


def _clean(value: Any, limit: int) -> str:
    text = strip_ansi(str(value or ""))
    text = "".join(character for character in text if character in "\n\t" or ord(character) >= 32)
    return redact_secrets(text).strip()[:limit]


def _load_json(path: Path, limit: int) -> dict[str, Any] | None:
    try:
        value = json.loads(redact_secrets(read_text_tail(path, limit)))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _status(result: dict[str, Any]) -> str:
    value = _clean(result.get("status"), 40).upper()
    return value if _SAFE_STATUS.fullmatch(value) else "INVALID"


def _branch(run: Run) -> str:
    value = _clean(run.branch, 120)
    return value if _SAFE_BRANCH.fullmatch(value) else "unknown"


def _number(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return f"{value:,}"
    return "unknown"


def _cost(value: Any) -> str:
    if (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(float(value)) and value >= 0
    ):
        return f"${float(value):.4f}"
    return "unknown"


def _duration(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unknown"
    if not math.isfinite(float(value)) or value < 0:
        return "unknown"
    seconds = int(round(float(value)))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _result_value(result: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            normalized = _clean(value, 40).casefold()
            if normalized in _SAFE_RESULT_STATUSES:
                return normalized
    return None


def _latest_operation(usage: dict[str, Any], role: str) -> dict[str, Any] | None:
    metrics = usage.get("metrics")
    operations = metrics.get("operations") if isinstance(metrics, dict) else None
    if not isinstance(operations, list):
        return None
    for item in reversed(operations):
        if isinstance(item, dict) and item.get("role") == role:
            return item
    return None


def _test_status(result: dict[str, Any], usage: dict[str, Any], terminal: str) -> str:
    explicit = _result_value(result, ("tests", "test_status", "tests_status"))
    if explicit:
        return explicit
    operation = _latest_operation(usage, "tests")
    if isinstance(operation, dict):
        status = operation.get("status")
        if status == "completed":
            return "passed"
        if isinstance(status, str) and status.strip():
            return "failed"
    if terminal == "APPROVED":
        return "passed"
    return "unknown"


def _latest_review(folder: Path) -> dict[str, Any] | None:
    latest = latest_numbered_log(folder, "review", "json")
    if latest is None:
        return None
    return _load_json(latest, 64_000)


def _review_status(result: dict[str, Any], usage: dict[str, Any], folder: Path,
                   terminal: str) -> str:
    explicit = _result_value(result, ("review", "review_status"))
    if explicit:
        return explicit
    verdict = _latest_review(folder)
    if isinstance(verdict, dict):
        if verdict.get("approved") is True:
            return "approved"
        if verdict.get("approved") is False:
            return "rejected"
    if terminal == "APPROVED":
        return "approved"
    if _latest_operation(usage, "reviewer") is not None:
        return "unknown"
    return "not reached"


def _status_label(value: str) -> str:
    icon, label = _STATUS_PRESENTATION.get(value, _STATUS_PRESENTATION["unknown"])
    return f"{icon} {label}"


def _stage_values(usage: dict[str, Any], stage: str) -> tuple[str, str]:
    by_stage = usage.get("by_stage")
    stage_usage = by_stage.get(stage) if isinstance(by_stage, dict) else None
    if not isinstance(stage_usage, dict):
        by_role = usage.get("by_role")
        stage_usage = by_role.get(stage) if isinstance(by_role, dict) else None
    if not isinstance(stage_usage, dict):
        stage_usage = {}
    token_value = stage_usage.get("total_tokens")
    if token_value is None and stage == "tests":
        token_value = 0
    return _number(token_value), _cost(stage_usage.get("estimated_cost_usd"))


def _next_action(issue: Issue, terminal: str) -> str:
    if issue.number is None:
        return "inspect the preserved run locally"
    if terminal == "APPROVED":
        return f"run `agentic --issue {issue.number} --publish` to create a draft pull request"
    return f"inspect the run logs and resume with `agentic --issue {issue.number}`"


def _marker(run: Run, issue: Issue, terminal: str) -> str:
    issue_payload = json.dumps(
        [issue.reference, issue.title, issue.body, issue.state, issue.source, issue.audit_key],
        ensure_ascii=False,
    )
    digest = hashlib.sha256(
        f"{run.branch}\0{terminal}\0{issue_payload}".encode("utf-8")
    ).hexdigest()[:20]
    return f"<!-- agentic:completion:{digest} -->"


def format_completion_comment(
    run: Run, issue: Issue, folder: Path, result: dict[str, Any],
) -> tuple[str, str]:
    """Return the deterministic marker and concise public completion body."""
    terminal = _status(result)
    usage = read_run_usage(folder)
    metrics = usage.get("metrics")
    duration = metrics.get("duration_seconds") if isinstance(metrics, dict) else None
    result_label = _status_label(terminal)
    tests_label = _status_label(_test_status(result, usage, terminal))
    review_label = _status_label(_review_status(result, usage, folder, terminal))
    issue_changes = read_issue_history(folder)
    issue_update = []
    if issue_changes:
        latest_change = issue_changes[-1]
        summary = latest_change.get("summary") if isinstance(latest_change, dict) else None
        if isinstance(summary, str) and summary.strip():
            issue_update = ["### Issue update", "", _clean(summary, 500), ""]
    stages = [
        ("Developer", *_stage_values(usage, "developer")),
        ("Tests", *_stage_values(usage, "tests")),
        ("Review", *_stage_values(usage, "reviewer")),
        ("Total", _number(usage.get("total_tokens")), _cost(usage.get("estimated_cost_usd"))),
    ]
    stage_rows = [f"| {label} | {tokens} | {cost} |" for label, tokens, cost in stages]
    heading_icon = _STATUS_PRESENTATION.get(terminal, _STATUS_PRESENTATION["unknown"])[0]
    body = "\n".join([
        _marker(run, issue, terminal),
        f"## {heading_icon} Agentic run completed",
        "",
        *issue_update,
        "| Check | Status |",
        "|---|---|",
        f"| Result | {result_label} |",
        f"| Tests | {tests_label} |",
        f"| Review | {review_label} |",
        f"| Duration | {_duration(duration)} |",
        "",
        "<details>",
        "<summary>Usage and stages</summary>",
        "",
        "| Stage | Tokens | Cost |",
        "|---|---:|---:|",
        *stage_rows,
        "",
        "</details>",
        "",
        "### Next step",
        "",
        _next_action(issue, terminal),
        "",
        "---",
        f"Branch: `{_branch(run)}`",
    ])
    return _marker(run, issue, terminal), _clean(body, MAX_COMMENT_CHARS)


def _github_environment(host: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment["GH_HOST"] = host
    return environment


def _save_state(folder: Path, state: dict[str, Any]) -> bool:
    try:
        atomic_write_json(folder / COMMENT_STATE_FILENAME, state)
    except OSError:
        return False
    return True


def _require_success(result: Any, operation: str) -> None:
    """Treat every nonzero or malformed command result as a delivery failure."""
    returncode = getattr(result, "returncode", None)
    if returncode == 0:
        return
    detail = _clean(
        getattr(result, "stdout", "") or getattr(result, "stderr", ""),
        MAX_ERROR_CHARS,
    )
    suffix = f": {detail}" if detail else ""
    raise RuntimeError(f"GitHub {operation} failed{suffix}")


def post_terminal_comment(
    run: Run, issue: Issue, folder: Path, *, github_repo: str | None,
    github_host: str = DEFAULT_GITHUB_HOST, cwd: Path | None = None,
    offline: bool = False,
    command_runner: Callable[..., Any] | None = None,
) -> bool:
    """Post one final issue comment, returning false only on a GitHub failure."""
    if offline or issue.is_audit or issue.is_local or issue.number is None or not github_repo:
        return True
    result = _load_json(folder / "result.json", MAX_RESULT_BYTES)
    if result is None:
        return True
    marker, body = format_completion_comment(run, issue, folder, result)
    state_path = folder / COMMENT_STATE_FILENAME
    state = _load_json(state_path, 16_000)
    if isinstance(state, dict) and state.get("marker") == marker and state.get("state") == "posted":
        return True
    runner = command_runner or command
    workdir = cwd or run.worktree
    environment = _github_environment(github_host)
    try:
        query = runner([
            "gh", "issue", "view", str(issue.number), "--repo", github_repo,
            "--json", "comments", "--jq",
            f"any(.comments[]?; (.body // \"\") | contains({json.dumps(marker)}))",
        ], workdir, env=environment)
        _require_success(query, "issue comment lookup")
        if str(getattr(query, "stdout", "")).strip().casefold() == "true":
            _save_state(folder, {
                "marker": marker, "status": _status(result), "state": "posted",
                "posted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            return True

        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=folder,
                prefix=".github-comment-", suffix=".md", delete=False,
            ) as stream:
                temporary = stream.name
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            comment = runner([
                "gh", "issue", "comment", str(issue.number), "--repo", github_repo,
                "--body-file", temporary,
            ], workdir, env=environment)
            _require_success(comment, "issue comment post")
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass
        _save_state(folder, {
            "marker": marker, "status": _status(result), "state": "posted",
            "posted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        return True
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        _save_state(folder, {
            "marker": marker, "status": _status(result), "state": "failed",
            "error": _clean(exc, MAX_ERROR_CHARS),
            "failed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        return False


__all__ = ["COMMENT_STATE_FILENAME", "format_completion_comment", "post_terminal_comment"]
