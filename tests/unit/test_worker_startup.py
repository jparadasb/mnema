from pathlib import Path

import pytest

from mnema.config import Settings
from mnema.jobs import Database
from mnema.jobs.models import RuntimeSetting
from mnema.worker.main import Worker


@pytest.mark.asyncio
async def test_missing_backup_keeps_deletion_fail_closed(tmp_path: Path) -> None:
    active = tmp_path / "active"
    staging = active / ".mnema-staging"
    source = tmp_path / "source"
    for path in (active, staging, source):
        path.mkdir()
    database_path = tmp_path / "worker.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database_path}",
        active_root=active,
        backup_root=tmp_path / "missing-backup",
        staging_root=staging,
        source_root=source,
    )
    database = Database(settings.database_url)
    database.create_schema()
    with database.session() as session:
        session.add_all(
            [
                RuntimeSetting(key="global_deletion_enabled", value="true"),
                RuntimeSetting(key="safety_lock", value="false"),
            ]
        )
    database.close()

    with pytest.raises(RuntimeError, match="startup safety checks failed"):
        await Worker(settings).run()

    reopened = Database(settings.database_url)
    with reopened.session() as session:
        deletion = session.get(RuntimeSetting, "global_deletion_enabled")
        lock = session.get(RuntimeSetting, "safety_lock")
        assert deletion and deletion.value == "false"
        assert lock and lock.value == "true"
    reopened.close()
