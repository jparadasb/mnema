from __future__ import annotations

import difflib
import json
import socket
from pathlib import Path
from typing import Annotated, NoReturn

import click
import typer
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from mnema.admin.config import (
    ApplianceConfig,
    CloudflareConfig,
    ColdStorageConfig,
    FileProviderConfig,
    ICloudConfig,
    PolicyConfig,
    ServiceConfig,
    SFTPGoConfig,
    StorageConfig,
    load_config,
    redacted_payload,
    render_environment,
)
from mnema.admin.host import ApplianceManager, ServiceEndpointReport

configure_app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Configure Mnema with guided prompts.",
)
config_app = typer.Typer(no_args_is_help=True, help="Inspect and apply desired configuration.")
backup_app = typer.Typer(no_args_is_help=True, help="Back up appliance configuration.")
restore_app = typer.Typer(no_args_is_help=True, help="Restore appliance configuration.")
uninstall_app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Safely disable Mnema without deleting data.",
)
icloud_app = typer.Typer(no_args_is_help=True, help="Manage guarded iCloud Photos archiving.")
icloud_cleanup_app = typer.Typer(
    no_args_is_help=True, help="Preview and approve capacity-driven iCloud cleanup."
)
icloud_app.add_typer(icloud_cleanup_app, name="cleanup")
file_provider_app = typer.Typer(no_args_is_help=True, help="Pair and manage Apple devices.")


def _manager() -> ApplianceManager:
    return ApplianceManager()


def _fail(error: Exception) -> NoReturn:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(2)


def _save(
    manager: ApplianceManager,
    config: ApplianceConfig,
    *,
    yes: bool,
    secret_updates: dict[Path, str] | None = None,
) -> None:
    typer.echo(yaml_redacted(config))
    if not yes and not typer.confirm("Apply configuration and restart affected services?"):
        typer.echo("No changes made.")
        raise typer.Exit()
    try:
        manager.save_config(config, secret_updates=secret_updates)
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)
    typer.echo("Configuration applied and health check passed.")


def yaml_redacted(config: ApplianceConfig) -> str:
    return str(yaml.safe_dump(redacted_payload(config), sort_keys=False))


def _render_endpoint_report(report: ServiceEndpointReport) -> str:
    headers = ("SERVICE", "URL", "SCOPE", "STATE")
    rows = [
        (endpoint.service, endpoint.url, endpoint.scope, endpoint.state)
        for endpoint in report.endpoints
    ]
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in rows)])
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows
    )
    lines.extend(f"Warning: {warning}" for warning in report.warnings)
    return "\n".join(lines)


def _prompt_storage(current: StorageConfig) -> StorageConfig:
    return StorageConfig(
        active_root=Path(typer.prompt("Active storage mount", default=str(current.active_root))),
        backup_root=Path(typer.prompt("Backup storage mount", default=str(current.backup_root))),
        source_root=Path(typer.prompt("Source directory", default=str(current.source_root))),
    )


def _secret_file_update(source: Path, destination: Path) -> dict[Path, str]:
    if not source.is_file():
        raise ValueError(f"secret source file does not exist: {source}")
    content = source.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"secret source file is empty: {source}")
    return {destination: f"{content}\n"}


