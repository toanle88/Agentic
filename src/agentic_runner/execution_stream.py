"""Provider execution, console output, and streamed command helpers."""
from __future__ import annotations

import builtins
import datetime
import json
import math
import os
import queue
import signal
import shutil
import subprocess
import sys
import threading
import tempfile
import time
from pathlib import Path
from typing import Any

from .models import Issue, Run
from .persistence import BoundedAppendWriter
from .output import (
    is_transient_progress,
    paint,
    sanitize_terminal_text,
    strip_ansi,
    style_system_text,
    style_text,
    timestamped_status,
)
from .sandbox import sandbox_command
from .security import redact_secrets
from .telemetry import read_run_usage, record_operation, record_usage


MAX_STREAM_QUEUE = 256
MAX_RAW_LOG_BYTES = 2_000_000
MAX_RAW_TRUNCATION_MARKER_BYTES = 256
MAX_HUMAN_LOG_BYTES = 200_000
STREAM_READER_JOIN_TIMEOUT = 1.0
HUMAN_LOG_TRUNCATION_MARKER = (
    "[AGENTIC] agentic_stream_truncated: human-readable log limit reached.\n"
)


def _process_group_kwargs() -> dict[str, Any]:
    """Start a provider in a process group so timeout cleanup includes children."""
    if os.name != "nt":
        return {"start_new_session": True}
    return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}


def _stop_process_tree(process: subprocess.Popen[Any]) -> None:
    """Stop a provider and wait for its process group to exit."""
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    else:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False,
            )
        except OSError:
            pass
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _append_bounded_human_log(path: Path, text: str) -> bool:
    """Append one human-log entry without exceeding the durable byte budget."""
    try:
        writer = BoundedAppendWriter(
            path, max_bytes=MAX_HUMAN_LOG_BYTES,
            truncation_marker=HUMAN_LOG_TRUNCATION_MARKER, truncate=False,
        )
        try:
            writer.write(text)
            return not writer.truncated
        finally:
            writer.close()
    except OSError:
        return False

def _short(value: Any, limit: int = 180) -> str:
    """Display single-line progress without flooding the terminal."""
    text = str(value).replace("\r", " ").replace("\n", " ⏎ ").strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _doctor_public_detail(
    label: str,
    ok: bool,
    required: bool,
    detail: str | None = None,
) -> str:
    """Return concise, safe details for doctor output.

    Successful checks retain their established compact labels. Required
    failures may expose the diagnostic reason, but only after terminal-control
    removal, secret redaction, and length bounding. Provider and configured
    command output must be converted to a fixed reason before reaching here.
    """
    if label == "Sandbox network":
        return (
            "enabled for developer and read-only reviewer; disable unless outbound access is required"
            if not ok else "disabled for developer and read-only reviewer"
        )
    safe_detail = redact_secrets(sanitize_terminal_text(detail or "")).strip()
    if ok and "copilot" in label.lower() and safe_detail.startswith("Copilot CLI"):
        return _short(safe_detail, 240)
    if ok:
        return "passed"
    if safe_detail:
        return _short(safe_detail, 240)
    return "required check failed" if required else "warning"


def _remaining_timeout(deadline: float | None, maximum: int | None) -> int | None:
    """Return the smaller configured/deadline timeout, never zero."""
    if deadline is None:
        return maximum
    remaining = max(1, int(deadline - time.monotonic()))
    return remaining if maximum is None else min(remaining, maximum)


def _record_provider_usage(output: Path, readonly: bool, usage: dict[str, Any], *,
                           model: str | None = None,
                           prompt_chars: int | None = None,
                           completion_chars: int | None = None,
                           turns: int | None = None,
                           duration_seconds: float | None = None,
                           status: str | None = None,
                           exit_code: int | None = None) -> None:
    role = (
        "investigator" if output.stem.casefold().startswith("investigation")
        else "reviewer" if readonly else "developer"
    )
    invocation: dict[str, Any] = {"name": output.stem}
    invocation["stage"] = role
    if model:
        invocation["model"] = model
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
        _console_print(
            style_system_text(
                timestamped_status(f"[COST] {role.title()}: {' '.join(parts)}"),
                system_prefix="COST",
            ),
            flush=True,
        )


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


def _fit_live_status(value: str) -> str:
    """Keep a redrawable status row inside the current terminal width."""
    width = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)
    text = strip_ansi(value).replace("\r", " ").replace("\n", " ")
    if len(text) <= width - 1:
        return text
    if width <= 4:
        return text[:width]
    return text[:width - 4] + "..."


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
    return paint(text, code)


