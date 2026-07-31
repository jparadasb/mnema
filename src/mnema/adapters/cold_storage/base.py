from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ColdRestorePending(RuntimeError):
    """Raised after an asynchronous archive restore is requested or remains pending."""

    def __init__(self, message: str, *, requested: bool) -> None:
        super().__init__(message)
        self.requested = requested


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

    async def archive_verified(self, receipt: ColdReceipt) -> None: ...

    async def restore(self, receipt: ColdReceipt, destination: Path) -> None: ...

    async def available(self) -> bool: ...
