"""Repository-path policy shared by the runner and provider tools."""
from __future__ import annotations

from pathlib import PurePosixPath


def forbidden(filename: str) -> bool:
    """Identify paths that must never be included in a proposed patch."""
    path = PurePosixPath(filename)
    base = path.name.lower()
    parts = [item.lower() for item in path.parts]
    return (
        filename in {"agentic.py", "agent_config.json", "AGENTS.md"}
        or base in {".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "agents.md", ".gitmodules"}
        or base.startswith(".env.") or base.endswith((".pem", ".p12", ".pfx", ".key"))
        or "secrets" in parts or ".git" in parts
        or ".codex" in parts
        or (len(parts) >= 3 and parts[:2] == [".github", "workflows"])
    )


def sensitive_read(filename: str) -> bool:
    """Identify files an API-backed agent should not exfiltrate to a model."""
    if not isinstance(filename, str):
        return False
    path = PurePosixPath(filename)
    base = path.name.lower()
    parts = [item.lower() for item in path.parts]
    return (
        base in {".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", ".aws", ".netrc"}
        or base.startswith(".env.")
        or base.endswith((".pem", ".p12", ".pfx", ".key"))
        or "secrets" in parts
        or ".git" in parts
        or ".codex" in parts
        or ".aws" in parts
    )
