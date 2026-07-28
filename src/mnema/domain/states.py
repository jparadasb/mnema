from __future__ import annotations

from enum import StrEnum


class ArchiveState(StrEnum):
    DISCOVERED = "DISCOVERED"
    INELIGIBLE = "INELIGIBLE"
    ELIGIBLE = "ELIGIBLE"
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    LOCAL_STAGED = "LOCAL_STAGED"
    LOCAL_VERIFIED = "LOCAL_VERIFIED"
    LOCAL_COMMITTED = "LOCAL_COMMITTED"
    LOCAL_BACKUP_PENDING = "LOCAL_BACKUP_PENDING"
    LOCAL_BACKUP_VERIFIED = "LOCAL_BACKUP_VERIFIED"
    COLD_UPLOAD_PENDING = "COLD_UPLOAD_PENDING"
    COLD_UPLOADED = "COLD_UPLOADED"
    COLD_VERIFIED = "COLD_VERIFIED"
    QUARANTINED = "QUARANTINED"
    READY_FOR_REVALIDATION = "READY_FOR_REVALIDATION"
    READY_FOR_DELETION = "READY_FOR_DELETION"
    DELETING = "DELETING"
    DELETED_FROM_SOURCE = "DELETED_FROM_SOURCE"
    ARCHIVED = "ARCHIVED"
    RESTORE_REQUESTED = "RESTORE_REQUESTED"
    RESTORING = "RESTORING"
    RESTORED = "RESTORED"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class InvalidTransition(ValueError):
    pass


_LINEAR: tuple[ArchiveState, ...] = (
    ArchiveState.DISCOVERED,
    ArchiveState.ELIGIBLE,
    ArchiveState.QUEUED,
    ArchiveState.DOWNLOADING,
    ArchiveState.LOCAL_STAGED,
    ArchiveState.LOCAL_VERIFIED,
    ArchiveState.LOCAL_COMMITTED,
    ArchiveState.LOCAL_BACKUP_PENDING,
    ArchiveState.LOCAL_BACKUP_VERIFIED,
    ArchiveState.COLD_UPLOAD_PENDING,
    ArchiveState.COLD_UPLOADED,
    ArchiveState.COLD_VERIFIED,
    ArchiveState.QUARANTINED,
    ArchiveState.READY_FOR_REVALIDATION,
    ArchiveState.READY_FOR_DELETION,
    ArchiveState.DELETING,
    ArchiveState.DELETED_FROM_SOURCE,
    ArchiveState.ARCHIVED,
)

ALLOWED_TRANSITIONS: dict[ArchiveState, frozenset[ArchiveState]] = {
    state: frozenset({_LINEAR[index + 1]}) for index, state in enumerate(_LINEAR[:-1])
}
ALLOWED_TRANSITIONS.update(
    {
        ArchiveState.DISCOVERED: frozenset({ArchiveState.ELIGIBLE, ArchiveState.INELIGIBLE}),
        ArchiveState.INELIGIBLE: frozenset({ArchiveState.ELIGIBLE, ArchiveState.INELIGIBLE}),
        ArchiveState.SOURCE_CHANGED: frozenset({ArchiveState.QUEUED}),
        ArchiveState.FAILED_RETRYABLE: frozenset({ArchiveState.QUEUED, ArchiveState.MANUAL_REVIEW}),
        ArchiveState.MANUAL_REVIEW: frozenset({ArchiveState.QUEUED}),
        ArchiveState.ARCHIVED: frozenset({ArchiveState.RESTORE_REQUESTED}),
        ArchiveState.RESTORE_REQUESTED: frozenset({ArchiveState.RESTORING}),
        ArchiveState.RESTORING: frozenset({ArchiveState.RESTORED, ArchiveState.FAILED_RETRYABLE}),
        ArchiveState.RESTORED: frozenset({ArchiveState.ARCHIVED}),
    }
)

_FAILURE_TARGETS = frozenset(
    {
        ArchiveState.FAILED_RETRYABLE,
        ArchiveState.MANUAL_REVIEW,
        ArchiveState.SOURCE_CHANGED,
    }
)


def validate_transition(current: ArchiveState, target: ArchiveState) -> None:
    if target == current:
        return
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset()) | _FAILURE_TARGETS
    if target not in allowed:
        raise InvalidTransition(f"invalid archive transition: {current} -> {target}")
