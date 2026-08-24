from datetime import UTC, datetime, timedelta

from mnema.domain.states import ArchiveState
from mnema.jobs import Database
from mnema.jobs.models import (
    ArchiveItem,
    AuditEvent,
    FileProviderItem,
    FileProviderItemKind,
    FileProviderItemStatus,
    FileProviderUpload,
    FileProviderUploadStatus,
)
from mnema.worker.recovery import reconcile_interrupted_deletions, reconcile_interrupted_items


def _item(
    state: ArchiveState, *, provider: str = "local_test", name: str | None = None
) -> ArchiveItem:
    label = name or state.value
    return ArchiveItem(
        source_provider=provider,
        source_identifier=label,
        original_path=f"{label}.bin",
        original_size=1,
        original_modified_at=datetime.now(UTC),
        source_version="v1",
        state=state,
    )


def test_unknown_delete_after_restart_enters_manual_review() -> None:
    database = Database("sqlite://")
    database.create_schema()
    with database.session() as session:
        item = _item(ArchiveState.DELETING)
        session.add(item)
    assert reconcile_interrupted_deletions(database) == 1
    with database.session() as session:
        recovered = session.get(ArchiveItem, item.id)
        assert recovered and recovered.state == ArchiveState.MANUAL_REVIEW


def test_interrupted_transfers_retry_from_source() -> None:
    database = Database("sqlite://")
    database.create_schema()
    with database.session() as session:
        items = [_item(state) for state in (ArchiveState.DOWNLOADING, ArchiveState.RESTORING)]
        session.add_all(items)
    assert reconcile_interrupted_items(database) == 2
    with database.session() as session:
        recovered = [session.get(ArchiveItem, item.id) for item in items]
        assert all(item and item.state == ArchiveState.FAILED_RETRYABLE for item in recovered)


def test_committed_work_resumes_in_place_after_restart() -> None:
    """A verified active copy must not be rewound to a re-download.

    Rewinding discarded verified work and, for items whose bytes arrived over
    HTTP, stranded them permanently because no source could supply them again.
    """
    database = Database("sqlite://")
    database.create_schema()
    resumable = (
        ArchiveState.LOCAL_BACKUP_PENDING,
        ArchiveState.COLD_UPLOAD_PENDING,
        ArchiveState.COLD_ARCHIVE_PENDING,
    )
    with database.session() as session:
        items = [_item(state) for state in resumable]
        session.add_all(items)
    assert reconcile_interrupted_items(database) == len(resumable)
    with database.session() as session:
        for item, state in zip(items, resumable, strict=True):
            recovered = session.get(ArchiveItem, item.id)
            assert recovered and recovered.state == state
        events = session.query(AuditEvent).all()
        assert len(events) == len(resumable)
        assert all(event.event_type == "interrupted_step_resumed" for event in events)
        assert all(event.actor == "startup-recovery" for event in events)


def test_resumable_upload_is_left_alone() -> None:
    """An open chunked upload is resumable from its offset; recovery must not touch it."""
    database = Database("sqlite://")
    database.create_schema()
    with database.session() as session:
        archive = _item(ArchiveState.DOWNLOADING, provider="file_provider_upload", name="upload")
        session.add(archive)
        session.flush()
        session.add(
            FileProviderItem(
                id="fp-1",
                parent_id="inbox",
                name="clip.mov",
                kind=FileProviderItemKind.FILE,
                status=FileProviderItemStatus.PROCESSING,
                archive_item_id=archive.id,
            )
        )
        session.add(
            FileProviderUpload(
                id="upload-1",
                item_id="fp-1",
                archive_item_id=archive.id,
                expected_size=100,
                received_size=50,
                status=FileProviderUploadStatus.OPEN,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    assert reconcile_interrupted_items(database) == 0
    with database.session() as session:
        recovered = session.get(ArchiveItem, archive.id)
        assert recovered and recovered.state == ArchiveState.DOWNLOADING
