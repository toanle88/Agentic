"""Shared read-only Git option classification."""
from __future__ import annotations

import os
from collections.abc import Mapping


GIT_SAFETY_CONFIG = (
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",
    # Native Windows checkouts commonly use CRLF while the index stores LF.
    # The global config is intentionally disabled below, so provide the
    # platform's line-ending policy explicitly at the safe command boundary.
    "-c", f"core.autocrlf={'true' if os.name == 'nt' else 'false'}",
    "-c", "core.whitespace=cr-at-eol",
    "-c", "credential.helper=",
    "-c", "core.sshCommand=ssh",
    "-c", "core.gitProxy=none",
    "-c", "protocol.ext.allow=never",
    "-c", "remote.origin.uploadpack=git-upload-pack",
    "-c", "remote.origin.receivepack=git-receive-pack",
    "-c", "diff.external=",
)
GIT_SAFETY_ENVIRONMENT = (
    ("GIT_CONFIG_NOSYSTEM", "1"),
    ("GIT_CONFIG_GLOBAL", os.devnull),
    ("GIT_ATTR_NOSYSTEM", "1"),
    ("GIT_OPTIONAL_LOCKS", "0"),
    ("GIT_TERMINAL_PROMPT", "0"),
)
GIT_FILTER_CONFIG_PATTERN = r"^filter\..+\.(clean|smudge|process|required)$"
_BLOCKED_GIT_ENVIRONMENT_NAMES = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_EXTERNAL_DIFF", "GIT_SSH_COMMAND", "GIT_PROXY_COMMAND",
    *(name for name, _ in GIT_SAFETY_ENVIRONMENT),
})
_BLOCKED_GIT_ENVIRONMENT_NAMES_CASEFOLDED = frozenset(
    name.casefold() for name in _BLOCKED_GIT_ENVIRONMENT_NAMES
)


def is_blocked_git_environment_name(name: str) -> bool:
    """Return whether an environment name can override hardened Git behavior."""
    normalized = name.casefold()
    return (
        normalized.startswith("git_config_")
        or normalized in _BLOCKED_GIT_ENVIRONMENT_NAMES_CASEFOLDED
    )


def _without_git_config_options(argv: list[str]) -> list[str]:
    """Remove caller-supplied Git configuration options.

    Git accepts configuration overrides as global options, and a later
    ``-c`` wins over an earlier one.  The hardened settings are therefore
    added only after every caller-supplied configuration option has been
    removed.  This also covers compatibility callers that invoke the shared
    wrapper without first applying OpenRouter's command allowlist.
    """
    filtered = [argv[0]]
    index = 1
    while index < len(argv):
        part = argv[index]
        exact_with_value = part in {"-c", "--config", "--config-env"}
        attached_config = part.startswith("-c") and not part.startswith("--") and len(part) > 2
        attached_long_config = part.startswith("--config") and len(part) > len("--config")
        if exact_with_value or attached_config or attached_long_config:
            if exact_with_value:
                if index + 1 >= len(argv):
                    raise ValueError(f"Git configuration option {part!r} needs a value")
                index += 2
            else:
                index += 1
            continue
        filtered.append(part)
        index += 1
    return filtered


def hardened_git_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Remove Git environment overrides and disable untrusted config sources."""
    environment = dict(os.environ if environ is None else environ)
    for name in list(environment):
        if is_blocked_git_environment_name(name):
            environment.pop(name, None)
    environment.update(dict(GIT_SAFETY_ENVIRONMENT))
    return environment


def hardened_git_argv(argv: list[str]) -> list[str]:
    """Prefix a Git argv with settings that caller arguments cannot override."""
    if not argv:
        return []
    return [argv[0], *GIT_SAFETY_CONFIG, *_without_git_config_options(argv)[1:]]


def git_filter_probe_argv(executable: str = "git") -> list[str]:
    """Return a hardened argv for detecting repository-configured filters."""
    return hardened_git_argv([
        executable, "config", "--local", "--name-only", "--get-regexp",
        GIT_FILTER_CONFIG_PATTERN,
    ])

GIT_LOG_CONTENT_OPTIONS = frozenset({
    "-p", "-u", "--patch", "--full-diff", "-c", "--cc",
    "--patch-with-stat", "--patch-with-raw", "-L", "--follow",
    "--word-diff", "--word-diff-regex", "--color-words",
    "--stat", "--numstat", "--shortstat", "--dirstat", "--summary",
    "--raw", "--patch-with-raw", "--name-only", "--name-status",
    "--full-index", "--binary", "--check", "--diff-filter",
    "--diff-merges", "--no-diff-merges", "--find-renames", "--find-copies",
    "--irreversible-delete", "--ext-diff", "--textconv", "--output",
})

# Git adds diff presentation options over time. OpenRouter therefore uses a
# positive metadata-only allowlist rather than relying on this content-option
# inventory to remain complete. Log output itself is separately restricted to
# the fixed format below; otherwise even metadata-looking directives can emit
# full messages or notes.
GIT_LOG_METADATA_FORMAT = "%H%x09%an%x09%aI%x09%s"
GIT_LOG_METADATA_FORMAT_OPTIONS = frozenset({
    f"--format={GIT_LOG_METADATA_FORMAT}",
    f"--pretty=format:{GIT_LOG_METADATA_FORMAT}",
})
GIT_LOG_METADATA_OPTIONS = frozenset({
    "--abbrev-commit", "--all", "--author-date-order", "--boundary",
    "--date-order", "--decorate", "--dense", "--first-parent",
    "--full-history", "--graph", "--left-right", "--no-abbrev-commit",
    "--no-decorate", "--no-merges", "--no-walk", "--oneline",
    "--parents", "--reverse", "--show-pulls", "--sparse",
    "--simplify-by-decoration", "--simplify-merges", "--source",
    "--topo-order", "--use-mailmap", "--cherry", "--cherry-mark",
    "--cherry-pick", "--all-match", "--invert-grep",
    "--regexp-ignore-case", "--extended-regexp", "--fixed-strings",
    "--ignore-case", "--ignore-missing", "--reflog", "--single-worktree",
})
GIT_LOG_METADATA_PREFIXES = (
    "--author=", "--committer=", "--date=", "--decorate=", "--grep=",
    "--max-count=", "--max-parents=", "--min-parents=",
    "--skip=", "--since=", "--after=", "--until=", "--before=",
)


def git_log_option_exposes_history(part: str) -> bool:
    """Return whether a ``git log`` option may expose historical file data.

    Unknown options are unsafe by default.  Git's log mode shares a large
    diff-option surface, and rejecting new or unclassified options avoids
    turning a future diff-format option into a protected-history bypass.
    """
    if not part.startswith("-") or part == "--":
        return False
    if part.startswith(("--format=", "--pretty=")):
        return part not in GIT_LOG_METADATA_FORMAT_OPTIONS
    option = part.split("=", 1)[0]
    if option in GIT_LOG_METADATA_OPTIONS or any(
        part.startswith(prefix) for prefix in GIT_LOG_METADATA_PREFIXES
    ):
        return False
    # Git accepts combined short options such as ``-pu`` and attached ``-L``
    # values.  The positive allowlist above means every other option is
    # rejected, including future diff-format forms.
    return True
