from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient

from mnema.config import Settings
from mnema.domain.states import ArchiveState
from mnema.file_provider import create_file_provider_app
from mnema.file_provider.auth import create_pairing_code
from mnema.file_provider.service import project_verified_archives
from mnema.jobs.models import (
    ArchiveItem,
    FileProviderItem,
    FileProviderItemKind,
    FileProviderItemStatus,
    FileProviderUpload,
    Job,
)


class DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


@pytest.fixture(autouse=True)
def stable_disk_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mnema.file_provider.service.shutil.disk_usage",
        lambda _path: DiskUsage(total=1_000_000_000, used=0, free=1_000_000_000),
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
        file_provider_minimum_free_percent=1,
        require_smart_health=False,
    )
    return settings, create_file_provider_app(settings)


def paired_client(
    client: TestClient, app: object, device_name: str = "Test iPhone"
) -> dict[str, str]:
    database = app.state.database  # type: ignore[attr-defined]
    with database.session() as session:
        code = create_pairing_code(session)
    response = client.post("/v1/auth/pair", json={"code": code, "device_name": device_name})
    assert response.status_code == 200
    return response.json()


def test_invalid_access_token_returns_unauthorized(tmp_path: Path) -> None:
    _, app = configured_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/v1/account", headers={"Authorization": "Bearer invalid-token"})

    assert response.status_code == 401


def test_pair_refresh_upload_and_change_journal(tmp_path: Path) -> None:
    settings, app = configured_app(tmp_path)
    payload = b"file-provider-streamed-content"
    digest = hashlib.sha256(payload).hexdigest()
    with TestClient(app) as client:
        tokens = paired_client(client, app)
        headers = {"Authorization": f"Bearer {tokens['accessToken']}"}
        account = client.get("/v1/account", headers=headers)
        assert account.status_code == 200
        assert account.json()["deviceId"] == tokens["deviceId"]
        assert account.json()["deviceName"] == "Test iPhone"

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


def test_devices_share_one_file_namespace(tmp_path: Path) -> None:
    settings, app = configured_app(tmp_path)
    payload = b"shared content"
    with TestClient(app) as client:
        first = paired_client(client, app, "First iPhone")
        second = paired_client(client, app, "Second iPhone")
        first_headers = {"Authorization": f"Bearer {first['accessToken']}"}
        second_headers = {"Authorization": f"Bearer {second['accessToken']}"}

        started = client.post(
            "/v1/uploads",
            headers=first_headers,
            json={"name": "shared-between-phones.txt", "size": len(payload)},
        )
        assert started.status_code == 201
        upload_id = started.json()["uploadId"]
        client.patch(
            f"/v1/uploads/{upload_id}",
            headers=first_headers | {"Upload-Offset": "0"},
            content=payload,
        ).raise_for_status()
        client.post(f"/v1/uploads/{upload_id}/complete", headers=first_headers).raise_for_status()

        visible = client.get("/v1/items/inbox/children", headers=second_headers)
        assert visible.status_code == 200
        assert started.json()["itemId"] in {item["id"] for item in visible.json()["items"]}

        with app.state.database.session() as session:
            upload = session.get(FileProviderUpload, upload_id)
            assert upload is not None
            archive = session.get(ArchiveItem, upload.archive_item_id)
            item = session.get(FileProviderItem, upload.item_id)
            assert archive is not None and item is not None
            archive.nas_path = str(settings.staging_root / f"{archive.id}.partial")
            archive.state = ArchiveState.QUARANTINED
            archive.cold_archived_at = datetime.now(UTC)
            item.status = FileProviderItemStatus.READY

        downloaded = client.get(
            f"/v1/items/{started.json()['itemId']}/content", headers=second_headers
        )
        assert downloaded.status_code == 200
        assert downloaded.content == payload


def test_verified_icloud_projection_preserves_and_reconciles_folders(tmp_path: Path) -> None:
    settings, app = configured_app(tmp_path)
    stored = settings.active_root / "icloud-photo.jpg"
    stored.write_bytes(b"verified-photo")
    with TestClient(app):
        database = app.state.database
        with database.session() as session:
            archive = ArchiveItem(
                source_provider="icloud_photos",
                source_identifier="icloud-photo-one",
                original_path="2026/08/Vacation/photo.jpg",
                original_size=stored.stat().st_size,
                original_modified_at=datetime.now(UTC),
                source_version="1",
                state=ArchiveState.ARCHIVED,
                nas_path=str(stored),
                plaintext_sha256=hashlib.sha256(stored.read_bytes()).hexdigest(),
                cold_archived_at=datetime.now(UTC),
            )
            session.add(archive)
            session.flush()
            assert project_verified_archives(session) == 1

            item = session.query(FileProviderItem).filter_by(archive_item_id=archive.id).one()
            assert item.name == "photo.jpg"
            names = []
            parent_id = item.parent_id
            while parent_id != "archive-icloud":
                folder = session.get(FileProviderItem, parent_id)
                assert folder is not None
                names.append(folder.name)
                parent_id = folder.parent_id
            assert list(reversed(names)) == ["2026", "08", "Vacation"]
            assert project_verified_archives(session) == 0


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
        item_response = client.get("/v1/items/ready-item", headers=headers)
        assert item_response.status_code == 200
        assert item_response.json()["modifiedAt"].endswith("+00:00")
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


def test_e2e_upload_cleanup_is_gated_and_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNEMA_ALLOW_TEST_DELETE", "1")
    settings, app = configured_app(tmp_path)
    payload = b"isolated-e2e-cleanup"
    with TestClient(app) as client:
        tokens = paired_client(client, app)
        headers = {"Authorization": f"Bearer {tokens['accessToken']}"}
        started = client.post(
            "/v1/uploads",
            headers=headers,
            json={"name": "mnema-e2e-cleanup.txt", "size": len(payload)},
        )
        assert started.status_code == 201, started.text
        upload_id = started.json()["uploadId"]
        item_id = started.json()["itemId"]
        client.patch(
            f"/v1/uploads/{upload_id}",
            headers=headers | {"Upload-Offset": "0"},
            content=payload,
        ).raise_for_status()
        client.post(f"/v1/uploads/{upload_id}/complete", headers=headers).raise_for_status()
        database = app.state.database
        with database.session() as session:
            upload = session.get(FileProviderUpload, upload_id)
            assert upload is not None
            archive_id = upload.archive_item_id
            staged = settings.staging_root / f"{archive_id}.partial"
            assert staged.read_bytes() == payload

        deleted = client.delete(f"/v1/items/{item_id}", headers=headers)
        assert deleted.status_code == 204
        assert not staged.exists()
        with database.session() as session:
            archive = session.get(ArchiveItem, archive_id)
            assert archive is not None
            assert archive.state == ArchiveState.TEST_CLEANED
            assert archive.audit_events[-1].actor == "e2e-cleanup"
            assert session.get(FileProviderUpload, upload_id) is None
            assert session.get(FileProviderItem, item_id) is None


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
