"""Regressions for the critical audit findings.

Each test names the finding it pins and fails against the pre-fix behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pytest

from mnema.adapters.cold_storage.base import ColdReceipt
from mnema.adapters.cold_storage.local import LocalEncryptedColdStorage
from mnema.adapters.nas.fileops import InsufficientScratchSpace, scratch_directory
from mnema.adapters.sources.icloud import icloud_session_status
from mnema.config import Settings
from mnema.domain.states import ArchiveState
from mnema.file_provider.service import (
    UPLOADS_ID,
    bootstrap_roots,
    create_upload,
    mark_upload_failed,
    promote_upload,
    reap_expired_uploads,
    unique_child_name,
    upload_path,
)
from mnema.jobs import Database, DurableQueue
from mnema.jobs.models import (
    ArchiveItem,
    FileProviderItem,
    FileProviderItemKind,
    FileProviderItemStatus,
    FileProviderUpload,
    FileProviderUploadStatus,
    JobStatus,
)


def _settings(tmp_path: Path) -> Settings:
    active = tmp_path / "active"
    staging = active / ".mnema-staging"
    for path in (active, staging):
        path.mkdir(parents=True, exist_ok=True)
    return Settings(
        database_url="sqlite://",
        active_root=active,
        staging_root=staging,
        backup_root=tmp_path / "backup",
        source_root=tmp_path / "source",
        file_provider_upload_root=active / ".mnema-file-provider",
    )


def _archive(state: ArchiveState, *, name: str = "clip.mov") -> ArchiveItem:
    return ArchiveItem(
        source_provider="file_provider_upload",
        source_identifier=name,
        original_path=f"Uploads/{name}",
        original_size=10,
        original_modified_at=datetime.now(UTC),
        source_version="v1",
        state=state,
        cold_archived_at=datetime.now(UTC),
    )


# --- F-01: scratch space -----------------------------------------------------


def test_scratch_directory_uses_the_configured_root(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    with scratch_directory(root, "probe-") as directory:
        assert directory.is_dir()
        assert directory.parent == root
    assert not directory.exists()


def test_scratch_directory_refuses_work_it_cannot_hold(tmp_path: Path) -> None:
    """A late ENOSPC used to surface as an ordinary retryable error."""
    with pytest.raises(InsufficientScratchSpace, match="required"):
        with scratch_directory(tmp_path / "scratch", "probe-", required_bytes=1 << 62):
            pass


def test_cold_storage_verification_stages_in_the_configured_scratch(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    storage = LocalEncryptedColdStorage(tmp_path / "cold", b"k" * 32, scratch_root=scratch)
    assert storage.scratch_root == scratch


def test_scratch_preflight_reports_the_shortfall(tmp_path: Path) -> None:
    receipt = ColdReceipt("local-test", "b", "o", "AES-256-GCM", 1 << 62, None)
    storage = LocalEncryptedColdStorage(tmp_path / "cold", b"k" * 32, scratch_root=tmp_path / "s")
    with pytest.raises(InsufficientScratchSpace):
        # verify() reserves the ciphertext and the plaintext at once.
        import asyncio

        asyncio.run(storage.verify(receipt, "0" * 64))


def test_upload_reserves_space_for_its_verification_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reserving only the staged copy accepted uploads that could never archive."""
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    database.create_schema()

    class Usage(NamedTuple):
        total: int
        used: int
        free: int

    monkeypatch.setattr(
        "mnema.file_provider.service.shutil.disk_usage",
        lambda _path: Usage(total=1000, used=500, free=500),
    )
    with database.session() as session:
        # 200 fits the staged copy on its own (300 free, 30%) but not the staged
        # copy plus the encrypt/verify scratch peak.
        with pytest.raises(OSError, match="verification scratch"):
            create_upload(
                session,
                settings,
                name="too-big.bin",
                size=200,
                content_type="application/octet-stream",
                sha256=None,
            )
        # 100 leaves room for all three copies and is still accepted.
        assert create_upload(
            session,
            settings,
            name="fits.bin",
            size=100,
            content_type="application/octet-stream",
            sha256=None,
        )


# --- F-03: lease loss must not kill the worker -------------------------------