def print_run_header(issue: Issue, run: Run, fresh: bool) -> None:
    state = "NEW" if fresh else "RESUME"
    separator = _unicode_or_ascii("─" * 72, "-" * 72)
    _console_print(timestamped_status("\n" + separator))
    _console_print(timestamped_status(
        f"AGENTIC  |  {issue.reference}  |  {_paint(state, '36' if fresh else '33')}"
    ))
    _console_print(timestamped_status(f"Title:    {_short(issue.title, 100)}"))
    _console_print(timestamped_status(f"Branch:   {run.branch}"))
    _console_print(timestamped_status(f"Worktree: {run.worktree}"))
    _console_print(timestamped_status(separator), flush=True)


def print_review_summary(verdict: dict[str, Any]) -> None:
    approved = bool(verdict.get("approved"))
    status = _paint("APPROVED", "32") if approved else _paint("REJECTED", "31")
    _console_print(style_text(timestamped_status(f"[REVIEW] {status}")))
    summary = str(verdict.get("summary", "")).strip()
    if summary:
        _console_print(style_text(timestamped_status(
            f"[REVIEW] {_short(summary, 500)}"
        ), style_status_tokens=False))
    findings = verdict.get("findings", [])
    if isinstance(findings, list):
        bullet = _unicode_or_ascii("•", "-")
        for finding in findings[:3]:
            _console_print(style_text(timestamped_status(
                f"  {bullet} {_short(finding, 260)}"
            ), style_status_tokens=False))
        if len(findings) > 3:
            _console_print(style_text(timestamped_status(
                f"  {bullet} ... and {len(findings) - 3} more; see the review JSON log"
            ), style_status_tokens=False))
    skipped = verdict.get("skipped", [])
    if isinstance(skipped, list) and skipped:
        _console_print(style_text(timestamped_status(
            f"[REVIEW] Skipped validation: {len(skipped)}"
        )))
        for item in skipped[:3]:
            _console_print(style_text(timestamped_status(
                f"  {bullet} {_short(item, 260)}"
            ), style_status_tokens=False))
        if len(skipped) > 3:
            _console_print(style_text(timestamped_status(
                f"  {bullet} ... and {len(skipped) - 3} more; see the review JSON log"
            ), style_status_tokens=False))
    repeated = verdict.get("_repeated_findings", [])
    if isinstance(repeated, list) and repeated:
        _console_print(style_text(timestamped_status(
            f"[REVIEW] Repeated findings from earlier attempts: {len(repeated)}"
        )))
        repeat_marker = _unicode_or_ascii("↻", "*")
        for item in repeated[:3]:
            if isinstance(item, dict):
                _console_print(style_text(timestamped_status(
                    f"  {repeat_marker} [{item.get('severity', 'UNSPECIFIED')}] "
                    f"{_short(item.get('text', ''), 240)}"
                ), style_status_tokens=False))


def _summary_token_text(value: Any) -> str:
    """Format a token count without turning missing provider data into zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    if value < 0 or (isinstance(value, float) and (not math.isfinite(value) or not value.is_integer())):
        return "unknown"
    return f"{int(value):,} tokens"


def _summary_stage_token_text(value: Any) -> str:
    """Format a stage token count for the labelled summary column."""
    return _summary_token_text(value).removesuffix(" tokens")


def _summary_duration_text(value: Any) -> str:
    """Format bounded telemetry durations for a compact terminal row."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    if not math.isfinite(float(value)) or value < 0:
        return "unknown"
    seconds = int(round(float(value)))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _summary_stage_data(usage: dict[str, Any], stage: str) -> tuple[int, float | None]:
    """Return operation count and summed duration for one execution stage."""
    metrics = usage.get("metrics")
    operations = metrics.get("operations") if isinstance(metrics, dict) else None
    if isinstance(operations, list):
        matching = [
            item for item in operations
            if isinstance(item, dict) and item.get("role") == stage
        ]
        if matching:
            durations: list[float] = []
            for item in matching:
                value = item.get("duration_seconds")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if math.isfinite(float(value)) and value >= 0:
                    durations.append(float(value))
            return len(matching), round(sum(durations), 3) if durations else None

    invocations = usage.get("by_invocation")
    if isinstance(invocations, list):
        matching = [
            item for item in invocations
            if isinstance(item, dict)
            and (item.get("stage") == stage or item.get("role") == stage)
        ]
        if matching:
            durations = []
            for item in matching:
                value = item.get("duration_seconds")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if math.isfinite(float(value)) and value >= 0:
                        durations.append(float(value))
            return len(matching), round(sum(durations), 3) if durations else None
    return 0, None


