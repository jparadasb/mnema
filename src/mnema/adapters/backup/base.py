from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class BackupReceipt:
    snapshot_id: str


class VersionedBackup(Protocol):
    async def snapshot(self, source: Path, idempotency_key: str) -> BackupReceipt: ...

    async def verify(self, receipt: BackupReceipt, expected_sha256: str) -> bool: ...

    async def restore(self, receipt: BackupReceipt, destination: Path) -> None: ...
