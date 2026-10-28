"""Read-only parsing of durable Agentic run logs.

The dashboard renderer should not know the on-disk schema. This module turns
centralized or legacy run folders into bounded snapshots while
preserving the optional fields used by older run records.
"""
from __future__ import annotations

import csv
import contextvars
import datetime as dt
import hashlib
import heapq
import io
import json
import os
if os.name == "nt":
    import ctypes
    from ctypes import wintypes
import re
import stat
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, NamedTuple

from .review import finding_severity
from .file_boundaries import _is_reparse_or_symbolic_link
from .persistence import read_text_bounded, read_text_prefix, read_text_tail
from .telemetry import read_issue_history, read_run_usage


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
MAX_DASHBOARD_RUNS = 500
MAX_DASHBOARD_CANDIDATE_FOLDERS = 2_000
MAX_DASHBOARD_ENTRIES_PER_RUN = 4_096
_INVENTORY_DIGEST_BITS = 256
_INVENTORY_DIGEST_MASK = (1 << _INVENTORY_DIGEST_BITS) - 1
DASHBOARD_EXCLUDED_FILES = frozenset(
    {"review-schema.json", "investigation-schema.json", "issue-snapshot.json"}
)
MAX_REVIEW_HISTORY_BYTES = 256_000
MAX_REVIEW_HISTORY_ENTRIES = 50
MAX_REVIEW_HISTORY_FINDINGS = 100
MAX_REVIEW_HISTORY_FIELD = 4_000
MAX_DURABLE_JSON_BYTES = 256_000
MAX_EXPORT_ROWS = 500
MAX_EXPORT_BYTES = 256_000
WORKTREE_STALE_DAYS = 30
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
_MAX_SNAPSHOT_RETRIES = 4
_RUN_SNAPSHOT_CACHE: dict[
    tuple[str, str], tuple[tuple[tuple[str, int, int, int | str], ...], dict[str, Any] | None]
] = {}
_RUN_SNAPSHOT_CACHE_LOCK = threading.RLock()
_SNAPSHOT_FILES: contextvars.ContextVar[tuple[Path, ...] | None] = contextvars.ContextVar(
    "agentic_dashboard_snapshot_files", default=None,
)


class _FolderMetadata(NamedTuple):
    activity_ns: int | None
    signature: tuple[tuple[str, int, int, int | str], ...]
    files: tuple[Path, ...]


if os.name == "nt":
    class _WindowsFileBasicInformation(ctypes.Structure):
        _fields_ = [
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("file_attributes", wintypes.DWORD),
        ]


    def _windows_file_change_time(path: str | Path, fallback: int) -> int:
        """Read NTFS ChangeTime without reading the artifact contents."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(
            str(path),
            0x00000080,  # FILE_READ_ATTRIBUTES
            0x00000007,  # share read/write/delete
            None,
            3,  # OPEN_EXISTING
            0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
            None,
        )
        value = getattr(handle, "value", handle)
        if value in (None, 0, ctypes.c_void_p(-1).value):
            return fallback
        try:
            info = _WindowsFileBasicInformation()
            if not kernel32.GetFileInformationByHandleEx(
                handle, 0, ctypes.byref(info), ctypes.sizeof(info),
            ):
                return fallback
            return int(info.change_time)
        finally:
            kernel32.CloseHandle(handle)


def filter_runs(runs: Iterable[dict[str, Any]], *, query: str = "",
                status: str = "all", issue: str = "all", stage: str = "all",
                severity: str = "all", model: str = "all",
                provider: str = "all", repository: str = "all",
                workflow_mode: str = "all", publication_state: str = "all",
                action_required: str = "all", date_from: str = "",
                date_to: str = "") -> list[dict[str, Any]]:
    """Apply the dashboard's global search and run filters to snapshots."""
    needle = str(query).casefold()
    visible: list[dict[str, Any]] = []
    for run in runs:
        issue_value = (
            "audit" if run.get("audit") else
            "local" if run.get("issue") is None else str(run.get("issue"))
        )
        searchable = _searchable_snapshot(run)
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
        if repository != "all" and str(run.get("repository", "unknown")) != repository:
            continue
        if workflow_mode != "all" and run.get("workflow_mode", "unknown") != workflow_mode:
            continue
        if publication_state != "all" and run.get("publication_state", "unknown") != publication_state:
            continue
        if action_required != "all":
            wants_action = action_required in {"yes", "true", "required"}
            if bool(run.get("action_required", _derived_action_required(run))) != wants_action:
                continue
        timestamp = _parse_timestamp(run.get("last_activity"))
        if date_from and (timestamp is None or timestamp.date() < _parse_date(date_from)):
            continue
        if date_to and (timestamp is None or timestamp.date() > _parse_date(date_to)):
            continue
        visible.append(run)
    return visible


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        # Invalid UI input must fail closed for that bound rather than broaden
        # the result set.
        return dt.date.max if value else dt.date.min


