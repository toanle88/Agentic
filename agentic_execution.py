"""Provider execution, console output, and streamed command helpers."""
from __future__ import annotations

import builtins
import datetime
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from agentic_models import Issue, Run
from agentic_guidance import guidance_phase, guidance_prompt
from agentic_providers import cli_provider_command, codex_command
from agentic_sandbox import sandbox_command
from agentic_security import redact_secrets
from agentic_telemetry import extract_jsonl_usage, record_operation, record_usage

def _short(value: Any, limit: int = 180) -> str:
    """Display single-line progress without flooding the terminal."""
    text = str(value).replace("\r", " ").replace("\n", " ⏎ ").strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _doctor_public_detail(label: str, ok: bool, required: bool) -> str:
    """Return only fixed, non-sensitive details for doctor output."""
    if label == "Sandbox network":
        return (
            "enabled for developer and read-only reviewer; disable unless outbound access is required"
            if not ok else "disabled for developer and read-only reviewer"
        )
    return "passed" if ok else ("required check failed" if required else "warning")


def _remaining_timeout(deadline: float | None, maximum: int | None) -> int | None:
    """Return the smaller configured/deadline timeout, never zero."""
    if deadline is None:
        return maximum
    remaining = max(1, int(deadline - time.monotonic()))
    return remaining if maximum is None else min(remaining, maximum)


def _record_provider_usage(output: Path, readonly: bool, usage: dict[str, Any], *,
                           prompt_chars: int | None = None,
                           completion_chars: int | None = None,
                           turns: int | None = None,
                           duration_seconds: float | None = None,
                           status: str | None = None,
                           exit_code: int | None = None) -> None:
    role = "reviewer" if readonly else "developer"
    invocation = {"name": output.stem}
    if prompt_chars is not None:
        invocation["prompt_chars"] = prompt_chars
    if completion_chars is not None:
        invocation["completion_chars"] = completion_chars
    if turns is not None:
        invocation["turns"] = turns
    if duration_seconds is not None:
        invocation["duration_seconds"] = round(max(0.0, duration_seconds), 3)
    if status is not None:
        invocation["status"] = status
    if exit_code is not None:
        invocation["exit_code"] = exit_code
    record_usage(output.parent, role, usage, invocation=invocation)
    tokens = usage.get("total_tokens")
    cost = usage.get("estimated_cost_usd")
    if tokens is not None or cost is not None:
        parts = []
        if tokens is not None:
            parts.append(f"{tokens:,} tokens")
        if cost is not None:
            parts.append(f"${cost:.4f}")
        print(f"[COST] {role.title()}: {' '.join(parts)}", flush=True)


def _console_text(value: Any, stream: Any = None) -> str:
    """Make human-facing output safe for legacy Windows console encodings."""
    text = str(value)
    target = stream or sys.stdout
    encoding = getattr(target, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    return text


def _absolute_path(path: Path) -> Path:
    """Make a path absolute without resolving Windows short-name aliases."""
    return Path(os.path.abspath(os.fspath(path)))


def _paths_equal(left: Path, right: Path) -> bool:
    """Compare paths by filesystem identity when possible, including Windows aliases."""
    try:
        return left.samefile(right)
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
            os.path.abspath(os.fspath(right))
        )


def _console_print(*values: Any, sep: str = " ", end: str = "\n",
                   file: Any = None, flush: bool = False) -> None:
    """Print without allowing provider or issue text to crash a Windows console."""
    target = file or sys.stdout
    builtins.print(*[_console_text(value, target) for value in values],
                   sep=sep, end=end, file=target, flush=flush)


def _unicode_or_ascii(unicode_text: str, ascii_text: str, stream: Any = None) -> str:
    target = stream or sys.stdout
    encoding = getattr(target, "encoding", None) or "utf-8"
    try:
        unicode_text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return ascii_text
    return unicode_text


def _paint(text: str, code: str) -> str:
    """Use color only for interactive output; logs remain plain text."""
    if os.environ.get("NO_COLOR") or not getattr(sys.stdout, "isatty", lambda: False)():
        return text
    return f"\033[{code}m{text}\033[0m"


