"""Repository prerequisite diagnostics.

The compatibility-facing runner keeps the public ``agentic.doctor`` entry
point, while this module owns the diagnostic workflow and its output format.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, ContextManager

from .config import (
    api_key_env_for_provider,
    auth_mode_for_settings,
    load_config,
    preflight_security,
    sandbox_env_for_provider,
)
from .execution import _doctor_public_detail
from .models import DEFAULT_GITHUB_HOST
from .output import style_system_text
from .providers import (
    display_provider_path,
    resolve_provider_executable,
)
from .sandbox import sandbox_command


AGENT_CONFIG_FILENAME = "agent_config.json"


def _github_environment(host: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment["GH_HOST"] = host
    return environment

# These are documented, read-only provider status commands.  Their output is
# deliberately discarded; only the exit status is used to distinguish an
# authenticated provider from an installed but unusable CLI.  Native providers
# without a documented noninteractive status command are handled as an
# explicit, nonblocking skip below.
PROVIDER_AUTH_STATUS_ARGS = {
    "codex": ("login", "status"),
    "claude": ("auth", "status"),
}


def _run_configured_command(
    argv: list[str], cwd: Path, *, timeout: int | None,
    sandbox: dict[str, object] | None,
) -> dict[str, object]:
    """Run one doctor command without exposing its output in doctor JSON."""
    started = time.monotonic()
    try:
        process = subprocess.run(
            sandbox_command(argv, cwd, sandbox, pass_env=[]),
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "argv": list(argv),
            "status": "timeout",
            "exit_code": 124,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except OSError:
        return {
            "argv": list(argv),
            "status": "not-started",
            "exit_code": None,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    return {
        "argv": list(argv),
        "status": "passed" if process.returncode == 0 else "failed",
        "exit_code": process.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def _configured_command_detail(
    command_results: list[dict[str, object]],
    profile: str,
    worktree: Path,
) -> str:
    """Describe configured command execution without exposing command text."""
    failures = [item for item in command_results if item.get("status") != "passed"]
    if not failures:
        return f"{len(command_results)} {profile} command(s) ran in {worktree}"

    if len(failures) == 1:
        result = failures[0]
        status = result.get("status")
        if status == "failed":
            exit_code = result.get("exit_code")
            if isinstance(exit_code, int):
                reason = f"exit code {exit_code}"
            else:
                reason = "a nonzero exit status"
            return f"{profile} command failed ({reason})"
        if status == "timeout":
            return f"{profile} command timed out"
        if status == "not-started":
            return f"{profile} command could not start"
        return f"{profile} command did not complete"

    counts = {
        "failed": sum(item.get("status") == "failed" for item in failures),
        "timeout": sum(item.get("status") == "timeout" for item in failures),
        "not-started": sum(item.get("status") == "not-started" for item in failures),
    }
    reasons = []
    if counts["failed"]:
        reasons.append(f"{counts['failed']} failed")
    if counts["timeout"]:
        reasons.append(f"{counts['timeout']} timed out")
    if counts["not-started"]:
        reasons.append(f"{counts['not-started']} could not start")
    return (
        f"{len(failures)} of {len(command_results)} {profile} commands failed"
        f" ({', '.join(reasons)})"
    )


def _public_command_results(
    command_results: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return command outcomes without configured argv or other input text."""
    return [
        {
            "status": str(result.get("status", "unknown")),
            "exit_code": result.get("exit_code"),
            "duration_seconds": result.get("duration_seconds"),
        }
        for result in command_results
    ]


def _public_check(check: dict[str, object]) -> dict[str, object]:
    """Keep credential and configuration values out of public diagnostics."""
    label = str(check["name"])
    ok = bool(check["ok"])
    required = bool(check["required"])
    public = {
        "category": str(check["category"]),
        "name": label,
        "ok": ok,
        "required": required,
        "status": str(check["status"]),
        "detail": _doctor_public_detail(
            label, ok, required, str(check["detail"]),
        ),
    }
    if "authentication" in label:
        public.update({
            "auth_mode": check.get("auth_mode"),
            "credential_set": check.get("credential_set"),
            "sandbox_passed": check.get("sandbox_passed"),
        })
    return public


