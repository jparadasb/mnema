from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_stress_and_failure_harness_smoke() -> None:
    script = Path(__file__).parents[2] / "scripts" / "stress-test.py"
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [
            sys.executable,
            str(script),
            "--mode",
            "all",
            "--smoke",
            "--concurrency",
            "2",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert result["temporary_data_only"] is True
    assert result["deletion_exercised"] is False
    assert result["large"]["quarantined"] == 1
    assert result["small"]["quarantined"] == 32
    assert result["small"]["concurrency"] == 2
    assert result["large"]["restore_items_verified"] == 1
    assert result["large"]["restore_copies_verified"] == 2
    assert result["small"]["restore_items_verified"] == 2
    assert result["small"]["restore_copies_verified"] == 4
    assert all(case["final_state"] == "QUARANTINED" for case in result["failures"])
    assert all(case["local_snapshots"] == 1 for case in result["failures"])
    assert all(case["cold_objects"] == 1 for case in result["failures"])
    assert result["missing_backup"]["backup_exists"] is False
    assert result["missing_backup"]["global_deletion_enabled"] == "false"
    assert result["missing_backup"]["safety_lock"] == "true"
    assert result["missing_backup"]["database_integrity_healthy"] is True
