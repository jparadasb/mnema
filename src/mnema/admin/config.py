from __future__ import annotations

import os
import re
import shutil
import tempfile
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, field_validator, model_validator


class StorageConfig(BaseModel):
    active_root: Path = Path("/srv/mnema-active")
    backup_root: Path = Path("/srv/mnema-backup")
    source_root: Path = Path("/var/lib/mnema/test-source")

    @field_validator("active_root", "backup_root", "source_root")
    @classmethod
    def absolute_safe_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("storage paths must be absolute")
        if any(character in str(value) for character in ("\n", "\r", "\0", "\\")):
            raise ValueError("storage paths contain unsupported characters")
        return value

    @model_validator(mode="after")
    def distinct_archive_roots(self) -> StorageConfig:
        if self.active_root == self.backup_root:
            raise ValueError("active and backup roots must differ")
        return self


class ColdStorageConfig(BaseModel):
    transport: Literal["rclone", "s3"] = "s3"
    provider: Literal["generic", "scaleway"] = "generic"
    rclone_remote_root: str = ""
    rclone_config_file: Path = Path("/etc/mnema/secrets/rclone.conf")
    s3_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_bucket: str = ""
    s3_access_key_file: Path = Path("/etc/mnema/secrets/s3_access_key")
    s3_secret_key_file: Path = Path("/etc/mnema/secrets/s3_secret_key")
    integration_minio: bool = True

    @field_validator(
        "rclone_config_file",
        "s3_access_key_file",
        "s3_secret_key_file",
    )
    @classmethod
    def absolute_secret_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("secret file paths must be absolute")
        return value

    @model_validator(mode="after")
    def transport_is_complete(self) -> ColdStorageConfig:
        if self.integration_minio:
            if self.provider != "generic":
                raise ValueError("local MinIO cannot use the Scaleway provider")
            return self
        if self.transport == "rclone":
            if self.provider != "generic":
                raise ValueError("Scaleway Glacier requires direct S3 transport")
            remote, separator, path = self.rclone_remote_root.partition(":")
            if (
                separator != ":"
                or not re.fullmatch(r"[A-Za-z0-9_-]+", remote)
                or not path
                or any(character in path for character in ("\n", "\r", "\0"))
            ):
                raise ValueError("rclone remote root must use remote:path syntax")
        else:
            endpoint = urlsplit(self.s3_endpoint_url)
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
            ):
                raise ValueError("direct S3 requires an HTTPS endpoint without credentials")
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.s3_bucket):
                raise ValueError("direct S3 bucket name is invalid")
            if self.provider == "scaleway":
                if self.s3_region not in {"fr-par", "nl-ams"}:
                    raise ValueError("Scaleway Glacier region must be fr-par or nl-ams")
                expected_endpoint = f"https://s3.{self.s3_region}.scw.cloud"
                if self.s3_endpoint_url != expected_endpoint:
                    raise ValueError(f"Scaleway endpoint must be {expected_endpoint}")
        return self


class CloudflareConfig(BaseModel):
    enabled: bool = False
    team_domain: str = ""
    audience: str = ""
    admin_hostname: str = ""
    tunnel_token_file: Path = Path("/etc/mnema/secrets/cloudflare_tunnel_token")

    @field_validator("tunnel_token_file")
    @classmethod
    def absolute_token_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Cloudflare tunnel token path must be absolute")
        return value

    @model_validator(mode="after")
    def enabled_configuration_is_complete(self) -> CloudflareConfig:
        if not self.enabled:
            return self
        team = urlsplit(self.team_domain)
        if (
            team.scheme != "https"
            or not team.hostname
            or not team.hostname.endswith(".cloudflareaccess.com")
            or team.username is not None
            or team.password is not None
            or team.port is not None
            or team.path not in {"", "/"}
            or team.query
            or team.fragment
        ):
            raise ValueError("Cloudflare team domain must be https://<team>.cloudflareaccess.com")
        if not self.audience or any(character.isspace() for character in self.audience):
            raise ValueError("Cloudflare Access audience is required")
        if (
            not self.admin_hostname
            or "://" in self.admin_hostname
            or "/" in self.admin_hostname
            or "." not in self.admin_hostname
            or any(character.isspace() for character in self.admin_hostname)
        ):
            raise ValueError("Cloudflare admin hostname must be a DNS hostname")
        return self


