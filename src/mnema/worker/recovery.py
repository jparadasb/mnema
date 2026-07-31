from __future__ import annotations

from sqlalchemy import select

from mnema.domain.states import ArchiveState
from mnema.jobs import Database
from mnema.jobs.models import ArchiveItem
from mnema.jobs.state_service import transition_item

RETRY_AFTER_RESTART = frozenset(
    {
        ArchiveState.DOWNLOADING,
        ArchiveState.LOCAL_BACKUP_PENDING,
        ArchiveState.COLD_UPLOAD_PENDING,
        ArchiveState.COLD_ARCHIVE_PENDING,
        ArchiveState.RESTORING,
    }
)


def reconcile_interrupted_deletions(database: Database) -> int:
    return reconcile_interrupted_items(database, only_deletions=True)


def reconcile_interrupted_items(
    database: Database,
    *,
    only_deletions: bool = False,
) -> int:
    states = {ArchiveState.DELETING}
    if not only_deletions:
        states.update(RETRY_AFTER_RESTART)
    with database.session() as session:
        items = session.scalars(select(ArchiveItem).where(ArchiveItem.state.in_(states))).all()
        for item in items:
            deletion_ambiguous = item.state == ArchiveState.DELETING
            transition_item(
                session,
                item,
                (
                    ArchiveState.MANUAL_REVIEW
                    if deletion_ambiguous
                    else ArchiveState.FAILED_RETRYABLE
                ),
                actor="startup-recovery",
                details={
                    "reason": (
                        "deletion result unknown after restart"
                        if deletion_ambiguous
                        else "operation interrupted by restart; external completion unconfirmed"
                    )
                },
            )
        return len(items)
