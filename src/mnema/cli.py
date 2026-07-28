from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from sqlalchemy import func, select

from mnema.adapters.backup.filesystem import FilesystemVersionedBackup
from mnema.adapters.cold_storage.local import LocalEncryptedColdStorage
from mnema.adapters.nas.fileops import sha256_file
from mnema.adapters.sources.local import LocalFilesystemSourceAdapter
from mnema.config import DeletionLimits, Settings, SourcePolicy
from mnema.diagnostics.health import startup_checks
from mnema.domain.factory import build_local_workflow
from mnema.domain.states import ArchiveState
from mnema.domain.workflow import ArchiveWorkflow
from mnema.jobs import Database
from mnema.jobs.models import ArchiveItem, AuditEvent, Job, RuntimeSetting
from mnema.policies.deletion import DeletionRunUsage
from mnema.worker.main import run_worker

app = typer.Typer(no_args_is_help=True, help="Mnema — Your cloud, remembered.")


def _database(settings: Settings) -> Database:
    database = Database(settings.database_url)
    database.create_schema()
    return database


def _runtime_value(database: Database, key: str, default: str) -> str:
    with database.session() as session:
        setting = session.get(RuntimeSetting, key)
        return setting.value if setting else default


def _set_runtime_value(database: Database, key: str, value: str) -> None:
    with database.session() as session:
        setting = session.get(RuntimeSetting, key)
        if setting is None:
            session.add(RuntimeSetting(key=key, value=value))
        else:
            setting.value = value


def _test_workflow(settings: Settings, *, deletion_enabled: bool = False) -> ArchiveWorkflow:
    return build_local_workflow(
        settings,
        policy=SourcePolicy(
            archive_after_days=30,
            stability_window_hours=24,
            quarantine_days=7,
            deletion_enabled=deletion_enabled,
            manual_approval=False,
        ),
        deletion_enabled=deletion_enabled,
    )


@app.command()
def status() -> None:
    """Show archive and job state without changing data."""
    settings = Settings()
    database = _database(settings)
    with database.session() as session:
        payload = {
            "items": session.scalar(select(func.count()).select_from(ArchiveItem)) or 0,
            "jobs": session.scalar(select(func.count()).select_from(Job)) or 0,
            "audit_events": session.scalar(select(func.count()).select_from(AuditEvent)) or 0,
            "deletion_enabled": _runtime_value(database, "global_deletion_enabled", "false")
            == "true",
            "safety_lock": _runtime_value(database, "safety_lock", "true") == "true",
        }
    typer.echo(json.dumps(payload, indent=2))


@app.command()
def scan(
    archive: Annotated[
        bool,
        typer.Option(help="Run local test archive workflow after discovery."),
    ] = False,
) -> None:
    """Discover local test files; optionally archive eligible items."""

    async def run() -> None:
        settings = Settings()
        database = _database(settings)
        workflow = _test_workflow(settings)
        with database.session() as session:
            items = await workflow.discover(session)
            if archive:
                for item in items:
                    if item.state in {
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
                    }:
                        await workflow.archive(session, item)
            typer.echo(f"discovered={len(items)} archive={archive}")

    asyncio.run(run())


@app.command("dry-run")
def dry_run() -> None:
    """Preview policy decisions without copying or deleting."""

    async def run() -> None:
        settings = Settings()
        workflow = _test_workflow(settings)
        page = await workflow.source.discover()
        now = datetime.now(UTC)
        from mnema.policies import evaluate_policy

        for item in page.objects:
            decision = evaluate_policy(item, workflow.policy, now=now)
            typer.echo(
                json.dumps(
                    {
                        "path": item.relative_path,
                        "eligible": decision.eligible,
                        "reasons": decision.reasons,
                    }
                )
            )

    asyncio.run(run())


@app.command("pause-deletion")
def pause_deletion() -> None:
    database = _database(Settings())
    _set_runtime_value(database, "global_deletion_enabled", "false")
    _set_runtime_value(database, "safety_lock", "true")
    typer.echo("Deletion paused; safety lock enabled.")


@app.command("resume-deletion")
def resume_deletion() -> None:
    """Fail closed unless storage and database prerequisites pass."""
    settings = Settings()
    database = _database(settings)
    health = startup_checks(
        database,
        settings.active_root,
        settings.backup_root,
        settings.staging_root,
        smart_health_file=settings.smart_health_file,
        require_smart_health=settings.require_smart_health,
    )
    if not health.healthy:
        typer.echo("Refused: startup safety prerequisites failed.", err=True)
        raise typer.Exit(2)
    _set_runtime_value(database, "global_deletion_enabled", "true")
    _set_runtime_value(database, "safety_lock", "false")
    typer.echo("Deletion gate enabled. Per-source policy and item checks still apply.")