class ICloudConfig(BaseModel):
    enabled: bool = False
    apple_id: str = ""
    library: Literal["Personal Library"] = "Personal Library"
    daily_at: str = "03:00"
    session_directory: Path = Path("/etc/mnema/icloud-session")
    capacity_relief_enabled: bool = False
    cleanup_trigger_percent: float = Field(default=90, gt=0, le=100)
    cleanup_target_percent: float = Field(default=80, ge=0, lt=100)
    cleanup_quarantine_days: int = Field(default=7, ge=1, le=3650)
    cleanup_max_assets: int = Field(default=1000, ge=1, le=1000)
    cleanup_max_quota_percent: float = Field(default=10, gt=0, le=10)

    @field_validator("apple_id")
    @classmethod
    def valid_apple_id(cls, value: str) -> str:
        value = value.strip()
        if value and (
            not re.fullmatch(
                r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}",
                value,
            )
            or len(value) > 254
        ):
            raise ValueError("Apple ID must be an email address")
        return value

    @field_validator("daily_at")
    @classmethod
    def valid_daily_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("iCloud daily schedule must use HH:MM")
        return value

    @field_validator("session_directory")
    @classmethod
    def absolute_session_directory(cls, value: Path) -> Path:
        if value != Path("/etc/mnema/icloud-session"):
            raise ValueError("iCloud session directory must be /etc/mnema/icloud-session")
        return value

    @model_validator(mode="after")
    def enabled_configuration_is_complete(self) -> ICloudConfig:
        if self.enabled and not self.apple_id:
            raise ValueError("Apple ID is required when iCloud is enabled")
        if self.cleanup_target_percent >= self.cleanup_trigger_percent:
            raise ValueError("iCloud cleanup target must be below trigger")
        if self.capacity_relief_enabled and not self.enabled:
            raise ValueError("iCloud capacity relief requires iCloud Photos")
        return self


class SFTPGoConfig(BaseModel):
    username: str = "mnema-user"
    bind_address: str = "0.0.0.0"  # noqa: S104 - encrypted SFTP is LAN-facing by default

    @field_validator("username")
    @classmethod
    def safe_username(cls, value: str) -> str:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
        if not value or len(value) > 64 or value[0] not in allowed or not set(value) <= allowed:
            raise ValueError("SFTPGo username contains unsupported characters")
        return value

    @field_validator("bind_address")
    @classmethod
    def ipv4_bind_address(cls, value: str) -> str:
        if ip_address(value).version != 4:
            raise ValueError("SFTP bind address must be IPv4")
        return value


class ServiceConfig(BaseModel):
    local_bind_address: str = "127.0.0.1"
    start_at_boot: bool = True
    worker_concurrency: int = Field(default=1, ge=1, le=2)

    @field_validator("local_bind_address")
    @classmethod
    def ipv4_bind_address(cls, value: str) -> str:
        if ip_address(value).version != 4:
            raise ValueError("local web bind address must be IPv4")
        return value


class PolicyConfig(BaseModel):
    archive_after_days: int = Field(default=30, ge=0, le=36500)
    stability_window_hours: int = Field(default=24, ge=0, le=8760)
    quarantine_days: int = Field(default=7, ge=1, le=3650)
    dry_run: bool = True
    manual_approval: bool = True


