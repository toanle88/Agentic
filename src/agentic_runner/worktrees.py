"""Repository identity, locking, and Git worktree lifecycle.

This module owns filesystem/state namespaces and worktree discovery/creation.
The compatibility-facing agentic module re-exports these helpers.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
from typing import Any

from .execution import _console_print
from .models import DEFAULT_GITHUB_HOST, Issue, PullRequest, Run, StatePaths
from .security import redact_secrets
from .output import style_system_text, style_text

BRANCH_RE = re.compile(
    r"^agent/issue-(?P<number>\d+)-"
    r"(?:(?P<timestamp>\d{8}T\d{6}(?:\d{6})?Z)-)?"
    r"(?P<ident>[a-f0-9]{8})$"
)
LOCAL_BRANCH_RE = re.compile(
    r"^agent/task-(?P<slug>[a-z0-9-]+)-(?P<key>[a-f0-9]{12})"
    r"(?:-(?P<timestamp>\d{8}T\d{6}(?:\d{6})?Z)"
    r"(?:-(?P<ident>[a-f0-9]{8}))?|-(?P<legacy_ident>[a-f0-9]{8}))?$"
)
AUDIT_BRANCH_RE = re.compile(
    r"^agent/audit-(?P<key>[a-f0-9]{12})"
    r"(?:(?:-(?P<timestamp>\d{8}T\d{6}(?:\d{6})?Z))"
    r"(?:-(?P<ident>[a-f0-9]{8}))?"
    r"|(?P<legacy_ident>-[a-f0-9]{8}))?$"
)
AUDIT_KEY = hashlib.sha256(b"agentic-repository-audit-v1").hexdigest()[:12]
RELATIVE_WORKTREE_GIT = (2, 48, 0)


class UserCancelled(RuntimeError):
    """Signal that a user cancelled an interactive runner step."""


def _status_print(message: Any, *, file: Any = None, flush: bool = False,
                  system_prefix: str | None = None) -> None:
    if system_prefix is not None:
        rendered = style_system_text(message, file, system_prefix=system_prefix)
    else:
        rendered = style_text(message, file)
    _console_print(rendered, file=file, flush=flush)

def default_state_dir() -> Path:
    """Return the user-level state root, overridable for shared WSL/Windows storage."""
    configured = os.environ.get("AGENTIC_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "Agentic"
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / "agentic"


def _portable_path_text(value: str) -> str:
    """Normalize WSL and Windows spellings of the same path for state identity."""
    text = value.replace("\\", "/").rstrip("/").lower()
    match = re.match(r"^/mnt/([a-z])/(.*)$", text)
    if match:
        return f"{match.group(1)}:/{match.group(2)}"
    return text


def repository_state_key(repo: Path) -> str:
    """Create a stable, filesystem-safe namespace for one Git checkout."""
    ok, common_dir = probe(["git", "rev-parse", "--git-common-dir"], repo)
    if ok and common_dir:
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = repo / common_path
        identity = _portable_path_text(str(common_path.resolve()))
    else:
        identity = _portable_path_text(str(repo.resolve()))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", repo.name) or "repository"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def state_paths(repo: Path, state_dir: Path | None = None) -> StatePaths:
    root = (state_dir or default_state_dir()).expanduser().resolve() / repository_state_key(repo)
    return StatePaths(root, root / "worktrees", root / "runs", root / "shell", root / "dashboards")


def legacy_worktrees_dir(repo: Path) -> Path:
    return repo.parent / f".{repo.name}-agent-worktrees"


def legacy_runs_dir(repo: Path) -> Path:
    return repo.parent / f".{repo.name}-agent-runs"


def eligible(issue: Issue) -> bool:
    """Local tasks and audits are always eligible; GitHub uses issue state."""
    return issue.is_local or issue.is_audit or issue.state.upper() == "OPEN"


def issue_fingerprint(issue: Issue) -> str:
    """Identify the issue content used by a cached investigation result."""
    payload = json.dumps(
        [issue.reference, issue.title, issue.body, issue.state, issue.source, issue.audit_key],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def command(argv: list[str], cwd: Path, *, timeout: int = 180,
            check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Subprocess without a shell; issue data never becomes shell code."""
    try:
        result = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, timeout=timeout,
                                check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not execute {argv[0]!r}: {exc}") from exc
    if check and result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {argv!r}\n"
            f"{redact_secrets(result.stdout[-3500:])}"
        )
    return result


