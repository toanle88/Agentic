"""Compatibility facade for OpenRouter policy and execution.

Scoped file/command tools live in agentic_openrouter_tools; the HTTP agent loop
lives in agentic_openrouter_loop. Historical private imports remain available.
"""
import subprocess  # noqa: F401 - historical facade patch point
import urllib  # noqa: F401 - historical facade patch point
from . import openrouter_loop as _loop
from . import openrouter_tools as _tools
from .config import (  # noqa: F401
    DEFAULT_OPENROUTER_BASE_URL,
    DEFAULT_OPENROUTER_COMMAND_TIMEOUT,
    OPENROUTER_MAX_TURNS,
)
from .openrouter_tools import (  # noqa: F401
    ALWAYS_BLOCKED_COMMANDS,
    GIT_UNSAFE_OPTIONS,
    INTERPRETER_EVAL_OPTIONS,
    INTERPRETER_EXECUTABLES,
    NETWORK_COMMANDS,
    OPENROUTER_COMMAND_OUTPUT_CHARS,
    OPENROUTER_LIST_FILES_MAX_PAGE,
    OPENROUTER_READ_FILE_CHARS,
    OPENROUTER_READ_SCAN_CHARS,
    OPENROUTER_READ_SCAN_LINES,
    OPENROUTER_WRITE_FILE_BYTES,
    OPENROUTER_MUTATION_BYTES,
    OPENROUTER_SEARCH_SCAN_BYTES,
    OPENROUTER_SEARCH_SCAN_BYTES_PER_FILE,
    OPENROUTER_SEARCH_SCAN_LINES,
    OPENROUTER_SEARCH_SCAN_LINES_PER_FILE,
    OPENROUTER_SEARCH_INCOMPLETE_MARKER,
    OPENROUTER_SEARCH_TRUNCATION_MARKER,
    READONLY_BRANCH_OPTIONS,
    READONLY_GIT_COMMANDS,
    READONLY_INSPECTION_COMMANDS,
    _openrouter_command_allowed,
    _openrouter_tools,
    _readonly_git_command_allowed,
    _readonly_inspection_command_allowed,
    _safe_worktree_path,
)


def _openrouter_request(*args, **kwargs):
    kwargs.setdefault("urllib_module", urllib)
    return _tools._openrouter_request(*args, **kwargs)


def _openrouter_tool_call(*args, **kwargs):
    kwargs.setdefault("subprocess_module", subprocess)
    return _tools._openrouter_tool_call(*args, **kwargs)


def run_openrouter(*args, **kwargs):
    """Run the API loop while retaining facade-level integration hooks."""
    return _loop.run_openrouter(
        *args, _request=_openrouter_request,
        _tool_call=_openrouter_tool_call,
        _max_tool_turns=OPENROUTER_MAX_TURNS,
        _command_timeout_default=DEFAULT_OPENROUTER_COMMAND_TIMEOUT,
        **kwargs,
    )