class ApplianceConfig(BaseModel):
    version: Literal[1] = 1
    storage: StorageConfig = Field(default_factory=StorageConfig)
    cold_storage: ColdStorageConfig = Field(default_factory=ColdStorageConfig)
    cloudflare: CloudflareConfig = Field(default_factory=CloudflareConfig)
    icloud: ICloudConfig = Field(default_factory=ICloudConfig)
    sftpgo: SFTPGoConfig = Field(default_factory=SFTPGoConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)

    def runtime_environment(self) -> dict[str, str]:
        profiles: list[str] = []
        if self.cold_storage.integration_minio:
            profiles.append("integration")
        if self.cloudflare.enabled:
            profiles.append("cloudflare")
        environment = {
            "MNEMA_DATABASE_URL": "sqlite:////var/lib/mnema/mnema.db",
            "MNEMA_ACTIVE_ROOT": "/data/active",
            "MNEMA_BACKUP_ROOT": "/data/backup",
            "MNEMA_STAGING_ROOT": "/data/active/.mnema-staging",
            "MNEMA_SOURCE_ROOT": "/data/test-source",
            "MNEMA_HOST_ACTIVE_ROOT": str(self.storage.active_root),
            "MNEMA_HOST_BACKUP_ROOT": str(self.storage.backup_root),
            "MNEMA_HOST_SOURCE_ROOT": str(self.storage.source_root),
            "MNEMA_HOST_CONFIG_ROOT": "/var/lib/mnema",
            "MNEMA_HOST_MINIO_ROOT": "/var/lib/mnema/minio",
            "MNEMA_HOST_SFTPGO_DATA_ROOT": "/var/lib/mnema/sftpgo-data",
            "MNEMA_HOST_SFTPGO_HOME_ROOT": "/var/lib/mnema/sftpgo-home",
            "MNEMA_HOST_ICLOUD_SESSION_ROOT": str(self.icloud.session_directory),
            "MNEMA_SECRET_KEY_FILE": "/run/secrets/mnema_secret_key",
            "MNEMA_COLD_ENCRYPTION_KEY_FILE": "/run/secrets/mnema_cold_key",
            "MNEMA_KOPIA_PASSWORD_FILE": "/run/secrets/kopia_password",
            "MNEMA_KOPIA_REPOSITORY": "/data/backup/kopia-repository",
            "MNEMA_KOPIA_CONFIG_FILE": "/var/lib/mnema/kopia/repository.config",
            "MNEMA_USE_EXTERNAL_TEST_STORAGE": "true",
            "MNEMA_COLD_STORAGE_TRANSPORT": self.cold_storage.transport,
            "MNEMA_S3_PROVIDER": self.cold_storage.provider,
            "MNEMA_RCLONE_CONFIG_FILE": "/run/secrets/rclone.conf",
            "MNEMA_RCLONE_REMOTE_ROOT": self.cold_storage.rclone_remote_root,
            "MNEMA_S3_ENDPOINT_URL": self.cold_storage.s3_endpoint_url,
            "MNEMA_S3_REGION": self.cold_storage.s3_region,
            "MNEMA_S3_BUCKET": self.cold_storage.s3_bucket,
            "MNEMA_S3_ACCESS_KEY_FILE": "/run/secrets/s3_access_key",
            "MNEMA_S3_SECRET_KEY_FILE": "/run/secrets/s3_secret_key",
            "MNEMA_SFTPGO_ENDPOINT_URL": "http://sftpgo:8080",
            "MNEMA_SFTPGO_API_KEY_FILE": "/run/secrets/sftpgo_api_key",
            "MNEMA_SMART_HEALTH_FILE": "/var/lib/mnema/smart-health.json",
            "MNEMA_REQUIRE_SMART_HEALTH": "true",
            "MNEMA_ONBOARDING_TOKEN_FILE": "/run/secrets/onboarding_token",
            "MNEMA_GLOBAL_DELETION_ENABLED": "false",
            "MNEMA_SAFETY_LOCK": "true",
            "MNEMA_WORKER_CONCURRENCY": str(self.service.worker_concurrency),
            "MNEMA_LOCAL_BIND": self.service.local_bind_address,
            "MNEMA_SFTP_BIND": self.sftpgo.bind_address,
            "MNEMA_CLOUDFLARE_ACCESS_REQUIRED": "false",
            "MNEMA_CLOUDFLARE_TEAM_DOMAIN": self.cloudflare.team_domain,
            "MNEMA_CLOUDFLARE_AUDIENCE": self.cloudflare.audience,
            "MNEMA_CLOUDFLARE_ADMIN_HOSTNAME": self.cloudflare.admin_hostname,
            "MNEMA_ICLOUD_ENABLED": str(self.icloud.enabled).lower(),
            "MNEMA_ICLOUD_APPLE_ID": self.icloud.apple_id,
            "MNEMA_ICLOUD_LIBRARY": "PrimarySync",
            "MNEMA_ICLOUD_SESSION_DIRECTORY": "/var/lib/mnema/icloud-session",
            "MNEMA_ICLOUD_IMPORT_ROOT": "/data/active/iCloud Photos",
            "MNEMA_ICLOUD_CAPACITY_RELIEF_ENABLED": str(
                self.icloud.capacity_relief_enabled
            ).lower(),
            "MNEMA_ICLOUD_CLEANUP_TRIGGER_PERCENT": str(self.icloud.cleanup_trigger_percent),
            "MNEMA_ICLOUD_CLEANUP_TARGET_PERCENT": str(self.icloud.cleanup_target_percent),
            "MNEMA_ICLOUD_CLEANUP_QUARANTINE_DAYS": str(self.icloud.cleanup_quarantine_days),
            "MNEMA_ICLOUD_CLEANUP_MAX_ASSETS": str(self.icloud.cleanup_max_assets),
            "MNEMA_ICLOUD_CLEANUP_MAX_QUOTA_PERCENT": str(self.icloud.cleanup_max_quota_percent),
            "COMPOSE_PROFILES": ",".join(profiles),
        }
        if self.cold_storage.integration_minio:
            environment.update(
                {
                    "MNEMA_COLD_STORAGE_TRANSPORT": "s3",
                    "MNEMA_S3_PROVIDER": "generic",
                    "MNEMA_S3_ENDPOINT_URL": "http://minio:9000",
                    "MNEMA_S3_REGION": "us-east-1",
                    "MNEMA_S3_BUCKET": "mnema-integration",
                    "MNEMA_S3_ACCESS_KEY_FILE": "/run/secrets/minio_user",
                    "MNEMA_S3_SECRET_KEY_FILE": "/run/secrets/minio_password",
                }
            )
        return environment