def git(repo: Path, *args: str, check: bool = True) -> str:
    return command(["git", *args], repo, check=check).stdout.strip()


def installed_git_version(executable: str = "git") -> tuple[int, int, int] | None:
    """Return a Git version, or None when an executable cannot be queried."""
    try:
        result = subprocess.run(
            [executable, "--version"], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\b(\d+)\.(\d+)(?:\.(\d+))?\b", result.stdout)
    if result.returncode or not match:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def probe(
    argv: list[str], cwd: Path, *, timeout: int = 10,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Run a small diagnostic command without turning its output into an exception."""
    try:
        result = subprocess.run(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = result.stdout.strip()
    return result.returncode == 0, output


def relative_worktree_paths_supported() -> bool:
    """Check every Git installation that must be able to open the worktree.

    WSL can invoke Windows Git as ``git.exe``. Relative worktrees use a Git
    repository extension, so enabling them while either installation is too
    old would make the shared repository incompatible with that installation.
    """
    current = installed_git_version()
    if current is None or current < RELATIVE_WORKTREE_GIT:
        return False
    if os.name == "nt" or not os.environ.get("WSL_DISTRO_NAME"):
        return True
    windows_git = shutil.which("git.exe")
    windows_version = installed_git_version(windows_git) if windows_git else None
    return windows_version is not None and windows_version >= RELATIVE_WORKTREE_GIT


def configure_relative_worktree_paths(repo: Path) -> bool:
    """Enable portable worktree metadata only when all required Git versions support it."""
    if not relative_worktree_paths_supported():
        return False
    git(repo, "config", "worktree.useRelativePaths", "true")
    return True


def repair_relative_worktree(repo: Path, worktree: Path) -> bool:
    """Convert an existing worktree's Git links to relative paths when safe."""
    if not configure_relative_worktree_paths(repo):
        return False
    git(repo, "worktree", "repair", "--relative-paths", str(worktree))
    return True


def refresh_base(repo: Path, base: str) -> None:
    """Update the remote-tracking base ref before validating or publishing."""
    git(repo, "fetch", "origin", base)


@contextmanager
def _exclusive_lock(lock_path: Path, message: str):
    """Acquire a cross-process lock using only a narrowly scoped state file."""
    lock_path = lock_path.resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)

    with lock_path.open("r+b") as lock_file:
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError) as exc:
            raise RuntimeError(message) from exc
        try:
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def repository_lock(repo: Path, state_dir: Path | None = None):
    """Serialize operations that mutate shared Git repository metadata."""
    lock_path = state_paths(repo, state_dir).root / "locks" / "repository.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Read-only test sandboxes and locked-down hosts may not permit the
        # configured user state root. Keep the operation safe with a narrow
        # Git-metadata fallback rather than disabling locking.
        lock_path = Path(git(repo, "rev-parse", "--git-path", "agentic.lock"))
        if not lock_path.is_absolute():
            lock_path = repo / lock_path
    with _exclusive_lock(
        lock_path,
        f"Another Agentic repository operation is already active for {repo}; wait for it to finish",
    ):
        yield


@contextmanager
def worktree_path_lock(repo: Path, worktree: Path, state_dir: Path | None = None):
    """Allow independent worktrees to run concurrently, but not one worktree twice."""
    identity = hashlib.sha256(str(worktree.resolve()).encode("utf-8")).hexdigest()[:16]
    lock_path = state_paths(repo, state_dir).root / "locks" / f"worktree-{identity}.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        git_dir = Path(git(repo, "rev-parse", "--git-path", "agentic-worktree-locks"))
        if not git_dir.is_absolute():
            git_dir = repo / git_dir
        lock_path = git_dir / f"{identity}.lock"
    with _exclusive_lock(
        lock_path,
        f"Another Agentic operation is already active for worktree {worktree}; wait for it to finish",
    ):
        yield


