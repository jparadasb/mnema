from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from mnema.config import DeletionLimits


@dataclass(frozen=True)
class DeletionFacts:
    local_exists: bool
    local_size_matches: bool
    local_sha256_exists: bool
    local_sha256_matches: bool
    kopia_verified: bool
    remote_verified: bool
    quarantine_expires_at: datetime | None
    source_exists: bool
    source_unchanged: bool
    source_identity_matches: bool
    active_disk_healthy: bool
    backup_disk_healthy: bool
    storage_devices_differ: bool
    sqlite_integrity_healthy: bool
    remote_available: bool
    global_deletion_enabled: bool
    source_deletion_enabled: bool
    safety_lock: bool
    manual_approval_required: bool = False
    manual_approval_granted: bool = False


@dataclass(frozen=True)
class DeletionRunUsage:
    files_deleted: int
    bytes_deleted: int
    eligible_files: int


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    blockers: tuple[str, ...]


def evaluate_deletion_gate(
    facts: DeletionFacts,
    usage: DeletionRunUsage,
    limits: DeletionLimits,
    *,
    item_size: int,
    now: datetime | None = None,
) -> GateDecision:
    now = now or datetime.now(UTC)
    checks = {
        "local file missing": facts.local_exists,
        "local size mismatch": facts.local_size_matches,
        "local SHA-256 missing": facts.local_sha256_exists,
        "local SHA-256 mismatch": facts.local_sha256_matches,
        "local Kopia backup not verified": facts.kopia_verified,
        "remote copy not verified": facts.remote_verified,
        "quarantine not configured": facts.quarantine_expires_at is not None,
        "source item missing": facts.source_exists,
        "source changed": facts.source_unchanged,
        "source identity mismatch": facts.source_identity_matches,
        "active disk unhealthy": facts.active_disk_healthy,
        "backup disk unhealthy": facts.backup_disk_healthy,
        "active and backup storage share a device": facts.storage_devices_differ,
        "SQLite integrity failed": facts.sqlite_integrity_healthy,
        "remote storage unavailable": facts.remote_available,
        "global deletion disabled": facts.global_deletion_enabled,
        "source deletion disabled": facts.source_deletion_enabled,
        "safety lock active": not facts.safety_lock,
        "manual approval missing": (
            not facts.manual_approval_required or facts.manual_approval_granted
        ),
    }
    blockers = [message for message, passed in checks.items() if not passed]
    if facts.quarantine_expires_at is not None:
        expires = facts.quarantine_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if now < expires:
            blockers.append("quarantine has not elapsed")
    if usage.files_deleted + 1 > limits.max_files_deleted_per_run:
        blockers.append("per-run file deletion limit exceeded")
    if usage.bytes_deleted + item_size > limits.max_bytes_deleted_per_run:
        blockers.append("per-run byte deletion limit exceeded")
    if usage.eligible_files <= 0:
        blockers.append("eligible file count is invalid")
    elif (
        usage.files_deleted + 1
    ) / usage.eligible_files * 100 > limits.max_percentage_deleted_per_run:
        blockers.append("per-run percentage deletion limit exceeded")
    return GateDecision(not blockers, tuple(blockers))
