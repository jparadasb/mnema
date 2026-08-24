from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from mnema.adapters.sources.local import LocalFilesystemSourceAdapter
from mnema.domain.source import AuthenticationStatus, DeleteReceipt, DiscoveryPage, SourceObject


def icloud_session_status(
    session_directory: Path,
    *,
    last_success: datetime | None,
    last_result: str | None,
    stale_after: timedelta = timedelta(days=2),
    now: datetime | None = None,
) -> AuthenticationStatus:
    """Derive iCloud session health from evidence, not from a directory existing.

    Three distinct states used to collapse into one optimistic answer: no
    session at all, a session whose last import failed, and a session that has
    simply not run recently.
    """
    now = now or datetime.now(UTC)
    present = session_directory.is_dir() and any(
        path.is_file() and not path.is_symlink() for path in session_directory.rglob("*")
    )
    if not present:
        return AuthenticationStatus(False, "iCloud authentication required")
    if last_result == "failed":
        return AuthenticationStatus(
            False, "last iCloud import failed; reauthentication may be required"
        )
    if last_success is None:
        return AuthenticationStatus(False, "iCloud session has never completed an import")
    aware = last_success if last_success.tzinfo else last_success.replace(tzinfo=UTC)
    if now - aware > stale_after:
        return AuthenticationStatus(False, f"no successful iCloud import since {aware.isoformat()}")
    return AuthenticationStatus(True, f"last iCloud import succeeded at {aware.isoformat()}")


class ICloudDriveSourceAdapter:
    """Future boundary. No authentication, transfer, or deletion is implemented."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError("iCloud Drive support is not implemented")


class ICloudPhotosSourceAdapter(LocalFilesystemSourceAdapter):
    """Read-only view of originals downloaded into Mnema active storage."""

    def __init__(
        self,
        root: Path,
        session_directory: Path,
        *,
        page_size: int = 100,
        last_success: datetime | None = None,
        last_result: str | None = None,
        stale_after: timedelta = timedelta(days=2),
    ) -> None:
        super().__init__(root, page_size=page_size, allow_delete=False)
        self.session_directory = session_directory
        self.last_success = last_success
        self.last_result = last_result
        self.stale_after = stale_after

    async def authentication_status(self) -> AuthenticationStatus:
        """Report whether imports are actually working.

        Apple's web sessions expire on their own schedule. Treating the mere
        presence of a cookie file as proof of authentication reported a healthy
        session for as long as the directory survived, while every scheduled
        import failed unnoticed.
        """
        return icloud_session_status(
            self.session_directory,
            last_success=self.last_success,
            last_result=self.last_result,
            stale_after=self.stale_after,
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