def _derived_action_required(run: dict[str, Any]) -> bool:
    status = str(run.get("status", ""))
    tests = _mapping_or_empty(run.get("tests"))
    review = _mapping_or_empty(run.get("review"))
    return bool(
        run.get("blocked")
        or status in {"blocked", "needs-fix", "tests-failed", "retry_exhausted"}
        or tests.get("status") == "failed"
        or review.get("status") == "rejected"
        or run.get("publication_state") == "pending"
        or run.get("worktree_state") == "stale"
    )


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    """Return a mapping for optional legacy fields, or an empty mapping."""
    return value if isinstance(value, dict) else {}


def sort_runs(runs: Iterable[dict[str, Any]], *, sort_by: str = "last_activity",
              descending: bool = True) -> list[dict[str, Any]]:
    """Sort snapshots by a bounded, allowlisted dashboard metric."""
    aliases = {
        "duration_seconds": "duration", "token_usage": "tokens",
        "estimated_cost_usd": "cost", "review_findings": "findings",
    }
    allowed = {"last_activity", "duration", "tokens", "cost", "retries", "findings"}
    field = aliases.get(sort_by, sort_by) if sort_by in (*allowed, *aliases) else "last_activity"

    def value(run: dict[str, Any]) -> tuple[bool, Any]:
        usage = _mapping_or_empty(run.get("usage"))
        workflow = _mapping_or_empty(usage.get("workflow_metrics"))
        metrics = _mapping_or_empty(usage.get("metrics"))
        if field == "last_activity":
            parsed = _parse_timestamp(run.get("last_activity"))
            return parsed is not None, parsed or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        keys = {
            "duration": metrics.get("duration_seconds"),
            "tokens": usage.get("total_tokens"),
            "cost": usage.get("estimated_cost_usd"),
            "retries": workflow.get("retries"),
            "findings": workflow.get("review_findings"),
        }
        raw = keys[field]
        try:
            return raw is not None, float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            return False, 0.0

    decorated = [(run, value(run)) for run in runs]
    known = [item for item in decorated if item[1][0]]
    unknown = [item for item in decorated if not item[1][0]]
    known.sort(key=lambda item: (item[1][1], str(item[0].get("id", ""))), reverse=descending)
    unknown.sort(key=lambda item: str(item[0].get("id", "")))
    return [run for run, _ in known + unknown]


def summary_row(run: dict[str, Any]) -> dict[str, Any]:
    """Return the explicit, bounded allowlist used by dashboard exports."""
    usage = _mapping_or_empty(run.get("usage"))
    metrics = _mapping_or_empty(usage.get("metrics"))
    workflow = _mapping_or_empty(usage.get("workflow_metrics"))
    review = _mapping_or_empty(run.get("review"))
    tests = _mapping_or_empty(run.get("tests"))
    reasons = run.get("action_reasons", [])
    if not isinstance(reasons, list):
        reasons = []
    return {
        "id": str(run.get("id", ""))[:160],
        "repository": str(run.get("repository", "unknown"))[:160],
        "issue": (
            run.get("issue") if isinstance(run.get("issue"), int)
            and not isinstance(run.get("issue"), bool)
            else str(run.get("issue", ""))[:80] if run.get("issue") is not None else None
        ),
        "label": str(run.get("label", "Run"))[:240],
        "status": str(run.get("status", "unknown"))[:40],
        "stage": str(run.get("stage", "unknown"))[:40],
        "severity": str(run.get("severity", "NONE"))[:40],
        "action_required": bool(run.get("action_required", _derived_action_required(run))),
        "action_reasons": [str(item)[:240] for item in reasons[:8]],
        "workflow_mode": str(run.get("workflow_mode", "unknown"))[:40],
        "publication_state": str(run.get("publication_state", "unknown"))[:40],
        "review_status": str(review.get("status", "unknown"))[:40],
        "tests_status": str(tests.get("status", "unknown"))[:40],
        "last_activity": str(run.get("last_activity", "unknown"))[:64],
        "duration_seconds": metrics.get("duration_seconds"),
        "total_tokens": usage.get("total_tokens"),
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
        "retries": workflow.get("retries"),
        "review_findings": workflow.get("review_findings"),
        "first_pass_approval": workflow.get("first_pass_approval"),
        "worktree_state": str(run.get("worktree_state", "unknown"))[:40],
    }


