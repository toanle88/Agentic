"""Repository-path and provider-independent permission policy."""
from __future__ import annotations

from typing import Any
from pathlib import PurePosixPath


PERMISSION_FIELDS = (
    "read_files",
    "search_files",
    "edit_files",
    "create_files",
    "git_read",
    "run_checks",
    "network",
    "allow_dangerous",
)
PERMISSION_ROLES = ("developer", "reviewer")

# These names are intentionally shared by publication and API-read policy.
# Matching is case-insensitive because repository paths are not uniformly
# case-sensitive across the supported platforms.
_PROTECTED_BASENAMES = frozenset({
    ".env", ".npmrc", ".pypirc", ".netrc", ".aws", ".docker",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "credentials.json", "credential.json", "service-account.json",
    "service_account.json", "google-credentials.json",
    "application_default_credentials.json", "token.json", "tokens.json",
    "agents.md", ".gitmodules",
})
_PROTECTED_SUFFIXES = (".pem", ".p12", ".pfx", ".key", ".ppk", ".der")


def _protected_path(filename: str) -> tuple[list[str], str]:
    path = PurePosixPath(filename)
    return [item.lower() for item in path.parts], path.name.lower()


def _protected_basename(parts: list[str], base: str) -> bool:
    return (
        base in _PROTECTED_BASENAMES
        or base.startswith(".env.")
        or base.endswith(_PROTECTED_SUFFIXES)
        or (len(parts) >= 2 and parts[-2:] == [".docker", "config.json"])
    )


def default_permission_policy(role: str) -> dict[str, bool]:
    """Return the safe, provider-independent policy for one workflow role."""
    if role not in {"developer", "reviewer"}:
        raise ValueError("permission policy role must be developer or reviewer")
    developer = role == "developer"
    return {
        "read_files": True,
        "search_files": True,
        "edit_files": developer,
        "create_files": developer,
        "git_read": True,
        "run_checks": developer,
        "network": False,
        "allow_dangerous": False,
    }


def validate_permission_policy_container(raw: Any, *, field_name: str) -> None:
    """Reject unknown top-level role or legacy capability keys."""
    if raw is None or not isinstance(raw, dict):
        return
    keys = set(raw)
    if keys & set(PERMISSION_ROLES):
        unknown = sorted(keys - set(PERMISSION_ROLES))
    else:
        unknown = sorted(keys - set(PERMISSION_FIELDS))
    if unknown:
        raise ValueError(
            f"{field_name} contains unsupported top-level key(s): {', '.join(unknown)}"
        )


def validate_permission_policy(
    raw: Any,
    *,
    role: str,
    security_mode: str = "trusted",
    default_network: bool = False,
) -> dict[str, bool]:
    """Normalize a role policy and fail closed for unsafe or unknown fields."""
    policy = default_permission_policy(role)
    if default_network:
        policy["network"] = True
    if raw is None:
        return policy
    if not isinstance(raw, dict):
        raise ValueError(f"{role}.permission_policy must be an object")
    unknown = sorted(set(raw) - set(PERMISSION_FIELDS))
    if unknown:
        raise ValueError(
            f"{role}.permission_policy contains unsupported field(s): {', '.join(unknown)}"
        )
    for name, value in raw.items():
        if type(value) is not bool:
            raise ValueError(f"{role}.permission_policy.{name} must be a boolean")
        policy[name] = value
    if role == "reviewer" and any(
        policy[name] for name in ("edit_files", "create_files", "run_checks", "allow_dangerous")
    ):
        raise ValueError("reviewer.permission_policy must remain read-only")
    if policy["allow_dangerous"] and security_mode != "isolated":
        raise ValueError(
            f"{role}.permission_policy.allow_dangerous requires security_mode 'isolated'"
        )
    return policy


def forbidden(filename: str) -> bool:
    """Identify paths that must never be included in a proposed patch."""
    parts, base = _protected_path(filename)
    return (
        filename.lower() in {"agentic.py", "agent_config.json", "agents.md"}
        or _protected_basename(parts, base)
        or "secrets" in parts or ".git" in parts
        or ".codex" in parts
        or (len(parts) >= 3 and parts[:2] == [".github", "workflows"])
    )


def sensitive_read(filename: str) -> bool:
    """Identify files an API-backed agent should not exfiltrate to a model."""
    if not isinstance(filename, str):
        return False
    parts, base = _protected_path(filename)
    return (
        _protected_basename(parts, base)
        or "secrets" in parts
        or ".git" in parts
        or ".codex" in parts
        or ".aws" in parts
    )
