from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import subprocess
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
from mnema.admin.cli import register_admin_commands
from mnema.admin.host import ApplianceManager
from mnema.config import DeletionLimits, Settings, SourcePolicy
from mnema.diagnostics.health import startup_checks
from mnema.domain.factory import build_icloud_workflow, build_local_workflow
from mnema.domain.states import ArchiveState
from mnema.domain.workflow import ArchiveWorkflow
from mnema.jobs import Database
from mnema.jobs.models import ArchiveItem, AuditEvent, Job, RuntimeSetting
from mnema.policies.deletion import DeletionRunUsage
from mnema.worker.main import run_worker

app = typer.Typer(no_args_is_help=True, help="Mnema — Your cloud, remembered.")
register_admin_commands(app)


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
    if Path("/opt/mnema/compose.yaml").is_file() and not Path("/.dockerenv").exists():
        ApplianceManager().status()
        return
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
                        ArchiveState.COLD_ARCHIVE_PENDING,
                        ArchiveState.COLD_ARCHIVED,
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


@app.command("apply-policy", hidden=True)
def apply_policy(
    archive_after_days: Annotated[int, typer.Option(min=0, max=36500)] = 30,
    stability_window_hours: Annotated[int, typer.Option(min=0, max=8760)] = 24,
    quarantine_days: Annotated[int, typer.Option(min=1, max=3650)] = 7,
) -> None:
    """Apply fail-closed policy values supplied by host administration."""
    policy = SourcePolicy(
        archive_after_days=archive_after_days,
        stability_window_hours=stability_window_hours,
        quarantine_days=quarantine_days,
        dry_run=True,
        manual_approval=True,
        deletion_enabled=False,
    )
    database = _database(Settings())
    _set_runtime_value(database, "archive_policy", policy.model_dump_json())
    _set_runtime_value(database, "global_deletion_enabled", "false")
    _set_runtime_value(database, "safety_lock", "true")
    typer.echo("Archive policy applied; deletion remains paused.")


