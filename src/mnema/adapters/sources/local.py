from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from mnema.domain.source import (
    AuthenticationStatus,
    DeleteReceipt,
    DiscoveryPage,
    DownloadReceipt,
    SourceCapabilities,
    SourceObject,
)
from mnema.domain.storage import UnsafePath, resolve_beneath


class SourceChangedError(RuntimeError):
    pass


class LocalFilesystemSourceAdapter:
    def __init__(
        self,
        root: Path,
        *,
        page_size: int = 100,
        interrupt_after_bytes: int | None = None,
        allow_delete: bool = False,
    ) -> None:
        self.root = root.resolve(strict=True)
        self.page_size = page_size
        self.interrupt_after_bytes = interrupt_after_bytes
        self.allow_delete = allow_delete

    async def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            can_delete=self.allow_delete,
            supports_cursor=True,
            stable_versions=True,
        )

    async def authentication_status(self) -> AuthenticationStatus:
        return AuthenticationStatus(True, "local filesystem")

    def _object(self, path: Path) -> SourceObject:
        relative = path.relative_to(self.root).as_posix()
        stat = path.stat(follow_symlinks=False)
        if path.is_symlink() or not path.is_file():
            raise UnsafePath("source object must be a regular non-symlink file")
        version = f"{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
        return SourceObject(
            source_id=relative,
            relative_path=relative,
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
            version=version,
        )

    async def discover(self, cursor: str | None = None) -> DiscoveryPage:
        files = sorted(
            path for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()
        )
        start = int(cursor or "0")
        page = files[start : start + self.page_size]
        objects = tuple(self._object(path) for path in page)
        next_index = start + len(page)
        next_cursor = str(next_index) if next_index < len(files) else None
        return DiscoveryPage(objects, next_cursor)

    async def stat(self, source_id: str) -> SourceObject:
        path = resolve_beneath(self.root, source_id, must_exist=True)
        return self._object(path)

    async def download(self, source_id: str, destination: Path) -> DownloadReceipt:
        before = await self.stat(source_id)
        source = resolve_beneath(self.root, source_id, must_exist=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        written = 0
        try:
            with source.open("rb") as reader, destination.open("xb") as writer:
                while chunk := reader.read(1024 * 1024):
                    writer.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if (
                        self.interrupt_after_bytes is not None
                        and written >= self.interrupt_after_bytes
                    ):
                        raise InterruptedError("simulated interrupted transfer")
                    await asyncio.sleep(0)
                writer.flush()
                os.fsync(writer.fileno())
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            raise
        after = await self.stat(source_id)
        if before.version != after.version:
            raise SourceChangedError("source changed during transfer")
        return DownloadReceipt(
            source_id=source_id,
            bytes_written=written,
            sha256=digest.hexdigest(),
            version=before.version,
        )

    async def delete(self, source_id: str, expected_version: str) -> DeleteReceipt:
        if not self.allow_delete or os.getenv("MNEMA_ALLOW_TEST_DELETE") != "1":
            raise PermissionError("test deletion is disabled")
        current = await self.stat(source_id)
        if current.version != expected_version:
            raise SourceChangedError("source changed before deletion")
        path = resolve_beneath(self.root, source_id, must_exist=True)
        path.unlink()
        return DeleteReceipt(source_id, expected_version, not path.exists())
