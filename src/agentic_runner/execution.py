"""Compatibility facade for provider execution helpers.

Streaming/process concerns live in agentic_execution_stream; provider message
and command adapters live in agentic_execution_providers. Historical imports
remain available from this module.
"""
import re

from . import execution_providers as _providers
from .execution_providers import (  # noqa: F401
    _extract_provider_message,
    _message_text,
    _provider_progress,
)
from .providers import cli_provider_command, codex_command
from .execution_stream import (  # noqa: F401
    MAX_HUMAN_LOG_BYTES,
    MAX_RAW_LOG_BYTES,
    _absolute_path,
    _codex_progress,
    _console_print,
    _doctor_public_detail,
    _paths_equal,
    _paint,
    _record_provider_usage,
    _remaining_timeout,
    _short,
    _unicode_or_ascii,
    print_review_summary,
    print_run_header,
    print_run_summary,
    streamed_command,
)


_UNEXPECTED_KEYWORD_RE = re.compile(
    r"^(?:[\w.<>]+(?:\(\))? )?got an unexpected keyword argument ['\"]([^'\"]+)['\"]$"
)


def run_codex(*args, **kwargs):
    """Call the adapter while honoring historical facade monkeypatches."""
    return _providers.run_codex(
        *args, _streamed_command=streamed_command,
        _codex_command=codex_command, **kwargs,
    )


def run_cli_provider(*args, **kwargs):
    """Call a CLI adapter while honoring historical facade monkeypatches."""
    def compatible_command(*command_args, **command_kwargs):
        while True:
            try:
                return cli_provider_command(*command_args, **command_kwargs)
            except TypeError as exc:
                match = _UNEXPECTED_KEYWORD_RE.fullmatch(str(exc))
                unsupported = next(
                    (
                        name for name in (
                            "executable_path", "worktree", "sandbox_enabled",
                            "sandbox_allow_network", "check_commands",
                            "permission_policy", "security_mode",
                        )
                        if (
                            name in command_kwargs
                            and match is not None
                            and match.group(1) == name
                        )
                    ),
                    None,
                )
                if unsupported is None:
                    raise
                command_kwargs.pop(unsupported)
    return _providers.run_cli_provider(
        *args, _streamed_command=streamed_command,
        _cli_provider_command=compatible_command, **kwargs,
    )
