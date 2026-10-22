"""Pure review-result parsing and finding-history helpers."""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .persistence import atomic_write_json
from .security import redact_secrets


FINDING_SEVERITIES = (
    "BLOCKER", "CRITICAL", "MAJOR", "HIGH", "MEDIUM", "MINOR", "LOW",
)


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


def finding_fingerprint(finding: str) -> str:
    """Create a stable short key for the same review finding across attempts."""
    normalized = re.sub(r"\s+", " ", str(finding or "")).strip().lower()
    normalized = re.sub(r"\[[^\]]+\]", " ", normalized)
    normalized = re.sub(r"\b(?:blocker|critical|major|high|medium|minor|low|p[0-3])\b", " ", normalized)
    normalized = re.sub(r"(?:/|\\)[^\s,:;()]+", " path ", normalized)
    normalized = re.sub(r"\b\d+(?:-\d+)?\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _finding_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("requirement", "text", "description", "action_target"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
    return ""


def _normalize_requirement(value: Any) -> str:
    """Canonicalize requirement text before deriving a durable finding ID."""
    return re.sub(r"\s+", " ", redact_secrets(str(value or ""))).strip()


def _finding_field(value: Any, key: str, default: str = "") -> str:
    """Read a string field from provider-shaped data without leaking ``Any``."""
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return default


def normalize_findings(findings: list[Any]) -> list[dict[str, str]]:
    """Give every finding a stable ID while accepting legacy string output.

    Reviewer-supplied IDs are intentionally ignored. They are untrusted model
    output and can be missing, malformed, or unstable across attempts. IDs are
    derived from the normalized requirement at this workflow boundary so every
    downstream consumer receives the same canonical identity.
    """
    normalized: list[dict[str, str]] = []
    seen: dict[str, int] = {}
    for value in findings:
        text = _normalize_requirement(_finding_text(value))
        if not text:
            continue
        requirement = _normalize_requirement(
            _finding_field(value, "requirement") or text,
        )
        if not requirement:
            continue
        fingerprint = finding_fingerprint(requirement)
        seen[fingerprint] = seen.get(fingerprint, 0) + 1
        suffix = f"-{seen[fingerprint]}" if seen[fingerprint] > 1 else ""
        identifier = f"F-{fingerprint}{suffix}"
        action_target = _normalize_requirement(
            _finding_field(value, "action_target") or requirement,
        )
        normalized.append({
            "id": identifier,
            "severity": finding_severity(text),
            "requirement": redact_secrets(requirement),
            "action_target": redact_secrets(action_target),
            "fingerprint": fingerprint,
            "text": text,
        })
    return normalized


def finding_texts(findings: list[Any]) -> list[str]:
    return [item["text"] for item in normalize_findings(findings)]


def analyze_findings(findings: list[Any]) -> list[dict[str, str]]:
    return normalize_findings(findings)


def record_review_findings(folder: Path, verdict: dict[str, Any]) -> dict[str, Any]:
    """Persist finding history and annotate recurring findings for the next attempt."""
    history_path = folder / "review-history.json"
    try:
        history = json.loads(redact_secrets(history_path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    if not isinstance(history, list):
        history = []
    findings = verdict.get("findings", [])
    details = analyze_findings(findings if isinstance(findings, list) else [])
    previous_counts: dict[str, int] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        for item in entry.get("findings", []):
            if isinstance(item, dict) and isinstance(item.get("fingerprint"), str):
                key = item["fingerprint"]
                previous_counts[key] = previous_counts.get(key, 0) + 1
    repeated = [item for item in details if previous_counts.get(item["fingerprint"], 0) > 0]
    history.append({
        "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "approved": bool(verdict.get("approved")),
        "findings": details,
    })
    atomic_write_json(history_path, history[-50:])
    verdict["_finding_analysis"] = details
    verdict["_repeated_findings"] = repeated
    # Keep the provider-facing result compact and preserve the canonical
    # records separately for resumes, dashboards, and developer actions.
    verdict["finding_records"] = details
    return verdict


def review_history_feedback(folder: Path) -> str:
    path = folder / "review-history.json"
    try:
        history = json.loads(redact_secrets(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return ""
    if not isinstance(history, list) or len(history) < 2:
        return ""
    counts: dict[str, tuple[int, str, str]] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        for item in entry.get("findings", []):
            if not isinstance(item, dict):
                continue
            key = item.get("fingerprint")
            if not isinstance(key, str):
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
    return "Recurring review findings from earlier attempts (prioritize resolving these):\n" + "\n".join(repeated[-8:])
