#!/usr/bin/env python3
"""Prepare a package-version pull request for Agentic.

This command updates ``agentic.__version__`` on a release branch, runs the
local release checks, pushes the branch, and opens a normal GitHub pull
request. The existing ``scripts/release.py`` remains responsible for creating
the tag after that pull request has been merged.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Sequence

try:
    from .release import (
        ReleaseError,
        _git,
        _run,
        _run_checked,
        _run_local_checks,
        package_version,
        release_tag,
    )
except ImportError:  # pragma: no cover - used when run as ``python scripts/prepare_release.py``
    from release import (  # type: ignore[no-redef]
        ReleaseError,
        _git,
        _run,
        _run_checked,
        _run_local_checks,
        package_version,
        release_tag,
    )


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
VERSION_LINE_PATTERN = re.compile(
    r"^(?P<prefix>\s*__version__\s*=\s*)(?P<quote>['\"])(?P<value>[^'\"]*)(?P=quote)"
    r"(?P<suffix>\s*(?:\r?\n)?$)"
)


def version_parts(version: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise ReleaseError(
            f"Version {version!r} is not supported; use MAJOR.MINOR.PATCH, such as 1.0.1"
        )
    return tuple(int(part) for part in match.groups())


def update_package_version(root: Path, version: str) -> None:
    """Update the single literal ``__version__`` assignment in agentic.py."""
    source = root / "agentic.py"
    try:
        lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        raise ReleaseError(f"Unable to read {source}: {exc}") from exc

    matches = [(index, VERSION_LINE_PATTERN.match(line)) for index, line in enumerate(lines)]
    matches = [(index, match) for index, match in matches if match is not None]
    if len(matches) != 1:
        raise ReleaseError(f"Expected one literal __version__ assignment in {source}, found {len(matches)}")

    index, match = matches[0]
    assert match is not None
    lines[index] = (
        f"{match.group('prefix')}{match.group('quote')}{version}"
        f"{match.group('quote')}{match.group('suffix')}"
    )
    try:
        source.write_text("".join(lines), encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(f"Unable to update {source}: {exc}") from exc


def _branch_exists(root: Path, remote: str, branch: str) -> bool:
    local = _run(
        ("git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
        root=root,
        capture_output=True,
    )
    if local.returncode == 0:
        return True

    remote_result = _run(
        ("git", "ls-remote", "--exit-code", "--heads", remote, branch),
        root=root,
        capture_output=True,
    )
    return remote_result.returncode == 0


def _ensure_prepare_preconditions(root: Path, remote: str, base: str, branch: str) -> str:
    _git("rev-parse", "--show-toplevel", root=root)
    current_branch = _git("branch", "--show-current", root=root)
    if current_branch != base:
        raise ReleaseError(f"Release PR must start from {base!r}; current branch is {current_branch or '(detached HEAD)'!r}")

    if _git("remote", "get-url", remote, root=root, check=False) == "":
        raise ReleaseError(f"Git remote {remote!r} does not exist")

    _run_checked(("git", "fetch", "--quiet", remote, base), root=root)
    status = _git("status", "--porcelain=v1", "--untracked-files=all", root=root)
    if status:
        raise ReleaseError("Working tree is not clean. Commit or remove these changes before preparing a release PR:\n" + status)

    local_head = _git("rev-parse", "HEAD", root=root)
    remote_head = _git("rev-parse", f"refs/remotes/{remote}/{base}", root=root)
    if local_head != remote_head:
        raise ReleaseError(f"Local {base} is not synchronized with {remote}/{base}; pull the approved changes first")

    branch_check = _run(
        ("git", "check-ref-format", "--branch", branch),
        root=root,
        capture_output=True,
    )
    if branch_check.returncode != 0:
        raise ReleaseError(f"Invalid release branch name: {branch!r}")
    if _branch_exists(root, remote, branch):
        raise ReleaseError(f"Release branch {branch!r} already exists locally or on {remote}")

    _run_checked(("git", "diff", "--check"), root=root)
    return local_head


def _ensure_github_auth(root: Path) -> None:
    result = _run(("gh", "auth", "status"), root=root, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "not authenticated").strip()
        raise ReleaseError(f"GitHub CLI authentication failed; run `gh auth login` first\n{detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update Agentic's version, push a release branch, and open a GitHub pull request."
    )
    parser.add_argument(
        "version",
        nargs="?",
        help="New MAJOR.MINOR.PATCH version; if omitted, prompt interactively",
    )
    parser.add_argument("--remote", default="origin", help="Git remote to fetch and push (default: origin)")
    parser.add_argument("--base", default="main", help="Base branch to release from and target (default: main)")
    parser.add_argument("--branch", help="Release branch name (default: codex/release-vVERSION)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run preconditions and checks without changing files, branches, commits, or remote state",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    return parser


def _prompt_for_version(current: str) -> str:
    try:
        value = input(f"New version (current {current}, for example {current.rsplit('.', 1)[0]}.1): ").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise ReleaseError("No version was provided") from exc
    if not value:
        raise ReleaseError("No version was provided")
    return value


def _pull_request_body(tag: str) -> str:
    return (
        f"## Release {tag}\n\n"
        "This pull request updates the Agentic package version.\n\n"
        "After merging, run `python scripts/release.py` from the synchronized `main` "
        "branch to create and push the release tag."
    )


def main(argv: Sequence[str] | None = None, *, root: Path = ROOT) -> int:
    args = build_parser().parse_args(argv)
    try:
        current_version = package_version(root)
        requested_version = args.version or _prompt_for_version(current_version)
        requested_parts = version_parts(requested_version)
        current_parts = version_parts(current_version)
        if requested_parts <= current_parts:
            raise ReleaseError(
                f"New version {requested_version} must be greater than the current version {current_version}"
            )
        tag = release_tag(requested_version)
        branch = args.branch or f"codex/release-{tag}"
        head = _ensure_prepare_preconditions(root, args.remote, args.base, branch)
        if args.dry_run:
            _run_local_checks(root)
            print(f"[release-pr] Ready to prepare {tag} from {args.remote}/{args.base} at {head[:12]}")
            print(f"[release-pr] Planned branch: {branch}")
            print("[release-pr] Dry run complete; no files, branch, commit, push, or PR was created")
            return 0
        _ensure_github_auth(root)
    except ReleaseError as exc:
        print(f"[release-pr] ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"[release-pr] Will prepare {tag} on branch {branch}")
    if not args.yes:
        try:
            answer = input("Create the release branch, push it, and open the PR? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[release-pr] Cancelled")
            return 0
        if answer not in {"y", "yes"}:
            print("[release-pr] Cancelled")
            return 0

    try:
        _run_checked(("git", "switch", "-c", branch), root=root)
        update_package_version(root, requested_version)
        if package_version(root) != requested_version:
            raise ReleaseError(f"Version update did not produce {requested_version!r}")
        _run_local_checks(root)
        _run_checked(("git", "diff", "--check"), root=root)
        _run_checked(("git", "add", "agentic.py"), root=root)
        _run_checked(("git", "commit", "-m", f"chore: prepare release {tag}"), root=root)
        _run_checked(("git", "push", "--set-upstream", args.remote, branch), root=root)
        pr = _run_checked(
            (
                "gh",
                "pr",
                "create",
                "--base",
                args.base,
                "--head",
                branch,
                "--title",
                f"chore: prepare release {tag}",
                "--body",
                _pull_request_body(tag),
            ),
            root=root,
            capture_output=True,
        )
    except (ReleaseError, OSError) as exc:
        print(f"[release-pr] ERROR: {exc}", file=sys.stderr)
        print(f"[release-pr] The branch may be preserved for recovery: {branch}", file=sys.stderr)
        return 1

    pr_url = (pr.stdout or pr.stderr or "").strip().splitlines()[-1:] or ["(URL not returned)"]
    print(f"[release-pr] Release PR created: {pr_url[0]}")
    print(f"[release-pr] Merge the PR, then run: python scripts/release.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
