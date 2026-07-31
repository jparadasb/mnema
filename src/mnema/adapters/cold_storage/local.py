from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from mnema.adapters.cold_storage.base import ColdReceipt
from mnema.adapters.cold_storage.crypto import decrypt_file, encrypt_file, sha256_hex
from mnema.adapters.nas.fileops import fsync_directory


class LocalEncryptedColdStorage:
    """Local protocol proof used when MinIO is unavailable."""

    def __init__(self, root: Path, key: bytes, bucket: str = "mnema-test") -> None:
        self.root = root
        self.key = key
        self.bucket = bucket
        self.root.mkdir(parents=True, exist_ok=True)

    async def upload(
        self,
        source: Path,
        object_identifier: str,
        idempotency_key: str,
    ) -> ColdReceipt:
        target = self.root / f"{idempotency_key}.mnema"
        if not target.exists():
            temporary = target.with_suffix(".partial")
            encrypt_file(source, temporary, self.key)
            os.replace(temporary, target)
            fsync_directory(self.root)
        await asyncio.sleep(0)
        return ColdReceipt(
            provider="local-test",
            bucket=self.bucket,
            object_identifier=target.name,
            encryption_mode="AES-256-GCM",
            remote_size=target.stat().st_size,
            remote_checksum=sha256_hex(target),
        )

    async def verify(self, receipt: ColdReceipt, expected_sha256: str) -> bool:
        with tempfile.TemporaryDirectory(prefix="mnema-cold-verify-") as directory:
            restored = Path(directory) / "plain"
            await self.restore(receipt, restored)
            return sha256_hex(restored) == expected_sha256

    async def archive_verified(self, receipt: ColdReceipt) -> None:
        del receipt

    async def restore(self, receipt: ColdReceipt, destination: Path) -> None:
        decrypt_file(self.root / receipt.object_identifier, destination, self.key)
        await asyncio.sleep(0)

    async def available(self) -> bool:
        return self.root.is_dir() and os.access(self.root, os.W_OK)
