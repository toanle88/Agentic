#!/usr/bin/env python3
"""Create and push a version tag for an Agentic release.

The GitHub Actions Release workflow builds the package and creates the GitHub
Release, including manual runs from ``main``. This script performs the local
safety checks and pushes the tag as a local fallback.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
RUNTIME_MODULES = (
    "agentic.py",
    "agentic_shell.py",
    "agentic_dashboard.py",
    "src/agentic_runner/__init__.py",
    "src/agentic_runner/cli.py",
    "src/agentic_runner/config.py",
    "src/agentic_runner/context.py",
    "src/agentic_runner/diagnostics.py",
    "src/agentic_runner/dashboard.py",
    "src/agentic_runner/dashboard_data.py",
    "src/agentic_runner/execution.py",
    "src/agentic_runner/execution_providers.py",
    "src/agentic_runner/execution_stream.py",
    "src/agentic_runner/guidance.py",
    "src/agentic_runner/openrouter.py",
    "src/agentic_runner/openrouter_loop.py",
    "src/agentic_runner/openrouter_tools.py",
    "src/agentic_runner/output.py",
    "src/agentic_runner/policy.py",
    "src/agentic_runner/prompts.py",
    "src/agentic_runner/shell.py",
    "src/agentic_runner/shell_commands.py",
    "src/agentic_runner/shell_input.py",
    "src/agentic_runner/shell_jobs.py",
    "src/agentic_runner/models.py",
    "src/agentic_runner/persistence.py",
    "src/agentic_runner/security.py",
    "src/agentic_runner/telemetry.py",
    "src/agentic_runner/sandbox.py",
    "src/agentic_runner/providers.py",
    "src/agentic_runner/review.py",
    "src/agentic_runner/test_discovery.py",
    "src/agentic_runner/workflow.py",
    "src/agentic_runner/workflow_state.py",
    "src/agentic_runner/worktrees.py",
)


class ReleaseError(RuntimeError):
    """Raised when the release preconditions are not satisfied."""


def _display_command(argv: Sequence[str]) -> str:
    return " ".join(_quote_argument(argument) for argument in argv)


def _quote_argument(argument: str) -> str:
    if argument and all(character.isalnum() or character in "-._/:=" for character in argument):
        return argument
    return repr(argument)


def _run(
    argv: Sequence[str],
    *,
    root: Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    if not capture_output:
        print(f"[release] {_display_command(argv)}")
    return subprocess.run(
        list(argv),
        cwd=root,
        check=False,
        text=True,
        capture_output=capture_output,
    )


def _run_checked(
    argv: Sequence[str],
    *,
    root: Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = _run(argv, root=root, capture_output=capture_output)
    if result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        detail = f"\n{output.strip()}" if output.strip() else ""
        raise ReleaseError(f"Command failed with exit {result.returncode}: {_display_command(argv)}{detail}")
    return result


def _git(*args: str, root: Path, check: bool = True) -> str:
    result = _run(("git", *args), root=root, capture_output=True)
    output = (result.stdout or "").strip()
    if check and result.returncode != 0:
        detail = (result.stderr or output).strip()
        raise ReleaseError(f"Git command failed: git {' '.join(args)}\n{detail}")
    return output


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout or "", result.stderr or "") if part).strip()


def package_version(root: Path) -> str:
    """Read the literal package version without executing project code."""
    source = root / "src" / "agentic_runner" / "__init__.py"
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError) as exc:
        raise ReleaseError(f"Unable to read package version from {source}: {exc}") from exc

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            continue
        try:
            version = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise ReleaseError(f"{source} has a non-literal __version__") from exc
        if isinstance(version, str):
            return version
        break
    raise ReleaseError(f"Could not find a string __version__ in {source}")


def release_tag(version: str) -> str:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ReleaseError(
            f"Version {version!r} is not supported by the release workflow; use MAJOR.MINOR.PATCH, such as 0.2.0"
        )
    return f"v{version}"


def _ensure_release_preconditions(root: Path, remote: str, branch: str) -> tuple[str, str]:
    _git("rev-parse", "--show-toplevel", root=root)
    current_branch = _git("branch", "--show-current", root=root)
    if current_branch != branch:
        raise ReleaseError(f"Release must run from {branch!r}; current branch is {current_branch or '(detached HEAD)'!r}")

    if _git("remote", "get-url", remote, root=root, check=False) == "":
        raise ReleaseError(f"Git remote {remote!r} does not exist")

    _run_checked(("git", "fetch", "--quiet", remote, branch), root=root)
    status = _git("status", "--porcelain=v1", "--untracked-files=all", root=root)
    if status:
        raise ReleaseError("Working tree is not clean. Commit or remove these changes before releasing:\n" + status)

    local_head = _git("rev-parse", "HEAD", root=root)
    remote_head = _git("rev-parse", f"refs/remotes/{remote}/{branch}", root=root)
    if local_head != remote_head:
        raise ReleaseError(
            f"Local {branch} is not synchronized with {remote}/{branch}; pull or push the approved changes first"
        )

    _run_checked(("git", "diff", "--check"), root=root)
    return current_branch, local_head


def _run_local_checks(root: Path) -> None:
    print("[release] Compiling runtime modules...")
    compile_result = _run(
        (sys.executable, "-m", "py_compile", *RUNTIME_MODULES),
        root=root,
        capture_output=True,
    )
    if compile_result.returncode != 0:
        output = _combined_output(compile_result)
        detail = f"\n{output[-8000:]}" if output else ""
        raise ReleaseError(f"Python compilation failed with exit {compile_result.returncode}{detail}")
    print("[release] Compile checks passed")


def _ensure_tag_is_unused(root: Path, remote: str, tag: str) -> None:
    if _git("tag", "--list", tag, root=root):
        raise ReleaseError(f"Tag {tag} already exists locally")
    if _git("ls-remote", "--tags", remote, f"refs/tags/{tag}", root=root):
        raise ReleaseError(f"Tag {tag} already exists on {remote}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify main, create the current version tag, and push it to start the GitHub release workflow."
    )
    parser.add_argument("--remote", default="origin", help="Git remote to fetch and push (default: origin)")
    parser.add_argument("--branch", default="main", help="Release branch (default: main)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all checks and show the release plan without creating or pushing a tag",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (useful for an explicitly reviewed release command)",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, root: Path = ROOT) -> int:
    args = build_parser().parse_args(argv)
    try:
        version = package_version(root)
        tag = release_tag(version)
        _, head = _ensure_release_preconditions(root, args.remote, args.branch)
        _run_local_checks(root)
        _ensure_tag_is_unused(root, args.remote, tag)
    except ReleaseError as exc:
        print(f"[release] ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"[release] Ready to release {tag} from {args.remote}/{args.branch} at {head[:12]}")
    if args.dry_run:
        print("[release] Dry run complete; no tag was created or pushed")
        return 0

    if not args.yes:
        try:
            answer = input(f"Create and push {tag}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[release] Cancelled")
            return 0
        if answer not in {"y", "yes"}:
            print("[release] Cancelled")
            return 0

    try:
        _run_checked(("git", "tag", "-a", tag, "-m", f"Release {tag}"), root=root)
        _run_checked(("git", "push", args.remote, tag), root=root)
    except ReleaseError as exc:
        print(f"[release] ERROR: {exc}", file=sys.stderr)
        print(f"[release] If the tag was created locally, push it with: git push {args.remote} {tag}", file=sys.stderr)
        return 1

    print(f"[release] Tag {tag} pushed. GitHub Actions will create the release.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
