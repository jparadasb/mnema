from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from mnema.config import Settings
from mnema.domain.states import ArchiveState
from mnema.domain.storage import safe_relative_path
from mnema.jobs import Database, DurableQueue
from mnema.jobs.models import (
    ArchiveItem,
    FileProviderChange,
    FileProviderItem,
    FileProviderItemKind,
    FileProviderItemStatus,
    FileProviderUpload,
    FileProviderUploadStatus,
    RuntimeSetting,
    utcnow,
)
from mnema.jobs.state_service import transition_item

LOGGER = logging.getLogger("mnema.file_provider")

ROOT_ID = "root"
INBOX_ID = "inbox"
ARCHIVE_ID = "archive"
UPLOADS_ID = "archive-uploads"
ICLOUD_ID = "archive-icloud"
LOCAL_ID = "archive-local"
COLLECTIONS = {
    "file_provider_upload": (UPLOADS_ID, "Uploads"),
    "icloud_photos": (ICLOUD_ID, "iCloud Photos"),
    "local_test": (LOCAL_ID, "Local Imports"),
}
READY_STATES = {
    ArchiveState.QUARANTINED,
    ArchiveState.READY_FOR_REVALIDATION,
    ArchiveState.READY_FOR_DELETION,
    ArchiveState.ARCHIVED,
}


def _setting(session: Session, key: str) -> str:
    value = session.get(RuntimeSetting, key)
    if value is None:
        value = RuntimeSetting(key=key, value=str(uuid.uuid4()))
        session.add(value)
        session.flush()
    return value.value


def bootstrap_roots(session: Session) -> None:
    roots = (
        (ROOT_ID, None, "Mnema"),
        (INBOX_ID, ROOT_ID, "Inbox"),
        (ARCHIVE_ID, ROOT_ID, "Archive"),
        (UPLOADS_ID, ARCHIVE_ID, "Uploads"),
        (ICLOUD_ID, ARCHIVE_ID, "iCloud Photos"),
        (LOCAL_ID, ARCHIVE_ID, "Local Imports"),
    )
    for identifier, parent, name in roots:
        if session.get(FileProviderItem, identifier) is None:
            session.add(
                FileProviderItem(
                    id=identifier,
                    parent_id=parent,
                    name=name,
                    kind=FileProviderItemKind.FOLDER,
                    status=FileProviderItemStatus.READY,
                )
            )
            session.flush()
            record_change(session, identifier)
    _setting(session, "file_provider_generation")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _suffixed(name: str, suffix: str) -> str:
    path = PurePosixPath(name)
    return f"{path.stem}-{suffix}{path.suffix}"


def unique_child_name(session: Session, parent_id: str, desired: str, *, item_id: str) -> str:
    """Return a name unique among a parent's children.

    ``(parent_id, name)`` is a unique index, so moving an item into a folder
    that already holds that name raises IntegrityError. Callers used to let it
    propagate, which burned a retry attempt and eventually failed the item
    outright. Candidates are derived from the item identifier rather than a
    counter so the same item always resolves to the same name on retry.
    """
    for candidate in (desired, _suffixed(desired, item_id[:8]), _suffixed(desired, item_id)):
        clash = session.scalar(
            select(FileProviderItem.id).where(
                FileProviderItem.parent_id == parent_id,
                FileProviderItem.name == candidate,
                FileProviderItem.id != item_id,
            )
        )
        if clash is None:
            return candidate
    raise ValueError("cannot derive a unique File Provider name")


def record_change(session: Session, item_id: str, operation: str = "UPSERT") -> int:
    change = FileProviderChange(item_id=item_id, operation=operation)
    session.add(change)
    session.flush()
    item = session.get(FileProviderItem, item_id)
    if item is not None:
        item.metadata_version = change.sequence
        item.modified_at = utcnow()
    return change.sequence


def encode_cursor(generation: str, sequence: int) -> str:
    payload = json.dumps({"g": generation, "s": sequence}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        generation = payload["g"]
        sequence = payload["s"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid change cursor") from error
    if not isinstance(generation, str) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("invalid change cursor")
    return generation, sequence


def upload_path(settings: Settings, upload_id: str) -> Path:
    if not upload_id.isascii() or not upload_id.replace("-", "").isalnum():
        raise ValueError("invalid upload identifier")
    root = settings.file_provider_upload_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{upload_id}.upload"
    if not path.parent.resolve().is_relative_to(settings.active_root.resolve()):
        raise ValueError("upload path escapes active storage")
    return path


def validate_upload_name(name: str) -> str:
    candidate = name.strip()
    safe_relative_path(candidate)
    if PurePosixPath(candidate).name != candidate or candidate in {".", ".."}:
        raise ValueError("uploads must use one safe file name")
    if len(candidate.encode()) > 255:
        raise ValueError("upload name is too long")
    return candidate


def create_upload(
    session: Session,
    settings: Settings,
    *,
    name: str,
    size: int,
    content_type: str,
    sha256: str | None,
) -> FileProviderUpload:
    bootstrap_roots(session)
    name = validate_upload_name(name)
    if size < 0 or size > settings.file_provider_max_file_size:
        raise ValueError("upload size exceeds configured limit")
    usage = shutil.disk_usage(settings.active_root)
    # The staged copy, plus the encrypt/verify scratch peak that holds the
    # ciphertext and the decrypted plaintext at the same time. Reserving only
    # the staged copy accepted uploads that could never finish archiving.
    projected_free = usage.free - size * 3
    if (
        projected_free < 0
        or projected_free * 100 / usage.total < settings.file_provider_minimum_free_percent
    ):
        raise OSError("active storage cannot hold this upload and its verification scratch")
    if sha256 is not None and (
        len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256)
    ):
        raise ValueError("SHA-256 must use 64 lowercase hexadecimal characters")
    upload_id = str(uuid.uuid4())
    fp_id = str(uuid.uuid4())
    archive = ArchiveItem(
        source_provider="file_provider_upload",
        source_identifier=upload_id,
        original_path=f"Uploads/{name}",
        original_size=size,
        original_modified_at=datetime.now(UTC),
        source_version=upload_id,
        state=ArchiveState.DISCOVERED,
    )
    session.add(archive)
    session.flush()
    for target in (ArchiveState.ELIGIBLE, ArchiveState.QUEUED, ArchiveState.DOWNLOADING):
        transition_item(session, archive, target, actor="file-provider")
    item = FileProviderItem(
        id=fp_id,
        parent_id=INBOX_ID,
        name=name,
        kind=FileProviderItemKind.FILE,
        status=FileProviderItemStatus.PROCESSING,
        archive_item_id=archive.id,
        size=size,
        content_type=content_type or "application/octet-stream",
    )
    session.add(item)
    session.flush()
    upload = FileProviderUpload(
        id=upload_id,
        item_id=fp_id,
        archive_item_id=archive.id,
        expected_size=size,
        expected_sha256=sha256,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    session.add(upload)
    record_change(session, item.id)
    return upload


def seal_upload(session: Session, settings: Settings, upload: FileProviderUpload) -> ArchiveItem:
    source = upload_path(settings, upload.id)
    if upload.status != FileProviderUploadStatus.SEALING:
        raise ValueError("upload is not ready to seal")
    archive = session.get(ArchiveItem, upload.archive_item_id)
    if archive is None:
        raise RuntimeError("upload archive item is missing")
    if upload.expected_size == 0 and not source.exists():
        descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    destination = settings.staging_root / f"{archive.id}.partial"
    candidate = source if source.is_file() else destination
    if (
        candidate.is_symlink()
        or not candidate.is_file()
        or candidate.stat().st_size != upload.expected_size
    ):
        raise ValueError("upload file is incomplete")
    digest = hashlib.sha256()
    with candidate.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    checksum = digest.hexdigest()
    if upload.expected_sha256 is not None and checksum != upload.expected_sha256:
        raise ValueError("upload SHA-256 does not match")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_symlink() or destination.stat().st_size != upload.expected_size:
            raise RuntimeError("staging collision requires manual review")
        source.unlink(missing_ok=True)
    else:
        os.replace(source, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    archive.plaintext_sha256 = checksum
    transition_item(session, archive, ArchiveState.LOCAL_STAGED, actor="file-provider")
    upload.status = FileProviderUploadStatus.SEALED
    item = session.get(FileProviderItem, upload.item_id)
    if item is None:
        raise RuntimeError("File Provider item is missing")
    item.content_version = checksum
    DurableQueue().enqueue(
        session,
        kind="archive_upload",
        adapter="file_provider",
        payload={"archive_item_id": archive.id, "file_provider_item_id": item.id},
        idempotency_key=f"file-provider-upload:{upload.id}",
    )
    record_change(session, item.id)
    return archive


def cleanup_e2e_upload(session: Session, settings: Settings, item_id: str) -> None:
    if os.getenv("MNEMA_ALLOW_TEST_DELETE") != "1":
        raise PermissionError("test deletion is disabled")
    item = session.get(FileProviderItem, item_id)
    if item is None:
        return
    if not item.name.startswith("mnema-e2e-"):
        raise PermissionError("only E2E-prefixed uploads may be cleaned")
    upload = session.scalar(select(FileProviderUpload).where(FileProviderUpload.item_id == item.id))
    if upload is None or upload.status != FileProviderUploadStatus.SEALED:
        raise PermissionError("item is not a sealed File Provider upload")
    archive = session.get(ArchiveItem, upload.archive_item_id)
    if (
        archive is None
        or archive.source_provider != "file_provider_upload"
        or archive.state != ArchiveState.LOCAL_STAGED
    ):
        raise PermissionError("upload is outside the E2E cleanup lifecycle")
    root = settings.staging_root.resolve()
    staged = settings.staging_root / f"{archive.id}.partial"
    if staged.is_symlink() or staged.resolve(strict=False).parent != root:
        raise PermissionError("E2E upload path is unsafe")
    staged.unlink(missing_ok=True)
    if staged.exists():
        raise OSError("E2E upload cleanup could not be verified")
    transition_item(session, archive, ArchiveState.TEST_CLEANED, actor="e2e-cleanup")
    record_change(session, item.id, operation="DELETE")
    session.delete(upload)
    session.flush()
    session.delete(item)


def reconcile_sealing_uploads(database: Database, settings: Settings) -> int:
    """Finish uploads interrupted mid-seal.

    Each upload is reconciled in its own transaction: one unsealable upload used
    to raise out of the loop and abort worker startup entirely.
    """
    recovered = 0
    with database.session() as session:
        pending = [
            upload.id
            for upload in session.scalars(
                select(FileProviderUpload).where(
                    FileProviderUpload.status == FileProviderUploadStatus.SEALING
                )
            ).all()
        ]
    for upload_id in pending:
        try:
            with database.session() as session:
                upload = session.get(FileProviderUpload, upload_id)
                if upload is None:
                    continue
                seal_upload(session, settings, upload)
                recovered += 1
        except (ValueError, OSError, RuntimeError):
            LOGGER.exception("could not seal interrupted upload", extra={"upload": upload_id})
            with database.session() as session:
                upload = session.get(FileProviderUpload, upload_id)
                if upload is None:
                    continue
                upload.status = FileProviderUploadStatus.FAILED
                mark_upload_failed(session, upload.item_id, "seal_failed")
    return recovered


def promote_upload(session: Session, archive: ArchiveItem, item_id: str) -> None:
    if archive.state not in READY_STATES or archive.cold_archived_at is None:
        return
    item = session.get(FileProviderItem, item_id)
    if item is None or item.status == FileProviderItemStatus.READY:
        return
    # Inbox names come straight from the phone and collide freely; the archive
    # identifier makes the promoted name deterministic and matches the name
    # project_verified_archives derives for the same item.
    prefix = f"{archive.id}-"
    desired = item.name if item.name.startswith(prefix) else f"{prefix}{item.name}"
    item.name = unique_child_name(session, UPLOADS_ID, desired, item_id=item.id)
    item.parent_id = UPLOADS_ID
    item.status = FileProviderItemStatus.READY
    item.error_message = None
    record_change(session, item.id)


def mark_upload_failed(session: Session, item_id: str, code: str) -> None:
    """Record a stable failure code on an item.

    ``error_message`` is served to paired devices, so it carries a short code
    only. The full redacted detail belongs in the job's ``last_error``.
    """
    item = session.get(FileProviderItem, item_id)
    if item is None:
        return
    item.status = FileProviderItemStatus.FAILED
    item.error_message = code[:120]
    record_change(session, item.id)


def reap_expired_uploads(database: Database, settings: Settings) -> int:
    """Release staged bytes for uploads that can never complete.

    Nothing previously cleaned up expired uploads, so their partial files
    occupied active storage forever while still counting against the free-space
    reserve that create_upload checks before accepting new work.
    """
    reaped = 0
    now = datetime.now(UTC)
    with database.session() as session:
        uploads = session.scalars(
            select(FileProviderUpload).where(
                FileProviderUpload.status == FileProviderUploadStatus.OPEN
            )
        ).all()
        for upload in uploads:
            if _aware(upload.expires_at) > now:
                continue
            path = upload_path(settings, upload.id)
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)
            upload.status = FileProviderUploadStatus.FAILED
            mark_upload_failed(session, upload.item_id, "upload_expired")
            archive = session.get(ArchiveItem, upload.archive_item_id)
            if archive is not None and archive.state != ArchiveState.MANUAL_REVIEW:
                transition_item(
                    session,
                    archive,
                    ArchiveState.MANUAL_REVIEW,
                    actor="upload-reaper",
                    details={"reason": "upload expired before completion"},
                )
            reaped += 1
    return reaped


def project_verified_archives(session: Session) -> int:
    bootstrap_roots(session)
    projected = 0
    candidates = session.scalars(select(ArchiveItem).where(ArchiveItem.nas_path.is_not(None))).all()
    for archive in candidates:
        if archive.state not in READY_STATES or archive.cold_archived_at is None:
            continue
        existing = session.scalar(
            select(FileProviderItem).where(FileProviderItem.archive_item_id == archive.id)
        )
        collection_id, _ = COLLECTIONS.get(archive.source_provider, (LOCAL_ID, "Local Imports"))
        original = safe_relative_path(archive.original_path)
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mnema:archive:{archive.id}"))
        parent_id = collection_id
        name = original.name
        if archive.source_provider == "icloud_photos":
            cumulative: list[str] = []
            for component in original.parts[:-1]:
                cumulative.append(component)
                folder_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"mnema:collection:{archive.source_provider}:{'/'.join(cumulative)}",
                    )
                )
                folder = session.get(FileProviderItem, folder_id)
                if folder is None:
                    # A single fixed "(folder)" suffix collided again on the
                    # second clash and aborted the whole projection.
                    display_name = unique_child_name(
                        session, parent_id, component, item_id=folder_id
                    )
                    folder = FileProviderItem(
                        id=folder_id,
                        parent_id=parent_id,
                        name=display_name,
                        kind=FileProviderItemKind.FOLDER,
                        status=FileProviderItemStatus.READY,
                    )
                    session.add(folder)
                    session.flush()
                    record_change(session, folder.id)
                parent_id = folder.id
            name = unique_child_name(
                session,
                parent_id,
                name,
                item_id=existing.id if existing is not None else identifier,
            )
        else:
            name = unique_child_name(
                session,
                parent_id,
                f"{archive.id}-{original.name}",
                item_id=existing.id if existing is not None else identifier,
            )
        if existing is None:
            existing = FileProviderItem(
                id=identifier,
                parent_id=parent_id,
                name=name,
                kind=FileProviderItemKind.FILE,
                status=FileProviderItemStatus.READY,
                archive_item_id=archive.id,
                size=archive.original_size,
                content_version=archive.plaintext_sha256 or "",
                modified_at=archive.original_modified_at,
            )
            session.add(existing)
            session.flush()
            record_change(session, existing.id)
            projected += 1
        elif existing.parent_id != parent_id or existing.name != name:
            existing.parent_id = parent_id
            existing.name = name
            record_change(session, existing.id)
            projected += 1
    return projected
