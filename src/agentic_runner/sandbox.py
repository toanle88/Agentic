"""Optional container command isolation for provider and test processes."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Any

from .file_boundaries import (
    _is_reparse_or_symbolic_link,
    iter_safe_regular_files,
    open_relative_regular,
    revalidate_single_link_regular,
)
from .git_policy import (
    GIT_SAFETY_ENVIRONMENT,
    hardened_git_argv,
    hardened_git_environment,
    is_blocked_git_environment_name,
)
from .policy import forbidden


SUPPORTED_ENGINES = ("docker", "podman")
SUPPORTED_LANGUAGES = ("python", "node", "go", "rust", "java", "dotnet")
DEFAULT_SANDBOX_IMAGE = "agentic-runner:latest"
DEFAULT_BUILD_NETWORK = False
MAX_BUILD_CONTEXT_BYTES = 64 * 1024 * 1024
MAX_BUILD_CONTEXT_FILE_BYTES = 16 * 1024 * 1024
MAX_BUILD_CONTEXT_FILES = 10_000
MAX_BUILD_CONTEXT_ENTRIES = 12_000
MAX_BUILD_CONTEXT_DIRECTORIES = 2_000
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Keep image references in the image position of the engine argv. In
# particular, never allow a leading dash that could be parsed as another
# Docker/Podman option.
IMAGE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")


def _relative_path_setting(name: str, value: Any, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip() or any(char in value for char in "\r\n\x00"):
        raise ValueError(f"sandbox.{name} must be a nonempty relative path")
    # Validate both path syntaxes so a configuration written on one platform
    # cannot bypass the repository boundary when read on another.
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        Path(value).is_absolute()
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise ValueError(f"sandbox.{name} must stay within the repository")
    return value


def validate_sandbox(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {
            "enabled": False, "engine": "docker", "image": DEFAULT_SANDBOX_IMAGE,
            "dockerfile": None, "build_context": ".", "language": None,
            "allow_network": False, "build_network": DEFAULT_BUILD_NETWORK, "pass_env": [],
        }
    if not isinstance(raw, dict):
        raise ValueError("sandbox must be an object")
    enabled = raw.get("enabled", False)
    engine = raw.get("engine", "docker")
    image = raw.get("image")
    dockerfile = _relative_path_setting("dockerfile", raw.get("dockerfile"))
    build_context = _relative_path_setting("build_context", raw.get("build_context"), default=".")
    language = raw.get("language")
    allow_network = raw.get("allow_network", False)
    build_network = raw.get("build_network", DEFAULT_BUILD_NETWORK)
    pass_env = raw.get("pass_env", [])
    if type(enabled) is not bool:
        raise ValueError("sandbox.enabled must be a boolean")
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(f"sandbox.engine must be one of: {', '.join(SUPPORTED_ENGINES)}")
    if image is None and not enabled:
        image = DEFAULT_SANDBOX_IMAGE
    if not isinstance(image, str) or not image.strip() or IMAGE_REFERENCE.fullmatch(image) is None:
        raise ValueError(
            "sandbox.image must be a nonempty Docker/Podman image reference "
            "containing no whitespace or option-like prefix"
        )
    if language is not None and language not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"sandbox.language must be one of: {', '.join(SUPPORTED_LANGUAGES)} or null"
        )
    if type(allow_network) is not bool:
        raise ValueError("sandbox.allow_network must be a boolean")
    if type(build_network) is not bool:
        raise ValueError("sandbox.build_network must be a boolean")
    if not isinstance(pass_env, list) or any(
        not isinstance(name, str) or not ENVIRONMENT_NAME.fullmatch(name) for name in pass_env
    ) or len(set(pass_env)) != len(pass_env):
        raise ValueError("sandbox.pass_env must be a list of unique environment variable names")
    return {
        "enabled": enabled,
        "engine": engine,
        "image": image,
        "dockerfile": dockerfile,
        "build_context": build_context,
        "language": language,
        "allow_network": allow_network,
        "build_network": build_network,
        "pass_env": list(pass_env),
    }


def _assert_no_redirecting_components(root: Path, relative: str, *, label: str) -> None:
    """Reject symlink/reparse components before resolving a repository path."""
    current = root
    for part in Path(relative).parts:
        if part in ("", "."):
            continue
        current /= part
        if _is_reparse_or_symbolic_link(current):
            raise ValueError(f"sandbox.{label} cannot contain symbolic links")


def _copy_build_context_file(
    source_root: Path, destination_root: Path, relative: str,
) -> int:
    """Copy one validated context file without following links or hard links."""
    descriptor, expected_identity = open_relative_regular(
        source_root, relative, os.O_RDONLY, description="sandbox build context file",
    )
    destination = destination_root / Path(*relative.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    try:
        with os.fdopen(descriptor, "rb") as source_stream:
            descriptor = -1
            metadata = revalidate_single_link_regular(
                source_stream.fileno(), expected_identity,
                description="sandbox build context file",
            )
            if metadata.st_size > MAX_BUILD_CONTEXT_FILE_BYTES:
                raise RuntimeError(f"Sandbox build context file is too large: {relative}")
            with destination.open("wb") as destination_stream:
                while True:
                    chunk = source_stream.read(64 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_BUILD_CONTEXT_FILE_BYTES:
                        raise RuntimeError(f"Sandbox build context file is too large: {relative}")
                    destination_stream.write(chunk)
            try:
                destination.chmod(metadata.st_mode & 0o777)
            except OSError:
                pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return copied


def _stage_sandbox_build_context(
    settings: dict[str, Any], repo: Path, destination: Path,
) -> tuple[Path, Path]:
    """Create a bounded build context and a separately staged Dockerfile."""
    policy = validate_sandbox(settings)
    dockerfile = policy.get("dockerfile")
    if not policy["enabled"] or not dockerfile:
        raise ValueError("A Dockerfile is required to stage a sandbox build")
    root = repo.resolve(strict=True)
    dockerfile_source = root / dockerfile
    context_source = root / policy["build_context"]
    _assert_no_redirecting_components(root, dockerfile, label="dockerfile")
    _assert_no_redirecting_components(root, policy["build_context"], label="build_context")
    dockerfile_path = dockerfile_source.resolve()
    context_path = context_source.resolve()
    for path, label in ((dockerfile_path, "sandbox.dockerfile"), (context_path, "sandbox.build_context")):
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} must stay within the repository") from exc
    if not dockerfile_path.is_file():
        raise ValueError(f"Configured sandbox Dockerfile does not exist: {dockerfile}")
    if not context_path.is_dir():
        raise ValueError(f"Configured sandbox build context is not a directory: {policy['build_context']}")
    context_relative = context_path.relative_to(root).as_posix()
    if context_relative == ".":
        context_relative = ""
    if context_relative and forbidden(context_relative):
        raise ValueError("sandbox.build_context is protected")
    dockerfile_relative = dockerfile_path.relative_to(root).as_posix()
    if forbidden(dockerfile_relative):
        raise ValueError("sandbox.dockerfile is protected")

    staged_context = destination / "context"
    staged_dockerfile = destination / "Dockerfile.agentic"
    staged_context.mkdir()
    staged_bytes = 0
    staged_files = 0
    try:
        for kind, relative, _ in iter_safe_regular_files(
            context_path,
            skip=lambda name: forbidden("/".join(part for part in (context_relative, name) if part)),
            fail_on_error=True,
            max_entries=MAX_BUILD_CONTEXT_ENTRIES,
            max_directories=MAX_BUILD_CONTEXT_DIRECTORIES,
        ):
            target = staged_context / Path(*relative.split("/"))
            if kind == "directory":
                target.mkdir(parents=True, exist_ok=True)
                continue
            if staged_files >= MAX_BUILD_CONTEXT_FILES:
                raise RuntimeError("Sandbox build context exceeded its file-count limit")
            copied = _copy_build_context_file(context_path, staged_context, relative)
            staged_files += 1
            staged_bytes += copied
            if staged_bytes > MAX_BUILD_CONTEXT_BYTES:
                raise RuntimeError("Sandbox build context exceeded its byte limit")
        dockerfile_source = destination / "dockerfile-source"
        dockerfile_source.mkdir()
        _copy_build_context_file(root, dockerfile_source, dockerfile_relative)
        shutil.copyfile(
            dockerfile_source / Path(*dockerfile_relative.split("/")),
            staged_dockerfile,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("Could not stage the complete sandbox build context") from exc
    return staged_dockerfile, staged_context


def sandbox_build_command(
    settings: dict[str, Any], repo: Path, *,
    dockerfile_path: Path, context_path: Path,
) -> list[str] | None:
    """Build a configured image from an already sanitized temporary context."""
    policy = validate_sandbox(settings)
    if not policy["enabled"] or not policy.get("dockerfile"):
        return None
    network = "default" if policy["build_network"] else "none"
    return [
        policy["engine"], "build", "--pull=false", f"--network={network}", "--tag", policy["image"],
        "--file", str(dockerfile_path), str(context_path),
    ]


@contextmanager
def sandbox_build_context(settings: dict[str, Any], repo: Path):
    """Yield a build command whose context is removed after the build."""
    with tempfile.TemporaryDirectory(prefix="agentic-sandbox-build-") as temporary:
        staged_dockerfile, staged_context = _stage_sandbox_build_context(
            settings, repo, Path(temporary),
        )
        build_command = sandbox_build_command(
            settings, repo,
            dockerfile_path=staged_dockerfile,
            context_path=staged_context,
        )
        if build_command is None:
            raise RuntimeError("A sandbox build command could not be created")
        yield build_command


def _authoritative_git_common_dir(root: Path) -> Path | None:
    """Ask Git which common directory belongs to this checkout."""
    try:
        result = subprocess.run(
            hardened_git_argv(["git", "-C", str(root), "rev-parse", "--git-common-dir"]),
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            env=hardened_git_environment(),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _git_metadata(root: Path) -> tuple[Path, Path | None] | None:
    """Locate safe Git metadata mounts for a normal or linked worktree."""
    marker = root / ".git"
    try:
        # Resolving a symlink here would turn an arbitrary host path into a
        # container mount.  A linked-worktree marker is a regular file and is
        # handled below; directory-style metadata must never be a symlink.
        if _is_reparse_or_symbolic_link(marker):
            return None
        if marker.is_dir():
            return marker, None
        if not marker.is_file():
            return None
        first_line = marker.read_text(encoding="utf-8").splitlines()[0]
        prefix = "gitdir: "
        if not first_line.startswith(prefix):
            return None
        git_dir = Path(first_line[len(prefix):])
        if not git_dir.is_absolute():
            git_dir = marker.parent / git_dir
        git_dir = git_dir.resolve()
        common_dir = git_dir
        commondir = git_dir / "commondir"
        if not commondir.is_file():
            return None
        common_value = commondir.read_text(encoding="utf-8").strip()
        common_dir = (git_dir / common_value).resolve()
        if not git_dir.is_dir() or not common_dir.is_dir():
            return None
        # Never turn a model-controlled .git pointer into an arbitrary host mount.
        if (
            common_dir.name.lower() != ".git"
            or git_dir.parent != common_dir / "worktrees"
        ):
            return None
        linked_marker = git_dir / "gitdir"
        if not linked_marker.is_file():
            return None
        linked_path = Path(linked_marker.read_text(encoding="utf-8").splitlines()[0])
        if not linked_path.is_absolute():
            linked_path = linked_marker.parent / linked_path
        if linked_path.resolve() != marker.resolve():
            return None
        authoritative_common_dir = _authoritative_git_common_dir(root)
        if authoritative_common_dir != common_dir:
            return None
        return git_dir, common_dir
    except (OSError, UnicodeError, IndexError, ValueError):
        return None


def _is_absolute_argument(value: str) -> bool:
    """Return whether an argv value is an absolute path on either platform."""
    return (
        Path(value).is_absolute()
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
    )


def command_uses_git_metadata(argv: list[str]) -> bool:
    """Return whether an argv command directly invokes Git.

    Validation commands are often test runners whose child processes create
    temporary repositories.  Injecting the selected worktree's Git
    environment into those commands redirects the child Git operations to the
    selected worktree.  Direct Git inspection still needs the mounted metadata.
    """
    return bool(argv) and Path(argv[0]).stem.casefold() == "git"


def sandbox_command(
    argv: list[str], cwd: Path, settings: dict[str, Any] | None,
    *, output_root: Path | None = None, read_only: bool = False,
    pass_env: list[str] | None = None, restricted: bool = False,
    preserve_git_metadata: bool = True,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Build a hardened container command while preserving argv semantics."""
    policy = validate_sandbox(settings)
    if not policy["enabled"]:
        return list(argv)
    root = cwd.resolve()
    command = [
        policy["engine"], "run", "--rm", "--init",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit", "256", "--memory", "2g", "--cpus", "2",
        "--read-only",
        # Docker's tmpfs defaults can include noexec.  Temporary provider
        # fixtures and provider helpers may need to resolve executable files,
        # so make the intended execution policy explicit while retaining the
        # read-only root filesystem and dropped capabilities.
        "--tmpfs", "/tmp:rw,nosuid,exec,size=256m",
        # Read-only is a filesystem/provider-capability policy. It must not
        # implicitly disable provider API access: native reviewers may need
        # outbound networking even though their worktree remains read-only.
        "--network", "none" if restricted else "bridge" if policy["allow_network"] else "none",
        "-v", f"{root}:/workspace:{'ro' if read_only else 'rw'}", "-w", "/workspace",
    ]
    # Rootless/containerized engines may map container root to an unmapped host
    # identity, which makes a writable bind mount appear read-only. Match the
    # invoking user's identity so developer mounts remain writable without
    # granting the process root privileges in the container.
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is not None and getgid is not None:
        command[2:2] = ["--user", f"{getuid()}:{getgid()}"]
    replacements: list[tuple[Path, str]] = [(root, "/workspace")]
    git_metadata = _git_metadata(root)
    if git_metadata and preserve_git_metadata:
        git_dir, common_dir = git_metadata
        if common_dir is None:
            command.extend(["-v", f"{git_dir}:/agentic-git:ro"])
            command.extend(["--env", "GIT_DIR=/agentic-git"])
        else:
            command.extend(["-v", f"{common_dir}:/agentic-git-common:ro"])
            # Developers may write the worktree, but Git metadata remains
            # immutable so provider processes cannot create commits, update
            # refs, or alter the linked-worktree index inside the container.
            command.extend([
                # Keep the linked-worktree admin directory at the same
                # relative location below the common Git directory. Git uses
                # the commondir file to resolve refs and worktree-specific
                # HEAD/index state; mounting it at an unrelated path makes
                # Git treat the checkout as an empty repository in a
                # container, even when GIT_COMMON_DIR is set explicitly.
                "-v", f"{git_dir}:/agentic-git-common/worktrees/agentic:ro",
            ])
            command.extend([
                "--env", "GIT_DIR=/agentic-git-common/worktrees/agentic",
            ])
        command.extend(["--env", "GIT_WORK_TREE=/workspace"])
        # The complete worktree bind mount is writable for developers, so
        # overlay the checkout's .git entry read-only as well. This is needed
        # for normal directory-style checkouts, not only linked worktrees.
        # Otherwise a provider can write refs, hooks, or the index through
        # /workspace/.git despite the separate metadata mount above.
        marker = root / ".git"
        if not _is_reparse_or_symbolic_link(marker) and (marker.is_file() or marker.is_dir()):
            command.extend(["-v", f"{marker}:/workspace/.git:ro"])
    elif not preserve_git_metadata:
        # A linked worktree exposes a .git pointer whose host-side target is
        # not mounted in validation containers.  Leaving that pointer visible
        # makes unrelated Git commands in test fixtures try to discover the
        # task repository and fail before they can use their own temp config or
        # repository.  Hide the pointer while keeping nested temp repositories
        # available to the validation process.
        marker = root / ".git"
        if not _is_reparse_or_symbolic_link(marker) and marker.is_file():
            command.extend(["-v", f"{os.devnull}:/workspace/.git:ro"])
        elif not _is_reparse_or_symbolic_link(marker) and marker.is_dir():
            command.extend(["--tmpfs", "/workspace/.git:rw,nosuid,nodev,noexec,size=1m"])
    if output_root is not None:
        output = output_root.resolve()
        if output != root and output.is_dir():
            command.extend(["-v", f"{output}:/agentic-output:rw"])
            replacements.append((output, "/agentic-output"))
    source_env = os.environ if env is None else env
    git_command = command_uses_git_metadata(argv)
    for name in policy["pass_env"] if pass_env is None else pass_env:
        if git_command and is_blocked_git_environment_name(name):
            continue
        if name in source_env:
            command.extend(["--env", name])
    if git_command:
        for name, value in GIT_SAFETY_ENVIRONMENT:
            command.extend(["--env", f"{name}={value}"])
    # The sandbox normally runs as the invoking non-root UID.  Minimal images
    # often only define /root, which leaves Git and other tools with an
    # unwritable or nonexistent home when Docker's --user flag is applied.
    # Keep all user-level state ephemeral and inside the writable tmpfs.
    command.extend(["--env", "HOME=/tmp"])
    rendered_argv: list[str] = []
    for part in argv:
        replaced = part
        # Relative executable names, flags, module names, and inline program
        # text must remain argv data. Only absolute host paths can refer to a
        # mounted worktree or invocation output directory.
        if not _is_absolute_argument(part):
            rendered_argv.append(part)
            continue
        for source, destination in replacements:
            source_text = os.fspath(source)
            normalized_source = os.path.normcase(os.path.realpath(os.path.normpath(source_text)))
            normalized_part = os.path.normcase(os.path.realpath(os.path.normpath(replaced)))
            if normalized_part == normalized_source:
                replaced = destination
                break
            prefix = normalized_source + os.sep
            if normalized_part.startswith(prefix):
                replaced = destination + normalized_part[len(normalized_source):].replace(os.sep, "/")
                break
        rendered_argv.append(replaced)
    if rendered_argv:
        # The sandbox is an argv runner; do not let an image entrypoint replace
        # the executable selected by Agentic.
        command.extend(["--entrypoint", rendered_argv[0]])
    command.append(policy["image"])
    command.extend(rendered_argv[1:])
    return command


def sandbox_executable(engine: str) -> str:
    return engine
