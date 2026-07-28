from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SmartDisk:
    mount: str
    device: str
    model: str
    serial: str
    passed: bool
    temperature_celsius: int | None
    power_on_hours: int | None


def read_smart_health(path: Path) -> tuple[SmartDisk, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    disks: list[SmartDisk] = []
    for entry in payload.get("disks", []):
        if not isinstance(entry, dict):
            raise ValueError("invalid SMART disk record")
        disks.append(
            SmartDisk(
                mount=str(entry["mount"]),
                device=str(entry["device"]),
                model=str(entry.get("model", "unknown")),
                serial=str(entry.get("serial", "unknown")),
                passed=bool(entry["passed"]),
                temperature_celsius=_optional_int(entry.get("temperature_celsius")),
                power_on_hours=_optional_int(entry.get("power_on_hours")),
            )
        )
    if not disks:
        raise ValueError("SMART health report contains no disks")
    return tuple(disks)


def smart_report_healthy(path: Path) -> bool:
    try:
        disks = read_smart_health(path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False
    return all(disk.passed for disk in disks)


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None