def test_fail_records_the_error_without_raising_on_a_lost_lease() -> None:
    """fail() runs inside an exception handler; raising there killed the worker."""
    database = Database("sqlite://")
    database.create_schema()
    queue = DurableQueue()
    with database.session() as session:
        job = queue.enqueue(
            session, kind="archive", adapter="local", payload={}, idempotency_key="k"
        )
        leased = queue.lease(session, worker_id="w", adapter="local")
        assert leased is not None
        # Another process reclaims the lease mid-flight.
        leased.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert queue.recover_expired(session) == 1
        assert queue.fail(session, job, "w", "boom") is False
        assert job.last_error == "boom"


def test_succeed_reports_a_lost_lease_without_raising() -> None:
    database = Database("sqlite://")
    database.create_schema()
    queue = DurableQueue()
    with database.session() as session:
        job = queue.enqueue(
            session, kind="archive", adapter="local", payload={}, idempotency_key="k"
        )
        leased = queue.lease(session, worker_id="w", adapter="local")
        assert leased is not None
        leased.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert queue.recover_expired(session) == 1
        assert queue.succeed(session, job, "w") is False
        assert job.status is JobStatus.RETRY


def test_owning_worker_still_completes_normally() -> None:
    database = Database("sqlite://")
    database.create_schema()
    queue = DurableQueue()
    with database.session() as session:
        job = queue.enqueue(
            session, kind="archive", adapter="local", payload={}, idempotency_key="k"
        )
        assert queue.lease(session, worker_id="w", adapter="local") is not None
        assert queue.succeed(session, job, "w") is True
        assert job.status is JobStatus.SUCCEEDED


def test_diagnostics_do_not_mutate_the_queue_by_default(tmp_path: Path) -> None:
    """`mnema diagnostics` reclaimed the lease of a job the worker was running."""
    from mnema.diagnostics import startup_checks

    settings = _settings(tmp_path)
    (tmp_path / "backup").mkdir(exist_ok=True)
    database = Database("sqlite://")
    database.create_schema()
    queue = DurableQueue()
    with database.session() as session:
        queue.enqueue(session, kind="a", adapter="local", payload={}, idempotency_key="k")
        leased = queue.lease(session, worker_id="w", adapter="local")
        assert leased is not None
        leased.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    health = startup_checks(
        database, settings.active_root, tmp_path / "backup", settings.staging_root
    )
    assert health.expired_jobs_recovered == 0


# --- F-17: promotion must not collide ---------------------------------------


def test_unique_child_name_resolves_collisions_deterministically() -> None:
    database = Database("sqlite://")
    database.create_schema()
    with database.session() as session:
        bootstrap_roots(session)
        session.add(
            FileProviderItem(
                id="existing",
                parent_id=UPLOADS_ID,
                name="clip.mov",
                kind=FileProviderItemKind.FILE,
                status=FileProviderItemStatus.READY,
            )
        )
        session.flush()
        first = unique_child_name(session, UPLOADS_ID, "clip.mov", item_id="abcdef1234")
        assert first != "clip.mov"
        assert first.endswith(".mov")
        # Deterministic: the same item resolves to the same name on retry.
        assert unique_child_name(session, UPLOADS_ID, "clip.mov", item_id="abcdef1234") == first
        # The holder of the name keeps it.
        assert unique_child_name(session, UPLOADS_ID, "clip.mov", item_id="existing") == "clip.mov"


def test_promoting_a_duplicate_name_does_not_fail_the_upload() -> None:
    """A same-named upload used to die on the unique index after five retries."""
    database = Database("sqlite://")
    database.create_schema()
    with database.session() as session:
        bootstrap_roots(session)
        first = _archive(ArchiveState.QUARANTINED)
        second = _archive(ArchiveState.QUARANTINED, name="clip-2.mov")
        session.add_all([first, second])
        session.flush()
        for index, archive in enumerate((first, second)):
            session.add(
                FileProviderItem(
                    id=f"fp-{index}",
                    parent_id="inbox",
                    name="clip.mov" if index == 0 else "clip.mov ",
                    kind=FileProviderItemKind.FILE,
                    status=FileProviderItemStatus.PROCESSING,
                    archive_item_id=archive.id,
                )
            )
        session.flush()
        promote_upload(session, first, "fp-0")
        promote_upload(session, second, "fp-1")
        session.flush()
        promoted = [session.get(FileProviderItem, f"fp-{i}") for i in range(2)]
        assert all(item and item.status is FileProviderItemStatus.READY for item in promoted)
        assert all(item and item.parent_id == UPLOADS_ID for item in promoted)
        assert promoted[0] and promoted[1] and promoted[0].name != promoted[1].name


