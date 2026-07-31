from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from mnema.adapters.backup.kopia import KopiaBackup
from mnema.adapters.cold_storage import s3 as s3_module
from mnema.adapters.cold_storage.base import ColdReceipt, ColdRestorePending
from mnema.adapters.cold_storage.crypto import sha256_hex
from mnema.adapters.cold_storage.rclone import (
    RcloneCommandError,
    RcloneEncryptedColdStorage,
)
from mnema.adapters.cold_storage.s3 import S3EncryptedColdStorage
from mnema.config import Settings
from mnema.domain.factory import build_local_workflow


class RecordingKopia(KopiaBackup):
    def __init__(self, tmp_path: Path, responses: list[bytes]) -> None:
        password = tmp_path / "password"
        password.write_text("not-a-real-secret", encoding="utf-8")
        super().__init__(tmp_path / "repository", password, tmp_path / "config")
        self.responses = responses
        self.commands: list[tuple[str, ...]] = []

    async def _ensure_connected(self) -> None:
        return

    async def _run(self, *arguments: str) -> bytes:
        self.commands.append(arguments)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_kopia_snapshot_uses_valid_idempotency_tag(tmp_path: Path) -> None:
    kopia = RecordingKopia(
        tmp_path,
        [b"[]", b'{"id":"snapshot-1"}'],
    )

    receipt = await kopia.snapshot(tmp_path / "source", "item-9")

    assert receipt.snapshot_id == "snapshot-1"
    assert kopia.commands[0][-1] == "mnema-id:item-9"
    assert kopia.commands[1][-1] == "mnema-id:item-9"


@pytest.mark.asyncio
async def test_kopia_snapshot_reuses_existing_receipt(tmp_path: Path) -> None:
    kopia = RecordingKopia(tmp_path, [b'[{"id":"snapshot-existing"}]'])

    receipt = await kopia.snapshot(tmp_path / "source", "item-9")

    assert receipt.snapshot_id == "snapshot-existing"
    assert len(kopia.commands) == 1


class FakeS3Client:
    class exceptions:
        ClientError = ClientError

    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.metadata: dict[tuple[str, str], dict[str, str]] = {}
        self.storage_classes: dict[tuple[str, str], str] = {}
        self.restore_headers: dict[tuple[str, str], str] = {}
        self.restore_requests = 0
        self.copy_requests = 0
        self.part_ranges: list[str] = []

    @staticmethod
    def _missing(operation: str) -> ClientError:
        return ClientError({"Error": {"Code": "404", "Message": "missing"}}, operation)

    def head_bucket(self, *, Bucket: str) -> None:
        if Bucket not in self.buckets:
            raise self._missing("HeadBucket")

    def create_bucket(self, *, Bucket: str) -> None:
        self.buckets.add(Bucket)

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        try:
            payload = self.objects[(Bucket, Key)]
        except KeyError:
            raise self._missing("HeadObject") from None
        result: dict[str, Any] = {
            "ContentLength": len(payload),
            "ETag": '"test-etag"',
            "Metadata": self.metadata.get((Bucket, Key), {}),
        }
        if storage_class := self.storage_classes.get((Bucket, Key)):
            result["StorageClass"] = storage_class
        if restore := self.restore_headers.get((Bucket, Key)):
            result["Restore"] = restore
        return result

    def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        ExtraArgs: dict[str, Any],
    ) -> None:
        self.objects[(bucket, key)] = Path(filename).read_bytes()
        self.metadata[(bucket, key)] = ExtraArgs["Metadata"]

    def copy_object(
        self,
        *,
        Bucket: str,
        Key: str,
        CopySource: dict[str, str],
        MetadataDirective: str,
        StorageClass: str,
    ) -> None:
        assert CopySource["Bucket"] == Bucket
        assert CopySource["Key"] == Key
        assert MetadataDirective == "COPY"
        self.storage_classes[(Bucket, Key)] = StorageClass
        self.copy_requests += 1

    def create_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        Metadata: dict[str, str],
        StorageClass: str,
    ) -> dict[str, str]:
        self.metadata[(Bucket, Key)] = Metadata
        self.storage_classes[(Bucket, Key)] = StorageClass
        return {"UploadId": "archive-copy"}

    def upload_part_copy(
        self,
        *,
        Bucket: str,
        Key: str,
        CopySource: dict[str, str],
        CopySourceRange: str,
        UploadId: str,
        PartNumber: int,
    ) -> dict[str, dict[str, str]]:
        del Bucket, Key, CopySource, UploadId
        self.part_ranges.append(CopySourceRange)
        return {"CopyPartResult": {"ETag": f"part-{PartNumber}"}}

    def complete_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, list[dict[str, int | str]]],
    ) -> None:
        del Bucket, Key, UploadId
        assert MultipartUpload["Parts"]

    def abort_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
    ) -> None:
        raise AssertionError(f"unexpected abort: {Bucket}/{Key} {UploadId}")

    def restore_object(
        self,
        *,
        Bucket: str,
        Key: str,
        RestoreRequest: dict[str, int],
    ) -> None:
        assert RestoreRequest == {"Days": 1}
        self.restore_requests += 1
        self.restore_headers[(Bucket, Key)] = 'ongoing-request="true"'

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        Path(filename).write_bytes(self.objects[(bucket, key)])


