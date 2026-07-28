from pathlib import Path

import pytest
from pydantic import ValidationError

from mnema.config import Settings


def test_rejects_relative_storage_path() -> None:
    with pytest.raises(ValidationError):
        Settings(active_root=Path("relative"))


def test_rejects_concurrency_above_two() -> None:
    with pytest.raises(ValidationError):
        Settings(worker_concurrency=3)
