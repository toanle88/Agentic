"""Provider command construction.

This module owns provider-specific CLI argv policy. Execution, logging,
sandbox wrapping, output parsing, and usage accounting remain in ``agentic``
so the compatibility-facing runner can preserve its existing behavior while
provider command construction is tested independently.
"""
from __future__ import annotations

import json
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


def cli_provider_command(
    provider: str,
    prompt: str,
    *,
    readonly: bool = False,
    model: str | None = None,
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
        args = ["copilot", "-p", prompt, "-s"]
        if model:
            args += ["--model", model]
        if readonly:
            # Copilot has no universal read-only mode; expose only inspection
            # tools and deny shell execution for review turns.
            args += ["--available-tools", "view,grep,glob", "--deny-tool", "shell(*)"]
        else:
            args += ["--allow-all-tools", "--allow-all-paths"]
        return args, False
    raise ValueError(f"Unsupported CLI provider: {provider}")
