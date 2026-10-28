"""Repository identity, locking, and Git worktree lifecycle.

This module owns filesystem/state namespaces and worktree discovery/creation.
The compatibility-facing agentic module re-exports these helpers.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Mapping
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from threading import Thread
import uuid
from typing import Any

from .execution import _console_print
from .execution_stream import _process_group_kwargs, _stop_process_tree
from .git_policy import (
    GIT_SAFETY_CONFIG,
    hardened_git_environment,
    hardened_git_argv,
    git_filter_probe_argv,
)
from .models import DEFAULT_GITHUB_HOST, Issue, PullRequest, Run, StatePaths
from .security import redact_secrets
from .output import style_system_text, style_text, timestamped_status

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
MAX_SELECTION_RUNS = 5
RELATIVE_WORKTREE_GIT = (2, 48, 0)
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
_GIT_SAFETY_CONFIG = GIT_SAFETY_CONFIG


class UserCancelled(RuntimeError):
    """Signal that a user cancelled an interactive runner step."""


def _status_print(message: Any, *, file: Any = None, flush: bool = False,
                  system_prefix: str | None = None) -> None:
    message = timestamped_status(message)
    if system_prefix is not None:
        rendered = style_system_text(message, file, system_prefix=system_prefix)
    else:
        rendered = style_text(message, file)
    _console_print(rendered, file=file, flush=flush)

def default_state_dir() -> Path:
    """Return the user-level state root, overridable for shared WSL/Windows storage."""
    configured = os.environ.get("AGENTIC_STATE_DIR")
    if configured:
        # Keep the lexical path until state_paths() can reject symlinked or
        # attacker-writable roots. Resolving here would silently bless a
        # replaceable AGENTIC_STATE_DIR before the security check runs.
        return Path(configured).expanduser()
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


def repository_identity(repo: Path) -> str:
    """Return a non-reversible identity for repository trust decisions.

    The trust record stores only the digest returned here.  The canonical
    checkout, Git common directory, and configured remote identities are used
    so a moved checkout or changed remote cannot silently inherit consent.
    """
    root = repo.resolve()
    try:
        root_text = git(repo, "rev-parse", "--show-toplevel").strip()
        if root_text:
            root = Path(root_text).resolve()
    except (OSError, RuntimeError):
        pass
    try:
        common_text = git(repo, "rev-parse", "--git-common-dir").strip()
        common = Path(common_text)
        if not common.is_absolute():
            common = root / common
        common_text = str(common.resolve())
    except (OSError, RuntimeError):
        common_text = ""
    try:
        remote_text = git(repo, "remote", "-v", check=False).strip()
    except (OSError, RuntimeError):
        remote_text = ""
    payload = "\0".join((str(root), common_text, remote_text))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _absolute_state_path(path: Path) -> Path:
    """Make an absolute state path, canonicalizing Windows spellings only."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    # Windows commonly returns short 8.3 spellings from temporary-directory
    # helpers. Canonicalize there so state paths compare consistently with
    # Path.resolve() and child processes receive the same spelling.
    return absolute.resolve() if os.name == "nt" else absolute


def _state_metadata_is_safe(
    metadata: os.stat_result, *, label: str, require_current_owner: bool,
    path: Path | None = None,
) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label} must not contain symbolic links")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a directory")
    current_uid = getattr(os, "getuid", lambda: metadata.st_uid)()
    trusted_shared_path = path in {
        Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"),
    }
    if require_current_owner:
        if metadata.st_uid != current_uid:
            raise ValueError(f"{label} must be owned by the current user")
    # A system-managed sticky directory such as /tmp is safe as an ancestor:
    # its owner cannot be an unprivileged attacker and sticky semantics protect
    # entries created below it. Other writable ancestors are replaceable by an
    # attacker. The configured state root itself must never be writable, even
    # when the current user is root.
    writable = metadata.st_mode & 0o022
    if writable and (
        require_current_owner
        or (metadata.st_uid != 0 and not trusted_shared_path)
        or not (metadata.st_mode & stat.S_ISVTX)
    ):
        raise ValueError(f"{label} must not be group/other writable")


def _existing_state_components(path: Path) -> list[tuple[Path, os.stat_result]]:
    """Return existing lexical components, without following a final link."""
    components: list[tuple[Path, os.stat_result]] = []
    current = path
    while True:
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            current = current.parent
        else:
            components.append((current, metadata))
            current = current.parent
        if current == current.parent:
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                pass
            else:
                components.append((current, metadata))
            return components


