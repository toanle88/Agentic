"""Provider command construction.

This module owns provider-specific CLI argv policy. Execution, logging,
sandbox wrapping, output parsing, and usage accounting remain in ``agentic``
so the compatibility-facing runner can preserve its existing behavior while
provider command construction is tested independently.
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def codex_command(
    prompt: str,
    *,
    readonly: bool = False,
    schema: Path | None = None,
    output: Path | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> list[str]:
    """Build a non-interactive Codex command without shell interpolation."""
    args = ["codex", "exec", "--json", "--sandbox", "read-only" if readonly else "workspace-write"]
    if model:
        args += ["--model", model]
    if reasoning_effort:
        args += ["--config", f"model_reasoning_effort={json.dumps(reasoning_effort)}"]
    if schema:
        args += ["--output-schema", str(schema)]
    if output:
        args += ["--output-last-message", str(output)]
    args.append(prompt)
    return args


@dataclass(frozen=True)
class ResolvedProviderExecutable:
    """A provider executable selected without changing the host environment."""

    path: str | None
    source: str | None = None


def _usable_executable(value: str) -> str | None:
    """Return an executable command or path only when it can be started."""
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or len(candidate.parts) > 1:
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    found = shutil.which(value)
    if found:
        return found
    return None


def copilot_candidate_paths(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Return known host paths used by the GitHub CLI Copilot installer."""
    environment = os.environ if environ is None else environ
    data_home_value = environment.get("XDG_DATA_HOME")
    data_home = (
        Path(data_home_value).expanduser()
        if data_home_value
        else Path.home() / ".local" / "share"
    )
    candidates = [data_home / "gh" / "copilot" / "copilot"]
    if os.name == "nt":
        candidates.append(data_home / "gh" / "copilot" / "copilot.exe")
    return candidates


def resolve_provider_executable(
    provider: str,
    configured_path: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    discover_copilot: bool = True,
) -> ResolvedProviderExecutable:
    """Resolve a provider command while preserving argv-only execution.

    Copilot's downloaded binary is considered only after an explicit setting
    and the normal PATH lookup. ``discover_copilot=False`` is used for image
    execution, where host paths are not visible inside the container.
    """
    if provider != "copilot":
        path = _usable_executable(provider)
        return ResolvedProviderExecutable(path, "PATH" if path else None)

    environment = os.environ if environ is None else environ
    if configured_path:
        path = _usable_executable(configured_path)
        return ResolvedProviderExecutable(path, "configuration" if path else None)
    if not discover_copilot:
        return ResolvedProviderExecutable(None)
    env_path = environment.get("COPILOT_PATH")
    if env_path:
        path = _usable_executable(env_path)
        return ResolvedProviderExecutable(path, "COPILOT_PATH" if path else None)
    path = _usable_executable("copilot")
    if path:
        return ResolvedProviderExecutable(path, "PATH")
    if discover_copilot:
        for candidate in copilot_candidate_paths(environment):
            path = _usable_executable(str(candidate))
            if path:
                return ResolvedProviderExecutable(path, "GitHub CLI download")
    return ResolvedProviderExecutable(None)


def display_provider_path(path: str) -> str:
    """Render a host path compactly for actionable terminal diagnostics."""
    candidate = Path(path)
    try:
        return "~" + str(candidate.expanduser().relative_to(Path.home()))
    except ValueError:
        return str(candidate)


def cli_provider_command(
    provider: str,
    prompt: str,
    *,
    readonly: bool = False,
    model: str | None = None,
    executable_path: str | None = None,
) -> tuple[list[str], bool]:
    """Build a native CLI provider command and report whether it is JSON-streamed."""
    if provider == "gemini":
        args = [
            "gemini", "-p", prompt, "--output-format", "stream-json",
            "--approval-mode", "plan" if readonly else "yolo",
        ]
        if model:
            args += ["--model", model]
        return args, True
    if provider == "claude":
        args = ["claude", "-p", prompt, "--output-format", "stream-json"]
        if readonly:
            args += ["--permission-mode", "plan"]
        else:
            args += ["--dangerously-skip-permissions"]
        if model:
            args += ["--model", model]
        return args, True
    if provider == "copilot":
        args = [executable_path or "copilot", "-p", prompt, "-s"]
        if model:
            args += ["--model", model]
        if readonly:
            # Keep native review turns closed: the runner supplies the staged
            # diff, so Copilot only needs file inspection tools. In particular,
            # do not expose bash/shell, whose unmatched commands could prompt
            # or inherit an existing approval.
            args += ["--available-tools", "view,grep,glob"]
        else:
            args += ["--allow-all-tools", "--allow-all-paths"]
        return args, False
    raise ValueError(f"Unsupported CLI provider: {provider}")
