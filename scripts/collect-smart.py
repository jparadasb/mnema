#!/usr/bin/env python3
"""Collect host SMART health into an atomic, non-secret JSON report."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def command(*arguments: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed programs, argument arrays, no shell
        arguments,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def disk_for_mount(mount: Path) -> Path:
    source = command("/usr/bin/findmnt", "-n", "-o", "SOURCE", "--target", str(mount))
    parent = command("/usr/bin/lsblk", "-n", "-d", "-o", "PKNAME", source)
    return Path("/dev") / parent if parent else Path(source)


def nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def inspect_disk(mount: Path) -> dict[str, Any]:
    device = disk_for_mount(mount)
    result = subprocess.run(  # noqa: S603 - validated block device, no shell
        ["/usr/sbin/smartctl", "--json", "--info", "--health", "--attributes", str(device)],
        check=False,
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        raise RuntimeError(f"smartctl returned no JSON for {device}")
    payload = json.loads(result.stdout)
    passed = nested(payload, "smart_status", "passed")
    if passed is None:
        raise RuntimeError(f"smartctl returned no health status for {device}")
    return {
        "mount": str(mount),
        "device": str(device),
        "model": payload.get("model_name", "unknown"),
        "serial": payload.get("serial_number", "unknown"),
        "passed": bool(passed),
        "temperature_celsius": nested(payload, "temperature", "current"),
        "power_on_hours": nested(payload, "power_on_time", "hours"),
    }


def write_report(output: Path, mounts: list[Path]) -> None:
    report = {
        "collected_at": datetime.now(UTC).isoformat(),
        "disks": [inspect_disk(mount) for mount in mounts],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".smart-health-",
        dir=output.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(report, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, output)
    except BaseException:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("mounts", nargs="+", type=Path)
    args = parser.parse_args()
    write_report(args.output, args.mounts)


if __name__ == "__main__":
    main()
