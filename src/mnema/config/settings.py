from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, field_validator
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
    max_files_deleted_per_run: Annotated[int, Field(ge=0, le=100)] = 5
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
    s3_bucket: str = "mnema-integration"
    s3_access_key_file: Path = Path("/run/secrets/minio_user")
    s3_secret_key_file: Path = Path("/run/secrets/minio_password")
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

    @field_validator(
        "active_root",
        "backup_root",
        "staging_root",
        "source_root",
        "kopia_repository",
        "kopia_config_file",
    )
    @classmethod
    def absolute_paths(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("storage paths must be absolute")
        return value
