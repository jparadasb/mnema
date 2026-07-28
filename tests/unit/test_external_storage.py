from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from mnema.adapters.backup.kopia import KopiaBackup
from mnema.adapters.cold_storage.crypto import sha256_hex
from mnema.adapters.cold_storage.s3 import S3EncryptedColdStorage


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
        return {"ContentLength": len(payload), "ETag": '"test-etag"'}

    def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        ExtraArgs: dict[str, Any],
    ) -> None:
        self.objects[(bucket, key)] = Path(filename).read_bytes()
        self.metadata[(bucket, key)] = ExtraArgs["Metadata"]

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
