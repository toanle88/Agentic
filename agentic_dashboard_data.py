"""Read-only parsing of durable Agentic run logs.

The dashboard renderer should not know the on-disk schema. This module turns
centralized or legacy run folders into bounded, redacted snapshots while
preserving the optional fields used by older run records.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Iterable

from agentic_security import redact_secrets
from agentic_telemetry import read_run_usage


RUN_NAME = re.compile(
    r"^(?:agent-issue-(?P<issue>\d+)-(?P<issue_token>[A-Za-z0-9_-]+)"
    r"|agent-task-(?P<task>.+)-(?P<task_token>[a-f0-9]{12}(?:-[a-f0-9]{8})?))$"
)
NUMBERED_FILE = re.compile(r"^(?P<prefix>.+)-(?P<number>\d+)(?P<suffix>\.[^.]+)$")
MAX_TEXT = 5_000
MAX_FILES_PER_RUN = 24


def collect_runs_from_roots(logs_roots: Iterable[Path]) -> list[dict[str, Any]]:
    """Merge centralized and legacy logs, preferring the centralized snapshot."""
    merged: dict[str, dict[str, Any]] = {}
    for root in logs_roots:
        for run in collect_runs(root):
            merged.setdefault(run["id"], run)
    return sorted(
        merged.values(),
        key=lambda run: (run["last_activity"], run["id"]),
        reverse=True,
    )


def _numbered_files(folder: Path, prefixes: Iterable[str]) -> list[Path]:
    wanted = set(prefixes)
    files: list[tuple[int, float, Path]] = []
    if not folder.is_dir():
        return []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        match = NUMBERED_FILE.fullmatch(path.name)
        if not match or match.group("prefix") not in wanted:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0
        files.append((int(match.group("number")), modified, path))
    return [
        path for _, _, path in sorted(
            files, key=lambda item: (item[0], item[1], item[2].name)
        )
    ]


def _latest(paths: Iterable[Path]) -> Path | None:
    candidates = list(paths)
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def _read(path: Path, limit: int = MAX_TEXT) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read {path.name}: {exc}"
    if len(text) > limit:
        text = f"[… earlier output omitted; showing the last {limit:,} characters …]\n" + text[-limit:]
    return redact_secrets(text)


def _timestamp(path: Path) -> str:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return "unknown"


def _file_record(path: Path, kind: str) -> dict[str, str]:
    return {
        "name": path.name,
        "kind": kind,
        "updated": _timestamp(path),
        "content": _read(path),
    }


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse strict JSON or a JSON object wrapped in prose/code fences."""
    candidates = [text.strip()]
    candidates.extend(re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE))
    start = text.find("{")
    if start >= 0:
        candidates.append(text[start:])
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                value, _ = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict):
            return value
    return None


def _review(folder: Path) -> tuple[dict[str, Any], Path | None]:
    path = _latest(
        path for path in _numbered_files(folder, ["review"])
        if path.suffix == ".json"
    )
    history_path = folder / "review-history.json"
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        history = []
    counts: dict[str, dict[str, Any]] = {}
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            for item in entry.get("findings", []):
                if not isinstance(item, dict) or not isinstance(item.get("fingerprint"), str):
                    continue
                key = item["fingerprint"]
                current = counts.setdefault(key, {
                    "fingerprint": key, "count": 0,
                    "severity": str(item.get("severity", "UNSPECIFIED")),
                    "text": str(item.get("text", "")),
                })
                current["count"] += 1
    recurring = sorted(
        (item for item in counts.values() if item["count"] > 1),
        key=lambda item: (-item["count"], item["fingerprint"]),
    )
    if path is None:
        return {
            "status": "not-run", "summary": "No review result recorded.",
            "findings": [], "skipped": [], "recurring": recurring,
        }, None
    raw = _parse_json_object(_read(path, 100_000))
    if raw is None:
        try:
            json.loads(_read(path, 100_000))
        except json.JSONDecodeError as exc:
            error = str(exc)
        else:
            error = "Review output is not a JSON object."
        return {
            "status": "invalid",
            "summary": f"Review output is not valid JSON: {error}",
            "findings": [], "skipped": [], "recurring": recurring,
        }, path
    findings = raw.get("findings", [])
    if not isinstance(findings, list):
        findings = [str(findings)]
    skipped = raw.get("skipped", [])
    if not isinstance(skipped, list):
        skipped = [str(skipped)]
    return {
        "status": "approved" if raw.get("approved") is True else "rejected",
        "summary": str(raw.get("summary", "No review summary.")),
        "findings": [str(item) for item in findings],
        "skipped": [str(item) for item in skipped],
        "recurring": recurring, "file": path.name, "updated": _timestamp(path),
    }, path


