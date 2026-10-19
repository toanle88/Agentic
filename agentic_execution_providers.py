"""Provider-specific adapters built on the shared streaming runner."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agentic_execution_stream import (
    _record_provider_usage,
    _short,
    streamed_command,
)
from agentic_guidance import guidance_phase, guidance_prompt
from agentic_providers import cli_provider_command, codex_command
from agentic_security import redact_secrets
from agentic_telemetry import extract_jsonl_usage

def run_codex(worktree: Path, prompt: str, *, output: Path, readonly: bool = False,
              schema: Path | None = None, timeout: int | None = None,
              model: str | None = None,
              reasoning_effort: str | None = None,
              sandbox: dict[str, Any] | None = None) -> tuple[bool, str]:
    # Each invocation creates a fresh session. The repository is the memory.
    prompt = guidance_prompt(
        worktree, prompt, readonly=readonly, audit_folder=output.parent,
        phase=guidance_phase(output, readonly=readonly),
    )
    args = codex_command(
        prompt, readonly=readonly, schema=schema, output=output,
        model=model, reasoning_effort=reasoning_effort,
    )
    label = "REVIEW" if readonly else "DEVELOPER"
    invocation_started = time.monotonic()
    result = streamed_command(args, worktree, label=label, log_file=output.with_suffix(".log"),
                              timeout=timeout, codex_json=True, sandbox=sandbox,
                              sandbox_read_only=readonly,
                              sandbox_pass_env=(sandbox or {}).get("pass_env", []),
                              metrics_folder=output.parent)
    message = output.read_text(encoding="utf-8") if output.exists() else result.stdout[-4000:]
    message = redact_secrets(message)
    output.write_text(message, encoding="utf-8")
    usage_path = output.with_suffix(".jsonl")
    usage = extract_jsonl_usage(
        usage_path.read_text(encoding="utf-8", errors="replace") if usage_path.exists() else "",
        provider="codex",
    )
    _record_provider_usage(
        output, readonly, usage, prompt_chars=len(prompt),
        completion_chars=len(message), turns=1,
        duration_seconds=time.monotonic() - invocation_started,
        status="completed" if result.returncode == 0 else "failed",
        exit_code=result.returncode,
    )
    return result.returncode == 0, message
def _provider_progress(line: str, provider: str) -> str | None:
    """Turn Gemini/Claude JSON events into compact terminal progress."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return _short(line, 350) if line.strip() else None
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type", ""))
    if event_type in {"init", "thread.started", "session_start"}:
        return f"{provider.title()} session started"
    if event_type in {"result", "turn.completed", "session_end"}:
        return f"{provider.title()} agent finished"
    if event_type in {"tool_use", "tool_call"}:
        tool = event.get("tool")
        tool_name = tool.get("name") if isinstance(tool, dict) else tool
        name = event.get("name") or event.get("tool_name") or tool_name
        return f"Using tool: {_short(name or 'tool', 160)}"
    if event_type in {"error", "turn.failed"}:
        return "ERROR: " + _short(event.get("error", event.get("message", line)), 300)
    if event_type in {"message", "assistant", "agent_message"}:
        content = event.get("content") or event.get("text")
        if isinstance(content, list):
            content = " ".join(
                str(item.get("text", "")) for item in content
                if isinstance(item, dict) and item.get("text")
            )
        return "Agent: " + _short(content, 350) if content else None
    return None


def _message_text(value: Any) -> str:
    """Extract text from the common CLI/API message content shapes."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return _message_text(value.get("text") or value.get("content") or value.get("response"))
    return ""


def _extract_provider_message(raw: str, provider: str) -> str:
    """Recover the final assistant message from a provider's JSON stream."""
    if provider == "copilot":
        return raw.strip()
    chunks: list[str] = []
    final: list[str] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        response = _message_text(event.get("response"))
        result = _message_text(event.get("result"))
        if response:
            final.append(response)
        if result:
            final.append(result)
        if event_type in {"message", "assistant", "agent_message"}:
            message = event.get("message")
            content = _message_text(event.get("content"))
            if not content and isinstance(message, dict):
                content = _message_text(message.get("content"))
            if content:
                chunks.append(content)
    return (final[-1] if final else "\n".join(chunks)).strip()


def run_cli_provider(worktree: Path, prompt: str, *, output: Path,
                     provider: str, readonly: bool = False,
                     timeout: int | None = None,
                     model: str | None = None,
                     sandbox: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Run a locally installed coding CLI in its non-interactive mode."""
    prompt = guidance_prompt(
        worktree, prompt, readonly=readonly, audit_folder=output.parent,
        phase=guidance_phase(output, readonly=readonly),
    )
    args, structured = cli_provider_command(provider, prompt, readonly=readonly, model=model)

    label = "REVIEW" if readonly else "DEVELOPER"
    invocation_started = time.monotonic()
    result = streamed_command(
        args, worktree, label=label, log_file=output.with_suffix(".log"),
        timeout=timeout, codex_json=structured,
        progress_parser=(lambda line: _provider_progress(line, provider)) if structured else None,
        sandbox=sandbox, sandbox_read_only=readonly,
        sandbox_pass_env=(sandbox or {}).get("pass_env", []),
        metrics_folder=output.parent,
    )
    raw_path = output.with_suffix(".jsonl") if structured else None
    raw = raw_path.read_text(encoding="utf-8", errors="replace") if raw_path and raw_path.exists() else result.stdout
    message = redact_secrets(_extract_provider_message(raw, provider) or result.stdout[-10000:].strip())
    output.write_text(message, encoding="utf-8")
    usage = extract_jsonl_usage(raw, provider=provider)
    _record_provider_usage(
        output, readonly, usage, prompt_chars=len(prompt),
        completion_chars=len(message), turns=1,
        duration_seconds=time.monotonic() - invocation_started,
        status="completed" if result.returncode == 0 else "failed",
        exit_code=result.returncode,
    )
    return result.returncode == 0, message
# End of provider adapters.