def _prompt_cold_storage(
    current: ColdStorageConfig,
    manager: ApplianceManager,
) -> tuple[ColdStorageConfig, dict[Path, str]]:
    transport = typer.prompt(
        "Cold-storage transport",
        default=current.transport,
        type=click.Choice(["rclone", "s3"]),
    )
    integration_minio = typer.confirm(
        "Use local MinIO integration storage (not off-site backup)?",
        default=current.integration_minio,
    )
    if transport == "rclone" and not integration_minio:
        target = manager.paths.secrets / "rclone.conf"
        updates: dict[Path, str] = {}
        if not target.is_file() or not typer.confirm(
            f"Reuse existing {target}?",
            default=True,
        ):
            source = Path(typer.prompt("Path to existing rclone config file"))
            updates = _secret_file_update(source, target)
        return (
            ColdStorageConfig(
                transport="rclone",
                rclone_remote_root=typer.prompt(
                    "rclone remote root",
                    default=current.rclone_remote_root or "remote:mnema",
                ),
                rclone_config_file=target,
            ),
            updates,
        )
    if integration_minio:
        return ColdStorageConfig(transport="s3", integration_minio=True), {}
    provider = typer.prompt(
        "S3 provider",
        default=current.provider,
        type=click.Choice(["scaleway", "generic"]),
    )
    if provider == "scaleway":
        region = typer.prompt(
            "Scaleway Glacier region",
            default=(current.s3_region if current.s3_region in {"fr-par", "nl-ams"} else "fr-par"),
            type=click.Choice(["fr-par", "nl-ams"]),
        )
        endpoint = f"https://s3.{region}.scw.cloud"
    else:
        region = typer.prompt("S3 region", default=current.s3_region)
        endpoint = typer.prompt(
            "S3 endpoint URL",
            default=current.s3_endpoint_url,
        )
    access_key = typer.prompt("S3 access key", hide_input=True)
    secret_key = typer.prompt("S3 secret key", hide_input=True, confirmation_prompt=True)
    if not access_key.strip() or not secret_key.strip():
        raise ValueError("S3 credentials cannot be empty")
    return (
        ColdStorageConfig(
            transport="s3",
            provider=provider,
            s3_endpoint_url=endpoint,
            s3_region=region,
            s3_bucket=typer.prompt("S3 bucket", default=current.s3_bucket or "mnema"),
            s3_access_key_file=manager.paths.secrets / "s3_access_key",
            s3_secret_key_file=manager.paths.secrets / "s3_secret_key",
        ),
        {
            manager.paths.secrets / "s3_access_key": f"{access_key.strip()}\n",
            manager.paths.secrets / "s3_secret_key": f"{secret_key.strip()}\n",
        },
    )


def _prompt_cloudflare(
    current: CloudflareConfig,
) -> tuple[CloudflareConfig, str | None]:
    enabled = typer.confirm("Enable Cloudflare Tunnel for Mnema admin?", default=current.enabled)
    if not enabled:
        return CloudflareConfig(enabled=False), None
    team_domain = typer.prompt(
        "Cloudflare Access team domain",
        default=current.team_domain or "https://team.cloudflareaccess.com",
    )
    audience = typer.prompt(
        "Cloudflare Access application audience",
        default=current.audience,
    )
    hostname = typer.prompt(
        "Public Mnema admin hostname",
        default=current.admin_hostname or "admin.example.com",
    )
    token = typer.prompt("Existing tunnel token", hide_input=True, confirmation_prompt=True)
    return (
        CloudflareConfig(
            enabled=True,
            team_domain=team_domain.rstrip("/"),
            audience=audience,
            admin_hostname=hostname,
            tunnel_token_file=current.tunnel_token_file,
        ),
        token,
    )


def _cloudflare_token_update(manager: ApplianceManager, token: str) -> dict[Path, str]:
    if len(token.strip()) < 32 or any(character.isspace() for character in token.strip()):
        raise ValueError("Cloudflare tunnel token is malformed")
    destination = manager.paths.secrets / "cloudflare_tunnel_token"
    return {destination: f"{token.strip()}\n"}


def _prompt_file_provider(
    current: FileProviderConfig, cloudflare: CloudflareConfig
) -> FileProviderConfig:
    enabled = typer.confirm("Enable iPhone File Provider?", default=current.enabled)
    if not enabled:
        return FileProviderConfig(enabled=False)
    if not cloudflare.enabled:
        raise ValueError("File Provider internet access requires Cloudflare Tunnel")
    return FileProviderConfig(
        enabled=True,
        public_url=typer.prompt(
            "Public File Provider URL",
            default=current.public_url or "https://files.example.com",
        ),
        max_file_size=current.max_file_size,
        minimum_free_percent=current.minimum_free_percent,
    )