def _validate_state_path_components(path: Path, *, label: str) -> None:
    """Reject replaceable or unsafe existing ancestors of a state path."""
    absolute = _absolute_state_path(path)
    components = _existing_state_components(absolute)
    for component, metadata in components:
        _state_metadata_is_safe(
            metadata, label=f"{label} ancestor {component}",
            require_current_owner=False,
            path=component,
        )


def _validate_state_directory(path: Path, *, label: str) -> None:
    """Reject attacker-writable POSIX state directories.

    Missing state roots are allowed for read-only callers. Existing roots must
    be owned by the current user and must not be group/other writable. A
    sticky temporary parent such as ``/tmp`` remains compatible with the
    documented disposable/shared-path use when a writer creates the root.
    Windows does not expose a portable stdlib ownership/mode contract here;
    descriptor-level log checks still apply there.
    """
    if os.name == "nt":
        return
    path = _absolute_state_path(path)
    _validate_state_path_components(path, label=label)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        # Read-only dashboard and diagnostics paths may legitimately point at
        # a state namespace that has not been created yet. Writers must call
        # ensure_state_directory/ensure_state_paths before creating artifacts.
        return
    _state_metadata_is_safe(
        metadata, label=label, require_current_owner=True, path=path,
    )


def _ensure_state_directory(path: Path, *, label: str) -> Path:
    """Create and validate a state directory through anchored descriptors."""
    path = _absolute_state_path(path)
    if os.name == "nt":
        path.mkdir(parents=True, exist_ok=True)
        return path
    if not (
        hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
        and os.open in getattr(os, "supports_dir_fd", set())
        and os.mkdir in getattr(os, "supports_dir_fd", set())
    ):
        raise OSError("anchored state-directory creation is unavailable")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor or os.path.sep, flags)
    try:
        parts = path.parts[1:] if path.anchor else path.parts
        component_path = Path(path.anchor or os.path.sep)
        for index, component in enumerate(parts):
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, flags, dir_fd=descriptor)
            try:
                _state_metadata_is_safe(
                    os.fstat(child), label=label,
                    require_current_owner=index == len(parts) - 1,
                    path=component_path.joinpath(*parts[:index + 1]),
                )
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        if not parts:
            _state_metadata_is_safe(
                os.fstat(descriptor), label=label, require_current_owner=True,
                path=path,
            )
    finally:
        os.close(descriptor)
    return path


def state_paths(repo: Path, state_dir: Path | None = None) -> StatePaths:
    base = _absolute_state_path((state_dir or default_state_dir()).expanduser())
    if os.name != "nt":
        _validate_state_path_components(base, label="state directory")
    _validate_state_directory(base, label="state directory")
    root = base / repository_state_key(repo)
    _validate_state_directory(root, label="repository state directory")
    for name in ("worktrees", "runs", "shell", "dashboards", "diagnostics"):
        child = root / name
        if child.exists() or child.is_symlink():
            _validate_state_directory(child, label=f"repository {name} directory")
    return StatePaths(
        root, root / "worktrees", root / "runs", root / "shell", root / "dashboards",
        root / "diagnostics",
    )


def ensure_state_directory(path: Path, *, label: str = "state directory") -> Path:
    """Create one writer-owned state directory with a race-resistant boundary."""
    return _ensure_state_directory(path, label=label)


def ensure_state_paths(repo: Path, state_dir: Path | None = None) -> StatePaths:
    """Create the central state hierarchy before a writer uses its paths."""
    base = _absolute_state_path((state_dir or default_state_dir()).expanduser())
    _ensure_state_directory(base, label="state directory")
    root = _ensure_state_directory(
        base / repository_state_key(repo), label="repository state directory",
    )
    for name in ("worktrees", "runs", "shell", "dashboards", "diagnostics", "locks"):
        _ensure_state_directory(root / name, label=f"repository {name} directory")
    return StatePaths(
        root, root / "worktrees", root / "runs", root / "shell", root / "dashboards",
        root / "diagnostics",
    )


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


