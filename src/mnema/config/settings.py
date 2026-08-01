from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SourcePolicy(BaseModel):
    included_directories: tuple[str, ...] = ()
    excluded_directories: tuple[str, ...] = ()
    included_globs: tuple[str, ...] = ("**/*", "*")
    excluded_globs: tuple[str, ...] = ()
    minimum_file_size: int = 0
    maximum_file_size: int | None = None
    archive_after_days: int = 30
    stability_window_hours: int = 24
    quarantine_days: int = 7
    dry_run: bool = True
    manual_approval: bool = True
    deletion_enabled: bool = False


class DeletionLimits(BaseModel):
    max_files_deleted_per_run: Annotated[int, Field(ge=0, le=1000)] = 5
    max_bytes_deleted_per_run: Annotated[int, Field(ge=0)] = 1_000_000_000
    max_percentage_deleted_per_run: Annotated[float, Field(ge=0, le=100)] = 1.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MNEMA_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = "sqlite:///./mnema.db"
    active_root: Path = Path("/data/active")
    backup_root: Path = Path("/data/backup")
    staging_root: Path = Path("/data/staging")
    source_root: Path = Path("/data/test-source")
    secret_key_file: Path = Path("/run/secrets/mnema_secret_key")
    cold_encryption_key_file: Path = Path("/run/secrets/mnema_cold_key")
    kopia_password_file: Path = Path("/run/secrets/kopia_password")
    kopia_repository: Path = Path("/data/backup/kopia-repository")
    kopia_config_file: Path = Path("/var/lib/mnema/kopia/repository.config")
    use_external_test_storage: bool = False
    s3_endpoint_url: str = "http://minio:9000"
    s3_provider: Literal["generic", "scaleway"] = "generic"
    s3_region: str = "us-east-1"
    s3_bucket: str = "mnema-integration"
    s3_access_key_file: Path = Path("/run/secrets/minio_user")
    s3_secret_key_file: Path = Path("/run/secrets/minio_password")
    cold_storage_transport: Literal["s3", "rclone"] = "s3"
    rclone_config_file: Path = Path("/run/secrets/rclone.conf")
    rclone_remote_root: str = "minio:mnema-integration"
    sftpgo_endpoint_url: str = "http://sftpgo:8080"
    sftpgo_api_key_file: Path = Path("/run/secrets/sftpgo_api_key")
    smart_health_file: Path = Path("/var/lib/mnema/smart-health.json")
    require_smart_health: bool = False
    onboarding_token_file: Path = Path("/run/secrets/onboarding_token")
    global_deletion_enabled: bool = False
    safety_lock: bool = True
    worker_concurrency: Annotated[int, Field(ge=1, le=2)] = 1
    per_adapter_concurrency: Annotated[int, Field(ge=1, le=2)] = 1
    pause_when_active_disk_free_percent_below: float = 10
    pause_when_backup_disk_free_percent_below: float = 10
    cloudflare_access_required: bool = False
    cloudflare_team_domain: str = ""
    cloudflare_audience: str = ""
    cloudflare_admin_hostname: str = ""
    file_provider_enabled: bool = False
    file_provider_public_url: str = ""
    file_provider_upload_root: Path = Path("/data/active/.mnema-file-provider")
    file_provider_max_file_size: Annotated[int, Field(gt=0)] = 53_687_091_200
    file_provider_minimum_free_percent: Annotated[float, Field(ge=1, le=50)] = 10
    icloud_enabled: bool = False
    icloud_apple_id: str = ""
    icloud_library: Literal["PrimarySync"] = "PrimarySync"
    icloud_session_directory: Path = Path("/var/lib/mnema/icloud-session")
    icloud_import_root: Path = Path("/data/active/iCloud Photos")
    icloud_capacity_relief_enabled: bool = False
    icloud_deletion_milestone_approved: bool = False
    icloud_cleanup_trigger_percent: Annotated[float, Field(gt=0, le=100)] = 90
    icloud_cleanup_target_percent: Annotated[float, Field(ge=0, lt=100)] = 80
    icloud_cleanup_quarantine_days: Annotated[int, Field(ge=1, le=3650)] = 7
    icloud_cleanup_max_assets: Annotated[int, Field(ge=1, le=1000)] = 1000
    icloud_cleanup_max_quota_percent: Annotated[float, Field(gt=0, le=10)] = 10

    @field_validator(
        "active_root",
        "backup_root",
        "staging_root",
        "source_root",
        "kopia_repository",
        "kopia_config_file",
        "rclone_config_file",
        "icloud_session_directory",
        "icloud_import_root",
        "file_provider_upload_root",
    )
    @classmethod
    def absolute_paths(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("storage paths must be absolute")
        return value

    @model_validator(mode="after")
    def cloudflare_access_is_complete(self) -> Settings:
        if not self.cloudflare_access_required:
            return self
        team = urlsplit(self.cloudflare_team_domain)
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
            raise ValueError("Cloudflare Access requires a valid HTTPS team domain")
        if not self.cloudflare_audience:
            raise ValueError("Cloudflare Access audience is required")
        return self

    @model_validator(mode="after")
    def file_provider_configuration_is_safe(self) -> Settings:
        if not self.file_provider_enabled:
            return self
        if not self.file_provider_upload_root.is_relative_to(self.active_root):
            raise ValueError("File Provider upload root must remain beneath active storage")
        public = urlsplit(self.file_provider_public_url)
        if (
            public.scheme != "https"
            or not public.hostname
            or public.username is not None
            or public.password is not None
            or public.port is not None
            or public.path not in {"", "/"}
            or public.query
            or public.fragment
        ):
            raise ValueError("File Provider requires an HTTPS public URL without a path")
        return self

    @model_validator(mode="after")
    def icloud_configuration_is_complete(self) -> Settings:
        if not self.icloud_enabled:
            return self
        if not self.icloud_apple_id:
            raise ValueError("iCloud Photos requires an Apple ID")
        if not self.icloud_import_root.is_relative_to(self.active_root):
            raise ValueError("iCloud import root must remain beneath active storage")
        if self.icloud_cleanup_target_percent >= self.icloud_cleanup_trigger_percent:
            raise ValueError("iCloud cleanup target must be below trigger")
        return self

    @model_validator(mode="after")
    def scaleway_glacier_configuration_is_safe(self) -> Settings:
        if self.s3_provider != "scaleway":
            return self
        if self.cold_storage_transport != "s3":
            raise ValueError("Scaleway Glacier requires direct S3 transport")
        if self.s3_region not in {"fr-par", "nl-ams"}:
            raise ValueError("Scaleway Glacier region must be fr-par or nl-ams")
        expected_endpoint = f"https://s3.{self.s3_region}.scw.cloud"
        if self.s3_endpoint_url != expected_endpoint:
            raise ValueError(f"Scaleway endpoint must be {expected_endpoint}")
        return self
