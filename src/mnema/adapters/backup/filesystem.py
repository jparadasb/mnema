from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from mnema.adapters.backup.base import BackupReceipt
from mnema.adapters.nas.fileops import fsync_directory, sha256_file


class FilesystemVersionedBackup:
    """Fast test backup preserving immutable, content-addressed copies."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.repository.mkdir(parents=True, exist_ok=True)

    async def snapshot(self, source: Path, idempotency_key: str) -> BackupReceipt:
        target = self.repository / f"{idempotency_key}.snapshot"
        if not target.exists():
            temporary = target.with_suffix(".partial")
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, target)
            fsync_directory(self.repository)
        await asyncio.sleep(0)
        return BackupReceipt(target.name)

    async def verify(self, receipt: BackupReceipt, expected_sha256: str) -> bool:
        await asyncio.sleep(0)
        return sha256_file(self.repository / receipt.snapshot_id) == expected_sha256

    async def restore(self, receipt: BackupReceipt, destination: Path) -> None:
        source = self.repository / receipt.snapshot_id
        with source.open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
        await asyncio.sleep(0)