def _run_command(argv: list[str], cwd: Path, *, timeout: int = 180,
                 check: bool = True, env: dict[str, str] | None = None,
                 max_output_bytes: int | None = None) -> subprocess.CompletedProcess[str]:
    """Subprocess without a shell; issue data never becomes shell code."""
    if max_output_bytes is None:
        max_output_bytes = MAX_COMMAND_OUTPUT_BYTES
    if max_output_bytes is not None and max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    try:
        if max_output_bytes is None:
            result = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, timeout=timeout,
                                    check=False, env=env)
        else:
            process = subprocess.Popen(
                argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, **_process_group_kwargs(),
            )
            chunks: list[bytes] = []
            overflow = False
            read_error: BaseException | None = None

            def read_output() -> None:
                nonlocal overflow, read_error
                total = 0
                stream = process.stdout
                if stream is None:
                    return
                try:
                    while True:
                        read = getattr(stream, "read1", stream.read)
                        chunk = read(64 * 1024)
                        if not chunk:
                            return
                        total += len(chunk)
                        if total > max_output_bytes:
                            overflow = True
                            try:
                                _stop_process_tree(process)
                            except OSError:
                                pass
                            return
                        chunks.append(chunk)
                except BaseException as exc:  # pragma: no cover - OS-specific pipe failures
                    read_error = exc

            reader = Thread(target=read_output, daemon=True)
            reader.start()
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _stop_process_tree(process)
                process.wait()
                reader.join(timeout=1)
                if process.stdout is not None:
                    process.stdout.close()
                raise exc
            reader.join(timeout=1)
            if reader.is_alive():
                _stop_process_tree(process)
                process.wait()
                reader.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
            if overflow:
                raise RuntimeError(
                    f"Command output exceeded the {max_output_bytes}-byte safety limit"
                )
            if read_error is not None:
                raise RuntimeError("Could not read command output") from read_error
            result = subprocess.CompletedProcess(
                argv, returncode, b"".join(chunks).decode("utf-8", errors="replace"), None,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not execute {argv[0]!r}: {exc}") from exc
    if check and result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {argv!r}\n"
            f"{redact_secrets(result.stdout[-3500:])}"
        )
    return result


def _is_git_command(argv: list[str]) -> bool:
    return bool(argv) and Path(argv[0]).name.casefold() in {"git", "git.exe"}


def _is_filter_probe(argv: list[str]) -> bool:
    return _is_git_command(argv) and argv == git_filter_probe_argv()


def _hardened_git_command(
    argv: list[str], cwd: Path, *, timeout: int, check: bool,
    env: dict[str, str] | None, max_output_bytes: int | None,
) -> subprocess.CompletedProcess[str]:
    """Run any Git argv through the repository-configuration safety boundary."""
    environment = hardened_git_environment(env)
    filter_probe = _run_command(
        git_filter_probe_argv(argv[0]), cwd, timeout=10, check=False,
        env=environment, max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    if filter_probe.returncode not in (0, 1):
        raise RuntimeError("Could not inspect repository Git filter configuration")
    if filter_probe.stdout.strip():
        raise RuntimeError(
            "Repository Git filter drivers are not supported for safe operations"
        )
    return _run_command(
        hardened_git_argv(argv), cwd, timeout=timeout, check=check,
        env=environment, max_output_bytes=max_output_bytes,
    )


def command(argv: list[str], cwd: Path, *, timeout: int = 180,
            check: bool = True, env: dict[str, str] | None = None,
            max_output_bytes: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run argv safely, routing every Git invocation through one dispatcher."""
    if _is_git_command(argv) and not _is_filter_probe(argv):
        return _hardened_git_command(
            argv, cwd, timeout=timeout, check=check, env=env,
            max_output_bytes=max_output_bytes,
        )
    return _run_command(
        argv, cwd, timeout=timeout, check=check, env=env,
        max_output_bytes=max_output_bytes,
    )


def git(repo: Path, *args: str, check: bool = True,
        env: Mapping[str, str] | None = None) -> str:
    # Repository-local Git configuration is untrusted input. These overrides
    # prevent status/diff/fetch/worktree operations from starting configured
    # fsmonitor, external-diff, credential, proxy, SSH, or transport helpers.
    # Keep the environment explicit as well: Git gives several environment
    # variables precedence over configuration files.
    environment = hardened_git_environment(env)
    filter_probe = command(
        git_filter_probe_argv(),
        repo, timeout=10, check=False, env=environment,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    if filter_probe.returncode not in (0, 1):
        raise RuntimeError("Could not inspect repository Git filter configuration")
    if filter_probe.stdout.strip():
        raise RuntimeError(
            "Repository Git filter drivers are not supported for safe operations"
        )
    return command(
        hardened_git_argv(["git", *args]), repo,
        check=check, env=environment,
    ).stdout.strip()


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
    if _is_git_command(argv):
        try:
            result = command(argv, cwd, timeout=timeout, check=False, env=env)
        except RuntimeError as exc:
            return False, str(exc)
        output = result.stdout.strip()
        return result.returncode == 0, output
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


def is_registered_worktree(
    repo: Path, worktree: Path, *, git_runner=None,
) -> bool:
    """Return whether a directory is registered in the repository's worktree list.
    """
    if not worktree.is_dir():
        return False
    if not (repo / ".git").exists():
        return False
    runner = git if git_runner is None else git_runner
    try:
        raw = runner(repo, "worktree", "list", "--porcelain")
    except RuntimeError:
        return False
    if not isinstance(raw, str):
        return False
    target = worktree.resolve()
    for line in raw.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line.removeprefix("worktree "))
        try:
            if candidate.resolve() == target:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def repair_relative_worktree(repo: Path, worktree: Path) -> bool:
    """Convert an existing worktree's Git links to relative paths when safe."""
    if not worktree.is_dir():
        return False
    if is_registered_worktree(repo, worktree) is False:
        raise RuntimeError(
            f"Git worktree is invalid or not registered: {worktree}; "
            "infrastructure failure, retries skipped"
        )
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
    paths = state_paths(repo, state_dir)
    lock_path = paths.root / "locks" / "repository.lock"
    try:
        ensure_state_directory(lock_path.parent, label="repository locks directory")
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
    paths = state_paths(repo, state_dir)
    lock_path = paths.root / "locks" / f"worktree-{identity}.lock"
    try:
        ensure_state_directory(lock_path.parent, label="worktree locks directory")
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
    if not run.worktree.is_dir():
        raise RuntimeError(
            f"Git worktree is missing: {run.worktree}; "
            "infrastructure failure, retries skipped"
        )
    if is_registered_worktree(repo, run.worktree) is False:
        raise RuntimeError(
            f"Git worktree is invalid or not registered: {run.worktree}; "
            "infrastructure failure, retries skipped"
        )
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
    ), repo, timeout=30, env=_github_environment(github_host), max_output_bytes=256_000).stdout)
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
    # Require a hostname boundary so ``evilgithub.com`` cannot be treated as
    # the configured ``github.com`` host. The lookahead preserves URL, SSH,
    # and scp-style remotes, whose host is followed by either '/' or ':'.
    match = re.search(
        rf"(?<![A-Za-z0-9.-]){escaped_host}(?=[:/])[:/]([^/]+)/([^/]+?)(?:\.git)?$",
        remote,
        re.IGNORECASE,
    )
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


