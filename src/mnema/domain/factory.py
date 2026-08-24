from __future__ import annotations

import os

from mnema.adapters.backup.base import VersionedBackup
from mnema.adapters.backup.filesystem import FilesystemVersionedBackup
from mnema.adapters.backup.kopia import KopiaBackup
from mnema.adapters.cold_storage.base import ColdStorage
from mnema.adapters.cold_storage.local import LocalEncryptedColdStorage
from mnema.adapters.cold_storage.rclone import RcloneEncryptedColdStorage
from mnema.adapters.cold_storage.s3 import S3EncryptedColdStorage
from mnema.adapters.sources.icloud import ICloudPhotosSourceAdapter
from mnema.adapters.sources.local import LocalFilesystemSourceAdapter
from mnema.config import Settings, SourcePolicy
from mnema.domain.source import SourceAdapter
from mnema.domain.workflow import ArchiveWorkflow


def build_local_workflow(
    settings: Settings,
    *,
    policy: SourcePolicy | None = None,
    deletion_enabled: bool = False,
    source: SourceAdapter | None = None,
    source_provider: str = "local_test",
    source_is_active: bool = False,
) -> ArchiveWorkflow:
    key_file = settings.cold_encryption_key_file
    if key_file.is_file():
        key = key_file.read_bytes()
    else:
        key = bytes.fromhex(os.getenv("MNEMA_COLD_KEY_HEX", "00" * 32))

    backup: VersionedBackup
    cold: ColdStorage
    scratch = settings.scratch_directory
    if settings.use_external_test_storage:
        backup = KopiaBackup(
            settings.kopia_repository,
            settings.kopia_password_file,
            settings.kopia_config_file,
            scratch_root=scratch,
        )
        if settings.cold_storage_transport == "rclone":
            cold = RcloneEncryptedColdStorage(
                remote_root=settings.rclone_remote_root,
                config_file=settings.rclone_config_file,
                key=key,
                scratch_root=scratch,
            )
        else:
            cold = S3EncryptedColdStorage(
                bucket=settings.s3_bucket,
                key=key,
                endpoint_url=settings.s3_endpoint_url,
                region_name=settings.s3_region,
                access_key_file=settings.s3_access_key_file,
                secret_key_file=settings.s3_secret_key_file,
                create_bucket_if_missing=settings.s3_provider == "generic",
                provider_name=("scaleway-glacier" if settings.s3_provider == "scaleway" else "s3"),
                archive_storage_class=("GLACIER" if settings.s3_provider == "scaleway" else None),
                scratch_root=scratch,
            )
    else:
        backup = FilesystemVersionedBackup(settings.backup_root / "mnema-test-repository")
        cold = LocalEncryptedColdStorage(
            settings.backup_root / "mnema-test-cold", key, scratch_root=scratch
        )

    return ArchiveWorkflow(
        source=source
        or LocalFilesystemSourceAdapter(
            settings.source_root,
            allow_delete=deletion_enabled,
        ),
        backup=backup,
        cold=cold,
        active_root=settings.active_root,
        staging_root=settings.staging_root,
        policy=policy
        or SourcePolicy(
            archive_after_days=30,
            stability_window_hours=24,
            quarantine_days=7,
            deletion_enabled=deletion_enabled,
            manual_approval=True,
        ),
        source_provider=source_provider,
        source_is_active=source_is_active,
    )


def build_icloud_workflow(
    settings: Settings,
    *,
    policy: SourcePolicy | None = None,
) -> ArchiveWorkflow:
    if not settings.icloud_enabled:
        raise ValueError("iCloud Photos is disabled")
    return build_local_workflow(
        settings,
        policy=policy,
        deletion_enabled=False,
        source=ICloudPhotosSourceAdapter(
            settings.icloud_import_root,
            settings.icloud_session_directory,
        ),
        source_provider="icloud_photos",
        source_is_active=True,
    )
