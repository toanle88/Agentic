"""Pure review-result parsing and finding-history helpers."""
from __future__ import annotations

from collections import deque
import datetime
import json
from pathlib import Path
import re
from typing import Any

from .persistence import atomic_write_json
from .security import redact_secrets


FINDING_SEVERITIES = (
    "BLOCKER", "CRITICAL", "MAJOR", "HIGH", "MEDIUM", "MINOR", "LOW",
)
MAX_REVIEW_HISTORY_BYTES = 256_000
MAX_REVIEW_HISTORY_ENTRIES = 50
MAX_REVIEW_HISTORY_FINDINGS = 100
MAX_REVIEW_HISTORY_FIELD = 4_000
MAX_REVIEW_FEEDBACK_CHARS = 9_000


def parse_review_result(text: str) -> dict[str, Any] | None:
    """Parse strict or lightly wrapped JSON returned by a reviewer."""
    candidates = [text.strip()]
    candidates.extend(
        match.group(1)
        for match in re.finditer(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
    )
    start = text.find("{")
    if start >= 0:
        candidates.append(text[start:])
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def finding_severity(finding: str) -> str:
    """Extract a conventional severity prefix while tolerating reviewer prose."""
    upper = finding.upper().strip()
    for severity in FINDING_SEVERITIES:
        if re.match(rf"^\[?{severity}\]?\s*(?:[-:—]|\b)", upper):
            return severity
    match = re.match(r"^P([0-3])\b", upper)
    if match:
        return {"0": "BLOCKER", "1": "HIGH", "2": "MEDIUM", "3": "LOW"}[match.group(1)]
    return "UNSPECIFIED"


def _finding_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("requirement", "text", "description", "action_target"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
    return ""


def _normalize_requirement(value: Any) -> str:
    """Canonicalize requirement text for bounded review feedback."""
    return re.sub(r"\s+", " ", redact_secrets(str(value or ""))).strip()[:MAX_REVIEW_HISTORY_FIELD]


def _finding_match_text(value: Any) -> str:
    """Build an in-memory comparison key without persisting an identifier."""
    text = _normalize_requirement(value)
    text = re.sub(
        r"^\s*\[?(?:BLOCKER|CRITICAL|MAJOR|HIGH|MEDIUM|MINOR|LOW|P[0-3])\]?\s*(?:[-:—]\s*)?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r":\d+(?:-\d+)?\b", ":<line>", text)
    return text.casefold()


def _finding_field(value: Any, key: str, default: str = "") -> str:
    """Read a string field from provider-shaped data without leaking ``Any``."""
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return default


def normalize_findings(findings: list[Any]) -> list[dict[str, str]]:
    """Normalize reviewer findings without assigning identifiers."""
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in findings[:MAX_REVIEW_HISTORY_FINDINGS]:
        text = _normalize_requirement(_finding_text(value))
        if not text:
            continue
        requirement = _normalize_requirement(
            _finding_field(value, "requirement") or text,
        )
        if not requirement:
            continue
        action_target = _normalize_requirement(
            _finding_field(value, "action_target") or requirement,
        )
        duplicate_key = (requirement.casefold(), action_target.casefold())
        if duplicate_key in seen:
            continue
        seen.add(duplicate_key)
        normalized.append({
            "severity": finding_severity(text),
            "requirement": redact_secrets(requirement),
            "action_target": redact_secrets(action_target),
            "text": text,
        })
    return normalized


def finding_texts(findings: list[Any]) -> list[str]:
    return [item["text"] for item in normalize_findings(findings)]


def analyze_findings(findings: list[Any]) -> list[dict[str, str]]:
    return normalize_findings(findings)


def _bounded_history_entry(value: Any) -> dict[str, Any] | None:
    """Keep a history entry within the same bounds used by its writer."""
    if not isinstance(value, dict):
        return None
    findings = value.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    bounded_findings: list[dict[str, str]] = []
    for item in findings[:MAX_REVIEW_HISTORY_FINDINGS]:
        if not isinstance(item, dict):
            continue
        bounded: dict[str, str] = {}
        for key in ("severity", "requirement", "action_target", "text"):
            candidate = item.get(key)
            if isinstance(candidate, str) and candidate:
                bounded[key] = redact_secrets(candidate)[:MAX_REVIEW_HISTORY_FIELD]
        if bounded:
            bounded_findings.append(bounded)
    return {
        "timestamp": redact_secrets(str(value.get("timestamp", "")))[:80],
        "approved": bool(value.get("approved")),
        "findings": bounded_findings,
    }


def load_review_history(path_or_folder: Path) -> list[dict[str, Any]]:
    """Read review history with byte, entry, and finding bounds.

    Complete current files retain their order.  If an older file is oversized
    or interrupted, decode complete history-shaped objects from its bounded
    tail so the newest recurring findings remain available without reading the
    whole artifact.
    """
    path = path_or_folder if path_or_folder.name == "review-history.json" else path_or_folder / "review-history.json"
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_REVIEW_HISTORY_BYTES + 1)
    except OSError:
        return []
    oversized = len(payload) > MAX_REVIEW_HISTORY_BYTES
    if oversized:
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - MAX_REVIEW_HISTORY_BYTES))
                payload = stream.read(MAX_REVIEW_HISTORY_BYTES)
        except OSError:
            return []
    text = redact_secrets(payload.decode("utf-8", errors="replace"))
    entries: list[Any] = []
    if not oversized:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            entries = parsed
    if oversized or not entries and text.strip():
        decoder = json.JSONDecoder()
        cursor = 0
        decoded_entries: deque[Any] = deque(maxlen=MAX_REVIEW_HISTORY_ENTRIES)
        while True:
            start = text.find("{", cursor)
            if start < 0:
                break
            try:
                value, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                cursor = start + 1
                continue
            cursor = end
            if isinstance(value, dict) and "findings" in value and "timestamp" in value:
                decoded_entries.append(value)
        entries = list(decoded_entries)
    bounded = [item for item in (_bounded_history_entry(entry) for entry in entries) if item is not None]
    return bounded[-MAX_REVIEW_HISTORY_ENTRIES:]


