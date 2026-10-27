"""Single-link regular-file checks for worktree/provider boundaries."""
from __future__ import annotations

import hashlib
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


class SafeTraversalUnavailable(RuntimeError):
    """The platform cannot provide an anchored, no-follow path traversal."""


@contextmanager
def exclusive_regular_file_lock(descriptor: int) -> Iterator[None]:
    """Hold an exclusive OS file lock while mutating an open regular file.

    The lock coordinates Agentic's own source mutations and narrows the
    validate-to-mutate window. Digest checks remain necessary because external
    tools may not participate in this advisory lock protocol.
    """
    if os.name == "nt":
        import msvcrt

        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


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


if os.name == "nt":
    import ctypes
    from ctypes import wintypes


    class _WindowsDirectoryHandle:
        """Small owner for a Windows directory handle used as a path anchor."""

        def __init__(self, handle: int) -> None:
            self.handle = handle

        def path(self) -> Path:
            return _windows_handle_path(self.handle)

        def close(self) -> None:
            if self.handle >= 0:
                _windows_close_handle(self.handle)
                self.handle = -1


    class _WindowsFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]


    def _windows_kernel32() -> Any:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_WindowsFileInformation),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ]
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        return kernel32


    def _windows_close_handle(handle: int) -> None:
        _windows_kernel32().CloseHandle(wintypes.HANDLE(handle))


    def _windows_handle_info(handle: int) -> _WindowsFileInformation:
        info = _WindowsFileInformation()
        if not _windows_kernel32().GetFileInformationByHandle(
            wintypes.HANDLE(handle), ctypes.byref(info),
        ):
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error))
        return info


    def _windows_handle_path(handle: int) -> Path:
        size = 512
        kernel32 = _windows_kernel32()
        while size <= 32768:
            buffer = ctypes.create_unicode_buffer(size)
            length = kernel32.GetFinalPathNameByHandleW(
                wintypes.HANDLE(handle), buffer, size, 0,
            )
            if length == 0:
                error = ctypes.get_last_error()
                raise OSError(error, os.strerror(error))
            if length < size - 1:
                value = buffer.value
                if value.startswith("\\\\?\\UNC\\"):
                    value = "\\\\" + value[8:]
                elif value.startswith("\\\\?\\"):
                    value = value[4:]
                return Path(value)
            size *= 2
        raise OSError("Windows path exceeds the supported handle-path limit")


    def _windows_open_handle(
        path: Path, *, directory: bool = False, flags: int = os.O_RDONLY,
        mode: int = 0o644,
    ) -> int:
        del mode
        kernel32 = _windows_kernel32()
        generic_read = 0x80000000
        generic_write = 0x40000000
        file_read_attributes = 0x00000080
        file_list_directory = 0x00000001
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        file_share_delete = 0x00000004
        open_existing = 3
        create_new = 1
        create_always = 2
        open_always = 4
        file_attribute_normal = 0x00000080
        file_flag_open_reparse_point = 0x00200000
        file_flag_backup_semantics = 0x02000000

        if directory:
            desired = file_read_attributes | file_list_directory
            creation = open_existing
            attributes = file_attribute_normal | file_flag_open_reparse_point | file_flag_backup_semantics
        else:
            read = not (flags & os.O_WRONLY) or bool(flags & os.O_RDWR)
            write = bool(flags & (os.O_WRONLY | os.O_RDWR))
            desired = (generic_read if read else 0) | (generic_write if write else 0)
            if flags & os.O_CREAT:
                creation = create_new if flags & os.O_EXCL else create_always if flags & os.O_TRUNC else open_always
            else:
                creation = create_always if flags & os.O_TRUNC else open_existing
            attributes = file_attribute_normal | file_flag_open_reparse_point

        handle = kernel32.CreateFileW(
            str(path), desired,
            file_share_read | file_share_write | file_share_delete,
            None, creation, attributes, None,
        )
        value = getattr(handle, "value", handle)
        if value in (None, 0, ctypes.c_void_p(-1).value):
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error), str(path))
        return int(value)


    def _windows_open_directory(path: Path) -> _WindowsDirectoryHandle:
        if _is_reparse_or_symbolic_link(path):
            raise ValueError("symbolic links are not allowed for directory traversal")
        handle = _windows_open_handle(path, directory=True)
        try:
            info = _windows_handle_info(handle)
            if not info.dwFileAttributes & 0x00000010:
                raise NotADirectoryError(str(path))
            if info.dwFileAttributes & 0x00000400:
                raise ValueError("reparse points are not allowed for directory traversal")
            return _WindowsDirectoryHandle(handle)
        except Exception:
            _windows_close_handle(handle)
            raise


    def _windows_open_directory_at(
        descriptor: _WindowsDirectoryHandle | Path, name: str,
    ) -> _WindowsDirectoryHandle:
        return _windows_open_directory(_windows_directory_path(descriptor, name))


    def _windows_directory_path(
        descriptor: _WindowsDirectoryHandle | Path, name: str,
    ) -> Path:
        base = descriptor.path() if isinstance(descriptor, _WindowsDirectoryHandle) else descriptor
        return base / name


    def _windows_open_regular_at(
        descriptor: _WindowsDirectoryHandle, name: str, flags: int, mode: int,
        *, description: str,
    ) -> tuple[int, tuple[int, int]]:
        path = descriptor.path() / name
        handle = _windows_open_handle(path, flags=flags, mode=mode)
        try:
            info = _windows_handle_info(handle)
            if info.dwFileAttributes & 0x00000400:
                raise ValueError(f"symbolic links are not allowed for {description}")
            fd_flags = os.O_BINARY
            if flags & os.O_RDWR:
                fd_flags |= os.O_RDWR
            elif flags & os.O_WRONLY:
                fd_flags |= os.O_WRONLY
            else:
                fd_flags |= os.O_RDONLY
            import msvcrt
            descriptor_fd = msvcrt.open_osfhandle(handle, fd_flags)
            handle = -1
            metadata = os.fstat(descriptor_fd)
            _validate_single_link_regular(metadata, description=description)
            return descriptor_fd, _identity(metadata)
        finally:
            if handle >= 0:
                _windows_close_handle(handle)