@app.command()
def verify() -> None:
    """Independently verify active files recorded in SQLite."""
    database = _database(Settings())
    failed = 0
    with database.session() as session:
        items = session.scalars(select(ArchiveItem).where(ArchiveItem.nas_path.is_not(None))).all()
        for item in items:
            path = Path(item.nas_path or "")
            good = (
                path.is_file()
                and item.plaintext_sha256 is not None
                and sha256_file(path) == item.plaintext_sha256
            )
            typer.echo(f"{item.id} {item.original_path}: {'ok' if good else 'FAILED'}")
            failed += not good
    if failed:
        raise typer.Exit(1)


@app.command()
def diagnostics() -> None:
    settings = Settings()
    database = _database(settings)
    health = startup_checks(
        database,
        settings.active_root,
        settings.backup_root,
        settings.staging_root,
        smart_health_file=settings.smart_health_file,
        require_smart_health=settings.require_smart_health,
    )
    typer.echo(
        json.dumps(
            {
                "healthy": health.healthy,
                "active": health.active.__dict__ | {"path": str(health.active.path)},
                "backup": health.backup.__dict__ | {"path": str(health.backup.path)},
                "devices_differ": health.devices_differ,
                "staging_shares_active_device": health.staging_shares_active_device,
                "sqlite_healthy": health.sqlite_healthy,
                "expired_jobs_recovered": health.expired_jobs_recovered,
                "partial_files": [str(path) for path in health.partial_files],
                "smart_healthy": health.smart_healthy,
                "smart_required": health.smart_required,
            },
            indent=2,
        )
    )


@app.command()
def web(
    host: str = "0.0.0.0",  # noqa: S104 - container server must accept mapped traffic
    port: int = 8080,
) -> None:
    from mnema.web.app import create_app

    uvicorn.run(create_app(Settings()), host=host, port=port)


@app.command()
def worker() -> None:
    run_worker()


@app.command("test-vertical-slice", hidden=True)
def test_vertical_slice() -> None:
    """Run complete temporary local proof, including guarded test deletion."""

    async def run() -> None:
        os.environ["MNEMA_ALLOW_TEST_DELETE"] = "1"
        with tempfile.TemporaryDirectory(prefix="mnema-proof-") as directory:
            root = Path(directory)
            source_root = root / "source"
            active_root = root / "active"
            backup_root = root / "backup"
            staging_root = root / "staging"
            for path in (source_root, active_root, backup_root, staging_root):
                path.mkdir()
            original = source_root / "proof.txt"
            original.write_bytes(secrets.token_bytes(2 * 1024 * 1024))
            database = Database(f"sqlite:///{root / 'mnema.db'}")
            database.create_schema()
            workflow = ArchiveWorkflow(
                source=LocalFilesystemSourceAdapter(source_root, allow_delete=True),
                backup=FilesystemVersionedBackup(backup_root / "versions"),
                cold=LocalEncryptedColdStorage(backup_root / "cold", secrets.token_bytes(32)),
                active_root=active_root,
                staging_root=staging_root,
                policy=SourcePolicy(
                    archive_after_days=0,
                    stability_window_hours=0,
                    quarantine_days=0,
                    deletion_enabled=True,
                    manual_approval=False,
                ),
            )
            with database.session() as session:
                item = (await workflow.discover(session))[0]
                await workflow.archive(session, item)
                local_restore = root / "local-restore"
                remote_restore = root / "remote-restore"
                if not await workflow.restore_local(item, local_restore):
                    raise RuntimeError("local restore verification failed")
                if not await workflow.restore_remote(item, remote_restore):
                    raise RuntimeError("remote restore verification failed")
                decision = await workflow.deletion_decision(
                    item,
                    active_disk_healthy=True,
                    backup_disk_healthy=True,
                    storage_devices_differ=True,
                    sqlite_integrity_healthy=database.integrity_check(),
                    global_deletion_enabled=True,
                    safety_lock=False,
                    usage=DeletionRunUsage(0, 0, 100),
                    limits=DeletionLimits(max_percentage_deleted_per_run=100),
                    now=datetime.now(UTC),
                )
                await workflow.delete_test_item(session, item, decision)
                if item.state != ArchiveState.ARCHIVED:
                    raise RuntimeError(f"unexpected final state: {item.state}")
                typer.echo(
                    json.dumps(
                        {
                            "state": item.state,
                            "source_deleted": not original.exists(),
                            "hash": item.plaintext_sha256,
                            "audit_events": len(item.audit_events),
                        },
                        indent=2,
                    )
                )

    asyncio.run(run())


if __name__ == "__main__":
    app()