@app.command("cold-storage-check", hidden=True)
def cold_storage_check() -> None:
    """Run an idempotent encrypted upload and independent restore canary."""

    async def run() -> None:
        settings = Settings()
        workflow = _test_workflow(settings)
        if not await workflow.cold.available():
            typer.echo("Cold storage is unavailable.", err=True)
            raise typer.Exit(2)
        with tempfile.TemporaryDirectory(prefix="mnema-cold-check-") as directory:
            root = Path(directory)
            source = root / "source.bin"
            restored = root / "restored.bin"
            digest = hashlib.sha256()
            block = hashlib.sha256(b"mnema-cold-storage-configuration-proof").digest() * 2048
            with source.open("xb") as output:
                for _ in range(16):
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            expected = digest.hexdigest()
            receipt = await workflow.cold.upload(
                source,
                object_identifier=source.name,
                idempotency_key="configuration-proof-v1",
            )
            repeated = await workflow.cold.upload(
                source,
                object_identifier=source.name,
                idempotency_key="configuration-proof-v1",
            )
            if receipt != repeated:
                raise RuntimeError("cold-storage idempotent receipt changed")
            if not await workflow.cold.verify(receipt, expected):
                raise RuntimeError("cold-storage independent verification failed")
            await workflow.cold.restore(receipt, restored)
            if sha256_file(restored) != expected:
                raise RuntimeError("cold-storage restored plaintext hash mismatch")
            typer.echo(
                json.dumps(
                    {
                        "provider": receipt.provider,
                        "remote_size": receipt.remote_size,
                        "idempotent": True,
                        "verified": True,
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
    if Path("/opt/mnema/compose.yaml").is_file() and not Path("/.dockerenv").exists():
        ApplianceManager().runtime_command("verify")
        return
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
    if Path("/opt/mnema/compose.yaml").is_file() and not Path("/.dockerenv").exists():
        ApplianceManager().runtime_command("diagnostics")
        return
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


def _icloudpd_arguments(settings: Settings) -> list[str]:
    return [
        "/usr/local/bin/icloudpd",
        "--no-progress-bar",
        "--log-level",
        "info",
        "--password-provider",
        "console",
        "--mfa-provider",
        "console",
        "--username",
        settings.icloud_apple_id,
        "--cookie-directory",
        str(settings.icloud_session_directory),
        "--library",
        settings.icloud_library,
        "--directory",
        str(settings.icloud_import_root),
        "--size",
        "original",
        "--live-photo-size",
        "original",
        "--folder-structure",
        "{:%Y/%m/%d}",
        "--file-match-policy",
        "name-id7",
    ]


def _require_icloud(settings: Settings) -> None:
    if not settings.icloud_enabled or not settings.icloud_apple_id:
        raise RuntimeError("iCloud Photos is not configured")
    settings.icloud_session_directory.mkdir(parents=True, exist_ok=True)


def _run_icloudpd(arguments: list[str], *, interactive: bool) -> None:
    result = subprocess.run(  # noqa: S603 - fixed executable and audited argument array
        arguments,
        check=False,
        stdin=None if interactive else subprocess.DEVNULL,
    )
    if result.returncode:
        raise RuntimeError("iCloud authentication or download failed")


@app.command("icloud-auth-internal", hidden=True)
def icloud_auth_internal() -> None:
    settings = Settings()
    _require_icloud(settings)
    _run_icloudpd(
        [
            "/usr/local/bin/icloudpd",
            "--log-level",
            "info",
            "--password-provider",
            "console",
            "--mfa-provider",
            "console",
            "--username",
            settings.icloud_apple_id,
            "--cookie-directory",
            str(settings.icloud_session_directory),
            "--auth-only",
        ],
        interactive=True,
    )
    typer.echo("iCloud authentication succeeded.")


@app.command("icloud-preview-internal", hidden=True)
def icloud_preview_internal() -> None:
    settings = Settings()
    _require_icloud(settings)
    base = _icloudpd_arguments(settings)
    arguments = [base[0], "--only-print-filenames", *base[1:]]
    process = subprocess.Popen(  # noqa: S603 - fixed executable and audited argument array
        arguments,
        stdout=subprocess.PIPE,
        text=True,
    )
    count = 0
    assert process.stdout is not None
    for line in process.stdout:
        value = line.rstrip()
        if value:
            count += 1
            if count <= 20:
                typer.echo(value)
    if process.wait():
        raise RuntimeError("iCloud preview failed")
    typer.echo(f"assets={count} download=false")


@app.command("icloud-sync-internal", hidden=True)
def icloud_sync_internal() -> None:
    async def run() -> None:
        settings = Settings()
        _require_icloud(settings)
        settings.icloud_import_root.mkdir(parents=True, exist_ok=True)
        database = _database(settings)
        _set_runtime_value(database, "icloud_last_run_started", datetime.now(UTC).isoformat())
        _set_runtime_value(database, "icloud_last_result", "running")
        try:
            _run_icloudpd(_icloudpd_arguments(settings), interactive=False)
            with database.session() as session:
                value = session.get(RuntimeSetting, "archive_policy")
                policy = (
                    SourcePolicy.model_validate_json(value.value)
                    if value and value.value
                    else SourcePolicy()
                )
                policy = policy.model_copy(
                    update={"deletion_enabled": False, "manual_approval": True}
                )
                workflow = build_icloud_workflow(settings, policy=policy)
                items = await workflow.discover(session)
                archived = 0
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
                        ArchiveState.COLD_ARCHIVE_PENDING,
                        ArchiveState.COLD_ARCHIVED,
                        ArchiveState.SOURCE_CHANGED,
                        ArchiveState.FAILED_RETRYABLE,
                    }:
                        await workflow.archive(session, item)
                        archived += 1
            _set_runtime_value(database, "icloud_last_result", "succeeded")
            _set_runtime_value(database, "icloud_last_success", datetime.now(UTC).isoformat())
            _set_runtime_value(database, "icloud_last_error", "")
            typer.echo(f"discovered={len(items)} archived={archived}")
        except Exception as error:
            _set_runtime_value(database, "icloud_last_result", "failed")
            _set_runtime_value(database, "icloud_last_error", type(error).__name__)
            raise
        finally:
            database.close()

    asyncio.run(run())


@app.command("icloud-status-internal", hidden=True)
def icloud_status_internal() -> None:
    settings = Settings()
    database = _database(settings)
    with database.session() as session:
        total = (
            session.scalar(
                select(func.count())
                .select_from(ArchiveItem)
                .where(ArchiveItem.source_provider == "icloud_photos")
            )
            or 0
        )
        last_result = session.get(RuntimeSetting, "icloud_last_result")
        last_success = session.get(RuntimeSetting, "icloud_last_success")
        last_error = session.get(RuntimeSetting, "icloud_last_error")
    authenticated = settings.icloud_session_directory.is_dir() and any(
        candidate.is_file() and not candidate.is_symlink()
        for candidate in settings.icloud_session_directory.rglob("*")
    )
    typer.echo(
        json.dumps(
            {
                "enabled": settings.icloud_enabled,
                "authenticated": authenticated,
                "reauthentication_required": settings.icloud_enabled and not authenticated,
                "items": total,
                "last_result": last_result.value if last_result else "never",
                "last_success": last_success.value if last_success else None,
                "last_error_type": last_error.value if last_error and last_error.value else None,
            },
            indent=2,
        )
    )
    database.close()


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
