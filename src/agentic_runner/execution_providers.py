"""Provider-specific adapters built on the shared streaming runner."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .execution_stream import (
    _record_provider_usage,
    _short,
    streamed_command,
)
from .file_boundaries import (
    exclusive_regular_file_lock,
    iter_safe_regular_files,
    open_relative_regular,
    open_single_link_regular,
    revalidate_single_link_regular,
    unlink_relative_regular,
)
from .guidance import guidance_phase, guidance_prompt
from .config import PROVIDER_API_KEY_ENV, provider_process_environment
from .persistence import atomic_write_text
from .persistence import read_text_tail
from .policy import provider_forbidden
from .providers import (
    cli_provider_command,
    codex_command,
    resolve_provider_executable,
)
from .security import redact_secrets
from .telemetry import extract_jsonl_usage


MAX_PROVIDER_RESULT_BYTES = 4 * 1024 * 1024
MAX_PROVIDER_STREAM_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_MESSAGE_CHARS = 50_000
MAX_PROVIDER_STAGE_BYTES = 64 * 1024 * 1024
MAX_PROVIDER_STAGE_FILES = 10_000
MAX_PROVIDER_STAGE_ENTRIES = 12_000
MAX_PROVIDER_STAGE_DIRECTORIES = 2_000
PROVIDER_MESSAGE_TRUNCATION_MARKER = "\n[agentic_provider_message_truncated]\n"


@dataclass(frozen=True)
class _ProjectionFile:
    """Bounded identity and content metadata for one projection file."""

    identity: tuple[int, int]
    size: int
    digest: str
    mode: int
    source_mode: int


def _bound_provider_message(value: str) -> str:
    """Keep durable provider messages within the documented character bound."""
    if len(value) <= MAX_PROVIDER_MESSAGE_CHARS:
        return value
    keep = max(0, MAX_PROVIDER_MESSAGE_CHARS - len(PROVIDER_MESSAGE_TRUNCATION_MARKER))
    return value[:keep] + PROVIDER_MESSAGE_TRUNCATION_MARKER


def _stage_provider_input(source: Path, destination: Path) -> None:
    """Copy one trusted provider input without following a source hardlink."""
    try:
        descriptor, expected_identity = open_single_link_regular(
            source, os.O_RDONLY, description="provider input",
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not inspect provider input: {source}") from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            metadata = revalidate_single_link_regular(
                stream.fileno(), expected_identity, description="provider input",
            )
            if metadata.st_size > MAX_PROVIDER_RESULT_BYTES:
                raise RuntimeError(f"Provider input is too large: {source}")
            data = stream.read(MAX_PROVIDER_RESULT_BYTES + 1)
            if len(data) > MAX_PROVIDER_RESULT_BYTES:
                raise RuntimeError(f"Provider input is too large: {source}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    destination.write_bytes(data)
    try:
        destination.chmod(0o444)
    except OSError:
        # The invocation directory is disposable; a platform that cannot set
        # this mode still keeps the durable source outside the provider mount.
        pass


def _read_projection_file(root: Path, relative: str) -> tuple[_ProjectionFile, bytes]:
    """Read one projection file through the anchored regular-file boundary."""
    descriptor, expected_identity = open_relative_regular(
        root, relative, os.O_RDONLY, description="provider projection file",
    )
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            metadata = revalidate_single_link_regular(
                stream.fileno(), expected_identity,
                description="provider projection file",
            )
            if metadata.st_size > MAX_PROVIDER_RESULT_BYTES:
                raise RuntimeError(f"Provider projection file is too large: {relative}")
            data = stream.read(MAX_PROVIDER_RESULT_BYTES + 1)
            if len(data) > MAX_PROVIDER_RESULT_BYTES:
                raise RuntimeError(f"Provider projection file is too large: {relative}")
            return (
                _ProjectionFile(
                    identity=expected_identity,
                    size=len(data),
                    digest=hashlib.sha256(data).hexdigest(),
                    mode=stat.S_IMODE(metadata.st_mode),
                    source_mode=stat.S_IMODE(metadata.st_mode),
                ),
                data,
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _copy_projection_file(
    source_root: Path, destination_root: Path, relative: str, *, read_only: bool,
) -> _ProjectionFile:
    """Copy a source file into a disposable projection and return its baseline."""
    descriptor, expected_identity = open_relative_regular(
        source_root, relative, os.O_RDONLY, description="provider worktree file",
    )
    destination = destination_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    copied = 0
    try:
        with os.fdopen(descriptor, "rb") as source_stream:
            descriptor = -1
            metadata = revalidate_single_link_regular(
                source_stream.fileno(), expected_identity,
                description="provider worktree file",
            )
            if metadata.st_size > MAX_PROVIDER_RESULT_BYTES:
                raise RuntimeError(f"Provider input is too large: {source_root / relative}")
            with destination.open("wb") as destination_stream:
                while True:
                    chunk = source_stream.read(64 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_PROVIDER_RESULT_BYTES:
                        raise RuntimeError(f"Provider input is too large: {source_root / relative}")
                    digest.update(chunk)
                    destination_stream.write(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    source_mode = stat.S_IMODE(getattr(metadata, "st_mode", stat.S_IFREG | 0o644))
    mode = 0o444 if read_only else (source_mode | 0o600)
    try:
        destination.chmod(mode)
    except OSError:
        pass
    return _ProjectionFile(
        identity=expected_identity,
        size=copied,
        digest=digest.hexdigest(),
        mode=mode,
        source_mode=source_mode,
    )


def _remove_staging_tree(destination: Path) -> None:
    """Remove a partial projection, including read-only files on Windows."""
    def _retry_after_clearing_read_only(
        function: Callable[..., Any], path: str, _exc_info: Any,
    ) -> None:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        function(path)

    shutil.rmtree(destination, onerror=_retry_after_clearing_read_only)


def _stage_worktree(
    source: Path, destination: Path, *, skip: Callable[[str], bool], read_only: bool,
) -> dict[str, _ProjectionFile]:
    """Create a bounded, regular-file-only provider projection."""
    source = source.resolve(strict=True)
    destination_preexisted = destination.exists()
    destination.mkdir(parents=True, exist_ok=True)
    baseline: dict[str, _ProjectionFile] = {}
    try:
        entries = iter_safe_regular_files(
            source, skip=skip, fail_on_error=True,
            max_entries=MAX_PROVIDER_STAGE_ENTRIES,
            max_directories=MAX_PROVIDER_STAGE_DIRECTORIES,
        )
        staged_bytes = 0
        for kind, relative, _ in entries:
            if kind != "file":
                continue
            if len(baseline) >= MAX_PROVIDER_STAGE_FILES:
                raise RuntimeError("Provider staging exceeded its file-count limit")
            metadata = _copy_projection_file(
                source, destination, relative, read_only=read_only,
            )
            staged_bytes += metadata.size
            if staged_bytes > MAX_PROVIDER_STAGE_BYTES:
                raise RuntimeError("Provider staging exceeded its byte limit")
            baseline[relative] = metadata
    except (OSError, RuntimeError, ValueError) as exc:
        if not destination_preexisted:
            _remove_staging_tree(destination)
        raise RuntimeError("Could not stage the complete provider worktree") from exc
    return baseline


def _stage_readonly_worktree(
    source: Path, destination: Path, *, allow_self_hosted_sources: bool = False,
) -> Path:
    """Create an allowlisted read-only provider view of a worktree.

    Native CLIs do not share a path-deny policy.  Staging the visible tree
    makes the protected-file boundary provider-independent for reviewer turns.
    Git metadata, symlinks, and paths covered by the publication ``forbidden``
    policy are never copied into the provider's cwd.
    """
    try:
        _stage_worktree(
            source, destination,
            skip=lambda filename: provider_forbidden(
                filename, allow_self_hosted_sources=allow_self_hosted_sources,
            ),
            read_only=True,
        )
    except RuntimeError as exc:
        raise RuntimeError("Could not stage the complete read-only worktree") from exc
    return destination


def _same_projection_content(
    actual: _ProjectionFile | None, expected: _ProjectionFile | None,
) -> bool:
    return bool(
        actual is not None and expected is not None
        and actual.size == expected.size
        and actual.digest == expected.digest
        and actual.mode == expected.mode
    )


def _same_source_file(
    actual: _ProjectionFile | None, expected: _ProjectionFile | None,
) -> bool:
    return bool(
        actual is not None and expected is not None
        and actual.size == expected.size
        and actual.digest == expected.digest
        and actual.identity == expected.identity
        and actual.mode == expected.source_mode
    )


def _descriptor_digest(descriptor: int, relative: str) -> str:
    """Hash an already-open provider target before mutating it."""
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PROVIDER_RESULT_BYTES:
                raise RuntimeError(f"Provider worktree file is too large: {relative}")
            digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return digest.hexdigest()
    except OSError as exc:
        raise RuntimeError(f"Could not validate provider worktree file: {relative}") from exc


def _snapshot_projection_descriptor(
    descriptor: int, relative: str, identity: tuple[int, int],
) -> _ProjectionFile:
    """Capture a mutation snapshot from the already-open validated target."""
    metadata = revalidate_single_link_regular(
        descriptor, identity, description="provider worktree file",
    )
    digest = _descriptor_digest(descriptor, relative)
    metadata = revalidate_single_link_regular(
        descriptor, identity, description="provider worktree file",
    )
    mode = stat.S_IMODE(metadata.st_mode)
    return _ProjectionFile(
        identity=identity,
        size=metadata.st_size,
        digest=digest,
        mode=mode,
        source_mode=mode,
    )


def _write_projection_file(
    root: Path, relative: str, data: bytes, mode: int,
    expected: _ProjectionFile | None,
) -> _ProjectionFile:
    """Write a changed safe file only after validating its original target."""
    # Windows' byte-range locking requires a read/write descriptor even for
    # newly created files.  Use the same descriptor mode for both paths so a
    # provider-created file can be validated and synchronized there too.
    flags = os.O_RDWR
    descriptor = -1
    created = False
    mutation_identity: tuple[int, int] | None = None
    post_mode = stat.S_IMODE(mode)
    digest = hashlib.sha256(data).hexdigest()
    try:
        try:
            descriptor, identity = open_relative_regular(
                root, relative, flags, description="provider worktree file",
            )
        except FileNotFoundError:
            descriptor, identity = open_relative_regular(
                root, relative, flags | os.O_CREAT | os.O_EXCL,
                create_parents=True, description="provider worktree file",
            )
            created = True
        with exclusive_regular_file_lock(descriptor):
            if expected is not None and identity != expected.identity:
                raise RuntimeError(f"Provider worktree file changed: {relative}")
            if expected is None and not created:
                raise RuntimeError(f"Provider worktree file appeared during sync: {relative}")
            metadata = revalidate_single_link_regular(
                descriptor, identity, description="provider worktree file",
            )
            if expected is not None:
                if (metadata.st_dev, metadata.st_ino) != expected.identity:
                    raise RuntimeError(f"Provider worktree file changed: {relative}")
                if stat.S_IMODE(metadata.st_mode) != expected.source_mode:
                    raise RuntimeError(f"Provider worktree file changed: {relative}")
                if _descriptor_digest(descriptor, relative) != expected.digest:
                    raise RuntimeError(f"Provider worktree file changed: {relative}")
                # Revalidate the descriptor after hashing and immediately before
                # truncation so a same-inode source edit cannot be overwritten.
                metadata = revalidate_single_link_regular(
                    descriptor, identity, description="provider worktree file",
                )
                if stat.S_IMODE(metadata.st_mode) != expected.source_mode:
                    raise RuntimeError(f"Provider worktree file changed: {relative}")
                if _descriptor_digest(descriptor, relative) != expected.digest:
                    raise RuntimeError(f"Provider worktree file changed: {relative}")
            mutation_identity = identity
            try:
                os.ftruncate(descriptor, 0)
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("provider worktree write made no progress")
                    view = view[written:]
                # Windows Python builds do not all expose fchmod, and Windows
                # does not preserve POSIX executable bits in the same way.  The
                # projection content and descriptor identity remain authoritative
                # there; apply mode-only changes where the descriptor primitive is
                # available.
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    fchmod(descriptor, stat.S_IMODE(mode))
                post_mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
                os.fsync(descriptor)
            except Exception as exc:
                # Preserve enough evidence for the caller to roll back a
                # partial write.  A later path scan is not sufficient for a
                # newly-created file: a concurrent file with the same bytes
                # must never be mistaken for our mutation.
                mutation_snapshot = None
                try:
                    if mutation_identity is not None:
                        mutation_snapshot = _snapshot_projection_descriptor(
                            descriptor, relative, mutation_identity,
                        )
                except (FileNotFoundError, OSError, RuntimeError, ValueError):
                    pass
                failure = RuntimeError(
                    f"Could not complete provider worktree write: {relative}",
                )
                setattr(failure, "_agentic_mutation_identity", mutation_identity)
                setattr(failure, "_agentic_mutation_snapshot", mutation_snapshot)
                raise failure from exc
    except FileExistsError:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        raise RuntimeError(f"Provider worktree file appeared during sync: {relative}")
    except Exception:
        # Release the source handle before the caller begins rollback.  Windows
        # otherwise rejects deletion of a newly-created file that this helper
        # just proved was ours.
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _ProjectionFile(
        identity=identity,
        size=len(data),
        digest=digest,
        mode=post_mode,
        source_mode=post_mode,
    )


def _validate_missing_projection_path(root: Path, relative: str) -> None:
    """Reject a provider replacement that would be mistaken for a deletion."""
    current = root
    parts = relative.split("/")
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"Provider projection contains a symbolic link: {relative}")
        if index < len(parts) - 1:
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(f"Provider projection contains a non-directory: {relative}")
        elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(f"Provider projection contains a non-regular file: {relative}")


def _recover_provider_write(
    source: Path, relative: str, data: bytes,
    expected: _ProjectionFile | None, *,
    mutation_identity: tuple[int, int] | None = None,
    mutation_snapshot: _ProjectionFile | None = None,
) -> _ProjectionFile | None:
    """Identify a source write that completed before its helper raised."""
    if expected is None and mutation_identity is None:
        return None
    if mutation_snapshot is not None:
        current_snapshot = mutation_snapshot
    else:
        try:
            current_snapshot, _ = _read_projection_file(source, relative)
        except FileNotFoundError:
            return None
    if mutation_identity is not None and current_snapshot.identity != mutation_identity:
        return None
    if expected is not None and current_snapshot.identity != expected.identity:
        return None
    if (
        current_snapshot.size != len(data)
        or current_snapshot.digest != hashlib.sha256(data).hexdigest()
    ):
        # A helper failure can happen after truncation but before all bytes
        # arrive.  The recorded identity and snapshot prove that this is our
        # partial mutation, so the normal rollback path can restore it.
        return current_snapshot if mutation_snapshot is not None else None
    return current_snapshot


def _sync_provider_worktree(
    source: Path, projection: Path, baseline: dict[str, _ProjectionFile], *,
    allow_self_hosted_sources: bool = False,
) -> None:
    """Apply only safe provider changes back to the original worktree."""
    current: dict[str, tuple[_ProjectionFile, bytes]] = {}
    current_files = 0
    current_bytes = 0
    for kind, relative, _ in iter_safe_regular_files(
        projection,
        skip=lambda filename: provider_forbidden(
            filename, allow_self_hosted_sources=allow_self_hosted_sources,
        ),
        fail_on_error=True,
        max_entries=MAX_PROVIDER_STAGE_ENTRIES,
        max_directories=MAX_PROVIDER_STAGE_DIRECTORIES,
    ):
        if kind == "file":
            if current_files >= MAX_PROVIDER_STAGE_FILES:
                raise RuntimeError("Provider synchronization exceeded its file-count limit")
            staged = _read_projection_file(projection, relative)
            current_files += 1
            current_bytes += staged[0].size
            if current_bytes > MAX_PROVIDER_STAGE_BYTES:
                raise RuntimeError("Provider synchronization exceeded its byte limit")
            current[relative] = staged

    changes: list[tuple[str, str, bytes | None, int]] = []
    originals: dict[str, tuple[_ProjectionFile | None, bytes | None]] = {}
    for relative in sorted(set(baseline) | set(current)):
        expected = baseline.get(relative)
        staged = current.get(relative)
        if staged is not None and expected is not None and _same_projection_content(staged[0], expected):
            continue
        source_snapshot: _ProjectionFile | None = None
        source_data: bytes | None = None
        try:
            source_snapshot, source_data = _read_projection_file(source, relative)
        except FileNotFoundError:
            pass
        if expected is not None and not _same_source_file(source_snapshot, expected):
            raise RuntimeError(f"Provider source file changed during invocation: {relative}")
        if staged is None:
            if expected is not None:
                _validate_missing_projection_path(projection, relative)
                changes.append(("delete", relative, None, expected.mode))
        else:
            if expected is None and source_snapshot is not None:
                raise RuntimeError(f"Provider source file appeared during invocation: {relative}")
            changes.append(("write", relative, staged[1], staged[0].mode))
        originals[relative] = (source_snapshot, source_data)

    successful_mutations: list[tuple[str, _ProjectionFile | None]] = []
    unresolved_mutations: list[str] = []
    try:
        for action, relative, data, mode in changes:
            if action == "delete":
                assert baseline[relative] is not None
                source_snapshot, _ = _read_projection_file(source, relative)
                if not _same_source_file(source_snapshot, baseline[relative]):
                    raise RuntimeError(f"Provider source file changed during invocation: {relative}")
                unlink_relative_regular(
                    source, relative, baseline[relative].identity,
                    expected_digest=baseline[relative].digest,
                    expected_size=baseline[relative].size,
                    description="provider worktree file",
                )
                successful_mutations.append((relative, None))
            else:
                write_data = data or b""
                expected = baseline.get(relative)
                try:
                    post_change = _write_projection_file(
                        source, relative, write_data, mode, expected,
                    )
                except Exception as write_exc:
                    # A write helper can raise after truncating and writing the
                    # source (for example during a later fsync or close).  If
                    # the helper proves its mutation, record even partial
                    # content so the outer rollback can restore the original
                    # safely.  Without that proof, fail closed rather than
                    # deleting a concurrent file that happens to match.
                    post_change = _recover_provider_write(
                        source, relative, write_data, expected,
                        mutation_identity=getattr(
                            write_exc, "_agentic_mutation_identity", None,
                        ),
                        mutation_snapshot=getattr(
                            write_exc, "_agentic_mutation_snapshot", None,
                        ),
                    )
                    if post_change is not None:
                        successful_mutations.append((relative, post_change))
                    else:
                        try:
                            current_snapshot, _ = _read_projection_file(source, relative)
                        except FileNotFoundError:
                            current_snapshot = None
                        if (
                            (expected is None and current_snapshot is not None)
                            or (
                                expected is not None
                                and not _same_source_file(current_snapshot, expected)
                            )
                        ):
                            unresolved_mutations.append(relative)
                    raise
                successful_mutations.append((relative, post_change))
    except Exception as exc:
        rollback_errors: list[str] = [
            f"{relative}: mutation ownership could not be proven"
            for relative in unresolved_mutations
        ]
        for relative, post_change in reversed(successful_mutations):
            original, original_data = originals[relative]
            try:
                try:
                    current_snapshot, _ = _read_projection_file(source, relative)
                except FileNotFoundError:
                    current_snapshot = None
                if post_change is None:
                    if current_snapshot is not None:
                        raise RuntimeError(
                            "current file changed after provider deletion; refusing rollback"
                        )
                    if original is not None and original_data is not None:
                        _write_projection_file(
                            source, relative, original_data, original.mode, None,
                        )
                    continue
                if not _same_source_file(current_snapshot, post_change):
                    raise RuntimeError(
                        "current file changed after provider update; refusing rollback"
                    )
                if original is None:
                    if current_snapshot is not None:
                        unlink_relative_regular(
                            source, relative, current_snapshot.identity,
                            expected_digest=current_snapshot.digest,
                            expected_size=current_snapshot.size,
                            description="provider worktree rollback",
                        )
                elif original_data is not None:
                    _write_projection_file(
                        source, relative, original_data, original.mode, current_snapshot,
                    )
            except Exception as rollback_exc:  # pragma: no cover - compounded I/O failure
                rollback_errors.append(f"{relative}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                "Provider worktree synchronization failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise RuntimeError("Provider worktree synchronization failed; changes rolled back") from exc


def _read_provider_result(root: Path, name: str) -> str | None:
    """Read an exact, bounded result file without following provider links."""
    descriptor = -1
    try:
        descriptor, expected_identity = open_relative_regular(
            root, name, os.O_RDONLY, description="provider result",
        )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            metadata = revalidate_single_link_regular(
                handle.fileno(), expected_identity, description="provider result",
            )
            if metadata.st_size > MAX_PROVIDER_RESULT_BYTES:
                return None
            data = handle.read(MAX_PROVIDER_RESULT_BYTES + 1)
        if len(data) > MAX_PROVIDER_RESULT_BYTES:
            return None
        return data.decode("utf-8")
    except (OSError, RuntimeError, ValueError, UnicodeError):
        if descriptor >= 0:
            os.close(descriptor)
        return None

def run_codex(worktree: Path, prompt: str, *, output: Path, readonly: bool = False,
              schema: Path | None = None, timeout: int | None = None,
              model: str | None = None,
              reasoning_effort: str | None = None,
              api_key_env: str | None = None,
              permission_policy: dict[str, bool] | None = None,
              verbose: bool = False,
              sandbox: dict[str, Any] | None = None,
              allow_self_hosted_sources: bool = False,
              log_label: str | None = None) -> tuple[bool, str]:
    # Each invocation creates a fresh session. The repository is the memory.
    prompt = guidance_prompt(
        worktree, prompt, readonly=readonly, audit_folder=output.parent,
        phase=guidance_phase(output, readonly=readonly),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    credential_name = api_key_env or PROVIDER_API_KEY_ENV.get("codex")
    credential_value = os.environ.get(credential_name) if credential_name else None
    redaction_values = [credential_value] if credential_value else []
    with tempfile.TemporaryDirectory(prefix="agentic-provider-") as temporary_directory:
        invocation_output = Path(temporary_directory)
        provider_worktree = worktree
        developer_baseline: dict[str, _ProjectionFile] | None = None
        if readonly:
            provider_worktree = _stage_readonly_worktree(
                worktree, invocation_output / "review-worktree",
                allow_self_hosted_sources=allow_self_hosted_sources,
            )
        else:
            provider_worktree = invocation_output / "developer-worktree"
            developer_baseline = _stage_worktree(
                worktree, provider_worktree,
                skip=lambda filename: provider_forbidden(
                    filename, allow_self_hosted_sources=allow_self_hosted_sources,
                ),
                read_only=False,
            )
        temporary_output = invocation_output / "result.txt"
        staged_schema: Path | None = None
        if schema is not None:
            staged_schema = invocation_output / "schema.json"
            _stage_provider_input(schema, staged_schema)
        command_kwargs: dict[str, Any] = {}
        if permission_policy is not None:
            command_kwargs["permission_policy"] = permission_policy
        args = codex_command(
            prompt, readonly=readonly, schema=staged_schema, output=temporary_output,
            model=model, reasoning_effort=reasoning_effort,
            worktree_is_projection=developer_baseline is not None,
            **command_kwargs,
        )
        label = log_label or ("REVIEW" if readonly else "DEVELOPER")
        invocation_started = time.monotonic()
        result = streamed_command(
            args, provider_worktree, label=label, log_file=output.with_suffix(".log"),
            timeout=timeout, codex_json=True, verbose=verbose, sandbox=sandbox,
            sandbox_read_only=readonly,
            sandbox_pass_env=(sandbox or {}).get("pass_env", []),
            metrics_folder=output.parent,
            output_root=invocation_output,
            env=provider_process_environment("codex", api_key_env),
            redaction_values=redaction_values,
        )
        if developer_baseline is not None:
            _sync_provider_worktree(
                worktree, provider_worktree, developer_baseline,
                allow_self_hosted_sources=allow_self_hosted_sources,
            )
        message = _read_provider_result(invocation_output, "result.txt")
        if message is None:
            message = result.stdout[-4000:]
        message = _bound_provider_message(
            redact_secrets(message, extra_values=redaction_values),
        )
        atomic_write_text(output, message, extra_values=redaction_values)
        usage_path = output.with_suffix(".jsonl")
        usage = extract_jsonl_usage(
            read_text_tail(usage_path, MAX_PROVIDER_STREAM_BYTES) if usage_path.exists() else "",
            provider="codex",
        )
        _record_provider_usage(
            output, readonly, usage, model=model, prompt_chars=len(prompt),
            completion_chars=len(message), turns=1,
            duration_seconds=time.monotonic() - invocation_started,
            status="completed" if result.returncode == 0 else "failed",
            exit_code=result.returncode,
        )
        return result.returncode == 0, message
def _provider_progress(line: str, provider: str) -> str | None:
    """Turn Gemini/Claude JSON events into compact terminal progress."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return _short(line, 350) if line.strip() else None
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type", ""))
    if event_type in {"init", "thread.started", "session_start"}:
        return f"{provider.title()} session started"
    if event_type in {"result", "turn.completed", "session_end"}:
        return f"{provider.title()} agent finished"
    if event_type in {"tool_use", "tool_call"}:
        tool = event.get("tool")
        tool_name = tool.get("name") if isinstance(tool, dict) else tool
        name = event.get("name") or event.get("tool_name") or tool_name
        return f"Using tool: {_short(name or 'tool', 160)}"
    if event_type in {"error", "turn.failed"}:
        return "ERROR: " + _short(event.get("error", event.get("message", line)), 300)
    if event_type in {"message", "assistant", "agent_message"}:
        content = event.get("content") or event.get("text")
        if isinstance(content, list):
            content = " ".join(
                str(item.get("text", "")) for item in content
                if isinstance(item, dict) and item.get("text")
            )
        return "Agent: " + _short(content, 350) if content else None
    return None


