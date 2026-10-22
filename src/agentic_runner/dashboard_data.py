"""Read-only parsing of durable Agentic run logs.

The dashboard renderer should not know the on-disk schema. This module turns
centralized or legacy run folders into bounded snapshots while
preserving the optional fields used by older run records.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .review import finding_severity
from .telemetry import read_run_usage


RUN_NAME = re.compile(
    r"^(?:agent-issue-(?P<issue>\d+)-(?P<issue_token>[A-Za-z0-9_-]+)"
    r"|agent-task-(?P<task>.+)-(?P<task_token>[a-f0-9]{12}"
    r"(?:-\d{8}T\d{6}(?:\d{6})?Z(?:-[a-f0-9]{8})?|-[a-f0-9]{8})?)"
    r"|agent-audit-(?P<audit_token>[a-f0-9]{12})"
    r"(?P<audit_suffix>(?:-\d{8}T\d{6}(?:\d{6})?Z(?:-[a-f0-9]{8})?)|(?:-[a-f0-9]{8}))?)$"
)
NUMBERED_FILE = re.compile(r"^(?P<prefix>.+)-(?P<number>\d+)(?P<suffix>\.[^.]+)$")
MAX_TEXT = 5_000
MAX_FILES_PER_RUN = 24
SEVERITY_RANK = {
    "BLOCKER": 7,
    "CRITICAL": 6,
    "MAJOR": 5,
    "HIGH": 4,
    "MEDIUM": 3,
    "MINOR": 2,
    "LOW": 1,
}
_RUN_CACHE_LIMIT = 512
_RUN_SNAPSHOT_CACHE: dict[tuple[str, str], tuple[tuple[tuple[str, int, int], ...], dict[str, Any] | None]] = {}


def filter_runs(runs: Iterable[dict[str, Any]], *, query: str = "",
                status: str = "all", issue: str = "all", stage: str = "all",
                severity: str = "all", model: str = "all",
                provider: str = "all") -> list[dict[str, Any]]:
    """Apply the dashboard's global search and run filters to snapshots."""
    needle = str(query).casefold()
    visible: list[dict[str, Any]] = []
    for run in runs:
        issue_value = (
            "audit" if run.get("audit") else
            "local" if run.get("issue") is None else str(run.get("issue"))
        )
        searchable = json.dumps(run, ensure_ascii=False, sort_keys=True).casefold()
        if needle and needle not in searchable:
            continue
        if status != "all" and run.get("status") != status:
            continue
        if issue != "all" and issue_value != issue:
            continue
        if stage != "all" and run.get("stage") != stage:
            continue
        if severity != "all" and run.get("severity") != severity:
            continue
        if model != "all" and model not in run.get("models", []):
            continue
        if provider != "all" and provider not in run.get("providers", []):
            continue
        visible.append(run)
    return visible


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


def _numbered_file_index(paths: Iterable[Path]) -> dict[str, list[Path]]:
    """Index numbered artifacts once per run snapshot."""
    indexed: dict[str, list[tuple[int, float, Path]]] = defaultdict(list)
    for path in paths:
        match = NUMBERED_FILE.fullmatch(path.name)
        if not match:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0
        indexed[match.group("prefix")].append((int(match.group("number")), modified, path))
    return {
        prefix: [path for _, _, path in sorted(values, key=lambda item: (item[0], item[1], item[2].name))]
        for prefix, values in indexed.items()
    }


def _indexed_files(index: dict[str, list[Path]] | None, folder: Path,
                   prefixes: Iterable[str]) -> list[Path]:
    if index is None:
        return _numbered_files(folder, prefixes)
    paths: list[Path] = []
    for prefix in prefixes:
        paths.extend(index.get(prefix, []))
    return sorted(paths, key=lambda path: (path.stat().st_mtime, path.name))