def _summary_stage_usage(usage: dict[str, Any], stage: str, count: int) -> dict[str, Any]:
    """Select stage usage while preserving unknown provider fields."""
    by_stage = usage.get("by_stage")
    candidate = by_stage.get(stage) if isinstance(by_stage, dict) else None
    if not isinstance(candidate, dict):
        by_role = usage.get("by_role")
        candidate = by_role.get(stage) if isinstance(by_role, dict) else None
    if isinstance(candidate, dict):
        return candidate
    return {"total_tokens": 0 if stage == "tests" else None}


def _summary_stage_line(stage: str, count: int, tokens: str, duration: str,
                        suffix: str = "", *, run_width: int = 2,
                        token_width: int = 9, duration_width: int = 7,
                        label_width: int = len("[DEVELOPER]") + 2) -> str:
    """Render one stage row from widths calculated from unstyled values."""
    label = "REVIEW" if stage == "reviewer" else stage.upper()
    plain_label = f"[{label}]".ljust(label_width)
    plain_duration = duration.rjust(duration_width)
    rendered_label = style_text(plain_label, style_status_tokens=False)
    rendered_duration = _paint(plain_duration, "36")
    return (
        f"{rendered_label}Runs: {count:>{run_width}} | "
        f"Tokens: {tokens:>{token_width}} | Time: {rendered_duration}{suffix}"
    )


def _print_execution_summary(folder: Path) -> None:
    """Print bounded, terminal-only execution telemetry for an approved run."""
    usage = read_run_usage(folder)
    total = _summary_token_text(usage.get("total_tokens"))
    prompt = _summary_token_text(usage.get("prompt_tokens"))
    completion = _summary_token_text(usage.get("completion_tokens"))
    cost_value = usage.get("estimated_cost_usd")
    if (
        isinstance(cost_value, (int, float))
        and not isinstance(cost_value, bool)
        and math.isfinite(float(cost_value))
        and cost_value >= 0
    ):
        cost = f"${float(cost_value):.4f}"
    else:
        cost = "unknown"
    metrics = usage.get("metrics")
    total_duration = metrics.get("duration_seconds") if isinstance(metrics, dict) else None
    _console_print(style_text(timestamped_status(
        f"[SUMMARY] Total: {total} | Prompt/input: {prompt} | "
        f"Completion/output: {completion} | Cost: {cost}"
    ), style_status_tokens=False))
    _console_print(style_text(timestamped_status(
        f"[SUMMARY] Duration: {_summary_duration_text(total_duration)}"
    ), style_status_tokens=False))

    stage_rows: list[tuple[str, int, str, str, str]] = []
    stages = ("developer", "tests", "investigator", "reviewer")
    for stage in stages:
        count, duration = _summary_stage_data(usage, stage)
        stage_usage = _summary_stage_usage(usage, stage, count)
        tokens = _summary_stage_token_text(stage_usage.get("total_tokens"))
        suffix = ""
        if stage == "reviewer":
            workflow_metrics = usage.get("workflow_metrics")
            findings = (
                workflow_metrics.get("review_findings")
                if isinstance(workflow_metrics, dict) else None
            )
            findings_text = (
                str(findings)
                if isinstance(findings, int) and not isinstance(findings, bool)
                else "unknown"
            )
            suffix = f" | Findings: {findings_text}"
        stage_rows.append((
            stage, count, tokens, _summary_duration_text(duration), suffix,
        ))

    run_width = max(2, *(len(str(row[1])) for row in stage_rows))
    token_width = max(1, *(len(row[2]) for row in stage_rows))
    duration_width = max(1, *(len(row[3]) for row in stage_rows))
    label_width = max(
        len(f"[{('REVIEW' if stage == 'reviewer' else stage.upper())}]")
        for stage in stages
    ) + 1
    for stage, count, tokens, duration, suffix in stage_rows:
        _console_print(timestamped_status(_summary_stage_line(
            stage, count, tokens, duration, suffix,
            run_width=run_width, token_width=token_width,
            duration_width=duration_width, label_width=label_width,
        )))

    workflow_metrics = usage.get("workflow_metrics")
    attempts = workflow_metrics.get("attempts") if isinstance(workflow_metrics, dict) else None
    retries = workflow_metrics.get("retries") if isinstance(workflow_metrics, dict) else None
    attempts_text = (
        str(attempts) if isinstance(attempts, int) and not isinstance(attempts, bool) else "unknown"
    )
    retries_text = (
        str(retries) if isinstance(retries, int) and not isinstance(retries, bool) else "unknown"
    )
    _console_print(style_text(timestamped_status(
        f"[RETRY] Attempts: {attempts_text} | Retries: {retries_text}"
    ), style_status_tokens=False))