def _message_text(value: Any) -> str:
    """Extract text from the common CLI/API message content shapes."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return _message_text(value.get("text") or value.get("content") or value.get("response"))
    return ""


def _extract_provider_message(raw: str, provider: str) -> str:
    """Recover the final assistant message from a provider's JSON stream."""
    if provider == "copilot":
        return _bound_provider_message(raw.strip())
    chunks: list[str] = []
    chunk_chars = 0
    chunks_truncated = False
    final = ""
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        response = _message_text(event.get("response"))
        result = _message_text(event.get("result"))
        if response:
            final = _bound_provider_message(response)
        if result:
            final = _bound_provider_message(result)
        if event_type in {"message", "assistant", "agent_message"}:
            message = event.get("message")
            content = _message_text(event.get("content"))
            if not content and isinstance(message, dict):
                content = _message_text(message.get("content"))
            if content:
                if not chunks_truncated:
                    remaining = MAX_PROVIDER_MESSAGE_CHARS - chunk_chars
                    if len(content) > remaining:
                        marker_space = max(
                            0, MAX_PROVIDER_MESSAGE_CHARS
                            - len(PROVIDER_MESSAGE_TRUNCATION_MARKER),
                        )
                        prefix = "".join(chunks)[:marker_space]
                        if len(prefix) < marker_space:
                            prefix += content[:marker_space - len(prefix)]
                        chunks[:] = [prefix, PROVIDER_MESSAGE_TRUNCATION_MARKER]
                        chunks_truncated = True
                        chunk_chars = MAX_PROVIDER_MESSAGE_CHARS
                    else:
                        chunks.append(content)
                        chunk_chars += len(content)
    return _bound_provider_message(final or "\n".join(chunks)).strip()


