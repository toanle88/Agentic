"""Configuration, provider preflight, and repository setup helpers.

This module owns the target-repository configuration boundary.  The workflow
runner imports these helpers for compatibility, while keeping provider
execution, Git worktrees, review, and publication in their existing modules.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agentic_models import AgentSettings
from agentic_sandbox import sandbox_build_command, validate_sandbox


SUPPORTED_PROVIDERS = ("codex", "gemini", "claude", "copilot", "openrouter")
SECURITY_MODES = ("trusted", "isolated")
DEFAULT_SECURITY_MODE = "trusted"
DEFAULT_INIT_SECURITY_MODE = "trusted"
PROVIDER_SANDBOX_ENV = {
    "codex": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY",),
    "claude": ("ANTHROPIC_API_KEY",),
    "copilot": ("GH_TOKEN",),
    "openrouter": (),
}
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Bound worst-case repeated context cost; callers can raise this explicitly
# for unusually large tasks.
OPENROUTER_MAX_TURNS = 40
OPENROUTER_MAX_TOOL_TURNS = (1, 200)
OPENROUTER_COMMAND_TIMEOUT = (1, 86400)
DEFAULT_TEST_TIMEOUT = 300
DEFAULT_DEVELOPER_TIMEOUT = 600
DEFAULT_REVIEWER_TIMEOUT = 300
DEFAULT_TASK_TIMEOUT = 1800
MAX_TASK_TIMEOUT = 86400
DEFAULT_OPENROUTER_COMMAND_TIMEOUT = 300


def _effective_timeout(value: int | None, default: int) -> int:
    """Turn legacy ``null`` timeout settings into safe runtime defaults."""
    return default if value is None else value


def _runtime_agent_settings(config: dict[str, Any], role: str) -> dict[str, Any]:
    settings = dict(config.get(f"{role}_agent", {}))
    default = DEFAULT_DEVELOPER_TIMEOUT if role == "developer" else DEFAULT_REVIEWER_TIMEOUT
    settings["timeout_seconds"] = _effective_timeout(settings.get("timeout_seconds"), default)
    if settings.get("provider") == "openrouter":
        settings["command_timeout_seconds"] = _effective_timeout(
            settings.get("command_timeout_seconds"), DEFAULT_OPENROUTER_COMMAND_TIMEOUT
        )
    return settings


def sandbox_env_for_provider(provider: str, sandbox: dict[str, Any] | None) -> list[str]:
    """Select only the explicit credential names relevant to one provider."""
    configured = list((sandbox or {}).get("pass_env", []))
    if provider == "openrouter":
        return []
    expected = set(PROVIDER_SANDBOX_ENV.get(provider, ()))
    matched = [name for name in configured if name in expected]
    return matched or configured


def _agent_settings_from_config(data: dict[str, Any], section: str, *,
                                legacy_provider: str, legacy_model: str | None,
                                legacy_reasoning: str | None,
                                legacy_timeout: int | None) -> AgentSettings:
    raw = data.get(section)
    if raw is None and section == "developer":
        raw = data.get("agent")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{section} must be an object")
    for secret_field in ("api_key", "apiKey", "token", "secret"):
        if secret_field in raw:
            raise ValueError(
                f"{section}.{secret_field} is not accepted; store credentials in the environment"
            )

    provider = raw.get("provider", data.get(f"{section}_provider", legacy_provider))
    model = raw.get("model", legacy_model)
    reasoning = raw.get("reasoning_effort", legacy_reasoning)
    timeout = raw.get("timeout_seconds", legacy_timeout)
    api_key_env = raw.get("api_key_env")
    base_url = raw.get("base_url")
    max_tool_turns = raw.get("max_tool_turns")
    command_timeout = raw.get("command_timeout_seconds")
    allow_network = raw.get("allow_network", False)
    json_mode = raw.get("json_mode", False)
    if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
        choices = ", ".join(SUPPORTED_PROVIDERS)
        raise ValueError(f"{section}.provider must be one of: {choices}")
    for name, value in (("model", model), ("reasoning_effort", reasoning), ("base_url", base_url)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{section}.{name} must be a nonempty string or null")
    if timeout is not None and (type(timeout) is not int or not 1 <= timeout <= 86400):
        raise ValueError(f"{section}.timeout_seconds must be null or an integer from 1 to 86400")
    if max_tool_turns is not None and (
        type(max_tool_turns) is not int
        or not OPENROUTER_MAX_TOOL_TURNS[0] <= max_tool_turns <= OPENROUTER_MAX_TOOL_TURNS[1]
    ):
        raise ValueError(
            f"{section}.max_tool_turns must be null or an integer from "
            f"{OPENROUTER_MAX_TOOL_TURNS[0]} to {OPENROUTER_MAX_TOOL_TURNS[1]}"
        )
    if command_timeout is not None and (
        type(command_timeout) is not int
        or not OPENROUTER_COMMAND_TIMEOUT[0] <= command_timeout <= OPENROUTER_COMMAND_TIMEOUT[1]
    ):
        raise ValueError(
            f"{section}.command_timeout_seconds must be null or an integer from "
            f"{OPENROUTER_COMMAND_TIMEOUT[0]} to {OPENROUTER_COMMAND_TIMEOUT[1]}"
        )
    if type(allow_network) is not bool:
        raise ValueError(f"{section}.allow_network must be a boolean")
    if type(json_mode) is not bool:
        raise ValueError(f"{section}.json_mode must be a boolean")
    if api_key_env is not None and (
        not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env)
    ):
        raise ValueError(f"{section}.api_key_env must be a valid environment variable name")
    if provider == "openrouter":
        if not model:
            raise ValueError(f"{section}.model is required when provider is openrouter")
        api_key_env = api_key_env or "OPENROUTER_API_KEY"
        base_url = base_url or DEFAULT_OPENROUTER_BASE_URL
        if not re.match(r"^https?://", base_url, re.IGNORECASE):
            raise ValueError(f"{section}.base_url must start with http:// or https://")
    elif api_key_env is not None or base_url is not None:
        raise ValueError(f"{section}.api_key_env and {section}.base_url are only valid for openrouter")
    elif max_tool_turns is not None or command_timeout is not None or allow_network or json_mode:
        raise ValueError(
            f"{section}.max_tool_turns, {section}.command_timeout_seconds, and "
            f"{section}.allow_network, and {section}.json_mode are only valid for openrouter"
        )
    return AgentSettings(
        provider, model, reasoning, timeout, api_key_env, base_url,
        max_tool_turns, command_timeout, allow_network, json_mode,
    )


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    task_source = data.get("task_source", "github")
    if task_source not in {"github", "local"}:
        raise ValueError("task_source must be 'github' or 'local'")
    commands = data.get("test_commands")
    if not isinstance(commands, list) or not commands or not all(
        isinstance(c, list) and c and all(isinstance(part, str) and part for part in c)
        for c in commands
    ):
        raise ValueError("test_commands must be a nonempty list of argv arrays")
    slow_commands = data.get("slow_test_commands", [])
    if not isinstance(slow_commands, list) or not all(
        isinstance(c, list) and c and all(isinstance(part, str) and part for part in c)
        for c in slow_commands
    ):
        raise ValueError("slow_test_commands must be a list of argv arrays")
    retries = data.get("max_retries", 3)
    max_files = data.get("max_files")
    codex_timeout = data.get("codex_timeout_seconds")
    test_timeout = data.get("test_timeout_seconds")
    task_timeout = data.get("task_timeout_seconds", DEFAULT_TASK_TIMEOUT)
    security_mode = data.get("security_mode", DEFAULT_SECURITY_MODE)
    codex_model = data.get("codex_model")
    reasoning_effort = data.get("codex_reasoning_effort")
    system_prompt = data.get("system_prompt")
    if type(retries) is not int or not 0 <= retries <= 5:
        raise ValueError("max_retries must be an integer from 0 to 5")
    if max_files is not None and (type(max_files) is not int or not 1 <= max_files <= 100):
        raise ValueError("max_files must be null or an integer from 1 to 100")
    if codex_timeout is not None and (type(codex_timeout) is not int or not 1 <= codex_timeout <= 86400):
        raise ValueError("codex_timeout_seconds must be null or an integer from 1 to 86400")
    if test_timeout is not None and (type(test_timeout) is not int or not 1 <= test_timeout <= 86400):
        raise ValueError("test_timeout_seconds must be null or an integer from 1 to 86400")
    if type(task_timeout) is not int or not 1 <= task_timeout <= MAX_TASK_TIMEOUT:
        raise ValueError(f"task_timeout_seconds must be an integer from 1 to {MAX_TASK_TIMEOUT}")
    if security_mode not in SECURITY_MODES:
        raise ValueError(
            f"security_mode must be one of: {', '.join(SECURITY_MODES)}"
        )
    for name, value in (("codex_model", codex_model), ("codex_reasoning_effort", reasoning_effort)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{name} must be a nonempty string or null")
    if system_prompt is not None and (
        not isinstance(system_prompt, str) or not system_prompt.strip()
    ):
        raise ValueError("system_prompt must be a nonempty string or null")
    developer_agent = _agent_settings_from_config(
        data, "developer", legacy_provider="codex", legacy_model=codex_model,
        legacy_reasoning=reasoning_effort, legacy_timeout=codex_timeout,
    )
    reviewer_agent = _agent_settings_from_config(
        data, "reviewer", legacy_provider="codex", legacy_model=codex_model,
        legacy_reasoning=reasoning_effort, legacy_timeout=codex_timeout,
    )
    sandbox = validate_sandbox(data.get("sandbox"))
    if security_mode == "isolated" and not sandbox["enabled"]:
        raise ValueError("security_mode 'isolated' requires sandbox.enabled=true")
    result = {
        "task_source": task_source,
        "test_commands": commands,
        "slow_test_commands": slow_commands,
        "max_retries": retries,
        "max_files": max_files,
        "codex_timeout_seconds": codex_timeout,
        "test_timeout_seconds": test_timeout,
        "task_timeout_seconds": task_timeout,
        # An omitted test timeout keeps the safe default; an explicit null
        # disables the per-test-command timeout.
        "effective_test_timeout_seconds": (
            DEFAULT_TEST_TIMEOUT if "test_timeout_seconds" not in data else test_timeout
        ),
        "effective_developer_timeout_seconds": _effective_timeout(
            developer_agent.timeout_seconds, DEFAULT_DEVELOPER_TIMEOUT
        ),
        "effective_reviewer_timeout_seconds": _effective_timeout(
            reviewer_agent.timeout_seconds, DEFAULT_REVIEWER_TIMEOUT
        ),
        "codex_model": codex_model,
        "codex_reasoning_effort": reasoning_effort,
        "security_mode": security_mode,
        "developer_agent": developer_agent.as_dict(),
        "reviewer_agent": reviewer_agent.as_dict(),
        "sandbox": sandbox,
    }
    # Keep the returned shape of older configurations unchanged. An explicit
    # null remains distinguishable from an omitted setting for callers that
    # inspect the normalized configuration.
    if "system_prompt" in data:
        result["system_prompt"] = system_prompt
    return result


def preflight_agent_settings(*settings: dict[str, Any],
                             sandbox: dict[str, Any] | None = None) -> None:
    """Check selected providers before creating work or spending API time."""
    checked: set[str] = set()
    for item in settings:
        provider = str(item.get("provider", "codex"))
        if provider in checked:
            continue
        checked.add(provider)
        if provider == "openrouter":
            env_name = item.get("api_key_env") or "OPENROUTER_API_KEY"
            if not os.environ.get(env_name):
                raise RuntimeError(f"OpenRouter API key is missing; set ${env_name}")
            continue
        if sandbox and sandbox.get("enabled"):
            missing_env = [
                name for name in sandbox_env_for_provider(provider, sandbox)
                if not os.environ.get(name)
            ]
            if missing_env:
                names = ", ".join(f"${name}" for name in missing_env)
                raise RuntimeError(
                    f"Sandbox credential environment variables are missing: {names}"
                )
            continue
        executable = shutil.which(provider)
        if not executable:
            install_hint = {
                "codex": "install/authenticate the Codex CLI",
                "gemini": "install/authenticate Gemini CLI",
                "claude": "install/authenticate Claude Code",
                "copilot": "install/authenticate GitHub Copilot CLI",
            }.get(provider, "install the provider CLI")
            raise RuntimeError(f"Configured provider {provider!r} is unavailable; {install_hint}")


def preflight_security(
    security_mode: str, sandbox: dict[str, Any], *, repo: Path | None = None,
) -> None:
    """Fail closed when an explicitly isolated run cannot be isolated."""
    if security_mode not in SECURITY_MODES:
        raise RuntimeError(f"Unknown security mode: {security_mode}")
    if security_mode == "isolated" and not sandbox.get("enabled"):
        raise RuntimeError("Isolated security mode requires an enabled sandbox")
    if sandbox.get("enabled"):
        engine = sandbox.get("engine", "docker")
        if not shutil.which(engine):
            raise RuntimeError(
                f"Sandbox engine {engine!r} is unavailable; isolated execution cannot continue"
            )
        if sandbox.get("dockerfile"):
            if repo is None:
                raise RuntimeError(
                    "A repository path is required to build sandbox.dockerfile"
                )
            build_command = sandbox_build_command(sandbox, repo)
        else:
            build_command = None
        if build_command is not None:
            try:
                result = subprocess.run(
                    build_command, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", timeout=1800, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"Sandbox image build could not start or timed out: {exc}") from exc
            if result.returncode:
                detail = result.stdout.strip()[-2000:]
                raise RuntimeError(
                    f"Sandbox image build failed with exit code {result.returncode}: {detail}"
                )


def detect_project_language(repo: Path) -> tuple[str | None, str]:
    """Return a conservative language hint for init and sandbox template selection."""
    markers = (
        ("python", ("pyproject.toml", "setup.py", "requirements.txt"), "Detected Python project"),
        ("node", ("package.json",), "Detected Node.js project"),
        ("go", ("go.mod",), "Detected Go module"),
        ("rust", ("Cargo.toml",), "Detected Rust crate"),
        ("java", ("pom.xml", "build.gradle", "build.gradle.kts"), "Detected Java project"),
    )
    for language, files, message in markers:
        if any((repo / name).is_file() for name in files):
            return language, message
    if any(repo.glob(pattern) for pattern in ("*.csproj", "*.sln", "global.json")):
        return "dotnet", "Detected .NET project"
    return None, "No supported project language detected"


def test_commands_for_profile(config: dict[str, Any], profile: str) -> list[list[str]]:
    """Return fast checks or fast checks plus the opt-in slow checks."""
    if profile == "fast":
        return config["test_commands"]
    if profile != "slow":
        raise ValueError(f"Unknown test profile: {profile}")
    slow = config.get("slow_test_commands", [])
    if not slow:
        raise RuntimeError(
            "The slow test profile is not configured; add slow_test_commands "
            "to the target agent_config.json"
        )
    return config["test_commands"] + slow


def detect_test_commands(repo: Path) -> tuple[list[list[str]], str]:
    """Choose a conservative starter check for ``agentic init``."""
    if (repo / "go.mod").is_file():
        return [["go", "test", "./..."]], "Detected Go module"
    if (repo / "Cargo.toml").is_file():
        return [["cargo", "test"]], "Detected Rust crate"
    package_json = repo / "package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            has_test = isinstance(package, dict) and isinstance(package.get("scripts"), dict) and "test" in package["scripts"]
        except (OSError, json.JSONDecodeError):
            has_test = False
        if has_test:
            if (repo / "pnpm-lock.yaml").is_file():
                return [["pnpm", "test"]], "Detected package.json test script and pnpm lockfile"
            if (repo / "yarn.lock").is_file():
                return [["yarn", "test"]], "Detected package.json test script and yarn lockfile"
            return [["npm", "test"]], "Detected package.json test script"
    makefile = next((repo / name for name in ("Makefile", "makefile") if (repo / name).is_file()), None)
    if makefile:
        content = makefile.read_text(encoding="utf-8", errors="replace")
        for target in ("check", "test"):
            if re.search(rf"(?m)^{re.escape(target)}\s*:", content):
                return [["make", target]], f"Detected Makefile {target} target"
    if (repo / "pyproject.toml").is_file() or (repo / "setup.py").is_file():
        if (repo / "tests").is_dir():
            return [["python", "-m", "unittest", "discover", "-s", "tests", "-q"]], "Detected Python tests directory"
    return [["git", "diff", "--check"]], "No test framework detected; using Git whitespace validation"


def _git_info_exclude_path(repo: Path) -> Path:
    """Resolve Git's local exclude file without assuming a directory-style ``.git``."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"Could not resolve Git info/exclude for {repo}: {exc}"
        ) from exc
    output = result.stdout.strip()
    if result.returncode or not output:
        detail = output or "Git returned no path"
        raise RuntimeError(
            f"Could not resolve Git info/exclude for {repo}: {detail}"
        )
    path = Path(output.splitlines()[-1])
    return path if path.is_absolute() else (repo / path).resolve()