def print_run_summary(run: Run, status: str, next_action: str, *,
                      attempts: int | None = None,
                      max_retries: int | None = None,
                      reason: str = "",
                      logs_path: Path | None = None) -> None:
    separator = _unicode_or_ascii("─" * 72, "-" * 72)
    _console_print(timestamped_status("\n" + separator))
    _console_print(style_text(timestamped_status(f"[RESULT] {status}")))
    _console_print(timestamped_status(f"Branch:   {run.branch}"))
    _console_print(timestamped_status(f"Worktree: {run.worktree}"))
    if attempts is not None:
        retry_detail = (
            f" (configured retries: {max_retries})"
            if max_retries is not None else ""
        )
        configured_attempts = (
            f"/{max_retries + 1}" if max_retries is not None else ""
        )
        _console_print(timestamped_status(
            f"Attempts: {attempts}{configured_attempts}{retry_detail}"
        ))
    if reason:
        _console_print(timestamped_status(f"Reason:   {reason}"))
    if logs_path is not None:
        _console_print(timestamped_status(f"Logs:     {logs_path}"))
    if logs_path is not None and status.upper() in {"APPROVED", "VALIDATED", "MANUALLY APPROVED"}:
        _print_execution_summary(logs_path)
    _console_print(timestamped_status(f"Next:     {next_action}"))
    _console_print(timestamped_status(separator), flush=True)


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


def _generated_progress_spans(message: str) -> list[tuple[int, int]]:
    """Return only status spans generated by Agentic's progress formatter."""
    spans: list[tuple[int, int]] = []
    if message.startswith("ERROR:"):
        spans.append((0, len("ERROR")))
    elif message.startswith("Running command:"):
        spans.append((0, len("Running")))
    elif message.startswith("Command finished"):
        finished = message.find("finished")
        if finished >= 0:
            spans.append((finished, finished + len("finished")))
        exit_start = message.find("exit ", finished + len("finished"))
        if exit_start >= 0:
            exit_end = message.find(")", exit_start)
            spans.append((exit_start, len(message) if exit_end < 0 else exit_end))
    elif message.endswith("session started") and ": " not in message:
        started = message.rfind("started")
        spans.append((started, started + len("started")))
    elif message.startswith("Agent finished") or (
        message.endswith("agent finished") and ": " not in message
    ):
        finished = message.find("finished")
        spans.append((finished, finished + len("finished")))
    return spans


def _style_generated_progress(formatted: str, message: str,
                              spans: list[tuple[int, int]]) -> str:
    """Style explicit generated spans while leaving progress details plain."""
    message_start = len(formatted) - len(message)
    output: list[str] = []
    cursor = 0
    for start, end in spans:
        absolute_start = message_start + start
        absolute_end = message_start + end
        output.append(style_text(formatted[cursor:absolute_start], style_status_tokens=False))
        output.append(style_text(formatted[absolute_start:absolute_end]))
        cursor = absolute_end
    output.append(style_text(formatted[cursor:], style_status_tokens=False))
    return "".join(output)