def _blocked(folder: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((folder / "blocked.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("status") == "blocked" else None


def _result(folder: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    if not isinstance(status, str) or status not in {
        "RETRY_EXHAUSTED", "BLOCKED", "NO_CHANGES",
    }:
        return None
    return value


def _investigation(folder: Path) -> tuple[dict[str, Any], Path | None]:
    path = _latest(
        path for path in _numbered_files(folder, ["investigation"])
        if path.suffix == ".json"
    )
    if path is None:
        return {
            "state": "not-run", "summary": "No issue investigation recorded.",
            "findings": [],
        }, None
    raw = _parse_json_object(_read(path, 100_000))
    if raw is None:
        return {
            "state": "invalid", "summary": "Investigation output is not valid JSON.",
            "findings": [], "file": path.name, "updated": _timestamp(path),
        }, path
    findings = raw.get("findings", [])
    if not isinstance(raw.get("summary"), str) or not isinstance(findings, list):
        return {
            "state": "invalid",
            "summary": "Investigation output does not contain valid feedback.",
            "findings": [], "file": path.name, "updated": _timestamp(path),
        }, path
    return {
        "state": "recorded", "summary": str(raw.get("summary")),
        "findings": [str(item) for item in findings],
        "file": path.name, "updated": _timestamp(path),
    }, path


def _tests(folder: Path) -> tuple[dict[str, Any], Path | None]:
    path = _latest(_numbered_files(folder, ["test-command", "tests"]))
    if path is None:
        return {"status": "not-run", "summary": "No test result recorded."}, None
    content = _read(path)
    failed = re.search(r"\bFAILED\b|exit [1-9]\b|\[TIMEOUT\]", content, re.IGNORECASE)
    passed = re.search(r"exit 0|\bPASSED\b|All configured checks passed", content, re.IGNORECASE)
    return {
        "status": "failed" if failed else "passed" if passed else "unknown",
        "summary": content[-2_000:], "file": path.name, "updated": _timestamp(path),
    }, path


def _latest_developer(folder: Path) -> tuple[dict[str, Any], list[Path]]:
    outputs = _numbered_files(folder, ["developer"])
    output = _latest(path for path in outputs if path.suffix == ".txt")
    progress = _latest(path for path in outputs if path.suffix == ".log")
    return {
        "attempts": len({path.stem for path in outputs}),
        "summary": _read(output, 3_000) if output else "No developer result recorded.",
        "file": output.name if output else "",
        "updated": _timestamp(output) if output else "unknown",
        "progress": _read(progress, 3_000) if progress else "",
    }, outputs


def _all_log_files(folder: Path) -> list[Path]:
    files = [
        path for path in folder.iterdir()
        if path.is_file() and path.name not in {"review-schema.json", "investigation-schema.json"}
    ] if folder.is_dir() else []
    return sorted(files, key=lambda path: (path.stat().st_mtime, path.name), reverse=True)


def _process_running(folder: Path) -> bool:
    progress = _latest(_numbered_files(folder, ["developer", "review", "investigation", "test-command"]))
    if progress is None:
        return False
    content = _read(progress, 20_000)
    return "Started" in content and "Finished in" not in content


def _run_snapshot(folder: Path) -> dict[str, Any] | None:
    match = RUN_NAME.fullmatch(folder.name)
    if not match:
        return None
    investigation, investigation_path = _investigation(folder)
    review, review_path = _review(folder)
    blocked = _blocked(folder)
    result = _result(folder)
    legacy_result_status = blocked.get("result_status") if blocked else None
    if blocked is not None and legacy_result_status is None:
        if blocked.get("kind") == "no-code-changes":
            legacy_result_status = "NO_CHANGES"
    if result is None and blocked is not None and legacy_result_status in {
        "BLOCKED", "NO_CHANGES",
    }:
        result = {
            key: blocked[key] for key in (
                "result_status", "attempts", "max_retries", "summary",
                "reason", "logs_path",
            ) if key in blocked
        }
        result["status"] = legacy_result_status
        if result["status"] == "NO_CHANGES":
            result["reason"] = blocked.get("developer_summary") or result.get("reason", "")
    tests, tests_path = _tests(folder)
    developer, developer_paths = _latest_developer(folder)
    all_files = _all_log_files(folder)
    last_activity = _latest(all_files)
    if _process_running(folder):
        status = "in-progress"
    elif investigation["state"] == "invalid" and not developer_paths and review["status"] == "not-run":
        status = "investigation-invalid"
    elif investigation["state"] == "recorded" and not developer_paths and review["status"] == "not-run":
        status = "awaiting-development"
    elif result is not None:
        status = str(result["status"]).lower()
    elif blocked is not None:
        status = "blocked"
    elif review["status"] == "approved":
        status = "approved"
    elif review["status"] == "rejected":
        status = "needs-fix"
    elif tests["status"] == "failed":
        status = "tests-failed"
    elif all_files:
        status = "awaiting-review"
    else:
        status = "no-activity"
    file_records = [
        _file_record(path, path.name.split("-", 1)[0])
        for path in all_files[:MAX_FILES_PER_RUN]
    ]
    issue = match.group("issue")
    task = match.group("task")
    usage = read_run_usage(folder)
    if issue is not None:
        label = f"Issue #{issue}"
        branch = f"agent/issue-{issue}-{match.group('issue_token')}"
        issue_value: int | None = int(issue)
    else:
        label = f"Local task: {task}"
        branch = f"agent/task-{task}-{match.group('task_token')}"
        issue_value = None
    snapshot: dict[str, Any] = {
        "id": folder.name, "issue": issue_value, "label": label, "branch": branch,
        "status": status, "investigation": investigation, "attempts": developer["attempts"],
        "last_activity": _timestamp(last_activity) if last_activity else "unknown",
        "review": review, "blocked": blocked, "result": result, "tests": tests,
        "developer": developer,
        "files": file_records, "file_count": len(all_files),
        "has_review": review_path is not None, "has_investigation": investigation_path is not None,
        "has_tests": tests_path is not None, "has_developer": bool(developer_paths),
        "usage": usage,
    }
    if usage.get("instruction") is not None:
        snapshot["instruction"] = usage["instruction"]
    return snapshot


def collect_runs(logs_root: Path) -> list[dict[str, Any]]:
    if not logs_root.is_dir():
        return []
    runs = [
        snapshot for folder in logs_root.iterdir() if folder.is_dir()
        for snapshot in [_run_snapshot(folder)] if snapshot is not None
    ]
    return sorted(runs, key=lambda run: (run["last_activity"], run["id"]), reverse=True)
