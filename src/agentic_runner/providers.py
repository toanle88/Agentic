"""Provider command construction.

This module owns provider-specific CLI argv policy. Execution, logging,
sandbox wrapping, output parsing, and usage accounting remain in ``agentic``
so the compatibility-facing runner can preserve its existing behavior while
provider command construction is tested independently.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath

from .policy import PERMISSION_FIELDS, default_permission_policy


_NETWORK_TOOL_NAMES = (
    "curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "netcat", "telnet",
)
_READONLY_GIT_SUBCOMMANDS = ("status", "diff", "log", "show")
_MUTATING_GIT_SUBCOMMANDS = ("commit", "push", "reset", "clean", "checkout", "switch", "worktree")
_NATIVE_PATTERN_METACHARS = frozenset("()[]{}*?$`\\\r\n")


def _validate_native_pattern_text(value: str, *, field: str) -> None:
    """Reject values that could alter native provider permission patterns."""
    if any(character in value for character in _NATIVE_PATTERN_METACHARS):
        raise ValueError(f"{field} contains unsupported native permission-pattern characters")


def _readonly_git_unsafe_patterns(prefix: str) -> list[str]:
    """Deny Git options that can write or execute outside the review boundary."""
    patterns: list[str] = []
    for command in _READONLY_GIT_SUBCOMMANDS:
        patterns += [
            f"{prefix}(git {command}:*--output*)",
            f"{prefix}(git {command}:* -o*)",
            f"{prefix}(git {command}:*--ext-diff*)",
            f"{prefix}(git {command}:*--textconv*)",
            f"{prefix}(git {command}:*>*)",
            f"{prefix}(git {command}:*>>*)",
        ]
    # Native provider tool patterns cannot safely inspect arbitrary log argv.
    # Keep bare ``git log`` metadata inspection available and reject every
    # argument-bearing form, including patch and --follow variants.
    patterns.append(f"{prefix}(git log:*)")
    patterns.append(f"{prefix}(git diff:*--no-index*)")
    return patterns


def _provider_worktree_scope(worktree: Path | None, *, sandbox_enabled: bool) -> PurePath:
    """Return the path visible to a native provider for worktree rules."""
    # This is a path inside the provider's container, not a host path.  Do
    # not resolve it through the host filesystem (which turns it into a
    # drive-qualified Windows path such as ``D:\\workspace``).
    if sandbox_enabled:
        return PurePosixPath("/workspace")
    # Native provider rules use slash-separated path patterns even when the
    # host is Windows. Preserve a rooted POSIX-looking test path such as
    # ``/tmp/worktree`` instead of resolving it to the current drive.
    rendered = str(worktree or Path.cwd()).replace("\\", "/")
    _validate_native_pattern_text(rendered, field="worktree")
    return PurePosixPath(rendered)


def _network_shell_patterns(prefix: str) -> list[str]:
    """Return explicit patterns for the supported network-capable tools."""
    return [f"{prefix}({command}:*)" for command in _NETWORK_TOOL_NAMES]


def _readonly_git_patterns(prefix: str) -> list[str]:
    """Return safe patterns for read-only Git commands.

    ``git diff`` and ``git show`` can execute repository-configured external
    diff and text-conversion helpers. Native provider tool patterns cannot
    inspect or rewrite command arguments, so those commands are only allowed
    when the disabling flags are part of the command itself. The same
    limitation means native ``git log`` is restricted to its bare,
    metadata-only form; OpenRouter performs argv-level validation for richer
    metadata-only log options.
    """
    patterns: list[str] = []
    for command in _READONLY_GIT_SUBCOMMANDS:
        if command in {"diff", "show"}:
            safe_command = f"git {command} --no-ext-diff --no-textconv"
            patterns += [f"{prefix}({safe_command})", f"{prefix}({safe_command}:*)"]
        elif command == "log":
            patterns.append(f"{prefix}(git log)")
        else:
            patterns += [f"{prefix}(git {command})", f"{prefix}(git {command}:*)"]
    return patterns


def _provider_permission_policy(
    permission_policy: dict[str, bool] | None, *, readonly: bool,
) -> dict[str, bool]:
    """Merge a caller policy while failing closed for unsupported overrides."""
    policy = default_permission_policy("reviewer" if readonly else "developer")
    if permission_policy is not None:
        if not isinstance(permission_policy, dict):
            raise ValueError("permission_policy must be an object")
        unknown = sorted(set(permission_policy) - set(PERMISSION_FIELDS))
        if unknown:
            raise ValueError(
                "permission_policy contains unsupported field(s): "
                + ", ".join(unknown)
            )
        for name, value in permission_policy.items():
            if type(value) is not bool:
                raise ValueError(f"permission_policy.{name} must be a boolean")
            policy[name] = value
    if readonly and any(
        policy[name] for name in ("edit_files", "create_files", "run_checks", "allow_dangerous")
    ):
        raise ValueError("read-only provider calls cannot enable writes, checks, or dangerous permissions")
    return policy


def validate_provider_permission_policy(
    provider: str, policy: dict[str, bool],
) -> None:
    """Reject capability combinations a native provider cannot represent."""
    if provider == "codex":
        unsupported: list[str] = []
        if not policy.get("read_files"):
            unsupported.append("read_files=false")
        if not policy.get("search_files"):
            unsupported.append("search_files=false")
        if not policy.get("git_read"):
            unsupported.append("git_read=false")
        if policy.get("network"):
            unsupported.append("network=true")
        if policy.get("allow_dangerous"):
            unsupported.append("allow_dangerous=true")
        if policy.get("edit_files") != policy.get("create_files"):
            unsupported.append("edit_files and create_files must match")
        if policy.get("run_checks") != policy.get("edit_files"):
            unsupported.append("run_checks must match the write sandbox capability")
        if unsupported:
            raise ValueError(
                "Codex cannot represent permission policy capability(s): "
                + ", ".join(unsupported)
            )
    if provider == "claude" and policy.get("create_files") and not policy.get("edit_files"):
        raise ValueError(
            "Claude cannot represent create_files without edit_files"
        )
    if provider == "gemini" and policy.get("network"):
        raise ValueError(
            "Gemini cannot represent network=true without unrestricted shell access"
        )
    if provider == "copilot" and policy.get("network") and not policy.get("allow_dangerous"):
        raise ValueError(
            "Copilot cannot represent network=true without unrestricted shell access"
        )
    if provider == "copilot" and policy.get("edit_files") != policy.get("create_files"):
        raise ValueError(
            "Copilot cannot represent independent edit_files and create_files capabilities"
        )


def _native_tool_policy_args(
    provider: str, policy: dict[str, bool], *, worktree: Path | None = None,
    readonly: bool = False,
    sandbox_enabled: bool = False, sandbox_allow_network: bool = False,
    check_commands: list[list[str]] | None = None,
) -> list[str]:
    """Translate common capabilities into native provider tool rules."""
    check_patterns = _check_command_patterns(check_commands)
    if provider == "claude":
        scope = _provider_worktree_scope(worktree, sandbox_enabled=sandbox_enabled)
        available: list[str] = []
        if policy.get("read_files"):
            available.append("Read")
        if policy.get("search_files"):
            available += ["Glob", "Grep"]
        if policy.get("edit_files"):
            available.append("Edit")
        if policy.get("create_files"):
            available.append("Write")
        if not readonly and (policy.get("run_checks") or policy.get("git_read")):
            available.append("Bash")
        if policy.get("network"):
            if not readonly and "Bash" not in available:
                available.append("Bash")
            available += ["WebFetch", "WebSearch"]
        # Claude expects one comma-separated value for --tools. Passing each
        # tool as a separate argv value makes the following tool names look
        # like unrelated CLI arguments.
        args = ["--tools", ",".join(available)]
        allowed: list[str] = []
        if policy.get("read_files"):
            allowed.append(f"Read(/{scope}/**)")
        if policy.get("search_files"):
            allowed += [f"Glob(/{scope}/**)", f"Grep(/{scope}/**)"]
        if policy.get("edit_files"):
            allowed.append(f"Edit(/{scope}/**)")
        if policy.get("create_files"):
            allowed.append(f"Write(/{scope}/**)")
        if policy.get("git_read") and not readonly:
            allowed += _readonly_git_patterns("Bash")
        if policy.get("run_checks"):
            allowed += [f"Bash({command})" for command in check_patterns]
        if policy.get("network"):
            allowed += _network_shell_patterns("Bash") + ["WebFetch", "WebSearch"]
        if allowed:
            # Claude Code spells these options in camelCase and accepts the
            # individual tool patterns as variadic values.
            args += ["--allowedTools", *allowed]
        denied: list[str] = []
        if not policy.get("git_read"):
            denied.append("Bash(git:*)")
        else:
            denied += [f"Bash(git {command})" for command in _MUTATING_GIT_SUBCOMMANDS]
            denied += _readonly_git_unsafe_patterns("Bash")
        if not policy.get("network"):
            denied += [f"Bash({command}:*)" for command in _NETWORK_TOOL_NAMES]
            denied += ["WebFetch", "WebSearch"]
        if denied:
            args += ["--disallowedTools", *denied]
        return args
    if provider == "gemini":
        # Gemini's allowlist is an approval policy, while approval-mode controls
        # whether edits or all tools are accepted automatically. Keep the
        # allowlist explicit so safe operations do not repeatedly prompt. The
        # CLI supports command-scoped ShellTool entries for the read-only Git
        # commands. Gemini exposes its network-capable shell as an unrestricted
        # tool, so network=true is rejected by validation below rather than
        # translated into a wildcard permission.
        allowed: list[str] = []
        if policy.get("read_files"):
            allowed += ["read_file", "list_directory"]
        if policy.get("search_files"):
            allowed.append("grep_search")
        if policy.get("edit_files"):
            allowed.append("replace")
        if policy.get("create_files"):
            allowed.append("write_file")
        if policy.get("git_read") and not readonly:
            for command in _READONLY_GIT_SUBCOMMANDS:
                if command in {"diff", "show"}:
                    allowed.append(
                        f"ShellTool(git {command} --no-ext-diff --no-textconv)"
                    )
                elif command == "log":
                    allowed.append("ShellTool(git log)")
                else:
                    allowed.append(f"ShellTool(git {command})")
        # Configured checks are still run by Agentic after the provider turn,
        # so ordinary run_checks must not grant the model unrestricted shell.
        return ["--allowed-tools", ",".join(allowed)] if allowed else []
    return []


def _check_command_patterns(check_commands: list[list[str]] | None) -> list[str]:
    """Render configured argv checks as exact native shell-tool patterns."""
    if check_commands is None:
        return []
    patterns: list[str] = []
    for command in check_commands:
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ValueError("check_commands must contain non-empty argv arrays")
        for part in command:
            _validate_native_pattern_text(part, field="check_commands")
        patterns.append(shlex.join(command))
    return patterns


def codex_command(
    prompt: str,
    *,
    readonly: bool = False,
    worktree_is_projection: bool = False,
    schema: Path | None = None,
    output: Path | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    permission_policy: dict[str, bool] | None = None,
) -> list[str]:
    """Build a non-interactive Codex command without shell interpolation."""
    policy = _provider_permission_policy(permission_policy, readonly=readonly)
    validate_provider_permission_policy("codex", policy)
    unsupported: list[str] = []
    if readonly and policy.get("edit_files"):
        unsupported.append("read-only calls cannot enable edit_files")
    if readonly and policy.get("create_files"):
        unsupported.append("read-only calls cannot enable create_files")
    if readonly and policy.get("run_checks"):
        unsupported.append("read-only calls cannot enable run_checks")
    if unsupported:
        raise ValueError(
            "Codex cannot represent permission policy capability(s): "
            + ", ".join(unsupported)
        )
    sandbox_mode = "read-only" if readonly or not policy.get("edit_files", False) else "workspace-write"
    args = ["codex", "exec", "--json", "--sandbox", sandbox_mode]
    if readonly or worktree_is_projection:
        # Native provider turns run from disposable projections without Git
        # metadata, so Codex must not reject the temporary cwd before it can
        # inspect or edit the staged files.
        args.append("--skip-git-repo-check")
    if model:
        args += ["--model", model]
    if reasoning_effort:
        args += ["--config", f"model_reasoning_effort={json.dumps(reasoning_effort)}"]
    if schema:
        args += ["--output-schema", str(schema)]
    if output:
        args += ["--output-last-message", str(output)]
    args.append(prompt)
    return args


@dataclass(frozen=True)
class ResolvedProviderExecutable:
    """A provider executable selected without changing the host environment."""

    path: str | None
    source: str | None = None


def _usable_executable(value: str) -> str | None:
    """Return an executable command or path only when it can be started."""
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or len(candidate.parts) > 1:
        if not candidate.is_file():
            return None
        # Windows launchability is determined by the executable suffix and
        # file association, not by POSIX execute permission bits. The
        # downloaded Copilot binary is intentionally extensionless.
        if os.name == "nt":
            return str(candidate)
        return str(candidate) if os.access(candidate, os.X_OK) else None
    found = shutil.which(value)
    if found:
        return found
    return None


def copilot_candidate_paths(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Return known host paths used by the GitHub CLI Copilot installer."""
    environment = os.environ if environ is None else environ
    data_home_value = environment.get("XDG_DATA_HOME")
    if data_home_value:
        data_home = Path(data_home_value).expanduser()
    else:
        try:
            data_home = Path.home() / ".local" / "share"
        except RuntimeError:
            # A deliberately minimal child environment may not contain any
            # home-directory variables.  Provider discovery is optional and
            # must not make an otherwise valid invocation fail.
            return []
    install_dir = data_home / "gh" / "copilot"
    # GitHub CLI has used both names across platforms/releases. Check both so
    # discovery remains stable when a Windows runner supplies a POSIX-style
    # XDG_DATA_HOME or when a copied fixture has the other suffix.
    return [install_dir / "copilot", install_dir / "copilot.exe"]


