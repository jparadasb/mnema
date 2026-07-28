from dataclasses import replace
from datetime import UTC, datetime, timedelta

from mnema.config import DeletionLimits
from mnema.policies.deletion import (
    DeletionFacts,
    DeletionRunUsage,
    evaluate_deletion_gate,
)


def complete_facts(now: datetime) -> DeletionFacts:
    return DeletionFacts(
        local_exists=True,
        local_size_matches=True,
        local_sha256_exists=True,
        local_sha256_matches=True,
        kopia_verified=True,
        remote_verified=True,
        quarantine_expires_at=now - timedelta(seconds=1),
        source_exists=True,
        source_unchanged=True,
        source_identity_matches=True,
        active_disk_healthy=True,
        backup_disk_healthy=True,
        storage_devices_differ=True,
        sqlite_integrity_healthy=True,
        remote_available=True,
        global_deletion_enabled=True,
        source_deletion_enabled=True,
        safety_lock=False,
    )


def test_all_conditions_allow_deletion() -> None:
    now = datetime.now(UTC)
    decision = evaluate_deletion_gate(
        complete_facts(now),
        DeletionRunUsage(0, 0, 100),
        DeletionLimits(max_percentage_deleted_per_run=1),
        item_size=10,
        now=now,
    )
    assert decision.allowed


def test_every_missing_receipt_or_health_fact_blocks() -> None:
    now = datetime.now(UTC)
    facts = complete_facts(now)
    for field in (
        "local_exists",
        "local_size_matches",
        "local_sha256_exists",
        "local_sha256_matches",
        "kopia_verified",
        "remote_verified",
        "source_exists",
        "source_unchanged",
        "source_identity_matches",
        "active_disk_healthy",
        "backup_disk_healthy",
        "storage_devices_differ",
        "sqlite_integrity_healthy",
        "remote_available",
        "global_deletion_enabled",
        "source_deletion_enabled",
    ):
        decision = evaluate_deletion_gate(
            replace(facts, **{field: False}),
            DeletionRunUsage(0, 0, 100),
            DeletionLimits(max_percentage_deleted_per_run=100),
            item_size=10,
            now=now,
        )
        assert not decision.allowed, field


def test_quarantine_pause_and_limits_block() -> None:
    now = datetime.now(UTC)
    facts = replace(
        complete_facts(now),
        quarantine_expires_at=now + timedelta(days=1),
        safety_lock=True,
    )
    decision = evaluate_deletion_gate(
        facts,
        DeletionRunUsage(5, 100, 5),
        DeletionLimits(max_files_deleted_per_run=5, max_percentage_deleted_per_run=1),
        item_size=10,
        now=now,
    )
    assert not decision.allowed
    assert "quarantine has not elapsed" in decision.blockers
    assert "safety lock active" in decision.blockers
