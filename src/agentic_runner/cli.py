#!/usr/bin/env python3
"""A stage-free, resumable coding-agent runner.

Install Agentic once, then place a target-specific agent_config.json in the
repository being worked on. The selected provider is responsible for
understanding progress; Git worktrees supply durable context. No third-party
Python packages are required by the runner itself.
"""
from __future__ import annotations

import argparse
import os
import time
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import config as _agentic_config
from . import context as _agentic_context
from . import workflow_state as _agentic_workflow_state
from . import worktrees as _agentic_worktrees
from . import __version__
from .application_diagnostics import record_diagnostic, run_with_diagnostics
from .config import (  # noqa: F401
    DEFAULT_INIT_SECURITY_MODE,
    DEFAULT_OPENROUTER_BASE_URL,
    DEFAULT_REVIEWER_TIMEOUT,
    DEFAULT_TASK_TIMEOUT,
    DEFAULT_TEST_TIMEOUT,
    AUTH_MODES,
    PROVIDER_API_KEY_ENV,
    OPENROUTER_MAX_TURNS,
    SECURITY_MODES,
    SAFE_GIT_DIFF_CHECK,
    SUPPORTED_PROVIDERS,
    _append_git_exclude_entry,
    default_provider_settings,
    detect_test_commands,
    detect_project_language,
    load_config,
    preflight_agent_settings,
    preflight_security,
    api_key_env_for_provider,
    auth_mode_for_settings,
    sandbox_env_for_provider,
    sandbox_env_for_providers,
    test_commands_for_profile,
)
from .context import (  # noqa: F401
    FEEDBACK_CONTEXT_CHARS,
    INSTRUCTION_CONTEXT_CHARS as _INSTRUCTION_CONTEXT_CHARS,
    REPORT_CONTEXT_CHARS,
    _bounded_text,
    _compact_feedback,
    _issue_context,
)
from .workflow_state import (  # noqa: F401
    clear_blocked,
    latest_numbered_log as _latest_numbered_log,
    load_blocked,
    load_latest_developer_report,
    next_log,
    record_blocked,
    record_no_code_changes_blocked,
)
from .prompts import (  # noqa: F401
    _developer_prompt_context,
    _developer_run_instruction_context,
    _developer_system_prompt_context,
    _run_instruction_context,
    _system_prompt_context,
    _untrusted_prompt_context,
)
from .diagnostics import run_doctor as _run_doctor
from .test_discovery import (
    DISCOVERY_TIMEOUT_SECONDS,
    MAX_DISCOVERY_TIMEOUT_SECONDS,
    DiscoveryResult,
    discover_and_validate,
)
from .execution import (  # noqa: F401
    _absolute_path,
    _console_print,
    _paths_equal,
    _remaining_timeout,
    _short,
    _unicode_or_ascii,
    print_review_summary,
    print_run_header,
    print_run_summary,
    run_cli_provider,
    run_codex,
    streamed_command,
)
from .github_comments import post_terminal_comment
from .ci_repair import (
    can_resume_dirty_worktree,
    fetch_ci_evidence,
    find_worktree_for_branch,
    poll_ci,
    record_event as record_ci_repair_event,
    stash_dirty_worktree_for_repair,
    verify_worktree_for_pr,
    worktree_change_fingerprint,
)
from .output import style_system_text, style_text
from .persistence import read_text_bounded, read_text_tail
from .openrouter import (  # noqa: F401
    run_openrouter,
)
from .policy import default_permission_policy, forbidden  # noqa: F401
from .models import DEFAULT_GITHUB_HOST, AgentSettings, Issue, PullRequest, Run, StatePaths  # noqa: F401
from .review import (  # noqa: F401
    parse_review_result,
    record_review_findings, review_history_feedback,
)
from .sandbox import DEFAULT_SANDBOX_IMAGE, SUPPORTED_LANGUAGES, validate_sandbox
from .security import redact_secrets  # noqa: F401
from .telemetry import (
    read_run_instruction,
    record_run_instruction,
    record_issue_metadata,
    read_issue_snapshot,
    record_security_policy,
)

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = _SOURCE_ROOT if (_SOURCE_ROOT / "agentic.py").is_file() else Path(__file__).resolve().parent
ISSUE_CONTEXT_CHARS = _agentic_context.ISSUE_CONTEXT_CHARS
INSTRUCTION_CONTEXT_CHARS = _INSTRUCTION_CONTEXT_CHARS
MAX_RESUME_LOG_BYTES = 64_000
MAX_RESULT_BYTES = 256_000
# Keep the configuration constants available from the historical runner module
# while the implementation lives in agentic_config.
DEFAULT_SECURITY_MODE = _agentic_config.DEFAULT_SECURITY_MODE


def _status_print(message: Any, *, file: Any = None, flush: bool = False,
                  system_prefix: str | None = None) -> None:
    """Print human-facing status with TTY-only styling."""
    if system_prefix is None:
        rendered = style_text(message, file)
    else:
        rendered = style_system_text(message, file, system_prefix=system_prefix)
    _console_print(rendered, file=file, flush=flush)


def _warn_dangerous_permissions(config: dict[str, Any]) -> None:
    """Warn whenever a run enables an explicit dangerous provider escape hatch."""
    dangerous_roles = [
        role for role in ("developer", "reviewer")
        if config.get(f"{role}_agent", {}).get("permission_policy", {}).get("allow_dangerous")
    ]
    if dangerous_roles:
        _status_print(
            "WARNING: dangerous provider permissions are enabled for "
            + ", ".join(dangerous_roles)
            + "; Agentic will record this exception and the isolated sandbox remains required.",
            file=sys.stderr,
            flush=True,
        )


def _auto_publish_enabled(config: dict[str, Any]) -> bool:
    automation = config.get("automation", {})
    return isinstance(automation, dict) and automation.get("auto_publish") is True


def _approved_result(folder: Path) -> bool:
    try:
        raw = read_text_bounded(folder / "result.json", MAX_RESULT_BYTES)
        result = json.loads(raw) if raw is not None else None
    except (OSError, TypeError, json.JSONDecodeError):
        return False
    return isinstance(result, dict) and result.get("status") == "APPROVED"


def _audit_has_terminal_state_marker(folder: Path) -> bool:
    """Reject audit publication when any other terminal state is recorded."""
    for name in ("blocked.json", "result.json"):
        try:
            # A malformed, oversized, unreadable, or non-regular marker is
            # still evidence that the audit state cannot be trusted.
            if os.path.lexists(folder / name):
                return True
        except OSError:
            return True
    return False


def _automatic_candidate_eligible(issue: Issue, folder: Path) -> bool:
    """Check durable completion evidence without touching the worktree."""
    if issue.is_audit:
        return (
            not _audit_has_terminal_state_marker(folder)
            and load_audit_report(folder, issue.audit_key or AUDIT_KEY) is not None
        )
    try:
        approval = load_publication_approval(folder)
    except (OSError, RuntimeError, ValueError):
        return False
    if approval is None or load_blocked(folder) is not None:
        return False
    if _approved_result(folder):
        return True
    # Standalone review and manual completion both record approval without a
    # developer result marker. A pre-existing failed/blocked result never
    # qualifies, while the approval itself remains subject to publication's
    # exact branch, issue, base, and patch validation.
    return not (folder / "result.json").exists() and approval.get("kind") in {
        "manual", "automated",
    }


def _attempt_auto_publication(
    *, run: Run, issue: Issue, github_repo: str | None, github_host: str,
    base: str, config: dict[str, Any], folder: Path,
    offline: bool = False,
    candidates: list[tuple[Run, Path]] | None = None,
) -> None:
    """Attempt opt-in publication while preserving a completed local run."""
    if not _auto_publish_enabled(config):
        return
    if offline:
        _status_print("[AUTO-PUBLISH] Skipped: offline mode")
        return
    if not github_repo:
        _status_print("[AUTO-PUBLISH] Skipped: no GitHub repository target")
        return
    ordered_candidates = candidates or [(run, folder)]
    selected = next(
        (
            (candidate_run, candidate_folder)
            for candidate_run, candidate_folder in ordered_candidates
            if _automatic_candidate_eligible(issue, candidate_folder)
        ),
        None,
    )
    if selected is None:
        _status_print("[AUTO-PUBLISH] Skipped: no eligible completed run")
        return
    run, folder = selected
    if issue.is_audit:
        try:
            url = publish_audit_issue(
                run, issue, github_repo, github_host, folder, auto=True,
            )
        except Exception as exc:  # publication is best-effort after completion
            _status_print(f"[AUTO-PUBLISH] Failed: {exc}", file=sys.stderr)
            return
        if url:
            _status_print(f"[AUTO-PUBLISH] Completed: GitHub issue: {url}")
        return
    try:
        approval = load_publication_approval(folder)
        if approval is None:
            _status_print("[AUTO-PUBLISH] Skipped: no valid publication approval")
            return
        url = publish(
            run, issue, github_repo, base, config, folder,
            test_commands=config["test_commands"],
            auto=True,
        )
    except Exception as exc:  # publication is best-effort after completion
        _status_print(f"[AUTO-PUBLISH] Failed: {exc}", file=sys.stderr)
        return
    if url:
        _status_print(f"[AUTO-PUBLISH] Completed: Draft PR: {url}")


