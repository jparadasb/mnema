#!/usr/bin/env python3
"""Measure Compose cgroup memory without relying on docker stats."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

CGROUP_ROOT = Path("/sys/fs/cgroup")
NON_FILE_MEMORY_KEYS = (
    "anon",
    "shmem",
    "kernel_stack",
    "pagetables",
    "percpu",
    "sock",
    "slab",
)


def command(*arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed Docker executable and argument array
        arguments,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def read_cgroup_path(pid: int) -> Path:
    entries = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
    unified = next((line.split(":", 2)[2] for line in entries if line.startswith("0::")), None)
    if unified is None:
        raise RuntimeError("cgroup v2 unified hierarchy is required")
    path = (CGROUP_ROOT / unified.lstrip("/")).resolve()
    if not path.is_relative_to(CGROUP_ROOT):
        raise RuntimeError("container cgroup escaped cgroup root")
    return path


def read_integer(path: Path) -> int | None:
    value = path.read_text(encoding="utf-8").strip()
    return None if value == "max" else int(value)


def read_memory_stat(path: Path) -> dict[str, int]:
    return {
        key: int(value)
        for key, value in (
            line.split(maxsplit=1)
            for line in (path / "memory.stat").read_text(encoding="utf-8").splitlines()
        )
    }


def read_smaps_rollup(pid: int) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in Path(f"/proc/{pid}/smaps_rollup").read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        fields = raw_value.split()
        if fields and fields[0].isdigit():
            values[key] = int(fields[0]) * 1024
    return values


def measure_from_processes(cgroup: Path) -> dict[str, int | None]:
    pids = [
        int(value) for value in (cgroup / "cgroup.procs").read_text(encoding="utf-8").splitlines()
    ]
    rollups: list[dict[str, int]] = []
    for pid in pids:
        try:
            rollups.append(read_smaps_rollup(pid))
        except FileNotFoundError:
            continue
    return {
        "pids": len(rollups),
        "resident_total_bytes": sum(item.get("Pss", 0) for item in rollups),
        "memory_peak_bytes": None,
        "non_file_memory_bytes": sum(
            item.get("Pss_Anon", 0) + item.get("Pss_Shmem", 0) for item in rollups
        ),
        "file_cache_bytes": sum(item.get("Pss_File", 0) for item in rollups),
        "swap_bytes": sum(item.get("SwapPss", 0) for item in rollups),
    }


def measure(compose_file: Path) -> dict[str, Any]:
    container_ids = command(
        "docker",
        "compose",
        "--file",
        str(compose_file),
        "ps",
        "--quiet",
    ).splitlines()
    if not container_ids:
        raise RuntimeError("no running Compose containers found")
    containers: list[dict[str, Any]] = []
    seen_cgroups: set[Path] = set()
    for container_id in container_ids:
        inspect = json.loads(command("docker", "inspect", container_id))[0]
        pid = int(inspect["State"]["Pid"])
        cgroup = read_cgroup_path(pid)
        if cgroup in seen_cgroups:
            raise RuntimeError("multiple containers resolved to same cgroup")
        seen_cgroups.add(cgroup)
        if (cgroup / "memory.stat").is_file():
            stats = read_memory_stat(cgroup)
            memory = {
                "pids": read_integer(cgroup / "pids.current"),
                "resident_total_bytes": read_integer(cgroup / "memory.current"),
                "memory_peak_bytes": read_integer(cgroup / "memory.peak"),
                "non_file_memory_bytes": sum(stats.get(key, 0) for key in NON_FILE_MEMORY_KEYS),
                "file_cache_bytes": stats.get("file", 0),
                "swap_bytes": stats.get("swap", 0),
            }
            method = "cgroup-v2-memory-controller"
        else:
            memory = measure_from_processes(cgroup)
            method = "cgroup-membership-proc-smaps-rollup"
        containers.append(
            {
                "service": inspect["Config"]["Labels"].get(
                    "com.docker.compose.service",
                    inspect["Name"].lstrip("/"),
                ),
                "container_id": container_id,
                "method": method,
                **memory,
            }
        )
    total_non_file = sum(item["non_file_memory_bytes"] for item in containers)
    return {
        "measurement": "Docker cgroup membership",
        "filesystem_cache_excluded": True,
        "containers": sorted(containers, key=lambda item: item["service"]),
        "total_non_file_memory_bytes": total_non_file,
        "target_bytes": int(1.2 * 1024**3),
        "below_target": total_non_file < int(1.2 * 1024**3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", type=Path, default=Path("compose.yaml"))
    args = parser.parse_args()
    if not args.compose_file.is_file():
        parser.error("compose file does not exist")
    print(json.dumps(measure(args.compose_file.resolve()), indent=2))


if __name__ == "__main__":
    main()