def resolve_provider_executable(
    provider: str,
    configured_path: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    discover_copilot: bool = True,
) -> ResolvedProviderExecutable:
    """Resolve a provider command while preserving argv-only execution.

    Copilot's downloaded binary is considered only after an explicit setting
    and the normal PATH lookup. ``discover_copilot=False`` is used for image
    execution, where host paths are not visible inside the container.
    """
    if provider != "copilot":
        path = _usable_executable(provider)
        return ResolvedProviderExecutable(path, "PATH" if path else None)

    environment = os.environ if environ is None else environ
    if configured_path:
        path = _usable_executable(configured_path)
        return ResolvedProviderExecutable(path, "configuration" if path else None)
    if not discover_copilot:
        return ResolvedProviderExecutable(None)
    env_path = environment.get("COPILOT_PATH")
    if env_path:
        path = _usable_executable(env_path)
        return ResolvedProviderExecutable(path, "COPILOT_PATH" if path else None)
    path = _usable_executable("copilot")
    if path:
        return ResolvedProviderExecutable(path, "PATH")
    if discover_copilot:
        for candidate in copilot_candidate_paths(environment):
            path = _usable_executable(str(candidate))
            if path:
                return ResolvedProviderExecutable(path, "GitHub CLI download")
    return ResolvedProviderExecutable(None)