def _bounded_history(entries: list[Any]) -> list[dict[str, Any]]:
    """Retain the newest complete entries that fit the durable byte budget."""
    def serialized_size(value: list[dict[str, Any]]) -> int:
        # Keep this in sync with ``atomic_write_json`` so escaped Unicode is
        # included in the same byte budget as the durable representation.
        return len((json.dumps(value, indent=2) + "\n").encode("utf-8"))

    bounded: list[dict[str, Any]] = []
    for value in reversed(entries[-MAX_REVIEW_HISTORY_ENTRIES:]):
        entry = _bounded_history_entry(value)
        if entry is None:
            continue
        candidate = [entry, *bounded]
        while (
            entry["findings"]
            and serialized_size(candidate) > MAX_REVIEW_HISTORY_BYTES
        ):
            entry["findings"].pop()
        if serialized_size(candidate) > MAX_REVIEW_HISTORY_BYTES:
            continue
        bounded = candidate
    return bounded


def record_review_findings(folder: Path, verdict: dict[str, Any]) -> dict[str, Any]:
    """Persist finding history and annotate recurring findings for the next attempt."""
    history_path = folder / "review-history.json"
    history = load_review_history(history_path)
    findings = verdict.get("findings", [])
    details = analyze_findings(findings if isinstance(findings, list) else [])
    previous_counts: dict[str, int] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        for item in entry.get("findings", []):
            if isinstance(item, dict):
                key = _finding_match_text(item.get("requirement") or item.get("text"))
                if not key:
                    continue
                previous_counts[key] = previous_counts.get(key, 0) + 1
    repeated = [
        item for item in details
        if previous_counts.get(
            _finding_match_text(item.get("requirement") or item.get("text")), 0,
        ) > 0
    ]
    history.append({
        "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "approved": bool(verdict.get("approved")),
        "findings": details,
    })
    atomic_write_json(history_path, _bounded_history(history))
    verdict["_finding_analysis"] = details
    verdict["_repeated_findings"] = repeated
    # Keep the provider-facing result compact and preserve the canonical
    # records separately for resumes, dashboards, and developer actions.
    verdict["finding_records"] = details
    return verdict


def review_history_feedback(folder: Path) -> str:
    history = load_review_history(folder)
    if not isinstance(history, list) or len(history) < 2:
        return ""
    counts: dict[str, tuple[int, str, str]] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        for item in entry.get("findings", []):
            if not isinstance(item, dict):
                continue
            key = _finding_match_text(item.get("requirement") or item.get("text"))
            if not key:
                continue
            count, severity, text = counts.get(
                key, (0, str(item.get("severity", "UNSPECIFIED")), redact_secrets(str(item.get("text", ""))))
            )
            counts[key] = (count + 1, severity, text)
    repeated = [
        f"[{severity}] ({count} reviews) {redact_secrets(text)}"
        for count, severity, text in counts.values()
        if count > 1
    ]
    if not repeated:
        return ""
    feedback = "Recurring review findings from earlier attempts (prioritize resolving these):\n" + "\n".join(repeated[-8:])
    if len(feedback) <= MAX_REVIEW_FEEDBACK_CHARS:
        return feedback
    return "[agentic_review_history_truncated]\n" + feedback[-(MAX_REVIEW_FEEDBACK_CHARS - 36):]
