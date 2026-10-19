"""Repository prerequisite diagnostics.

The compatibility-facing runner keeps the public ``agentic.doctor`` entry
point, while this module owns the diagnostic workflow and its output format.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable

from agentic_config import load_config, sandbox_env_for_provider
from agentic_execution import _doctor_public_detail


AGENT_CONFIG_FILENAME = "agent_config.json"


def run_doctor(
    repo: Path,
    config_path: Path | None = None,
    *,
    json_output: bool = False,
    installed_git_version: Callable[..., tuple[int, int, int] | None],
    probe: Callable[..., tuple[bool, str]],
    preflight_test_commands: Callable[..., None],
    relative_worktree_paths_supported: Callable[[], bool],
) -> int:
    """Report local prerequisites and return nonzero when a required item is missing."""
    repo = repo.resolve()
    checks: list[tuple[str, bool, str, bool]] = []
    git_version = installed_git_version()
    checks.append(("Git", git_version is not None, ".".join(map(str, git_version)) if git_version else "not found", True))
    root_ok, _ = probe(["git", "rev-parse", "--show-toplevel"], repo)
    checks.append(("Git repository", root_ok, "repository detected" if root_ok else "not a repository", True))
    if root_ok:
        origin_ok, _ = probe(["git", "remote", "get-url", "origin"], repo)
        checks.append(("origin remote", origin_ok, "origin remote configured" if origin_ok else "missing origin remote", True))
    else:
        checks.append(("origin remote", False, "skipped", True))
    gh_path = shutil.which("gh")
    gh_ok, _ = probe(
        [gh_path or "gh", "auth", "status", "--active", "--hostname", "github.com"],
        repo, timeout=30,
    ) if gh_path else (False, "gh not found")
    checks.append(("GitHub CLI auth", gh_ok, "authenticated" if gh_ok else "not authenticated", True))
    target_config = (config_path or repo / AGENT_CONFIG_FILENAME).resolve()
    if target_config.is_file():
        try:
            loaded = load_config(target_config)
            preflight_test_commands(
                repo, loaded["test_commands"] + loaded["slow_test_commands"],
                sandbox=loaded.get("sandbox"),
            )
            checks.append(("Agentic config", True, str(target_config), True))
            checks.append(("Configured checks", True, f"{len(loaded['test_commands'])} fast / {len(loaded['slow_test_commands'])} slow", True))
            if loaded["security_mode"] == "isolated":
                checks.append(("Security mode", True, "isolated execution requested", True))
            else:
                checks.append((
                    "Security mode", False,
                    "trusted host execution; use security_mode=isolated for untrusted code",
                    False,
                ))
            sandbox = loaded.get("sandbox", {})
            if sandbox.get("enabled"):
                engine = sandbox["engine"]
                engine_path = shutil.which(engine)
                image_ok = False
                checks.append(("Sandbox engine", engine_path is not None,
                               f"{engine} is available" if engine_path else f"{engine} not found", True))
                if engine_path:
                    image_ok, _ = probe(
                        [engine_path, "image", "inspect", sandbox["image"]], repo, timeout=30
                    )
                    checks.append((
                        "Sandbox image", image_ok,
                        "sandbox image is available" if image_ok else "sandbox image is not available",
                        True,
                    ))
                if sandbox.get("allow_network"):
                    checks.append((
                        "Sandbox network", False,
                        "enabled for developer and read-only reviewer; disable unless outbound access is required",
                        False,
                    ))
                else:
                    checks.append((
                        "Sandbox network", True,
                        "disabled for developer and read-only reviewer",
                        False,
                    ))
            providers = []
            for role, settings in (("developer", loaded["developer_agent"]), ("reviewer", loaded["reviewer_agent"])):
                provider = settings["provider"]
                if provider in providers:
                    continue
                providers.append(provider)
                if provider == "openrouter":
                    env_name = settings["api_key_env"]
                    checks.append((f"{role.title()} provider ({provider})", bool(os.environ.get(env_name)),
                                   f"${env_name} is set" if os.environ.get(env_name) else f"${env_name} is missing", True))
                elif loaded.get("sandbox", {}).get("enabled"):
                    pass_env = sandbox_env_for_provider(provider, loaded["sandbox"])
                    missing_env = [name for name in pass_env if not os.environ.get(name)]
                    if pass_env:
                        detail = (
                            "configured environment credentials are set"
                            if not missing_env else
                            "missing: " + ", ".join(f"${name}" for name in missing_env)
                        )
                        checks.append((
                            f"{role.title()} sandbox credentials", not missing_env, detail, True,
                        ))
                    else:
                        checks.append((
                            f"{role.title()} provider ({provider})", False,
                            "provider credentials must be provisioned in the image or sandbox.pass_env",
                            False,
                        ))
                    if engine_path and image_ok:
                        provider_ok, provider_output = probe(
                            [engine_path, "run", "--rm", "--network", "none",
                             "--entrypoint", provider,
                             loaded["sandbox"]["image"], "--version"],
                            repo, timeout=30,
                        )
                        checks.append((
                            f"{role.title()} provider ({provider})", provider_ok,
                            "provider executable is available in the sandbox image"
                            if provider_ok else
                            f"provider executable is unavailable in the sandbox image: {provider_output[-240:]}",
                            True,
                        ))
                    else:
                        checks.append((
                            f"{role.title()} provider ({provider})", False,
                            "provider executable was not probed because the sandbox engine or image is unavailable",
                            True,
                        ))
                else:
                    executable = shutil.which(provider)
                    ok, _ = probe([executable or provider, "--version"], repo) if executable else (False, f"{provider} not found")
                    checks.append((f"{role.title()} provider ({provider})", ok, "provider is available" if ok else "provider is not available", True))
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            checks.append(("Agentic config", False, str(exc), True))
    else:
        checks.append(("Agentic config", False, f"missing: {target_config} (run agentic init)", True))
    if git_version is not None and os.environ.get("WSL_DISTRO_NAME"):
        if relative_worktree_paths_supported():
            checks.append(("WSL/Windows worktree paths", True, "relative paths supported", False))
        else:
            checks.append(("WSL/Windows worktree paths", False, "Git 2.48+ is required in WSL and Windows Git", False))
    success = all(ok or not required for _, ok, _, required in checks)
    if json_output:
        print(json.dumps({
            "repository": str(repo),
            "ok": success,
            "checks": [
                {
                    "name": label,
                    "ok": ok,
                    "required": required,
                    "status": "ok" if ok else ("fail" if required else "warn"),
                    "detail": _doctor_public_detail(label, ok, required),
                }
                for label, ok, _detail, required in checks
            ],
        }, indent=2))
    else:
        print("Agentic doctor")
        print(f"Repository: {repo}")
        for label, ok, _detail, required in checks:
            state = "OK" if ok else ("FAIL" if required else "WARN")
            print(f"[{state:<4}] {label}: {_doctor_public_detail(label, ok, required)}")
    return 0 if success else 1