PROVIDER_SANDBOX_ENV = _agentic_config.PROVIDER_SANDBOX_ENV
DEFAULT_OPENROUTER_COMMAND_TIMEOUT = _agentic_config.DEFAULT_OPENROUTER_COMMAND_TIMEOUT
OPENROUTER_MAX_TOOL_TURNS = _agentic_config.OPENROUTER_MAX_TOOL_TURNS
OPENROUTER_COMMAND_TIMEOUT = _agentic_config.OPENROUTER_COMMAND_TIMEOUT
DEFAULT_DEVELOPER_TIMEOUT = _agentic_config.DEFAULT_DEVELOPER_TIMEOUT
MAX_TASK_TIMEOUT = _agentic_config.MAX_TASK_TIMEOUT
BRANCH_RE = re.compile(r"^agent/issue-(\d+)-([a-f0-9]{8})$")
LOCAL_BRANCH_RE = re.compile(
    r"^agent/task-(?P<slug>[a-z0-9-]+)-(?P<key>[a-f0-9]{12})(?:-(?P<ident>[a-f0-9]{8}))?$"
)
RELATIVE_WORKTREE_GIT = (2, 48, 0)
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "skipped": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approved", "summary", "findings", "skipped"],
    "additionalProperties": False,
}

INVESTIGATION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "findings"],
    "additionalProperties": False,
}

FINDING_SEVERITIES = (
    "BLOCKER", "CRITICAL", "MAJOR", "HIGH", "MEDIUM", "MINOR", "LOW",
)
AGENT_CONFIG_FILENAME = "agent_config.json"
AGENTS_FILENAME = "AGENTS.md"
DEFAULT_AGENTS = """# Agent instructions

## Working agreement

- Read the issue and existing code before making changes.
- Keep changes focused on the requested behavior.
- Add or update regression tests for behavior changes.
- Run the configured fast checks before handing work back.
- Do not commit, push, or publish from inside an Agentic developer run.

## Project-specific notes

Add architecture, testing, generated-file, and local-development notes here.
"""


from .worktrees import (  # noqa: E402, F401
    AUDIT_KEY,
    choose_existing,
    create_audit_worktree,
    create_local_worktree,
    create_worktree,
    default_base_branch,
    default_state_dir,
    ensure_state_directory,
    discover_runs,
    ensure_state_paths,
    eligible,
    github_metadata,
    github_repo_from_remote,
    installed_git_version,
    issue_fingerprint,
    legacy_runs_dir,
    legacy_worktrees_dir,
    load_local_task,
    probe,
    repository_lock,
    repository_state_key,
    runs_for_issue,
    runs_for_audit,
    runs_for_task,
    run_for_branch,
    get_pull_request,
    state_paths,
    UserCancelled,
    worktree_lock,
    worktree_path_lock,
)


def relative_worktree_paths_supported() -> bool:
    """Check Git compatibility through the historical patchable boundary."""
    current = installed_git_version()
    if current is None or current < RELATIVE_WORKTREE_GIT:
        return False
    if os.name == "nt" or not os.environ.get("WSL_DISTRO_NAME"):
        return True
    windows_git = shutil.which("git.exe")
    windows_version = installed_git_version(windows_git) if windows_git else None
    return windows_version is not None and windows_version >= RELATIVE_WORKTREE_GIT


def configure_relative_worktree_paths(repo: Path) -> bool:
    if not relative_worktree_paths_supported():
        return False
    git(repo, "config", "worktree.useRelativePaths", "true")
    return True


def repair_relative_worktree(repo: Path, worktree: Path) -> bool:
    if not configure_relative_worktree_paths(repo):
        return False
    git(repo, "worktree", "repair", "--relative-paths", str(worktree))
    return True


