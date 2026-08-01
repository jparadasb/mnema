from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from mnema.domain.states import ArchiveState


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ArchiveItem(Base):
    __tablename__ = "archive_items"
    __table_args__ = (
        Index("ix_archive_source_identity", "source_provider", "source_identifier", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_provider: Mapped[str] = mapped_column(String(64))
    source_identifier: Mapped[str] = mapped_column(Text)
    original_path: Mapped[str] = mapped_column(Text)
    original_size: Mapped[int] = mapped_column(Integer)
    original_modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_version: Mapped[str] = mapped_column(Text)
    state: Mapped[ArchiveState] = mapped_column(
        Enum(ArchiveState, native_enum=False),
        default=ArchiveState.DISCOVERED,
    )
    plaintext_sha256: Mapped[str | None] = mapped_column(String(64))
    nas_path: Mapped[str | None] = mapped_column(Text)
    nas_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kopia_snapshot_id: Mapped[str | None] = mapped_column(Text)
    kopia_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_provider: Mapped[str | None] = mapped_column(String(64))
    remote_bucket: Mapped[str | None] = mapped_column(Text)
    remote_object_identifier: Mapped[str | None] = mapped_column(Text)
    encryption_mode: Mapped[str | None] = mapped_column(String(64))
    remote_size: Mapped[int | None] = mapped_column(Integer)
    remote_checksum: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_verification_method: Mapped[str | None] = mapped_column(Text)
    cold_archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quarantine_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restore_test_status: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(back_populates="item")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_item_id: Mapped[int | None] = mapped_column(ForeignKey("archive_items.id"))
    event_type: Mapped[str] = mapped_column(String(100))
    from_state: Mapped[str | None] = mapped_column(String(64))
    to_state: Mapped[str | None] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    item: Mapped[ArchiveItem | None] = relationship(back_populates="audit_events")


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY = "RETRY"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(100))
    adapter: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False),
        default=JobStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    level: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    adapter: Mapped[str] = mapped_column(String(100))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class ICloudCleanupStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    EXPIRED = "EXPIRED"


class ICloudAsset(Base):
    __tablename__ = "icloud_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    apple_asset_id: Mapped[str] = mapped_column(Text, unique=True)
    asset_record_name: Mapped[str] = mapped_column(Text)
    change_tag: Mapped[str] = mapped_column(Text)
    created_at_remote: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    original_size: Mapped[int] = mapped_column(BigInteger)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    library: Mapped[str] = mapped_column(String(64), default="PrimarySync")
    expected_components: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    remotely_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manual_review_reason: Mapped[str | None] = mapped_column(Text)
    components: Mapped[list[ICloudAssetComponent]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )


class ICloudAssetComponent(Base):
    __tablename__ = "icloud_asset_components"
    __table_args__ = (
        Index("ix_icloud_asset_component", "icloud_asset_id", "archive_item_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    icloud_asset_id: Mapped[int] = mapped_column(ForeignKey("icloud_assets.id"))
    archive_item_id: Mapped[int] = mapped_column(ForeignKey("archive_items.id"), unique=True)
    asset: Mapped[ICloudAsset] = relationship(back_populates="components")
    archive_item: Mapped[ArchiveItem] = relationship()


class ICloudQuotaObservation(Base):
    __tablename__ = "icloud_quota_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    used_bytes: Mapped[int] = mapped_column(BigInteger)
    quota_bytes: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ICloudCleanupManifest(Base):
    __tablename__ = "icloud_cleanup_manifests"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[ICloudCleanupStatus] = mapped_column(
        Enum(ICloudCleanupStatus, native_enum=False), default=ICloudCleanupStatus.PENDING_APPROVAL
    )
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    used_bytes: Mapped[int] = mapped_column(BigInteger)
    quota_bytes: Mapped[int] = mapped_column(BigInteger)
    target_bytes: Mapped[int] = mapped_column(BigInteger)
    planned_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    entries: Mapped[list[ICloudCleanupEntry]] = relationship(
        back_populates="manifest",
        cascade="all, delete-orphan",
        order_by="ICloudCleanupEntry.position",
    )


class ICloudCleanupEntry(Base):
    __tablename__ = "icloud_cleanup_entries"
    __table_args__ = (
        Index("ix_icloud_cleanup_entry", "manifest_id", "icloud_asset_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    manifest_id: Mapped[int] = mapped_column(ForeignKey("icloud_cleanup_manifests.id"))
    icloud_asset_id: Mapped[int] = mapped_column(ForeignKey("icloud_assets.id"))
    position: Mapped[int] = mapped_column(Integer)
    apple_asset_id: Mapped[str] = mapped_column(Text)
    asset_record_name: Mapped[str] = mapped_column(Text)
    change_tag: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    evidence_digest: Mapped[str] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manifest: Mapped[ICloudCleanupManifest] = relationship(back_populates="entries")
    asset: Mapped[ICloudAsset] = relationship()


class FileProviderItemKind(StrEnum):
    FILE = "FILE"
    FOLDER = "FOLDER"


class FileProviderItemStatus(StrEnum):
    READY = "READY"
    PROCESSING = "PROCESSING"
    FAILED = "FAILED"


class FileProviderItem(Base):
    __tablename__ = "file_provider_items"
    __table_args__ = (Index("ix_file_provider_parent_name", "parent_id", "name", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[FileProviderItemKind] = mapped_column(
        Enum(FileProviderItemKind, native_enum=False)
    )
    status: Mapped[FileProviderItemStatus] = mapped_column(
        Enum(FileProviderItemStatus, native_enum=False), default=FileProviderItemStatus.READY
    )
    archive_item_id: Mapped[int | None] = mapped_column(ForeignKey("archive_items.id"), unique=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    content_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    content_version: Mapped[str] = mapped_column(String(64), default="")
    metadata_version: Mapped[int] = mapped_column(BigInteger, default=1)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    archive_item: Mapped[ArchiveItem | None] = relationship()


class FileProviderChange(Base):
    __tablename__ = "file_provider_changes"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(64), index=True)
    operation: Mapped[str] = mapped_column(String(16), default="UPSERT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FileProviderUploadStatus(StrEnum):
    OPEN = "OPEN"
    SEALING = "SEALING"
    SEALED = "SEALED"
    FAILED = "FAILED"


class FileProviderUpload(Base):
    __tablename__ = "file_provider_uploads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("file_provider_items.id"), unique=True)
    archive_item_id: Mapped[int] = mapped_column(ForeignKey("archive_items.id"), unique=True)
    expected_size: Mapped[int] = mapped_column(BigInteger)
    received_size: Mapped[int] = mapped_column(BigInteger, default=0)
    expected_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[FileProviderUploadStatus] = mapped_column(
        Enum(FileProviderUploadStatus, native_enum=False), default=FileProviderUploadStatus.OPEN
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class FileProviderPairingCode(Base):
    __tablename__ = "file_provider_pairing_codes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FileProviderDevice(Base):
    __tablename__ = "file_provider_devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
