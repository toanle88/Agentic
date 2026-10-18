"""Optional container command isolation for provider and test processes."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


SUPPORTED_ENGINES = ("docker", "podman")
DEFAULT_SANDBOX_IMAGE = "agentic-runner:latest"


def validate_sandbox(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"enabled": False, "engine": "docker", "image": DEFAULT_SANDBOX_IMAGE, "allow_network": False}
    if not isinstance(raw, dict):
        raise ValueError("sandbox must be an object")
    enabled = raw.get("enabled", False)
    engine = raw.get("engine", "docker")
    image = raw.get("image", DEFAULT_SANDBOX_IMAGE)
    allow_network = raw.get("allow_network", False)
    if type(enabled) is not bool:
        raise ValueError("sandbox.enabled must be a boolean")
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(f"sandbox.engine must be one of: {', '.join(SUPPORTED_ENGINES)}")
    if not isinstance(image, str) or not image.strip() or any(char in image for char in "\r\n"):
        raise ValueError("sandbox.image must be a nonempty single-line string")
    if type(allow_network) is not bool:
        raise ValueError("sandbox.allow_network must be a boolean")
    return {
        "enabled": enabled,
        "engine": engine,
        "image": image,
        "allow_network": allow_network,
    }


def sandbox_command(
    argv: list[str], cwd: Path, settings: dict[str, Any] | None,
    *, output_root: Path | None = None, read_only: bool = False,
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
        "--network", "bridge" if policy["allow_network"] else "none",
        "-v", f"{root}:/workspace:{'ro' if read_only else 'rw'}", "-w", "/workspace",
    ]
    replacements: list[tuple[Path, str]] = [(root, "/workspace")]
    if output_root is not None:
        output = output_root.resolve()
        if output != root and output.is_dir():
            command.extend(["-v", f"{output}:/agentic-output:rw"])
            replacements.append((output, "/agentic-output"))
    command.append(policy["image"])
    for part in argv:
        replaced = part
        for source, destination in replacements:
            source_text = os.fspath(source)
            if replaced == source_text:
                replaced = destination
                break
            prefix = source_text + os.sep
            if replaced.startswith(prefix):
                replaced = destination + replaced[len(source_text):].replace(os.sep, "/")
                break
        command.append(replaced)
    return command


def sandbox_executable(engine: str) -> str:
    return engine
