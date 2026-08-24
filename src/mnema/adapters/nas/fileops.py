from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from mnema.domain.storage import resolve_beneath, safe_relative_path

# Leave room for filesystem overhead and a concurrent small write.
SCRATCH_MARGIN_BYTES = 64 * 1024 * 1024


class InsufficientScratchSpace(OSError):
    """Raised when a staging location cannot hold the work about to be done."""


@contextmanager
def scratch_directory(
    root: Path | None,
    prefix: str,
    *,
    required_bytes: int = 0,
) -> Iterator[Path]:
    """Yield a temporary directory sized for the work about to run.

    Cold-storage encryption, verification, and restore all materialise whole
    files. Defaulting those to the system temporary directory put them on a
    container tmpfs of a few hundred megabytes, so every file larger than that
    failed with ENOSPC reported as an ordinary retryable error. Passing a root on
    real storage removes the ceiling; the preflight turns a late ENOSPC into an
    early, specific refusal.
    """
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
    target = root if root is not None else Path(tempfile.gettempdir())
    if required_bytes:
        free = shutil.disk_usage(target).free
        if free < required_bytes:
            raise InsufficientScratchSpace(
                f"scratch space at {target} has {free} bytes free but {required_bytes} are required"
            )
    with tempfile.TemporaryDirectory(prefix=prefix, dir=root) as directory:
        yield Path(directory)


@dataclass(frozen=True)
class CommitReceipt:
    path: Path
    size: int
    sha256: str
    transformed_from: str | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def partial_path(staging_root: Path, archive_item_id: int) -> Path:
    staging_root.mkdir(parents=True, exist_ok=True)
    return staging_root / f"{archive_item_id}.partial"


def commit_staged(
    staged: Path,
    active_root: Path,
    relative_path: str,
    expected_sha256: str,
    expected_size: int,
    *,
    modified_timestamp: float | None = None,
) -> CommitReceipt:
    safe_relative_path(relative_path)
    if staged.is_symlink() or not staged.is_file():
        raise ValueError("staged object must be a regular non-symlink file")
    stat = staged.stat(follow_symlinks=False)
    if stat.st_size != expected_size:
        raise ValueError("staged size does not match source metadata")
    actual_hash = sha256_file(staged)
    if actual_hash != expected_sha256:
        raise ValueError("staged hash does not match download receipt")
    final = resolve_beneath(active_root, relative_path)
    final.parent.mkdir(parents=True, exist_ok=True)
    final = resolve_beneath(active_root, relative_path)
    transformed_from: str | None = None
    if final.exists():
        if final.is_symlink():
            raise ValueError("destination collision is a symlink")
        if final.stat().st_size == expected_size and sha256_file(final) == expected_sha256:
            staged.unlink()
            return CommitReceipt(final, expected_size, expected_sha256)
        suffix = expected_sha256[:12]
        transformed_from = relative_path
        final = final.with_name(f"{final.stem}.mnema-{suffix}{final.suffix}")
        if not final.resolve().is_relative_to(active_root.resolve()):
            raise ValueError("collision path escapes active root")
    os.replace(staged, final)
    if modified_timestamp is not None:
        os.utime(final, (modified_timestamp, modified_timestamp), follow_symlinks=False)
    fsync_directory(final.parent)
    return CommitReceipt(final, expected_size, expected_sha256, transformed_from)


def inspect_partial_files(staging_root: Path) -> tuple[Path, ...]:
    if not staging_root.exists():
        return ()
    return tuple(
        path for path in staging_root.iterdir() if path.is_file() and path.name.endswith(".partial")
    )