@contextmanager
def worktree_lock(repo: Path, run: Run, state_dir: Path | None = None):
    """Allow independent worktrees to run concurrently, but not one worktree twice."""
    with worktree_path_lock(repo, run.worktree, state_dir):
        yield


def _github_command(host: str, *args: str) -> list[str]:
    """Build a GitHub CLI argv; the caller supplies the host per process."""
    return ["gh", *args]


def _github_environment(host: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment["GH_HOST"] = host
    return environment


def github_metadata(repo: Path, github_host: str = DEFAULT_GITHUB_HOST) -> tuple[str, str]:
    data = json.loads(command(
        _github_command(github_host, "repo", "view", "--json", "nameWithOwner,defaultBranchRef"),
        repo, env=_github_environment(github_host),
    ).stdout)
    return data["nameWithOwner"], data["defaultBranchRef"]["name"]


def fetch_issues(
    repo: Path, github_repo: str, github_host: str = DEFAULT_GITHUB_HOST,
) -> list[Issue]:
    raw = json.loads(command(_github_command(
        github_host, "issue", "list", "--repo", github_repo, "--state", "open", "--limit", "100",
        "--json", "number,title,body,state",
    ), repo, env=_github_environment(github_host)).stdout)
    # No metadata-based filtering or ranking: present oldest issues first.
    return sorted(
        (issue for item in raw if eligible(issue := Issue.parse(item))),
        key=lambda issue: issue.number if issue.number is not None else 0,
    )


def get_issue(
    repo: Path, github_repo: str, number: int, github_host: str = DEFAULT_GITHUB_HOST,
) -> Issue:
    raw = json.loads(command(_github_command(
        github_host, "issue", "view", str(number), "--repo", github_repo,
        "--json", "number,title,body,state",
    ), repo, env=_github_environment(github_host)).stdout)
    return Issue.parse(raw)


def get_pull_request(
    repo: Path, github_repo: str, number: int,
    github_host: str = DEFAULT_GITHUB_HOST,
) -> PullRequest:
    """Read only the PR identity required for a repair or guarded push."""
    raw = json.loads(command(_github_command(
        github_host, "pr", "view", str(number), "--repo", github_repo,
        "--json", (
            "number,title,body,state,url,baseRefName,baseRefOid,"
            "headRefName,headRefOid,headRepository,headRepositoryOwner"
        ),
    ), repo, env=_github_environment(github_host)).stdout)
    return PullRequest.parse(raw)


def load_local_task(path: Path) -> Issue:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read local task file {path}: {exc}") from exc
    return Issue.from_local_text(text, path.resolve())


def github_repo_from_remote(
    repo: Path, github_host: str = DEFAULT_GITHUB_HOST,
) -> str | None:
    """Extract owner/repository from a GitHub origin without contacting GitHub."""
    remote = git(repo, "remote", "get-url", "origin", check=False).strip()
    escaped_host = re.escape(github_host)
    match = re.search(rf"(?:{escaped_host})[:/]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def default_base_branch(repo: Path) -> str:
    """Resolve the origin base branch without requiring GitHub issue access."""
    remote_head = git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
    prefix = "refs/remotes/origin/"
    if remote_head.startswith(prefix):
        return remote_head.removeprefix(prefix)
    current = git(repo, "branch", "--show-current", check=False)
    return current or "main"


def discover_runs(repo: Path) -> list[Run]:
    """Git's worktree records are the source of truth; no run-state JSON."""
    output = git(repo, "worktree", "list", "--porcelain")
    runs: list[Run] = []
    for record in output.split("\n\n"):
        data: dict[str, str] = {}
        for line in record.splitlines():
            if " " in line:
                key, value = line.split(" ", 1)
                data[key] = value
        branch = data.get("branch", "").removeprefix("refs/heads/")
        match = BRANCH_RE.fullmatch(branch)
        if match and "worktree" in data:
            worktree = Path(data["worktree"])
            if worktree.is_dir():
                runs.append(Run(int(match.group(1)), branch, worktree.resolve()))
                continue
        local_match = LOCAL_BRANCH_RE.fullmatch(branch)
        if local_match and "worktree" in data:
            worktree = Path(data["worktree"])
            if worktree.is_dir():
                runs.append(Run(None, branch, worktree.resolve(), local_match.group("key")))
                continue
        audit_match = AUDIT_BRANCH_RE.fullmatch(branch)
        if audit_match and "worktree" in data:
            worktree = Path(data["worktree"])
            if worktree.is_dir():
                runs.append(Run(
                    None, branch, worktree.resolve(),
                    audit_key=audit_match.group("key"),
                ))
    return sorted(runs, key=lambda r: (r.issue_number is None, r.issue_number or 0, r.branch))


def runs_for_issue(repo: Path, number: int) -> list[Run]:
    runs = [run for run in discover_runs(repo) if run.issue_number == number]
    return sorted(runs, key=_run_sort_key, reverse=True)


def runs_for_task(repo: Path, task_key: str) -> list[Run]:
    runs = [run for run in discover_runs(repo) if run.task_key == task_key]
    return sorted(runs, key=_run_sort_key, reverse=True)


def runs_for_audit(repo: Path, audit_key: str = AUDIT_KEY) -> list[Run]:
    runs = [run for run in discover_runs(repo) if run.audit_key == audit_key]
    return sorted(runs, key=_audit_sort_key, reverse=True)


def _audit_sort_key(run: Run) -> str:
    return _run_sort_key(run)[0]


def _run_sort_key(run: Run) -> tuple[str, str]:
    """Sort timestamped runs newest first, with legacy names sorted last."""
    for pattern in (BRANCH_RE, LOCAL_BRANCH_RE, AUDIT_BRANCH_RE):
        match = pattern.fullmatch(run.branch)
        if match:
            return match.groupdict().get("timestamp") or "", run.branch
    return "", run.branch


def _utc_timestamp() -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond:06d}Z"


def run_for_branch(repo: Path, branch: str) -> Run | None:
    """Find an existing Agentic worktree without creating or checking out one."""
    return next((run for run in discover_runs(repo) if run.branch == branch), None)


def choose_existing(runs: list[Run], *, prompt_prefix: str = "RESUME") -> Run:
    label = prompt_prefix.upper()
    if len(runs) == 1:
        _status_print(
            f"[{label}] Worktree selected: {runs[0].branch} ({runs[0].worktree})"
        )
        return runs[0]
    _status_print("\nMultiple worktrees exist for this issue:")
    for idx, run in enumerate(runs, 1):
        _status_print(f" {idx}. {run.branch}  ({run.worktree})")
    prompt = (
        f"\n[{label}] Waiting for worktree selection. "
        "Enter a number or 'q' to cancel: "
    )
    while True:
        try:
            choice = input(prompt).strip()
        except KeyboardInterrupt as exc:
            _status_print(f"[{label}] Worktree selection cancelled by Ctrl-C")
            raise UserCancelled("Worktree selection cancelled") from exc
        if choice.casefold() == "q":
            _status_print(f"[{label}] Worktree selection cancelled")
            raise UserCancelled("Worktree selection cancelled")
        try:
            # ``str.isdigit`` accepts non-ASCII numerals, and ``int`` can
            # reject an excessively long ASCII digit string. Treat both as
            # ordinary invalid input so the prompt remains retryable.
            if not choice.isascii() or not choice.isdigit():
                raise ValueError
            selected_number = int(choice)
        except ValueError:
            _status_print(
                f"[{label}] Invalid worktree selection. Enter a number from 1 to "
                f"{len(runs)} or 'q' to cancel."
            )
            continue
        if not 1 <= selected_number <= len(runs):
            _status_print(
                f"[{label}] Invalid worktree selection. Enter a number from 1 to "
                f"{len(runs)} or 'q' to cancel."
            )
            continue
        selected = runs[selected_number - 1]
        _status_print(
            f"[{label}] Worktree {selected_number} selected: "
            f"{selected.branch} ({selected.worktree})"
        )
        return selected


def create_worktree(repo: Path, base: str, issue_number: int,
                    state_dir: Path | None = None) -> Run:
    with repository_lock(repo, state_dir):
        refresh_base(repo, base)
        relative_paths = configure_relative_worktree_paths(repo)
        timestamp = _utc_timestamp()
        ident = uuid.uuid4().hex[:8]
        branch = f"agent/issue-{issue_number}-{timestamp}-{ident}"
        # ``state_dir`` is supplied by the CLI. The legacy fallback keeps this
        # low-level helper compatible for callers that intentionally manage paths.
        parent = state_paths(repo, state_dir).worktrees if state_dir else legacy_worktrees_dir(repo)
        parent.mkdir(parents=True, exist_ok=True)
        worktree = parent / f"issue-{issue_number}-{timestamp}-{ident}"
        git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base}")
    if relative_paths:
        _status_print("[GIT] Using relative worktree paths for WSL/Windows compatibility")
    else:
        _status_print("[GIT] Relative worktree paths unavailable; using absolute paths")
    return Run(issue_number, branch, worktree.resolve())


