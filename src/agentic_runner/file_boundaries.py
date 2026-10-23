"""Single-link regular-file checks for worktree/provider boundaries."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Callable, Iterator


class SafeTraversalUnavailable(RuntimeError):
    """The platform cannot provide an anchored, no-follow path traversal."""


def _identity(metadata: Any) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _validate_single_link_regular(metadata: Any, *, description: str) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{description} must be regular files")
    if getattr(metadata, "st_nlink", 1) != 1:
        raise ValueError(f"hard-linked {description} files are not allowed")


def _supports_anchored_traversal() -> bool:
    return (
        os.name != "nt"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in getattr(os, "supports_dir_fd", set())
    )


def _is_reparse_or_symbolic_link(path: Path) -> bool:
    """Return whether a path component redirects through another object."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _parts(relative: str | Path) -> list[str]:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be relative and stay inside the trusted root")
    return [part for part in path.parts if part not in {"", "."}]


def _open_directory_at(descriptor: int, name: str) -> int:
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=descriptor,
    )


def _open_root_directory(root: Path) -> int:
    if not _supports_anchored_traversal():
        raise SafeTraversalUnavailable(
            "this platform cannot safely traverse worktree paths"
        )
    try:
        return os.open(
            str(root.resolve(strict=True)),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise SafeTraversalUnavailable("trusted root could not be opened safely") from exc


def _walk_to_parent(root: Path, parts: list[str], *, create: bool) -> tuple[int, str]:
    root_descriptor = _open_root_directory(root)
    current = root_descriptor
    try:
        for part in parts[:-1]:
            try:
                child = _open_directory_at(current, part)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=current)
                child = _open_directory_at(current, part)
            if current != root_descriptor:
                os.close(current)
            current = child
        return current, parts[-1]
    except Exception:
        os.close(current)
        raise


def _validate_windows_path_components(
    root: Path, parts: list[str], *, create: bool, description: str,
) -> Path:
    """Validate a Windows path when POSIX ``dir_fd`` traversal is unavailable.

    Windows does not expose Python's ``dir_fd``/``O_NOFOLLOW`` primitives.
    Keep the existing safe behavior for ordinary Windows worktrees by
    validating every component, rejecting reparse points, and checking the
    resolved target before and after opening it.  The descriptor identity
    check in ``open_single_link_regular`` still rejects a final-component
    replacement between the metadata check and ``os.open``.
    """
    root = root.resolve(strict=True)
    if not root.is_dir() or _is_reparse_or_symbolic_link(root):
        raise ValueError(f"{description} root must be a real directory")
    current = root
    for index, part in enumerate(parts):
        current /= part
        is_final = index == len(parts) - 1
        if _is_reparse_or_symbolic_link(current):
            raise ValueError(f"symbolic links are not allowed for {description}")
        if current.exists():
            if not is_final and not current.is_dir():
                raise NotADirectoryError(str(current))
            continue
        if not create:
            raise FileNotFoundError(str(current))
        if not is_final:
            current.mkdir()
    target = root.joinpath(*parts)
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError(f"{description} must stay inside the worktree")
    return target


def _open_relative_regular_without_dir_fd(
    root: Path,
    parts: list[str],
    flags: int,
    mode: int,
    *,
    create_parents: bool,
    description: str,
) -> tuple[int, tuple[int, int]]:
    """Use the validated Windows path fallback for regular-file operations."""
    target = _validate_windows_path_components(
        root, parts, create=create_parents, description=description,
    )
    descriptor, expected_identity = open_single_link_regular(
        target, flags | getattr(os, "O_NONBLOCK", 0), mode,
        description=description,
    )
    try:
        resolved = target.resolve(strict=False)
        if not resolved.is_relative_to(root.resolve(strict=True)):
            raise ValueError(f"{description} must stay inside the worktree")
        return descriptor, expected_identity
    except Exception:
        os.close(descriptor)
        raise


def _iter_safe_regular_files_without_dir_fd(
    root: Path,
    parts: list[str],
    *,
    skip: Callable[[str], bool] | None,
    max_entries: int | None,
    max_directories: int | None,
) -> Iterator[tuple[str, str, os.stat_result]]:
    """Walk regular files on Windows without following directory links."""
    target = _validate_windows_path_components(
        root, parts, create=False, description="directory",
    )
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    stack: list[tuple[Path, str]] = [(target, "/".join(parts))]
    entries_seen = 0
    directories_seen = 0
    while stack:
        directory, directory_relative = stack.pop()
        directories_seen += 1
        if max_directories is not None and directories_seen > max_directories:
            raise SafeTraversalUnavailable("directory listing exceeded its safety bound")
        directory_metadata = directory.stat()
        yield ("directory", directory_relative, directory_metadata)
        children: list[tuple[Path, str, os.stat_result]] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                entries_seen += 1
                if max_entries is not None and entries_seen > max_entries:
                    raise SafeTraversalUnavailable("directory listing exceeded its safety bound")
                child_relative = "/".join(
                    item for item in (directory_relative, entry.name) if item
                )
                if skip is not None and skip(child_relative):
                    continue
                child = Path(entry.path)
                if _is_reparse_or_symbolic_link(child):
                    continue
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                children.append((child, child_relative, metadata))
        children.sort(key=lambda item: item[1])
        subdirectories: list[tuple[Path, str]] = []
        for child, child_relative, metadata in children:
            if stat.S_ISDIR(metadata.st_mode):
                subdirectories.append((child, child_relative))
            elif stat.S_ISREG(metadata.st_mode):
                yield ("file", child_relative, metadata)
        stack.extend(reversed(subdirectories))


