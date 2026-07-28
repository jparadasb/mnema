import json
from pathlib import Path

from mnema.diagnostics.smart import read_smart_health, smart_report_healthy


def report(path: Path, *, active_passed: bool = True, backup_passed: bool = True) -> None:
    path.write_text(
        json.dumps(
            {
                "collected_at": "2026-07-28T10:00:00Z",
                "disks": [
                    {
                        "mount": "/srv/mnema-active",
                        "device": "/dev/sda",
                        "model": "Kingston",
                        "serial": "active-serial",
                        "passed": active_passed,
                        "temperature_celsius": 31,
                        "power_on_hours": 100,
                    },
                    {
                        "mount": "/srv/mnema-backup",
                        "device": "/dev/sdb",
                        "model": "Western Digital",
                        "serial": "backup-serial",
                        "passed": backup_passed,
                        "temperature_celsius": 33,
                        "power_on_hours": 200,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_smart_health_requires_every_disk_to_pass(tmp_path: Path) -> None:
    path = tmp_path / "smart.json"
    report(path)
    disks = read_smart_health(path)
    assert len(disks) == 2
    assert disks[0].temperature_celsius == 31
    assert smart_report_healthy(path)

    report(path, backup_passed=False)
    assert not smart_report_healthy(path)


def test_missing_or_invalid_smart_report_fails_closed(tmp_path: Path) -> None:
    assert not smart_report_healthy(tmp_path / "missing.json")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    assert not smart_report_healthy(invalid)
