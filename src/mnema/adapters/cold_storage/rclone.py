from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from mnema.adapters.cold_storage.base import ColdReceipt
from mnema.adapters.cold_storage.crypto import decrypt_file, encrypt_file, sha256_hex

_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class RcloneCommandError(RuntimeError):
    def __init__(self, operation: str, exit_code: int) -> None:
        super().__init__(f"rclone command failed: {operation} (exit {exit_code})")
        self.exit_code = exit_code


class RcloneEncryptedColdStorage:
    def __init__(
        self,
        *,
        remote_root: str,
        config_file: Path,
        key: bytes,
        executable: str = "rclone",
    ) -> None:
        if ":" not in remote_root or any(character in remote_root for character in "\r\n\0"):
            raise ValueError("rclone remote root must use remote:path syntax")
        if not config_file.is_absolute():
            raise ValueError("rclone config path must be absolute")
        if len(key) != 32:
            raise ValueError("cold encryption key must contain exactly 32 bytes")
        self.remote_root = remote_root.rstrip("/")
        self.config_file = config_file
        self.key = key
        self.executable = executable

    async def _run(self, *arguments: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "--config",
            str(self.config_file),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate()
        assert process.returncode is not None
        if process.returncode != 0:
            raise RcloneCommandError(arguments[0], process.returncode)
        return stdout

    def _object_path(self, idempotency_key: str) -> str:
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise ValueError("invalid rclone idempotency key")
        return f"{self.remote_root}/mnema/{idempotency_key}.mnema"

    async def _stat(self, object_path: str) -> dict[str, Any] | None:
        try:
            payload = await self._run("lsjson", "--stat", object_path)
        except RcloneCommandError as error:
            if error.exit_code == 3:
                return None
            raise
        result = json.loads(payload)
        return result if isinstance(result, dict) else None

    def _validate_receipt(self, receipt: ColdReceipt) -> None:
        prefix = f"{self.remote_root}/mnema/"
        if (
            receipt.provider != "rclone"
            or receipt.bucket != self.remote_root
            or not receipt.object_identifier.startswith(prefix)
            or not receipt.object_identifier.endswith(".mnema")
        ):
            raise ValueError("rclone receipt does not belong to configured remote")
        key = receipt.object_identifier.removeprefix(prefix).removesuffix(".mnema")
        if not _IDEMPOTENCY_KEY.fullmatch(key):
            raise ValueError("rclone receipt contains an invalid object identifier")

    async def upload(
        self,
        source: Path,
        object_identifier: str,
        idempotency_key: str,
    ) -> ColdReceipt:
        del object_identifier
        object_path = self._object_path(idempotency_key)
        stat = await self._stat(object_path)
        if stat is None:
            with tempfile.TemporaryDirectory(prefix="mnema-rclone-upload-") as directory:
                encrypted = Path(directory) / "encrypted"
                encrypt_file(source, encrypted, self.key)
                await self._run("copyto", "--immutable", str(encrypted), object_path)
            stat = await self._stat(object_path)
        if stat is None:
            raise RuntimeError("rclone upload could not be independently observed")
        hashes = stat.get("Hashes")
        checksum = next(iter(hashes.values()), None) if isinstance(hashes, dict) else None
        return ColdReceipt(
            provider="rclone",
            bucket=self.remote_root,
            object_identifier=object_path,
            encryption_mode="AES-256-GCM",
            remote_size=int(stat["Size"]),
            remote_checksum=str(checksum) if checksum else None,
        )

    async def verify(self, receipt: ColdReceipt, expected_sha256: str) -> bool:
        with tempfile.TemporaryDirectory(prefix="mnema-rclone-verify-") as directory:
            restored = Path(directory) / "plain"
            await self.restore(receipt, restored)
            return sha256_hex(restored) == expected_sha256

    async def archive_verified(self, receipt: ColdReceipt) -> None:
        del receipt

    async def restore(self, receipt: ColdReceipt, destination: Path) -> None:
        self._validate_receipt(receipt)
        with tempfile.TemporaryDirectory(prefix="mnema-rclone-restore-") as directory:
            encrypted = Path(directory) / "encrypted"
            await self._run("copyto", "--immutable", receipt.object_identifier, str(encrypted))
            decrypt_file(encrypted, destination, self.key)

    async def available(self) -> bool:
        try:
            await self._run("lsd", self.remote_root)
        except RcloneCommandError:
            return False
        return True
