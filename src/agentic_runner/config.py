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
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import AgentSettings, DEFAULT_GITHUB_HOST
from .policy import (
    validate_permission_policy,
    validate_permission_policy_container,
)
from .providers import (
    resolve_provider_executable,
    validate_provider_permission_policy,
)
from .sandbox import sandbox_build_context, validate_sandbox
from .ci_repair import validate_repair_limits


SUPPORTED_PROVIDERS = ("codex", "gemini", "claude", "copilot", "openrouter")
SECURITY_MODES = ("trusted", "isolated")
WORKFLOW_MODES = ("quick", "standard", "strict")
DEFAULT_SECURITY_MODE = "trusted"
DEFAULT_INIT_SECURITY_MODE = "trusted"
PROVIDER_SANDBOX_ENV = {
    "codex": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY",),
    "claude": ("ANTHROPIC_API_KEY",),
    "copilot": ("GH_TOKEN",),
    "openrouter": (),
}
PROVIDER_API_KEY_ENV = {
    "codex": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "copilot": "GH_TOKEN",
    "openrouter": "OPENROUTER_API_KEY",
}
# Native provider CLIs receive only variables needed to locate executables,
# user configuration, temporary files, and terminal formatting.  In
# particular, do not inherit the host environment wholesale: variables such
# as PYTHONPATH, BASH_ENV, NODE_OPTIONS, and LD_PRELOAD can alter provider
# code loading or command execution.
_PROVIDER_RUNTIME_ENV_ALLOWLIST = frozenset({
    "PATH", "Path", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "TERM", "COLORTERM", "NO_COLOR", "CI", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "SYSTEMROOT", "SystemRoot", "WINDIR", "ComSpec", "COMSPEC",
    "GH_HOST", "COPILOT_GH_HOST",
    "PATHEXT",
})
_PROVIDER_RUNTIME_ENV_ALLOWLIST_CASEFOLDED = frozenset(
    name.casefold() for name in _PROVIDER_RUNTIME_ENV_ALLOWLIST
)
AUTH_MODES = ("native_cli", "api_key", "both")
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROVIDER_CONTROL_ENV_NAMES = frozenset({
    "BASH_ENV", "CDPATH", "CLASSPATH", "COMSPEC", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "ENV", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_SYSTEM", "GIT_EXEC_PATH", "GIT_SSH_COMMAND", "GIT_TEMPLATE_DIR",
    "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "LD_LIBRARY_PATH", "LD_PRELOAD",
    "NODE_OPTIONS", "NODE_PATH", "PERL5LIB", "PERL5OPT", "PYTHONHOME",
    "PYTHONPATH", "PYTHONSTARTUP", "RUBYLIB", "RUBYOPT", "SHELL",
    "_JAVA_OPTIONS",
})
_PROVIDER_FORBIDDEN_CREDENTIAL_NAMES = frozenset(
    _PROVIDER_RUNTIME_ENV_ALLOWLIST_CASEFOLDED
    | {name.casefold() for name in _PROVIDER_CONTROL_ENV_NAMES}
)
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_ALLOWED_HOSTS = frozenset({"openrouter.ai"})
# Keep this list intentionally small: these are models validated against
# Agentic's OpenRouter tool-calling workflow, not an exhaustive OpenRouter
# catalog. Explicit model settings remain supported for experimentation.
SUPPORTED_OPENROUTER_MODELS = ("openai/gpt-6-luna",)
DEFAULT_OPENROUTER_MODEL = SUPPORTED_OPENROUTER_MODELS[0]
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
SAFE_GIT_DIFF_CHECK = ("git", "diff", "--no-ext-diff", "--no-textconv", "--check")
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
PROVIDER_SETTING_FIELDS = frozenset({
    "provider", "model", "reasoning_effort", "timeout_seconds", "auth",
    "api_key_env", "base_url", "allow_custom_endpoint", "executable_path",
    "host", "max_tool_turns", "command_timeout_seconds", "allow_network",
    "json_mode", "permission_policy",
})
AUTOMATION_FIELDS = frozenset({"auto_publish"})