def export_summary_json(runs: Iterable[dict[str, Any]]) -> str:
    rows = [summary_row(run) for run in list(runs)[:MAX_EXPORT_ROWS]]
    for count in range(len(rows), -1, -1):
        payload = json.dumps(rows[:count], ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) <= MAX_EXPORT_BYTES:
            return payload
    return "[]"


def export_summary_csv(runs: Iterable[dict[str, Any]]) -> str:
    values = list(runs)
    fieldnames = list(summary_row({}).keys())
    header_output = io.StringIO(newline="")
    csv.DictWriter(
        header_output, fieldnames=fieldnames, lineterminator="\n",
    ).writeheader()
    chunks = [header_output.getvalue()]
    output_bytes = len(chunks[0].encode("utf-8"))
    for run in values[:MAX_EXPORT_ROWS]:
        row = summary_row(run)
        row["action_reasons"] = "; ".join(row["action_reasons"])
        row_output = io.StringIO(newline="")
        csv.DictWriter(
            row_output, fieldnames=fieldnames, lineterminator="\n",
        ).writerow(row)
        row_text = row_output.getvalue()
        row_bytes = len(row_text.encode("utf-8"))
        if output_bytes + row_bytes > MAX_EXPORT_BYTES:
            break
        chunks.append(row_text)
        output_bytes += row_bytes
    return "".join(chunks)


def _searchable_snapshot(value: Any) -> str:
    """Build filter text without serializing bounded log payloads to JSON."""
    parts: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                visit(key)
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif item is None:
            parts.append("null")
        elif item is True:
            parts.append("true")
        elif item is False:
            parts.append("false")
        else:
            parts.append(str(item))

    visit(value)
    return " ".join(parts).casefold()


def collect_runs_from_roots(logs_roots: Iterable[Path]) -> list[dict[str, Any]]:
    """Merge centralized and legacy logs, preferring the centralized snapshot."""
    merged: dict[str, dict[str, Any]] = {}
    for root in logs_roots:
        for run in collect_runs(root):
            merged.setdefault(run["id"], run)
        if len(merged) > MAX_DASHBOARD_RUNS:
            keep = sorted(
                merged.values(),
                key=lambda run: (run["last_activity"], run["id"]),
                reverse=True,
            )[:MAX_DASHBOARD_RUNS]
            merged = {run["id"]: run for run in keep}
    return sorted(
        list(merged.values())[:MAX_DASHBOARD_RUNS],
        key=lambda run: (run["last_activity"], run["id"]),
        reverse=True,
    )


def _numbered_files(folder: Path, prefixes: Iterable[str]) -> list[Path]:
    wanted = set(prefixes)
    files: list[tuple[int, float, Path]] = []
    if not folder.is_dir():
        return []
    for path in folder.iterdir():
        metadata = _regular_artifact_stat(path)
        if metadata is None:
            continue
        match = NUMBERED_FILE.fullmatch(path.name)
        if not match or match.group("prefix") not in wanted:
            continue
        try:
            modified = metadata.st_mtime
        except (AttributeError, OSError):
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
        metadata = _regular_artifact_stat(path)
        if metadata is None:
            continue
        try:
            modified = metadata.st_mtime
        except (AttributeError, OSError):
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
    paths: list[tuple[Path, os.stat_result]] = []
    for prefix in prefixes:
        for path in index.get(prefix, []):
            metadata = _regular_artifact_stat(path)
            if metadata is not None:
                paths.append((path, metadata))
    paths.sort(key=lambda item: (item[1].st_mtime, item[0].name))
    return [path for path, _ in paths]


