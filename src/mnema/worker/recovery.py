from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from mnema.domain.states import ArchiveState
from mnema.jobs import Database
from mnema.jobs.models import (
    ArchiveItem,
    AuditEvent,
    FileProviderUpload,
    FileProviderUploadStatus,
)
from mnema.jobs.state_service import transition_item

# Interrupted here, the transfer itself is unfinished and the staged partial is
# untrustworthy, so the item goes back for another attempt from the source.
RETRY_AFTER_RESTART = frozenset(
    {
        ArchiveState.DOWNLOADING,
        ArchiveState.RESTORING,
    }
)

# Interrupted here, the active copy is already committed and hash-verified.
# These steps are individually idempotent and re-verify their own results, so
# the item resumes at the step it stopped on. Rewinding it to DOWNLOADING would
# discard verified work and, for items with no re-readable source, strand them
# permanently.
RESUME_IN_PLACE = frozenset(
    {
        ArchiveState.LOCAL_BACKUP_PENDING,
        ArchiveState.COLD_UPLOAD_PENDING,
        ArchiveState.COLD_ARCHIVE_PENDING,
    }
)

_LIVE_UPLOAD_STATES = frozenset(
    {
        FileProviderUploadStatus.OPEN,
        FileProviderUploadStatus.SEALING,
    }
)


def _awaiting_client_upload(session: Session, item: ArchiveItem) -> bool:
    """True when the item's bytes arrive over HTTP rather than from a source.

    Chunked uploads are resumable from their recorded offset, so touching the
    archive item would abandon bytes the phone could still finish sending — and
    there is no source to re-download them from afterwards.
    """
    if item.source_provider != "file_provider_upload":
        return False
    upload = session.scalar(
        select(FileProviderUpload).where(FileProviderUpload.archive_item_id == item.id)
    )
    return upload is not None and upload.status in _LIVE_UPLOAD_STATES


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
        states.update(RESUME_IN_PLACE)
    with database.session() as session:
        items = session.scalars(select(ArchiveItem).where(ArchiveItem.state.in_(states))).all()
        reconciled = 0
        for item in items:
            if item.state == ArchiveState.DELETING:
                transition_item(
                    session,
                    item,
                    ArchiveState.MANUAL_REVIEW,
                    actor="startup-recovery",
                    details={"reason": "deletion result unknown after restart"},
                )
                reconciled += 1
                continue
            if item.state in RESUME_IN_PLACE:
                # Keep the state; record that the external result is unconfirmed
                # so the audit trail still shows the interruption.
                session.add(
                    AuditEvent(
                        archive_item_id=item.id,
                        event_type="interrupted_step_resumed",
                        from_state=item.state.value,
                        to_state=item.state.value,
                        actor="startup-recovery",
                        details={
                            "reason": "step interrupted by restart; resuming in place",
                        },
                    )
                )
                reconciled += 1
                continue
            if _awaiting_client_upload(session, item):
                # The upload is still resumable; leave it to the upload lifecycle.
                continue
            transition_item(
                session,
                item,
                ArchiveState.FAILED_RETRYABLE,
                actor="startup-recovery",
                details={"reason": "transfer interrupted by restart; retrying from source"},
            )
            reconciled += 1
        return reconciled
