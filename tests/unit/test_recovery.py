from datetime import UTC, datetime

from mnema.domain.states import ArchiveState
from mnema.jobs import Database
from mnema.jobs.models import ArchiveItem, AuditEvent
from mnema.worker.recovery import reconcile_interrupted_deletions, reconcile_interrupted_items


def test_unknown_delete_after_restart_enters_manual_review() -> None:
    database = Database("sqlite://")
    database.create_schema()
    with database.session() as session:
        item = ArchiveItem(
            source_provider="local_test",
            source_identifier="file",
            original_path="file",
            original_size=1,
            original_modified_at=datetime.now(UTC),
            source_version="v1",
            state=ArchiveState.DELETING,
        )
        session.add(item)
    assert reconcile_interrupted_deletions(database) == 1
    with database.session() as session:
        assert session.get(ArchiveItem, item.id).state == ArchiveState.MANUAL_REVIEW


def test_interrupted_operations_become_retryable_after_restart() -> None:
    database = Database("sqlite://")
    database.create_schema()
    with database.session() as session:
        items = [
            ArchiveItem(
                source_provider="local_test",
                source_identifier=state.value,
                original_path=f"{state.value}.bin",
                original_size=1,
                original_modified_at=datetime.now(UTC),
                source_version="v1",
                state=state,
            )
            for state in (
                ArchiveState.DOWNLOADING,
                ArchiveState.LOCAL_BACKUP_PENDING,
                ArchiveState.COLD_UPLOAD_PENDING,
                ArchiveState.RESTORING,
            )
        ]
        session.add_all(items)
    assert reconcile_interrupted_items(database) == 4
    with database.session() as session:
        recovered = [session.get(ArchiveItem, item.id) for item in items]
        assert all(item and item.state == ArchiveState.FAILED_RETRYABLE for item in recovered)
        events = session.query(AuditEvent).all()
        assert len(events) == 4
        assert all(event.actor == "startup-recovery" for event in events)
