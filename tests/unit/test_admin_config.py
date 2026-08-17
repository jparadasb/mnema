from pathlib import Path

import pytest

from mnema.admin.config import (
    ApplianceConfig,
    CloudflareConfig,
    ColdStorageConfig,
    ICloudConfig,
    TelegramConfig,
    atomic_write,
    dump_config,
    load_config,
    redacted_payload,
    render_environment,
)


def test_appliance_config_round_trip_and_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    config = ApplianceConfig()
    atomic_write(path, dump_config(config), mode=0o640)

    loaded = load_config(path)
    environment = render_environment(loaded)

    assert loaded == config
    assert "MNEMA_GLOBAL_DELETION_ENABLED=false" in environment
    assert "MNEMA_SAFETY_LOCK=true" in environment
    assert "COMPOSE_PROFILES=integration" in environment
    assert path.stat().st_mode & 0o777 == 0o640


def test_cloudflare_requires_complete_access_configuration() -> None:
    with pytest.raises(ValueError, match="team domain"):
        CloudflareConfig(enabled=True)

    configured = CloudflareConfig(
        enabled=True,
        team_domain="https://mnema.cloudflareaccess.com",
        audience="audience-tag",
        admin_hostname="admin.example.com",
    )
    config = ApplianceConfig(cloudflare=configured)

    assert "cloudflare" in config.runtime_environment()["COMPOSE_PROFILES"]
    assert config.runtime_environment()["MNEMA_CLOUDFLARE_ACCESS_REQUIRED"] == "false"

    with pytest.raises(ValueError, match="team domain"):
        CloudflareConfig(
            enabled=True,
            team_domain="https://example.com/path.cloudflareaccess.com",
            audience="audience-tag",
            admin_hostname="admin.example.com",
        )


def test_cold_storage_validation_and_redaction() -> None:
    with pytest.raises(ValueError, match="remote:path"):
        ColdStorageConfig(
            transport="rclone",
            integration_minio=False,
            rclone_remote_root="invalid",
        )

    config = ApplianceConfig(
        cold_storage=ColdStorageConfig(
            transport="rclone",
            integration_minio=False,
            rclone_remote_root="remote:mnema",
        )
    )
    redacted = redacted_payload(config)

    assert redacted["cold_storage"]["rclone_config_file"] == "<secret-file>"
    assert "remote:mnema" in render_environment(config)


def test_scaleway_glacier_requires_supported_region_and_exact_endpoint() -> None:
    with pytest.raises(ValueError, match="region"):
        ColdStorageConfig(
            transport="s3",
            provider="scaleway",
            integration_minio=False,
            s3_region="pl-waw",
            s3_endpoint_url="https://s3.pl-waw.scw.cloud",
            s3_bucket="mnema-archive",
        )
    with pytest.raises(ValueError, match="endpoint"):
        ColdStorageConfig(
            transport="s3",
            provider="scaleway",
            integration_minio=False,
            s3_region="fr-par",
            s3_endpoint_url="https://example.com",
            s3_bucket="mnema-archive",
        )

    cold = ColdStorageConfig(
        transport="s3",
        provider="scaleway",
        integration_minio=False,
        s3_region="fr-par",
        s3_endpoint_url="https://s3.fr-par.scw.cloud",
        s3_bucket="mnema-archive",
    )
    environment = ApplianceConfig(cold_storage=cold).runtime_environment()

    assert environment["MNEMA_S3_PROVIDER"] == "scaleway"
    assert environment["MNEMA_S3_REGION"] == "fr-par"
    assert environment["MNEMA_S3_ENDPOINT_URL"] == "https://s3.fr-par.scw.cloud"


def test_icloud_configuration_is_complete_safe_and_redacted() -> None:
    with pytest.raises(ValueError, match="Apple ID"):
        ICloudConfig(enabled=True)
    with pytest.raises(ValueError, match="HH:MM"):
        ICloudConfig(enabled=True, apple_id="archive@example.com", daily_at="25:00")
    with pytest.raises(ValueError, match="/etc/mnema/icloud-session"):
        ICloudConfig(session_directory=Path("/var/lib/mnema/not-session"))

    config = ApplianceConfig(
        icloud=ICloudConfig(enabled=True, apple_id="archive@example.com", daily_at="03:00")
    )

    assert config.runtime_environment()["MNEMA_ICLOUD_ENABLED"] == "true"
    assert redacted_payload(config)["icloud"]["apple_id"] == "<redacted-apple-id>"


def test_telegram_configuration_requires_allowlist_and_renders_profile() -> None:
    with pytest.raises(ValueError, match="allowed user ID"):
        TelegramConfig(enabled=True)

    config = ApplianceConfig(telegram=TelegramConfig(enabled=True, allowed_user_ids=(12345, 12345)))
    environment = config.runtime_environment()
    assert "telegram" in environment["COMPOSE_PROFILES"]
    assert environment["MNEMA_TELEGRAM_ALLOWED_USER_IDS"] == "[12345]"
    assert redacted_payload(config)["telegram"]["bot_token_file"] == "<secret-file>"  # noqa: S105