def open_relative_regular(
    root: Path,
    relative: str | Path,
    flags: int,
    mode: int = 0o644,
    *,
    create_parents: bool = False,
    description: str = "file",
) -> tuple[int, tuple[int, int]]:
    """Open a regular file by walking every component from an anchored root.

    On POSIX this keeps parent directory descriptors open while the final
    component is selected, so replacing a validated parent cannot redirect the
    operation outside the worktree.  The caller owns the returned descriptor.
    """
    parts = _parts(relative)
    if not parts:
        raise ValueError(f"{description} must name a file")
    if not _supports_anchored_traversal():
        return _open_relative_regular_without_dir_fd(
            root, parts, flags, mode, create_parents=create_parents,
            description=description,
        )
    parent, name = _walk_to_parent(root, parts, create=create_parents)
    try:
        open_flags = flags | os.O_NOFOLLOW
        # A special file must not be able to block the boundary check.  This
        # is harmless for regular files and is available on the POSIX
        # platforms where anchored traversal is supported.
        open_flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(name, open_flags, mode, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        metadata = os.fstat(descriptor)
        _validate_single_link_regular(metadata, description=description)
        return descriptor, _identity(metadata)
    except Exception:
        os.close(descriptor)
        raise


def iter_safe_regular_files(
    root: Path,
    relative: str | Path = ".",
    *,
    skip: Callable[[str], bool] | None = None,
    max_entries: int | None = None,
    max_directories: int | None = None,
) -> Iterator[tuple[str, str, os.stat_result]]:
    """Yield ``(kind, relative_path, metadata)`` without raced parents."""
    parts = _parts(relative)
    if not _supports_anchored_traversal():
        yield from _iter_safe_regular_files_without_dir_fd(
            root, parts, skip=skip, max_entries=max_entries,
            max_directories=max_directories,
        )
        return
    root_descriptor = _open_root_directory(root)
    current = root_descriptor
    stack: list[tuple[int, str]] = []
    entries_seen = 0
    directories_seen = 0
    try:
        for part in parts:
            child = _open_directory_at(current, part)
            if current != root_descriptor:
                os.close(current)
            current = child
        stack = [(current, "/".join(parts))]
        current = -1
        while stack:
            directory, directory_relative = stack.pop()
            try:
                directories_seen += 1
                if max_directories is not None and directories_seen > max_directories:
                    raise SafeTraversalUnavailable("directory listing exceeded its safety bound")
                metadata = os.fstat(directory)
                yield ("directory", directory_relative, metadata)
                with os.scandir(directory) as entries:
                    children = []
                    for entry in entries:
                        entries_seen += 1
                        if max_entries is not None and entries_seen > max_entries:
                            raise SafeTraversalUnavailable("directory listing exceeded its safety bound")
                        children.append(entry)
                    children.sort(key=lambda entry: entry.name)
                    subdirectories: list[tuple[str, int]] = []
                    for entry in children:
                        child_relative = "/".join(
                            item for item in (directory_relative, entry.name) if item
                        )
                        if skip is not None and skip(child_relative):
                            continue
                        try:
                            child_metadata = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISDIR(child_metadata.st_mode):
                            try:
                                child_descriptor = _open_directory_at(directory, entry.name)
                            except OSError:
                                continue
                            subdirectories.append((child_relative, child_descriptor))
                        elif stat.S_ISREG(child_metadata.st_mode):
                            yield ("file", child_relative, child_metadata)
                    for child_relative, child_descriptor in reversed(subdirectories):
                        stack.append((child_descriptor, child_relative))
            finally:
                os.close(directory)
    finally:
        if current >= 0:
            os.close(current)
        for descriptor, _ in stack:
            os.close(descriptor)


def revalidate_single_link_regular(
    descriptor: int,
    expected_identity: tuple[int, int],
    *,
    description: str = "file",
) -> os.stat_result:
    """Recheck an open descriptor immediately before using its contents."""
    metadata = os.fstat(descriptor)
    _validate_single_link_regular(metadata, description=description)
    if _identity(metadata) != expected_identity:
        raise ValueError(f"{description} changed while it was being opened")
    return metadata


def open_single_link_regular(
    path: Path,
    flags: int,
    mode: int = 0o644,
    *,
    description: str = "file",
) -> tuple[int, tuple[int, int]]:
    """Open a regular file only when its path and descriptor identify one inode.

    The caller owns the returned descriptor. ``O_CREAT`` is supported for
    safe creation of new worktree files; the descriptor is still rejected if
    an existing hard-linked target is selected by a race.
    """
    try:
        path_metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        path_metadata = None
    if path_metadata is not None:
        _validate_single_link_regular(path_metadata, description=description)

    open_flags = flags | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, open_flags, mode)
    try:
        descriptor_metadata = os.fstat(descriptor)
        _validate_single_link_regular(descriptor_metadata, description=description)
        if (
            path_metadata is not None
            and _identity(descriptor_metadata) != _identity(path_metadata)
        ):
            raise ValueError(f"{description} changed while it was being opened")
        expected_identity = _identity(descriptor_metadata)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, expected_identity