def _parts(relative: str | Path) -> list[str]:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be relative and stay inside the trusted root")
    return [part for part in path.parts if part not in {"", "."}]


def _open_directory_at(descriptor: Any, name: str) -> Any:
    if os.name == "nt":
        return _windows_open_directory_at(descriptor, name)
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
    if os.name == "nt":
        root_handle = _windows_open_directory(root)
        current = root_handle
        try:
            for part in parts[:-1]:
                try:
                    child = _open_directory_at(current, part)
                except FileNotFoundError:
                    if not create_parents:
                        raise
                    _windows_directory_path(current, part).mkdir()
                    child = _open_directory_at(current, part)
                current.close()
                current = child
            return _windows_open_regular_at(
                current, parts[-1], flags, mode, description=description,
            )
        finally:
            current.close()
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
    fail_on_error: bool,
) -> Iterator[tuple[str, str, os.stat_result]]:
    """Walk regular files on Windows without following directory links."""
    if os.name == "nt":
        root_handle = _windows_open_directory(root)
        current = root_handle
        stack: list[tuple[_WindowsDirectoryHandle, str]] = []
        try:
            for part in parts:
                child = _open_directory_at(current, part)
                current.close()
                current = child
            stack = [(current, "/".join(parts))]
            current = None
            entries_seen = 0
            directories_seen = 0
            while stack:
                directory, directory_relative = stack.pop()
                try:
                    directories_seen += 1
                    if max_directories is not None and directories_seen > max_directories:
                        raise SafeTraversalUnavailable("directory listing exceeded its safety bound")
                    directory_path = directory.path()
                    directory_metadata = os.stat(directory_path, follow_symlinks=False)
                    yield ("directory", directory_relative, directory_metadata)
                    children: list[tuple[str, os.stat_result]] = []
                    with os.scandir(directory_path) as entries:
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
                                if fail_on_error:
                                    raise
                                continue
                            children.append((entry.name, metadata))
                    children.sort(key=lambda item: item[0])
                    subdirectories: list[tuple[_WindowsDirectoryHandle, str]] = []
                    for name, metadata in children:
                        child_relative = "/".join(
                            item for item in (directory_relative, name) if item
                        )
                        if stat.S_ISDIR(metadata.st_mode):
                            try:
                                child_directory = _open_directory_at(directory, name)
                            except OSError:
                                if fail_on_error:
                                    raise
                                continue
                            subdirectories.append((child_directory, child_relative))
                        elif stat.S_ISREG(metadata.st_mode):
                            yield ("file", child_relative, metadata)
                    stack.extend(reversed(subdirectories))
                finally:
                    directory.close()
        finally:
            if current is not None:
                current.close()
            for directory, _ in stack:
                directory.close()
        return
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


