from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from mnema.adapters.nas.fileops import sha256_file
from mnema.adapters.sources.icloud_control import (
    ICloudQuota,
    ICloudRemoteAsset,
    PyiCloudControlClient,
)
from mnema.config import Settings
from mnema.domain.icloud_cleanup import ICloudCleanupBlocked, ICloudCleanupService
from mnema.domain.states import ArchiveState
from mnema.jobs import Database
from mnema.jobs.models import ArchiveItem, ICloudCleanupStatus, RuntimeSetting


class FakeICloudControl:
    def __init__(self, *, used: int = 950, favorite: bool = False) -> None:
        self.quota_value = ICloudQuota(used, 1000)
        self.asset = ICloudRemoteAsset(
            "asset-one",
            "record-one",
            "change-one",
            datetime(2020, 1, 1, tzinfo=UTC),
            100,
            favorite,
            1,
        )
        self.deleted: list[str] = []
        self.confirm = True

    def quota(self) -> ICloudQuota:
        return self.quota_value

    def assets(self) -> tuple[ICloudRemoteAsset, ...]:
        return (self.asset,)

    def delete_to_recently_deleted(
        self, apple_asset_id: str, asset_record_name: str, change_tag: str
    ) -> bool:
        assert asset_record_name == "record-one"
        assert change_tag == "change-one"
        self.deleted.append(apple_asset_id)
        return self.confirm


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "storageUsageInfo": {
                "storageQuota": 1000,
                "storageUsageByMedia": [{"usage": 400}, {"usage": 500}],
            }
        }


class FakeSession:
    def post(self, _url: str, **_kwargs: object) -> FakeResponse:
        return FakeResponse()


class FakeService:
    SETUP_ENDPOINT = "https://setup.invalid"
    params: ClassVar[dict[str, object]] = {}
    session = FakeSession()


def test_private_quota_shape_sums_media(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = PyiCloudControlClient("archive@example.com", tmp_path)
    monkeypatch.setattr(client, "_service", lambda: FakeService())
    assert client.quota() == ICloudQuota(900, 1000)


def build(tmp_path: Path, client: FakeICloudControl) -> tuple[Database, ICloudCleanupService]:
    for name in ("active", "backup", "staging"):
        (tmp_path / name).mkdir()
    token = base64.b64encode(client.asset.apple_asset_id.encode()).decode("ascii")[:7]
    relative = Path("2020/01/01") / f"photo_{token}.jpg"
    local = tmp_path / "active" / "iCloud Photos" / relative
    local.parent.mkdir(parents=True)
    local.write_bytes(b"0123456789")
    digest = sha256_file(local)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'mnema.db'}",
        active_root=tmp_path / "active",
        backup_root=tmp_path / "backup",
        staging_root=tmp_path / "staging",
        icloud_enabled=True,
        icloud_apple_id="archive@example.com",
        icloud_import_root=tmp_path / "active/iCloud Photos",
        icloud_session_directory=tmp_path / "session",
        icloud_capacity_relief_enabled=True,
        icloud_deletion_milestone_approved=True,
    )
    database = Database(settings.database_url)
    database.create_schema()
    with database.session() as session:
        session.add(
            ArchiveItem(
                source_provider="icloud_photos",
                source_identifier=str(relative),
                original_path=f"iCloud Photos/{relative}",
                original_size=10,
                original_modified_at=datetime.now(UTC),
                source_version="version",
                state=ArchiveState.QUARANTINED,
                plaintext_sha256=digest,
                nas_path=str(local),
                kopia_snapshot_id="snapshot-one",
                kopia_verified_at=datetime.now(UTC),
                remote_verified_at=datetime.now(UTC),
                remote_provider="scaleway-glacier",
                remote_bucket="archive",
                remote_object_identifier="mnema/item-1.mnema",
                encryption_mode="AES-256-GCM",
                remote_size=42,
                cold_archived_at=datetime.now(UTC) - timedelta(days=8),
                quarantine_expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        session.add(RuntimeSetting(key="global_deletion_enabled", value="true"))
        session.add(RuntimeSetting(key="safety_lock", value="false"))
    return database, ICloudCleanupService(settings, client)


def test_pressure_creates_exact_manifest_and_approved_delete(tmp_path: Path) -> None:
    client = FakeICloudControl()
    database, cleanup = build(tmp_path, client)
    with database.session() as session:
        manifest = cleanup.create_manifest(session)
        assert manifest is not None
        manifest_id = manifest.id
        digest = manifest.digest
        assert len(manifest.entries) == 1
    with database.session() as session:
        cleanup.execute(session, manifest_id, digest, gates_ready=True)
        manifest = session.get(type(manifest), manifest_id)
        assert manifest is not None
        assert manifest.status == ICloudCleanupStatus.COMPLETED
    assert client.deleted == ["asset-one"]
    database.close()


@pytest.mark.parametrize("used,favorite", [(899, False), (950, True)])
def test_below_trigger_or_favorite_produces_no_manifest(
    tmp_path: Path, used: int, favorite: bool
) -> None:
    client = FakeICloudControl(used=used, favorite=favorite)
    database, cleanup = build(tmp_path, client)
    with database.session() as session:
        assert cleanup.create_manifest(session) is None
    database.close()


def test_ambiguous_delete_closes_global_gate(tmp_path: Path) -> None:
    client = FakeICloudControl()
    client.confirm = False
    database, cleanup = build(tmp_path, client)
    with database.session() as session:
        manifest = cleanup.create_manifest(session)
        assert manifest is not None
        manifest_id, digest = manifest.id, manifest.digest
    with database.session() as session:
        with pytest.raises(ICloudCleanupBlocked, match="manual review"):
            cleanup.execute(session, manifest_id, digest, gates_ready=True)
    with database.session() as session:
        assert session.get(RuntimeSetting, "global_deletion_enabled").value == "false"
        assert session.get(RuntimeSetting, "safety_lock").value == "true"
    database.close()
