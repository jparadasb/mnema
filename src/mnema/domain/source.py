from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SourceCapabilities:
    can_delete: bool
    supports_cursor: bool
    stable_versions: bool


@dataclass(frozen=True)
class AuthenticationStatus:
    authenticated: bool
    detail: str


@dataclass(frozen=True)
class SourceObject:
    source_id: str
    relative_path: str
    size: int
    modified_at: datetime
    version: str


@dataclass(frozen=True)
class DiscoveryPage:
    objects: tuple[SourceObject, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class DownloadReceipt:
    source_id: str
    bytes_written: int
    sha256: str
    version: str


@dataclass(frozen=True)
class DeleteReceipt:
    source_id: str
    expected_version: str
    confirmed_absent: bool


class SourceAdapter(Protocol):
    async def capabilities(self) -> SourceCapabilities: ...

    async def authentication_status(self) -> AuthenticationStatus: ...

    async def discover(self, cursor: str | None = None) -> DiscoveryPage: ...

    async def stat(self, source_id: str) -> SourceObject: ...

    async def download(self, source_id: str, destination: Path) -> DownloadReceipt: ...

    async def delete(self, source_id: str, expected_version: str) -> DeleteReceipt: ...
