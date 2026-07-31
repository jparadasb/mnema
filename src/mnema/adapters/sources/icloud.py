from __future__ import annotations

from pathlib import Path

from mnema.adapters.sources.local import LocalFilesystemSourceAdapter
from mnema.domain.source import AuthenticationStatus, DeleteReceipt, DiscoveryPage, SourceObject


class ICloudDriveSourceAdapter:
    """Future boundary. No authentication, transfer, or deletion is implemented."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError("iCloud Drive support is not implemented")


class ICloudPhotosSourceAdapter(LocalFilesystemSourceAdapter):
    """Read-only view of originals downloaded into Mnema active storage."""

    def __init__(self, root: Path, session_directory: Path, *, page_size: int = 100) -> None:
        super().__init__(root, page_size=page_size, allow_delete=False)
        self.session_directory = session_directory

    async def authentication_status(self) -> AuthenticationStatus:
        authenticated = self.session_directory.is_dir() and any(
            path.is_file() and not path.is_symlink() for path in self.session_directory.rglob("*")
        )
        return AuthenticationStatus(
            authenticated,
            "iCloud session present" if authenticated else "iCloud authentication required",
        )

    async def discover(self, cursor: str | None = None) -> DiscoveryPage:
        incomplete_suffixes = {".icloud", ".part", ".partial", ".tmp"}
        files = sorted(
            path
            for path in self.root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and not any(part.startswith(".") for part in path.relative_to(self.root).parts)
            and path.suffix.lower() not in incomplete_suffixes
        )
        start = int(cursor or "0")
        page = files[start : start + self.page_size]
        objects = tuple(self._object(path) for path in page)
        next_index = start + len(page)
        next_cursor = str(next_index) if next_index < len(files) else None
        return DiscoveryPage(objects, next_cursor)

    def _object(self, path: Path) -> SourceObject:
        item = super()._object(path)
        return SourceObject(
            source_id=item.source_id,
            relative_path=f"iCloud Photos/{item.relative_path}",
            size=item.size,
            modified_at=item.modified_at,
            version=item.version,
        )

    async def delete(self, source_id: str, expected_version: str) -> DeleteReceipt:
        del source_id, expected_version
        raise PermissionError("iCloud Photos deletion is not implemented")