def atomic_replace_relative_regular(
    root: Path,
    relative: str | Path,
    content: bytes,
    *,
    expected_identity: tuple[int, int] | None = None,
    mode: int | None = None,
    create_parents: bool = False,
    allow_existing: bool = False,
    require_existing: bool = False,
    description: str = "file",
) -> None:
    """Write bytes through a same-directory temporary file and replace safely.

    The destination is never truncated.  Existing targets are revalidated by
    descriptor identity immediately before replacement, and the temporary
    file is removed on every failed write or replace.  POSIX replacement is
    anchored to the already validated parent directory; Windows uses the
    validated path fallback provided by this module.
    """
    parts = _parts(relative)
    if not parts:
        raise ValueError(f"{description} must name a file")
    target_descriptor = -1
    temporary_descriptor = -1
    temporary_name: str | None = None
    temporary_path: Path | None = None
    parent = -1
    target_path: Path | None = None
    target_was_absent = False
    try:
        if _supports_anchored_traversal():
            parent, name = _walk_to_parent(root, parts, create=create_parents)
            try:
                target_descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=parent,
                )
            except FileNotFoundError:
                if expected_identity is not None or require_existing:
                    raise
                target_was_absent = True
            else:
                metadata = os.fstat(target_descriptor)
                _validate_single_link_regular(metadata, description=description)
                if expected_identity is None and not allow_existing:
                    raise FileExistsError(f"{description} already exists")
                if expected_identity is None:
                    expected_identity = _identity(metadata)
                elif _identity(metadata) != expected_identity:
                    raise ValueError(f"{description} changed while it was being replaced")
                if mode is None:
                    mode = stat.S_IMODE(metadata.st_mode)

            for _ in range(10):
                candidate = f".{name}.agentic-{uuid.uuid4().hex}.tmp"
                try:
                    temporary_descriptor = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                        | getattr(os, "O_NONBLOCK", 0),
                        mode if mode is not None else 0o644,
                        dir_fd=parent,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            if temporary_descriptor < 0:
                raise FileExistsError(f"could not create a temporary {description}")
        else:
            target_path = _validate_windows_path_components(
                root, parts, create=create_parents, description=description,
            )
            try:
                target_descriptor, target_identity = open_single_link_regular(
                    target_path,
                    os.O_WRONLY | getattr(os, "O_NONBLOCK", 0),
                    description=description,
                )
            except FileNotFoundError:
                if expected_identity is not None or require_existing:
                    raise
                target_was_absent = True
            else:
                if expected_identity is None and not allow_existing:
                    raise FileExistsError(f"{description} already exists")
                if expected_identity is None:
                    expected_identity = target_identity
                elif target_identity != expected_identity:
                    raise ValueError(f"{description} changed while it was being replaced")
                if mode is None:
                    mode = stat.S_IMODE(os.fstat(target_descriptor).st_mode)
            parent_path = target_path.parent
            for _ in range(10):
                candidate_path = parent_path / f".{target_path.name}.agentic-{uuid.uuid4().hex}.tmp"
                try:
                    temporary_descriptor, _ = open_single_link_regular(
                        candidate_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NONBLOCK", 0),
                        mode if mode is not None else 0o644,
                        description=f"temporary {description}",
                    )
                except FileExistsError:
                    continue
                temporary_path = candidate_path
                break
            if temporary_descriptor < 0:
                raise FileExistsError(f"could not create a temporary {description}")

        remaining = memoryview(content)
        while remaining:
            written = os.write(temporary_descriptor, remaining)
            if written <= 0:
                raise OSError("temporary file write made no progress")
            remaining = remaining[written:]
        if mode is not None:
            if hasattr(os, "fchmod"):
                os.fchmod(temporary_descriptor, mode)
            elif temporary_path is not None:
                os.chmod(temporary_path, mode)
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        if target_descriptor >= 0:
            assert expected_identity is not None
            revalidate_single_link_regular(
                target_descriptor, expected_identity, description=description,
            )
            if parent >= 0:
                path_metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            else:
                assert target_path is not None
                path_metadata = os.stat(target_path, follow_symlinks=False)
            _validate_single_link_regular(path_metadata, description=description)
            if _identity(path_metadata) != expected_identity:
                raise ValueError(f"{description} changed while it was being replaced")
            # Windows cannot replace a file while its descriptor is open.  The
            # identity and path checks above are the final validation before
            # closing the descriptor and committing the replacement.
            os.close(target_descriptor)
            target_descriptor = -1
        if target_was_absent:
            # A create-only write must not clobber a file that appeared after
            # the initial absence check.  Linking the fully written temporary
            # file is an atomic no-replace commit in the same directory.
            if parent >= 0:
                assert temporary_name is not None
                os.link(
                    temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent,
                )
                os.unlink(temporary_name, dir_fd=parent)
            else:
                assert temporary_path is not None
                assert target_path is not None
                os.link(temporary_path, target_path)
                temporary_path.unlink()
            temporary_name = None
            temporary_path = None
            return
        if parent >= 0:
            assert temporary_name is not None
            os.replace(temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent)
        else:
            assert temporary_path is not None
            assert target_path is not None
            os.replace(temporary_path, target_path)
        temporary_name = None
        temporary_path = None
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if target_descriptor >= 0:
            os.close(target_descriptor)
        if parent >= 0:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent)
                except FileNotFoundError:
                    pass
            os.close(parent)
        elif temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def iter_safe_regular_files(
    root: Path,
    relative: str | Path = ".",
    *,
    skip: Callable[[str], bool] | None = None,
    max_entries: int | None = None,
    max_directories: int | None = None,
    fail_on_error: bool = False,
) -> Iterator[tuple[str, str, os.stat_result]]:
    """Yield ``(kind, relative_path, metadata)`` without raced parents.

    Protected or non-regular entries remain intentionally filtered.  When
    ``fail_on_error`` is true, operational traversal failures are surfaced so
    callers that construct a security-sensitive staged view cannot silently
    expose a partial tree.
    """
    parts = _parts(relative)
    if not _supports_anchored_traversal():
        yield from _iter_safe_regular_files_without_dir_fd(
            root, parts, skip=skip, max_entries=max_entries,
            max_directories=max_directories, fail_on_error=fail_on_error,
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
                            if fail_on_error:
                                raise
                            continue
                        if stat.S_ISDIR(child_metadata.st_mode):
                            try:
                                child_descriptor = _open_directory_at(directory, entry.name)
                            except OSError:
                                if fail_on_error:
                                    raise
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


def unlink_relative_regular(
    root: Path,
    relative: str | Path,
    expected_identity: tuple[int, int],
    *,
    expected_digest: str | None = None,
    expected_size: int | None = None,
    description: str = "file",
) -> None:
    """Unlink one regular file only when its identity/content are unchanged."""

    def validate_descriptor(descriptor: int, identity: tuple[int, int]) -> None:
        metadata = os.fstat(descriptor)
        _validate_single_link_regular(metadata, description=description)
        if _identity(metadata) != identity:
            raise ValueError(f"{description} changed while it was being removed")
        if expected_digest is None:
            return
        if expected_size is not None and metadata.st_size != expected_size:
            raise ValueError(f"{description} changed while it was being removed")
        digest = hashlib.sha256()
        total = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if expected_size is not None and total > expected_size:
                raise ValueError(f"{description} changed while it was being removed")
            digest.update(chunk)
        if total != metadata.st_size or digest.hexdigest() != expected_digest:
            raise ValueError(f"{description} changed while it was being removed")

    parts = _parts(relative)
    if not parts:
        raise ValueError(f"{description} must name a file")
    if not _supports_anchored_traversal():
        target = _validate_windows_path_components(
            root, parts, create=False, description=description,
        )
        descriptor, identity = open_single_link_regular(
            target, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
            description=description,
        )
        try:
            with exclusive_regular_file_lock(descriptor):
                validate_descriptor(descriptor, identity)
                if identity != expected_identity:
                    raise ValueError(f"{description} changed while it was being removed")
                os.unlink(target)
        finally:
            os.close(descriptor)
        return
    parent, name = _walk_to_parent(root, parts, create=False)
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent,
        )
        try:
            with exclusive_regular_file_lock(descriptor):
                identity = _identity(os.fstat(descriptor))
                validate_descriptor(descriptor, identity)
                if identity != expected_identity:
                    raise ValueError(f"{description} changed while it was being removed")
                os.unlink(name, dir_fd=parent)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
