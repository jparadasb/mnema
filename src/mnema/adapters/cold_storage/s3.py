from __future__ import annotations

import asyncio
import math
import re
from pathlib import Path
from typing import Any, Literal

import boto3  # type: ignore[import-untyped]

from mnema.adapters.cold_storage.base import ColdReceipt, ColdRestorePending
from mnema.adapters.cold_storage.crypto import decrypt_file, encrypt_file, sha256_hex
from mnema.adapters.nas.fileops import SCRATCH_MARGIN_BYTES, scratch_directory

_SINGLE_COPY_LIMIT = 5 * 1024**3
_MIN_COPY_PART_SIZE = 64 * 1024**2
_MAX_MULTIPART_PARTS = 10_000
_OBJECT_KEY = re.compile(r"mnema/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.mnema")


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
        provider_name: str = "s3",
        archive_storage_class: Literal["GLACIER"] | None = None,
        restore_days: int = 1,
        scratch_root: Path | None = None,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.key = key
        self.scratch_root = scratch_root
        self.create_bucket_if_missing = create_bucket_if_missing
        self.provider_name = provider_name
        self.archive_storage_class = archive_storage_class
        if restore_days < 1:
            raise ValueError("S3 restore duration must be at least one day")
        self.restore_days = restore_days
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
        del object_identifier
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", idempotency_key):
            raise ValueError("invalid S3 idempotency key")
        await self._ensure_bucket()
        key = f"mnema/{idempotency_key}.mnema"
        plaintext_size = source.stat().st_size
        with scratch_directory(
            self.scratch_root,
            "mnema-s3-upload-",
            required_bytes=plaintext_size + SCRATCH_MARGIN_BYTES,
        ) as directory:
            encrypted = directory / "encrypted"
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
                provider=self.provider_name,
                bucket=self.bucket,
                object_identifier=key,
                encryption_mode="AES-256-GCM",
                remote_size=int(head["ContentLength"]),
                remote_checksum=str(head.get("ETag", "")).strip('"') or None,
            )

    async def verify(self, receipt: ColdReceipt, expected_sha256: str) -> bool:
        # Verification holds the downloaded ciphertext and the decrypted
        # plaintext at the same time.
        with scratch_directory(
            self.scratch_root,
            "mnema-s3-verify-",
            required_bytes=2 * receipt.remote_size + SCRATCH_MARGIN_BYTES,
        ) as directory:
            restored = directory / "plain"
            await self.restore(receipt, restored)
            return sha256_hex(restored) == expected_sha256

    async def archive_verified(self, receipt: ColdReceipt) -> None:
        if self.archive_storage_class is None:
            return
        self._validate_receipt(receipt)
        head = await asyncio.to_thread(
            self.client.head_object,
            Bucket=self.bucket,
            Key=receipt.object_identifier,
        )
        if head.get("StorageClass") == self.archive_storage_class:
            return
        size = int(head["ContentLength"])
        copy_source = {
            "Bucket": self.bucket,
            "Key": receipt.object_identifier,
        }
        if version_id := head.get("VersionId"):
            copy_source["VersionId"] = str(version_id)
        if size <= _SINGLE_COPY_LIMIT:
            await asyncio.to_thread(
                self.client.copy_object,
                Bucket=self.bucket,
                Key=receipt.object_identifier,
                CopySource=copy_source,
                MetadataDirective="COPY",
                StorageClass=self.archive_storage_class,
            )
        else:
            await self._multipart_archive_copy(
                receipt.object_identifier,
                copy_source,
                size,
                metadata=head.get("Metadata", {}),
            )
        archived = await asyncio.to_thread(
            self.client.head_object,
            Bucket=self.bucket,
            Key=receipt.object_identifier,
        )
        if archived.get("StorageClass") != self.archive_storage_class:
            raise RuntimeError("S3 object storage-class transition was not independently observed")

    async def _multipart_archive_copy(
        self,
        object_key: str,
        copy_source: dict[str, str],
        size: int,
        *,
        metadata: dict[str, str],
    ) -> None:
        part_size = max(_MIN_COPY_PART_SIZE, math.ceil(size / _MAX_MULTIPART_PARTS))
        created = await asyncio.to_thread(
            self.client.create_multipart_upload,
            Bucket=self.bucket,
            Key=object_key,
            Metadata=metadata,
            StorageClass=self.archive_storage_class,
        )
        upload_id = str(created["UploadId"])
        parts: list[dict[str, int | str]] = []
        try:
            for part_number, start in enumerate(range(0, size, part_size), start=1):
                end = min(start + part_size, size) - 1
                copied = await asyncio.to_thread(
                    self.client.upload_part_copy,
                    Bucket=self.bucket,
                    Key=object_key,
                    CopySource=copy_source,
                    CopySourceRange=f"bytes={start}-{end}",
                    UploadId=upload_id,
                    PartNumber=part_number,
                )
                parts.append(
                    {
                        "ETag": str(copied["CopyPartResult"]["ETag"]),
                        "PartNumber": part_number,
                    }
                )
            await asyncio.to_thread(
                self.client.complete_multipart_upload,
                Bucket=self.bucket,
                Key=object_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except BaseException:
            await asyncio.to_thread(
                self.client.abort_multipart_upload,
                Bucket=self.bucket,
                Key=object_key,
                UploadId=upload_id,
            )
            raise

    async def restore(self, receipt: ColdReceipt, destination: Path) -> None:
        self._validate_receipt(receipt)
        head = await asyncio.to_thread(
            self.client.head_object,
            Bucket=self.bucket,
            Key=receipt.object_identifier,
        )
        if head.get("StorageClass") == "GLACIER":
            restore_status = str(head.get("Restore", ""))
            if 'ongoing-request="false"' not in restore_status:
                requested = False
                if 'ongoing-request="true"' not in restore_status:
                    try:
                        await asyncio.to_thread(
                            self.client.restore_object,
                            Bucket=self.bucket,
                            Key=receipt.object_identifier,
                            RestoreRequest={"Days": self.restore_days},
                        )
                        requested = True
                    except self.client.exceptions.ClientError as error:
                        code = str(error.response.get("Error", {}).get("Code", ""))
                        if code != "RestoreAlreadyInProgress":
                            raise
                raise ColdRestorePending(
                    "Glacier restore requested; retry after Scaleway finishes retrieval",
                    requested=requested,
                )
        with scratch_directory(
            self.scratch_root,
            "mnema-s3-restore-",
            required_bytes=receipt.remote_size + SCRATCH_MARGIN_BYTES,
        ) as directory:
            encrypted = directory / "encrypted"
            await asyncio.to_thread(
                self.client.download_file,
                receipt.bucket,
                receipt.object_identifier,
                str(encrypted),
            )
            decrypt_file(encrypted, destination, self.key)

    def _validate_receipt(self, receipt: ColdReceipt) -> None:
        if (
            receipt.provider != self.provider_name
            or receipt.bucket != self.bucket
            or not _OBJECT_KEY.fullmatch(receipt.object_identifier)
        ):
            raise ValueError("S3 receipt does not belong to configured cold storage")

    async def available(self) -> bool:
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except Exception:
            return False
        return True
