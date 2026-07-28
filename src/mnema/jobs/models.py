from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, Integer, String, Text
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
