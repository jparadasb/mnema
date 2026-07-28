from __future__ import annotations

import asyncio
import logging
import signal
import socket

from mnema.config import Settings
from mnema.diagnostics.health import startup_checks
from mnema.jobs import Database, DurableQueue
from mnema.jobs.models import RuntimeSetting
from mnema.worker.recovery import reconcile_interrupted_items

LOGGER = logging.getLogger("mnema.worker")


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
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=5)
                except TimeoutError:
                    pass
        finally:
            self.database.close()
            LOGGER.info("worker stopped", extra={"worker_id": self.worker_id})

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