def _latest(paths: Iterable[Path]) -> Path | None:
    candidates = list(paths)
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def _read(path: Path, limit: int = MAX_TEXT) -> str:
    """Read only a bounded tail from a potentially large durable artifact."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            # UTF-8 characters are at most four bytes. The small multiplier
            # lets us retain ``limit`` characters without reading the whole
            # file, while still keeping memory bounded for hostile logs.
            start = max(0, size - max(limit * 4, 4096))
            stream.seek(start)
            text = stream.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read {path.name}: {exc}"
    truncated = start > 0
    if len(text) > limit:
        truncated = True
        text = text[-limit:]
    if truncated:
        text = f"[… earlier output omitted; showing the last {limit:,} characters …]\n" + text[-limit:]
    return text


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


def _finding_severity(value: Any, finding_text: str) -> str:
    severity = str(value).strip().upper()
    if severity in SEVERITY_RANK:
        return severity
    if severity not in {"", "UNSPECIFIED"}:
        explicit = finding_severity(severity)
        if explicit != "UNSPECIFIED":
            return explicit
    return finding_severity(finding_text)


def _finding_records(values: Any) -> tuple[list[str], list[dict[str, str]]]:
    """Keep legacy finding text while exposing bounded filter metadata."""
    if not isinstance(values, list):
        values = [values]
    text: list[str] = []
    records: list[dict[str, str]] = []
    for value in values:
        if isinstance(value, dict):
            finding_text = str(value.get("requirement", value.get("text", value.get("message", ""))))
            severity = _finding_severity(value.get("severity", "UNSPECIFIED"), finding_text)
            record = {
                "id": str(value.get("id", "")),
                "text": finding_text,
                "severity": severity,
            }
        else:
            finding_text = str(value)
            severity = _finding_severity("UNSPECIFIED", finding_text)
            record = {"text": finding_text, "severity": severity}
        text.append(finding_text)
        records.append(record)
    return text, records


def _review(folder: Path, index: dict[str, list[Path]] | None = None) -> tuple[dict[str, Any], Path | None]:
    path = _latest(
        path for path in _indexed_files(index, folder, ["review"])
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
            "findings": [], "finding_records": [], "skipped": [], "recurring": recurring,
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
            "findings": [], "finding_records": [], "skipped": [], "recurring": recurring,
        }, path
    findings, finding_records = _finding_records(raw.get("findings", []))
    skipped = raw.get("skipped", [])
    if not isinstance(skipped, list):
        skipped = [str(skipped)]
    return {
        "status": "approved" if raw.get("approved") is True else "rejected",
        "summary": str(raw.get("summary", "No review summary.")),
        "findings": findings, "finding_records": finding_records,
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


def _investigation(folder: Path, index: dict[str, list[Path]] | None = None) -> tuple[dict[str, Any], Path | None]:
    path = _latest(
        path for path in _indexed_files(index, folder, ["investigation"])
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


def _audit(folder: Path) -> tuple[dict[str, Any], Path | None]:
    path = folder / "audit-report.json"
    if not path.is_file():
        return {"state": "not-run", "summary": "No repository audit recorded.", "findings": [], "measurements": [], "skipped": []}, None
    raw = _parse_json_object(_read(path, 100_000))
    if raw is None or not isinstance(raw.get("summary"), str) or not isinstance(raw.get("findings"), list):
        return {"state": "invalid", "summary": "Audit report is not valid JSON.", "findings": [], "measurements": [], "skipped": [], "file": path.name, "updated": _timestamp(path)}, path
    findings = [item for item in raw["findings"] if isinstance(item, dict)][:40]
    return {
        "state": "recorded",
        "summary": str(raw.get("summary", ""))[:2_000],
        "findings": findings,
        "measurements": [item for item in raw.get("measurements", []) if isinstance(item, dict)][:20],
        "skipped": [str(item) for item in raw.get("skipped", []) if isinstance(item, str)][:20],
        "file": path.name,
        "updated": _timestamp(path),
    }, path


def _tests(folder: Path, index: dict[str, list[Path]] | None = None) -> tuple[dict[str, Any], Path | None]:
    path = _latest(_indexed_files(index, folder, ["test-command", "tests"]))
    if path is None:
        return {"status": "not-run", "summary": "No test result recorded."}, None
    content = _read(path)
    failed = re.search(r"\bFAILED\b|exit [1-9]\b|\[TIMEOUT\]", content, re.IGNORECASE)
    passed = re.search(r"exit 0|\bPASSED\b|All configured checks passed", content, re.IGNORECASE)
    return {
        "status": "failed" if failed else "passed" if passed else "unknown",
        "summary": content[-2_000:], "file": path.name, "updated": _timestamp(path),
    }, path


def _latest_developer(folder: Path, index: dict[str, list[Path]] | None = None) -> tuple[dict[str, Any], list[Path]]:
    outputs = _indexed_files(index, folder, ["developer"])
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
        if path.is_file() and path.name not in {
            "review-schema.json", "investigation-schema.json", "issue-snapshot.json",
        }
    ] if folder.is_dir() else []
    return sorted(files, key=lambda path: (path.stat().st_mtime, path.name), reverse=True)


def _process_running(folder: Path, index: dict[str, list[Path]] | None = None) -> bool:
    progress = _latest(_indexed_files(index, folder, ["developer", "review", "investigation", "audit", "test-command"]))
    if progress is None:
        return False
    content = _read(progress, 20_000)
    return "Started" in content and "Finished in" not in content


def _run_snapshot(folder: Path) -> dict[str, Any] | None:
    match = RUN_NAME.fullmatch(folder.name)
    if not match:
        return None
    all_files = _all_log_files(folder)
    index = _numbered_file_index(all_files)
    investigation, investigation_path = _investigation(folder, index)
    audit, audit_path = _audit(folder)
    review, review_path = _review(folder, index)
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
    tests, tests_path = _tests(folder, index)
    developer, developer_paths = _latest_developer(folder, index)
    last_activity = _latest(all_files)
    if _process_running(folder, index):
        status = "in-progress"
    elif audit["state"] == "invalid" and not developer_paths and review["status"] == "not-run":
        status = "audit-invalid"
    elif audit["state"] == "recorded" and not developer_paths and review["status"] == "not-run":
        status = "audit-complete"
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
    invocations = usage.get("by_invocation", [])
    if not isinstance(invocations, list):
        invocations = []
    providers = sorted({
        str(item.get("provider")) for item in invocations
        if isinstance(item, dict) and item.get("provider")
    } | ({str(usage["provider"])} if usage.get("provider") else set()))
    models = sorted({
        str(item.get("model")) for item in invocations
        if isinstance(item, dict) and item.get("model")
    })
    severity_values = [
        item.get("severity", "UNSPECIFIED")
        for item in review.get("finding_records", [])
        if isinstance(item, dict)
    ] + [
        item.get("severity", "UNSPECIFIED")
        for item in review.get("recurring", [])
        if isinstance(item, dict)
    ]
    severity_values += [
        item.get("severity", "INFO")
        for item in audit.get("findings", []) if isinstance(item, dict)
    ]
    severity = max(
        (str(value).upper() for value in severity_values),
        key=lambda value: SEVERITY_RANK.get(value, 0),
        default="NONE",
    )
    if result is not None:
        stage = "blocked" if result.get("status") == "BLOCKED" else "completed"
    elif blocked is not None:
        stage = "blocked"
    elif review["status"] in {"approved", "rejected", "invalid"}:
        stage = "review"
    elif tests["status"] in {"passed", "failed", "unknown"}:
        stage = "tests"
    elif developer_paths or status == "in-progress":
        stage = "development"
    elif audit["state"] != "not-run":
        stage = "audit"
    elif investigation["state"] != "not-run":
        stage = "investigation"
    else:
        stage = "idle"
    audit_key = match.group("audit_token")
    audit_suffix = match.group("audit_suffix") or ""
    audit_issue: dict[str, Any] | None = None
    if audit_key is not None:
        raw_audit_issue = _parse_json_object(_read(folder / "audit-issue.json", 10_000))
        if isinstance(raw_audit_issue, dict) and isinstance(raw_audit_issue.get("url"), str):
            audit_issue = {
                "status": str(raw_audit_issue.get("status") or "published"),
                "url": raw_audit_issue["url"],
            }
    if audit_key is not None:
        label = "Repository audit"
        branch = f"agent/audit-{audit_key}{audit_suffix}"
        issue_value: int | None = None
    elif issue is not None:
        label = f"Issue #{issue}"
        branch = f"agent/issue-{issue}-{match.group('issue_token')}"
        issue_value: int | None = int(issue)
    else:
        label = f"Local task: {task}"
        branch = f"agent/task-{task}-{match.group('task_token')}"
        issue_value = None
    raw_issue_metadata = usage.get("issue_metadata")
    if isinstance(raw_issue_metadata, dict) and issue_value is not None:
        issue_metadata: dict[str, Any] = {
            "number": issue_value,
            "title": str(raw_issue_metadata.get("title") or "Title unavailable"),
            "state": str(raw_issue_metadata.get("state") or "UNKNOWN").upper(),
            "source": str(raw_issue_metadata.get("source") or "github"),
        }
    elif audit_key is not None:
        issue_metadata = {
            "number": None, "title": "Repository audit", "state": "AUDIT",
            "source": "audit", "audit_key": audit_key,
        }
    elif issue_value is None:
        issue_metadata = {
            "number": None, "title": task, "state": "LOCAL", "source": "local",
        }
    else:
        issue_metadata = {
            "number": issue_value, "title": "Title unavailable",
            "state": "UNKNOWN", "source": "github",
        }
    snapshot: dict[str, Any] = {
        "id": folder.name, "issue": issue_value, "label": label, "branch": branch,
        "audit": audit_key is not None, "audit_key": audit_key,
        "status": status, "investigation": investigation, "attempts": developer["attempts"],
        "stage": stage, "severity": severity, "providers": providers, "models": models,
        "issue_metadata": issue_metadata,
        "last_activity": _timestamp(last_activity) if last_activity else "unknown",
        "review": review, "blocked": blocked, "result": result, "tests": tests,
        "audit_report": audit,
        "audit_issue": audit_issue,
        "developer": developer,
        "files": file_records, "file_count": len(all_files),
        "has_review": review_path is not None, "has_investigation": investigation_path is not None,
        "has_audit": audit_path is not None,
        "has_tests": tests_path is not None, "has_developer": bool(developer_paths),
        "usage": usage,
    }
    if usage.get("instruction") is not None:
        snapshot["instruction"] = usage["instruction"]
    return snapshot


def collect_runs(logs_root: Path) -> list[dict[str, Any]]:
    if not logs_root.is_dir():
        return []
    root_key = str(logs_root.resolve())
    runs: list[dict[str, Any]] = []
    for folder in logs_root.iterdir():
        if not folder.is_dir():
            continue
        signature: list[tuple[str, int, int]] = []
        try:
            for path in folder.iterdir():
                if not path.is_file() or path.is_symlink():
                    continue
                stat = path.stat()
                signature.append((path.name, stat.st_size, stat.st_mtime_ns))
        except OSError:
            signature = []
        cache_key = (root_key, folder.name)
        cached = _RUN_SNAPSHOT_CACHE.get(cache_key)
        snapshot = cached[1] if cached and cached[0] == tuple(sorted(signature)) else _run_snapshot(folder)
        if snapshot is not None:
            runs.append(snapshot)
            _RUN_SNAPSHOT_CACHE[cache_key] = (tuple(sorted(signature)), snapshot)
    if len(_RUN_SNAPSHOT_CACHE) > _RUN_CACHE_LIMIT:
        for key in list(_RUN_SNAPSHOT_CACHE)[:len(_RUN_SNAPSHOT_CACHE) - _RUN_CACHE_LIMIT]:
            _RUN_SNAPSHOT_CACHE.pop(key, None)
    return sorted(runs, key=lambda run: (run["last_activity"], run["id"]), reverse=True)
