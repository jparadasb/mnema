from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from mnema.config import Settings
from mnema.domain.states import ArchiveState
from mnema.file_provider import create_file_provider_app
from mnema.file_provider.auth import create_pairing_code
from mnema.jobs.models import (
    ArchiveItem,
    FileProviderItem,
    FileProviderItemKind,
    FileProviderItemStatus,
    FileProviderUpload,
    Job,
)


def configured_app(tmp_path: Path) -> tuple[Settings, object]:
    active = tmp_path / "active"
    backup = tmp_path / "backup"
    source = tmp_path / "source"
    for path in (active, backup, source):
        path.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("test-signing-key-that-is-long-enough-for-file-provider\n", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'mnema.db'}",
        active_root=active,
        backup_root=backup,
        staging_root=active / ".mnema-staging",
        source_root=source,
        secret_key_file=secret,
        file_provider_enabled=True,
        file_provider_public_url="https://files.example.com",
        file_provider_upload_root=active / ".mnema-file-provider",
        require_smart_health=False,
    )
    return settings, create_file_provider_app(settings)


def paired_client(client: TestClient, app: object) -> dict[str, str]:
    database = app.state.database  # type: ignore[attr-defined]
    with database.session() as session:
        code = create_pairing_code(session)
    response = client.post("/v1/auth/pair", json={"code": code, "device_name": "Test iPhone"})
    assert response.status_code == 200
    return response.json()


def test_pair_refresh_upload_and_change_journal(tmp_path: Path) -> None:
    settings, app = configured_app(tmp_path)
    payload = b"file-provider-streamed-content"
    digest = hashlib.sha256(payload).hexdigest()
    with TestClient(app) as client:
        tokens = paired_client(client, app)
        headers = {"Authorization": f"Bearer {tokens['accessToken']}"}
        assert client.get("/v1/account", headers=headers).status_code == 200

        refresh = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refreshToken"]})
        assert refresh.status_code == 200
        headers = {"Authorization": f"Bearer {refresh.json()['accessToken']}"}

        started = client.post(
            "/v1/uploads",
            headers=headers,
            json={
                "name": "from-iphone.txt",
                "size": len(payload),
                "content_type": "text/plain",
                "sha256": digest,
            },
        )
        assert started.status_code == 201
        upload_id = started.json()["uploadId"]
        appended = client.patch(
            f"/v1/uploads/{upload_id}",
            headers=headers | {"Upload-Offset": "0"},
            content=payload,
        )
        assert appended.json() == {"offset": len(payload)}
        completed = client.post(f"/v1/uploads/{upload_id}/complete", headers=headers)
        assert completed.status_code == 200

        changes = client.get("/v1/changes", headers=headers).json()
        assert changes["resetRequired"] is False
        assert any(change["itemId"] == started.json()["itemId"] for change in changes["changes"])

        database = app.state.database
        with database.session() as session:
            upload = session.get(FileProviderUpload, upload_id)
            assert upload is not None
            archive = session.get(ArchiveItem, upload.archive_item_id)
            assert archive is not None
            assert archive.state == ArchiveState.LOCAL_STAGED
            assert archive.plaintext_sha256 == digest
            assert session.query(Job).filter_by(adapter="file_provider").count() == 1
            assert (settings.staging_root / f"{archive.id}.partial").read_bytes() == payload


def test_ready_content_supports_ranges(tmp_path: Path) -> None:
    settings, app = configured_app(tmp_path)
    content = b"0123456789"
    stored = settings.active_root / "ready.bin"
    stored.write_bytes(content)
    with TestClient(app) as client:
        tokens = paired_client(client, app)
        headers = {"Authorization": f"Bearer {tokens['accessToken']}"}
        database = app.state.database
        with database.session() as session:
            archive = ArchiveItem(
                source_provider="local_test",
                source_identifier="ready",
                original_path="ready.bin",
                original_size=len(content),
                original_modified_at=datetime.now(UTC),
                source_version="1",
                state=ArchiveState.QUARANTINED,
                nas_path=str(stored),
                plaintext_sha256=hashlib.sha256(content).hexdigest(),
            )
            session.add(archive)
            session.flush()
            item = FileProviderItem(
                id="ready-item",
                parent_id="archive-local",
                name="ready.bin",
                kind=FileProviderItemKind.FILE,
                status=FileProviderItemStatus.READY,
                archive_item_id=archive.id,
                size=len(content),
                content_version=archive.plaintext_sha256,
            )
            session.add(item)
        response = client.get(
            "/v1/items/ready-item/content", headers=headers | {"Range": "bytes=2-5"}
        )
        assert response.status_code == 206
        assert response.content == b"2345"


def test_zero_byte_upload_seals_without_a_patch_request(tmp_path: Path) -> None:
    settings, app = configured_app(tmp_path)
    with TestClient(app) as client:
        tokens = paired_client(client, app)
        headers = {"Authorization": f"Bearer {tokens['accessToken']}"}
        started = client.post(
            "/v1/uploads",
            headers=headers,
            json={"name": "empty.txt", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()},
        )
        assert started.status_code == 201
        completed = client.post(
            f"/v1/uploads/{started.json()['uploadId']}/complete", headers=headers
        )
        assert completed.status_code == 200
        database = app.state.database
        with database.session() as session:
            upload = session.get(FileProviderUpload, started.json()["uploadId"])
            assert upload is not None
            archive = session.get(ArchiveItem, upload.archive_item_id)
            assert archive is not None
            assert (settings.staging_root / f"{archive.id}.partial").read_bytes() == b""


def test_oversized_chunk_rolls_back_for_resumption(tmp_path: Path) -> None:
    settings, app = configured_app(tmp_path)
    with TestClient(app) as client:
        tokens = paired_client(client, app)
        headers = {"Authorization": f"Bearer {tokens['accessToken']}"}
        started = client.post(
            "/v1/uploads",
            headers=headers,
            json={"name": "bounded.txt", "size": 3},
        )
        upload_id = started.json()["uploadId"]
        rejected = client.patch(
            f"/v1/uploads/{upload_id}",
            headers=headers | {"Upload-Offset": "0"},
            content=b"four",
        )
        assert rejected.status_code == 413
        resumed = client.patch(
            f"/v1/uploads/{upload_id}",
            headers=headers | {"Upload-Offset": "0"},
            content=b"abc",
        )
        assert resumed.json() == {"offset": 3}
        assert (settings.file_provider_upload_root / f"{upload_id}.upload").read_bytes() == b"abc"