def _prompt_icloud(current: ICloudConfig) -> ICloudConfig:
    enabled = typer.confirm("Enable read-only iCloud Photos archiving?", default=current.enabled)
    if not enabled:
        return ICloudConfig(enabled=False)
    typer.echo(
        "Requirement: dedicated Apple account, iCloud web access enabled, "
        "Advanced Data Protection disabled."
    )
    if not typer.confirm("I understand and have applied these Apple account requirements."):
        raise ValueError(
            "iCloud setup stopped because Apple account requirements were not accepted"
        )
    capacity_relief = typer.confirm(
        "Enable guarded capacity-relief proposals at 90% iCloud usage?",
        default=current.capacity_relief_enabled,
    )
    if capacity_relief:
        typer.echo(
            "Cleanup requires exact manifest approval and moves assets to Recently Deleted. "
            "Favorites and Raspberry Pi copies remain protected."
        )
    return ICloudConfig(
        enabled=True,
        apple_id=typer.prompt("Dedicated Apple ID", default=current.apple_id),
        daily_at=typer.prompt("Daily import time (HH:MM)", default=current.daily_at),
        capacity_relief_enabled=capacity_relief,
        cleanup_trigger_percent=current.cleanup_trigger_percent,
        cleanup_target_percent=current.cleanup_target_percent,
        cleanup_quarantine_days=current.cleanup_quarantine_days,
        cleanup_max_assets=current.cleanup_max_assets,
        cleanup_max_quota_percent=current.cleanup_max_quota_percent,
    )


@configure_app.callback()
def configure_all(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", help="Apply without final confirmation.")] = False,
) -> None:
    """Run complete configuration wizard when no section is specified."""
    if ctx.invoked_subcommand is not None:
        return
    manager = _manager()
    try:
        current = manager.config()
        storage = _prompt_storage(current.storage)
        cold, cold_secrets = _prompt_cold_storage(current.cold_storage, manager)
        cloudflare, token = _prompt_cloudflare(current.cloudflare)
        file_provider = _prompt_file_provider(current.file_provider, cloudflare)
        icloud = _prompt_icloud(current.icloud)
        sftpgo = SFTPGoConfig(
            username=typer.prompt("SFTPGo username", default=current.sftpgo.username),
            bind_address=typer.prompt(
                "SFTP bind address",
                default=current.sftpgo.bind_address,
            ),
        )
        service = ServiceConfig(
            local_bind_address=typer.prompt(
                "Local web bind address",
                default=current.service.local_bind_address,
            ),
            start_at_boot=typer.confirm(
                "Start Mnema automatically at boot?",
                default=current.service.start_at_boot,
            ),
            worker_concurrency=typer.prompt(
                "Worker concurrency",
                default=current.service.worker_concurrency,
                type=int,
            ),
        )
        config = current.model_copy(
            update={
                "storage": storage,
                "cold_storage": cold,
                "cloudflare": cloudflare,
                "file_provider": file_provider,
                "icloud": icloud,
                "sftpgo": sftpgo,
                "service": service,
            }
        )
        secret_updates = cold_secrets
        if token is not None:
            secret_updates.update(_cloudflare_token_update(manager, token))
        _save(manager, config, yes=yes, secret_updates=secret_updates)
        if icloud.enabled:
            manager.icloud_auth()
            manager.icloud_preview()
    except (OSError, ValueError, ValidationError, PermissionError) as error:
        _fail(error)