def display_provider_path(path: str) -> str:
    """Render a host path compactly for actionable terminal diagnostics."""
    candidate = Path(path)
    try:
        return "~" + str(candidate.expanduser().relative_to(Path.home()))
    except (ValueError, RuntimeError):
        return str(candidate)


def cli_provider_command(
    provider: str,
    prompt: str,
    *,
    readonly: bool = False,
    model: str | None = None,
    executable_path: str | None = None,
    permission_policy: dict[str, bool] | None = None,
    worktree: Path | None = None,
    sandbox_enabled: bool = False,
    sandbox_allow_network: bool = False,
    check_commands: list[list[str]] | None = None,
) -> tuple[list[str], bool]:
    """Build a native CLI provider command and report whether it is JSON-streamed."""
    policy = _provider_permission_policy(permission_policy, readonly=readonly)
    validate_provider_permission_policy(provider, policy)
    dangerous = bool(policy.get("allow_dangerous")) and not readonly
    can_write = bool(policy.get("edit_files") or policy.get("create_files")) and not readonly
    if provider == "gemini":
        args = [
            "gemini", "-p", prompt, "--output-format", "stream-json",
            "--approval-mode", "plan" if not can_write else ("yolo" if dangerous else "auto_edit"),
        ]
        if not dangerous:
            args += _native_tool_policy_args(
                provider, policy, worktree=worktree, readonly=readonly,
                sandbox_enabled=sandbox_enabled,
                sandbox_allow_network=sandbox_allow_network, check_commands=check_commands,
            )
        if model:
            args += ["--model", model]
        return args, True
    if provider == "claude":
        args = ["claude", "-p", prompt, "--output-format", "stream-json"]
        if dangerous:
            args += ["--dangerously-skip-permissions"]
        else:
            # ``acceptEdits`` also auto-approves filesystem commands such as
            # mkdir, touch, mv, and cp.  ``dontAsk`` permits only the explicit
            # tool patterns above and denies everything else without prompts.
            args += ["--permission-mode", "dontAsk"]
        if not dangerous:
            args += _native_tool_policy_args(
                provider, policy, worktree=worktree, readonly=readonly,
                sandbox_enabled=sandbox_enabled,
                check_commands=check_commands,
            )
        if model:
            args += ["--model", model]
        return args, True
    if provider == "copilot":
        # Copilot's shell permission rules cannot close over saved approvals:
        # exact allow rules still leave unmatched commands prompt-capable, while
        # a catch-all deny also denies the exact commands. Keep the native
        # developer boundary shell-free; Agentic owns configured checks and Git
        # inspection through its argv-only runner.
        args = [executable_path or "copilot", "-p", prompt, "-s", "--no-ask-user"]
        # Copilot's default path policy includes the current directory and the
        # system temp directory. Agentic runs with cwd=worktree, so remove the
        # extra temp-dir capability and scope writes to that worktree below.
        args += ["--disallow-temp-dir"]
        if model:
            args += ["--model", model]
        if readonly:
            # Copilot has no universal read-only mode. Reviewers receive only
            # file-inspection tools; Agentic supplies the staged patch from its
            # own argv-only Git runner, so the provider needs no shell. Keep
            # native review turns closed: unmatched commands must not prompt
            # or inherit an existing approval.
            available_tools = []
            if policy.get("read_files"):
                available_tools.append("view")
            if policy.get("search_files"):
                available_tools += ["grep", "glob"]
            args += ["--available-tools", ",".join(available_tools)]
        else:
            available_tools: list[str] = []
            if policy.get("read_files"):
                available_tools.append("view")
            if policy.get("search_files"):
                available_tools += ["grep", "glob"]
            if policy.get("edit_files"):
                available_tools.append("edit")
            if policy.get("create_files"):
                available_tools.append("create")
            if dangerous:
                # The explicit isolated-mode escape hatch intentionally keeps
                # Copilot's unrestricted tool behavior for trusted callers.
                available_tools.append("bash")
            args += ["--available-tools", ",".join(available_tools)]
            if dangerous:
                args += ["--allow-all-tools", "--allow-all-paths"]
            else:
                allow_tools: list[str] = []
                if policy.get("edit_files") or policy.get("create_files"):
                    # Copilot's bare write permission is scoped to the
                    # process cwd, which is the assigned worktree (or
                    # /workspace in the container). Path-shaped write rules
                    # are file-specific and do not recurse into nested files.
                    allow_tools.append("write")
                if allow_tools:
                    for value in allow_tools:
                        args += ["--allow-tool", value]
        return args, False
    raise ValueError(f"Unsupported CLI provider: {provider}")
