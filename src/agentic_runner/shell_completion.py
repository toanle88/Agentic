"""Pure command-completion helpers for the interactive Agentic shell."""
from __future__ import annotations

import shlex
from typing import Any


SHELL_COMMANDS = (
    "/run", "/ps", "/logs", "/list", "/runs", "/stop", "/status", "/dashboard",
    "/init", "/doctor", "/cleanup", "/version", "/help", "/quit", "/exit", "/?", "/audit", "/repair-pr",
)
SHELL_OPTIONS = {
    "/audit": ("--profile", "--instruction", "--publish", "--new", "--resume", "--offline", "--verbose"),
    "/run": ("--profile", "--instruction", "--review", "--publish", "--mark-done", "--reason", "--new", "--verbose", "--task-file"),
    "/logs": ("--verbose",), "/ps": ("--all",), "/runs": ("--json",),
    "/status": ("--json",), "/doctor": ("--json", "--commands", "--profile", "--worktree"),
    "/cleanup": ("--issue", "--all", "--yes", "--remove-logs"), "/stop": ("--all",),
    "/init": ("--provider", "--reviewer-provider", "--security-mode", "--sandbox-image", "--agents", "--force"),
}
COMMAND_DESCRIPTIONS: dict[str, str] = {
    "/audit": "run a fresh read-only repository audit", "/run": "start a background job or local task",
    "/ps": "show running or persisted background jobs", "/logs": "follow or show captured terminal output",
    "/list": "list open GitHub issues", "/runs": "list existing Agentic worktrees",
    "/stop": "stop one or all running background jobs", "/status": "show Agentic worktree and git status",
    "/dashboard": "generate and open the HTML dashboard", "/init": "initialize target-repository configuration",
    "/doctor": "diagnose local prerequisites", "/cleanup": "preview or remove clean worktrees",
    "/version": "show the Agentic version", "/help": "show available commands and usage",
    "/repair-pr": "inspect failed CI and repair an existing pull request",
    "/quit": "leave the shell; running jobs continue", "/exit": "leave the shell; running jobs continue",
    "/?": "show available commands and usage",
}
OPTION_DESCRIPTIONS: dict[str, str] = {
    "--profile": "execution profile (fast or slow)", "--instruction": "optional instruction preserved for this run",
    "--review": "request read-only review", "--publish": "publish the existing approved patch as a draft PR",
    "--mark-done": "record human completion for the current patch", "--reason": "reason recorded with manual completion",
    "--new": "start fresh worktree, ignoring cached state", "--verbose": "keep detailed provider progress in human-readable logs",
    "--all": "show jobs across all shell sessions", "--json": "output status in JSON format",
    "--commands": "run configured doctor checks (may mutate the worktree)", "--worktree": "worktree used by doctor checks",
    "--task-file": "use an existing task file", "--issue": "select one issue worktree",
    "--remove-logs": "also remove matching run logs", "--yes": "confirm cleanup changes",
    "--provider": "developer provider for initialization", "--reviewer-provider": "independent reviewer provider",
    "--sandbox-image": "container image for isolated execution", "--agents": "create a starter AGENTS.md",
    "--force": "replace generated setup files", "fast": "run the configured fast checks",
    "slow": "run fast checks plus configured slow checks",
}
LOG_COMPLETION_LIMIT = 20