def run_cli_provider(worktree: Path, prompt: str, *, output: Path,
                     provider: str, readonly: bool = False,
                     timeout: int | None = None,
                     model: str | None = None,
                     permission_policy: dict[str, bool] | None = None,
                     security_mode: str = "trusted",
                     check_commands: list[list[str]] | None = None,
                     executable_path: str | None = None,
                     host: str | None = None,
                     api_key_env: str | None = None,
                     verbose: bool = False,
                     sandbox: dict[str, Any] | None = None,
                     allow_self_hosted_sources: bool = False,
                     log_label: str | None = None) -> tuple[bool, str]:
    """Run a locally installed coding CLI in its non-interactive mode."""
    prompt = guidance_prompt(
        worktree, prompt, readonly=readonly, audit_folder=output.parent,
        phase=guidance_phase(output, readonly=readonly),
    )
    command_kwargs: dict[str, Any] = {}
    if provider == "copilot":
        resolved = resolve_provider_executable(
            provider,
            executable_path,
            discover_copilot=not bool((sandbox or {}).get("enabled")),
        )
        effective_path = resolved.path or executable_path
        if effective_path:
            command_kwargs["executable_path"] = effective_path
    if permission_policy is not None:
        command_kwargs["permission_policy"] = permission_policy
    if security_mode != "trusted":
        command_kwargs["security_mode"] = security_mode
    if check_commands is not None:
        command_kwargs["check_commands"] = check_commands
    effective_sandbox = dict(sandbox) if sandbox is not None else None
    command_kwargs["sandbox_enabled"] = bool(effective_sandbox and effective_sandbox.get("enabled"))
    command_kwargs["sandbox_allow_network"] = bool(
        effective_sandbox and effective_sandbox.get("allow_network")
    )
    label = log_label or ("REVIEW" if readonly else "DEVELOPER")
    invocation_started = time.monotonic()
    sandbox_pass_env = list((sandbox or {}).get("pass_env", []))
    if provider == "copilot" and host and "COPILOT_GH_HOST" not in sandbox_pass_env:
        sandbox_pass_env.append("COPILOT_GH_HOST")
    credential_name = api_key_env or PROVIDER_API_KEY_ENV.get(provider)
    credential_value = os.environ.get(credential_name) if credential_name else None
    redaction_values = [credential_value] if credential_value else []
    process_env = provider_process_environment(provider, api_key_env)
    if provider == "copilot" and host:
        process_env = dict(process_env)
        process_env["COPILOT_GH_HOST"] = host
    with tempfile.TemporaryDirectory(prefix="agentic-provider-") as temporary_directory:
        invocation_root = Path(temporary_directory)
        provider_worktree = worktree
        developer_baseline: dict[str, _ProjectionFile] | None = None
        if readonly:
            provider_worktree = _stage_readonly_worktree(
                worktree, invocation_root / "review-worktree",
                allow_self_hosted_sources=allow_self_hosted_sources,
            )
        else:
            provider_worktree = invocation_root / "developer-worktree"
            developer_baseline = _stage_worktree(
                worktree, provider_worktree,
                skip=lambda filename: provider_forbidden(
                    filename, allow_self_hosted_sources=allow_self_hosted_sources,
                ),
                read_only=False,
            )
        args, structured = cli_provider_command(
            provider, prompt, readonly=readonly, model=model,
            worktree=provider_worktree, **command_kwargs,
        )
        output_root = invocation_root / "output"
        output_root.mkdir()
        result = streamed_command(
            args, provider_worktree, label=label, log_file=output.with_suffix(".log"),
            timeout=timeout, codex_json=structured, verbose=verbose,
            progress_parser=(lambda line: _provider_progress(line, provider)) if structured else None,
            sandbox=effective_sandbox, sandbox_read_only=readonly,
            sandbox_pass_env=sandbox_pass_env,
            metrics_folder=output.parent, env=process_env,
            redaction_values=redaction_values,
            output_root=output_root,
        )
        if developer_baseline is not None:
            _sync_provider_worktree(
                worktree, provider_worktree, developer_baseline,
                allow_self_hosted_sources=allow_self_hosted_sources,
            )
    raw_path = output.with_suffix(".jsonl") if structured else None
    raw = read_text_tail(raw_path, MAX_PROVIDER_STREAM_BYTES) if raw_path and raw_path.exists() else result.stdout
    message = redact_secrets(
        _extract_provider_message(raw, provider) or result.stdout[-10000:].strip(),
        extra_values=redaction_values,
    )
    if (
        provider == "copilot" and host and model and result.returncode != 0
        and re.search(r"model .*?(?:not available|unavailable)", message, re.IGNORECASE)
    ):
        message = (
            f"Copilot model {model!r} is unavailable on provider host {host!r}. "
            "Verify the model is provisioned for that Copilot host and account.\n"
            + message
        )
    message = _bound_provider_message(message)
    atomic_write_text(output, message, extra_values=redaction_values)
    usage = extract_jsonl_usage(raw, provider=provider)
    _record_provider_usage(
        output, readonly, usage, model=model, prompt_chars=len(prompt),
        completion_chars=len(message), turns=1,
        duration_seconds=time.monotonic() - invocation_started,
        status="completed" if result.returncode == 0 else "failed",
        exit_code=result.returncode,
    )
    return result.returncode == 0, message
# End of provider adapters.
