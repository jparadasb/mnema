from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class UnsafePath(ValueError):
    pass


def safe_relative_path(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or not candidate.parts:
        raise UnsafePath("path must be non-empty and relative")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise UnsafePath("path contains unsafe segment")
    return candidate


def resolve_beneath(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    root_resolved = root.resolve(strict=True)
    rel = safe_relative_path(relative)
    candidate = root_resolved.joinpath(rel)
    resolved = candidate.resolve(strict=must_exist)
    if not resolved.is_relative_to(root_resolved):
        raise UnsafePath("path escapes configured root")
    if must_exist:
        current = root_resolved
        for part in rel.parts:
            current = current / part
            if current.is_symlink():
                raise UnsafePath("symlinks are not allowed")
    return resolved


@dataclass(frozen=True)
class StorageIdentity:
    path: Path
    device_id: int
    filesystem_uuid: str | None = None


def storage_identity(path: Path, filesystem_uuid: str | None = None) -> StorageIdentity:
    return StorageIdentity(path.resolve(strict=True), os.stat(path).st_dev, filesystem_uuid)


def storage_is_separate(active: StorageIdentity, backup: StorageIdentity) -> bool:
    if active.filesystem_uuid and backup.filesystem_uuid:
        return active.filesystem_uuid != backup.filesystem_uuid
    return active.device_id != backup.device_id