def _latest(paths: Iterable[Path]) -> Path | None:
    candidates: list[tuple[Path, os.stat_result]] = []
    for path in paths:
        metadata = _regular_artifact_stat(path)
        if metadata is not None:
            candidates.append((path, metadata))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[1].st_mtime, item[0].name))[0]


def _regular_artifact_stat(path: Path) -> os.stat_result | None:
    """Return metadata only for a single-link regular artifact."""
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return None
    return metadata


def _read(path: Path, limit: int = MAX_TEXT) -> str:
    """Read only a bounded tail from a potentially large durable artifact."""
    metadata = _regular_artifact_stat(path)
    if metadata is None:
        return f"Could not read {path.name}: artifact is not a regular file"
    size = metadata.st_size
    # UTF-8 characters are at most four bytes. The small multiplier lets us
    # retain ``limit`` characters without reading the whole file.
    start = max(0, size - max(limit * 4, 4096))
    text = read_text_tail(path, max(limit * 4, 4096))
    truncated = start > 0
    if len(text) > limit:
        truncated = True
        text = text[-limit:]
    if truncated:
        text = f"[… earlier output omitted; showing the last {limit:,} characters …]\n" + text[-limit:]
    return text


def _timestamp(path: Path) -> str:
    metadata = _regular_artifact_stat(path)
    if metadata is None:
        return "unknown"
    try:
        return dt.datetime.fromtimestamp(metadata.st_mtime).astimezone().isoformat(timespec="seconds")
    except (OSError, ValueError):
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


def _bounded_review_history(path: Path) -> list[Any]:
    """Read review history without allowing an unbounded JSON parse.

    History written by Agentic is limited to 50 entries, so complete legacy
    files under the byte budget retain their existing behavior.  For an
    oversized or partially written file, decode complete leading array items
    only; recurring findings are useful evidence, but are never approval
    state.
    """
    text = read_text_prefix(path, MAX_REVIEW_HISTORY_BYTES)
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != "[":
        return []
    index += 1
    entries: list[Any] = []
    while len(entries) < MAX_REVIEW_HISTORY_ENTRIES:
        while index < len(text) and (text[index].isspace() or text[index] == ","):
            index += 1
        if index >= len(text) or text[index] == "]":
            break
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            break
        entries.append(value)
        index = end
    return entries


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
    """Keep finding text while exposing bounded filter metadata."""
    if not isinstance(values, list):
        values = [values]
    text: list[str] = []
    records: list[dict[str, str]] = []
    for value in values:
        if isinstance(value, dict):
            finding_text = str(value.get("requirement", value.get("text", value.get("message", ""))))
            severity = _finding_severity(value.get("severity", "UNSPECIFIED"), finding_text)
            record = {
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
    history = _bounded_review_history(folder / "review-history.json")
    counts: dict[str, dict[str, Any]] = {}
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            findings = entry.get("findings", [])
            if not isinstance(findings, list):
                continue
            for item in findings[:MAX_REVIEW_HISTORY_FINDINGS]:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("requirement") or item.get("text") or "").strip()[:256]
                if not key:
                    continue
                current = counts.setdefault(key, {
                    "count": 0,
                    "severity": str(item.get("severity", "UNSPECIFIED")),
                    "text": str(item.get("text", ""))[:MAX_REVIEW_HISTORY_FIELD],
                })
                current["count"] += 1
    recurring = sorted(
        (item for item in counts.values() if item["count"] > 1),
        key=lambda item: (-item["count"], item["text"]),
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
    raw = read_text_bounded(folder / "blocked.json", MAX_DURABLE_JSON_BYTES)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) and value.get("status") == "blocked" else None


def _result(folder: Path) -> dict[str, Any] | None:
    raw = read_text_bounded(folder / "result.json", MAX_DURABLE_JSON_BYTES)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    if not isinstance(status, str) or status not in {
        "APPROVED", "RETRY_EXHAUSTED", "BLOCKED", "NO_CHANGES",
    }:
        return None
    return value