def test_failure_codes_do_not_leak_internals() -> None:
    database = Database("sqlite://")
    database.create_schema()
    with database.session() as session:
        bootstrap_roots(session)
        session.add(
            FileProviderItem(
                id="fp",
                parent_id="inbox",
                name="clip.mov",
                kind=FileProviderItemKind.FILE,
                status=FileProviderItemStatus.PROCESSING,
            )
        )
        session.flush()
        mark_upload_failed(session, "fp", "archive_failed:job-7")
        item = session.get(FileProviderItem, "fp")
        assert item and item.error_message == "archive_failed:job-7"
        assert item.error_message is not None
        assert "sqlalchemy" not in item.error_message.lower()


# --- F-18: expired uploads must be reclaimed --------------------------------


def test_expired_uploads_release_their_staged_bytes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'reap.sqlite'}")
    database.create_schema()
    with database.session() as session:
        bootstrap_roots(session)
        archive = _archive(ArchiveState.DOWNLOADING)
        archive.cold_archived_at = None
        session.add(archive)
        session.flush()
        session.add(
            FileProviderItem(
                id="fp",
                parent_id="inbox",
                name="clip.mov",
                kind=FileProviderItemKind.FILE,
                status=FileProviderItemStatus.PROCESSING,
                archive_item_id=archive.id,
            )
        )
        session.add(
            FileProviderUpload(
                id="11111111-1111-1111-1111-111111111111",
                item_id="fp",
                archive_item_id=archive.id,
                expected_size=100,
                received_size=8,
                status=FileProviderUploadStatus.OPEN,
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        archive_id = archive.id
    staged = upload_path(settings, "11111111-1111-1111-1111-111111111111")
    staged.write_bytes(b"partial")

    assert reap_expired_uploads(database, settings) == 1
    assert not staged.exists()
    with database.session() as session:
        item = session.get(FileProviderItem, "fp")
        assert item and item.status is FileProviderItemStatus.FAILED
        assert item.error_message == "upload_expired"
        assert session.get(ArchiveItem, archive_id).state is ArchiveState.MANUAL_REVIEW


def test_live_uploads_survive_the_reaper(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'live.sqlite'}")
    database.create_schema()
    with database.session() as session:
        bootstrap_roots(session)
        archive = _archive(ArchiveState.DOWNLOADING)
        session.add(archive)
        session.flush()
        session.add(
            FileProviderItem(
                id="fp",
                parent_id="inbox",
                name="clip.mov",
                kind=FileProviderItemKind.FILE,
                status=FileProviderItemStatus.PROCESSING,
                archive_item_id=archive.id,
            )
        )
        session.add(
            FileProviderUpload(
                id="22222222-2222-2222-2222-222222222222",
                item_id="fp",
                archive_item_id=archive.id,
                expected_size=100,
                received_size=8,
                status=FileProviderUploadStatus.OPEN,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    assert reap_expired_uploads(database, settings) == 0


# --- F-05: iCloud session honesty -------------------------------------------


def test_session_directory_alone_is_not_authentication(tmp_path: Path) -> None:
    """A surviving cookie directory reported a healthy session indefinitely."""
    session_dir = tmp_path / "icloud-session"
    session_dir.mkdir()
    (session_dir / "cookies").write_text("stale")

    never_ran = icloud_session_status(session_dir, last_success=None, last_result=None)
    assert not never_ran.authenticated

    failed = icloud_session_status(
        session_dir, last_success=datetime.now(UTC), last_result="failed"
    )
    assert not failed.authenticated
    assert "reauthentication" in failed.detail

    stale = icloud_session_status(
        session_dir,
        last_success=datetime.now(UTC) - timedelta(days=22),
        last_result="succeeded",
    )
    assert not stale.authenticated
    assert "no successful iCloud import since" in stale.detail

    healthy = icloud_session_status(
        session_dir, last_success=datetime.now(UTC), last_result="succeeded"
    )
    assert healthy.authenticated


def test_missing_session_is_unauthenticated(tmp_path: Path) -> None:
    status = icloud_session_status(
        tmp_path / "absent", last_success=datetime.now(UTC), last_result="succeeded"
    )
    assert not status.authenticated