def _validate_host(value: Any, field: str, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not HOST_RE.fullmatch(value) or ".." in value:
        raise ValueError(f"{field} must be a hostname without a scheme or path")
    return value


def _effective_timeout(value: int | None, default: int) -> int:
    """Turn legacy ``null`` timeout settings into safe runtime defaults."""
    return default if value is None else value


def validate_openrouter_base_url(
    value: str, field: str = "base_url", *, allow_custom_endpoint: bool = False,
) -> str:
    """Validate an OpenRouter endpoint before a credential can be sent to it."""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() != "https":
        raise ValueError(f"{field} must use https://; credentials are never sent over HTTP")
    if not host or parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must be an HTTPS URL without embedded credentials")
    if parsed.fragment:
        raise ValueError(f"{field} must not contain a URL fragment")
    if not allow_custom_endpoint and host not in OPENROUTER_ALLOWED_HOSTS:
        raise ValueError(
            f"{field} must use an approved OpenRouter host or set "
            "allow_custom_endpoint=true for an explicit HTTPS custom endpoint"
        )
    return value


def _runtime_agent_settings(config: dict[str, Any], role: str) -> dict[str, Any]:
    settings = dict(config.get(f"{role}_agent", {}))
    default = DEFAULT_DEVELOPER_TIMEOUT if role == "developer" else DEFAULT_REVIEWER_TIMEOUT
    settings["timeout_seconds"] = _effective_timeout(settings.get("timeout_seconds"), default)
    if settings.get("provider") == "openrouter":
        settings["command_timeout_seconds"] = _effective_timeout(
            settings.get("command_timeout_seconds"), DEFAULT_OPENROUTER_COMMAND_TIMEOUT
        )
    return settings


def api_key_env_for_provider(provider: str, api_key_env: str | None = None) -> str | None:
    """Return the selected provider credential name without reading its value."""
    return api_key_env or PROVIDER_API_KEY_ENV.get(provider)


def _validate_provider_credential_name(name: str, field: str) -> None:
    if not isinstance(name, str) or not ENVIRONMENT_NAME_RE.fullmatch(name):
        raise ValueError(f"{field} must be a valid environment variable name")
    if name.casefold() in _PROVIDER_FORBIDDEN_CREDENTIAL_NAMES:
        raise ValueError(
            f"{field} must not use a provider runtime or control environment variable"
        )


def provider_process_environment(provider: str, api_key_env: str | None) -> dict[str, str]:
    """Return a sanitized environment with only the selected credential."""
    canonical = PROVIDER_API_KEY_ENV.get(provider)
    selected_name = api_key_env or canonical
    if api_key_env is not None:
        _validate_provider_credential_name(api_key_env, "api_key_env")
    environment = {
        name: value for name, value in os.environ.items()
        if name.casefold() in _PROVIDER_RUNTIME_ENV_ALLOWLIST_CASEFOLDED
    }
    value = os.environ.get(selected_name) if selected_name else None
    if value and selected_name:
        # Keep the explicitly configured name for sandbox pass-through, and
        # map aliases to the provider's canonical name for native CLIs.
        environment[selected_name] = value
    if value and canonical:
        environment[canonical] = value
    return environment


def auth_mode_for_settings(settings: dict[str, Any]) -> str:
    """Resolve the explicit or compatibility authentication mode."""
    provider = str(settings.get("provider", "codex"))
    configured = settings.get("auth")
    if configured is None:
        if provider == "openrouter" or settings.get("api_key_env") is not None:
            return "api_key"
        return "native_cli"
    if configured == "native":
        configured = "native_cli"
    if configured not in AUTH_MODES:
        raise ValueError(
            f"{provider} auth must be one of: {', '.join(AUTH_MODES)}"
        )
    return str(configured)


def sandbox_env_for_provider(provider: str, sandbox: dict[str, Any] | None,
                             api_key_env: str | None = None) -> list[str]:
    """Select only the explicit credential names relevant to one provider."""
    configured = list((sandbox or {}).get("pass_env", []))
    if provider == "openrouter":
        return []
    if api_key_env is not None:
        return [name for name in configured if name == api_key_env]
    expected = set(PROVIDER_SANDBOX_ENV.get(provider, ()))
    matched = [name for name in configured if name in expected]
    return matched or configured


def _agent_settings_from_config(data: dict[str, Any], section: str, *,
                                legacy_provider: str, legacy_model: str | None,
                                legacy_reasoning: str | None,
                                legacy_timeout: int | None,
                                security_mode: str,
                                fallback: Mapping[str, Any] | None = None) -> AgentSettings:
    defaults = dict(fallback or {})
    raw = data.get(section)
    if raw is None and section == "developer":
        raw = data.get("agent")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{section} must be an object")
    unknown = sorted(set(raw) - PROVIDER_SETTING_FIELDS)
    if unknown:
        raise ValueError(
            f"{section} contains unsupported field(s): {', '.join(unknown)}"
        )
    for secret_field in ("api_key", "apiKey", "token", "secret"):
        if secret_field in raw:
            raise ValueError(
                f"{section}.{secret_field} is not accepted; store credentials in the environment"
            )

    provider = raw.get(
        "provider", data.get(f"{section}_provider", defaults.get("provider", legacy_provider))
    )
    provider_changed = provider != defaults.get("provider")
    model = raw.get("model", defaults.get("model", legacy_model))
    reasoning = raw.get("reasoning_effort", defaults.get("reasoning_effort", legacy_reasoning))
    timeout = raw.get("timeout_seconds", defaults.get("timeout_seconds", legacy_timeout))

    def provider_default(name: str, default: Any = None) -> Any:
        if name in raw:
            return raw[name]
        if not provider_changed and name in defaults:
            return defaults[name]
        return default

    auth = provider_default("auth")
    api_key_env = provider_default("api_key_env")
    base_url = provider_default("base_url")
    allow_custom_endpoint = provider_default("allow_custom_endpoint", False)
    executable_path = provider_default("executable_path")
    host = _validate_host(provider_default("host"), f"{section}.host")
    max_tool_turns = provider_default("max_tool_turns")
    command_timeout = provider_default("command_timeout_seconds")
    allow_network = provider_default("allow_network", False)
    json_mode = provider_default("json_mode", False)
    policy_value = raw.get("permission_policy")
    policy_explicit = policy_value is not None
    if policy_value is None:
        policy_config = data.get("permission_policy")
        if policy_config is None:
            policy_config = data.get("permissions")
        if isinstance(policy_config, dict) and section in policy_config:
            policy_value = policy_config[section]
            policy_explicit = True
        elif policy_config is not None and not isinstance(policy_config, dict):
            policy_value = policy_config
            policy_explicit = True
        elif isinstance(policy_config, dict) and any(
            name in policy_config for name in (
                "read_files", "search_files", "edit_files", "create_files",
                "git_read", "run_checks", "network", "allow_dangerous",
            )
        ):
            policy_value = policy_config
            policy_explicit = True
        elif "permission_policy" in defaults:
            policy_value = defaults["permission_policy"]
    if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
        choices = ", ".join(SUPPORTED_PROVIDERS)
        raise ValueError(f"{section}.provider must be one of: {choices}")
    for name, value in (("model", model), ("reasoning_effort", reasoning), ("base_url", base_url)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{section}.{name} must be a nonempty string or null")
    if executable_path is not None and (
        not isinstance(executable_path, str) or not executable_path.strip()
    ):
        raise ValueError(f"{section}.executable_path must be a nonempty string or null")
    if provider != "copilot" and executable_path is not None:
        raise ValueError(f"{section}.executable_path is only valid for copilot")
    if provider != "copilot" and host is not None:
        raise ValueError(f"{section}.host is only valid for copilot")
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
    if section == "investigator":
        if raw.get("allow_network") is True or (
            policy_explicit and isinstance(policy_value, dict)
            and policy_value.get("network") is True
        ):
            raise ValueError("investigator network access is not permitted")
        # Reviewer fallback may contain network capability. It remains valid
        # for the reviewer, but must not cross the investigator boundary.
        allow_network = False
        if isinstance(policy_value, dict) and policy_value.get("network"):
            policy_value = dict(policy_value)
            policy_value["network"] = False
    if type(json_mode) is not bool:
        raise ValueError(f"{section}.json_mode must be a boolean")
    if type(allow_custom_endpoint) is not bool:
        raise ValueError(f"{section}.allow_custom_endpoint must be a boolean")
    if api_key_env is not None:
        _validate_provider_credential_name(api_key_env, f"{section}.api_key_env")
    if auth == "native":
        auth = "native_cli"
    if auth is not None and auth not in AUTH_MODES:
        raise ValueError(f"{section}.auth must be one of: {', '.join(AUTH_MODES)}")
    if provider == "openrouter":
        if not model:
            raise ValueError(f"{section}.model is required when provider is openrouter")
        api_key_env = api_key_env or "OPENROUTER_API_KEY"
        auth = auth or "api_key"
        if auth != "api_key":
            raise ValueError(f"{section}.auth must be api_key for openrouter")
        base_url = base_url or DEFAULT_OPENROUTER_BASE_URL
        validate_openrouter_base_url(
            base_url, f"{section}.base_url",
            allow_custom_endpoint=allow_custom_endpoint,
        )
    elif base_url is not None:
        raise ValueError(f"{section}.base_url is only valid for openrouter")
    elif allow_custom_endpoint:
        raise ValueError(f"{section}.allow_custom_endpoint is only valid for openrouter")
    else:
        auth = auth or ("api_key" if api_key_env is not None else "native_cli")
        if auth == "native_cli" and api_key_env is not None:
            raise ValueError(
                f"{section}.api_key_env requires auth=api_key or auth=both"
            )
        if auth in {"api_key", "both"}:
            api_key_env = api_key_env or PROVIDER_API_KEY_ENV[provider]
        if max_tool_turns is not None or command_timeout is not None or allow_network or json_mode:
            raise ValueError(
                f"{section}.max_tool_turns, {section}.command_timeout_seconds, and "
                f"{section}.allow_network, and {section}.json_mode are only valid for openrouter"
            )
    permission_policy = validate_permission_policy(
        policy_value,
        role="developer" if section == "developer" else section,
        security_mode=security_mode,
        default_network=(
            bool(allow_network)
            if provider == "openrouter" and policy_value is None else False
        ),
    )
    validate_provider_permission_policy(provider, permission_policy)
    return AgentSettings(
        provider=provider,
        model=model,
        reasoning_effort=reasoning,
        timeout_seconds=timeout,
        auth=auth or "native_cli",
        api_key_env=api_key_env,
        base_url=base_url,
        allow_custom_endpoint=allow_custom_endpoint,
        max_tool_turns=max_tool_turns,
        command_timeout_seconds=command_timeout,
        allow_network=allow_network,
        json_mode=json_mode,
        executable_path=executable_path,
        host=host,
        permission_policy=permission_policy,
    )


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("configuration root must be an object")
    for field_name in ("permission_policy", "permissions"):
        validate_permission_policy_container(data.get(field_name), field_name=field_name)
    task_source = data.get("task_source", "github")
    if not isinstance(task_source, str) or task_source not in {"github", "local"}:
        raise ValueError("task_source must be 'github' or 'local'")
    github_host = _validate_host(
        data.get("github_host", DEFAULT_GITHUB_HOST), "github_host", allow_none=False,
    )
    automation = data.get("automation", {})
    if not isinstance(automation, dict):
        raise ValueError("automation must be an object")
    unknown_automation = sorted(set(automation) - AUTOMATION_FIELDS)
    if unknown_automation:
        raise ValueError(
            "automation contains unsupported field(s): " + ", ".join(unknown_automation)
        )
    auto_publish = automation.get("auto_publish", False)
    if type(auto_publish) is not bool:
        raise ValueError("automation.auto_publish must be a boolean")
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
    ci_repair = validate_repair_limits(data.get("ci_repair"))
    workflow_mode = data.get("workflow_mode", "standard")
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
    if not isinstance(security_mode, str) or security_mode not in SECURITY_MODES:
        raise ValueError(
            f"security_mode must be one of: {', '.join(SECURITY_MODES)}"
        )
    if not isinstance(workflow_mode, str) or workflow_mode not in WORKFLOW_MODES:
        raise ValueError(
            f"workflow_mode must be one of: {', '.join(WORKFLOW_MODES)}"
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
        security_mode=security_mode,
    )
    reviewer_agent = _agent_settings_from_config(
        data, "reviewer", legacy_provider="codex", legacy_model=codex_model,
        legacy_reasoning=reasoning_effort, legacy_timeout=codex_timeout,
        security_mode=security_mode,
    )
    investigator_agent = _agent_settings_from_config(
        data, "investigator", legacy_provider="codex", legacy_model=codex_model,
        legacy_reasoning=reasoning_effort, legacy_timeout=codex_timeout,
        security_mode=security_mode, fallback=reviewer_agent.as_dict(),
    )
    sandbox = validate_sandbox(data.get("sandbox"))
    if security_mode == "isolated" and not sandbox["enabled"]:
        raise ValueError("security_mode 'isolated' requires sandbox.enabled=true")
    result = {
        "task_source": task_source,
        "github_host": github_host,
        "automation": {"auto_publish": auto_publish},
        "test_commands": commands,
        "slow_test_commands": slow_commands,
        "max_retries": retries,
        "max_files": max_files,
        "codex_timeout_seconds": codex_timeout,
        "test_timeout_seconds": test_timeout,
        "task_timeout_seconds": task_timeout,
        "ci_repair": ci_repair,
        "workflow_mode": workflow_mode,
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
        "effective_investigator_timeout_seconds": _effective_timeout(
            investigator_agent.timeout_seconds, DEFAULT_REVIEWER_TIMEOUT
        ),
        "codex_model": codex_model,
        "codex_reasoning_effort": reasoning_effort,
        "security_mode": security_mode,
        "developer_agent": developer_agent.as_dict(),
        "reviewer_agent": reviewer_agent.as_dict(),
        "investigator_agent": investigator_agent.as_dict(),
        "permission_policy": {
            "developer": developer_agent.permission_policy,
            "reviewer": reviewer_agent.permission_policy,
            "investigator": investigator_agent.permission_policy,
        },
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
    """Check each configured role before creating work or spending API time."""
    for item in settings:
        provider = str(item.get("provider", "codex"))
        auth = auth_mode_for_settings(item)
        api_key_env = (
            api_key_env_for_provider(provider, item.get("api_key_env"))
            if auth in {"api_key", "both"} else None
        )
        if api_key_env is not None:
            if not os.environ.get(api_key_env):
                credential = "GitHub token" if provider == "copilot" else "API key"
                raise RuntimeError(f"{provider.title()} {credential} is missing; set ${api_key_env}")
        if sandbox and sandbox.get("enabled"):
            selected_env = sandbox_env_for_provider(provider, sandbox, api_key_env)
            # OpenRouter sends its API request from the host. Its sandbox is
            # only used for scoped tool commands, so the API credential must
            # not be required or passed into that container.
            if provider != "openrouter" and api_key_env and api_key_env not in selected_env:
                raise RuntimeError(
                    f"{provider.title()} credential ${api_key_env} must be listed in sandbox.pass_env"
                )
            missing_env = [
                name for name in selected_env
                if not os.environ.get(name)
            ]
            if missing_env:
                names = ", ".join(f"${name}" for name in missing_env)
                raise RuntimeError(
                    f"Sandbox credential environment variables are missing: {names}"
                )
            continue
        if provider == "copilot":
            resolved = resolve_provider_executable(
                provider, item.get("executable_path"), discover_copilot=True,
            )
            executable = resolved.path
        else:
            executable = shutil.which(provider)
        if not executable:
            install_hint = {
                "codex": "install/authenticate the Codex CLI",
                "gemini": "install/authenticate Gemini CLI",
                "claude": "install/authenticate Claude Code",
                "copilot": "install/authenticate GitHub Copilot CLI",
            }.get(provider, "install the provider CLI")
            if provider == "copilot":
                install_hint += (
                    "; if installed with 'gh copilot', set COPILOT_PATH or "
                    "developer/reviewer executable_path to the downloaded binary"
                )
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
            build_context = sandbox_build_context(sandbox, repo)
        else:
            build_context = None
        if build_context is not None:
            try:
                with build_context as build_command:
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
    return [list(SAFE_GIT_DIFF_CHECK)], "No test framework detected; using Git whitespace validation"


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
        "host": None,
        "auth": "native_cli",
    }
    if provider == "openrouter":
        settings.update({
            "model": DEFAULT_OPENROUTER_MODEL,
            "auth": "api_key",
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


def credential_environment_names(config: Mapping[str, Any]) -> list[str]:
    """Return configured provider and sandbox credential environment names."""
    names: list[str] = []
    for role in ("developer_agent", "reviewer_agent", "investigator_agent"):
        settings = config.get(role)
        if not isinstance(settings, Mapping):
            continue
        provider = str(settings.get("provider", "codex"))
        name = api_key_env_for_provider(provider, settings.get("api_key_env"))
        if name and name not in names:
            names.append(name)
    sandbox = config.get("sandbox")
    if isinstance(sandbox, Mapping):
        for name in sandbox.get("pass_env", ()):
            if isinstance(name, str) and name not in names:
                names.append(name)
    return names