def print_run_header(issue: Issue, run: Run, fresh: bool) -> None:
    state = "NEW" if fresh else "RESUME"
    separator = _unicode_or_ascii("─" * 72, "-" * 72)
    _console_print("\n" + separator)
    _console_print(f"AGENTIC  |  {issue.reference}  |  {_paint(state, '36' if fresh else '33')}")
    _console_print(f"Title:    {_short(issue.title, 100)}")
    _console_print(f"Branch:   {run.branch}")
    _console_print(f"Worktree: {run.worktree}")
    _console_print(separator, flush=True)


def print_review_summary(verdict: dict[str, Any]) -> None:
    approved = bool(verdict.get("approved"))
    status = _paint("APPROVED", "32") if approved else _paint("REJECTED", "31")
    _console_print(f"[REVIEW] {status}")
    summary = str(verdict.get("summary", "")).strip()
    if summary:
        _console_print(f"[REVIEW] {_short(summary, 500)}")
    findings = verdict.get("findings", [])
    if isinstance(findings, list):
        bullet = _unicode_or_ascii("•", "-")
        for finding in findings[:3]:
            _console_print(f"  {bullet} {_short(finding, 260)}")
        if len(findings) > 3:
            _console_print(f"  {bullet} ... and {len(findings) - 3} more; see the review JSON log")
    skipped = verdict.get("skipped", [])
    if isinstance(skipped, list) and skipped:
        _console_print(f"[REVIEW] Skipped validation: {len(skipped)}")
        for item in skipped[:3]:
            _console_print(f"  {bullet} {_short(item, 260)}")
        if len(skipped) > 3:
            _console_print(f"  {bullet} ... and {len(skipped) - 3} more; see the review JSON log")
    repeated = verdict.get("_repeated_findings", [])
    if isinstance(repeated, list) and repeated:
        _console_print(f"[REVIEW] Repeated findings from earlier attempts: {len(repeated)}")
        repeat_marker = _unicode_or_ascii("↻", "*")
        for item in repeated[:3]:
            if isinstance(item, dict):
                _console_print(
                    f"  {repeat_marker} [{item.get('severity', 'UNSPECIFIED')}] "
                    f"{_short(item.get('text', ''), 240)}"
                )


def print_run_summary(run: Run, status: str, next_action: str) -> None:
    color = "32" if status in {"APPROVED", "VALIDATED"} else "33"
    separator = _unicode_or_ascii("─" * 72, "-" * 72)
    _console_print("\n" + separator)
    _console_print(f"RESULT:   {_paint(status, color)}")
    _console_print(f"Branch:   {run.branch}")
    _console_print(f"Worktree: {run.worktree}")
    _console_print(f"Next:     {next_action}")
    _console_print(separator, flush=True)


