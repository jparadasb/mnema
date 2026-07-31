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


def test_enabled_icloud_requires_identity_and_active_storage_containment(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="Apple ID"):
        Settings(icloud_enabled=True)

    with pytest.raises(ValidationError, match="beneath active storage"):
        Settings(
            active_root=tmp_path / "active",
            icloud_enabled=True,
            icloud_apple_id="archive@example.com",
            icloud_import_root=tmp_path / "outside",
        )


def test_scaleway_runtime_configuration_fails_closed() -> None:
    with pytest.raises(ValidationError, match="region"):
        Settings(
            s3_provider="scaleway",
            s3_region="pl-waw",
            s3_endpoint_url="https://s3.pl-waw.scw.cloud",
        )

    configured = Settings(
        s3_provider="scaleway",
        s3_region="nl-ams",
        s3_endpoint_url="https://s3.nl-ams.scw.cloud",
    )

    assert configured.s3_provider == "scaleway"
