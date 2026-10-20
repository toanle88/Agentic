"""Process-level restrictions for human-directed chat edit turns."""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_BLOCKED_COMMANDS = (
    "gh", "curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "netcat", "telnet",
)


def _write_wrapper(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o700)


@contextmanager
def restricted_process_environment(enabled: bool, *, sandbox_enabled: bool = False) -> Iterator[dict[str, str] | None]:
    """Yield an environment that blocks chat edit side effects outside a sandbox.

    A configured container provides the stronger filesystem and network boundary;
    native/trusted execution still gets fixed command wrappers so common commit,
    push, PR, and network escape routes are rejected at process execution time.
    """
    if not enabled or sandbox_enabled:
        yield None
        return

    actual_git = shutil.which("git")
    if not actual_git:
        raise RuntimeError("Chat edit turns require git on PATH for execution restrictions")
    with tempfile.TemporaryDirectory(prefix="agentic-chat-guard-") as directory:
        root = Path(directory)
        _write_wrapper(
            root / "git",
            "for arg in \"$@\"; do\n"
            "  case \"$arg\" in\n"
            "    commit|push|tag|merge|rebase|reset|clean|checkout|switch|worktree|update-ref|config|--config*|-c)\n"
            "      echo 'Agentic chat restriction: mutating Git operations are disabled' >&2\n"
            "      exit 126\n"
            "      ;;\n"
            "  esac\n"
            "done\n"
            f"exec {actual_git!r} \"$@\"\n",
        )
        for command in _BLOCKED_COMMANDS:
            _write_wrapper(
                root / command,
                "echo 'Agentic chat restriction: network/publication commands are disabled' >&2\n"
                "exit 126\n",
            )
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join([str(root), environment.get("PATH", "")])
        yield environment
