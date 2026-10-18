"""Optional container command isolation for provider and test processes."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any


SUPPORTED_ENGINES = ("docker", "podman")
DEFAULT_SANDBOX_IMAGE = "agentic-runner:latest"
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_sandbox(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {
            "enabled": False, "engine": "docker", "image": DEFAULT_SANDBOX_IMAGE,
            "allow_network": False, "pass_env": [],
        }
    if not isinstance(raw, dict):
        raise ValueError("sandbox must be an object")
    enabled = raw.get("enabled", False)
    engine = raw.get("engine", "docker")
    image = raw.get("image")
    allow_network = raw.get("allow_network", False)
    pass_env = raw.get("pass_env", [])
    if type(enabled) is not bool:
        raise ValueError("sandbox.enabled must be a boolean")
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(f"sandbox.engine must be one of: {', '.join(SUPPORTED_ENGINES)}")
    if image is None and not enabled:
        image = DEFAULT_SANDBOX_IMAGE
    if not isinstance(image, str) or not image.strip() or any(char in image for char in "\r\n"):
        raise ValueError("sandbox.image must be a nonempty single-line string when sandbox.enabled is true")
    if type(allow_network) is not bool:
        raise ValueError("sandbox.allow_network must be a boolean")
    if not isinstance(pass_env, list) or any(
        not isinstance(name, str) or not ENVIRONMENT_NAME.fullmatch(name) for name in pass_env
    ) or len(set(pass_env)) != len(pass_env):
        raise ValueError("sandbox.pass_env must be a list of unique environment variable names")
    return {
        "enabled": enabled,
        "engine": engine,
        "image": image,
        "allow_network": allow_network,
        "pass_env": list(pass_env),
    }


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
    pass_env: list[str] | None = None,
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
        "--network", "bridge" if policy["allow_network"] else "none",
        "-v", f"{root}:/workspace:{'ro' if read_only else 'rw'}", "-w", "/workspace",
    ]
    replacements: list[tuple[Path, str]] = [(root, "/workspace")]
    git_metadata = _git_metadata(root)
    if git_metadata:
        git_dir, common_dir = git_metadata
        if common_dir is None:
            command.extend(["-v", f"{git_dir}:/agentic-git:ro"])
            command.extend(["--env", "GIT_DIR=/agentic-git"])
        else:
            command.extend(["-v", f"{common_dir}:/agentic-git-common:ro"])
            command.extend([
                "-v", f"{git_dir}:/agentic-git-worktree:{'ro' if read_only else 'rw'}",
            ])
            command.extend([
                "--env", "GIT_DIR=/agentic-git-worktree",
                "--env", "GIT_COMMON_DIR=/agentic-git-common",
            ])
        command.extend(["--env", "GIT_WORK_TREE=/workspace"])
    if output_root is not None:
        output = output_root.resolve()
        if output != root and output.is_dir():
            command.extend(["-v", f"{output}:/agentic-output:rw"])
            replacements.append((output, "/agentic-output"))
    for name in policy["pass_env"] if pass_env is None else pass_env:
        if name in os.environ:
            command.extend(["--env", name])
    command.append(policy["image"])
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
        command.append(replaced)
    return command


def sandbox_executable(engine: str) -> str:
    return engine
