"""Crash-safe helpers for Agentic's durable state files.

Run metadata is read by later invocations and by the dashboard while a job may
still be running.  Replacing a complete temporary file keeps readers from
observing half-written JSON after a process interruption.
"""
from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Iterable

from .file_boundaries import atomic_replace_relative_regular, open_single_link_regular
from .security import redact_secrets


def _validate_log_descriptor(descriptor: int, path: Path) -> os.stat_result:
    """Validate an already-open durable log without a path/open race."""
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"durable log is not a regular file: {path}")
    if metadata.st_nlink != 1:
        raise OSError(f"durable log must not be hard-linked: {path}")
    return metadata


def _validate_log_parent(path: Path) -> None:
    """Reject symlinked components in the directory used for a durable log."""
    parent = path.parent
    if parent.is_symlink():
        raise OSError(f"durable log parent is a symbolic link: {parent}")
    while not parent.exists():
        next_parent = parent.parent
        if next_parent == parent:
            break
        parent = next_parent
    while True:
        if parent.is_symlink():
            raise OSError(f"durable log parent is a symbolic link: {parent}")
        next_parent = parent.parent
        if next_parent == parent:
            break
        parent = next_parent


def _supports_anchored_log_open() -> bool:
    return (
        os.name != "nt"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in getattr(os, "supports_dir_fd", set())
        and os.mkdir in getattr(os, "supports_dir_fd", set())
    )


def _open_log_directory_at(parent: int, name: str) -> int:
    """Open one directory component without following a replacement link."""
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent,
    )