def _append_git_exclude_entry(repo: Path, entry: str) -> tuple[Path, bool]:
    """Add an entry to Git's local exclude file, preserving existing content."""
    path = _git_info_exclude_path(repo)
    try:
        if path.exists():
            with path.open("r", encoding="utf-8", newline="") as stream:
                content = stream.read()
        else:
            content = ""
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"Could not read Git info/exclude {path}: {exc}") from exc
    lines = {line.strip() for line in content.splitlines()}
    if entry in lines:
        return path, False
    newline = "\r\n" if "\r\n" in content else "\r" if "\r" in content else "\n"
    prefix = "" if not content or content.endswith(("\n", "\r")) else newline
    updated = content + prefix + f"# Agentic local configuration{newline}{entry}{newline}"
    try:
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"Could not write Git info/exclude {path}: {exc}") from exc
    return path, True


def default_provider_settings(provider: str) -> dict[str, Any]:
    """Return a copy-ready provider block for ``agentic init``."""
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"provider must be one of: {', '.join(SUPPORTED_PROVIDERS)}")
    settings: dict[str, Any] = {
        "provider": provider,
        "model": None,
        "reasoning_effort": None,
        "timeout_seconds": DEFAULT_DEVELOPER_TIMEOUT,
    }
    if provider == "openrouter":
        settings.update({
            "model": "openai/gpt-5.4",
            "api_key_env": "OPENROUTER_API_KEY",
            "base_url": DEFAULT_OPENROUTER_BASE_URL,
            "max_tool_turns": OPENROUTER_MAX_TURNS,
            "command_timeout_seconds": DEFAULT_OPENROUTER_COMMAND_TIMEOUT,
            "allow_network": False,
            "json_mode": False,
        })
    return settings


def sandbox_env_for_providers(*providers: str) -> list[str]:
    """Return provider credential names without copying any secret values."""
    names: list[str] = []
    for provider in providers:
        for name in PROVIDER_SANDBOX_ENV.get(provider, ()):
            if name not in names:
                names.append(name)
    return names
