from pathlib import Path

import pytest

from mnema.adapters.backup.filesystem import FilesystemVersionedBackup
from mnema.adapters.cold_storage.local import LocalEncryptedColdStorage
from mnema.adapters.sources.icloud import ICloudPhotosSourceAdapter
from mnema.cli import _icloudpd_arguments
from mnema.config import Settings, SourcePolicy
from mnema.domain.states import ArchiveState
from mnema.domain.workflow import ArchiveWorkflow
from mnema.jobs import Database
from mnema.policies.deletion import GateDecision


@pytest.mark.asyncio
async def test_icloud_active_asset_is_archived_without_source_deletion(tmp_path: Path) -> None:
    active = tmp_path / "active"
    import_root = active / "iCloud Photos"
    asset = import_root / "2020/01/02/IMG_0001_QWxwaGE.HEIC"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"original-photo" * 1024)
    (asset.parent / ".unfinished.partial").write_bytes(b"incomplete")
    session = tmp_path / "session"
    session.mkdir()
    (session / "cookie").write_text("session", encoding="utf-8")
    staging = active / ".mnema-staging"
    staging.mkdir()
    backup = tmp_path / "backup"
    database = Database(f"sqlite:///{tmp_path / 'mnema.db'}")
    database.create_schema()
    source = ICloudPhotosSourceAdapter(import_root, session)
    workflow = ArchiveWorkflow(
        source=source,
        backup=FilesystemVersionedBackup(backup / "versions"),
        cold=LocalEncryptedColdStorage(backup / "cold", b"k" * 32),
        active_root=active,
        staging_root=staging,
        policy=SourcePolicy(
            archive_after_days=0,
            stability_window_hours=0,
            quarantine_days=1,
            deletion_enabled=False,
        ),
        source_provider="icloud_photos",
        source_is_active=True,
    )

    with database.session() as database_session:
        item = (await workflow.discover(database_session))[0]
        assert len(await workflow.discover(database_session)) == 1
        await workflow.archive(database_session, item)

        assert item.state == ArchiveState.QUARANTINED
        assert item.source_provider == "icloud_photos"
        assert Path(item.nas_path or "") == asset
        assert asset.is_file()
        assert not list(staging.iterdir())
        with pytest.raises(PermissionError, match="deletion is unavailable"):
            await workflow.delete_test_item(database_session, item, GateDecision(True, ()))

    assert not (await source.capabilities()).can_delete
    database.close()


def test_icloudpd_arguments_cannot_enable_destructive_modes(tmp_path: Path) -> None:
    settings = Settings(
        active_root=tmp_path / "active",
        backup_root=tmp_path / "backup",
        staging_root=tmp_path / "staging",
        source_root=tmp_path / "source",
        icloud_enabled=True,
        icloud_apple_id="archive@example.com",
        icloud_session_directory=tmp_path / "session",
        icloud_import_root=tmp_path / "active/iCloud Photos",
    )

    arguments = _icloudpd_arguments(settings)

    assert arguments[0] == "/usr/local/bin/icloudpd"
    assert "--password" not in arguments
    assert "--auto-delete" not in arguments
    assert "--delete-after-download" not in arguments
    assert "--keep-icloud-recent-days" not in arguments
    assert arguments[arguments.index("--file-match-policy") + 1] == "name-id7"
