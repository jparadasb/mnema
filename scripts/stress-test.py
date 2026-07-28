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
from mnema.config import SourcePolicy
from mnema.domain.states import ArchiveState
from mnema.domain.workflow import ArchiveWorkflow
from mnema.jobs import Database
from mnema.jobs.models import ArchiveItem, AuditEvent
from mnema.jobs.state_service import transition_item
from mnema.worker.recovery import reconcile_interrupted_items

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
        cold=S3EncryptedColdStorage(
            bucket=backend.s3_bucket,
            key=key,
            endpoint_url=backend.s3_endpoint,
            access_key_file=backend.s3_access_key_file,
            secret_key_file=backend.s3_secret_key_file,
            create_bucket_if_missing=True,
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
        with database.session() as session:
            item = session.get(ArchiveItem, item_id)
            if item is None or item.state != ArchiveState.FAILED_RETRYABLE:
                raise RuntimeError(f"{stage} did not become retryable")
            transition_item(
                session,
                item,
                ArchiveState.QUEUED,
                actor="stress-harness",
                details={"approved_test_retry": True},
            )
            await workflow.archive(session, item)
            if item.state != ArchiveState.QUARANTINED:
                raise RuntimeError(f"{stage} retry did not finish")
            local_verified = await workflow.restore_local(item, root / "local.restore")
            remote_verified = await workflow.restore_remote(item, root / "remote.restore")
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
            "recovered_state": ArchiveState.FAILED_RETRYABLE.value,
            "final_state": ArchiveState.QUARANTINED.value,
            "local_restore_verified": local_verified,
            "remote_restore_verified": remote_verified,
            "local_snapshots": snapshots,
            "cold_objects": cold_objects,
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
    parser.add_argument("--mode", choices=("large", "small", "failure", "all"), default="all")
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
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Use 16 MiB and 32 small files for quick harness validation.",
    )
    args = parser.parse_args()
    if args.large_bytes < 1 or args.small_files < 1 or args.small_bytes < 1:
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