def load_config(path: Path) -> ApplianceConfig:
    if not path.is_file():
        return ApplianceConfig()
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("appliance configuration must be a YAML mapping")
    return ApplianceConfig.model_validate(payload)


def dump_config(config: ApplianceConfig) -> str:
    return str(
        yaml.safe_dump(
            config.model_dump(mode="json"),
            sort_keys=False,
            default_flow_style=False,
        )
    )


def render_environment(config: ApplianceConfig) -> str:
    lines = []
    for key, value in sorted(config.runtime_environment().items()):
        if any(character in value for character in ("\n", "\r", "\0")):
            raise ValueError(f"runtime value for {key} contains an unsupported character")
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, content: str, *, mode: int) -> None:
    atomic_write_bytes(path, content.encode(), mode=mode)


def atomic_write_bytes(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_copy(source: Path, destination: Path, *, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def redacted_payload(config: ApplianceConfig) -> dict[str, Any]:
    payload = config.model_dump(mode="json")
    payload["cloudflare"]["tunnel_token_file"] = "<secret-file>"  # noqa: S105
    payload["cold_storage"]["rclone_config_file"] = "<secret-file>"
    payload["cold_storage"]["s3_access_key_file"] = "<secret-file>"
    payload["cold_storage"]["s3_secret_key_file"] = "<secret-file>"  # noqa: S105
    if payload["icloud"]["apple_id"]:
        payload["icloud"]["apple_id"] = "<redacted-apple-id>"
    payload["icloud"]["session_directory"] = "<secret-directory>"
    return payload