def command(
    argv: list[str], cwd: Path, *, timeout: int = 180,
    check: bool = True, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Compatibility wrapper keeping command patching at the CLI boundary."""
    return _agentic_worktrees.command(argv, cwd, timeout=timeout, check=check, env=env)


def git(repo: Path, *args: str, check: bool = True) -> str:
    return command(["git", *args], repo, check=check).stdout.strip()


def refresh_base(repo: Path, base: str) -> None:
    git(repo, "fetch", "origin", base)


def _github_command(host: str, *args: str) -> list[str]:
    """Build GitHub CLI argv; the caller supplies the host per process."""
    return ["gh", *args]


def _github_environment(host: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment["GH_HOST"] = host
    return environment


def _github_failure_kind(detail: str) -> str:
    """Classify a GitHub CLI failure without exposing its output."""
    lowered = detail.casefold()
    if any(marker in lowered for marker in (
        "tls handshake", "connection", "network", "timeout", "timed out",
        "dial tcp", "no such host", "temporary failure", "certificate",
        "x509", "unknown authority", "certificate verify failed",
    )):
        return "connectivity"
    if any(marker in lowered for marker in (
        "authentication", "not logged in", "bad credentials", "invalid token",
        "unauthorized", "401",
    )):
        return "authentication"
    return "github"


def _github_preflight(repo: Path, github_host: str) -> None:
    """Verify the active GitHub CLI credential before a repair starts."""
    gh_path = shutil.which("gh")
    if not gh_path:
        raise RuntimeError(
            "CI repair requires the GitHub CLI (`gh`), but it was not found on PATH"
        )
    authenticated, detail = _agentic_worktrees.probe(
        [gh_path, "auth", "status", "--active"],
        repo,
        timeout=30,
        env=_github_environment(github_host),
    )
    if authenticated:
        return
    kind = _github_failure_kind(detail)
    if kind == "connectivity":
        raise RuntimeError(
            f"GitHub API connectivity check failed for {github_host}; "
            "check the network, VPN, or proxy and retry"
        )
    if kind == "authentication":
        raise RuntimeError(
            f"GitHub CLI authentication failed for {github_host}; "
            "run `gh auth login` or provide a valid GH_TOKEN and retry"
        )
    raise RuntimeError(
        f"GitHub CLI authentication could not be verified for {github_host}; "
        "run `gh auth status` for details"
    )


def _github_operation_error(action: str, github_host: str, exc: RuntimeError) -> RuntimeError:
    """Turn a GitHub CLI failure into a bounded, actionable diagnostic."""
    kind = _github_failure_kind(str(exc))
    if kind == "connectivity":
        return RuntimeError(
            f"GitHub API connectivity failed while {action} on {github_host}; "
            "check the network, VPN, or proxy and retry"
        )
    if kind == "authentication":
        return RuntimeError(
            f"GitHub CLI authentication failed while {action} on {github_host}; "
            "run `gh auth status` or provide a valid GH_TOKEN and retry"
        )
    return exc


def fetch_issues(
    repo: Path, github_repo: str, github_host: str = DEFAULT_GITHUB_HOST,
) -> list[Issue]:
    raw = json.loads(command(_github_command(
        github_host, "issue", "list", "--repo", github_repo, "--state", "open", "--limit", "100",
        "--json", "number,title,body,state",
    ), repo, env=_github_environment(github_host)).stdout)
    return sorted(
        (issue for item in raw if eligible(issue := Issue.parse(item))),
        key=lambda issue: issue.number if issue.number is not None else 0,
    )


def get_issue(
    repo: Path, github_repo: str, number: int, github_host: str = DEFAULT_GITHUB_HOST,
) -> Issue:
    raw = json.loads(command(_github_command(
        github_host, "issue", "view", str(number), "--repo", github_repo,
        "--json", "number,title,body,state",
    ), repo, env=_github_environment(github_host)).stdout)
    return Issue.parse(raw)

def logs_path(repo: Path, run: Run, state_dir: Path | None = None) -> Path:
    """Return preferred run logs, falling back to the pre-centralized location."""
    paths = state_paths(repo, state_dir) if state_dir else None
    preferred = (paths.runs if paths else legacy_runs_dir(repo)) / run.branch.replace("/", "-")
    legacy = legacy_runs_dir(repo) / run.branch.replace("/", "-")
    folder = legacy if state_dir and legacy.is_dir() and not preferred.exists() else preferred
    if folder == preferred and state_dir:
        ensure_state_paths(repo, state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _confirm_ci_repair(pr_number: int, classification: str) -> bool:
    """Require explicit consent for failures that may not be code-repairable."""
    from .shell_input import _interactive_line

    try:
        answer = _interactive_line(
            f"\n[CI-REPAIR] Failure classified as {classification}; attempt code repair for PR "
            f"#{pr_number}? [y/N]: ",
            system_prefix="CI-REPAIR",
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().casefold() in {"y", "yes", "repair"}


def _ci_repair_secret_values(config: dict[str, Any]) -> tuple[str, ...]:
    """Resolve configured credential values only for in-memory redaction."""
    names: set[str] = set()
    for role in ("developer_agent", "reviewer_agent"):
        settings = config.get(role)
        if isinstance(settings, dict) and isinstance(settings.get("api_key_env"), str):
            names.add(settings["api_key_env"])
    sandbox = config.get("sandbox")
    if isinstance(sandbox, dict):
        names.update(name for name in sandbox.get("pass_env", ()) if isinstance(name, str))
    return tuple(value for name in names if (value := os.environ.get(name)) and len(value) >= 8)


def repair_ci(
    repo: Path,
    pr_number: int,
    config: dict[str, Any],
    *,
    state_dir: Path | None = None,
    verbose: bool = False,
    clean: bool = False,
) -> int:
    """Repair failed CI in the exact existing PR worktree, with bounded retries."""
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise RuntimeError("--pr must be a positive pull request number")
    github_host = config.get("github_host", DEFAULT_GITHUB_HOST)
    github_repo = github_repo_from_remote(repo, github_host)
    if not github_repo:
        raise RuntimeError("CI repair requires a GitHub origin remote for the target repository")
    _github_preflight(repo, github_host)
    _status_print(f"[CI-REPAIR] GitHub CLI authentication verified for {github_host}")
    _status_print(f"[CI-REPAIR] Reading PR #{pr_number} metadata")
    try:
        pull_request = get_pull_request(repo, github_repo, pr_number, github_host)
    except RuntimeError as exc:
        raise _github_operation_error("reading PR metadata", github_host, exc) from exc
    _status_print(f"[CI-REPAIR] PR #{pr_number} metadata loaded; verifying worktree")
    if pull_request.state.casefold() != "open":
        raise RuntimeError(f"Pull request #{pr_number} is not open; CI repair is blocked")
    run = find_worktree_for_branch(repo, pull_request.head)
    folder = logs_path(repo, run, state_dir)
    # Keep validation, fingerprinting, and optional stashing in one critical
    # section so another Agentic operation cannot interleave a worktree change.
    with worktree_lock(repo, run, state_dir):
        verify_worktree_for_pr(run, pull_request, allow_dirty=True)
        dirty_status = git(
            run.worktree, "status", "--porcelain=v1", "--untracked-files=all", check=False,
        )
        cleaned = bool(clean and dirty_status)
        if cleaned:
            stash_dirty_worktree_for_repair(run, pr_number)
            _status_print(
                "[CI-REPAIR] Stashed the previous dirty worktree; starting a fresh repair"
            )
            verify_worktree_for_pr(run, pull_request)
        dirty_fingerprint = worktree_change_fingerprint(run.worktree)
        resume_dirty = can_resume_dirty_worktree(
            folder, pr_number, pull_request.head, pull_request.head_sha, run.worktree,
            current_fingerprint=dirty_fingerprint,
        )
        verify_worktree_for_pr(run, pull_request, allow_dirty=resume_dirty)
    if resume_dirty:
        _status_print(
            "[CI-REPAIR] Resuming the preserved patch from the previous repair attempt"
        )
    record_issue_metadata(
        folder, number=pr_number, title=f"PR #{pr_number}: {pull_request.title}",
        state=pull_request.state, source="github-pr", body=pull_request.body,
        shell_job_id=os.environ.get("AGENTIC_SHELL_JOB_ID"),
    )
    record_security_policy(folder, config)
    secret_values = _ci_repair_secret_values(config)

    def record_event(event: dict[str, Any]) -> None:
        record_ci_repair_event(folder, event, extra_values=secret_values)

    record_event({
        "event": "repair_started", "pr": pr_number, "branch": pull_request.head,
        "head_sha": pull_request.head_sha, "worktree": str(run.worktree),
        "resumed": resume_dirty, "cleaned": cleaned,
    })
    issue = Issue(
        pr_number, f"Repair CI for PR #{pr_number}: {pull_request.title}",
        pull_request.body, state="OPEN", source="github",
    )
    limits = config.get("ci_repair", {})
    max_attempts = int(limits.get("max_attempts", 2))
    poll_interval = int(limits.get("poll_interval_seconds", 15))
    poll_timeout = int(limits.get("poll_timeout_seconds", 900))
    auto_confirm_non_code_failures = limits.get("auto_confirm_non_code_failures", False) is True
    deadline = time.monotonic() + int(config.get("task_timeout_seconds", DEFAULT_TASK_TIMEOUT))
    _status_print(f"[CI-REPAIR] Fetching failed CI evidence for PR #{pr_number}")
    evidence = fetch_ci_evidence(
        repo, github_repo, pull_request, github_host, extra_values=secret_values,
    )
    _status_print(
        f"[CI-REPAIR] CI evidence collected; failure classified as {evidence.classification}"
    )
    record_event({
        "event": "ci_evidence", "attempt": 0, "classification": evidence.classification,
        "evidence": evidence.to_dict(),
    })
    if evidence.classification in {"infrastructure", "permission", "ambiguous"}:
        if auto_confirm_non_code_failures:
            _status_print(
                f"[CI-REPAIR] Auto-confirming {evidence.classification} failure "
                "per ci_repair.auto_confirm_non_code_failures"
            )
            record_event({
                "event": "repair_confirmation_skipped",
                "reason": "ci_repair.auto_confirm_non_code_failures",
                "classification": evidence.classification,
            })
        elif not _confirm_ci_repair(pr_number, evidence.classification):
            record_event({
                "event": "repair_blocked", "reason": "explicit confirmation not granted",
                "classification": evidence.classification,
            })
            raise RuntimeError(
                f"CI failure classified as {evidence.classification}; no repair attempted without explicit confirmation"
            )

    for attempt in range(1, max_attempts + 1):
        if time.monotonic() >= deadline:
            raise RuntimeError("CI repair task timeout reached; preserved the PR worktree and logs")
        feedback = evidence.prompt_context()
        with worktree_lock(repo, run, state_dir):
            completed = develop(
                run, issue, pull_request.base, config, folder,
                initial_feedback=feedback,
                test_commands=test_commands_for_profile(config, "fast"),
                deadline=deadline, verbose=verbose,
            )
        attempt_event = {
            "event": "repair_attempt", "attempt": attempt, "completed": completed,
            "classification": evidence.classification,
        }
        if not completed:
            attempt_event["dirty_fingerprint"] = worktree_change_fingerprint(run.worktree)
            if attempt_event["dirty_fingerprint"] is None:
                attempt_event["dirty_fingerprint_unavailable"] = True
        record_event(attempt_event)
        if not completed:
            raise RuntimeError(
                f"CI repair attempt {attempt}/{max_attempts} did not pass local checks and review; "
                f"inspect the durable logs at {folder}"
            )
        if not _auto_publish_enabled(config) and not _confirm_ci_repair(pr_number, "reviewed code/test patch"):
            record_event({"event": "push_blocked", "attempt": attempt})
            _status_print("[CI-REPAIR] Patch prepared and reviewed; push cancelled")
            return 0
        record_event({
            "event": "push_authorized", "attempt": attempt,
            "automatic": _auto_publish_enabled(config),
        })
        with worktree_lock(repo, run, state_dir):
            pushed_head_sha = push_existing_pr(
                run, issue, github_repo, pull_request.base, config, folder, pull_request,
            )
        if not isinstance(pushed_head_sha, str) or not pushed_head_sha:
            raise RuntimeError("Publication did not return the pushed PR head; refusing to monitor unbound CI")
        outcome = poll_ci(
            repo, github_repo, pull_request, github_host,
            timeout_seconds=min(poll_timeout, max(1, int(deadline - time.monotonic()))),
            interval_seconds=poll_interval,
            expected_head_sha=pushed_head_sha,
        )
        record_event({
            "event": "ci_outcome", "attempt": attempt, "outcome": outcome,
            "head_sha": pushed_head_sha,
        })
        if outcome == "passed":
            _status_print(f"[CI-REPAIR] PR #{pr_number} CI passed after attempt {attempt}")
            return 0
        if outcome == "timeout":
            raise RuntimeError(
                f"CI remained pending beyond the configured polling bound; inspect PR #{pr_number} and logs at {folder}"
            )
        if outcome == "head_changed":
            raise RuntimeError(
                f"PR #{pr_number} no longer reports the pushed head; refusing to accept unrelated CI results"
            )
        if attempt >= max_attempts:
            raise RuntimeError(
                f"CI still failed after {max_attempts} bounded repair attempt(s); logs: {folder}"
            )
        updated = get_pull_request(repo, github_repo, pr_number, github_host)
        if updated.head_sha != pushed_head_sha:
            raise RuntimeError(
                f"PR #{pr_number} changed after the monitored push; refusing to repair an unrelated head"
            )
        pull_request = updated
        verify_worktree_for_pr(run, pull_request)
        evidence = fetch_ci_evidence(
            repo, github_repo, pull_request, github_host, extra_values=secret_values,
        )
        record_event({
            "event": "ci_evidence", "attempt": attempt, "classification": evidence.classification,
            "evidence": evidence.to_dict(),
        })
        if evidence.classification in {"infrastructure", "permission", "ambiguous"}:
            if auto_confirm_non_code_failures:
                _status_print(
                    f"[CI-REPAIR] Auto-confirming follow-up {evidence.classification} failure "
                    "per ci_repair.auto_confirm_non_code_failures"
                )
            elif not _confirm_ci_repair(pr_number, evidence.classification):
                raise RuntimeError(
                    f"Follow-up CI failure classified as {evidence.classification}; explicit confirmation is required"
                )
    raise RuntimeError("CI repair ended without a terminal result")


MANUAL_APPROVAL_FILENAME = _agentic_workflow_state.MANUAL_APPROVAL_FILENAME
REVIEW_APPROVAL_FILENAME = _agentic_workflow_state.REVIEW_APPROVAL_FILENAME
BLOCKED_FILENAME = "blocked.json"


manual_approval_path = _agentic_workflow_state.manual_approval_path
review_approval_path = _agentic_workflow_state.review_approval_path


def blocked_path(folder: Path) -> Path:
    """Return the durable deferred-run marker path."""
    return folder / BLOCKED_FILENAME


def load_manual_approval(folder: Path) -> dict[str, Any] | None:
    """Load a manual completion record, rejecting corrupt approval state."""
    return _agentic_workflow_state.load_manual_approval(folder)


def record_manual_approval(run: Run, issue: Issue, base: str, config: dict[str, Any],
                           folder: Path, reason: str) -> dict[str, Any]:
    """Record a human completion decision tied to the exact current patch."""
    reason = reason.strip() or "Manually marked done by user"
    base_sha, digest, summary = prepare_patch(run.worktree, base, config["max_files"])
    approval = _agentic_workflow_state.record_manual_approval(
        run, issue, base, base_sha, digest, folder, reason,
    )
    path = manual_approval_path(folder)
    _status_print(f"[MANUAL] Marked complete: {summary.strip() or '(patch recorded)'}")
    _status_print(f"[MANUAL] Approval record: {path}")
    return approval


def record_review_approval(run: Run, issue: Issue, base: str, base_sha: str,
                           patch_digest: str, verdict: dict[str, Any],
                           config: dict[str, Any], folder: Path) -> dict[str, Any]:
    """Persist automated approval for the exact patch that was reviewed."""
    return _agentic_workflow_state.record_review_approval(
        run, issue, base, base_sha, patch_digest, verdict, config, folder,
    )


def load_review_approval(folder: Path) -> dict[str, Any] | None:
    """Load an automated approval, rejecting corrupt approval state."""
    return _agentic_workflow_state.load_review_approval(folder)


def load_publication_approval(folder: Path) -> dict[str, Any] | None:
    """Load the newest human or automated approval available for publication."""
    return _agentic_workflow_state.load_publication_approval(
        folder,
        manual_loader=load_manual_approval,
        review_loader=load_review_approval,
    )


def load_resume_feedback(folder: Path) -> str:
    """Load the latest failed review and test evidence for a resumed run."""
    chunks: list[str] = []
    review_log = _latest_numbered_log(folder, "review", "json")
    if review_log:
        raw = redact_secrets(read_text_tail(review_log, MAX_RESUME_LOG_BYTES))
        try:
            verdict = json.loads(raw)
        except json.JSONDecodeError:
            verdict = None
        if isinstance(verdict, dict):
            if verdict.get("approved") is not True:
                chunks.append("Previous independent review (untrusted evidence):\n" +
                              json.dumps(verdict, indent=2))
        elif raw.strip():
            chunks.append("Previous review output (untrusted evidence):\n" + raw)

    test_log = _latest_numbered_log(folder, "tests", "txt")
    if test_log is None:
        test_log = _latest_numbered_log(folder, "test-command", "txt")
    if test_log:
        raw = redact_secrets(read_text_tail(test_log, MAX_RESUME_LOG_BYTES))
        if raw.strip():
            chunks.append("Previous test evidence (untrusted output):\n" + raw)
    recurring = review_history_feedback(folder)
    if recurring:
        chunks.append(recurring)
    return redact_secrets("\n\n".join(chunks)[-9000:])





# Compatibility wrappers preserve patchable agentic.<name> seams.
from . import workflow as _agentic_workflow  # noqa: E402

# Keep the historical public exception tied to the implementation raised by
# the workflow module.  Callers catching ``agentic.NoCodeChangesError`` must
# also catch errors raised by the extracted workflow implementation.
NoCodeChangesError = _agentic_workflow.NoCodeChangesError


def _workflow_call(name: str, *args, **kwargs):
    _agentic_workflow.bind(globals())
    return getattr(_agentic_workflow, name)(*args, **kwargs)

def run_agent(*args, **kwargs):
    return _workflow_call("run_agent", *args, **kwargs)

def run_tests(*args, **kwargs):
    return _workflow_call("run_tests", *args, **kwargs)

def preflight_test_commands(*args, **kwargs):
    return _workflow_call("preflight_test_commands", *args, **kwargs)

def retryable_test_failure(*args, **kwargs):
    return _workflow_call("retryable_test_failure", *args, **kwargs)

def pending_files(*args, **kwargs):
    return _workflow_call("pending_files", *args, **kwargs)

def _common_repository_root(*args, **kwargs):
    return _workflow_call("_common_repository_root", *args, **kwargs)

def _runner_source_change_allowed(*args, **kwargs):
    return _workflow_call("_runner_source_change_allowed", *args, **kwargs)

def _publication_forbidden(*args, **kwargs):
    return _workflow_call("_publication_forbidden", *args, **kwargs)

def base_revision(*args, **kwargs):
    return _workflow_call("base_revision", *args, **kwargs)

def prepare_patch(*args, **kwargs):
    return _workflow_call("prepare_patch", *args, **kwargs)

def investigate_issue(*args, **kwargs):
    return _workflow_call("investigate_issue", *args, **kwargs)

def _latest_investigation(*args, **kwargs):
    return _workflow_call("_latest_investigation", *args, **kwargs)

def print_investigation_summary(*args, **kwargs):
    return _workflow_call("print_investigation_summary", *args, **kwargs)

def _run_investigation(*args, **kwargs):
    return _workflow_call("_run_investigation", *args, **kwargs)

def audit_repository(*args, **kwargs):
    return _workflow_call("audit_repository", *args, **kwargs)

def _run_audit(*args, **kwargs):
    return _workflow_call("_run_audit", *args, **kwargs)

def audit_feedback(*args, **kwargs):
    return _workflow_call("audit_feedback", *args, **kwargs)

def load_audit_report(*args, **kwargs):
    return _workflow_call("load_audit_report", *args, **kwargs)

def publish_audit_issue(*args, **kwargs):
    return _workflow_call("publish_audit_issue", *args, **kwargs)

def print_audit_summary(*args, **kwargs):
    return _workflow_call("print_audit_summary", *args, **kwargs)

def investigation_feedback(*args, **kwargs):
    return _workflow_call("investigation_feedback", *args, **kwargs)

def review(*args, **kwargs):
    return _workflow_call("review", *args, **kwargs)

def agent_prompt(*args, **kwargs):
    return _workflow_call("agent_prompt", *args, **kwargs)

def validate_developer_review_actions(*args, **kwargs):
    return _workflow_call("validate_developer_review_actions", *args, **kwargs)

def verify_and_review(*args, **kwargs):
    return _workflow_call("verify_and_review", *args, **kwargs)

def validate_publication_approval(*args, **kwargs):
    return _workflow_call("validate_publication_approval", *args, **kwargs)

def check_existing_pr(*args, **kwargs):
    return _workflow_call("check_existing_pr", *args, **kwargs)

def push_existing_pr(*args, **kwargs):
    return _workflow_call("push_existing_pr", *args, **kwargs)

def _clean_terminal_output(*args, **kwargs):
    return _workflow_call("_clean_terminal_output", *args, **kwargs)

def _validation_summary(*args, **kwargs):
    return _workflow_call("_validation_summary", *args, **kwargs)

def _change_summary(*args, **kwargs):
    return _workflow_call("_change_summary", *args, **kwargs)

def publish(*args, **kwargs):
    return _workflow_call("publish", *args, **kwargs)

def develop(*args, **kwargs):
    return _workflow_call("develop", *args, **kwargs)

def init_repository(repo: Path, *, force: bool = False, create_agents: bool = False,
                     provider: str = "codex", reviewer_provider: str | None = None,
                     security_mode: str = DEFAULT_INIT_SECURITY_MODE,
                     sandbox_image: str | None = None,
                     sandbox_dockerfile: str | None = None,
                     sandbox_context: str = ".",
                     sandbox_language: str | None = None,
                     discover_tests: bool = False,
                     discovery_timeout: int = DISCOVERY_TIMEOUT_SECONDS) -> int:
    """Create personal target-repository setup without overwriting it by default."""
    if security_mode not in SECURITY_MODES:
        raise ValueError(f"security_mode must be one of: {', '.join(SECURITY_MODES)}")
    repo = repo.resolve()
    root_result = probe(["git", "rev-parse", "--show-toplevel"], repo)
    if not root_result[0]:
        raise RuntimeError(f"{repo} is not a Git repository")
    root = Path(root_result[1].splitlines()[-1]).resolve()
    if root != repo:
        raise RuntimeError(f"Run init from the repository root or pass --repo {root}")
    config_path = repo / AGENT_CONFIG_FILENAME
    commands, detection = detect_test_commands(repo)
    detected_language, _ = detect_project_language(repo)
    sandbox_language = sandbox_language or detected_language
    if sandbox_language is not None and sandbox_language not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"sandbox language must be one of: {', '.join(SUPPORTED_LANGUAGES)}"
        )
    reviewer_provider = reviewer_provider or provider
    if security_mode == "isolated":
        sandbox_image = sandbox_image or os.environ.get("AGENTIC_SANDBOX_IMAGE")
        if not sandbox_image and not sandbox_dockerfile:
            raise ValueError(
                "isolated init requires --sandbox-image, --sandbox-dockerfile, or "
                "AGENTIC_SANDBOX_IMAGE; "
                "use --security-mode trusted when host execution is appropriate"
            )
    else:
        sandbox_image = sandbox_image or DEFAULT_SANDBOX_IMAGE
    sandbox_image = sandbox_image or DEFAULT_SANDBOX_IMAGE
    developer_settings = default_provider_settings(provider)
    reviewer_settings = default_provider_settings(reviewer_provider)
    reviewer_settings["timeout_seconds"] = DEFAULT_REVIEWER_TIMEOUT
    sandbox = validate_sandbox({
        "enabled": security_mode == "isolated",
        "engine": "docker",
        "image": sandbox_image,
        "dockerfile": sandbox_dockerfile,
        "build_context": sandbox_context,
        "language": sandbox_language,
        "allow_network": False,
        "build_network": True,
        "pass_env": sandbox_env_for_providers(provider, reviewer_provider),
    })
    slow_commands: list[list[str]] = []
    discovery_result: DiscoveryResult | None = None
    discovery_credential_names = list(sandbox.get("pass_env", []))
    for settings in (developer_settings, reviewer_settings):
        name = api_key_env_for_provider(
            str(settings.get("provider", provider)), settings.get("api_key_env"),
        )
        if name and name not in discovery_credential_names:
            discovery_credential_names.append(name)
    if discover_tests and (
        not isinstance(discovery_timeout, int)
        or isinstance(discovery_timeout, bool)
        or not 1 <= discovery_timeout <= MAX_DISCOVERY_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "discovery timeout must be from 1 to "
            f"{MAX_DISCOVERY_TIMEOUT_SECONDS} seconds"
        )
    skip_protected_discovery = discover_tests and config_path.exists() and not force
    if discover_tests and not skip_protected_discovery:
        # Isolated discovery must fail closed before a provider or candidate
        # command can run. Trusted discovery filters credential-like host
        # variables before candidate validation; it is not full isolation.
        if security_mode == "isolated":
            preflight_security(security_mode, sandbox, repo=repo)
        if not _confirm_discovery_execution():
            _status_print("[INIT] Discovery declined; no repository checks were executed")
            return 0
        agent_settings = default_provider_settings(provider)
        # Discovery is deliberately bounded even when a provider's normal
        # developer timeout is much larger.
        agent_settings["timeout_seconds"] = min(
            int(agent_settings.get("timeout_seconds") or discovery_timeout),
            discovery_timeout,
        )
        with tempfile.TemporaryDirectory(prefix="agentic-discovery-") as temp:
            discovery_result = discover_and_validate(
                repo,
                agent_runner=run_agent,
                agent_settings=agent_settings,
                output=Path(temp) / "agent-message.txt",
                timeout=discovery_timeout,
                sandbox=sandbox,
                credential_names=discovery_credential_names,
            )
        _print_discovery_results(discovery_result)
        if discovery_result.agent_status:
            _status_print(f"[DISCOVERY] {discovery_result.agent_status}")
        if not discovery_result.fast_commands:
            _status_print(
                "[DISCOVERY] No passing fast checks were found; configuration was not written",
                file=sys.stderr,
            )
            return 1
        if not _confirm_discovered_configuration():
            _status_print("[INIT] Discovery declined; no configuration was written")
            return 0
        commands = discovery_result.fast_commands
        slow_commands = discovery_result.slow_commands
        detection = "Assisted test discovery"
    config = {
        "task_source": "github",
        "github_host": DEFAULT_GITHUB_HOST,
        "automation": {"auto_publish": False},
        "test_commands": commands,
        "slow_test_commands": slow_commands,
        "max_retries": 3,
        "ci_repair": {
            "max_attempts": 2,
            "poll_interval_seconds": 15,
            "poll_timeout_seconds": 900,
            "auto_confirm_non_code_failures": False,
        },
        "max_files": None,
        "test_timeout_seconds": DEFAULT_TEST_TIMEOUT,
        "task_timeout_seconds": DEFAULT_TASK_TIMEOUT,
        "system_prompt": None,
        "permission_policy": {
            "developer": default_permission_policy("developer"),
            "reviewer": default_permission_policy("reviewer"),
        },
        "developer": developer_settings,
        "reviewer": reviewer_settings,
        "security_mode": security_mode,
        "sandbox": sandbox,
    }
    if config_path.exists() and not force:
        _status_print(f"[INIT] Keeping existing {config_path}")
    else:
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        _status_print(f"[INIT] Wrote {config_path}")
    exclude_path, added = _append_git_exclude_entry(repo, AGENT_CONFIG_FILENAME)
    if added:
        _status_print(f"[INIT] Added {AGENT_CONFIG_FILENAME} to {exclude_path}")
    agents_path = repo / AGENTS_FILENAME
    if create_agents:
        if agents_path.exists() and not force:
            _status_print(f"[INIT] Keeping existing {agents_path}")
        else:
            agents_path.write_text(DEFAULT_AGENTS, encoding="utf-8")
            _status_print(f"[INIT] Wrote {agents_path}")
    _status_print(f"[INIT] {detection}: {' '.join(commands[0])}")
    if commands == [list(SAFE_GIT_DIFF_CHECK)]:
        _status_print("[INIT] Warning: replace the starter check with the project's real test/build commands")
    _status_print(f"[INIT] Providers: developer={provider}, reviewer={reviewer_provider}")
    if security_mode == "isolated":
        _status_print(
            "[INIT] Security mode: isolated; verify the Docker image, provider tools, "
            "credentials, and network policy with agentic doctor"
        )
    else:
        _status_print(
            "[INIT] Warning: trusted mode runs provider CLIs and checks with host "
            "account permissions; use --security-mode isolated for untrusted code"
        )
    _status_print("[INIT] Review agent_config.json before the first issue run")
    return 0


def _print_discovery_results(result: DiscoveryResult) -> None:
    """Print bounded candidate evidence without including command output."""
    _status_print("[DISCOVERY] Candidate checks:")
    for item in result.results:
        status = str(item.get("status", ""))
        label = {
            "passed": "SLOW" if str(item.get("profile", "")) == "slow" else "OK",
            "missing-dependency": "SKIP",
            "timeout": "TIMEOUT",
            "failed": "FAIL",
        }.get(status, "SKIP")
        detail = str(item.get("detail", ""))[:240]
        _status_print(f"  [{label:<7}] {item.get('display', '')} — {detail}")


def _confirm_discovered_configuration() -> bool:
    try:
        answer = input("Save these commands to agent_config.json? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


def _confirm_discovery_execution() -> bool:
    """Require consent before running commands derived from repository metadata."""
    try:
        answer = input(
            "Run test candidates discovered from repository metadata? "
            "They may execute project-defined scripts. [y/N]: "
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


def doctor(
    repo: Path,
    config_path: Path | None = None,
    *,
    json_output: bool = False,
    run_commands: bool = False,
    profile: str = "fast",
    worktree: Path | None = None,
) -> int:
    """Compatibility wrapper for the extracted diagnostics workflow."""
    return _run_doctor(
        repo,
        config_path,
        json_output=json_output,
        run_commands=run_commands,
        profile=profile,
        worktree=worktree,
        command_lock=(lambda path: worktree_path_lock(repo, path)) if run_commands else None,
        installed_git_version=installed_git_version,
        probe=probe,
        preflight_test_commands=preflight_test_commands,
        relative_worktree_paths_supported=relative_worktree_paths_supported,
    )


def _run_log_folder(repo: Path, run: Run, state_dir: Path | None = None) -> Path:
    preferred = (state_paths(repo, state_dir).runs if state_dir else legacy_runs_dir(repo)) / run.branch.replace("/", "-")
    legacy = legacy_runs_dir(repo) / run.branch.replace("/", "-")
    return legacy if state_dir and legacy.is_dir() and not preferred.exists() else preferred


def cleanup(repo: Path, *, issue: int | None = None, all_runs: bool = False,
            confirm: bool = False, remove_logs: bool = False,
            state_dir: Path | None = None) -> int:
    """Preview or remove clean Agentic worktrees while preserving dirty ones."""
    if issue is None and not all_runs:
        raise RuntimeError("cleanup requires --issue N or --all")
    if remove_logs and not confirm:
        raise RuntimeError("--remove-logs requires --yes; cleanup is a dry run by default")
    runs = discover_runs(repo)
    if issue is not None:
        runs = [run for run in runs if run.issue_number == issue]
    if not runs:
        _status_print("No matching Agentic worktrees.")
        return 0
    removed = 0
    for run in runs:
        dirty = bool(git(run.worktree, "status", "--porcelain", "--untracked-files=all", "--ignored"))
        folder = _run_log_folder(repo, run, state_dir)
        if dirty:
            _status_print(f"[SKIP] {run.branch}: worktree has uncommitted changes ({run.worktree})")
            continue
        if not confirm:
            _status_print(f"[DRY RUN] would remove clean worktree: {run.worktree}")
            if remove_logs:
                _status_print(f"[DRY RUN] would remove logs: {folder}")
            continue
        try:
            git(repo, "worktree", "remove", str(run.worktree))
        except RuntimeError as exc:
            _status_print(f"[SKIP] {run.branch}: {exc}")
            continue
        if remove_logs and folder.is_dir():
            shutil.rmtree(folder)
            _status_print(f"[CLEAN] removed logs: {folder}")
        _status_print(f"[CLEAN] removed worktree: {run.worktree}")
        removed += 1
    if not confirm:
        _status_print("Nothing removed. Re-run with --yes after inspecting the list.")
    else:
        _status_print(f"Removed {removed} worktree(s); uncommitted work was preserved.")
    return 0


def run_status_record(repo: Path, run: Run) -> dict[str, Any]:
    dirty = bool(git(run.worktree, "status", "--porcelain", "--untracked-files=all"))
    return {
        "issue": run.issue_number,
        "task_key": run.task_key,
        "audit_key": run.audit_key,
        "branch": run.branch,
        "worktree": str(run.worktree),
        "status": "changed" if dirty else "clean",
        "dirty": dirty,
    }


def show_runs(repo: Path, *, json_output: bool = False) -> None:
    runs = discover_runs(repo)
    if json_output:
        print(json.dumps([run_status_record(repo, run) for run in runs], indent=2))
        return
    if not runs:
        _status_print("No existing agent worktrees.")
        return
    for run in runs:
        record = run_status_record(repo, run)
        label = (
            f"#{run.issue_number}" if run.issue_number is not None
            else f"audit:{run.audit_key[:8]}" if run.audit_key
            else f"local:{(run.task_key or 'unknown')[:8]}"
        )
        _status_print(f"{label:<16} {run.branch:<44} [{record['status']}] {run.worktree}")


def show_issues(repo: Path, issues: list[Issue]) -> None:
    ongoing = {r.issue_number for r in discover_runs(repo)}
    if not issues:
        _status_print("No open GitHub issues found.")
    for issue in issues:
        marker = " [RESUME]" if issue.number in ongoing else ""
        _status_print(f"#{issue.number:<5} {issue.title[:75]}{marker}")


def select_issue(issues: list[Issue]) -> int:
    show = {issue.number for issue in issues}
    if not show:
        raise RuntimeError("No open issues to select; use --issue N for an existing worktree")
    value = input("\nChoose GitHub issue number: #").strip()
    if not value.isdigit() or int(value) not in show:
        raise RuntimeError("Please select an open issue number")
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "command", nargs="?", choices=("help", "shell", "dashboard", "audit", "init", "doctor", "cleanup", "status", "runs"),
        help="Show help or route to a built-in subcommand",
    )
    parser.add_argument("--repo", default=str(Path.cwd()), help="Project root (default: current directory)")
    parser.add_argument(
        "--state-dir", default=None,
        help="Agentic state root (default: user state directory; use a shared /mnt drive for WSL/Windows)",
    )
    parser.add_argument("--interactive", action="store_true", help="Open the interactive background-job shell")
    parser.add_argument(
        "--config", default=None,
        help="Configuration file (default: agent_config.json in the target repository)",
    )
    parser.add_argument(
        "--profile", choices=("fast", "slow"), default="fast",
        help="Test profile: fast checks by default, or fast plus slow_test_commands",
    )
    parser.add_argument(
        "--workflow-mode", choices=("quick", "standard", "strict"), default=None,
        help="Workflow depth: quick skips low-risk investigation; standard is the default; strict always investigates",
    )
    parser.add_argument("--issue", type=int, help="GitHub issue number")
    parser.add_argument("--pr", type=int, dest="pr_number", help="Existing GitHub pull request number")
    parser.add_argument(
        "--repair-ci", action="store_true",
        help="Inspect failed CI and repair an existing pull request in its worktree",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="With --repair-ci, stash dirty worktree changes before a fresh repair",
    )
    parser.add_argument(
        "--instruction", default=None,
        help="Optional untrusted instruction preserved for this run and its resumes",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Resume an existing GitHub run from its bounded issue snapshot without GitHub API calls",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Keep detailed provider and command progress in human-readable logs",
    )
    parser.add_argument("--task", help="Local task text (used when task_source is local)")
    parser.add_argument("--task-file", type=Path, help="File containing a local task (used when task_source is local)")
    parser.add_argument("--list", action="store_true", help="List open issues")
    parser.add_argument("--runs", action="store_true", help="List existing worktrees without contacting GitHub")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON for status/runs output")
    parser.add_argument(
        "--commands", action="store_true",
        help="With doctor, run configured checks in the selected worktree (opt-in; may mutate it)",
    )
    parser.add_argument(
        "--worktree", default=None,
        help="With doctor --commands, run checks from this worktree (default: --repo)",
    )
    parser.add_argument("--new", action="store_true", help="Make another worktree even if one already exists")
    parser.add_argument(
        "--resume", action="store_true",
        help="With audit, reuse an existing audit worktree and report",
    )
    parser.add_argument("--force", action="store_true", help="Allow init to replace generated setup files")
    parser.add_argument(
        "--discover-tests", action="store_true",
        help="With init, discover, validate, classify, and confirm test commands before saving",
    )
    parser.add_argument(
        "--discovery-timeout", type=int, default=DISCOVERY_TIMEOUT_SECONDS,
        help="With --discover-tests, maximum seconds for each candidate check (default: 60)",
    )
    parser.add_argument("--agents", action="store_true", help="With init, create a starter AGENTS.md")
    parser.add_argument(
        "--provider", choices=SUPPORTED_PROVIDERS, default="codex",
        help="With init, provider for the developer and (by default) reviewer",
    )
    parser.add_argument(
        "--reviewer-provider", choices=SUPPORTED_PROVIDERS, default=None,
        help="With init, use a different provider for the independent reviewer",
    )
    parser.add_argument(
        "--security-mode", choices=SECURITY_MODES, default=DEFAULT_INIT_SECURITY_MODE,
        help="With init, choose trusted host execution or explicitly opt into isolated container execution",
    )
    parser.add_argument(
        "--secure", action="store_true",
        help="With init, shorthand for --security-mode isolated",
    )
    parser.add_argument(
        "--sandbox-image", default=None,
        help="With isolated init, container image to use (or set AGENTIC_SANDBOX_IMAGE)",
    )
    parser.add_argument(
        "--sandbox-dockerfile", default=None,
        help="With isolated init, build the sandbox image from this repository-relative Dockerfile",
    )
    parser.add_argument(
        "--sandbox-context", default=".",
        help="With --sandbox-dockerfile, repository-relative Docker build context (default: .)",
    )
    parser.add_argument(
        "--sandbox-language", choices=SUPPORTED_LANGUAGES, default=None,
        help="With init, override the detected language used to choose a sandbox template",
    )
    parser.add_argument("--yes", action="store_true", help="Confirm a cleanup operation")
    parser.add_argument("--remove-logs", action="store_true", help="With cleanup, also delete matching run logs")
    parser.add_argument("--all", dest="all_runs", action="store_true", help="With cleanup, select all clean Agentic worktrees")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--review", action="store_true", help="Rerun tests + fresh review; do not code")
    mode.add_argument(
        "--publish", action="store_true",
        help="Publish the existing run (draft PR for tasks; GitHub issue for audits)",
    )
    mode.add_argument(
        "--mark-done", action="store_true",
        help="Record explicit human completion for the current patch",
    )
    parser.add_argument(
        "--reason", default=None,
        help="Optional reason recorded with --mark-done",
    )
    return parser


def execute(args: argparse.Namespace) -> int:
    repo = _absolute_path(Path(args.repo))
    root = _absolute_path(Path(git(repo, "rev-parse", "--show-toplevel")))
    if not _paths_equal(repo, root):
        raise RuntimeError("Run agentic.py from the project root or pass --repo pointing to that root")
    if args.runs:
        show_runs(repo)
        return 0

    return execute_locked(args, repo)


def execute_locked(args: argparse.Namespace, repo: Path) -> int:
    if args.clean and not args.repair_ci:
        raise RuntimeError("--clean is only available with --repair-ci")
    if args.repair_ci and args.pr_number is None:
        raise RuntimeError("--repair-ci requires --pr N")
    if args.pr_number is not None and not args.repair_ci:
        raise RuntimeError("--pr requires --repair-ci")
    if args.repair_ci and any((args.issue is not None, args.task is not None, args.task_file is not None,
                               args.review, args.publish, args.mark_done, args.new, args.offline,
                               args.list, args.runs, args.command is not None)):
        raise RuntimeError("--repair-ci can only be combined with --pr, --repo, --config, --state-dir, and --verbose")
    if args.new and (args.review or args.publish or args.mark_done or args.resume):
        raise RuntimeError("--new cannot be combined with --review, --publish, --mark-done, or --resume")
    if args.resume and args.command != "audit":
        raise RuntimeError("--resume is only available with audit")
    if args.reason and not args.mark_done:
        raise RuntimeError("--reason can only be used with --mark-done")
    if args.offline and (args.new or args.publish or args.list or args.runs):
        raise RuntimeError("--offline only supports resuming an existing issue run")
    if args.runs:
        if args.json:
            show_runs(repo, json_output=True)
        else:
            show_runs(repo)
        return 0
    if args.list:
        config_path = Path(args.config).resolve() if args.config else repo / "agent_config.json"
        github_host = DEFAULT_GITHUB_HOST
        if config_path.is_file():
            github_host = load_config(config_path)["github_host"]
        github_repo, _ = github_metadata(repo, github_host)
        show_issues(repo, fetch_issues(repo, github_repo, github_host))
        return 0
    config_path = Path(args.config).resolve() if args.config else repo / "agent_config.json"
    if not config_path.is_file():
        raise RuntimeError(
            f"No configuration found at {config_path}. Create a target-repository "
            "agent_config.json or pass --config explicitly"
        )
    config = load_config(config_path)
    if args.workflow_mode is not None:
        config["workflow_mode"] = args.workflow_mode
    github_host = config.get("github_host", DEFAULT_GITHUB_HOST)
    test_commands = (
        config["test_commands"] if args.publish
        else test_commands_for_profile(config, args.profile)
    )
    preflight_security(config["security_mode"], config["sandbox"], repo=repo)
    if not args.publish:
        preflight_test_commands(repo, test_commands, sandbox=config["sandbox"])
    # Manual completion and publication use recorded state and do not need
    # provider credentials. The normal development/review path still does.
    if not args.mark_done and not args.publish:
        preflight_agent_settings(
            config["developer_agent"], config["reviewer_agent"], sandbox=config["sandbox"]
        )
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
    if args.repair_ci:
        pr_number = args.pr_number
        if not isinstance(pr_number, int) or isinstance(pr_number, bool):
            raise RuntimeError("--repair-ci requires --pr N")
        return repair_ci(
            repo, pr_number, config, state_dir=state_dir,
            verbose=args.verbose, clean=args.clean,
        )
    task_deadline = time.monotonic() + config["task_timeout_seconds"]

    task_source = config["task_source"]
    offline_run: Run | None = None
    is_audit = args.command == "audit"
    if is_audit:
        if any(value is not None for value in (args.issue, args.task, args.task_file)):
            raise RuntimeError("audit does not accept --issue, --task, or --task-file")
        github_repo = github_repo_from_remote(repo, github_host)
        base = default_base_branch(repo)
        issue = Issue.for_audit(
            AUDIT_KEY,
            "Audit the current repository for code quality, security, reliability, and performance. "
            "Record bounded findings for dashboard review and optional GitHub issue publication.",
        )
        previous = runs_for_audit(repo, AUDIT_KEY)
        if args.publish:
            previous = [
                run for run in previous
                if load_audit_report(logs_path(repo, run, state_dir), AUDIT_KEY) is not None
            ]
            if not previous:
                raise RuntimeError(
                    "No completed audit report found; run `agentic audit` before publishing"
                )
        if args.offline:
            if not previous:
                raise RuntimeError("No existing audit worktree found; offline audit resume cannot start a run")
            offline_run = choose_existing(previous, prompt_prefix="RESUME")
        elif args.resume and not previous:
            raise RuntimeError("No existing audit worktree found; run audit first without --resume")
    elif task_source == "local":
        if args.offline:
            raise RuntimeError("--offline is only available for GitHub issue runs")
        if args.issue is not None:
            raise RuntimeError("This repository uses local task mode; use --task or --task-file instead of --issue")
        if args.task is not None and args.task_file is not None:
            raise RuntimeError("Use only one of --task or --task-file")
        if args.task_file is not None:
            issue = load_local_task(args.task_file.expanduser().resolve())
        elif args.task is not None:
            issue = Issue.from_local_text(args.task)
        else:
            raise RuntimeError("Local task mode requires --task or --task-file; from the shell use /run to paste a task")
        github_repo = github_repo_from_remote(repo, github_host)
        base = default_base_branch(repo)
        previous = runs_for_task(repo, issue.task_key or "")
    else:
        if args.task is not None or args.task_file is not None:
            raise RuntimeError("This repository uses GitHub issue mode; remove --task/--task-file or set task_source to local")
        if args.offline:
            if args.issue is None:
                raise RuntimeError("--offline requires --issue N so an existing run can be selected")
            github_repo = None
            base = default_base_branch(repo)
            previous = runs_for_issue(repo, args.issue)
            if not previous:
                raise RuntimeError(f"No existing worktree found for issue #{args.issue}; offline resume cannot start a run")
            offline_run = choose_existing(previous, prompt_prefix="RESUME")
            cached = read_issue_snapshot(logs_path(repo, offline_run, state_dir))
            if cached is None:
                raise RuntimeError(
                    "This run has no bounded issue snapshot; resume once online before using --offline"
                )
            issue = Issue(
                int(cached.get("number", args.issue)),
                str(cached.get("title", f"Issue #{args.issue}")),
                str(cached.get("body", "")),
                str(cached.get("state", "OPEN")),
                str(cached.get("source", "github")),
            )
            if issue.number != args.issue or issue.is_local:
                raise RuntimeError("Cached issue snapshot does not match the requested GitHub issue")
        else:
            github_repo, base = github_metadata(repo, github_host)
            if args.issue is None:
                issues = fetch_issues(repo, github_repo, github_host)
                show_issues(repo, issues)
                number = select_issue(issues)
            else:
                number = args.issue
            previous = runs_for_issue(repo, number)
            issue = get_issue(repo, github_repo, number, github_host)

    if args.publish and not github_repo:
        try:
            github_repo, base = github_metadata(repo, github_host)
        except RuntimeError as exc:
            raise RuntimeError(
                "Publishing requires a GitHub origin remote and GitHub CLI access"
            ) from exc
    issue_number = issue.number
    if not issue.is_local and not issue.is_audit and issue_number is None:
        raise RuntimeError("GitHub issue data did not include an issue number")
    if offline_run is not None:
        run = offline_run
        fresh = False
    elif args.new or (is_audit and not (args.resume or args.publish)):
        if not eligible(issue):
            raise RuntimeError("Only open issues can start a new worktree")
        if issue.is_audit:
            run = create_audit_worktree(
                repo, base, issue, state_dir,
                separate=args.new or bool(previous),
            )
        elif issue.is_local:
            run = create_local_worktree(repo, base, issue, state_dir, separate=True)
        else:
            assert issue_number is not None
            run = create_worktree(repo, base, issue_number, state_dir)
        fresh = True
    elif previous:
        run = choose_existing(
            previous, prompt_prefix="PUBLISH" if args.publish else "RESUME"
        )
        fresh = False
        with repository_lock(repo, state_dir):
            refresh_base(repo, base)
            repaired = repair_relative_worktree(repo, run.worktree)
        if repaired:
            _status_print(f"[GIT] Repaired worktree metadata: {run.worktree}")
    else:
        if args.review or args.publish or args.mark_done:
            raise RuntimeError(
                "No existing worktree for this task; start it first without --review, --publish, or --mark-done"
            )
        if not eligible(issue):
            raise RuntimeError("Only open issues can start a new worktree")
        if issue.is_audit:
            run = create_audit_worktree(repo, base, issue, state_dir)
        elif issue.is_local:
            run = create_local_worktree(repo, base, issue, state_dir)
        else:
            assert issue_number is not None
            run = create_worktree(repo, base, issue_number, state_dir)
        fresh = True
    folder = logs_path(repo, run, state_dir)
    requested_instruction = getattr(args, "instruction", None)
    if requested_instruction is not None:
        record_run_instruction(folder, requested_instruction)
    instruction = read_run_instruction(folder)
    system_prompt = config.get("system_prompt")
    record_issue_metadata(
        folder, number=issue.number, title=issue.title,
        state=issue.state, source=issue.source, body=issue.body,
        audit_key=issue.audit_key,
        shell_job_id=os.environ.get("AGENTIC_SHELL_JOB_ID"),
    )
    record_security_policy(folder, config)
    manual_approval = load_publication_approval(folder)
    print_run_header(issue, run, fresh)
    _warn_dangerous_permissions(config)
    _status_print(
        f"Providers: developer={config['developer_agent']['provider']} "
        f"reviewer={config['reviewer_agent']['provider']}",
        flush=True,
    )
    if args.review:
        if issue.is_audit:
            raise RuntimeError("Audit reports are read-only; use --publish to create a GitHub issue")
        with worktree_lock(repo, run, state_dir):
            passed, *_ = verify_and_review(
                run, issue, base, config, folder, test_commands=test_commands,
                deadline=task_deadline,
                instruction=instruction,
                system_prompt=system_prompt,
                verbose=args.verbose,
            )
        if not passed:
            raise RuntimeError("Review or tests did not approve current changes")
        with worktree_lock(repo, run, state_dir):
            _attempt_auto_publication(
                run=run, issue=issue, github_repo=github_repo, github_host=github_host,
                base=base, config=config, folder=folder,
                offline=offline_run is not None,
                candidates=[(run, folder)] + [
                    (candidate, _run_log_folder(repo, candidate, state_dir))
                    for candidate in previous if candidate != run
                ],
            )
        next_action = (
            "python agentic.py audit --publish" if issue.is_audit
            else f"python agentic.py --issue {issue.number} --publish"
            if not issue.is_local else "rerun with --publish to create a GitHub draft PR"
        )
        print_run_summary(run, "VALIDATED", next_action, logs_path=folder)
        return 0
    if args.publish:
        if not github_repo:
            raise RuntimeError("Publishing requires a GitHub origin remote and GitHub CLI access")
        if issue.is_audit:
            with worktree_lock(repo, run, state_dir):
                publish_audit_issue(run, issue, github_repo, github_host, folder)
        else:
            with worktree_lock(repo, run, state_dir):
                publish(
                    run, issue, github_repo, base, config, folder,
                    test_commands=test_commands, deadline=task_deadline,
                    manual_approval=manual_approval,
                )
        return 0
    if args.mark_done:
        if not eligible(issue):
            raise RuntimeError("Issue is closed; manual completion is blocked")
        with worktree_lock(repo, run, state_dir):
            record_manual_approval(
                run, issue, base, config, folder,
                (args.reason or "Manually marked done by user").strip()
                or "Manually marked done by user",
            )
        _attempt_auto_publication(
            run=run, issue=issue, github_repo=github_repo, github_host=github_host,
            base=base, config=config, folder=folder,
            offline=offline_run is not None,
        )
        next_action = (
            "python agentic.py audit --publish" if issue.is_audit
            else f"python agentic.py --issue {issue.number} --publish"
            if not issue.is_local else "rerun with --publish to create a GitHub draft PR"
        )
        print_run_summary(run, "MANUALLY APPROVED", next_action, logs_path=folder)
        return 0
    if not eligible(issue):
        raise RuntimeError("Issue is closed; development blocked")
    with worktree_lock(repo, run, state_dir):
        investigation = (
            _run_audit(run, issue, config, folder, deadline=task_deadline, verbose=args.verbose)
            if issue.is_audit else _run_investigation(
                run, issue, config, folder, deadline=task_deadline,
                verbose=args.verbose,
            )
        )
    if issue.is_audit:
        _status_print("[AUDIT] Read-only audit complete; no code changes were made")
        _status_print(f"[AUDIT] Report and logs: {folder}")
        with worktree_lock(repo, run, state_dir):
            _attempt_auto_publication(
                run=run, issue=issue, github_repo=github_repo, github_host=github_host,
                base=base, config=config, folder=folder,
                offline=offline_run is not None,
                candidates=[(run, folder)] + [
                    (candidate, _run_log_folder(repo, candidate, state_dir))
                    for candidate in previous if candidate != run
                ],
            )
        _status_print("[AUDIT] Next: run `agentic audit --publish` to create a GitHub issue")
        return 0
    if not fresh and github_repo:
        existing = check_existing_pr(run.worktree, github_repo, run.branch, github_host)
        if existing:
            raise RuntimeError(f"A PR already exists: {existing[0]['url']}. Review it instead of editing here")
    if fresh:
        _status_print("[BASELINE] Verifying untouched base before development")
        with worktree_lock(repo, run, state_dir):
            passed, report = run_tests(
                run.worktree, test_commands, folder,
                timeout=_remaining_timeout(task_deadline, config["effective_test_timeout_seconds"]),
                sandbox=config.get("sandbox"),
                deadline=task_deadline,
                verbose=args.verbose,
            )
        if not passed:
            raise RuntimeError(f"Base tests are failing; retained worktree.\n{report[-2500:]}")
    feedback_parts = [
        audit_feedback(investigation) if issue.is_audit else investigation_feedback(investigation)
    ]
    resume_feedback = load_resume_feedback(folder) if not fresh else ""
    if resume_feedback:
        _status_print("[RESUME] Loaded previous review/test feedback for the developer")
        feedback_parts.append(resume_feedback)
    with worktree_lock(repo, run, state_dir):
        completed = develop(
            run, issue, base, config, folder,
            initial_feedback="\n\n".join(feedback_parts), test_commands=test_commands,
            deadline=task_deadline, instruction=instruction,
            system_prompt=system_prompt,
            verbose=args.verbose,
        )
    if github_repo and not issue.is_local and not issue.is_audit:
        if not post_terminal_comment(
            run, issue, folder, github_repo=github_repo, github_host=github_host,
            cwd=repo, offline=offline_run is not None,
        ):
            _status_print(
                "[GITHUB] Completion comment was not posted; the terminal result remains in local logs.",
                file=sys.stderr,
            )
    if not completed:
        terminal_result = None
        try:
            raw_result = read_text_bounded(folder / "result.json", MAX_RESULT_BYTES)
            candidate = json.loads(raw_result) if raw_result is not None else None
        except (OSError, TypeError, json.JSONDecodeError):
            candidate = None
        if isinstance(candidate, dict):
            status = candidate.get("status")
            next_action = candidate.get("next_action")
            if (
                status in {"RETRY_EXHAUSTED", "BLOCKED", "NO_CHANGES"}
                and isinstance(next_action, str)
            ):
                terminal_result = f"{status}: {next_action}"
        raise RuntimeError(terminal_result or "Unable to complete the task; resume the preserved worktree")
    with worktree_lock(repo, run, state_dir):
        _attempt_auto_publication(
            run=run, issue=issue, github_repo=github_repo, github_host=github_host,
            base=base, config=config, folder=folder,
            offline=offline_run is not None,
            candidates=[(run, folder)] + [
                (candidate, _run_log_folder(repo, candidate, state_dir))
                for candidate in previous if candidate != run
            ],
        )
    return 0


def _main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "shell":
        from .shell import main as shell_main
        return shell_main(raw_args[1:], _diagnostics=False)
    if raw_args and raw_args[0] == "dashboard":
        from .dashboard import main as dashboard_main
        return dashboard_main(raw_args[1:], _diagnostics=False)
    parser = build_parser()
    try:
        args = parser.parse_args(raw_args)
        if args.command == "help":
            parser.print_help()
            return 0
        if args.command != "init" and (
            args.discover_tests or args.discovery_timeout != DISCOVERY_TIMEOUT_SECONDS
        ):
            parser.error("--discover-tests and --discovery-timeout are only valid with init")
        if args.command == "init":
            return init_repository(
                Path(args.repo), force=args.force, create_agents=args.agents,
                provider=args.provider, reviewer_provider=args.reviewer_provider,
                security_mode="isolated" if args.secure else args.security_mode,
                sandbox_image=args.sandbox_image,
                sandbox_dockerfile=args.sandbox_dockerfile,
                sandbox_context=args.sandbox_context,
                sandbox_language=args.sandbox_language,
                discover_tests=args.discover_tests,
                discovery_timeout=args.discovery_timeout,
            )
        if args.secure:
            parser.error("--secure is only valid with init")
        if args.command == "doctor":
            return doctor(
                Path(args.repo), Path(args.config) if args.config else None,
                json_output=args.json, run_commands=args.commands,
                profile=args.profile,
                worktree=Path(args.worktree) if args.worktree else None,
            )
        if args.command in {"status", "runs"}:
            repo = _absolute_path(Path(args.repo))
            root = _absolute_path(Path(git(repo, "rev-parse", "--show-toplevel")))
            if not _paths_equal(repo, root):
                raise RuntimeError("Run status from the project root or pass --repo pointing to that root")
            show_runs(repo, json_output=args.json)
            return 0
        if args.command == "cleanup":
            if args.issue is not None and args.all_runs:
                parser.error("cleanup accepts either --issue N or --all, not both")
            repo = _absolute_path(Path(args.repo))
            root = _absolute_path(Path(git(repo, "rev-parse", "--show-toplevel")))
            if not _paths_equal(repo, root):
                raise RuntimeError("Run cleanup from the project root or pass --repo pointing to that root")
            cleanup_state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
            with repository_lock(repo, cleanup_state_dir):
                return cleanup(
                    repo, issue=args.issue, all_runs=args.all_runs,
                    confirm=args.yes, remove_logs=args.remove_logs,
                    state_dir=cleanup_state_dir,
                )
        if (
            args.profile != "fast" and args.command != "audit"
            and args.issue is None and args.task is None and args.task_file is None
        ):
            parser.error("--profile requires --issue, --task, --task-file, or audit; use /run ISSUE --profile slow in the interactive shell")
        # The interactive supervisor is the natural entry point when no
        # runner action was requested.  Keep explicit issue/list/run modes
        # unchanged, while making `agentic` and `agentic --repo ...` useful
        # without requiring a special flag.
        default_interactive = (
            args.command != "audit"
            and args.issue is None
            and args.pr_number is None
            and not args.repair_ci
            and not args.list
            and not args.runs
            and args.task is None
            and args.task_file is None
            and not args.new
            and not args.review
            and not args.publish
        )
        if args.interactive or default_interactive:
            if any((args.command == "audit", args.issue is not None, args.pr_number is not None,
                    args.repair_ci, args.task is not None, args.task_file is not None,
                    args.list, args.runs, args.new, args.review, args.publish)):
                parser.error("--interactive cannot be combined with a task, issue, list, runs, new, review, or publish option")
            from .shell import run_shell
            config = Path(args.config).resolve() if args.config else None
            state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
            runner = SCRIPT_DIR / "agentic.py"
            if not runner.is_file():
                runner = Path(sys.executable)
            return run_shell(Path(args.repo), runner, config, state_dir=state_dir)
        return execute(args)
    except UserCancelled as exc:
        record_diagnostic("cancelled", exc=exc, argv=raw_args, entrypoint="cli")
        _status_print(f"[CANCEL] {exc}", file=sys.stderr)
        return 0
    except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        record_diagnostic("handled_error", exc=exc, argv=raw_args, entrypoint="cli")
        _status_print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    entrypoint = "dashboard" if raw_args and raw_args[0] == "dashboard" else (
        "shell" if not raw_args or raw_args[0] == "shell" or "--interactive" in raw_args else "cli"
    )
    return run_with_diagnostics(
        lambda: _main(raw_args), argv=raw_args, entrypoint=entrypoint,
    )


if __name__ == "__main__":
    sys.exit(main())