def completion_candidates(line: str, cursor: int | None = None,
                          job_numbers: tuple[int, ...] = (),
                          jobs: dict[int, Any] | None = None) -> list[str]:
    """Return safe slash-command completions for the current input buffer."""
    cursor = len(line) if cursor is None else max(0, min(cursor, len(line)))
    before_cursor = line[:cursor]
    stripped = before_cursor.lstrip()
    if not stripped.startswith("/"):
        return []
    trailing_space = bool(before_cursor and before_cursor[-1].isspace())
    try:
        tokens = shlex.split(before_cursor)
    except ValueError:
        tokens = before_cursor.split()
    if not tokens:
        return list(SHELL_COMMANDS)
    command = tokens[0].lower()
    if len(tokens) == 1 and not trailing_space:
        return [candidate for candidate in SHELL_COMMANDS if candidate.startswith(command)]
    current = "" if trailing_space else tokens[-1]
    previous = tokens[-1] if trailing_space else (tokens[-2] if len(tokens) > 1 else "")
    if command in {"/audit", "/run", "/doctor"} and previous == "--profile":
        return [value for value in ("fast", "slow") if value.startswith(current)]
    if command == "/repair-pr":
        return []
    if command == "/stop" and (current.startswith("-") or not job_numbers):
        return [option for option in SHELL_OPTIONS["/stop"] if option.startswith(current)]
    if command in {"/logs", "/stop"} and current.startswith("-"):
        return [option for option in SHELL_OPTIONS[command] if option.startswith(current)]
    if command == "/stop" and not job_numbers:
        return [option for option in SHELL_OPTIONS[command] if option.startswith(current)]
    if command in {"/logs", "/stop"}:
        if command == "/logs":
            running = sorted(
                number for number in job_numbers
                if jobs and number in jobs and jobs[number].running
            )
            completed = sorted(
                (number for number in job_numbers
                 if not (jobs and number in jobs and jobs[number].running)),
                reverse=True,
            )
            numbers = running + completed[:LOG_COMPLETION_LIMIT]
        else:
            numbers = sorted(job_numbers, key=lambda number: (
                not bool(jobs and number in jobs and jobs[number].running), number,
            ))
        values = [str(number) for number in numbers]
        if command == "/stop":
            values.insert(0, "--all")
        return [value for value in values if value.startswith(current)]
    return [option for option in SHELL_OPTIONS.get(command, ()) if option.startswith(current)]


def token_start(line: str, cursor: int) -> int:
    """Return the start of the token immediately before the cursor."""
    start = max(0, min(cursor, len(line)))
    while start and not line[start - 1].isspace():
        start -= 1
    return start


def replace_current_token(line: str, replacement: str) -> str:
    """Replace the whitespace-delimited token at the cursor's end."""
    return line[:token_start(line, len(line))] + replacement


def apply_completion(line: str, cursor: int, replacement: str,
                     *, add_space: bool = False) -> tuple[str, int]:
    """Replace the token at the cursor and return the new line and cursor."""
    start = token_start(line, cursor)
    updated = line[:start] + replacement + line[cursor:]
    new_cursor = start + len(replacement)
    if add_space and new_cursor == len(updated) and not updated.endswith(" "):
        updated += " "
        new_cursor += 1
    return updated, new_cursor


def describe_candidate(candidate: str, command: str = "",
                        jobs: dict[int, Any] | None = None) -> str:
    """Return a concise description for a command, option, or job candidate."""
    del command
    if candidate in COMMAND_DESCRIPTIONS:
        return COMMAND_DESCRIPTIONS[candidate]
    if candidate in OPTION_DESCRIPTIONS:
        return OPTION_DESCRIPTIONS[candidate]
    if candidate.isdigit() and jobs and int(candidate) in jobs:
        job = jobs[int(candidate)]
        state = "RUNNING" if job.running else f"EXIT {job.exit_code if job.exit_code is not None else '?'}"
        return f"{state} (PID {job.pid})"
    return ""


def completion_items(line: str, cursor: int | None = None,
                     job_numbers: tuple[int, ...] = (),
                     jobs: dict[int, Any] | None = None) -> list[tuple[str, str]]:
    """Return candidates paired with descriptions for the dropdown menu."""
    candidates = completion_candidates(line, cursor, job_numbers, jobs=jobs)
    tokens = line[:cursor].split() if cursor is not None else line.split()
    command = tokens[0].lower() if tokens else ""
    return [(candidate, describe_candidate(candidate, command, jobs)) for candidate in candidates]