def _task_slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (slug[:40].rstrip("-") or "local-task")


def create_local_worktree(repo: Path, base: str, issue: Issue,
                          state_dir: Path | None = None,
                          separate: bool = False) -> Run:
    if not issue.is_local or not issue.task_key:
        raise ValueError("create_local_worktree requires a local task")
    with repository_lock(repo, state_dir):
        refresh_base(repo, base)
        relative_paths = configure_relative_worktree_paths(repo)
        slug = _task_slug(issue.title)
        timestamp = _utc_timestamp()
        ident = f"-{uuid.uuid4().hex[:8]}" if separate else ""
        branch = f"agent/task-{slug}-{issue.task_key}-{timestamp}{ident}"
        parent = state_paths(repo, state_dir).worktrees if state_dir else legacy_worktrees_dir(repo)
        parent.mkdir(parents=True, exist_ok=True)
        worktree = parent / f"task-{slug}-{issue.task_key}-{timestamp}{ident}"
        git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base}")
    if relative_paths:
        _status_print("[GIT] Using relative worktree paths for WSL/Windows compatibility")
    else:
        _status_print("[GIT] Relative worktree paths unavailable; using absolute paths")
    return Run(None, branch, worktree.resolve(), issue.task_key)


def create_audit_worktree(repo: Path, base: str, issue: Issue,
                          state_dir: Path | None = None,
                          separate: bool = False) -> Run:
    """Create an isolated worktree for the repository-wide audit workflow."""
    if not issue.is_audit or not issue.audit_key:
        raise ValueError("create_audit_worktree requires an audit work item")
    with repository_lock(repo, state_dir):
        refresh_base(repo, base)
        relative_paths = configure_relative_worktree_paths(repo)
        ident = f"-{uuid.uuid4().hex[:8]}" if separate else ""
        timestamp = _utc_timestamp()
        branch = f"agent/audit-{issue.audit_key}-{timestamp}{ident}"
        parent = state_paths(repo, state_dir).worktrees if state_dir else legacy_worktrees_dir(repo)
        parent.mkdir(parents=True, exist_ok=True)
        worktree = parent / f"audit-{issue.audit_key}-{timestamp}{ident}"
        git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base}")
    if relative_paths:
        _status_print("[GIT] Using relative worktree paths for WSL/Windows compatibility")
    else:
        _status_print("[GIT] Relative worktree paths unavailable; using absolute paths")
    return Run(None, branch, worktree.resolve(), audit_key=issue.audit_key)
