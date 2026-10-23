"""Compatibility facade for provider execution helpers.

Streaming/process concerns live in agentic_execution_stream; provider message
and command adapters live in agentic_execution_providers. Historical imports
remain available from this module.
"""
import threading

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


# Provider adapters temporarily receive compatibility callbacks through their
# module globals. Hold this process-local lock for the full facade call so
# concurrent callers cannot observe a partially patched adapter. It is
# deliberately non-reentrant; adapters must not call this facade recursively.
_COMPATIBILITY_LOCK = threading.Lock()


def run_codex(*args, **kwargs):
    """Call the adapter while honoring historical facade monkeypatches."""
    with _COMPATIBILITY_LOCK:
        old_streamed = _providers.streamed_command
        old_codex = _providers.codex_command
        _providers.streamed_command = streamed_command
        _providers.codex_command = codex_command
        try:
            return _providers.run_codex(*args, **kwargs)
        finally:
            _providers.streamed_command = old_streamed
            _providers.codex_command = old_codex


def run_cli_provider(*args, **kwargs):
    """Call a CLI adapter while honoring historical facade monkeypatches."""
    with _COMPATIBILITY_LOCK:
        old_streamed = _providers.streamed_command
        old_command = _providers.cli_provider_command
        _providers.streamed_command = streamed_command

        def compatible_command(*command_args, **command_kwargs):
            while True:
                try:
                    return cli_provider_command(*command_args, **command_kwargs)
                except TypeError as exc:
                    unsupported = next(
                        (
                            name for name in (
                                "executable_path", "worktree", "sandbox_enabled",
                                "sandbox_allow_network", "check_commands",
                                "permission_policy",
                            )
                            if name in command_kwargs and name in str(exc)
                        ),
                        None,
                    )
                    if unsupported is None:
                        raise
                    command_kwargs.pop(unsupported)
        _providers.cli_provider_command = compatible_command
        try:
            return _providers.run_cli_provider(*args, **kwargs)
        finally:
            _providers.streamed_command = old_streamed
            _providers.cli_provider_command = old_command