def streamed_command(argv: list[str], cwd: Path, *, label: str, log_file: Path,
                     timeout: int | None, codex_json: bool = False,
                     heartbeat: int = 15,
                     progress_parser: Any | None = None,
                     verbose: bool = False,
                     display_output: bool = True,
                     sandbox: dict[str, Any] | None = None,
                     sandbox_read_only: bool = False,
                     sandbox_pass_env: list[str] | None = None,
                     metrics_folder: Path | None = None,
                     env: dict[str, str] | None = None,
                     redaction_values: list[str] | None = None,
                     output_root: Path | None = None,
                     preserve_git_metadata: bool = True) -> subprocess.CompletedProcess[str]:
    """Stream a subprocess to terminal + durable log; print heartbeats when quiet.

    Raw JSONL events are stored next to the human-readable log when a provider
    emits structured output.
    Only a bounded output tail is retained in memory for error reporting.
    ``display_output`` controls terminal rendering independently from durable
    logging, so noisy checks can keep their output in per-command logs without
    flooding the workflow transcript.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    invocation_temp: tempfile.TemporaryDirectory[str] | None = None
    if sandbox is not None and sandbox.get("enabled") and output_root is None:
        invocation_temp = tempfile.TemporaryDirectory(prefix="agentic-provider-")
        output_root = Path(invocation_temp.name)
    if output_root is not None:
        output_path = output_root.resolve()
        durable_path = log_file.parent.resolve()
        try:
            output_path.relative_to(durable_path)
        except ValueError:
            pass
        else:
            raise ValueError("provider output directory must be outside durable run state")
    raw_writer = None
    human_log = None
    proc = None
    try:
        raw_path = log_file.with_suffix(".jsonl") if codex_json else None
        values = redaction_values or ()
        raw_writer = (
            BoundedAppendWriter(
                raw_path, max_bytes=MAX_RAW_LOG_BYTES,
                truncation_marker=json.dumps({
                    "type": "agentic_stream_truncated",
                    "stream": "provider_jsonl",
                    "reason": "artifact_size_limit",
                }) + "\n",
                redaction_values=values,
            )
            if raw_path else None
        )
        human_log = BoundedAppendWriter(
            log_file, max_bytes=MAX_HUMAN_LOG_BYTES,
            truncation_marker=HUMAN_LOG_TRUNCATION_MARKER,
            redaction_values=values,
        )
        process_argv = sandbox_command(
            argv, cwd, sandbox, output_root=output_root, read_only=sandbox_read_only,
            pass_env=sandbox_pass_env, env=env,
            preserve_git_metadata=preserve_git_metadata,
        )
        proc = subprocess.Popen(
            process_argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=env, **_process_group_kwargs(),
        )
    except OSError as exc:
        if human_log is not None:
            human_log.close()
        if raw_writer is not None:
            raw_writer.close()
        if invocation_temp is not None:
            invocation_temp.cleanup()
        raise RuntimeError(f"Could not start {argv[0]!r}: {exc}") from exc
    except Exception:
        if human_log is not None:
            human_log.close()
        if raw_writer is not None:
            raw_writer.close()
        if invocation_temp is not None:
            invocation_temp.cleanup()
        raise

    lines: queue.Queue[str | None] = queue.Queue(maxsize=MAX_STREAM_QUEUE)
    dropped_lines = 0
    reader_stop = threading.Event()

    def reader() -> None:
        nonlocal dropped_lines
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                if reader_stop.is_set():
                    break
                try:
                    lines.put_nowait(raw)
                except queue.Full:
                    dropped_lines += 1
        except (OSError, ValueError):
            # Cleanup closes the pipe to unblock a reader after cancellation.
            return
        finally:
            while not reader_stop.is_set():
                try:
                    lines.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue

    reader_thread = threading.Thread(target=reader, name="agentic-stream-reader", daemon=True)
    reader_thread.start()
    started = time.monotonic()
    started_at = datetime.datetime.now(datetime.timezone.utc)
    last_notice = started
    tail = ""
    timed_out = False
    raw_bytes = 0
    raw_log_truncated = False
    human_log_truncated = False
    try:
        with human_log:
            live_status = False
            live_status_enabled = bool(getattr(sys.stdout, "isatty", lambda: False)())
            last_heartbeat_log: float | None = None
            modified_paths: set[str] = set()

            def clear_live_status() -> None:
                nonlocal live_status
                if live_status and live_status_enabled:
                    _console_print("\r\033[2K", end="", flush=True)
                live_status = False

            def display(message: str, *, style_status_tokens: bool = False,
                        generated_status_spans: list[tuple[int, int]] | None = None,
                        transient: bool = False, persist: bool = True,
                        show_on_terminal: bool = True) -> None:
                nonlocal human_log_truncated, last_notice, live_status
                formatted = strip_ansi(timestamped_status(f"[{label}] {message}"))
                if generated_status_spans is not None:
                    rendered = _style_generated_progress(
                        formatted, message, generated_status_spans,
                    )
                else:
                    rendered = style_text(formatted, style_status_tokens=style_status_tokens)
                if show_on_terminal:
                    if transient and live_status_enabled:
                        clear_live_status()
                        rendered = style_text(
                            _fit_live_status(formatted),
                            style_status_tokens=style_status_tokens,
                        )
                        _console_print("\r" + rendered, end="", flush=True)
                        live_status = True
                    else:
                        clear_live_status()
                        _console_print(rendered, flush=True)
                if persist:
                    entry = formatted + "\n"
                    if not human_log_truncated:
                        human_log.write(entry)
                        human_log_truncated = human_log.truncated
                last_notice = time.monotonic()

            def write_raw(line: str) -> None:
                nonlocal raw_bytes, raw_log_truncated
                if raw_writer is None or raw_log_truncated:
                    return
                encoded = line.encode("utf-8")
                if raw_bytes + len(encoded) > MAX_RAW_LOG_BYTES - MAX_RAW_TRUNCATION_MARKER_BYTES:
                    raw_log_truncated = True
                    return
                raw_writer.write(line)
                raw_bytes = raw_writer.written_bytes

            display(f"Started (PID {proc.pid}); progress log: {log_file}", style_status_tokens=True)
            while True:
                now = time.monotonic()
                if timeout is not None and now - started > timeout:
                    timed_out = True
                    display(f"Time limit reached ({timeout}s). Stopping process.", style_status_tokens=True)
                    reader_stop.set()
                    _stop_process_tree(proc)
                    proc.wait()
                    break
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    now = time.monotonic()
                    if now - last_notice >= heartbeat:
                        display(
                            f"Still running ({int(now - started)}s elapsed)...",
                            style_status_tokens=True, transient=True,
                        )
                    continue
                if line is None:
                    break
                safe_line = strip_ansi(redact_secrets(line, extra_values=redaction_values or ()))
                write_raw(safe_line)
                tail = (tail + safe_line)[-10000:]
                if codex_json:
                    shown = (progress_parser or _codex_progress)(safe_line)
                else:
                    shown = safe_line.strip("\r\n")
                if shown:
                    displayed = (
                        redact_secrets(shown, extra_values=redaction_values or ())
                        if codex_json else _short(shown, 500)
                    )
                    transient = is_transient_progress(f"[{label}] {displayed}")
                    persist = verbose or not transient or not display_output
                    if not verbose and displayed.startswith("Still running ("):
                        now = time.monotonic()
                        persist = (
                            last_heartbeat_log is None
                            or now - last_heartbeat_log >= 60
                        )
                        if persist:
                            last_heartbeat_log = now
                    if not verbose and displayed.startswith("Modified files:"):
                        details = displayed.partition(":")[2].strip()
                        if details == "patch applied":
                            modified_paths.add("<patch>")
                        else:
                            modified_paths.update(
                                item.strip() for item in details.split(",") if item.strip()
                            )
                        persist = False
                    display(
                        displayed,
                        generated_status_spans=(
                            _generated_progress_spans(displayed) if codex_json else None
                        ),
                        transient=transient,
                        persist=persist,
                        show_on_terminal=display_output,
                    )
            proc.wait()
            if dropped_lines or raw_log_truncated or human_log_truncated:
                marker = json.dumps({
                    "type": "agentic_stream_truncated",
                    "dropped_lines": dropped_lines,
                    "raw_log_truncated": raw_log_truncated,
                    "human_log_truncated": human_log_truncated,
                }) + "\n"
                if raw_writer:
                    raw_writer.write(marker)
                    raw_bytes = raw_writer.written_bytes
                display(
                    "Output truncated: "
                    f"{dropped_lines} queued line(s) dropped"
                    + ("; raw JSONL log limit reached" if raw_log_truncated else "")
                    + ("; human-readable log limit reached" if human_log_truncated else "."),
                    style_status_tokens=True,
                )
            seconds = int(time.monotonic() - started)
            code = proc.returncode
            if modified_paths and not verbose:
                count = len(modified_paths)
                display(
                    f"Modified files: {count} file{'s' if count != 1 else ''} changed",
                )
            display(
                f"Finished in {seconds}s (exit {code})" + (" [TIMEOUT]" if timed_out else ""),
                style_status_tokens=True,
            )
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
        _append_bounded_human_log(
            log_file,
            timestamped_status(f"[{label}] Cancelled by user.") + "\n",
        )
        _stop_process_tree(proc)
        proc.wait()
        raise
    finally:
        if proc.poll() is None:
            reader_stop.set()
            _stop_process_tree(proc)
            proc.wait()
        reader_stop.set()
        if proc.stdout:
            proc.stdout.close()
        reader_thread.join(timeout=STREAM_READER_JOIN_TIMEOUT)
        if human_log:
            human_log.close()
        if raw_writer:
            raw_writer.close()
        if invocation_temp is not None:
            invocation_temp.cleanup()
    return subprocess.CompletedProcess(argv, proc.returncode if not timed_out else 124, stdout=tail)
