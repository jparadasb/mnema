import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mnema.adapters.backup.filesystem import FilesystemVersionedBackup
from mnema.adapters.cold_storage.local import LocalEncryptedColdStorage
from mnema.adapters.sources.local import LocalFilesystemSourceAdapter
from mnema.config import DeletionLimits, SourcePolicy
from mnema.domain.states import ArchiveState
from mnema.domain.workflow import ArchiveWorkflow
from mnema.jobs import Database
from mnema.policies.deletion import DeletionRunUsage


def build(tmp_path: Path, *, quarantine_days: int = 0) -> tuple[Database, ArchiveWorkflow, Path]:
    roots = {name: tmp_path / name for name in ("source", "active", "backup", "staging", "cold")}
    for path in roots.values():
        path.mkdir()
    source_file = roots["source"] / "folder" / "large.bin"
    source_file.parent.mkdir()
    source_file.write_bytes(secrets.token_bytes(3 * 1024 * 1024 + 17))
    old = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(source_file, (old, old))
    database = Database(f"sqlite:///{tmp_path / 'mnema.sqlite'}")
    database.create_schema()
    workflow = ArchiveWorkflow(
        source=LocalFilesystemSourceAdapter(roots["source"], allow_delete=True),
        backup=FilesystemVersionedBackup(roots["backup"]),
        cold=LocalEncryptedColdStorage(roots["cold"], secrets.token_bytes(32)),
        active_root=roots["active"],
        staging_root=roots["staging"],
        policy=SourcePolicy(
            archive_after_days=0,
            stability_window_hours=0,
            quarantine_days=quarantine_days,
            deletion_enabled=True,
            manual_approval=False,
        ),
    )
    return database, workflow, source_file


@pytest.mark.asyncio
async def test_complete_archive_restore_and_guarded_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNEMA_ALLOW_TEST_DELETE", "1")
    database, workflow, source_file = build(tmp_path)
    original_hash = __import__("hashlib").sha256(source_file.read_bytes()).hexdigest()
    with database.session() as session:
        item = (await workflow.discover(session))[0]
        await workflow.archive(session, item)
        assert item.state == ArchiveState.QUARANTINED
        assert item.plaintext_sha256 == original_hash
        assert item.kopia_verified_at and item.remote_verified_at
        local_restore = tmp_path / "local.restore"
        remote_restore = tmp_path / "remote.restore"
        assert await workflow.restore_local(item, local_restore)
        assert await workflow.restore_remote(item, remote_restore)
        decision = await workflow.deletion_decision(
            item,
            active_disk_healthy=True,
            backup_disk_healthy=True,
            storage_devices_differ=True,
            sqlite_integrity_healthy=True,
            global_deletion_enabled=True,
            safety_lock=False,
            usage=DeletionRunUsage(0, 0, 100),
            limits=DeletionLimits(max_percentage_deleted_per_run=100),
        )
        assert decision.allowed
        await workflow.delete_test_item(session, item, decision)
        assert item.state == ArchiveState.ARCHIVED
        assert not source_file.exists()
        assert item.deletion_timestamp
        assert len(item.audit_events) >= 17


@pytest.mark.asyncio
async def test_quarantine_missing_storage_and_source_change_block(
    tmp_path: Path,
) -> None:
    database, workflow, source_file = build(tmp_path, quarantine_days=7)
    with database.session() as session:
        item = (await workflow.discover(session))[0]
        await workflow.archive(session, item)
        blocked = await workflow.deletion_decision(
            item,
            active_disk_healthy=True,
            backup_disk_healthy=False,
            storage_devices_differ=True,
            sqlite_integrity_healthy=True,
            global_deletion_enabled=True,
            safety_lock=False,
            usage=DeletionRunUsage(0, 0, 100),
            limits=DeletionLimits(max_percentage_deleted_per_run=100),
        )
        assert not blocked.allowed
        assert "backup disk unhealthy" in blocked.blockers
        source_file.write_bytes(b"changed")
        changed = await workflow.deletion_decision(
            item,
            active_disk_healthy=True,
            backup_disk_healthy=True,
            storage_devices_differ=True,
            sqlite_integrity_healthy=True,
            global_deletion_enabled=True,
            safety_lock=False,
            usage=DeletionRunUsage(0, 0, 100),
            limits=DeletionLimits(max_percentage_deleted_per_run=100),
            now=datetime.now(UTC) + timedelta(days=8),
        )
        assert not changed.allowed
        assert "source changed" in changed.blockers


@pytest.mark.asyncio
async def test_interrupted_download_preserves_partial(tmp_path: Path) -> None:
    database, workflow, _ = build(tmp_path)
    workflow.source = LocalFilesystemSourceAdapter(
        tmp_path / "source",
        interrupt_after_bytes=1024,
    )
    with database.session() as session:
        item = (await workflow.discover(session))[0]
        with pytest.raises(InterruptedError):
            await workflow.archive(session, item)
        partial = tmp_path / "staging" / f"{item.id}.partial"
        assert partial.exists()
