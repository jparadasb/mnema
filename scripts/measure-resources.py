#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import resource
import secrets
import tempfile
import time
from pathlib import Path

from sqlalchemy import text

from mnema.adapters.backup.filesystem import FilesystemVersionedBackup
from mnema.adapters.cold_storage.local import LocalEncryptedColdStorage
from mnema.adapters.sources.local import LocalFilesystemSourceAdapter
from mnema.config import SourcePolicy
from mnema.domain.workflow import ArchiveWorkflow
from mnema.jobs import Database


def write_stream(path: Path, size: int) -> None:
    remaining = size
    with path.open("wb") as file:
        while remaining:
            chunk = secrets.token_bytes(min(1024 * 1024, remaining))
            file.write(chunk)
            remaining -= len(chunk)


async def measure(mode: str) -> dict[str, int | float | str]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"mnema-resource-{mode}-") as directory:
        root = Path(directory)
        source = root / "source"
        active = root / "active"
        backup = root / "backup"
        cold = root / "cold"
        staging = root / "staging"
        for path in (source, active, backup, cold, staging):
            path.mkdir()
        if mode == "large":
            write_stream(source / "large.bin", 128 * 1024 * 1024)
            expected_files = 1
        else:
            expected_files = 1000
            for index in range(expected_files):
                write_stream(source / f"small-{index:04}.bin", 4096)
        database = Database(f"sqlite:///{root / 'mnema.sqlite'}")
        database.create_schema()
        workflow = ArchiveWorkflow(
            source=LocalFilesystemSourceAdapter(source),
            backup=FilesystemVersionedBackup(backup),
            cold=LocalEncryptedColdStorage(cold, secrets.token_bytes(32)),
            active_root=active,
            staging_root=staging,
            policy=SourcePolicy(
                archive_after_days=0,
                stability_window_hours=0,
                quarantine_days=7,
            ),
        )
        with database.session() as session:
            items = await workflow.discover(session)
            for item in items:
                await workflow.archive(session, item)
        with database.engine.begin() as connection:
            connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        database.engine.dispose()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return {
            "mode": mode,
            "files": expected_files,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "peak_rss_kib": usage.ru_maxrss,
            "database_bytes": (root / "mnema.sqlite").stat().st_size,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("large", "small"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(measure(args.mode)), indent=2))


if __name__ == "__main__":
    main()