def run_doctor(
    repo: Path,
    config_path: Path | None = None,
    *,
    json_output: bool = False,
    run_commands: bool = False,
    profile: str = "fast",
    worktree: Path | None = None,
    command_lock: Callable[[Path], ContextManager[None]] | None = None,
    installed_git_version: Callable[..., tuple[int, int, int] | None],
    probe: Callable[..., tuple[bool, str]],
    preflight_test_commands: Callable[..., None],
    relative_worktree_paths_supported: Callable[[], bool],
    security_mode_override: str | None = None,
) -> int:
    """Report local prerequisites and return nonzero when a required item is missing."""
    if profile not in {"fast", "slow"}:
        raise ValueError("profile must be 'fast' or 'slow'")
    repo = repo.resolve()
    command_worktree = (worktree or repo).resolve()
    checks: list[dict[str, object]] = []

    def add_check(
        category: str,
        label: str,
        ok: bool,
        detail: str,
        required: bool = True,
        status: str | None = None,
        **metadata: object,
    ) -> None:
        check: dict[str, object] = {
            "category": category,
            "name": label,
            "ok": ok,
            "required": required,
            "status": status or ("ok" if ok else ("fail" if required else "warn")),
            "detail": detail,
        }
        check.update(metadata)
        checks.append(check)

    def add_skip(category: str, label: str, detail: str) -> None:
        add_check(category, label, False, detail, required=False, status="skip")

    command_results: list[dict[str, object]] = []
    git_version = installed_git_version()
    add_check(
        "repository", "Git executable", git_version is not None,
        ".".join(map(str, git_version)) if git_version else "not found",
    )
    root_ok, _ = probe(["git", "rev-parse", "--show-toplevel"], repo)
    add_check(
        "repository", "Git repository", root_ok,
        "repository detected" if root_ok else "not a repository",
    )
    if root_ok:
        origin_ok, _ = probe(["git", "remote", "get-url", "origin"], repo)
        add_check(
            "repository", "origin remote", origin_ok,
            "origin remote configured" if origin_ok else "missing origin remote",
        )
    else:
        add_skip("repository", "origin remote", "requires a Git repository")

    if git_version is None:
        add_skip(
            "repository", "Worktree path compatibility",
            "requires the Git executable",
        )
    elif os.name == "nt" or os.environ.get("WSL_DISTRO_NAME"):
        paths_ok = relative_worktree_paths_supported()
        add_check(
            "repository", "Worktree path compatibility", paths_ok,
            "relative paths supported" if paths_ok else
            "Git 2.48+ is required in WSL and Windows Git",
            required=False if not paths_ok else True,
        )
    else:
        add_check(
            "repository", "Worktree path compatibility", True,
            "native Git paths are compatible",
        )
    target_config = (config_path or repo / AGENT_CONFIG_FILENAME).resolve()
    if target_config.is_file():
        try:
            loaded = load_config(target_config)
            if security_mode_override == "isolated" and loaded.get("security_mode") == "trusted":
                loaded = dict(loaded)
                loaded["security_mode"] = "isolated"
                loaded["sandbox"] = dict(loaded.get("sandbox") or {})
            if run_commands and loaded.get("security_mode") == "isolated":
                preflight_security(
                    loaded["security_mode"], loaded.get("sandbox", {}), repo=repo,
                )
            github_host = loaded.get("github_host", DEFAULT_GITHUB_HOST)
            gh_path = None if loaded["task_source"] == "local" else shutil.which("gh")
            if loaded["task_source"] == "local":
                add_skip(
                    "github", "GitHub repository access",
                    "local task mode; not required",
                )
                add_skip(
                    "github", "GitHub issue access",
                    "local task mode; not required",
                )
            elif not gh_path:
                add_check(
                    "github", "GitHub CLI authentication", False,
                    "GitHub CLI (gh) not found on PATH",
                )
                add_check(
                    "github", "GitHub repository access", False,
                    "GitHub CLI (gh) not found on PATH",
                )
                add_check(
                    "github", "GitHub issue access", False,
                    "GitHub CLI (gh) not found on PATH",
                )
            else:
                gh_auth_ok, _ = probe(
                    [gh_path, "auth", "status", "--active"],
                    repo, timeout=30, env=_github_environment(github_host),
                )
                add_check(
                    "github", "GitHub CLI authentication", gh_auth_ok,
                    "authenticated" if gh_auth_ok else "not authenticated",
                )
                repo_access_ok, _ = probe(
                    [gh_path, "repo", "view", "--json", "nameWithOwner"],
                    repo, timeout=30, env=_github_environment(github_host),
                )
                add_check(
                    "github", "GitHub repository access", repo_access_ok,
                    "repository is accessible" if repo_access_ok else
                    "repository is not accessible",
                )
                issue_access_ok, _ = probe(
                    [gh_path, "issue", "list", "--limit", "1", "--json", "number"],
                    repo, timeout=30, env=_github_environment(github_host),
                )
                add_check(
                    "github", "GitHub issue access", issue_access_ok,
                    "issues are readable" if issue_access_ok else
                    "issues are not readable",
                )
            preflight_test_commands(
                command_worktree if run_commands and command_worktree.is_dir() else repo,
                loaded["test_commands"] + loaded["slow_test_commands"],
                sandbox=loaded.get("sandbox"),
            )
            add_check("repository", "Agentic config", True, str(target_config))
            add_check(
                "repository", "Configured checks", True,
                f"{len(loaded['test_commands'])} fast / {len(loaded['slow_test_commands'])} slow",
            )
            worktree_ok = True
            if run_commands:
                worktree_ok = command_worktree.is_dir()
                if worktree_ok:
                    worktree_ok, _ = probe(
                        ["git", "rev-parse", "--show-toplevel"], command_worktree,
                    )
                if worktree_ok:
                    repo_git_ok, repo_git_common = probe(
                        ["git", "rev-parse", "--git-common-dir"], repo,
                    )
                    worktree_git_ok, worktree_git_common = probe(
                        ["git", "rev-parse", "--git-common-dir"], command_worktree,
                    )
                    if repo_git_ok and worktree_git_ok:
                        def absolute_git_common(path: Path, value: str) -> Path:
                            common = Path(value.strip())
                            return (common if common.is_absolute() else path / common).resolve()

                        worktree_ok = (
                            absolute_git_common(repo, repo_git_common)
                            == absolute_git_common(command_worktree, worktree_git_common)
                        )
                    else:
                        worktree_ok = False
                add_check(
                    "repository", "Command worktree", worktree_ok,
                    str(command_worktree) if worktree_ok else
                    f"not a Git worktree: {command_worktree}",
                )
            if loaded["security_mode"] == "isolated":
                add_check("repository", "Security mode", True, "isolated execution requested")
            else:
                add_check(
                    "repository", "Security mode", False,
                    "trusted host execution; use security_mode=isolated for untrusted code",
                    required=False,
                )
            sandbox = loaded.get("sandbox", {})
            if sandbox.get("enabled"):
                engine = sandbox["engine"]
                engine_path = shutil.which(engine)
                image_ok = False
                add_check(
                    "repository", "Sandbox engine", engine_path is not None,
                    f"{engine} is available" if engine_path else f"{engine} not found",
                )
                if engine_path:
                    image_ok, _ = probe(
                        [engine_path, "image", "inspect", sandbox["image"]], repo, timeout=30
                    )
                    add_check(
                        "repository", "Sandbox image", image_ok,
                        "sandbox image is available" if image_ok else "sandbox image is not available",
                    )
                if sandbox.get("allow_network"):
                    add_check(
                        "repository", "Sandbox network", False,
                        "enabled for developer and read-only reviewer; disable unless outbound access is required",
                        required=False,
                    )
                else:
                    add_check(
                        "repository", "Sandbox network", True,
                        "disabled for developer and read-only reviewer",
                        required=False,
                    )
            for role, settings in (("developer", loaded["developer_agent"]), ("reviewer", loaded["reviewer_agent"])):
                provider = settings["provider"]
                role_name = role.title()
                auth_mode = auth_mode_for_settings(settings)
                api_key_env = (
                    api_key_env_for_provider(provider, settings.get("api_key_env"))
                    if auth_mode in {"api_key", "both"} else None
                )
                credential_set = (
                    bool(os.environ.get(api_key_env)) if api_key_env else None
                )
                sandbox_passed = (
                    api_key_env in sandbox_env_for_provider(
                        provider, loaded.get("sandbox"), api_key_env,
                    )
                    if api_key_env and provider != "openrouter"
                    and loaded.get("sandbox", {}).get("enabled") else None
                )
                executable = None
                provider_ok = False
                native_ok: bool | None = None
                native_detail = "native CLI authentication was not selected"
                native_required = False
                if provider == "openrouter":
                    add_skip(
                        "agent", f"{role_name} provider ({provider})",
                        "API provider has no local executable",
                    )
                    provider_ok = True
                elif loaded.get("sandbox", {}).get("enabled"):
                    pass_env = sandbox_env_for_provider(
                        provider, loaded["sandbox"], api_key_env,
                    )
                    missing_env = [name for name in pass_env if not os.environ.get(name)]
                    if pass_env:
                        detail = (
                            "configured environment credentials are set"
                            if not missing_env else
                            "one or more configured environment credentials are missing"
                        )
                        add_check("agent", f"{role_name} sandbox credentials", not missing_env, detail)
                    else:
                        if api_key_env:
                            add_check(
                                "agent", f"{role_name} sandbox credentials", False,
                                "configured API credential environment variable is not listed in sandbox.pass_env",
                            )
                    if engine_path and image_ok:
                        image_executable = settings.get("executable_path") or provider
                        executable = str(image_executable)
                        provider_ok, _provider_output = probe(
                            [engine_path, "run", "--rm", "--network", "none",
                             "--entrypoint", str(image_executable),
                             loaded["sandbox"]["image"], "--version"],
                            repo, timeout=30,
                        )
                        add_check(
                            "agent", f"{role_name} provider ({provider})", provider_ok,
                            f"provider executable {image_executable!r} is available in the sandbox image"
                            if provider_ok else
                            f"provider executable {image_executable!r} failed its --version check in the sandbox image",
                        )
                    else:
                        add_check(
                            "agent", f"{role_name} provider ({provider})", False,
                            "provider executable was not probed because the sandbox engine or image is unavailable",
                        )
                    if auth_mode in {"native_cli", "both"}:
                        native_detail = (
                            "native CLI authentication is selected; the sandbox image is responsible "
                            "for its login state"
                        )
                else:
                    resolved = resolve_provider_executable(
                        provider, settings.get("executable_path"),
                    )
                    executable = resolved.path
                    provider_ok, _provider_output = probe([executable, "--version"], repo) if executable else (False, "")
                    if provider_ok and executable:
                        if provider == "copilot" and resolved.source == "GitHub CLI download":
                            detail = (
                                f"Copilot CLI was found at {display_provider_path(executable)} "
                                "but is not on PATH; add its directory to PATH or configure "
                                "executable_path/COPILOT_PATH"
                            )
                        elif provider == "copilot" and resolved.source in {"configuration", "COPILOT_PATH"}:
                            detail = f"Copilot CLI is available at {display_provider_path(executable)}"
                        else:
                            detail = "provider is available"
                    elif executable:
                        detail = "executable failed its --version check"
                    else:
                        if provider == "copilot":
                            gh_path = shutil.which("gh")
                            gh_available = bool(
                                gh_path and probe([gh_path, "copilot", "--help"], repo)[0]
                            )
                            if gh_available:
                                detail = (
                                    "executable not found on PATH; 'gh copilot' is "
                                    "available, locate the downloaded binary and set "
                                    "COPILOT_PATH or copilot executable_path"
                                )
                            else:
                                detail = (
                                    "executable not found on PATH; install Copilot CLI or "
                                    "set COPILOT_PATH/copilot executable_path"
                                )
                        else:
                            detail = "executable not found on PATH"
                    add_check("agent", f"{role_name} provider ({provider})", provider_ok, detail)
                    auth_args = PROVIDER_AUTH_STATUS_ARGS.get(provider)
                    native_required = bool(auth_args)
                    if auth_mode not in {"native_cli", "both"}:
                        native_ok = None
                        native_detail = "native CLI authentication was not checked because API-key mode is selected"
                    elif executable and provider_ok and auth_args:
                        native_ok, _auth_output = probe(
                            [executable, *auth_args], repo, timeout=30,
                        )
                        native_detail = (
                            "provider authentication is available" if native_ok else
                            "provider is not authenticated or its auth status could not be checked"
                        )
                    elif not executable:
                        native_ok = False
                        native_detail = "authentication was not checked because the executable is unavailable"
                    elif not provider_ok:
                        native_ok = False
                        native_detail = "authentication was not checked because the executable failed its version check"
                    else:
                        native_ok = False
                        native_detail = (
                            "provider CLI has no documented noninteractive authentication "
                            "status check"
                        )
                api_required = auth_mode in {"api_key", "both"}
                api_ok = (
                    bool(credential_set)
                    and (sandbox_passed is not False)
                    if api_required else True
                )
                if auth_mode == "native_cli":
                    auth_ok = bool(native_ok)
                    auth_required = native_required
                    auth_status = None if native_required else "skip"
                    auth_detail = f"mode: native CLI; status: {native_detail}"
                elif auth_mode == "api_key":
                    auth_ok = api_ok
                    auth_required = True
                    auth_status = None
                    credential_label = "GitHub token" if provider == "copilot" else "API key"
                    sandbox_detail = (
                        "; sandbox: passed" if sandbox_passed is True else
                        "; sandbox: not included" if sandbox_passed is False else ""
                    )
                    auth_detail = (
                        f"mode: API key ({credential_label}); "
                        f"status: {'set' if credential_set else 'missing'}{sandbox_detail}"
                    )
                else:
                    native_component_ok = bool(native_ok) if native_required else True
                    auth_ok = api_ok and native_component_ok
                    auth_required = True
                    auth_status = None
                    credential_label = "GitHub token" if provider == "copilot" else "API key"
                    auth_detail = (
                        "mode: both; "
                        f"{credential_label}: {'set' if credential_set else 'missing'}; "
                        f"native CLI: {native_detail}"
                    )
                add_check(
                    "agent", f"{role_name} agent authentication ({provider})",
                    auth_ok, auth_detail,
                    required=auth_required,
                    status=auth_status,
                    auth_mode=auth_mode,
                    credential_set=credential_set,
                    sandbox_passed=sandbox_passed,
                )
            if run_commands and worktree_ok:
                commands = list(loaded["test_commands"])
                if profile == "slow":
                    commands.extend(loaded["slow_test_commands"])
                try:
                    with (command_lock(command_worktree) if command_lock else nullcontext()):
                        for argv in commands:
                            if not json_output:
                                print(
                                    style_system_text(
                                        f"[DOCTOR] Running: {shlex.join(argv)}",
                                        system_prefix="DOCTOR",
                                    ),
                                    flush=True,
                                )
                            command_results.append(_run_configured_command(
                                argv,
                                command_worktree,
                                timeout=loaded["effective_test_timeout_seconds"],
                                sandbox=loaded.get("sandbox"),
                            ))
                except RuntimeError as exc:
                    add_check("repository", "Command worktree lock", False, str(exc))
                else:
                    commands_ok = all(item["status"] == "passed" for item in command_results)
                    add_check(
                        "repository", "Configured command execution", commands_ok,
                        _configured_command_detail(command_results, profile, command_worktree),
                    )
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            add_check("repository", "Agentic config", False, str(exc))
    else:
        add_check(
            "repository", "Agentic config", False,
            f"missing: {target_config} (run agentic init)",
        )
    success = all(
        bool(check["ok"]) or not bool(check["required"])
        for check in checks
    )
    category_order = {"repository": 0, "github": 1, "agent": 2}
    display_checks = sorted(
        checks, key=lambda check: category_order.get(str(check["category"]), 99),
    )
    public_checks = [_public_check(check) for check in display_checks]
    if json_output:
        print(json.dumps({
            "repository": str(repo),
            "ok": success,
            "command_profile": profile if run_commands else None,
            "command_worktree": str(command_worktree) if run_commands else None,
            "checks": public_checks,
            "command_results": _public_command_results(command_results),
        }, indent=2))
    else:
        print("Agentic doctor")
        print(f"Repository: {repo}")
        if run_commands:
            print(f"Command worktree: {command_worktree}")
        category_titles = {
            "repository": "Repository prerequisites",
            "github": "GitHub task access",
            "agent": "Agent access",
        }
        current_category = None
        for check in public_checks:
            category = str(check["category"])
            if category != current_category:
                print(category_titles.get(category, category.title()))
                current_category = category
            label = str(check["name"])
            detail = str(check["detail"])
            state = str(check["status"]).upper()
            print(
                style_system_text(
                    f"[{state:<4}] {label}: {detail}",
                    system_prefix=state,
                    style_status_tokens=False,
                )
            )
        if command_results:
            print("Configured commands:")
            for index, result in enumerate(command_results, start=1):
                status = str(result["status"]).upper()
                duration = result["duration_seconds"]
                print(
                    style_system_text(
                        f"[{status:<11}] configured command #{index} ({duration}s)",
                        system_prefix=status,
                        style_status_tokens=False,
                    )
                )
    return 0 if success else 1