def _open_log_parent(path: Path) -> tuple[int, str]:
    """Return an anchored parent descriptor and final filename on POSIX."""
    absolute = Path(os.path.abspath(path))
    parts = absolute.parts
    if not parts or parts[-1] in {"", os.path.sep}:
        raise ValueError(f"durable log path must name a file: {path}")
    descriptor = os.open(
        os.path.sep,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        for component in parts[1:-1]:
            try:
                child = _open_log_directory_at(descriptor, component)
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = _open_log_directory_at(descriptor, component)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _open_log_descriptor_legacy(
    path: Path, *, truncate: bool, readable: bool = False,
) -> int:
    """Open a log on platforms without an ``openat``-style stdlib API."""
    _validate_log_parent(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_log_parent(path)
    parent_metadata = os.stat(path.parent, follow_symlinks=False)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    append = 0 if truncate else getattr(os, "O_APPEND", 0)
    binary = getattr(os, "O_BINARY", 0)
    access = os.O_RDWR if readable else os.O_WRONLY
    base_flags = access | getattr(os, "O_NONBLOCK", 0) | nofollow | append | binary
    try:
        descriptor = os.open(path, base_flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = os.open(path, base_flags)
    try:
        _validate_log_parent(path)
        current_parent = os.stat(path.parent, follow_symlinks=False)
        if (parent_metadata.st_dev, parent_metadata.st_ino) != (
            current_parent.st_dev, current_parent.st_ino,
        ):
            raise OSError(f"durable log parent changed during open: {path.parent}")
        metadata = _validate_log_descriptor(descriptor, path)
        target_metadata = os.stat(path, follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != (
            target_metadata.st_dev, target_metadata.st_ino,
        ):
            raise OSError(f"durable log target changed during open: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_log_descriptor(
    path: Path, *, truncate: bool, readable: bool = False,
) -> int:
    """Open a single-link regular log, rejecting replaceable final symlinks.

    Creation uses ``O_EXCL`` and existing files use ``O_NOFOLLOW`` where the
    platform provides it.  The returned descriptor is the object that is
    subsequently written; callers never reopen the predictable pathname.
    """
    parent_descriptor = -1
    if _supports_anchored_log_open():
        parent_descriptor, name = _open_log_parent(path)
        nofollow = os.O_NOFOLLOW
        append = 0 if truncate else getattr(os, "O_APPEND", 0)
        binary = getattr(os, "O_BINARY", 0)
        access = os.O_RDWR if readable else os.O_WRONLY
        base_flags = access | getattr(os, "O_NONBLOCK", 0) | nofollow | append | binary
        try:
            try:
                descriptor = os.open(
                    name, base_flags | os.O_CREAT | os.O_EXCL, 0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                descriptor = os.open(name, base_flags, dir_fd=parent_descriptor)
        finally:
            os.close(parent_descriptor)
            parent_descriptor = -1
    else:
        descriptor = _open_log_descriptor_legacy(
            path, truncate=truncate, readable=readable,
        )
    try:
        _validate_log_descriptor(descriptor, path)
        if truncate:
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
        else:
            os.lseek(descriptor, 0, os.SEEK_END)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def rotate_bounded_log(path: Path, backups: int) -> None:
    """Rotate a log using an anchored parent directory when available."""
    if backups < 1:
        return
    if _supports_anchored_log_open():
        parent_descriptor, name = _open_log_parent(path)
        try:
            oldest = f"{name}.{backups}"
            try:
                os.unlink(oldest, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            for index in range(backups - 1, 0, -1):
                try:
                    os.replace(
                        f"{name}.{index}", f"{name}.{index + 1}",
                        src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
                    )
                except FileNotFoundError:
                    pass
            try:
                os.replace(
                    name, f"{name}.1",
                    src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
                )
            except FileNotFoundError:
                pass
        finally:
            os.close(parent_descriptor)
        return

    # Older platforms lack the directory-descriptor operations above. Keep
    # the compatibility path conservative: validate the parent before and
    # after the short rotation sequence and refuse a changed directory.
    _validate_log_parent(path)
    try:
        before = os.stat(path.parent, follow_symlinks=False)
    except OSError:
        before = None
    oldest_path = path.with_name(f"{path.name}.{backups}")
    try:
        oldest_path.unlink()
    except FileNotFoundError:
        pass
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        target = path.with_name(f"{path.name}.{index + 1}")
        if source.exists():
            os.replace(source, target)
    if path.exists():
        os.replace(path, path.with_name(f"{path.name}.1"))
    _validate_log_parent(path)
    if before is not None:
        after = os.stat(path.parent, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise OSError(f"durable log parent changed during rotation: {path.parent}")


class BoundedAppendWriter:
    """Write one bounded, redacted durable log through one safe descriptor.

    ``retain_suffix`` preserves the shell's historical tail behavior. The
    default retains a prefix and writes one explicit marker when the byte
    budget is reached. The descriptor remains owned by this object for the
    entire invocation, so later appends cannot follow a replaced pathname.
    """

    def __init__(self, path: Path, *, max_bytes: int, truncation_marker: str = "",
                 retain_suffix: bool = False, redaction_values: Iterable[str] = (),
                 encoding: str = "utf-8", truncate: bool = True) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        self.path = path
        self.max_bytes = max_bytes
        self.retain_suffix = retain_suffix
        self.encoding = encoding
        self._redaction_values = tuple(redaction_values)
        self._marker = redact_secrets(
            truncation_marker, self._redaction_values,
        ).encode(encoding)
        self._retained = bytearray()
        self._truncated = False
        self._closed = False
        self._descriptor = _open_log_descriptor(
            path, truncate=truncate, readable=retain_suffix and not truncate,
        )
        try:
            self._written_bytes = os.fstat(self._descriptor).st_size
            if self.retain_suffix and not truncate:
                capacity = max(0, self.max_bytes - len(self._marker))
                if capacity:
                    start = max(0, self._written_bytes - capacity)
                    os.lseek(self._descriptor, start, os.SEEK_SET)
                    remaining = min(capacity, self._written_bytes - start)
                    while remaining:
                        chunk = os.read(self._descriptor, remaining)
                        if not chunk:
                            break
                        self._retained.extend(chunk)
                        remaining -= len(chunk)
                    os.lseek(self._descriptor, 0, os.SEEK_END)
        except BaseException:
            os.close(self._descriptor)
            self._closed = True
            raise

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def written_bytes(self) -> int:
        return self._written_bytes

    def _redact(self, data: bytes | str) -> bytes:
        if isinstance(data, bytes):
            text = data.decode(self.encoding, errors="replace")
        else:
            text = data
        return redact_secrets(text, self._redaction_values).encode(self.encoding)

    def _write_all(self, payload: bytes) -> None:
        _validate_log_descriptor(self._descriptor, self.path)
        view = memoryview(payload)
        while view:
            written = os.write(self._descriptor, view)
            if written <= 0:
                raise OSError("durable log write made no progress")
            view = view[written:]
        _validate_log_descriptor(self._descriptor, self.path)

    def _rewrite_suffix(self) -> None:
        payload = bytes(self._retained)
        if self._truncated:
            payload = self._marker + payload
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        os.ftruncate(self._descriptor, 0)
        self._write_all(payload[:self.max_bytes])
        self._written_bytes = min(len(payload), self.max_bytes)

    def write(self, data: bytes | str) -> int:
        """Redact and write *data*, returning its original byte length."""
        if self._closed:
            raise ValueError("I/O operation on closed durable log")
        payload = self._redact(data)
        original_length = len(data.encode(self.encoding) if isinstance(data, str) else data)
        if not payload:
            return original_length
        if self.retain_suffix:
            if not self._truncated and self._written_bytes + len(payload) <= self.max_bytes:
                self._write_all(payload)
                self._written_bytes += len(payload)
                self._retained.extend(payload)
                return original_length
            self._truncated = True
            capacity = max(0, self.max_bytes - len(self._marker))
            self._retained = bytearray((bytes(self._retained) + payload)[-capacity:])
            self._rewrite_suffix()
            return original_length
        if self._truncated:
            return original_length
        available = self.max_bytes - len(self._marker) - self._written_bytes
        if len(payload) <= max(0, available):
            self._write_all(payload)
            self._written_bytes += len(payload)
            return original_length
        if self._written_bytes + len(self._marker) <= self.max_bytes:
            self._write_all(self._marker)
            self._written_bytes += len(self._marker)
        self._truncated = True
        return original_length

    def flush(self, *, sync: bool = False) -> None:
        if self._closed:
            return
        _validate_log_descriptor(self._descriptor, self.path)
        if sync:
            os.fsync(self._descriptor)

    def close(self, *, sync: bool = False) -> None:
        if self._closed:
            return
        try:
            self.flush(sync=sync)
        finally:
            os.close(self._descriptor)
            self._closed = True

    def __enter__(self) -> "BoundedAppendWriter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _fsync_parent_directory(path: Path) -> None:
    """Durably publish a replacement on POSIX; Windows has no dir-fsync API."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write_bytes_anchored(path: Path, payload: bytes) -> None:
    """Publish a complete artifact through an anchored parent descriptor."""
    parent, name = _open_log_parent(path)
    target = -1
    temporary = -1
    temporary_name: str | None = None
    expected_identity: tuple[int, int] | None = None
    try:
        try:
            target = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent,
            )
        except FileNotFoundError:
            pass
        else:
            metadata = _validate_log_descriptor(target, path)
            expected_identity = (int(metadata.st_dev), int(metadata.st_ino))

        for _ in range(10):
            candidate = f".{name}.agentic-{uuid.uuid4().hex}.tmp"
            try:
                temporary = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary < 0 or temporary_name is None:
            raise FileExistsError(f"could not create a temporary durable artifact: {path}")

        remaining = memoryview(payload)
        while remaining:
            written = os.write(temporary, remaining)
            if written <= 0:
                raise OSError("durable artifact write made no progress")
            remaining = remaining[written:]
        os.fsync(temporary)
        os.close(temporary)
        temporary = -1

        if target >= 0:
            assert expected_identity is not None
            metadata = _validate_log_descriptor(target, path)
            if (int(metadata.st_dev), int(metadata.st_ino)) != expected_identity:
                raise OSError(f"durable log changed while being replaced: {path}")
            path_metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(path_metadata.st_mode) or path_metadata.st_nlink != 1:
                raise OSError(f"durable log target changed while being replaced: {path}")
            if (int(path_metadata.st_dev), int(path_metadata.st_ino)) != expected_identity:
                raise OSError(f"durable log changed while being replaced: {path}")
            os.close(target)
            target = -1
            # The descriptor check cannot protect the pathname once the
            # descriptor is closed.  Revalidate the anchored parent entry at
            # the final mutation boundary so a swapped artifact fails closed.
            path_metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(path_metadata.st_mode) or path_metadata.st_nlink != 1:
                raise OSError(f"durable log target changed while being replaced: {path}")
            if (int(path_metadata.st_dev), int(path_metadata.st_ino)) != expected_identity:
                raise OSError(f"durable log changed while being replaced: {path}")
            os.replace(temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent)
        else:
            os.link(temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.unlink(temporary_name, dir_fd=parent)
        temporary_name = None
        os.fsync(parent)
    finally:
        if temporary >= 0:
            os.close(temporary)
        if target >= 0:
            os.close(target)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8",
                      redact: bool = True, extra_values: Iterable[str] = ()) -> None:
    """Optionally redact, then replace *path* after the complete write succeeds.

    ``extra_values`` carries selected credentials whose environment names are
    intentionally non-semantic and therefore cannot be inferred safely.

    A failed write or replacement leaves an existing destination untouched and
    removes the same-directory temporary file. This is the persistence
    boundary for durable text as well as JSON serialized through
    :func:`atomic_write_json`. Generated HTML may disable the final text pass
    when its dynamic data has already been safely encoded and redacted; running
    the credential regex over JavaScript source can turn expressions such as
    ``key=value`` into invalid code.
    """
    if redact:
        text = redact_secrets(text, extra_values=extra_values)
    payload = text.encode(encoding)
    if _supports_anchored_log_open():
        _atomic_write_bytes_anchored(path, payload)
    else:
        _validate_log_parent(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _validate_log_parent(path)
        atomic_replace_relative_regular(
            path.parent, path.name, payload,
            allow_existing=True, description="durable artifact",
        )
    _fsync_parent_directory(path)


def atomic_write_json(path: Path, value: Any, *, indent: int = 2) -> None:
    """Serialize JSON and persist it atomically with a trailing newline."""
    atomic_write_text(path, json.dumps(value, indent=indent) + "\n")


def read_text_tail(path: Path, max_bytes: int, *, encoding: str = "utf-8") -> str:
    """Read at most the final *max_bytes* of a text artifact.

    Durable provider and test logs are untrusted evidence.  Reading their tail
    keeps resume and diagnostic paths bounded even when an older writer created
    an oversized artifact.  ``errors='replace'`` also makes a byte-window cut
    at a UTF-8 boundary harmless to callers.
    """
    if max_bytes <= 0:
        return ""
    try:
        descriptor, _ = open_single_link_regular(
            path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0), description="text artifact",
        )
        try:
            size = os.lseek(descriptor, 0, os.SEEK_END)
            os.lseek(descriptor, max(0, size - max_bytes), os.SEEK_SET)
            payload = os.read(descriptor, max_bytes)
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return ""
    return payload.decode(encoding, errors="replace")


def read_text_prefix(path: Path, max_bytes: int, *, encoding: str = "utf-8") -> str:
    """Read at most the first *max_bytes* from a single-link regular file."""
    if max_bytes <= 0:
        return ""
    try:
        descriptor, _ = open_single_link_regular(
            path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0), description="text artifact",
        )
        try:
            payload = os.read(descriptor, max_bytes)
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return ""
    return payload.decode(encoding, errors="replace")


def read_text_bounded(path: Path, max_bytes: int, *, encoding: str = "utf-8") -> str | None:
    """Read a complete text artifact only when it fits the byte budget.

    JSON state must be parsed as a complete document, so tail reads are not a
    safe substitute here.  Returning ``None`` for missing, unreadable, or
    oversized files lets callers fail closed without allocating for an
    attacker-controlled artifact.
    """
    if max_bytes <= 0:
        return "" if max_bytes == 0 else None
    try:
        descriptor, _ = open_single_link_regular(
            path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0), description="text artifact",
        )
        try:
            payload = os.read(descriptor, max_bytes + 1)
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return None
    if len(payload) > max_bytes:
        return None
    return payload.decode(encoding, errors="replace")
