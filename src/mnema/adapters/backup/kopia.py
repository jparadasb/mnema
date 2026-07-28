from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from mnema.adapters.backup.base import BackupReceipt
from mnema.adapters.nas.fileops import sha256_file


class KopiaBackup:
    def __init__(
        self,
        repository: Path,
        password_file: Path,
        config_file: Path,
    ) -> None:
        self.repository = repository
        self.password_file = password_file
        self.config_file = config_file
        self._connection_lock = asyncio.Lock()

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["KOPIA_PASSWORD"] = self.password_file.read_text(encoding="utf-8").strip()
        environment["HOME"] = str(self.config_file.parent)
        environment["KOPIA_CHECK_FOR_UPDATES"] = "false"
        return environment

    async def _run(self, *arguments: str) -> bytes:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        log_directory = Path(tempfile.gettempdir()) / "mnema-kopia-logs"
        log_directory.mkdir(exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            "kopia",
            f"--config-file={self.config_file}",
            f"--log-dir={log_directory}",
            *arguments,
            env=self._environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"Kopia command failed: {stderr.decode(errors='replace')[:500]}")
        return stdout

    async def _ensure_connected(self) -> None:
        if self.config_file.is_file():
            return
        async with self._connection_lock:
            if self.config_file.is_file():
                return
            self.repository.mkdir(parents=True, exist_ok=True)
            has_repository_data = any(self.repository.iterdir())
            action = "connect" if has_repository_data else "create"
            await self._run(
                "repository",
                action,
                "filesystem",
                f"--path={self.repository}",
            )

    async def snapshot(self, source: Path, idempotency_key: str) -> BackupReceipt:
        await self._ensure_connected()
        tag = f"mnema-id:{idempotency_key}"
        existing = await self._run(
            "snapshot",
            "list",
            str(source),
            "--json",
            "--tags",
            tag,
        )
        snapshots = json.loads(existing or b"[]")
        if snapshots:
            return BackupReceipt(str(snapshots[0]["id"]))
        created = await self._run(
            "snapshot",
            "create",
            str(source),
            "--json",
            "--tags",
            tag,
        )
        payload = json.loads(created)
        return BackupReceipt(str(payload["id"]))

    async def verify(self, receipt: BackupReceipt, expected_sha256: str) -> bool:
        with tempfile.TemporaryDirectory(prefix="mnema-kopia-verify-") as directory:
            destination = Path(directory) / "restored"
            await self.restore(receipt, destination)
            return sha256_file(destination) == expected_sha256

    async def restore(self, receipt: BackupReceipt, destination: Path) -> None:
        await self._ensure_connected()
        await self._run("snapshot", "restore", receipt.snapshot_id, str(destination))
