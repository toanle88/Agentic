"""Small, dependency-free safety helpers shared by Agentic components."""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping


_SENSITIVE_ENV_NAME = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}")
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


def redact_secrets(text: str, extra_values: Iterable[str] = ()) -> str:
    """Redact credentials from terminal output, durable logs, and dashboards.

    This is deliberately conservative: it protects common credential formats
    and values from credential-looking environment variables. It is not a
    replacement for running Agentic in a trusted checkout or a process-level
    secret-isolation mechanism.
    """
    if not text:
        return text
    redacted = _BEARER.sub(r"\1[REDACTED]", text)
    redacted = _KEY_VALUE.sub(r"\1\2[REDACTED]\4", redacted)
    values = list(sensitive_environment_values())
    values.extend(value for value in extra_values if isinstance(value, str) and len(value) >= 8)
    for value in sorted(set(values), key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted
