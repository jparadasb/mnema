from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from mnema.adapters.nas.fileops import sha256_file
from mnema.adapters.sources.icloud_control import (
    ICloudControlClient,
    ICloudRemoteAsset,
)
from mnema.config import DeletionLimits, Settings
from mnema.domain.states import ArchiveState
from mnema.jobs.models import (
    ArchiveItem,
    AuditEvent,
    ICloudAsset,
    ICloudAssetComponent,
    ICloudCleanupEntry,
    ICloudCleanupManifest,
    ICloudCleanupStatus,
    ICloudQuotaObservation,
    RuntimeSetting,
    utcnow,
)
from mnema.policies.deletion import (
    DeletionFacts,
    DeletionRunUsage,
    evaluate_deletion_gate,
)


class ICloudCleanupBlocked(RuntimeError):
    pass


class ICloudCleanupService:
    def __init__(self, settings: Settings, client: ICloudControlClient) -> None:
        self.settings = settings
        self.client = client

    def observe_quota(self, session: Session) -> ICloudQuotaObservation:
        quota = self.client.quota()
        observation = ICloudQuotaObservation(
            used_bytes=quota.used_bytes, quota_bytes=quota.quota_bytes
        )
        session.add(observation)
        session.flush()
        return observation

    def refresh_inventory(self, session: Session) -> int:
        remote_assets = self.client.assets()
        items = session.scalars(
            select(ArchiveItem).where(ArchiveItem.source_provider == "icloud_photos")
        ).all()
        by_token: dict[str, list[ArchiveItem]] = {}
        for item in items:
            for token in self._tokens(Path(item.source_identifier).name):
                by_token.setdefault(token, []).append(item)
        remote_by_token: dict[str, list[ICloudRemoteAsset]] = {}
        for remote in remote_assets:
            remote_by_token.setdefault(self._asset_token(remote.apple_asset_id), []).append(remote)
        now = utcnow()
        mapped = 0
        for remote in remote_assets:
            asset = session.scalar(
                select(ICloudAsset).where(ICloudAsset.apple_asset_id == remote.apple_asset_id)
            )
            if asset is None:
                asset = ICloudAsset(apple_asset_id=remote.apple_asset_id)
                session.add(asset)
            self._update_asset(asset, remote, now)
            session.flush()
            asset.components.clear()
            token = self._asset_token(remote.apple_asset_id)
            candidates = by_token.get(token, [])
            if len(remote_by_token[token]) == 1 and len(candidates) == remote.expected_components:
                for item in candidates:
                    asset.components.append(ICloudAssetComponent(archive_item=item))
                mapped += 1
        session.flush()
        return mapped

    def create_manifest(self, session: Session) -> ICloudCleanupManifest | None:
        if not self.settings.icloud_capacity_relief_enabled:
            raise ICloudCleanupBlocked("iCloud capacity relief is disabled")
        now = utcnow()
        self._expire_manifests(session, now)
        quota = self.client.quota()
        session.add(
            ICloudQuotaObservation(used_bytes=quota.used_bytes, quota_bytes=quota.quota_bytes)
        )
        if quota.used_percent < self.settings.icloud_cleanup_trigger_percent:
            return None
        pending = session.scalar(
            select(ICloudCleanupManifest)
            .where(ICloudCleanupManifest.status == ICloudCleanupStatus.PENDING_APPROVAL)
            .order_by(ICloudCleanupManifest.id.desc())
        )
        if pending is not None:
            return pending
        self.refresh_inventory(session)
        candidates = [
            asset
            for asset in session.scalars(
                select(ICloudAsset)
                .where(
                    ICloudAsset.favorite.is_(False),
                    ICloudAsset.remotely_deleted_at.is_(None),
                    ICloudAsset.library == "PrimarySync",
                )
                .order_by(ICloudAsset.created_at_remote, ICloudAsset.apple_asset_id)
            ).all()
            if self._eligible(asset, now)
        ]
        target_bytes = int(quota.quota_bytes * self.settings.icloud_cleanup_target_percent / 100)
        bytes_needed = max(0, quota.used_bytes - target_bytes)
        byte_cap = int(quota.quota_bytes * self.settings.icloud_cleanup_max_quota_percent / 100)
        selected: list[ICloudAsset] = []
        planned = 0
        for asset in candidates:
            if len(selected) >= self.settings.icloud_cleanup_max_assets:
                break
            if selected and planned + asset.original_size > byte_cap:
                break
            selected.append(asset)
            planned += asset.original_size
            if planned >= bytes_needed:
                break
        if not selected:
            return None
        canonical: dict[str, object] = {
            "created_at": now.isoformat(),
            "used_bytes": quota.used_bytes,
            "quota_bytes": quota.quota_bytes,
            "assets": [
                {
                    "apple_asset_id": asset.apple_asset_id,
                    "asset_record_name": asset.asset_record_name,
                    "change_tag": asset.change_tag,
                    "size_bytes": asset.original_size,
                    "evidence_digest": self._evidence_digest(asset),
                }
                for asset in selected
            ],
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing = session.scalar(
            select(ICloudCleanupManifest).where(ICloudCleanupManifest.digest == digest)
        )
        if existing is not None:
            return existing
        manifest = ICloudCleanupManifest(
            digest=digest,
            used_bytes=quota.used_bytes,
            quota_bytes=quota.quota_bytes,
            target_bytes=target_bytes,
            planned_bytes=planned,
            expires_at=now + timedelta(hours=24),
        )
        session.add(manifest)
        session.flush()
        for position, asset in enumerate(selected):
            manifest.entries.append(
                ICloudCleanupEntry(
                    icloud_asset_id=asset.id,
                    position=position,
                    apple_asset_id=asset.apple_asset_id,
                    asset_record_name=asset.asset_record_name,
                    change_tag=asset.change_tag,
                    size_bytes=asset.original_size,
                    evidence_digest=self._evidence_digest(asset),
                )
            )
        self._audit(session, "icloud_cleanup_manifest_created", None, {"manifest_id": manifest.id})
        session.flush()
        return manifest

    def execute(
        self, session: Session, manifest_id: int, digest: str, *, gates_ready: bool
    ) -> None:
        if not self.settings.icloud_deletion_milestone_approved:
            raise ICloudCleanupBlocked("iCloud deletion milestone awaits synthetic-account review")
        manifest = session.get(ICloudCleanupManifest, manifest_id)
        if manifest is None or manifest.digest != digest:
            raise ICloudCleanupBlocked("cleanup manifest or digest is invalid")
        now = utcnow()
        if manifest.status != ICloudCleanupStatus.PENDING_APPROVAL:
            raise ICloudCleanupBlocked("cleanup manifest is not pending approval")
        if self._aware(manifest.expires_at) <= now:
            manifest.status = ICloudCleanupStatus.EXPIRED
            raise ICloudCleanupBlocked("cleanup manifest expired")
        if not gates_ready:
            raise ICloudCleanupBlocked("global deletion safety gate is closed")
        quota = self.client.quota()
        if quota.used_percent < self.settings.icloud_cleanup_trigger_percent:
            raise ICloudCleanupBlocked("iCloud usage is below cleanup trigger")
        current = {asset.apple_asset_id: asset for asset in self.client.assets()}
        usage = DeletionRunUsage(0, 0, len(manifest.entries))
        for entry in manifest.entries:
            remote = current.get(entry.apple_asset_id)
            if remote is None or remote.favorite:
                raise ICloudCleanupBlocked("approved iCloud asset is missing or protected")
            if (
                remote.asset_record_name != entry.asset_record_name
                or remote.change_tag != entry.change_tag
            ):
                raise ICloudCleanupBlocked("approved iCloud asset changed")
            if not self._eligible(entry.asset, now):
                raise ICloudCleanupBlocked("archival evidence changed after approval")
            if self._evidence_digest(entry.asset) != entry.evidence_digest:
                raise ICloudCleanupBlocked("archival receipts changed after approval")
            decision = evaluate_deletion_gate(
                self._deletion_facts(entry.asset, gates_ready, now),
                usage,
                DeletionLimits(
                    max_files_deleted_per_run=self.settings.icloud_cleanup_max_assets,
                    max_bytes_deleted_per_run=int(
                        manifest.quota_bytes * self.settings.icloud_cleanup_max_quota_percent / 100
                    ),
                    max_percentage_deleted_per_run=100,
                ),
                item_size=entry.size_bytes,
                now=now,
            )
            if not decision.allowed:
                raise ICloudCleanupBlocked("; ".join(decision.blockers))
            usage = DeletionRunUsage(
                usage.files_deleted + 1,
                usage.bytes_deleted + entry.size_bytes,
                usage.eligible_files,
            )
        manifest.status = ICloudCleanupStatus.EXECUTING
        manifest.approved_at = now
        self._audit(session, "icloud_cleanup_approved", None, {"manifest_id": manifest.id})
        session.commit()
        for entry in manifest.entries:
            try:
                confirmed = self.client.delete_to_recently_deleted(
                    entry.apple_asset_id, entry.asset_record_name, entry.change_tag
                )
            except Exception as error:
                self._manual_review(session, manifest, type(error).__name__)
                raise ICloudCleanupBlocked(
                    "iCloud deletion result requires manual review"
                ) from error
            if not confirmed:
                self._manual_review(session, manifest, "absence_not_confirmed")
                raise ICloudCleanupBlocked("iCloud deletion result requires manual review")
            entry.completed_at = utcnow()
            entry.asset.remotely_deleted_at = entry.completed_at
            for component in entry.asset.components:
                self._audit(
                    session,
                    "icloud_asset_moved_to_recently_deleted",
                    component.archive_item_id,
                    {"manifest_id": manifest.id, "apple_asset_id": entry.apple_asset_id},
                )
            session.commit()
        manifest.status = ICloudCleanupStatus.COMPLETED
        manifest.completed_at = utcnow()
        self._audit(session, "icloud_cleanup_completed", None, {"manifest_id": manifest.id})

    def _eligible(self, asset: ICloudAsset, now: datetime) -> bool:
        if asset.favorite or asset.remotely_deleted_at is not None:
            return False
        if len(asset.components) != asset.expected_components:
            return False
        for component in asset.components:
            item = component.archive_item
            if (
                item.state not in {ArchiveState.QUARANTINED, ArchiveState.ARCHIVED}
                or item.nas_path is None
                or item.plaintext_sha256 is None
                or item.kopia_verified_at is None
                or item.kopia_snapshot_id is None
                or item.remote_verified_at is None
                or item.cold_archived_at is None
                or item.remote_provider != "scaleway-glacier"
                or item.remote_bucket is None
                or item.remote_object_identifier is None
                or item.encryption_mode is None
                or item.remote_size is None
                or item.quarantine_expires_at is None
                or self._aware(item.quarantine_expires_at) > now
                or self._aware(item.cold_archived_at)
                + timedelta(days=self.settings.icloud_cleanup_quarantine_days)
                > now
            ):
                return False
            path = Path(item.nas_path)
            if path.is_symlink() or not path.is_file():
                return False
            if (
                path.stat().st_size != item.original_size
                or sha256_file(path) != item.plaintext_sha256
            ):
                return False
        return True

    def _deletion_facts(
        self, asset: ICloudAsset, gates_ready: bool, now: datetime
    ) -> DeletionFacts:
        items = [component.archive_item for component in asset.components]
        hashes_match = all(
            item.nas_path is not None
            and Path(item.nas_path).is_file()
            and item.plaintext_sha256 is not None
            and sha256_file(Path(item.nas_path)) == item.plaintext_sha256
            for item in items
        )
        quarantine = max(
            (self._aware(item.cold_archived_at) for item in items if item.cold_archived_at),
            default=now + timedelta(days=3650),
        ) + timedelta(days=self.settings.icloud_cleanup_quarantine_days)
        return DeletionFacts(
            local_exists=all(item.nas_path and Path(item.nas_path).is_file() for item in items),
            local_size_matches=all(
                item.nas_path and Path(item.nas_path).stat().st_size == item.original_size
                for item in items
            ),
            local_sha256_exists=all(item.plaintext_sha256 for item in items),
            local_sha256_matches=hashes_match,
            kopia_verified=all(item.kopia_verified_at for item in items),
            remote_verified=all(item.remote_verified_at for item in items),
            quarantine_expires_at=quarantine,
            source_exists=True,
            source_unchanged=True,
            source_identity_matches=True,
            active_disk_healthy=gates_ready,
            backup_disk_healthy=gates_ready,
            storage_devices_differ=gates_ready,
            sqlite_integrity_healthy=gates_ready,
            remote_available=gates_ready,
            global_deletion_enabled=gates_ready,
            source_deletion_enabled=self.settings.icloud_capacity_relief_enabled,
            safety_lock=not gates_ready,
            manual_approval_required=True,
            manual_approval_granted=True,
        )

    def _manual_review(
        self, session: Session, manifest: ICloudCleanupManifest, reason: str
    ) -> None:
        manifest.status = (
            ICloudCleanupStatus.PARTIAL
            if any(entry.completed_at for entry in manifest.entries)
            else ICloudCleanupStatus.MANUAL_REVIEW
        )
        manifest.failure_reason = reason
        self._set_runtime(session, "global_deletion_enabled", "false")
        self._set_runtime(session, "safety_lock", "true")
        self._audit(
            session,
            "icloud_cleanup_manual_review",
            None,
            {"manifest_id": manifest.id, "reason": reason},
        )
        session.commit()

    @staticmethod
    def _evidence_digest(asset: ICloudAsset) -> str:
        evidence = [
            {
                "archive_item_id": component.archive_item_id,
                "plaintext_sha256": component.archive_item.plaintext_sha256,
                "kopia_snapshot_id": component.archive_item.kopia_snapshot_id,
                "remote_object_identifier": component.archive_item.remote_object_identifier,
                "remote_checksum": component.archive_item.remote_checksum,
            }
            for component in sorted(asset.components, key=lambda value: value.archive_item_id)
        ]
        return hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _set_runtime(session: Session, key: str, value: str) -> None:
        setting = session.get(RuntimeSetting, key)
        if setting is None:
            session.add(RuntimeSetting(key=key, value=value))
        else:
            setting.value = value

    @staticmethod
    def _audit(
        session: Session,
        event_type: str,
        archive_item_id: int | None,
        details: dict[str, object],
    ) -> None:
        session.add(
            AuditEvent(
                archive_item_id=archive_item_id,
                event_type=event_type,
                from_state=None,
                to_state=None,
                actor="icloud-cleanup",
                details=details,
            )
        )

    @staticmethod
    def _update_asset(asset: ICloudAsset, remote: ICloudRemoteAsset, now: datetime) -> None:
        asset.asset_record_name = remote.asset_record_name
        asset.change_tag = remote.change_tag
        asset.created_at_remote = remote.created_at
        asset.original_size = remote.size_bytes
        asset.favorite = remote.favorite
        asset.library = "PrimarySync"
        asset.expected_components = remote.expected_components
        asset.last_seen_at = now

    @staticmethod
    def _asset_token(asset_id: str) -> str:
        return base64.b64encode(asset_id.encode()).decode("ascii")[:7]

    @staticmethod
    def _tokens(filename: str) -> tuple[str, ...]:
        match = re.search(r"_([^_]{7})(?:_HEVC)?\.[^.]+$", filename, flags=re.IGNORECASE)
        return (match.group(1),) if match else ()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    @staticmethod
    def _expire_manifests(session: Session, now: datetime) -> None:
        manifests = session.scalars(
            select(ICloudCleanupManifest).where(
                ICloudCleanupManifest.status == ICloudCleanupStatus.PENDING_APPROVAL
            )
        ).all()
        for manifest in manifests:
            expires = manifest.expires_at
            if (expires if expires.tzinfo else expires.replace(tzinfo=UTC)) <= now:
                manifest.status = ICloudCleanupStatus.EXPIRED
