from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ColdReceipt:
    provider: str
    bucket: str
    object_identifier: str
    encryption_mode: str
    remote_size: int
    remote_checksum: str | None


class ColdStorage(Protocol):
    async def upload(
        self,
        source: Path,
        object_identifier: str,
        idempotency_key: str,
    ) -> ColdReceipt: ...

    async def verify(self, receipt: ColdReceipt, expected_sha256: str) -> bool: ...

    async def restore(self, receipt: ColdReceipt, destination: Path) -> None: ...

    async def available(self) -> bool: ...
