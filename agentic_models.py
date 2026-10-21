"""Shared immutable data models used by the Agentic runner."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_GITHUB_HOST = "github.com"


@dataclass(frozen=True)
class Issue:
    number: int | None
    title: str
    body: str
    state: str = "OPEN"
    source: str = "github"
    task_key: str | None = None
    source_path: Path | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "Issue":
        return cls(
            number=int(raw["number"]),
            title=str(raw["title"]),
            body=str(raw.get("body") or ""),
            state=str(raw.get("state", "OPEN")),
        )

    @classmethod
    def from_local_text(cls, text: str, source_path: Path | None = None) -> "Issue":
        body = text.strip()
        if not body:
            raise ValueError("Local task text must not be empty")
        first_line = next((line.strip() for line in body.splitlines() if line.strip()), "Local task")
        title = re.sub(r"^#+\s*", "", first_line).strip() or "Local task"
        title = title[:140]
        task_key = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        return cls(None, title, body, source="local", task_key=task_key, source_path=source_path)

    @property
    def is_local(self) -> bool:
        return self.source == "local"

    @property
    def reference(self) -> str:
        if self.is_local:
            return f"LOCAL TASK {self.task_key[:8] if self.task_key else 'unknown'}"
        return f"ISSUE #{self.number}"


@dataclass(frozen=True)
class Run:
    issue_number: int | None
    branch: str
    worktree: Path
    task_key: str | None = None


@dataclass(frozen=True)
class PullRequest:
    """The bounded GitHub identity needed to repair an existing PR."""

    number: int
    title: str
    body: str
    state: str
    base: str
    base_sha: str
    head: str
    head_sha: str
    head_repo: str
    url: str

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "PullRequest":
        base_ref = raw.get("baseRefName") or raw.get("base") or ""
        head_ref = raw.get("headRefName") or raw.get("head") or ""
        head_repo = raw.get("headRepository", {}) or {}
        if isinstance(head_repo, dict):
            head_repo_name = head_repo.get("nameWithOwner") or head_repo.get("fullName") or ""
        else:
            head_repo_name = str(head_repo)
        return cls(
            number=int(raw["number"]),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            state=str(raw.get("state") or ""),
            base=str(base_ref),
            base_sha=str(raw.get("baseRefOid") or raw.get("baseSha") or ""),
            head=str(head_ref),
            head_sha=str(raw.get("headRefOid") or raw.get("headSha") or ""),
            head_repo=head_repo_name,
            url=str(raw.get("url") or ""),
        )


@dataclass(frozen=True)
class StatePaths:
    root: Path
    worktrees: Path
    runs: Path
    shell: Path
    dashboards: Path


@dataclass(frozen=True)
class AgentSettings:
    """Provider settings shared by developer and reviewer invocations."""

    provider: str = "codex"
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    max_tool_turns: int | None = None
    command_timeout_seconds: int | None = None
    allow_network: bool = False
    json_mode: bool = False
    executable_path: str | None = None
    host: str | None = None
    permission_policy: dict[str, bool] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "timeout_seconds": self.timeout_seconds,
            "api_key_env": self.api_key_env,
            "base_url": self.base_url,
            "executable_path": self.executable_path,
            "max_tool_turns": self.max_tool_turns,
            "command_timeout_seconds": self.command_timeout_seconds,
            "allow_network": self.allow_network,
            "json_mode": self.json_mode,
            "host": self.host,
            "permission_policy": dict(self.permission_policy or {}),
        }

