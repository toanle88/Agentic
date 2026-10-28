"""Small, dependency-free safety helpers shared by Agentic components."""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping


_SENSITIVE_ENV_NAME = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_URL_USERINFO = re.compile(
    r"(?i)\b([A-Za-z][A-Za-z0-9+.-]*://)([^/?#\s@]+)@"
)
_CREDENTIAL_KEY = (
    r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|"
    r"password|passwd|secret|token|credential|key|"
    r"(?:[A-Za-z_][A-Za-z0-9_]*[_-])+(?:key|token|secret|password|passwd|credential|auth))"
)
_KEY_VALUE = re.compile(
    rf"(?i)([\"']?\b{_CREDENTIAL_KEY}\b[\"']?\s*[:=]\s*)"
    r"([\"']?)([^\s,;\"'}]+)(\2)"
)


def sensitive_environment_values(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return likely credential values without exposing environment names."""
    source = os.environ if environ is None else environ
    values = {
        value
        for name, value in source.items()
        if _SENSITIVE_ENV_NAME.search(name) and len(value) >= 8
    }
    return sorted(values, key=len, reverse=True)


def configured_environment_values(
    environ: Mapping[str, str], names: Iterable[str],
) -> list[str]:
    """Return every non-empty value named by explicit credential settings.

    Explicit configuration is an intentional secret boundary even when the
    environment variable name is not credential-shaped. The length heuristic
    belongs only to discovery of likely credentials from arbitrary names.
    """
    values = {
        value for name in names
        if isinstance(name, str)
        and (value := environ.get(name))
        and isinstance(value, str)
    }
    return sorted(values, key=len, reverse=True)


def restricted_test_environment(
    environ: Mapping[str, str] | None = None,
    blocked_names: Iterable[str] = (),
) -> tuple[dict[str, str], list[str]]:
    """Remove credential-like variables before running repository tests.

    Configured credential names are accepted separately because their names may
    be intentionally non-semantic (for example ``SERVICE_VALUE``).  The
    returned values are used only for output redaction and are never persisted.
    """
    source = os.environ if environ is None else environ
    blocked = {str(name).casefold() for name in blocked_names}
    safe: dict[str, str] = {}
    removed_values: list[str] = []
    for name, value in source.items():
        explicitly_blocked = name.casefold() in blocked
        if _SENSITIVE_ENV_NAME.search(name) or explicitly_blocked:
            # A configured credential name is an explicit secret boundary;
            # short values must be redacted just like longer credentials.
            if (explicitly_blocked or len(value) >= 8) and value:
                removed_values.append(value)
            continue
        safe[name] = value
    return safe, sorted(set(removed_values), key=len, reverse=True)


def redact_secrets(text: str, extra_values: Iterable[str] = ()) -> str:
    """Redact credentials from terminal output, durable logs, and dashboards.

    This is deliberately conservative: it protects common credential formats
    and values from credential-looking environment variables. It is not a
    replacement for running Agentic in a trusted checkout or a process-level
    secret-isolation mechanism.
    """
    if not text:
        return text
    redacted = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    redacted = _BEARER.sub(r"\1[REDACTED]", redacted)

    def redact_key_value(match: re.Match[str]) -> str:
        # ``audit_key`` is a stable run identifier, not a credential.  It is
        # persisted in audit reports and must survive the durable redaction
        # boundary so a later publish can match the report to its run.
        field = re.search(r"[A-Za-z_][A-Za-z0-9_-]*", match.group(1))
        if field and field.group(0).casefold() == "audit_key":
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(4)}"

    redacted = _KEY_VALUE.sub(redact_key_value, redacted)
    values = list(sensitive_environment_values())
    # Callers use extra_values for explicitly configured credentials. Do not
    # apply the heuristic length threshold to those values.
    values.extend(value for value in extra_values if isinstance(value, str) and value)
    for value in sorted(set(values), key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted
