from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from mnema.adapters.backup.base import BackupReceipt, VersionedBackup
from mnema.adapters.cold_storage.base import ColdReceipt, ColdStorage
from mnema.adapters.nas.fileops import commit_staged, partial_path, sha256_file
from mnema.adapters.sources.local import SourceChangedError
from mnema.config import DeletionLimits, SourcePolicy
from mnema.domain.source import SourceAdapter, SourceObject
from mnema.domain.states import ArchiveState
from mnema.domain.storage import resolve_beneath
from mnema.jobs.models import ArchiveItem, utcnow
from mnema.jobs.state_service import transition_item
from mnema.policies.deletion import (
    DeletionFacts,
    DeletionRunUsage,
    GateDecision,
    evaluate_deletion_gate,
)
from mnema.policies.eligibility import evaluate_policy


class VerificationFailure(RuntimeError):
    pass


class ArchiveWorkflow:
    def __init__(
        self,
        *,
        source: SourceAdapter,
        backup: VersionedBackup,
        cold: ColdStorage,
        active_root: Path,
        staging_root: Path,
        policy: SourcePolicy,
        source_provider: str = "local_test",
        source_is_active: bool = False,
    ) -> None:
        self.source = source
        self.backup = backup
        self.cold = cold
        self.active_root = active_root
        self.staging_root = staging_root
        self.policy = policy
        self.source_provider = source_provider
        self.source_is_active = source_is_active

    async def discover(self, session: Session) -> list[ArchiveItem]:
        cursor: str | None = None
        found: list[ArchiveItem] = []
        while True:
            page = await self.source.discover(cursor)
            for source_object in page.objects:
                item = session.scalar(
                    select(ArchiveItem).where(
                        ArchiveItem.source_provider == self.source_provider,
                        ArchiveItem.source_identifier == source_object.source_id,
                    )
                )
                if item is None:
                    item = self._new_item(source_object)
                    session.add(item)
                    session.flush()
                    decision = evaluate_policy(source_object, self.policy)
                    transition_item(
                        session,
                        item,
                        ArchiveState.ELIGIBLE if decision.eligible else ArchiveState.INELIGIBLE,
                        actor="scanner",
                        details={"policy_reasons": decision.reasons},
                    )
                elif self._source_changed(item, source_object):
                    transition_item(
                        session,
                        item,
                        ArchiveState.SOURCE_CHANGED,
                        actor="scanner",
                        details={"reason": "source metadata changed"},
                    )
                    item.original_path = source_object.relative_path
                    item.original_size = source_object.size
                    item.original_modified_at = source_object.modified_at
                    item.source_version = source_object.version
                elif item.state == ArchiveState.INELIGIBLE:
                    decision = evaluate_policy(source_object, self.policy)
                    transition_item(
                        session,
                        item,
                        ArchiveState.ELIGIBLE if decision.eligible else ArchiveState.INELIGIBLE,
                        actor="scanner",
                        details={"policy_reasons": decision.reasons},
                    )
                found.append(item)
            cursor = page.next_cursor
            if cursor is None:
                break
        return found

    def _new_item(self, source: SourceObject) -> ArchiveItem:
        return ArchiveItem(
            source_provider=self.source_provider,
            source_identifier=source.source_id,
            original_path=source.relative_path,
            original_size=source.size,
            original_modified_at=source.modified_at,
            source_version=source.version,
            state=ArchiveState.DISCOVERED,
        )

    async def archive(self, session: Session, item: ArchiveItem) -> ArchiveItem:
        if item.state in {
            ArchiveState.ELIGIBLE,
            ArchiveState.SOURCE_CHANGED,
            ArchiveState.FAILED_RETRYABLE,
            ArchiveState.MANUAL_REVIEW,
        }:
            transition_item(session, item, ArchiveState.QUEUED, actor="worker")
        if item.state == ArchiveState.QUEUED:
            transition_item(session, item, ArchiveState.DOWNLOADING, actor="worker")
            session.commit()
        if item.state == ArchiveState.DOWNLOADING:
            staged = partial_path(self.staging_root, item.id)
            if staged.exists():
                staged.unlink()
            try:
                receipt = await self.source.download(item.source_identifier, staged)
            except SourceChangedError:
                transition_item(
                    session,
                    item,
                    ArchiveState.SOURCE_CHANGED,
                    actor="worker",
                    details={"stage": "download"},
                )
                session.commit()
                return item
            item.plaintext_sha256 = receipt.sha256
            transition_item(session, item, ArchiveState.LOCAL_STAGED, actor="worker")
            session.commit()
        staged = partial_path(self.staging_root, item.id)
        if item.state == ArchiveState.LOCAL_STAGED:
            if sha256_file(staged) != item.plaintext_sha256:
                raise VerificationFailure("staged copy hash mismatch")
            transition_item(session, item, ArchiveState.LOCAL_VERIFIED, actor="worker")
            session.commit()
        if item.state == ArchiveState.LOCAL_VERIFIED:
            assert item.plaintext_sha256 is not None
            if self.source_is_active:
                active = resolve_beneath(self.active_root, item.original_path, must_exist=True)
                if active.is_symlink() or not active.is_file():
                    raise VerificationFailure("active source must be a regular non-symlink file")
                if active.stat().st_size != item.original_size:
                    raise VerificationFailure("active source size changed before adoption")
                if sha256_file(active) != item.plaintext_sha256:
                    raise VerificationFailure("active source hash changed before adoption")
                staged.unlink()
                item.nas_path = str(active)
                transformed_from = None
            else:
                commit = commit_staged(
                    staged,
                    self.active_root,
                    item.original_path,
                    item.plaintext_sha256,
                    item.original_size,
                    modified_timestamp=item.original_modified_at.timestamp(),
                )
                item.nas_path = str(commit.path)
                transformed_from = commit.transformed_from
            item.nas_verified_at = utcnow()
            transition_item(
                session,
                item,
                ArchiveState.LOCAL_COMMITTED,
                actor="worker",
                details={"filename_transformed_from": transformed_from},
            )
            session.commit()
        if item.state == ArchiveState.LOCAL_COMMITTED:
            transition_item(session, item, ArchiveState.LOCAL_BACKUP_PENDING, actor="worker")
            session.commit()
        if item.state == ArchiveState.LOCAL_BACKUP_PENDING:
            assert item.nas_path is not None and item.plaintext_sha256 is not None
            backup_receipt = await self.backup.snapshot(Path(item.nas_path), f"item-{item.id}")
            if not await self.backup.verify(backup_receipt, item.plaintext_sha256):
                raise VerificationFailure("independent local backup restore verification failed")
            item.kopia_snapshot_id = backup_receipt.snapshot_id
            item.kopia_verified_at = utcnow()
            transition_item(
                session,
                item,
                ArchiveState.LOCAL_BACKUP_VERIFIED,
                actor="worker",
            )
            session.commit()
        if item.state == ArchiveState.LOCAL_BACKUP_VERIFIED:
            transition_item(session, item, ArchiveState.COLD_UPLOAD_PENDING, actor="worker")
            session.commit()
        if item.state == ArchiveState.COLD_UPLOAD_PENDING:
            assert item.nas_path is not None and item.plaintext_sha256 is not None
            cold_receipt = await self.cold.upload(
                Path(item.nas_path),
                item.original_path,
                f"item-{item.id}",
            )
            self._record_cold_receipt(item, cold_receipt)
            transition_item(session, item, ArchiveState.COLD_UPLOADED, actor="worker")
            session.commit()
        if item.state == ArchiveState.COLD_UPLOADED:
            assert item.plaintext_sha256 is not None
            cold_verification_receipt = self._cold_receipt(item)
            if not await self.cold.verify(cold_verification_receipt, item.plaintext_sha256):
                raise VerificationFailure("independent remote restore verification failed")
            item.remote_verified_at = utcnow()
            item.remote_verification_method = "download-decrypt-sha256"
            transition_item(session, item, ArchiveState.COLD_VERIFIED, actor="worker")
            session.commit()
        if item.state == ArchiveState.COLD_VERIFIED:
            transition_item(session, item, ArchiveState.COLD_ARCHIVE_PENDING, actor="worker")
            session.commit()
        if item.state == ArchiveState.COLD_ARCHIVE_PENDING:
            await self.cold.archive_verified(self._cold_receipt(item))
            transition_item(session, item, ArchiveState.COLD_ARCHIVED, actor="worker")
            session.commit()
        if item.state == ArchiveState.COLD_ARCHIVED:
            item.quarantine_expires_at = utcnow() + timedelta(days=self.policy.quarantine_days)
            transition_item(session, item, ArchiveState.QUARANTINED, actor="worker")
            session.commit()
        return item

    async def restore_local(
        self,
        item: ArchiveItem,
        destination: Path,
    ) -> bool:
        if item.kopia_snapshot_id is None or item.plaintext_sha256 is None:
            return False
        await self.backup.restore(BackupReceipt(item.kopia_snapshot_id), destination)
        return sha256_file(destination) == item.plaintext_sha256

    async def restore_remote(
        self,
        item: ArchiveItem,
        destination: Path,
    ) -> bool:
        if item.plaintext_sha256 is None:
            return False
        await self.cold.restore(self._cold_receipt(item), destination)
        return sha256_file(destination) == item.plaintext_sha256

    async def deletion_decision(
        self,
        item: ArchiveItem,
        *,
        active_disk_healthy: bool,
        backup_disk_healthy: bool,
        storage_devices_differ: bool,
        sqlite_integrity_healthy: bool,
        global_deletion_enabled: bool,
        safety_lock: bool,
        usage: DeletionRunUsage,
        limits: DeletionLimits,
        manual_approval_granted: bool = False,
        now: datetime | None = None,
    ) -> GateDecision:
        source_exists = True
        source_unchanged = False
        identity_matches = False
        try:
            current = await self.source.stat(item.source_identifier)
            source_unchanged = self._source_matches(item, current)
            identity_matches = current.source_id == item.source_identifier
        except FileNotFoundError:
            source_exists = False
        nas_path = Path(item.nas_path) if item.nas_path else None
        local_exists = bool(nas_path and nas_path.is_file())
        local_size_matches = bool(
            local_exists and nas_path and nas_path.stat().st_size == item.original_size
        )
        local_hash_matches = bool(
            local_exists
            and nas_path
            and item.plaintext_sha256
            and sha256_file(nas_path) == item.plaintext_sha256
        )
        facts = DeletionFacts(
            local_exists=local_exists,
            local_size_matches=local_size_matches,
            local_sha256_exists=item.plaintext_sha256 is not None,
            local_sha256_matches=local_hash_matches,
            kopia_verified=item.kopia_verified_at is not None,
            remote_verified=item.remote_verified_at is not None,
            quarantine_expires_at=item.quarantine_expires_at,
            source_exists=source_exists,
            source_unchanged=source_unchanged,
            source_identity_matches=identity_matches,
            active_disk_healthy=active_disk_healthy,
            backup_disk_healthy=backup_disk_healthy,
            storage_devices_differ=storage_devices_differ,
            sqlite_integrity_healthy=sqlite_integrity_healthy,
            remote_available=await self.cold.available(),
            global_deletion_enabled=global_deletion_enabled,
            source_deletion_enabled=self.policy.deletion_enabled,
            safety_lock=safety_lock,
            manual_approval_required=self.policy.manual_approval,
            manual_approval_granted=manual_approval_granted,
        )
        return evaluate_deletion_gate(
            facts,
            usage,
            limits,
            item_size=item.original_size,
            now=now,
        )

    async def delete_test_item(
        self,
        session: Session,
        item: ArchiveItem,
        decision: GateDecision,
    ) -> None:
        capabilities = await self.source.capabilities()
        if not capabilities.can_delete:
            raise PermissionError(f"{self.source_provider} source deletion is unavailable")
        if not decision.allowed:
            raise PermissionError("; ".join(decision.blockers))
        current = await self.source.stat(item.source_identifier)
        if not self._source_matches(item, current):
            transition_item(session, item, ArchiveState.SOURCE_CHANGED, actor="guard")
            return
        if item.state == ArchiveState.QUARANTINED:
            transition_item(session, item, ArchiveState.READY_FOR_REVALIDATION, actor="guard")
        if item.state == ArchiveState.READY_FOR_REVALIDATION:
            transition_item(session, item, ArchiveState.READY_FOR_DELETION, actor="guard")
        transition_item(session, item, ArchiveState.DELETING, actor="guard")
        session.commit()
        try:
            receipt = await self.source.delete(item.source_identifier, item.source_version)
        except SourceChangedError:
            transition_item(session, item, ArchiveState.SOURCE_CHANGED, actor="guard")
            return
        except Exception as error:
            transition_item(
                session,
                item,
                ArchiveState.MANUAL_REVIEW,
                actor="guard",
                details={"ambiguous_delete_error": type(error).__name__},
            )
            return
        if not receipt.confirmed_absent:
            transition_item(
                session,
                item,
                ArchiveState.MANUAL_REVIEW,
                actor="guard",
                details={"delete_result": "source presence ambiguous"},
            )
            return
        item.deletion_timestamp = utcnow()
        transition_item(session, item, ArchiveState.DELETED_FROM_SOURCE, actor="guard")
        transition_item(
            session,
            item,
            ArchiveState.ARCHIVED,
            actor="guard",
            details={"tombstone": True},
        )

    @staticmethod
    def _source_matches(item: ArchiveItem, current: SourceObject) -> bool:
        stored_modified = item.original_modified_at
        current_modified = current.modified_at
        if stored_modified.tzinfo is None:
            stored_modified = stored_modified.replace(tzinfo=UTC)
        if current_modified.tzinfo is None:
            current_modified = current_modified.replace(tzinfo=UTC)
        return (
            current.source_id == item.source_identifier
            and current.relative_path == item.original_path
            and current.size == item.original_size
            and current_modified == stored_modified
            and current.version == item.source_version
        )

    @staticmethod
    def _source_changed(item: ArchiveItem, current: SourceObject) -> bool:
        return not ArchiveWorkflow._source_matches(item, current)

    @staticmethod
    def _record_cold_receipt(item: ArchiveItem, receipt: ColdReceipt) -> None:
        item.remote_provider = receipt.provider
        item.remote_bucket = receipt.bucket
        item.remote_object_identifier = receipt.object_identifier
        item.encryption_mode = receipt.encryption_mode
        item.remote_size = receipt.remote_size
        item.remote_checksum = receipt.remote_checksum
        item.uploaded_at = utcnow()

    @staticmethod
    def _cold_receipt(item: ArchiveItem) -> ColdReceipt:
        if (
            item.remote_provider is None
            or item.remote_bucket is None
            or item.remote_object_identifier is None
            or item.encryption_mode is None
            or item.remote_size is None
        ):
            raise VerificationFailure("remote receipt is incomplete")
        return ColdReceipt(
            item.remote_provider,
            item.remote_bucket,
            item.remote_object_identifier,
            item.encryption_mode,
            item.remote_size,
            item.remote_checksum,
        )