def _publication(folder: Path) -> dict[str, Any] | None:
    """Read the optional publication record without treating it as approval."""
    raw = read_text_bounded(folder / "publication.json", MAX_DURABLE_JSON_BYTES)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    status = str(value.get("status", "")).strip().lower()
    if status not in {"pending", "published", "failed", "cancelled"}:
        return None
    return {
        "status": status,
        "kind": str(value.get("kind", ""))[:40],
        "url": str(value.get("url", ""))[:500] if value.get("url") else "",
        "published_at": str(value.get("published_at", ""))[:64],
    }


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
    if _regular_artifact_stat(path) is None:
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
    command_path = _latest(_indexed_files(index, folder, ["test-command"]))
    usage = read_run_usage(folder)
    test_result = usage.get("test_result")
    terminal_status = (
        test_result.get("status")
        if isinstance(test_result, dict) else None
    )
    if terminal_status in {"passed", "failed"}:
        path = command_path or folder / "run.json"
        content = _read(command_path) if command_path is not None else (
            f"Recorded test result: {terminal_status}."
        )
        return {
            "status": terminal_status, "summary": content[-2_000:],
            "file": path.name, "updated": _timestamp(path),
        }, path

    metrics = usage.get("metrics")
    operations = metrics.get("operations", []) if isinstance(metrics, dict) else []
    latest_operation = next(
        (
            operation for operation in reversed(operations)
            if isinstance(operation, dict)
            and operation.get("role") == "tests"
            and str(operation.get("name", "")).startswith("test-command-")
        ),
        None,
    ) if isinstance(operations, list) else None
    if latest_operation is not None:
        exit_code = latest_operation.get("exit_code")
        operation_status = latest_operation.get("status")
        valid_exit_code = isinstance(exit_code, int) and not isinstance(exit_code, bool)
        if operation_status in {"failed", "timeout"}:
            status = "failed"
        elif (valid_exit_code and exit_code == 0) or (
            exit_code is None and operation_status == "completed"
        ):
            status = "passed"
        elif valid_exit_code:
            status = "failed"
        else:
            status = "unknown"
        path = command_path or folder / "run.json"
        content = _read(command_path) if command_path is not None else (
            f"Recorded test command result: {status}."
        )
        return {
            "status": status, "summary": content[-2_000:],
            "file": path.name, "updated": _timestamp(path),
        }, path

    # Older runs have no durable terminal status. Only command logs are valid
    # text evidence; aggregate reports may contain expected fixture failures.
    path = command_path
    if path is None:
        return {"status": "not-run", "summary": "No test result recorded."}, None
    content = _read(path)
    terminal_exits = re.findall(
        r"(?im)^\s*(?:\[TESTS\]\s*)?Finished in .*?\(exit\s+(-?\d+)\)",
        content,
    )
    if terminal_exits:
        status = "passed" if terminal_exits[-1] == "0" else "failed"
        return {
            "status": status, "summary": content[-2_000:],
            "file": path.name, "updated": _timestamp(path),
        }, path
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
    return list(_folder_metadata(folder).files)