@configure_app.command("storage")
def configure_storage(
    active_root: Annotated[Path | None, typer.Option()] = None,
    backup_root: Annotated[Path | None, typer.Option()] = None,
    source_root: Annotated[Path | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    manager = _manager()
    try:
        current = manager.config()
        storage = (
            _prompt_storage(current.storage)
            if active_root is None or backup_root is None or source_root is None
            else StorageConfig(
                active_root=active_root,
                backup_root=backup_root,
                source_root=source_root,
            )
        )
        _save(manager, current.model_copy(update={"storage": storage}), yes=yes)
    except (OSError, ValueError, ValidationError, PermissionError) as error:
        _fail(error)


@configure_app.command("cold-storage")
def configure_cold_storage(
    transport: Annotated[str | None, typer.Option()] = None,
    provider: Annotated[str | None, typer.Option()] = None,
    region: Annotated[str | None, typer.Option()] = None,
    remote_root: Annotated[str | None, typer.Option()] = None,
    s3_endpoint: Annotated[str | None, typer.Option()] = None,
    s3_bucket: Annotated[str | None, typer.Option()] = None,
    rclone_config_file: Annotated[Path | None, typer.Option()] = None,
    s3_access_key_file: Annotated[Path | None, typer.Option()] = None,
    s3_secret_key_file: Annotated[Path | None, typer.Option()] = None,
    integration_minio: Annotated[bool, typer.Option()] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    manager = _manager()
    try:
        current = manager.config()
        secret_updates: dict[Path, str] = {}
        if transport is None and provider is None:
            cold, secret_updates = _prompt_cold_storage(current.cold_storage, manager)
        elif transport == "rclone":
            cold = ColdStorageConfig(
                transport="rclone",
                rclone_remote_root=remote_root or "",
                rclone_config_file=manager.paths.secrets / "rclone.conf",
            )
            if rclone_config_file is not None:
                secret_updates.update(
                    _secret_file_update(
                        rclone_config_file,
                        manager.paths.secrets / "rclone.conf",
                    )
                )
            elif not (manager.paths.secrets / "rclone.conf").is_file():
                raise ValueError("--rclone-config-file is required for initial setup")
        elif transport == "s3" or provider == "scaleway":
            selected_provider = provider or "generic"
            selected_region = region or (
                "fr-par" if selected_provider == "scaleway" else "us-east-1"
            )
            selected_endpoint = (
                f"https://s3.{selected_region}.scw.cloud"
                if selected_provider == "scaleway"
                else (s3_endpoint or "")
            )
            cold = ColdStorageConfig(
                transport="s3",
                provider=selected_provider,
                s3_endpoint_url=selected_endpoint,
                s3_region=selected_region,
                s3_bucket=s3_bucket or "",
                integration_minio=integration_minio,
                s3_access_key_file=manager.paths.secrets / "s3_access_key",
                s3_secret_key_file=manager.paths.secrets / "s3_secret_key",
            )
            if not integration_minio:
                if s3_access_key_file is None or s3_secret_key_file is None:
                    raise ValueError("direct S3 requires both credential files")
                secret_updates.update(
                    _secret_file_update(
                        s3_access_key_file,
                        manager.paths.secrets / "s3_access_key",
                    )
                )
                secret_updates.update(
                    _secret_file_update(
                        s3_secret_key_file,
                        manager.paths.secrets / "s3_secret_key",
                    )
                )
        else:
            raise ValueError("transport must be rclone or s3; provider may be scaleway")
        _save(
            manager,
            current.model_copy(update={"cold_storage": cold}),
            yes=yes,
            secret_updates=secret_updates,
        )
    except (OSError, ValueError, ValidationError, PermissionError) as error:
        _fail(error)


@configure_app.command("cloudflare")
def configure_cloudflare(
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
    team_domain: Annotated[str | None, typer.Option()] = None,
    audience: Annotated[str | None, typer.Option()] = None,
    hostname: Annotated[str | None, typer.Option()] = None,
    token_file: Annotated[Path | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    manager = _manager()
    try:
        current = manager.config()
        token: str | None = None
        if enabled is None:
            cloudflare, token = _prompt_cloudflare(current.cloudflare)
        elif not enabled:
            cloudflare = CloudflareConfig(enabled=False)
        else:
            cloudflare = CloudflareConfig(
                enabled=True,
                team_domain=(team_domain or "").rstrip("/"),
                audience=audience or "",
                admin_hostname=hostname or "",
            )
            if token_file is None or not token_file.is_file():
                raise ValueError("--token-file must reference an existing file")
            token = token_file.read_text(encoding="utf-8").strip()
        secret_updates = _cloudflare_token_update(manager, token) if token is not None else None
        _save(
            manager,
            current.model_copy(update={"cloudflare": cloudflare}),
            yes=yes,
            secret_updates=secret_updates,
        )
    except (OSError, ValueError, ValidationError, PermissionError) as error:
        _fail(error)


@configure_app.command("sftpgo")
def configure_sftpgo(
    username: Annotated[str | None, typer.Option()] = None,
    bind_address: Annotated[str | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    manager = _manager()
    try:
        current = manager.config()
        value = SFTPGoConfig(
            username=username or typer.prompt("SFTPGo username", default=current.sftpgo.username),
            bind_address=bind_address
            or typer.prompt("SFTP bind address", default=current.sftpgo.bind_address),
        )
        _save(manager, current.model_copy(update={"sftpgo": value}), yes=yes)
    except (OSError, ValueError, ValidationError, PermissionError) as error:
        _fail(error)


@configure_app.command("file-provider")
def configure_file_provider(
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
    public_url: Annotated[str | None, typer.Option("--public-url")] = None,
    max_file_size: Annotated[int | None, typer.Option(min=1)] = None,
    minimum_free_percent: Annotated[float | None, typer.Option(min=1, max=50)] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    manager = _manager()
    try:
        current = manager.config()
        use_enabled = (
            enabled
            if enabled is not None
            else typer.confirm(
                "Enable iPhone File Provider?", default=current.file_provider.enabled
            )
        )
        if use_enabled and not current.cloudflare.enabled:
            raise ValueError("configure Cloudflare before enabling File Provider")
        value = FileProviderConfig(
            enabled=use_enabled,
            public_url=(
                public_url
                or (
                    typer.prompt(
                        "Public File Provider URL",
                        default=current.file_provider.public_url or "https://files.example.com",
                    )
                    if use_enabled
                    else ""
                )
            ),
            max_file_size=max_file_size or current.file_provider.max_file_size,
            minimum_free_percent=(
                minimum_free_percent or current.file_provider.minimum_free_percent
            ),
        )
        _save(manager, current.model_copy(update={"file_provider": value}), yes=yes)
    except (OSError, ValueError, ValidationError, PermissionError) as error:
        _fail(error)


@file_provider_app.command("pair")
def file_provider_pair() -> None:
    try:
        result = _manager().file_provider_runtime_command(
            "file-provider-pair-internal", capture_output=True
        )
        typer.echo(result.stdout.strip())
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)


@file_provider_app.command("devices")
def file_provider_devices() -> None:
    try:
        result = _manager().file_provider_runtime_command(
            "file-provider-devices-internal", capture_output=True
        )
        typer.echo(result.stdout.strip())
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)


@file_provider_app.command("revoke")
def file_provider_revoke(device_id: str) -> None:
    try:
        result = _manager().file_provider_runtime_command(
            "file-provider-revoke-internal", device_id, capture_output=True
        )
        typer.echo(result.stdout.strip())
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)


@file_provider_app.command("project")
def file_provider_project() -> None:
    try:
        result = _manager().file_provider_runtime_command(
            "file-provider-project-internal", capture_output=True
        )
        typer.echo(result.stdout.strip())
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)


@configure_app.command("icloud")
def configure_icloud(
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
    apple_id: Annotated[str | None, typer.Option("--apple-id")] = None,
    daily_at: Annotated[str | None, typer.Option("--daily-at")] = None,
    capacity_relief: Annotated[bool, typer.Option("--capacity-relief")] = False,
    read_only: Annotated[bool, typer.Option("--read-only")] = False,
    defer_auth: Annotated[bool, typer.Option("--defer-auth")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    manager = _manager()
    try:
        if capacity_relief and read_only:
            raise ValueError("choose either --capacity-relief or --read-only")
        current = manager.config()
        if enabled is None:
            icloud = _prompt_icloud(current.icloud)
        elif not enabled:
            icloud = ICloudConfig(enabled=False)
        else:
            icloud = ICloudConfig(
                enabled=True,
                apple_id=apple_id or current.icloud.apple_id,
                daily_at=daily_at or current.icloud.daily_at,
                capacity_relief_enabled=(
                    True
                    if capacity_relief
                    else False
                    if read_only
                    else current.icloud.capacity_relief_enabled
                ),
                cleanup_trigger_percent=current.icloud.cleanup_trigger_percent,
                cleanup_target_percent=current.icloud.cleanup_target_percent,
                cleanup_quarantine_days=current.icloud.cleanup_quarantine_days,
                cleanup_max_assets=current.icloud.cleanup_max_assets,
                cleanup_max_quota_percent=current.icloud.cleanup_max_quota_percent,
            )
        _save(manager, current.model_copy(update={"icloud": icloud}), yes=yes)
        if icloud.enabled and not defer_auth:
            manager.icloud_auth()
            manager.icloud_preview()
            typer.echo("Preview complete. Run `sudo mnema icloud sync` to start first archive.")
    except (OSError, ValueError, ValidationError, RuntimeError, PermissionError) as error:
        _fail(error)


@icloud_app.command("auth")
def icloud_auth() -> None:
    try:
        _manager().icloud_auth()
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)


@icloud_app.command("preview")
def icloud_preview() -> None:
    try:
        _manager().icloud_preview()
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)


@icloud_app.command("sync")
def icloud_sync(
    scheduled: Annotated[bool, typer.Option("--scheduled", hidden=True)] = False,
) -> None:
    try:
        _manager().icloud_sync(scheduled=scheduled)
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)


@icloud_app.command("status")
def icloud_status() -> None:
    try:
        _manager().icloud_status()
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)


@icloud_app.command("storage")
def icloud_storage() -> None:
    try:
        _manager().icloud_storage()
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)


@icloud_cleanup_app.command("preview")
def icloud_cleanup_preview() -> None:
    try:
        _manager().icloud_cleanup_preview()
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)


