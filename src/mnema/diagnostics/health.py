from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from mnema.diagnostics.smart import smart_report_healthy
from mnema.domain.storage import storage_identity, storage_is_separate
from mnema.jobs import Database, DurableQueue


@dataclass(frozen=True)
class StorageHealth:
    path: Path
    exists: bool
    writable: bool
    free_percent: float

    @property
    def healthy(self) -> bool:
        return self.exists and self.writable and self.free_percent > 0


@dataclass(frozen=True)
class StartupHealth:
    active: StorageHealth
    backup: StorageHealth
    devices_differ: bool
    staging_shares_active_device: bool
    sqlite_healthy: bool
    expired_jobs_recovered: int
    partial_files: tuple[Path, ...]
    smart_healthy: bool | None = None
    smart_required: bool = False

    @property
    def healthy(self) -> bool:
        return (
            self.active.healthy
            and self.backup.healthy
            and self.devices_differ
            and self.staging_shares_active_device
            and self.sqlite_healthy
            and (self.smart_healthy is True if self.smart_required else True)
        )


def storage_health(path: Path) -> StorageHealth:
    if not path.is_dir():
        return StorageHealth(path, False, False, 0)
    usage = shutil.disk_usage(path)
    return StorageHealth(
        path=path,
        exists=True,
        writable=os.access(path, os.W_OK),
        free_percent=(usage.free / usage.total * 100) if usage.total else 0,
    )


def startup_checks(
    database: Database,
    active_root: Path,
    backup_root: Path,
    staging_root: Path,
    *,
    smart_health_file: Path | None = None,
    require_smart_health: bool = False,
    recover_expired_jobs: bool = True,
) -> StartupHealth:
    active = storage_health(active_root)
    backup = storage_health(backup_root)
    devices_differ = False
    staging_shares_active_device = False
    if active.exists and backup.exists:
        devices_differ = storage_is_separate(
            storage_identity(active_root),
            storage_identity(backup_root),
        )
    if active.exists and staging_root.is_dir():
        staging_shares_active_device = (
            storage_identity(active_root).device_id == storage_identity(staging_root).device_id
        )
    recovered = 0
    if recover_expired_jobs:
        with database.session() as session:
            recovered = DurableQueue().recover_expired(session)
    partials = (
        tuple(
            path
            for path in staging_root.iterdir()
            if path.is_file() and path.name.endswith(".partial")
        )
        if staging_root.is_dir()
        else ()
    )
    return StartupHealth(
        active,
        backup,
        devices_differ,
        staging_shares_active_device,
        database.integrity_check(),
        recovered,
        partials,
        smart_report_healthy(smart_health_file) if smart_health_file else None,
        require_smart_health,
    )
