#!/usr/bin/env python3
"""Temporary-data-only stress and restart-recovery harness for Mnema."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import resource
import shutil
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from mnema.adapters.backup.base import BackupReceipt, VersionedBackup
from mnema.adapters.backup.filesystem import FilesystemVersionedBackup
from mnema.adapters.backup.kopia import KopiaBackup
from mnema.adapters.cold_storage.base import ColdReceipt, ColdStorage
from mnema.adapters.cold_storage.local import LocalEncryptedColdStorage
from mnema.adapters.cold_storage.s3 import S3EncryptedColdStorage
from mnema.adapters.sources.local import LocalFilesystemSourceAdapter
from mnema.config import Settings, SourcePolicy
from mnema.domain.states import ArchiveState
from mnema.domain.workflow import ArchiveWorkflow
from mnema.jobs import Database
from mnema.jobs.models import ArchiveItem, AuditEvent, RuntimeSetting
from mnema.jobs.state_service import transition_item
from mnema.worker.main import Worker
from mnema.worker.recovery import RESUME_IN_PLACE, reconcile_interrupted_items

MIB = 1024 * 1024
GIB = 1024 * MIB


@dataclass(frozen=True)
class ScaleCase:
    name: str
    file_count: int
    file_size: int
    concurrency: int

    @property
    def source_bytes(self) -> int:
        return self.file_count * self.file_size


@dataclass(frozen=True)
class ExternalBackend:
    kopia_password_file: Path
    s3_endpoint: str
    s3_bucket: str
    s3_access_key_file: Path
    s3_secret_key_file: Path
    cold_key_file: Path


class InterruptOnceBackup(FilesystemVersionedBackup):
    def __init__(self, repository: Path) -> None:
        super().__init__(repository)
        self.interrupted = False

    async def snapshot(self, source: Path, idempotency_key: str) -> BackupReceipt:
        receipt = await super().snapshot(source, idempotency_key)
        if not self.interrupted:
            self.interrupted = True
            raise InterruptedError("simulated interruption after backup write")
        return receipt


class InterruptOnceColdStorage(LocalEncryptedColdStorage):
    def __init__(self, root: Path, key: bytes) -> None:
        super().__init__(root, key)
        self.interrupted = False

    async def upload(
        self,
        source: Path,
        object_identifier: str,
        idempotency_key: str,
    ) -> ColdReceipt:
        receipt = await super().upload(source, object_identifier, idempotency_key)
        if not self.interrupted:
            self.interrupted = True
            raise InterruptedError("simulated interruption after cold-storage write")
        return receipt


class SignalingS3ColdStorage(S3EncryptedColdStorage):
    def __init__(self, *, upload_started_file: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.upload_started_file = upload_started_file

    async def upload(
        self,
        source: Path,
        object_identifier: str,
        idempotency_key: str,
    ) -> ColdReceipt:
        with self.upload_started_file.open("xb") as marker:
            marker.write(b"upload-started\n")
            marker.flush()
            os.fsync(marker.fileno())
        return await super().upload(source, object_identifier, idempotency_key)


def write_stream(path: Path, size: int, seed: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(seed.encode()).digest()
    block = (digest * ((MIB // len(digest)) + 1))[:MIB]
    remaining = size
    with path.open("xb") as file:
        while remaining:
            chunk = block[: min(len(block), remaining)]
            file.write(chunk)
            remaining -= len(chunk)
        file.flush()
        os.fsync(file.fileno())


def roots_for(root: Path) -> dict[str, Path]:
    roots = {name: root / name for name in ("source", "active", "backup", "cold", "staging")}
    for path in roots.values():
        path.mkdir()
    return roots


def workflow_for(
    roots: dict[str, Path],
    *,
    source: LocalFilesystemSourceAdapter | None = None,
    backup: VersionedBackup | None = None,
    cold: ColdStorage | None = None,
) -> ArchiveWorkflow:
    return ArchiveWorkflow(
        source=source or LocalFilesystemSourceAdapter(roots["source"]),
        backup=backup or FilesystemVersionedBackup(roots["backup"]),
        cold=cold or LocalEncryptedColdStorage(roots["cold"], b"s" * 32),
        active_root=roots["active"],
        staging_root=roots["staging"],
        policy=SourcePolicy(
            archive_after_days=0,
            stability_window_hours=0,
            quarantine_days=7,
        ),
    )


def external_workflow(
    roots: dict[str, Path],
    backend: ExternalBackend,
    *,
    upload_started_file: Path | None = None,
) -> ArchiveWorkflow:
    key = backend.cold_key_file.read_bytes()
    if len(key) != 32:
        raise ValueError("external cold encryption key must contain exactly 32 bytes")
    return workflow_for(
        roots,
        backup=KopiaBackup(
            roots["backup"] / "kopia-repository",
            backend.kopia_password_file,
            roots["backup"] / "kopia-config" / "repository.config",
        ),
        cold=(
            SignalingS3ColdStorage(
                bucket=backend.s3_bucket,
                key=key,
                endpoint_url=backend.s3_endpoint,
                access_key_file=backend.s3_access_key_file,
                secret_key_file=backend.s3_secret_key_file,
                create_bucket_if_missing=True,
                upload_started_file=upload_started_file,
            )
            if upload_started_file is not None
            else S3EncryptedColdStorage(
                bucket=backend.s3_bucket,
                key=key,
                endpoint_url=backend.s3_endpoint,
                access_key_file=backend.s3_access_key_file,
                secret_key_file=backend.s3_secret_key_file,
                create_bucket_if_missing=True,
            )
        ),
    )


def ensure_capacity(root: Path, case: ScaleCase) -> None:
    restore_bytes = min(case.file_count, 2) * case.file_size * 2
    required = case.source_bytes * 4 + restore_bytes + 256 * MIB
    free = shutil.disk_usage(root).free
    if free < required:
        raise RuntimeError(
            f"stress case needs approximately {required} free bytes; only {free} available"
        )


def physical_memory_bytes() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None
    return int(pages * page_size)


def s3_object_count(storage: S3EncryptedColdStorage) -> int:
    paginator = storage.client.get_paginator("list_objects_v2")
    return sum(
        int(page.get("KeyCount", 0))
        for page in paginator.paginate(Bucket=storage.bucket, Prefix="mnema/")
    )


def s3_multipart_upload_count(storage: S3EncryptedColdStorage) -> int:
    paginator = storage.client.get_paginator("list_multipart_uploads")
    return sum(
        len(page.get("Uploads", []))
        for page in paginator.paginate(Bucket=storage.bucket, Prefix="mnema/")
    )


async def archive_ids(
    database: Database,
    workflow: ArchiveWorkflow,
    item_ids: list[int],
    concurrency: int,
    workflow_factory: Callable[[], ArchiveWorkflow] | None = None,
    *,
    progress_offset: int = 0,
    progress_total: int | None = None,
) -> None:
    def archive_in_worker_thread(item_id: int) -> None:
        async def execute() -> None:
            with database.session() as session:
                item = session.get(ArchiveItem, item_id)
                if item is None:
                    raise RuntimeError(f"archive item {item_id} disappeared")
                worker_workflow = workflow_factory() if workflow_factory else workflow
                await worker_workflow.archive(session, item)

        asyncio.run(execute())

    total = progress_total or len(item_ids)
    progress_interval = max(1, total // 10)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for completed, _ in enumerate(
            executor.map(archive_in_worker_thread, item_ids),
            start=1,
        ):
            completed_total = progress_offset + completed
            if total >= 100 and (
                completed_total % progress_interval == 0 or completed_total == total
            ):
                print(
                    f"archive progress: {completed_total}/{total}",
                    file=sys.stderr,
                    flush=True,
                )


async def run_scale_case(
    case: ScaleCase,
    temporary_root: Path | None = None,
    external: ExternalBackend | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix=f"mnema-stress-{case.name}-",
        dir=temporary_root,
    ) as directory:
        root = Path(directory)
        ensure_capacity(root, case)
        roots = roots_for(root)
        for index in range(case.file_count):
            relative = (
                Path("large.bin")
                if case.file_count == 1
                else Path(f"group-{index // 100:04}") / f"small-{index:06}.bin"
            )
            write_stream(roots["source"] / relative, case.file_size, str(index))

        database_path = root / "mnema.sqlite"
        database = Database(f"sqlite:///{database_path}")
        database.create_schema()
        workflow = external_workflow(roots, external) if external else workflow_for(roots)
        with database.session() as session:
            items = await workflow.discover(session)
            item_ids = [item.id for item in items]
        workflow_factory = (
            (lambda: external_workflow(roots, external)) if external is not None else None
        )
        if external is not None and item_ids:
            await archive_ids(database, workflow, item_ids[:1], 1)
            await archive_ids(
                database,
                workflow,
                item_ids[1:],
                case.concurrency,
                workflow_factory,
                progress_offset=1,
                progress_total=len(item_ids),
            )
        else:
            await archive_ids(database, workflow, item_ids, case.concurrency)

        with database.session() as session:
            quarantined = session.scalar(
                select(func.count())
                .select_from(ArchiveItem)
                .where(ArchiveItem.state == ArchiveState.QUARANTINED)
            )
            audit_events = session.scalar(select(func.count()).select_from(AuditEvent))
            first = session.get(ArchiveItem, item_ids[0])
            last = session.get(ArchiveItem, item_ids[-1])
            if first is None or last is None:
                raise RuntimeError("archive receipts missing after stress run")
            samples = [("first", first)]
            if last.id != first.id:
                samples.append(("last", last))
            restore_copies_verified = 0
            for label, item in samples:
                local_restore = root / f"{label}.local.restore"
                remote_restore = root / f"{label}.remote.restore"
                if not await workflow.restore_local(item, local_restore):
                    raise RuntimeError(f"{label} local restore verification failed")
                restore_copies_verified += 1
                if not await workflow.restore_remote(item, remote_restore):
                    raise RuntimeError(f"{label} remote restore verification failed")
                restore_copies_verified += 1

        with database.engine.begin() as connection:
            connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        if external:
            if not isinstance(workflow.backup, KopiaBackup) or not isinstance(
                workflow.cold,
                S3EncryptedColdStorage,
            ):
                raise RuntimeError("external workflow adapters have an invalid type")
            snapshot_payload = await workflow.backup._run(
                "snapshot",
                "list",
                "--all",
                "--json",
            )
            local_snapshots = len(json.loads(snapshot_payload or b"[]"))
            cold_objects = await asyncio.to_thread(s3_object_count, workflow.cold)
        else:
            local_snapshots = len(list(roots["backup"].glob("*.snapshot")))
            cold_objects = len(list(roots["cold"].glob("*.mnema")))
        if local_snapshots != quarantined or cold_objects != quarantined:
            raise RuntimeError("snapshot or cold-object count differs from quarantined item count")
        database.close()
        memory = physical_memory_bytes()
        return {
            "mode": case.name,
            "backend": "kopia-minio" if external else "filesystem-local-cold",
            "files": case.file_count,
            "file_bytes": case.file_size,
            "source_bytes": case.source_bytes,
            "concurrency": case.concurrency,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "physical_memory_bytes": memory,
            "source_larger_than_physical_memory": (
                case.source_bytes > memory if memory is not None else None
            ),
            "database_bytes": database_path.stat().st_size,
            "quarantined": quarantined,
            "audit_events": audit_events,
            "local_snapshots": local_snapshots,
            "cold_objects": cold_objects,
            "restore_items_verified": len(samples),
            "restore_copies_verified": restore_copies_verified,
        }


async def run_failure_case(
    stage: str,
    temporary_root: Path | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix=f"mnema-failure-{stage}-",
        dir=temporary_root,
    ) as directory:
        root = Path(directory)
        roots = roots_for(root)
        write_stream(roots["source"] / "proof.bin", 2 * MIB + 17, stage)
        source = (
            LocalFilesystemSourceAdapter(roots["source"], interrupt_after_bytes=1024)
            if stage == "download"
            else LocalFilesystemSourceAdapter(roots["source"])
        )
        backup = (
            InterruptOnceBackup(roots["backup"])
            if stage == "backup"
            else FilesystemVersionedBackup(roots["backup"])
        )
        cold = (
            InterruptOnceColdStorage(roots["cold"], b"s" * 32)
            if stage == "cold_upload"
            else LocalEncryptedColdStorage(roots["cold"], b"s" * 32)
        )
        workflow = workflow_for(roots, source=source, backup=backup, cold=cold)
        database = Database(f"sqlite:///{root / 'mnema.sqlite'}")
        database.create_schema()
        with database.session() as session:
            item = (await workflow.discover(session))[0]
            item_id = item.id
            try:
                await workflow.archive(session, item)
            except InterruptedError:
                pass
            else:
                raise RuntimeError(f"{stage} interruption did not occur")
            interrupted_state = item.state

        if reconcile_interrupted_items(database) != 1:
            raise RuntimeError(f"{stage} restart reconciliation count was not one")
        workflow.source = LocalFilesystemSourceAdapter(roots["source"])
        workflow.backup = FilesystemVersionedBackup(roots["backup"])
        workflow.cold = LocalEncryptedColdStorage(roots["cold"], b"s" * 32)
        # Steps whose active copy is already committed and verified resume where
        # they stopped; only an unfinished transfer rewinds to the source.
        resumes_in_place = interrupted_state in RESUME_IN_PLACE
        expected_recovered = (
            interrupted_state if resumes_in_place else ArchiveState.FAILED_RETRYABLE
        )
        with database.session() as session:
            recovered_item = session.get(ArchiveItem, item_id)
            if recovered_item is None or recovered_item.state != expected_recovered:
                raise RuntimeError(
                    f"{stage} recovered to {recovered_item and recovered_item.state.value}, "
                    f"expected {expected_recovered.value}"
                )
            if not resumes_in_place:
                transition_item(
                    session,
                    recovered_item,
                    ArchiveState.QUEUED,
                    actor="stress-harness",
                    details={"approved_test_retry": True},
                )
            await workflow.archive(session, recovered_item)
            if recovered_item.state.value != ArchiveState.QUARANTINED.value:
                raise RuntimeError(f"{stage} retry did not finish")
            local_verified = await workflow.restore_local(
                recovered_item,
                root / "local.restore",
            )
            remote_verified = await workflow.restore_remote(
                recovered_item,
                root / "remote.restore",
            )
        snapshots = len(list(roots["backup"].glob("*.snapshot")))
        cold_objects = len(list(roots["cold"].glob("*.mnema")))
        database.close()
        if not local_verified or not remote_verified:
            raise RuntimeError(f"{stage} restore verification failed")
        if snapshots != 1 or cold_objects != 1:
            raise RuntimeError(f"{stage} retry created duplicate backup objects")
        return {
            "stage": stage,
            "interrupted_state": interrupted_state.value,
            "recovered_state": expected_recovered.value,
            "final_state": ArchiveState.QUARANTINED.value,
            "local_restore_verified": local_verified,
            "remote_restore_verified": remote_verified,
            "local_snapshots": snapshots,
            "cold_objects": cold_objects,
        }


def require_external_failure_paths(args: argparse.Namespace) -> tuple[Path, ExternalBackend]:
    if args.fault_root is None:
        raise RuntimeError("external failure mode requires --fault-root")
    if args.external_backend is None:
        raise RuntimeError("external failure mode requires external backend")
    return args.fault_root, args.external_backend


async def run_external_failure_attempt(args: argparse.Namespace) -> dict[str, Any]:
    fault_root, backend = require_external_failure_paths(args)
    if fault_root.exists():
        raise RuntimeError("fault root must not exist before attempt phase")
    fault_root.mkdir(parents=True)
    roots = roots_for(fault_root)
    write_stream(roots["source"] / "proof.bin", args.fault_bytes, "external-cold-upload")
    database = Database(f"sqlite:///{fault_root / 'mnema.sqlite'}")
    database.create_schema()
    workflow = external_workflow(
        roots,
        backend,
        upload_started_file=fault_root / "upload-started",
    )
    error_type: str | None = None
    with database.session() as session:
        item = (await workflow.discover(session))[0]
        item_id = item.id
        try:
            await workflow.archive(session, item)
        except Exception as error:
            error_type = type(error).__name__
        if error_type is None:
            raise RuntimeError("external upload was not interrupted")
        if item.state != ArchiveState.COLD_UPLOAD_PENDING:
            raise RuntimeError(f"unexpected interrupted state: {item.state.value}")
    integrity_healthy = database.integrity_check()
    database.close()
    if not integrity_healthy:
        raise RuntimeError("database integrity failed after interrupted upload")
    return {
        "phase": "attempt",
        "item_id": item_id,
        "interrupted_state": ArchiveState.COLD_UPLOAD_PENDING.value,
        "error_type": error_type,
        "database_integrity_healthy": integrity_healthy,
    }


async def run_external_failure_recovery(args: argparse.Namespace) -> dict[str, Any]:
    fault_root, backend = require_external_failure_paths(args)
    roots = {name: fault_root / name for name in ("source", "active", "backup", "cold", "staging")}
    if not all(path.is_dir() for path in roots.values()):
        raise RuntimeError("fault root does not contain a complete interrupted run")
    database = Database(f"sqlite:///{fault_root / 'mnema.sqlite'}")
    if reconcile_interrupted_items(database) != 1:
        raise RuntimeError("external restart reconciliation count was not one")
    workflow = external_workflow(roots, backend)
    with database.session() as session:
        item = session.scalar(select(ArchiveItem))
        # The interruption happens after the active copy is committed and
        # verified, so recovery resumes the cold step in place rather than
        # rewinding the item to a re-download.
        if item is None or item.state != ArchiveState.COLD_UPLOAD_PENDING:
            raise RuntimeError(
                f"external interrupted item is {item and item.state.value}, "
                f"expected {ArchiveState.COLD_UPLOAD_PENDING.value}"
            )
        recovered_state = item.state
        await workflow.archive(session, item)
        if item.state.value != ArchiveState.QUARANTINED.value:
            raise RuntimeError("external retry did not finish")
        local_verified = await workflow.restore_local(item, fault_root / "local.restore")
        remote_verified = await workflow.restore_remote(item, fault_root / "remote.restore")
        audit_events = int(session.scalar(select(func.count(AuditEvent.id))) or 0)
    if not isinstance(workflow.backup, KopiaBackup) or not isinstance(
        workflow.cold,
        S3EncryptedColdStorage,
    ):
        raise RuntimeError("external workflow adapters have an invalid type")
    snapshot_payload = await workflow.backup._run("snapshot", "list", "--all", "--json")
    snapshots = len(json.loads(snapshot_payload or b"[]"))
    objects = await asyncio.to_thread(s3_object_count, workflow.cold)
    multipart_uploads = await asyncio.to_thread(s3_multipart_upload_count, workflow.cold)
    integrity_healthy = database.integrity_check()
    database.close()
    if not local_verified or not remote_verified:
        raise RuntimeError("external recovery restore verification failed")
    if snapshots != 1 or objects != 1 or multipart_uploads != 0:
        raise RuntimeError("external retry left duplicate or incomplete backup objects")
    if not integrity_healthy:
        raise RuntimeError("database integrity failed after external recovery")
    return {
        "phase": "recover",
        "recovered_state": recovered_state.value,
        "final_state": ArchiveState.QUARANTINED.value,
        "local_restore_verified": local_verified,
        "remote_restore_verified": remote_verified,
        "local_snapshots": snapshots,
        "cold_objects": objects,
        "incomplete_multipart_uploads": multipart_uploads,
        "audit_events": audit_events,
        "database_integrity_healthy": integrity_healthy,
    }


async def run_missing_backup_case(temporary_root: Path | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix="mnema-missing-backup-",
        dir=temporary_root,
    ) as directory:
        root = Path(directory)
        active = root / "active"
        staging = active / ".mnema-staging"
        source = root / "source"
        for path in (active, staging, source):
            path.mkdir(parents=True)
        database_path = root / "mnema.sqlite"
        settings = Settings(
            database_url=f"sqlite:///{database_path}",
            active_root=active,
            backup_root=root / "missing-backup",
            staging_root=staging,
            source_root=source,
        )
        database = Database(settings.database_url)
        database.create_schema()
        with database.session() as session:
            session.add_all(
                [
                    RuntimeSetting(key="global_deletion_enabled", value="true"),
                    RuntimeSetting(key="safety_lock", value="false"),
                ]
            )
        database.close()

        error_type: str | None = None
        try:
            await Worker(settings).run()
        except RuntimeError as error:
            if "startup safety checks failed" not in str(error):
                raise
            error_type = type(error).__name__
        if error_type is None:
            raise RuntimeError("worker started with missing backup storage")

        reopened = Database(settings.database_url)
        with reopened.session() as session:
            deletion = session.get(RuntimeSetting, "global_deletion_enabled")
            safety_lock = session.get(RuntimeSetting, "safety_lock")
            deletion_enabled = deletion.value if deletion else None
            safety_lock_enabled = safety_lock.value if safety_lock else None
        integrity_healthy = reopened.integrity_check()
        reopened.close()
        if deletion_enabled != "false" or safety_lock_enabled != "true":
            raise RuntimeError("missing backup did not force deletion pause and safety lock")
        if not integrity_healthy:
            raise RuntimeError("database integrity failed after missing-backup startup")
        return {
            "stage": "missing_backup_startup",
            "worker_error_type": error_type,
            "backup_exists": settings.backup_root.exists(),
            "global_deletion_enabled": deletion_enabled,
            "safety_lock": safety_lock_enabled,
            "database_integrity_healthy": integrity_healthy,
        }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    results: dict[str, Any] = {"temporary_data_only": True, "deletion_exercised": False}
    external = (
        ExternalBackend(
            args.kopia_password_file,
            args.s3_endpoint,
            args.s3_bucket,
            args.s3_access_key_file,
            args.s3_secret_key_file,
            args.cold_key_file,
        )
        if args.backend == "external"
        else None
    )
    args.external_backend = external
    if args.mode == "external-failure":
        results["external_failure"] = (
            await run_external_failure_attempt(args)
            if args.fault_phase == "attempt"
            else await run_external_failure_recovery(args)
        )
        return results
    if args.mode in {"missing-backup", "all"}:
        results["missing_backup"] = await run_missing_backup_case(args.temporary_root)
    if args.mode == "missing-backup":
        return results
    if args.mode in {"large", "all"}:
        results["large"] = await run_scale_case(
            ScaleCase(
                "large",
                1,
                16 * MIB if args.smoke else args.large_bytes,
                args.concurrency,
            ),
            args.temporary_root,
            external,
        )
    if args.mode in {"small", "all"}:
        results["small"] = await run_scale_case(
            ScaleCase(
                "small",
                32 if args.smoke else args.small_files,
                1024 if args.smoke else args.small_bytes,
                args.concurrency,
            ),
            args.temporary_root,
            external,
        )
    if args.mode in {"failure", "all"}:
        results["failures"] = [
            await run_failure_case(stage, args.temporary_root)
            for stage in ("download", "backup", "cold_upload")
        ]
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Mnema archive stress tests using disposable temporary data only."
    )
    parser.add_argument(
        "--mode",
        choices=(
            "large",
            "small",
            "failure",
            "external-failure",
            "missing-backup",
            "all",
        ),
        default="all",
    )
    parser.add_argument(
        "--backend",
        choices=("local", "external"),
        default="local",
        help="Use test doubles or real Kopia and S3-compatible storage.",
    )
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=1)
    parser.add_argument("--large-bytes", type=int, default=5 * GIB)
    parser.add_argument("--small-files", type=int, default=10_000)
    parser.add_argument("--small-bytes", type=int, default=4096)
    parser.add_argument(
        "--temporary-root",
        type=Path,
        help="Existing writable directory under which disposable test directories are created.",
    )
    parser.add_argument("--kopia-password-file", type=Path)
    parser.add_argument("--s3-endpoint")
    parser.add_argument("--s3-bucket", default="mnema-stress")
    parser.add_argument("--s3-access-key-file", type=Path)
    parser.add_argument("--s3-secret-key-file", type=Path)
    parser.add_argument("--cold-key-file", type=Path)
    parser.add_argument("--fault-phase", choices=("attempt", "recover"), default="attempt")
    parser.add_argument("--fault-root", type=Path)
    parser.add_argument("--fault-bytes", type=int, default=256 * MIB)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Use 16 MiB and 32 small files for quick harness validation.",
    )
    args = parser.parse_args()
    if args.large_bytes < 1 or args.small_files < 1 or args.small_bytes < 1 or args.fault_bytes < 1:
        parser.error("sizes and file counts must be positive")
    if args.temporary_root is not None:
        if not args.temporary_root.is_dir():
            parser.error("temporary root must be an existing directory")
        if not os.access(args.temporary_root, os.W_OK):
            parser.error("temporary root must be writable")
    if args.backend == "external":
        required_files = (
            args.kopia_password_file,
            args.s3_access_key_file,
            args.s3_secret_key_file,
            args.cold_key_file,
        )
        if args.s3_endpoint is None or any(path is None for path in required_files):
            parser.error("external backend requires endpoint and all credential/key files")
        parsed_endpoint = urllib.parse.urlsplit(args.s3_endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
            parser.error("S3 endpoint must be an HTTP or HTTPS URL")
        if any(not path.is_file() for path in required_files):
            parser.error("external backend credential/key files must exist")
    return args


def main() -> None:
    print(json.dumps(asyncio.run(run(parse_args())), indent=2))


if __name__ == "__main__":
    main()