@icloud_cleanup_app.command("status")
def icloud_cleanup_status() -> None:
    try:
        _manager().icloud_cleanup_status()
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)


@icloud_cleanup_app.command("approve")
def icloud_cleanup_approve(manifest_id: int) -> None:
    manager = _manager()
    try:
        payload = manager.icloud_cleanup_manifest(manifest_id)
        digest = str(payload["digest"])
        typer.echo(json.dumps(payload, indent=2))
        confirmation = typer.prompt("Type manifest digest prefix to approve")
        if confirmation != digest[:12]:
            raise ValueError("manifest digest confirmation does not match")
        manager.icloud_cleanup_approve(manifest_id, digest)
    except (OSError, ValueError, RuntimeError, PermissionError, KeyError) as error:
        _fail(error)


@configure_app.command("policy")
def configure_policy(
    archive_after_days: Annotated[int | None, typer.Option()] = None,
    stability_window_hours: Annotated[int | None, typer.Option()] = None,
    quarantine_days: Annotated[int | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    manager = _manager()
    try:
        current = manager.config()
        policy = PolicyConfig(
            archive_after_days=archive_after_days
            if archive_after_days is not None
            else typer.prompt(
                "Archive after days",
                default=current.policy.archive_after_days,
                type=int,
            ),
            stability_window_hours=stability_window_hours
            if stability_window_hours is not None
            else typer.prompt(
                "Stability window hours",
                default=current.policy.stability_window_hours,
                type=int,
            ),
            quarantine_days=quarantine_days
            if quarantine_days is not None
            else typer.prompt(
                "Quarantine days",
                default=current.policy.quarantine_days,
                type=int,
            ),
            dry_run=True,
            manual_approval=True,
        )
        _save(manager, current.model_copy(update={"policy": policy}), yes=yes)
    except (OSError, ValueError, ValidationError, PermissionError) as error:
        _fail(error)


@config_app.command("show")
def config_show() -> None:
    try:
        typer.echo(yaml_redacted(_manager().config()))
    except (OSError, ValueError, ValidationError) as error:
        _fail(error)


@config_app.command("validate")
def config_validate() -> None:
    manager = _manager()
    try:
        config = manager.config()
        manager.validate_host(config)
    except (OSError, ValueError, ValidationError) as error:
        _fail(error)
    typer.echo("Configuration and storage validation passed.")


@config_app.command("diff")
def config_diff() -> None:
    manager = _manager()
    try:
        desired = render_environment(manager.config()).splitlines(keepends=True)
        current = (
            manager.paths.environment.read_text(encoding="utf-8").splitlines(keepends=True)
            if manager.paths.environment.is_file()
            else []
        )
    except (OSError, ValueError, ValidationError) as error:
        _fail(error)
    typer.echo(
        "".join(
            difflib.unified_diff(
                current,
                desired,
                fromfile=str(manager.paths.environment),
                tofile="desired",
            )
        )
        or "No configuration drift."
    )


@config_app.command("apply")
def config_apply(
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    manager = _manager()
    try:
        _save(manager, manager.config(), yes=yes)
    except (OSError, ValueError, ValidationError, PermissionError) as error:
        _fail(error)


@backup_app.command("create")
def backup_create(destination: Path) -> None:
    try:
        _manager().backup(destination)
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)
    typer.echo(f"Configuration backup created: {destination}. It contains secrets.")


@backup_app.command("list")
def backup_list(directory: Path = Path("/srv/mnema-backup")) -> None:
    if not directory.is_dir():
        _fail(ValueError("backup directory does not exist"))
    for candidate in sorted(directory.glob("mnema-config-*.tar.gz")):
        typer.echo(candidate)


@restore_app.command("config")
def restore_config(source: Path) -> None:
    if not typer.confirm(
        "Stop Mnema and restore configuration/database from this backup?",
        abort=False,
    ):
        typer.echo("No changes made.")
        raise typer.Exit()
    try:
        _manager().restore_config(source)
    except (OSError, ValueError, RuntimeError, PermissionError) as error:
        _fail(error)
    typer.echo("Configuration restored and service health recovery started.")


@uninstall_app.callback()
def uninstall(
    ctx: typer.Context,
    execute: Annotated[bool, typer.Option("--execute")] = False,
    confirm: Annotated[str | None, typer.Option("--confirm")] = None,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    manager = _manager()
    for item in manager.uninstall_plan():
        typer.echo(f"- {item}")
    if not execute:
        typer.echo("Plan only. No changes made.")
        return
    if confirm != socket.gethostname():
        _fail(ValueError("--confirm must equal this host name"))
    try:
        manager.uninstall_runtime()
    except (OSError, RuntimeError, PermissionError) as error:
        _fail(error)
    typer.echo("Mnema runtime disabled. Data, configuration, secrets, and archives retained.")


def register_admin_commands(app: typer.Typer) -> None:
    app.add_typer(configure_app, name="configure")
    app.add_typer(config_app, name="config")
    app.add_typer(backup_app, name="backup")
    app.add_typer(restore_app, name="restore")
    app.add_typer(uninstall_app, name="uninstall")
    app.add_typer(icloud_app, name="icloud")
    app.add_typer(file_provider_app, name="file-provider")

    @app.command("install")
    def install(
        source_root: Annotated[Path | None, typer.Option()] = None,
        config_file: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        manager = _manager()
        try:
            source_root = source_root or Path.cwd()
            config = load_config(config_file) if config_file else manager.config()
            if config_file is None and not manager.paths.config.is_file():
                config = config.model_copy(update={"storage": _prompt_storage(config.storage)})
            manager.install_from_source(source_root.resolve(), config)
        except (OSError, ValueError, RuntimeError, PermissionError) as error:
            _fail(error)
        typer.echo("Mnema installed. Deletion remains disabled and safety lock remains enabled.")

    @app.command("start")
    def start() -> None:
        try:
            _manager().start()
        except (OSError, RuntimeError, PermissionError) as error:
            _fail(error)

    @app.command("startup")
    def startup() -> None:
        """Enable Mnema at boot and start it now."""
        try:
            manager = _manager()
            manager.enable()
            manager.start()
        except (OSError, RuntimeError, PermissionError) as error:
            _fail(error)

    @app.command("stop")
    def stop() -> None:
        try:
            _manager().stop()
        except (OSError, RuntimeError, PermissionError) as error:
            _fail(error)

    @app.command("restart")
    def restart() -> None:
        try:
            _manager().restart()
        except (OSError, RuntimeError, PermissionError) as error:
            _fail(error)

    @app.command("enable")
    def enable() -> None:
        try:
            _manager().enable()
        except (OSError, RuntimeError, PermissionError) as error:
            _fail(error)

    @app.command("disable")
    def disable() -> None:
        try:
            _manager().disable()
        except (OSError, RuntimeError, PermissionError) as error:
            _fail(error)

    @app.command("appliance-status")
    def appliance_status() -> None:
        try:
            _manager().status()
        except OSError as error:
            _fail(error)

    @app.command("urls")
    def urls(
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Emit machine-readable JSON."),
        ] = False,
    ) -> None:
        """Show reachable service endpoints and runtime state."""
        try:
            report = _manager().service_endpoints()
        except (OSError, ValueError, RuntimeError, PermissionError) as error:
            _fail(error)
        if as_json:
            typer.echo(json.dumps(report.as_dict(), indent=2))
            return
        typer.echo(_render_endpoint_report(report))

    @app.command("logs")
    def logs(
        follow: Annotated[bool, typer.Option("--follow", "-f")] = False,
        lines: Annotated[int, typer.Option(min=1, max=10000)] = 200,
    ) -> None:
        try:
            _manager().logs(follow=follow, lines=lines)
        except (OSError, RuntimeError) as error:
            _fail(error)

    @app.command("update")
    def update(
        archive: Annotated[Path | None, typer.Option()] = None,
        sha256: Annotated[str | None, typer.Option("--sha256")] = None,
        latest: Annotated[bool, typer.Option("--latest")] = False,
        repository: Annotated[str, typer.Option("--repository")] = "jparadasb/mnema",
        yes: Annotated[bool, typer.Option("--yes")] = False,
    ) -> None:
        if latest == (archive is not None):
            _fail(ValueError("choose --latest or --archive with --sha256"))
        if archive is not None and sha256 is None:
            _fail(ValueError("--archive requires --sha256"))
        if latest and sha256 is not None:
            _fail(ValueError("--sha256 cannot be combined with --latest"))
        if not yes and not typer.confirm(
            "Back up configuration, stop Mnema, install verified release, and health-check?"
        ):
            typer.echo("No changes made.")
            raise typer.Exit()
        try:
            manager = _manager()
            if latest:
                tag, release = manager.update_from_latest_release(repository)
                typer.echo(f"GitHub release {tag} resolved and verified.")
            else:
                assert archive is not None and sha256 is not None
                release = manager.update_from_archive(archive.resolve(), sha256)
        except (OSError, ValueError, RuntimeError, PermissionError) as error:
            _fail(error)
        typer.echo(f"Release {release} installed. Rollback metadata retained.")

    @app.command("rollback")
    def rollback() -> None:
        if not typer.confirm("Stop Mnema and restore the previous application release?"):
            typer.echo("No changes made.")
            raise typer.Exit()
        try:
            release = _manager().rollback()
        except (OSError, ValueError, RuntimeError, PermissionError) as error:
            _fail(error)
        typer.echo(f"Rolled back release {release}. Configuration and archives retained.")
