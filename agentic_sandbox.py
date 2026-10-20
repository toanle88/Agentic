"""Optional container command isolation for provider and test processes."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any


SUPPORTED_ENGINES = ("docker", "podman")
SUPPORTED_LANGUAGES = ("python", "node", "go", "rust", "java", "dotnet")
DEFAULT_SANDBOX_IMAGE = "agentic-runner:latest"
DEFAULT_BUILD_NETWORK = True
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
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
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


def sandbox_build_command(settings: dict[str, Any], repo: Path) -> list[str] | None:
    """Build a configured image using only paths inside the target repository."""
    policy = validate_sandbox(settings)
    dockerfile = policy.get("dockerfile")
    if not policy["enabled"] or not dockerfile:
        return None
    root = repo.resolve()
    dockerfile_path = (root / dockerfile).resolve()
    context_path = (root / policy["build_context"]).resolve()
    for path, label in ((dockerfile_path, "sandbox.dockerfile"), (context_path, "sandbox.build_context")):
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} must stay within the repository") from exc
    if not dockerfile_path.is_file():
        raise ValueError(f"Configured sandbox Dockerfile does not exist: {dockerfile}")
    if not context_path.is_dir():
        raise ValueError(f"Configured sandbox build context is not a directory: {policy['build_context']}")
    network = "default" if policy["build_network"] else "none"
    return [
        policy["engine"], "build", "--pull=false", f"--network={network}", "--tag", policy["image"],
        "--file", str(dockerfile_path), str(context_path),
    ]


def _git_metadata(root: Path) -> tuple[Path, Path | None] | None:
    """Locate safe Git metadata mounts for a normal or linked worktree."""
    marker = root / ".git"
    try:
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
        return git_dir, common_dir
    except (OSError, UnicodeError, IndexError, ValueError):
        return None


def sandbox_command(
    argv: list[str], cwd: Path, settings: dict[str, Any] | None,
    *, output_root: Path | None = None, read_only: bool = False,
    pass_env: list[str] | None = None, restricted: bool = False,
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
        "--tmpfs", "/tmp:rw,nosuid,size=256m",
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
    if git_metadata:
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
                "-v", f"{git_dir}:/agentic-git-worktree:ro",
            ])
            command.extend([
                "--env", "GIT_DIR=/agentic-git-worktree",
                "--env", "GIT_COMMON_DIR=/agentic-git-common",
            ])
        command.extend(["--env", "GIT_WORK_TREE=/workspace"])
        if restricted:
            # A linked worktree's .git file is part of the writable worktree
            # bind mount. Protect the marker itself too: otherwise a provider
            # could replace it, run ``git init``, and create writable metadata
            # outside the read-only mounts above.
            marker = root / ".git"
            if not marker.is_symlink() and (marker.is_file() or marker.is_dir()):
                command.extend(["-v", f"{marker}:/workspace/.git:ro"])
    if output_root is not None:
        output = output_root.resolve()
        if output != root and output.is_dir():
            command.extend(["-v", f"{output}:/agentic-output:rw"])
            replacements.append((output, "/agentic-output"))
    for name in policy["pass_env"] if pass_env is None else pass_env:
        if name in os.environ:
            command.extend(["--env", name])
    rendered_argv: list[str] = []
    for part in argv:
        replaced = part
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
