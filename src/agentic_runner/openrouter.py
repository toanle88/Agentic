"""Compatibility facade for OpenRouter policy and execution.

Scoped file/command tools live in agentic_openrouter_tools; the HTTP agent loop
lives in agentic_openrouter_loop. Historical private imports remain available.
"""
import subprocess
import urllib
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
    OPENROUTER_READ_FILE_CHARS,
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
    old_urllib = _tools.urllib
    _tools.urllib = urllib
    try:
        return _tools._openrouter_request(*args, **kwargs)
    finally:
        _tools.urllib = old_urllib


def _openrouter_tool_call(*args, **kwargs):
    old_subprocess = _tools.subprocess
    _tools.subprocess = subprocess
    try:
        return _tools._openrouter_tool_call(*args, **kwargs)
    finally:
        _tools.subprocess = old_subprocess


def run_openrouter(*args, **kwargs):
    """Run the API loop while retaining facade-level integration hooks."""
    old_request = _loop._openrouter_request
    old_tool_call = _loop._openrouter_tool_call
    old_max_turns = _loop.OPENROUTER_MAX_TURNS
    old_command_timeout = _loop.DEFAULT_OPENROUTER_COMMAND_TIMEOUT
    _loop._openrouter_request = _openrouter_request
    _loop._openrouter_tool_call = _openrouter_tool_call
    _loop.OPENROUTER_MAX_TURNS = OPENROUTER_MAX_TURNS
    _loop.DEFAULT_OPENROUTER_COMMAND_TIMEOUT = DEFAULT_OPENROUTER_COMMAND_TIMEOUT
    try:
        return _loop.run_openrouter(*args, **kwargs)
    finally:
        _loop._openrouter_request = old_request
        _loop._openrouter_tool_call = old_tool_call
        _loop.OPENROUTER_MAX_TURNS = old_max_turns
        _loop.DEFAULT_OPENROUTER_COMMAND_TIMEOUT = old_command_timeout