def _folder_metadata(folder: Path) -> _FolderMetadata:
    """Collect bounded activity, cache metadata, and artifact paths in one scan."""
    try:
        activity_ns = folder.stat().st_mtime_ns
    except OSError:
        return _FolderMetadata(None, (), ())
    files: list[tuple[int, str, Path]] = []
    signature: list[tuple[int, str, int, int | str]] = []
    # Keep the inventory digest independent of scandir order without retaining
    # one metadata tuple per artifact.
    inventory_sum = 0
    inventory_xor = 0
    regular_count = 0
    truncated = False
    try:
        entries = os.scandir(folder)
        with entries:
            for entry_number, entry in enumerate(entries):
                if entry_number >= MAX_DASHBOARD_ENTRIES_PER_RUN:
                    truncated = True
                    break
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                regular_count += 1
                signature_change_time = (
                    f"{_windows_file_change_time(entry.path, stat.st_ctime_ns)}:"
                    f"{getattr(stat, 'st_ino', 0)}"
                    if os.name == "nt" else stat.st_ctime_ns
                )
                signature_candidate = (
                    stat.st_mtime_ns, entry.name, stat.st_size, signature_change_time,
                )
                inventory_item = (
                    f"{entry.name}\0{stat.st_size}\0{stat.st_mtime_ns}\0"
                    f"{signature_change_time}\n"
                ).encode("utf-8", errors="surrogateescape")
                inventory_value = int.from_bytes(
                    hashlib.blake2b(inventory_item, digest_size=_INVENTORY_DIGEST_BITS // 8).digest(),
                    "big",
                )
                inventory_sum = (inventory_sum + inventory_value) & _INVENTORY_DIGEST_MASK
                inventory_xor ^= inventory_value
                if len(signature) < MAX_FILES_PER_RUN:
                    heapq.heappush(signature, signature_candidate)
                elif signature_candidate > signature[0]:
                    heapq.heapreplace(signature, signature_candidate)
                if entry.name in DASHBOARD_EXCLUDED_FILES:
                    continue
                activity_ns = max(activity_ns, stat.st_mtime_ns)
                file_candidate = (stat.st_mtime_ns, entry.name, folder / entry.name)
                if len(files) < MAX_FILES_PER_RUN:
                    heapq.heappush(files, file_candidate)
                elif file_candidate > files[0]:
                    heapq.heapreplace(files, file_candidate)
    except OSError:
        return _FolderMetadata(activity_ns, (), ())

    bounded_signature = [
        (name, size, modified, change_time)
        for modified, name, size, change_time in sorted(signature, reverse=True)
    ]
    if regular_count > MAX_FILES_PER_RUN or truncated:
        inventory_digest = hashlib.sha256()
        inventory_digest.update(regular_count.to_bytes(8, "big"))
        inventory_digest.update(inventory_sum.to_bytes(_INVENTORY_DIGEST_BITS // 8, "big"))
        inventory_digest.update(inventory_xor.to_bytes(_INVENTORY_DIGEST_BITS // 8, "big"))
        bounded_signature.append((
            "__agentic_inventory__", regular_count, 0,
            inventory_digest.hexdigest()
            + (":scan-truncated" if truncated else ""),
        ))
    bounded_files = tuple(
        path for _, _, path in sorted(files, key=lambda item: (item[0], item[1]), reverse=True)
    )
    return _FolderMetadata(activity_ns, tuple(bounded_signature), bounded_files)


def _folder_activity_ns(folder: Path) -> int | None:
    """Return activity from the bounded folder metadata scan."""
    return _folder_metadata(folder).activity_ns


def _folder_signature(folder: Path) -> tuple[tuple[str, int, int, int | str], ...]:
    """Return cache metadata from the bounded folder metadata scan."""
    return _folder_metadata(folder).signature


def _process_running(folder: Path, index: dict[str, list[Path]] | None = None) -> bool:
    progress = _latest(_indexed_files(index, folder, ["developer", "review", "investigation", "audit", "test-command"]))
    if progress is None:
        return False
    content = _read(progress, 20_000)
    return "Started" in content and "Finished in" not in content


def _run_snapshot(
    folder: Path, all_files: Iterable[Path] | None = None,
) -> dict[str, Any] | None:
    match = RUN_NAME.fullmatch(folder.name)
    if not match:
        return None
    if all_files is None:
        all_files = _SNAPSHOT_FILES.get()
        if all_files is None:
            all_files = _all_log_files(folder)
    all_files = list(all_files)
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
    publication = _publication(folder)
    if publication is None and audit_issue is not None:
        publication = {
            "status": str(audit_issue.get("status", "published")).lower(),
            "kind": "audit_issue", "url": audit_issue.get("url", ""),
        }
    publication_state = publication["status"] if publication else "not-published"
    if publication is None and result is not None and result.get("status") == "APPROVED":
        publication_state = "pending"
    workflow_mode = usage.get("workflow_mode")
    if not isinstance(workflow_mode, str) or not workflow_mode.strip():
        workflow_mode = "unknown"
    repository = usage.get("repository")
    if not isinstance(repository, str) or not repository.strip():
        repository = "unknown"
    action_reasons: list[str] = []
    if blocked is not None or status == "blocked":
        action_reasons.append("blocked run")
    if tests["status"] == "failed":
        action_reasons.append("failed checks")
    if review["status"] == "rejected":
        action_reasons.append("rejected review")
    if result is not None and result.get("status") == "RETRY_EXHAUSTED":
        action_reasons.append("retries exhausted")
    if result is not None and result.get("status") == "NO_CHANGES":
        action_reasons.append("no code changes")
    if publication_state == "pending":
        action_reasons.append("pending publication")
    snapshot: dict[str, Any] = {
        "id": folder.name, "issue": issue_value, "label": label, "branch": branch,
        "audit": audit_key is not None, "audit_key": audit_key,
        "shell_job_id": usage.get("shell_job_id"),
        "status": status, "investigation": investigation, "attempts": developer["attempts"],
        "stage": stage, "severity": severity, "providers": providers, "models": models,
        "repository": repository, "workflow_mode": workflow_mode,
        "publication": publication, "publication_state": publication_state,
        "action_required": bool(action_reasons), "action_reasons": action_reasons,
        "issue_metadata": issue_metadata,
        "issue_changes": read_issue_history(folder),
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
    folders: list[tuple[int, str, Path]] = []
    metadata_by_folder: dict[str, _FolderMetadata] = {}
    candidate_count = 0
    try:
        for folder in logs_root.iterdir():
            if _is_reparse_or_symbolic_link(folder) or not folder.is_dir():
                continue
            candidate_count += 1
            if candidate_count > MAX_DASHBOARD_CANDIDATE_FOLDERS:
                break
            metadata = _folder_metadata(folder)
            activity_ns = metadata.activity_ns
            if activity_ns is None:
                continue
            metadata_by_folder[folder.name] = metadata
            candidate = (activity_ns, folder.name, folder)
            if len(folders) < MAX_DASHBOARD_RUNS:
                heapq.heappush(folders, candidate)
            elif candidate > folders[0]:
                heapq.heapreplace(folders, candidate)
    except OSError:
        return []
    selected_folders = [folder for _, _, folder in sorted(folders, reverse=True)]
    runs: list[dict[str, Any]] = []
    for folder in selected_folders:
        cache_key = (root_key, folder.name)
        metadata = metadata_by_folder.get(folder.name, _FolderMetadata(None, (), ()))
        snapshot: dict[str, Any] | None = None
        for _ in range(_MAX_SNAPSHOT_RETRIES):
            normalized_signature = metadata.signature
            with _RUN_SNAPSHOT_CACHE_LOCK:
                cached = _RUN_SNAPSHOT_CACHE.get(cache_key)
            if cached and cached[0] == normalized_signature:
                snapshot = cached[1]
                break

            snapshot_token = _SNAPSHOT_FILES.set(metadata.files)
            try:
                candidate = _run_snapshot(folder)
            finally:
                _SNAPSHOT_FILES.reset(snapshot_token)
            current_metadata = _folder_metadata(folder)
            current_signature = current_metadata.signature
            with _RUN_SNAPSHOT_CACHE_LOCK:
                current = _RUN_SNAPSHOT_CACHE.get(cache_key)
                if current_signature != normalized_signature:
                    # The candidate was built from a stale view. Use a cache
                    # entry only when it matches the newer signature; an
                    # otherwise stale candidate must never reach the caller.
                    if current and current[0] == current_signature:
                        snapshot = current[1]
                        break
                    metadata = current_metadata
                    continue
                if current and current[0] == current_signature:
                    snapshot = current[1]
                else:
                    snapshot = candidate
                    _RUN_SNAPSHOT_CACHE[cache_key] = (current_signature, candidate)
                break
        else:
            # An actively written run may not have a stable view yet. Return
            # a matching cached snapshot if one exists; otherwise omit this
            # run rather than exposing a candidate known to be stale.
            current_signature = metadata.signature
            with _RUN_SNAPSHOT_CACHE_LOCK:
                current = _RUN_SNAPSHOT_CACHE.get(cache_key)
                if current and current[0] == current_signature:
                    snapshot = current[1]
        if snapshot is not None:
            runs.append(snapshot)
    with _RUN_SNAPSHOT_CACHE_LOCK:
        if len(_RUN_SNAPSHOT_CACHE) > _RUN_CACHE_LIMIT:
            for key in list(_RUN_SNAPSHOT_CACHE)[:len(_RUN_SNAPSHOT_CACHE) - _RUN_CACHE_LIMIT]:
                _RUN_SNAPSHOT_CACHE.pop(key, None)
    return sorted(runs, key=lambda run: (run["last_activity"], run["id"]), reverse=True)