def choose_existing(
    runs: list[Run], *, prompt_prefix: str = "RESUME", interactive: bool = True
) -> Run:
    """Choose an existing run, or reuse the newest run without a prompt.

    Background shell jobs intentionally have stdin connected to ``DEVNULL``.
    When more than one worktree exists, prompting from such a process raises
    ``EOFError`` and prevents the child runner from starting. The discovery
    order is newest-first, so selecting the first run is deterministic and
    matches the default choice a user would normally make for a background
    resume. Direct terminal invocations continue to use the explicit prompt.
    """
    displayed_runs = runs[:MAX_SELECTION_RUNS]
    label = prompt_prefix.upper()
    if len(displayed_runs) == 1:
        _status_print(
            f"[{label}] Worktree selected: {displayed_runs[0].branch} "
            f"({displayed_runs[0].worktree})"
        )
        return displayed_runs[0]
    _status_print("\nMultiple worktrees exist for this issue:")
    for idx, run in enumerate(displayed_runs, 1):
        _status_print(f" {idx}. {run.branch}  ({run.worktree})")
    if not interactive:
        selected = displayed_runs[0]
        _status_print(
            f"[{label}] Non-interactive run; selecting newest worktree: "
            f"{selected.branch} ({selected.worktree})"
        )
        return selected
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
                f"{len(displayed_runs)} or 'q' to cancel."
            )
            continue
        if not 1 <= selected_number <= len(displayed_runs):
            _status_print(
                f"[{label}] Invalid worktree selection. Enter a number from 1 to "
                f"{len(displayed_runs)} or 'q' to cancel."
            )
            continue
        selected = displayed_runs[selected_number - 1]
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
        parent = ensure_state_paths(repo, state_dir).worktrees if state_dir else legacy_worktrees_dir(repo)
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
        parent = ensure_state_paths(repo, state_dir).worktrees if state_dir else legacy_worktrees_dir(repo)
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
        parent = ensure_state_paths(repo, state_dir).worktrees if state_dir else legacy_worktrees_dir(repo)
        parent.mkdir(parents=True, exist_ok=True)
        worktree = parent / f"audit-{issue.audit_key}-{timestamp}{ident}"
        git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base}")
    if relative_paths:
        _status_print("[GIT] Using relative worktree paths for WSL/Windows compatibility")
    else:
        _status_print("[GIT] Relative worktree paths unavailable; using absolute paths")
    return Run(None, branch, worktree.resolve(), audit_key=issue.audit_key)
