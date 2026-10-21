#!/usr/bin/env python3
"""A stage-free, resumable coding-agent runner.

Install Agentic once, then place a target-specific agent_config.json in the
repository being worked on. The selected provider is responsible for
understanding progress; Git worktrees supply durable context. No third-party
Python packages are required by the runner itself.
"""
from __future__ import annotations

import argparse
import datetime
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

import agentic_config as _agentic_config
import agentic_context as _agentic_context
import agentic_worktrees as _agentic_worktrees
from agentic_config import (  # noqa: F401
    DEFAULT_INIT_SECURITY_MODE,
    DEFAULT_OPENROUTER_BASE_URL,
    DEFAULT_REVIEWER_TIMEOUT,
    DEFAULT_TASK_TIMEOUT,
    DEFAULT_TEST_TIMEOUT,
    AUTH_MODES,
    PROVIDER_API_KEY_ENV,
    OPENROUTER_MAX_TURNS,
    SECURITY_MODES,
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
from agentic_context import (  # noqa: F401
    FEEDBACK_CONTEXT_CHARS,
    INSTRUCTION_CONTEXT_CHARS as _INSTRUCTION_CONTEXT_CHARS,
    REPORT_CONTEXT_CHARS,
    _bounded_text,
    _compact_feedback,
    _issue_context,
)
from agentic_prompts import (  # noqa: F401
    _developer_prompt_context,
    _developer_run_instruction_context,
    _developer_system_prompt_context,
    _run_instruction_context,
    _system_prompt_context,
    _untrusted_prompt_context,
)
from agentic_diagnostics import run_doctor as _run_doctor
from agentic_test_discovery import (
    DISCOVERY_TIMEOUT_SECONDS,
    MAX_DISCOVERY_TIMEOUT_SECONDS,
    DiscoveryResult,
    discover_and_validate,
)
from agentic_execution import (  # noqa: F401
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
from agentic_output import style_system_text, style_text
from agentic_persistence import atomic_write_json
from agentic_openrouter import (  # noqa: F401
    run_openrouter,
)
from agentic_policy import default_permission_policy, forbidden  # noqa: F401
from agentic_models import DEFAULT_GITHUB_HOST, AgentSettings, Issue, PullRequest, Run, StatePaths  # noqa: F401
from agentic_review import (  # noqa: F401
    parse_review_result,
    record_review_findings, review_history_feedback,
)
from agentic_sandbox import DEFAULT_SANDBOX_IMAGE, SUPPORTED_LANGUAGES, validate_sandbox
from agentic_security import redact_secrets  # noqa: F401
from agentic_telemetry import (
    read_run_instruction,
    record_run_instruction,
    record_issue_metadata,
    read_issue_snapshot,
    record_security_policy,
)

SCRIPT_DIR = Path(__file__).resolve().parent
__version__ = "1.1.65"
ISSUE_CONTEXT_CHARS = _agentic_context.ISSUE_CONTEXT_CHARS
INSTRUCTION_CONTEXT_CHARS = _INSTRUCTION_CONTEXT_CHARS
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


from agentic_worktrees import (  # noqa: E402, F401
    choose_existing,
    create_local_worktree,
    create_worktree,
    default_base_branch,
    default_state_dir,
    discover_runs,
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
    preferred = (state_paths(repo, state_dir).runs if state_dir else legacy_runs_dir(repo)) / run.branch.replace("/", "-")
    legacy = legacy_runs_dir(repo) / run.branch.replace("/", "-")
    folder = legacy if state_dir and legacy.is_dir() and not preferred.exists() else preferred
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def next_log(folder: Path, kind: str, ext: str = "txt") -> Path:
    nums = []
    for p in folder.glob(f"{kind}-*.{ext}"):
        suffix = p.stem.removeprefix(f"{kind}-")
        if suffix.isdigit():
            nums.append(int(suffix))
    return folder / f"{kind}-{max(nums, default=0) + 1:03d}.{ext}"


MANUAL_APPROVAL_FILENAME = "manual-approval.json"
REVIEW_APPROVAL_FILENAME = "review-approval.json"
BLOCKED_FILENAME = "blocked.json"


def manual_approval_path(folder: Path) -> Path:
    """Return the durable human-completion record for a run."""
    return folder / MANUAL_APPROVAL_FILENAME


def review_approval_path(folder: Path) -> Path:
    """Return the durable automated-review approval record for a run."""
    return folder / REVIEW_APPROVAL_FILENAME


def blocked_path(folder: Path) -> Path:
    """Return the durable deferred-run marker path."""
    return folder / BLOCKED_FILENAME


def load_blocked(folder: Path) -> dict[str, Any] | None:
    """Load a blocked marker, ignoring incomplete or corrupt markers."""
    path = blocked_path(folder)
    try:
        value = json.loads(redact_secrets(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("status") != "blocked":
        return None
    if not isinstance(value.get("patch_digest"), str):
        return None
    return value


def record_blocked(folder: Path, patch_digest: str, findings: list[str], *,
                   issue_digest: str = "") -> None:
    """Persist a repeated unchanged review blocker for future invocations."""
    atomic_write_json(blocked_path(folder), {
        "status": "blocked",
        "reason": "Repeated review blocker with no patch change",
        "patch_digest": patch_digest,
        "issue_digest": issue_digest,
        "findings": findings,
        "blocked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


def record_no_code_changes_blocked(folder: Path, developer_report: str, *,
                                   issue_digest: str = "") -> None:
    """Persist a developer blocker for a passing run with no proposed patch."""
    developer_log = _latest_numbered_log(folder, "developer", "txt")
    summary = _bounded_text(developer_report, 2_000).strip()
    atomic_write_json(blocked_path(folder), {
        "status": "blocked",
        "kind": "no-code-changes",
        "reason": "Developer reported a blocker without producing code changes",
        "patch_digest": "",
        "issue_digest": issue_digest,
        "developer_summary": summary or "No developer blocker summary was provided.",
        "developer_log": developer_log.name if developer_log else "",
        "logs_path": str(folder),
        "blocked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


def clear_blocked(folder: Path) -> None:
    """Clear a deferred marker after the proposed patch changes."""
    try:
        blocked_path(folder).unlink()
    except FileNotFoundError:
        pass


def load_manual_approval(folder: Path) -> dict[str, Any] | None:
    """Load a manual completion record, rejecting corrupt approval state."""
    path = manual_approval_path(folder)
    if not path.is_file():
        return None
    try:
        value = json.loads(redact_secrets(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Manual completion record is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("status") != "approved":
        raise RuntimeError(f"Manual completion record is invalid: {path}")
    required = ("branch", "issue_digest", "base_sha", "patch_digest", "reason")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
        raise RuntimeError(f"Manual completion record is incomplete: {path}")
    return value


def record_manual_approval(run: Run, issue: Issue, base: str, config: dict[str, Any],
                           folder: Path, reason: str) -> dict[str, Any]:
    """Record a human completion decision tied to the exact current patch."""
    reason = reason.strip() or "Manually marked done by user"
    base_sha, digest, summary = prepare_patch(run.worktree, base, config["max_files"])
    approval = {
        "status": "approved",
        "issue": issue.reference,
        "issue_digest": issue_fingerprint(issue),
        "branch": run.branch,
        "base": base,
        "base_sha": base_sha,
        "patch_digest": digest,
        "reason": reason,
        "marked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    path = manual_approval_path(folder)
    atomic_write_json(path, approval)
    _status_print(f"[MANUAL] Marked complete: {summary.strip() or '(patch recorded)'}")
    _status_print(f"[MANUAL] Approval record: {path}")
    return approval


def record_review_approval(run: Run, issue: Issue, base: str, base_sha: str,
                           patch_digest: str, verdict: dict[str, Any],
                           config: dict[str, Any], folder: Path) -> dict[str, Any]:
    """Persist automated approval for the exact patch that was reviewed."""
    approval = {
        "status": "approved",
        "kind": "automated",
        "issue": issue.reference,
        "issue_digest": issue_fingerprint(issue),
        "branch": run.branch,
        "base": base,
        "base_sha": base_sha,
        "patch_digest": patch_digest,
        "summary": str(verdict.get("summary", "")),
        "skipped": [item for item in verdict.get("skipped", []) if isinstance(item, str)],
        "reviewer_provider": config.get("reviewer_agent", {}).get("provider", "codex"),
        "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    path = review_approval_path(folder)
    atomic_write_json(path, approval)
    return approval


def load_review_approval(folder: Path) -> dict[str, Any] | None:
    """Load an automated approval, rejecting corrupt approval state."""
    path = review_approval_path(folder)
    if not path.is_file():
        return None
    try:
        value = json.loads(redact_secrets(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Automated review approval is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("status") != "approved":
        raise RuntimeError(f"Automated review approval is invalid: {path}")
    required = ("branch", "issue_digest", "base_sha", "patch_digest")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
        raise RuntimeError(f"Automated review approval is incomplete: {path}")
    return value


def load_publication_approval(folder: Path) -> dict[str, Any] | None:
    """Load the newest human or automated approval available for publication."""
    candidates: list[tuple[float, dict[str, Any]]] = []
    manual = load_manual_approval(folder)
    if manual is not None:
        manual["kind"] = "manual"
        candidates.append((manual_approval_path(folder).stat().st_mtime, manual))
    automated = load_review_approval(folder)
    if automated is not None:
        candidates.append((review_approval_path(folder).stat().st_mtime, automated))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _latest_numbered_log(folder: Path, kind: str, ext: str) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    for path in folder.glob(f"{kind}-*.{ext}"):
        suffix = path.stem.removeprefix(f"{kind}-")
        if suffix.isdigit():
            candidates.append((int(suffix), path))
    return max(candidates, default=(0, None))[1]


def load_resume_feedback(folder: Path) -> str:
    """Load the latest failed review and test evidence for a resumed run."""
    chunks: list[str] = []
    review_log = _latest_numbered_log(folder, "review", "json")
    if review_log:
        raw = redact_secrets(review_log.read_text(encoding="utf-8", errors="replace"))
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
        raw = redact_secrets(test_log.read_text(encoding="utf-8", errors="replace"))
        if raw.strip():
            chunks.append("Previous test evidence (untrusted output):\n" + raw)
    recurring = review_history_feedback(folder)
    if recurring:
        chunks.append(recurring)
    return redact_secrets("\n\n".join(chunks)[-9000:])


def load_latest_developer_report(folder: Path, limit: int = 6000) -> str:
    """Load the latest developer handoff for standalone review or publication."""
    path = _latest_numbered_log(folder, "developer", "txt")
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""








# Compatibility wrappers preserve patchable agentic.<name> seams.
import agentic_workflow as _agentic_workflow  # noqa: E402

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
        # command can run. Trusted discovery deliberately retains its host
        # execution behavior; its sandbox is disabled by policy.
        if security_mode == "isolated":
            preflight_security(security_mode, sandbox, repo=repo)
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
        "test_commands": commands,
        "slow_test_commands": slow_commands,
        "max_retries": 3,
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
    if commands == [["git", "diff", "--check"]]:
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
        label = (f"#{run.issue_number}" if run.issue_number is not None
                 else f"local:{(run.task_key or 'unknown')[:8]}")
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
        "command", nargs="?", choices=("help", "shell", "dashboard", "init", "doctor", "cleanup", "status", "runs"),
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
    parser.add_argument("--issue", type=int, help="GitHub issue number")
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
    mode.add_argument("--publish", action="store_true", help="Publish the existing approved patch as a draft PR")
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
    if args.new and (args.review or args.publish or args.mark_done):
        raise RuntimeError("--new cannot be combined with --review, --publish, or --mark-done")
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
    state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir()
    task_deadline = time.monotonic() + config["task_timeout_seconds"]

    task_source = config["task_source"]
    offline_run: Run | None = None
    if task_source == "local":
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
    if not issue.is_local and issue_number is None:
        raise RuntimeError("GitHub issue data did not include an issue number")
    if offline_run is not None:
        run = offline_run
        fresh = False
    elif args.new:
        if not eligible(issue):
            raise RuntimeError("Only open issues can start a new worktree")
        if issue.is_local:
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
        if issue.is_local:
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
        next_action = (f"python agentic.py --issue {issue.number} --publish"
                       if not issue.is_local else "rerun with --publish to create a GitHub draft PR")
        print_run_summary(run, "VALIDATED", next_action)
        return 0
    if args.publish:
        if not github_repo:
            raise RuntimeError("Publishing requires a GitHub origin remote and GitHub CLI access")
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
        next_action = (f"python agentic.py --issue {issue.number} --publish"
                       if not issue.is_local else "rerun with --publish to create a GitHub draft PR")
        print_run_summary(run, "MANUALLY APPROVED", next_action)
        return 0
    if not eligible(issue):
        raise RuntimeError("Issue is closed; development blocked")
    with worktree_lock(repo, run, state_dir):
        investigation = _run_investigation(
            run, issue, config, folder, deadline=task_deadline,
            verbose=args.verbose,
        )
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
    feedback_parts = [investigation_feedback(investigation)]
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
    if not completed:
        terminal_result = None
        try:
            candidate = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "shell":
        from agentic_shell import main as shell_main
        return shell_main(raw_args[1:])
    if raw_args and raw_args[0] == "dashboard":
        from agentic_dashboard import main as dashboard_main
        return dashboard_main(raw_args[1:])
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
            cleanup_state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir()
            with repository_lock(repo, cleanup_state_dir):
                return cleanup(
                    repo, issue=args.issue, all_runs=args.all_runs,
                    confirm=args.yes, remove_logs=args.remove_logs,
                    state_dir=cleanup_state_dir,
                )
        if args.profile != "fast" and args.issue is None and args.task is None and args.task_file is None:
            parser.error("--profile requires --issue or --task; use /run ISSUE --profile slow in the interactive shell")
        # The interactive supervisor is the natural entry point when no
        # runner action was requested.  Keep explicit issue/list/run modes
        # unchanged, while making `agentic` and `agentic --repo ...` useful
        # without requiring a special flag.
        default_interactive = (
            args.issue is None
            and not args.list
            and not args.runs
            and args.task is None
            and args.task_file is None
            and not args.new
            and not args.review
            and not args.publish
        )
        if args.interactive or default_interactive:
            if any((args.issue is not None, args.task is not None, args.task_file is not None,
                    args.list, args.runs, args.new, args.review, args.publish)):
                parser.error("--interactive cannot be combined with a task, issue, list, runs, new, review, or publish option")
            from agentic_shell import run_shell
            config = Path(args.config).resolve() if args.config else None
            state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir()
            return run_shell(Path(args.repo), Path(__file__).resolve(), config, state_dir=state_dir)
        return execute(args)
    except UserCancelled as exc:
        _status_print(f"[CANCEL] {exc}", file=sys.stderr)
        return 0
    except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        _status_print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
