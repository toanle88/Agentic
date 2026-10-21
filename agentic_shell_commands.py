"""Validation and extraction of interactive-shell command arguments.

This module deliberately returns argv fragments only. It does not execute
commands or interpret option values as shell syntax, leaving process lifecycle
and terminal rendering to :mod:`agentic_shell`.
"""
from __future__ import annotations

from collections.abc import Sequence


_RUN_VALUE_OPTIONS = {"--profile", "--instruction", "--reason", "--task-file"}
_RUN_BOOLEAN_OPTIONS = {
    "--review", "--publish", "--mark-done", "--new", "--offline", "--verbose",
}
_LOG_BOOLEAN_OPTIONS = {"--verbose"}


def has_job_flag(flags: Sequence[str], option: str) -> bool:
    """Check a boolean /run option without treating a value as a flag."""
    index = 0
    while index < len(flags):
        if flags[index] in _RUN_VALUE_OPTIONS:
            index += 2
            continue
        if flags[index] == option:
            return True
        index += 1
    return False


def option_value(flags: Sequence[str], option: str) -> str | None:
    """Return one value option from a validated shell argv fragment."""
    value_options = _RUN_VALUE_OPTIONS | {"--issue"}
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag in value_options:
            if flag == option and index + 1 < len(flags):
                return flags[index + 1]
            index += 2
            continue
        index += 1
    return None


def parse_job_command(parts: list[str]) -> tuple[str | None, list[str]]:
    """Parse ``/run`` while preserving option-like instruction data."""
    issue: str | None = None
    if len(parts) > 1 and parts[1].isdigit():
        issue = parts[1]
        flags = parts[2:]
    elif len(parts) == 1 or parts[1].startswith("--"):
        flags = parts[1:]
    else:
        raise ValueError(
            'usage: /run ISSUE [--profile fast|slow] [--instruction "text"] '
            '[--review|--publish|--mark-done|--new|--offline|--verbose]'
        )
    seen_values: set[str] = set()
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag in _RUN_VALUE_OPTIONS:
            if flag in seen_values:
                raise ValueError(f"/run accepts {flag} only once")
            seen_values.add(flag)
            if index + 1 >= len(flags):
                raise ValueError(f"{flag} requires a value")
            # Instructions are data, including when their value starts with
            # ``--``. Other options retain option-looking-value validation.
            if flag != "--instruction" and flags[index + 1].startswith("--"):
                raise ValueError(f"{flag} requires a value")
            if flag == "--profile" and flags[index + 1] not in {"fast", "slow"}:
                raise ValueError("--profile must be fast or slow")
            index += 2
            continue
        if flag not in _RUN_BOOLEAN_OPTIONS:
            raise ValueError(f"unknown /run option: {flag}")
        index += 1
    if has_job_flag(flags, "--review") and has_job_flag(flags, "--publish"):
        raise ValueError("/run cannot combine --review and --publish")
    if has_job_flag(flags, "--mark-done") and has_job_flag(flags, "--publish"):
        raise ValueError("/run cannot combine --mark-done and --publish")
    if not has_job_flag(flags, "--mark-done") and "--reason" in seen_values:
        raise ValueError("/run --reason requires --mark-done")
    return issue, flags


def parse_log_command(parts: list[str]) -> tuple[int, int, bool]:
    """Validate ``/logs`` and return its identifier, tail size, and verbosity."""
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError("usage: /logs JOB|PID|ISSUE [LINES] [--verbose]")
    identifier = int(parts[1])
    lines = 40
    verbose = False
    seen_lines = False
    for value in parts[2:]:
        if value in _LOG_BOOLEAN_OPTIONS:
            if verbose:
                raise ValueError("usage: /logs JOB|PID|ISSUE [LINES] [--verbose]")
            verbose = True
        elif value.isdigit() and not seen_lines and int(value) >= 1:
            lines = min(int(value), 500)
            seen_lines = True
        else:
            raise ValueError("usage: /logs JOB|PID|ISSUE [LINES] [--verbose]")
    return identifier, lines, verbose


def parse_options(
    parts: list[str], *, usage: str, value_options: set[str],
    boolean_options: set[str],
) -> list[str]:
    """Validate a shell subcommand's options and return its argv unchanged."""
    flags = parts[1:]
    seen: set[str] = set()
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag in value_options:
            if flag in seen:
                raise ValueError(f"{flag} accepts one value")
            if index + 1 >= len(flags) or flags[index + 1].startswith("--"):
                raise ValueError(f"{flag} requires a value")
            seen.add(flag)
            index += 2
            continue
        if flag not in boolean_options:
            raise ValueError(f"usage: {usage}")
        index += 1
    return flags
