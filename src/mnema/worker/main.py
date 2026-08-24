from __future__ import annotations

import asyncio
import logging
import signal
import socket
from contextlib import suppress

from mnema.config import Settings, SourcePolicy
from mnema.diagnostics.health import startup_checks
from mnema.domain.factory import build_local_workflow
from mnema.file_provider.service import (
    mark_upload_failed,
    project_verified_archives,
    promote_upload,
    reap_expired_uploads,
    reconcile_sealing_uploads,
)
from mnema.jobs import Database, DurableQueue
from mnema.jobs.models import ArchiveItem, Job, RuntimeSetting
from mnema.worker.recovery import reconcile_interrupted_items

LOGGER = logging.getLogger("mnema.worker")


LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.database_url)
        self.queue = DurableQueue()
        self.stop_event = asyncio.Event()
        self.worker_id = f"{socket.gethostname()}-{id(self)}"

    async def run(self) -> None:
        self.database.create_schema()
        health = startup_checks(
            self.database,
            self.settings.active_root,
            self.settings.backup_root,
            self.settings.staging_root,
            smart_health_file=self.settings.smart_health_file,
            require_smart_health=self.settings.require_smart_health,
            recover_expired_jobs=True,
        )
        if not health.healthy:
            self._pause_deletion()
            self.database.close()
            raise RuntimeError("startup safety checks failed; deletion remains paused")
        if health.partial_files:
            LOGGER.warning(
                "partial staging files require reconciliation",
                extra={"count": len(health.partial_files)},
            )
        reconcile_interrupted_items(self.database)
        recovered_uploads = reconcile_sealing_uploads(self.database, self.settings)
        if recovered_uploads:
            LOGGER.warning(
                "recovered interrupted File Provider uploads",
                extra={"count": recovered_uploads},
            )
        reaped = reap_expired_uploads(self.database, self.settings)
        if reaped:
            LOGGER.warning("released expired upload staging", extra={"count": reaped})
        loop = asyncio.get_running_loop()
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(name, self.stop_event.set)
        LOGGER.info("worker started", extra={"worker_id": self.worker_id})
        try:
            while not self.stop_event.is_set():
                with self.database.session() as session:
                    recovered = self.queue.recover_expired(session)
                    if recovered:
                        LOGGER.warning(
                            "recovered expired jobs",
                            extra={"count": recovered},
                        )
                if await self._process_file_provider_job():
                    continue
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=5)
                except TimeoutError:
                    pass
        finally:
            self.database.close()
            LOGGER.info("worker stopped", extra={"worker_id": self.worker_id})

    async def _heartbeat(self, job_id: int) -> None:
        """Keep a long-running job's lease alive.

        Without this, any concurrent lease recovery reclaims the job while it is
        still executing, and the cold-storage steps routinely outlast the lease.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                with self.database.session() as session:
                    job = session.get(Job, job_id)
                    if job is None or job.lease_owner != self.worker_id:
                        return
                    self.queue.heartbeat(session, job, self.worker_id, lease_seconds=LEASE_SECONDS)
            except Exception:
                LOGGER.warning("job heartbeat failed", extra={"job_id": job_id})

    async def _process_file_provider_job(self) -> bool:
        with self.database.session() as session:
            job = self.queue.lease(
                session,
                worker_id=self.worker_id,
                adapter="file_provider",
                lease_seconds=LEASE_SECONDS,
                adapter_limit=self.settings.per_adapter_concurrency,
                global_limit=self.settings.worker_concurrency,
            )
        if job is None:
            return False
        job_id = job.id
        item_id = int(job.payload.get("archive_item_id", 0))
        provider_item_id = str(job.payload.get("file_provider_item_id", ""))
        heartbeat = asyncio.create_task(self._heartbeat(job_id))
        try:
            workflow = build_local_workflow(
                self.settings,
                policy=SourcePolicy(
                    archive_after_days=0,
                    stability_window_hours=0,
                    quarantine_days=7,
                    dry_run=False,
                    manual_approval=False,
                    deletion_enabled=False,
                ),
            )
            with self.database.session() as session:
                item = session.get(ArchiveItem, item_id)
                if item is None:
                    raise RuntimeError("queued File Provider archive item is missing")
                await workflow.archive(session, item)
                promote_upload(session, item, provider_item_id)
                # Publishing is part of archiving, not a separate ritual: an item
                # nobody can see has not finished being archived.
                project_verified_archives(session)
                active_job = session.get(Job, job_id)
                if active_job is None:
                    raise RuntimeError("queued File Provider job is missing")
                if not self.queue.succeed(session, active_job, self.worker_id):
                    LOGGER.warning(
                        "job completed after its lease was reclaimed",
                        extra={"job_id": job_id, "item_id": item_id},
                    )
        except Exception as error:
            LOGGER.exception("File Provider archive job failed", extra={"item_id": item_id})
            with self.database.session() as session:
                active_job = session.get(Job, job_id)
                if active_job is None:
                    LOGGER.error("failed File Provider job is missing", extra={"job_id": job_id})
                    return True
                self.queue.fail(session, active_job, self.worker_id, str(error))
                if active_job.attempts >= active_job.max_attempts:
                    mark_upload_failed(session, provider_item_id, f"archive_failed:job-{job_id}")
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    def _pause_deletion(self) -> None:
        with self.database.session() as session:
            for key, value in (
                ("global_deletion_enabled", "false"),
                ("safety_lock", "true"),
            ):
                setting = session.get(RuntimeSetting, key)
                if setting is None:
                    session.add(RuntimeSetting(key=key, value=value))
                else:
                    setting.value = value


def run_worker() -> None:
    asyncio.run(Worker(Settings()).run())