def _codex_progress(line: str) -> str | None:
    """Summarize Codex JSONL events. Unknown events remain in the raw trace."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return _short(line, 350) if line.strip() else None
    if not isinstance(event, dict):
        return None
    event_type = event.get("type", "")
    if event_type in {"thread.started", "turn.started"}:
        return "Codex session started" if event_type == "thread.started" else "Agent is working"
    if event_type == "turn.completed":
        usage = event.get("usage") or {}
        if isinstance(usage, dict):
            tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            if tokens:
                return f"Agent finished its turn ({tokens:,} input/output tokens)"
        return "Agent finished its turn"
    if event_type in {"turn.failed", "error"}:
        return "ERROR: " + _short(event.get("error", event.get("message", line)), 300)
    if event_type not in {"item.started", "item.completed", "item.updated"}:
        return None
    item = event.get("item") or {}
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if kind == "command_execution":
        if event_type == "item.started":
            return "Running command: " + _short(item.get("command", "?"), 220)
        if event_type == "item.completed":
            code = item.get("exit_code")
            suffix = "" if code is None else f" (exit {code})"
            return "Command finished" + suffix
    if kind == "file_change" and event_type == "item.completed":
        changes = item.get("changes") or []
        paths = [c.get("path", "") for c in changes if isinstance(c, dict)]
        return "Modified files: " + _short(", ".join(paths) if paths else "patch applied", 240)
    if kind == "mcp_tool_call" and event_type == "item.started":
        return "Using tool: " + _short(item.get("tool", "MCP"))
    if kind == "agent_message" and event_type == "item.completed":
        return "Agent: " + _short(item.get("text", ""), 350) if item.get("text") else None
    return None


def streamed_command(argv: list[str], cwd: Path, *, label: str, log_file: Path,
                     timeout: int | None, codex_json: bool = False,
                     heartbeat: int = 15,
                     progress_parser: Any | None = None,
                     sandbox: dict[str, Any] | None = None,
                     sandbox_read_only: bool = False,
                     sandbox_pass_env: list[str] | None = None,
                     metrics_folder: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Stream a subprocess to terminal + durable log; print heartbeats when quiet.

    Raw JSONL events are stored next to the human-readable log when a provider
    emits structured output.
    Only a bounded output tail is retained in memory for error reporting.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    process_argv = sandbox_command(
        argv, cwd, sandbox, output_root=log_file.parent, read_only=sandbox_read_only,
        pass_env=sandbox_pass_env,
    )
    try:
        proc = subprocess.Popen(
            process_argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            bufsize=1, start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        raise RuntimeError(f"Could not start {argv[0]!r}: {exc}") from exc

    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                lines.put(raw)
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    started = time.monotonic()
    started_at = datetime.datetime.now(datetime.timezone.utc)
    last_notice = started
    tail = ""
    timed_out = False
    raw_path = log_file.with_suffix(".jsonl") if codex_json else None
    raw_file = raw_path.open("w", encoding="utf-8") if raw_path else None
    try:
        with log_file.open("w", encoding="utf-8", buffering=1) as human_log:
            def display(message: str) -> None:
                nonlocal last_notice
                now = datetime.datetime.now().strftime("%H:%M:%S")
                prefix = f"[{now}] [{label}]"
                formatted = f"{prefix} {message}"
                colored_label = _paint(f"[{label}]", {
                    "DEVELOPER": "36", "REVIEW": "35", "TESTS": "34",
                }.get(label, "37"))
                _console_print(f"[{now}] {colored_label} {message}", flush=True)
                human_log.write(formatted + "\n")
                last_notice = time.monotonic()

            display(f"Started (PID {proc.pid}); progress log: {log_file}")
            while True:
                now = time.monotonic()
                if timeout is not None and now - started > timeout:
                    timed_out = True
                    display(f"Time limit reached ({timeout}s). Stopping process.")
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                    proc.wait()
                    break
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    if now - last_notice >= heartbeat:
                        display(f"Still running ({int(now - started)}s elapsed)...")
                    continue
                if line is None:
                    break
                safe_line = redact_secrets(line)
                if raw_file:
                    raw_file.write(safe_line)
                    raw_file.flush()
                tail = (tail + safe_line)[-10000:]
                if codex_json:
                    shown = (progress_parser or _codex_progress)(safe_line)
                else:
                    shown = safe_line.strip("\r\n")
                if shown:
                    display(redact_secrets(shown) if codex_json else _short(shown, 500))
            proc.wait()
            seconds = int(time.monotonic() - started)
            code = proc.returncode
            display(f"Finished in {seconds}s (exit {code})" + (" [TIMEOUT]" if timed_out else ""))
            if metrics_folder is not None:
                record_operation(
                    metrics_folder, label.lower(), log_file.stem,
                    started_at=started_at.isoformat(timespec="milliseconds"),
                    finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(
                        timespec="milliseconds"
                    ),
                    duration_seconds=time.monotonic() - started,
                    status="timeout" if timed_out else "completed" if code == 0 else "failed",
                    exit_code=124 if timed_out else code,
                )
    except KeyboardInterrupt:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait()
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()
        if raw_file:
            raw_file.close()
    return subprocess.CompletedProcess(argv, proc.returncode if not timed_out else 124, stdout=tail)


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

