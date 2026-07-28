from pathlib import Path

from mnema.diagnostics import startup_checks
from mnema.jobs import Database


def test_same_temporary_device_keeps_startup_unhealthy(tmp_path: Path) -> None:
    active = tmp_path / "active"
    backup = tmp_path / "backup"
    staging = tmp_path / "staging"
    for path in (active, backup, staging):
        path.mkdir()
    (staging / "1.partial").write_bytes(b"x")
    database = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    database.create_schema()
    health = startup_checks(database, active, backup, staging)
    assert not health.devices_differ
    assert not health.healthy
    assert health.sqlite_healthy
    assert len(health.partial_files) == 1
