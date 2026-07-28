from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]

from mnema.adapters.cold_storage.base import ColdReceipt
from mnema.adapters.cold_storage.crypto import decrypt_file, encrypt_file, sha256_hex


class S3EncryptedColdStorage:
    def __init__(
        self,
        *,
        bucket: str,
        key: bytes,
        endpoint_url: str | None = None,
        region_name: str = "us-east-1",
        access_key_file: Path | None = None,
        secret_key_file: Path | None = None,
        create_bucket_if_missing: bool = False,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.key = key
        self.create_bucket_if_missing = create_bucket_if_missing
        credentials: dict[str, str] = {}
        if access_key_file is not None and secret_key_file is not None:
            credentials = {
                "aws_access_key_id": access_key_file.read_text(encoding="utf-8").strip(),
                "aws_secret_access_key": secret_key_file.read_text(encoding="utf-8").strip(),
            }
        self.client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            **credentials,
        )

    async def _ensure_bucket(self) -> None:
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except self.client.exceptions.ClientError:
            if not self.create_bucket_if_missing:
                raise
            await asyncio.to_thread(self.client.create_bucket, Bucket=self.bucket)

    async def upload(
        self,
        source: Path,
        object_identifier: str,
        idempotency_key: str,
    ) -> ColdReceipt:
        await self._ensure_bucket()
        key = f"mnema/{idempotency_key}.mnema"
        with tempfile.TemporaryDirectory(prefix="mnema-s3-upload-") as directory:
            encrypted = Path(directory) / "encrypted"
            encrypt_file(source, encrypted, self.key)
            try:
                head = await asyncio.to_thread(
                    self.client.head_object,
                    Bucket=self.bucket,
                    Key=key,
                )
            except self.client.exceptions.ClientError:
                await asyncio.to_thread(
                    self.client.upload_file,
                    str(encrypted),
                    self.bucket,
                    key,
                    ExtraArgs={
                        "Metadata": {
                            "mnema-idempotency-key": idempotency_key,
                            "mnema-encryption": "AES-256-GCM",
                        }
                    },
                )
                head = await asyncio.to_thread(
                    self.client.head_object,
                    Bucket=self.bucket,
                    Key=key,
                )
            return ColdReceipt(
                provider="s3",
                bucket=self.bucket,
                object_identifier=key,
                encryption_mode="AES-256-GCM",
                remote_size=int(head["ContentLength"]),
                remote_checksum=str(head.get("ETag", "")).strip('"') or None,
            )

    async def verify(self, receipt: ColdReceipt, expected_sha256: str) -> bool:
        with tempfile.TemporaryDirectory(prefix="mnema-s3-verify-") as directory:
            restored = Path(directory) / "plain"
            await self.restore(receipt, restored)
            return sha256_hex(restored) == expected_sha256

    async def restore(self, receipt: ColdReceipt, destination: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="mnema-s3-restore-") as directory:
            encrypted = Path(directory) / "encrypted"
            await asyncio.to_thread(
                self.client.download_file,
                receipt.bucket,
                receipt.object_identifier,
                str(encrypted),
            )
            decrypt_file(encrypted, destination, self.key)

    async def available(self) -> bool:
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except Exception:
            return False
        return True