@pytest.mark.asyncio
async def test_s3_storage_creates_bucket_and_independently_restores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def inline_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    source = tmp_path / "source"
    restored = tmp_path / "restored"
    source.write_bytes(b"Mnema encrypted S3 proof")
    client = FakeS3Client()
    storage = S3EncryptedColdStorage(
        bucket="integration",
        key=b"k" * 32,
        client=client,
        create_bucket_if_missing=True,
    )

    receipt = await asyncio.wait_for(storage.upload(source, source.name, "item-1"), timeout=3)

    assert receipt.remote_size > source.stat().st_size
    assert client.objects[("integration", "mnema/item-1.mnema")] != source.read_bytes()
    assert await asyncio.wait_for(storage.verify(receipt, sha256_hex(source)), timeout=3)
    await asyncio.wait_for(storage.restore(receipt, restored), timeout=3)
    assert restored.read_bytes() == source.read_bytes()


@pytest.mark.asyncio
async def test_scaleway_archives_only_after_verification_and_restores_asynchronously(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def inline_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    source = tmp_path / "source"
    restored = tmp_path / "restored"
    source.write_bytes(b"verified before Glacier")
    client = FakeS3Client()
    client.buckets.add("archive")
    storage = S3EncryptedColdStorage(
        bucket="archive",
        key=b"k" * 32,
        client=client,
        provider_name="scaleway-glacier",
        archive_storage_class="GLACIER",
    )

    receipt = await storage.upload(source, source.name, "item-2")

    assert receipt.provider == "scaleway-glacier"
    assert ("archive", receipt.object_identifier) not in client.storage_classes
    assert await storage.verify(receipt, sha256_hex(source))

    await storage.archive_verified(receipt)
    await storage.archive_verified(receipt)

    assert client.storage_classes[("archive", receipt.object_identifier)] == "GLACIER"
    assert client.copy_requests == 1
    with pytest.raises(ColdRestorePending) as requested:
        await storage.restore(receipt, restored)
    assert requested.value.requested
    with pytest.raises(ColdRestorePending) as waiting:
        await storage.restore(receipt, restored)
    assert not waiting.value.requested
    assert client.restore_requests == 1

    client.restore_headers[("archive", receipt.object_identifier)] = (
        'ongoing-request="false", expiry-date="tomorrow"'
    )
    await storage.restore(receipt, restored)
    assert restored.read_bytes() == source.read_bytes()


@pytest.mark.asyncio
async def test_scaleway_large_object_uses_idempotent_multipart_archive_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def inline_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(s3_module, "_SINGLE_COPY_LIMIT", 1)
    monkeypatch.setattr(s3_module, "_MIN_COPY_PART_SIZE", 10)
    source = tmp_path / "large"
    source.write_bytes(b"x" * 25)
    client = FakeS3Client()
    client.buckets.add("archive")
    storage = S3EncryptedColdStorage(
        bucket="archive",
        key=b"k" * 32,
        client=client,
        provider_name="scaleway-glacier",
        archive_storage_class="GLACIER",
    )
    receipt = await storage.upload(source, source.name, "item-large")

    await storage.archive_verified(receipt)

    assert len(client.part_ranges) > 1
    assert client.storage_classes[("archive", receipt.object_identifier)] == "GLACIER"


class MemoryRclone(RcloneEncryptedColdStorage):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(
            remote_root="memory:cold",
            config_file=tmp_path / "rclone.conf",
            key=b"r" * 32,
        )
        self.objects: dict[str, bytes] = {}
        self.copy_uploads = 0

    async def _run(self, *arguments: str) -> bytes:
        if arguments[0] == "lsd":
            return b""
        if arguments[0] == "lsjson":
            object_path = arguments[-1]
            if object_path not in self.objects:
                raise RcloneCommandError("lsjson", 3)
            return json.dumps(
                {
                    "Path": object_path,
                    "Size": len(self.objects[object_path]),
                    "Hashes": {"md5": "test-checksum"},
                }
            ).encode()
        if arguments[0] == "copyto":
            source, destination = arguments[-2:]
            if destination.startswith("memory:"):
                self.objects[destination] = Path(source).read_bytes()
                self.copy_uploads += 1
            else:
                Path(destination).write_bytes(self.objects[source])
            return b""
        raise AssertionError(f"unexpected rclone command: {arguments[0]}")


@pytest.mark.asyncio
async def test_rclone_storage_is_idempotent_and_independently_restores(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    restored = tmp_path / "restored"
    source.write_bytes(b"Mnema encrypted rclone proof")
    storage = MemoryRclone(tmp_path)

    receipt = await storage.upload(source, source.name, "item-4")
    repeated = await storage.upload(source, source.name, "item-4")

    assert receipt == repeated
    assert receipt.provider == "rclone"
    assert receipt.remote_checksum == "test-checksum"
    assert storage.copy_uploads == 1
    assert storage.objects[receipt.object_identifier] != source.read_bytes()
    assert await storage.verify(receipt, sha256_hex(source))
    await storage.restore(receipt, restored)
    assert restored.read_bytes() == source.read_bytes()
    assert await storage.available()


def test_rclone_storage_rejects_unsafe_identifiers(tmp_path: Path) -> None:
    storage = MemoryRclone(tmp_path)

    with pytest.raises(ValueError, match="idempotency"):
        storage._object_path("../outside")
    with pytest.raises(ValueError, match="configured remote"):
        storage._validate_receipt(
            ColdReceipt(
                provider="rclone",
                bucket="other:cold",
                object_identifier="other:cold/mnema/item-1.mnema",
                encryption_mode="AES-256-GCM",
                remote_size=1,
                remote_checksum=None,
            )
        )


def test_workflow_factory_selects_rclone_transport(tmp_path: Path) -> None:
    cold_key = tmp_path / "cold-key"
    cold_key.write_bytes(b"r" * 32)
    for directory in ("active", "backup", "source"):
        (tmp_path / directory).mkdir()
    (tmp_path / "active" / "staging").mkdir()
    settings = Settings(
        active_root=tmp_path / "active",
        backup_root=tmp_path / "backup",
        staging_root=tmp_path / "active" / "staging",
        source_root=tmp_path / "source",
        cold_encryption_key_file=cold_key,
        kopia_repository=tmp_path / "kopia",
        kopia_password_file=tmp_path / "kopia-password",
        kopia_config_file=tmp_path / "kopia.config",
        use_external_test_storage=True,
        cold_storage_transport="rclone",
        rclone_config_file=tmp_path / "rclone.conf",
        rclone_remote_root="minio:mnema-integration",
    )

    workflow = build_local_workflow(settings)

    assert isinstance(workflow.cold, RcloneEncryptedColdStorage)


def test_workflow_factory_selects_scaleway_glacier_without_bucket_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cold_key = tmp_path / "cold-key"
    access_key = tmp_path / "access-key"
    secret_key = tmp_path / "secret-key"
    cold_key.write_bytes(b"s" * 32)
    access_key.write_text("test-access", encoding="utf-8")
    secret_key.write_text("test-secret", encoding="utf-8")
    for directory in ("active", "backup", "source"):
        (tmp_path / directory).mkdir()
    (tmp_path / "active" / "staging").mkdir()
    client = FakeS3Client()
    monkeypatch.setattr(s3_module.boto3, "client", lambda *args, **kwargs: client)
    settings = Settings(
        active_root=tmp_path / "active",
        backup_root=tmp_path / "backup",
        staging_root=tmp_path / "active" / "staging",
        source_root=tmp_path / "source",
        cold_encryption_key_file=cold_key,
        kopia_repository=tmp_path / "kopia",
        kopia_password_file=tmp_path / "kopia-password",
        kopia_config_file=tmp_path / "kopia.config",
        use_external_test_storage=True,
        cold_storage_transport="s3",
        s3_provider="scaleway",
        s3_region="fr-par",
        s3_endpoint_url="https://s3.fr-par.scw.cloud",
        s3_bucket="mnema-archive",
        s3_access_key_file=access_key,
        s3_secret_key_file=secret_key,
    )

    workflow = build_local_workflow(settings)

    assert isinstance(workflow.cold, S3EncryptedColdStorage)
    assert workflow.cold.provider_name == "scaleway-glacier"
    assert workflow.cold.archive_storage_class == "GLACIER"
    assert not workflow.cold.create_bucket_if_missing
