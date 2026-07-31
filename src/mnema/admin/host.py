from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from mnema.admin.config import (
    ApplianceConfig,
    atomic_copy,
    atomic_write,
    dump_config,
    load_config,
    render_environment,
)


@dataclass(frozen=True)
class AppliancePaths:
    config: Path = Path("/etc/mnema/config.yaml")
    secrets: Path = Path("/etc/mnema/secrets")
    install_root: Path = Path("/opt/mnema")
    data_root: Path = Path("/var/lib/mnema")
    service: Path = Path("/etc/systemd/system/mnema.service")
    lock: Path = Path("/run/lock/mnema-admin.lock")

    @property
    def environment(self) -> Path:
        return self.install_root / ".env"

    @property
    def compose(self) -> Path:
        return self.install_root / "compose.yaml"

    @property
    def icloud_service(self) -> Path:
        return self.service.with_name("mnema-icloud.service")

    @property
    def icloud_timer(self) -> Path:
        return self.service.with_name("mnema-icloud.timer")

    @property
    def icloud_schedule_armed(self) -> Path:
        return self.data_root / "icloud-schedule-armed"

    @property
    def icloud_session(self) -> Path:
        if self.service == Path("/etc/systemd/system/mnema.service"):
            return Path("/etc/mnema/icloud-session")
        return self.config.parent / "icloud-session"


EndpointScope = Literal["internet", "lan", "localhost"]
EndpointState = Literal["running", "stopped", "unknown"]


@dataclass(frozen=True)
class ServiceEndpoint:
    service: str
    url: str
    scope: EndpointScope
    state: EndpointState

    def as_dict(self) -> dict[str, str]:
        return {
            "service": self.service,
            "url": self.url,
            "scope": self.scope,
            "state": self.state,
        }


@dataclass(frozen=True)
class ServiceEndpointReport:
    endpoints: tuple[ServiceEndpoint, ...]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "services": [endpoint.as_dict() for endpoint in self.endpoints],
            "warnings": list(self.warnings),
        }


class CommandRunner:
    def run(
        self,
        arguments: Sequence[str],
        *,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - arguments are arrays from trusted code
            list(arguments),
            check=check,
            capture_output=capture_output,
            text=True,
        )


class ApplianceManager:
    def __init__(
        self,
        paths: AppliancePaths | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self.paths = paths or AppliancePaths()
        self.runner = runner or CommandRunner()

    @staticmethod
    def require_root() -> None:
        if os.geteuid() != 0:
            raise PermissionError("this command must run as root")

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.paths.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.lock.open("a", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("another Mnema administration command is running") from error
            yield

    def config(self) -> ApplianceConfig:
        return load_config(self.paths.config)

    def validate_host(self, config: ApplianceConfig, *, require_mounts: bool = True) -> None:
        if config.storage.active_root.is_symlink() or config.storage.backup_root.is_symlink():
            raise ValueError("storage roots cannot be symlinks")
        if not require_mounts:
            return
        for path in (config.storage.active_root, config.storage.backup_root):
            if not path.is_dir():
                raise ValueError(f"storage root does not exist: {path}")
            self.runner.run(["mountpoint", "--quiet", str(path)])
        active = self.runner.run(
            ["findmnt", "-n", "-o", "UUID", "--target", str(config.storage.active_root)],
            capture_output=True,
        ).stdout.strip()
        backup = self.runner.run(
            ["findmnt", "-n", "-o", "UUID", "--target", str(config.storage.backup_root)],
            capture_output=True,
        ).stdout.strip()
        if not active or not backup or active == backup:
            raise ValueError("active and backup storage UUIDs must exist and differ")

    def save_config(
        self,
        config: ApplianceConfig,
        *,
        secret_updates: dict[Path, str] | None = None,
    ) -> None:
        self.require_root()
        with self.locked():
            self.validate_host(config)
            previous_appliance = (
                load_config(self.paths.config) if self.paths.config.is_file() else None
            )
            previous_config = (
                self.paths.config.read_bytes() if self.paths.config.is_file() else None
            )
            previous_environment = (
                self.paths.environment.read_bytes() if self.paths.environment.is_file() else None
            )
            previous_icloud_service = (
                self.paths.icloud_service.read_bytes()
                if self.paths.icloud_service.is_file()
                else None
            )
            previous_icloud_timer = (
                self.paths.icloud_timer.read_bytes() if self.paths.icloud_timer.is_file() else None
            )
            secret_updates = secret_updates or {}
            previous_secrets: dict[Path, bytes | None] = {}
            for path in secret_updates:
                if path.parent != self.paths.secrets:
                    raise ValueError("secret updates must remain beneath Mnema secret root")
                previous_secrets[path] = path.read_bytes() if path.is_file() else None
            try:
                atomic_write(self.paths.config, dump_config(config), mode=0o640)
                atomic_write(self.paths.environment, render_environment(config), mode=0o600)
                self._write_icloud_units(config)
                for path, secret_content in secret_updates.items():
                    atomic_write(path, secret_content, mode=0o640)
                self.runner.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(self.paths.compose),
                        "config",
                        "--quiet",
                    ]
                )
                self._reconcile_icloud_timer(config)
                if self.is_active():
                    self.restart()
                    self.runner.run(["curl", "--fail", "--silent", "http://127.0.0.1:8080/healthz"])
                    self.verify_services(config)
                    if (
                        previous_appliance is None
                        or previous_appliance.cold_storage != config.cold_storage
                    ):
                        self.runtime_command("cold-storage-check")
                    self.apply_runtime_policy(config)
            except Exception:
                self._restore_file(self.paths.config, previous_config, 0o640)
                self._restore_file(self.paths.environment, previous_environment, 0o600)
                self._restore_file(self.paths.icloud_service, previous_icloud_service, 0o644)
                self._restore_file(self.paths.icloud_timer, previous_icloud_timer, 0o644)
                self.runner.run(["systemctl", "daemon-reload"], check=False)
                for path, previous_secret in previous_secrets.items():
                    self._restore_file(path, previous_secret, 0o640)
                if self.is_active():
                    self.restart()
                    if self.paths.config.is_file():
                        self.apply_runtime_policy(self.config())
                raise

    def _write_icloud_units(self, config: ApplianceConfig) -> None:
        atomic_write(
            self.paths.icloud_service,
            "\n".join(
                (
                    "[Unit]",
                    "Description=Mnema read-only iCloud Photos import",
                    "After=docker.service mnema.service network-online.target",
                    "Requires=docker.service",
                    "",
                    "[Service]",
                    "Type=oneshot",
                    "ExecStart=/usr/local/bin/mnema icloud sync --scheduled",
                    "NoNewPrivileges=true",
                    "PrivateTmp=true",
                    "ProtectHome=true",
                    "",
                )
            ),
            mode=0o644,
        )
        atomic_write(
            self.paths.icloud_timer,
            "\n".join(
                (
                    "[Unit]",
                    "Description=Run Mnema iCloud Photos import daily",
                    "",
                    "[Timer]",
                    f"OnCalendar=*-*-* {config.icloud.daily_at}:00",
                    "Persistent=true",
                    "RandomizedDelaySec=5min",
                    "Unit=mnema-icloud.service",
                    "",
                    "[Install]",
                    "WantedBy=timers.target",
                    "",
                )
            ),
            mode=0o644,
        )

    def _reconcile_icloud_timer(self, config: ApplianceConfig) -> None:
        self.runner.run(["systemctl", "daemon-reload"])
        if config.icloud.enabled and self.paths.icloud_schedule_armed.is_file():
            self.runner.run(["systemctl", "enable", "--now", "mnema-icloud.timer"])
        else:
            self.runner.run(
                ["systemctl", "disable", "--now", "mnema-icloud.timer"],
                check=False,
            )

    @staticmethod
    def _restore_file(path: Path, content: bytes | None, mode: int) -> None:
        if content is None:
            path.unlink(missing_ok=True)
            return
        atomic_write(path, content.decode(), mode=mode)

    def is_active(self) -> bool:
        result = self.runner.run(
            ["systemctl", "is-active", "--quiet", "mnema.service"],
            check=False,
        )
        return result.returncode == 0

    def start(self) -> None:
        self.require_root()
        self.runner.run(["systemctl", "start", "mnema.service"])
        try:
            if self.paths.config.is_file():
                config = self.config()
                self.runner.run(["curl", "--fail", "--silent", "http://127.0.0.1:8080/healthz"])
                self.verify_services(config)
                self.runtime_command("cold-storage-check")
                self.apply_runtime_policy(config)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        self.require_root()
        self.runner.run(["systemctl", "stop", "mnema-icloud.service"], check=False)
        self.runner.run(["systemctl", "stop", "mnema.service"])

    def restart(self) -> None:
        self.require_root()
        self.runner.run(["systemctl", "restart", "mnema.service"])

    def enable(self) -> None:
        self.require_root()
        self.runner.run(["systemctl", "enable", "mnema.service", "mnema-smart.timer"])

    def disable(self) -> None:
        self.require_root()
        self.runner.run(["systemctl", "disable", "mnema.service", "mnema-smart.timer"])
        self.runner.run(["systemctl", "disable", "mnema-icloud.timer"], check=False)

    def status(self) -> None:
        self.runner.run(
            ["systemctl", "--no-pager", "--full", "status", "mnema.service"],
            check=False,
        )
        self.runner.run(
            ["docker", "compose", "-f", str(self.paths.compose), "ps"],
            check=False,
        )

    def service_endpoints(self) -> ServiceEndpointReport:
        config = self.config()
        running_services = self._running_compose_services()
        lan_addresses = self._default_route_ipv4_addresses()
        endpoints: list[ServiceEndpoint] = []
        warnings: list[str] = []

        if config.cloudflare.enabled:
            endpoints.append(
                ServiceEndpoint(
                    service="mnema-web",
                    url=f"https://{config.cloudflare.admin_hostname}",
                    scope="internet",
                    state=self._service_state(
                        running_services,
                        required={"web-public", "cloudflared"},
                    ),
                )
            )
            endpoints.append(
                ServiceEndpoint(
                    service="mnema-recovery-web",
                    url="http://127.0.0.1:8080",
                    scope="localhost",
                    state=self._service_state(running_services, required={"web"}),
                )
            )
        else:
            self._append_bound_endpoints(
                endpoints=endpoints,
                warnings=warnings,
                service="mnema-web",
                scheme="http",
                username=None,
                bind_address=config.service.local_bind_address,
                port=8080,
                lan_addresses=lan_addresses,
                state=self._service_state(running_services, required={"web"}),
            )
            if config.service.local_bind_address == "127.0.0.1":
                warnings.append("Web is localhost-only. Use: ssh -L 8080:127.0.0.1:8080 USER@HOST")

        self._append_bound_endpoints(
            endpoints=endpoints,
            warnings=warnings,
            service="sftp",
            scheme="sftp",
            username=config.sftpgo.username,
            bind_address=config.sftpgo.bind_address,
            port=2022,
            lan_addresses=lan_addresses,
            state=self._service_state(running_services, required={"sftpgo"}),
        )
        return ServiceEndpointReport(
            endpoints=tuple(endpoints),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _running_compose_services(self) -> set[str] | None:
        try:
            result = self.runner.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(self.paths.compose),
                    "ps",
                    "--status",
                    "running",
                    "--services",
                ],
                check=False,
                capture_output=True,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def _default_route_ipv4_addresses(self) -> tuple[str, ...]:
        try:
            routes = self.runner.run(
                ["ip", "-json", "-4", "route", "show", "default"],
                check=False,
                capture_output=True,
            )
        except OSError:
            return ()
        if routes.returncode != 0:
            return ()
        try:
            route_payload = json.loads(routes.stdout)
        except (json.JSONDecodeError, TypeError):
            return ()
        interfaces = {
            route["dev"]
            for route in route_payload
            if isinstance(route, dict) and isinstance(route.get("dev"), str)
        }
        if not interfaces:
            return ()
        try:
            addresses = self.runner.run(
                ["ip", "-json", "-4", "address", "show", "up"],
                check=False,
                capture_output=True,
            )
        except OSError:
            return ()
        if addresses.returncode != 0:
            return ()
        try:
            address_payload = json.loads(addresses.stdout)
        except (json.JSONDecodeError, TypeError):
            return ()
        discovered: list[str] = []
        for interface in address_payload:
            if not isinstance(interface, dict) or interface.get("ifname") not in interfaces:
                continue
            for candidate in interface.get("addr_info", []):
                if not isinstance(candidate, dict) or candidate.get("family") != "inet":
                    continue
                local = candidate.get("local")
                if not isinstance(local, str):
                    continue
                try:
                    parsed = ip_address(local)
                except ValueError:
                    continue
                if parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified:
                    continue
                discovered.append(local)
        return tuple(sorted(set(discovered), key=lambda value: int(ip_address(value))))

    @staticmethod
    def _service_state(
        running_services: set[str] | None,
        *,
        required: set[str],
    ) -> EndpointState:
        if running_services is None:
            return "unknown"
        return "running" if required <= running_services else "stopped"

    @staticmethod
    def _append_bound_endpoints(
        *,
        endpoints: list[ServiceEndpoint],
        warnings: list[str],
        service: str,
        scheme: str,
        username: str | None,
        bind_address: str,
        port: int,
        lan_addresses: tuple[str, ...],
        state: EndpointState,
    ) -> None:
        if bind_address == "0.0.0.0":  # noqa: S104 - reporting an existing bind
            addresses = lan_addresses
            scope: EndpointScope = "lan"
            if not addresses:
                warnings.append(f"{service}: no usable LAN IPv4 address detected")
                return
        else:
            addresses = (bind_address,)
            scope = "localhost" if ip_address(bind_address).is_loopback else "lan"
        userinfo = f"{quote(username, safe='')}@" if username else ""
        endpoints.extend(
            ServiceEndpoint(
                service=service,
                url=f"{scheme}://{userinfo}{address}:{port}",
                scope=scope,
                state=state,
            )
            for address in addresses
        )

    def runtime_command(self, command: str) -> None:
        self.runner.run(["docker", "exec", "mnema-web-1", "mnema", command])

    def icloud_runtime_command(
        self,
        command: str,
        *,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return self.runner.run(
            [
                "docker",
                "compose",
                "-f",
                str(self.paths.compose),
                "run",
                "--rm",
                "icloud-runner",
                command,
            ],
            capture_output=capture_output,
        )

    def icloud_auth(self) -> None:
        self.require_root()
        config = self.config()
        if not config.icloud.enabled:
            raise ValueError("iCloud Photos is disabled")
        session = self.paths.icloud_session
        session.mkdir(parents=True, exist_ok=True)
        os.chown(session, 10001, 10001)
        os.chmod(session, 0o700)
        self.icloud_runtime_command("icloud-auth-internal")
        self._secure_icloud_session(session)

    @staticmethod
    def _secure_icloud_session(session: Path) -> None:
        for candidate in session.rglob("*"):
            if candidate.is_symlink():
                raise RuntimeError("iCloud session contains an unsafe link")
            if candidate.is_dir():
                os.chmod(candidate, 0o700)
            elif candidate.is_file():
                os.chmod(candidate, 0o600)
            else:
                raise RuntimeError("iCloud session contains an unsafe entry")
            os.chown(candidate, 10001, 10001)

    def icloud_preview(self) -> None:
        self.require_root()
        config = self.config()
        if not config.icloud.enabled:
            raise ValueError("iCloud Photos is disabled")
        if not self.paths.icloud_session.is_dir():
            raise RuntimeError("iCloud authentication is required")
        self.icloud_runtime_command("icloud-preview-internal")

    def icloud_sync(self, *, scheduled: bool) -> None:
        self.require_root()
        config = self.config()
        if not config.icloud.enabled:
            raise ValueError("iCloud Photos is disabled")
        if not self.paths.icloud_session.is_dir():
            raise RuntimeError("iCloud authentication is required")
        with self.locked():
            self.icloud_runtime_command("icloud-sync-internal")
            if not scheduled and not self.paths.icloud_schedule_armed.is_file():
                atomic_write(self.paths.icloud_schedule_armed, "armed\n", mode=0o600)
                self._reconcile_icloud_timer(config)

    def icloud_status(self) -> None:
        self.require_root()
        config = self.config()
        if not config.icloud.enabled or not self.paths.icloud_session.is_dir():
            print(
                json.dumps(
                    {
                        "enabled": config.icloud.enabled,
                        "authenticated": False,
                        "reauthentication_required": config.icloud.enabled,
                        "items": 0,
                        "last_result": "never",
                    },
                    indent=2,
                )
            )
            return
        typer_status = self.icloud_runtime_command(
            "icloud-status-internal",
            capture_output=True,
        )
        print(typer_status.stdout, end="")
        self.runner.run(
            ["systemctl", "--no-pager", "list-timers", "mnema-icloud.timer"],
            check=False,
        )

    def apply_runtime_policy(self, config: ApplianceConfig) -> None:
        self.runner.run(
            [
                "docker",
                "exec",
                "mnema-web-1",
                "mnema",
                "apply-policy",
                "--archive-after-days",
                str(config.policy.archive_after_days),
                "--stability-window-hours",
                str(config.policy.stability_window_hours),
                "--quarantine-days",
                str(config.policy.quarantine_days),
            ]
        )

    def verify_services(self, config: ApplianceConfig) -> None:
        result = self.runner.run(
            [
                "docker",
                "compose",
                "-f",
                str(self.paths.compose),
                "ps",
                "--status",
                "running",
                "--services",
            ],
            capture_output=True,
        )
        running = set(result.stdout.splitlines())
        required = {"web", "worker", "sftpgo"}
        if config.cold_storage.integration_minio:
            required.add("minio")
        if config.cloudflare.enabled:
            required.update({"web-public", "cloudflared"})
        missing = required - running
        if missing:
            raise RuntimeError(f"required services are not running: {', '.join(sorted(missing))}")

    def logs(self, *, follow: bool, lines: int) -> None:
        arguments = [
            "docker",
            "compose",
            "-f",
            str(self.paths.compose),
            "logs",
            "--tail",
            str(lines),
        ]
        if follow:
            arguments.append("--follow")
        self.runner.run(arguments)

    def backup(self, destination: Path) -> None:
        self.require_root()
        if not destination.is_absolute():
            raise ValueError("backup destination must be absolute")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="mnema-config-backup-") as directory:
            database_source = self.paths.data_root / "mnema.db"
            database_backup = Path(directory) / "mnema.db"
            if database_source.is_file():
                with (
                    sqlite3.connect(database_source) as source_connection,
                    sqlite3.connect(database_backup) as backup_connection,
                ):
                    source_connection.backup(backup_connection)
                if not self._sqlite_healthy(database_backup):
                    raise RuntimeError("configuration backup SQLite integrity failed")
            with tarfile.open(destination, "x:gz") as archive:
                for source, name in (
                    (self.paths.config, "etc/mnema/config.yaml"),
                    (self.paths.secrets, "etc/mnema/secrets"),
                    (self.paths.icloud_session, "etc/mnema/icloud-session"),
                    (self.paths.environment, "opt/mnema/.env"),
                    (database_backup, "var/lib/mnema/mnema.db"),
                ):
                    if source.exists():
                        archive.add(source, arcname=name, recursive=True)
        os.chmod(destination, 0o600)

    def restore_config(self, source: Path) -> None:
        self.require_root()
        if not source.is_file():
            raise ValueError("configuration backup does not exist")
        was_active = self.is_active()
        if was_active:
            self.stop()
        with self.locked(), tempfile.TemporaryDirectory(prefix="mnema-restore-") as directory:
            root = Path(directory)
            with tarfile.open(source, "r:gz") as archive:
                self._safe_extract(archive, root)
            database = root / "var/lib/mnema/mnema.db"
            if database.is_file() and not self._sqlite_healthy(database):
                raise RuntimeError("configuration backup SQLite integrity failed")
            restored_config = root / "etc/mnema/config.yaml"
            restored_environment = root / "opt/mnema/.env"
            if not restored_config.is_file() or not restored_environment.is_file():
                raise RuntimeError("configuration backup is incomplete")
            atomic_write(
                self.paths.config,
                restored_config.read_text(encoding="utf-8"),
                mode=0o640,
            )
            atomic_write(
                self.paths.environment,
                restored_environment.read_text(encoding="utf-8"),
                mode=0o600,
            )
            restored_secrets = root / "etc/mnema/secrets"
            if restored_secrets.is_dir():
                for secret in restored_secrets.iterdir():
                    if not secret.is_file() or secret.is_symlink():
                        raise RuntimeError("configuration backup contains an unsafe secret entry")
                    atomic_copy(
                        secret,
                        self.paths.secrets / secret.name,
                        mode=0o640,
                    )
            restored_icloud_session = root / "etc/mnema/icloud-session"
            if restored_icloud_session.is_dir():
                self._restore_directory(
                    restored_icloud_session,
                    self.paths.icloud_session,
                )
            if database.is_file():
                atomic_copy(
                    database,
                    self.paths.data_root / "mnema.db",
                    mode=0o640,
                )
        if was_active:
            self.start()

    @staticmethod
    def _restore_directory(source: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        os.chmod(destination, 0o700)
        for candidate in source.rglob("*"):
            relative = candidate.relative_to(source)
            target = destination / relative
            if candidate.is_symlink():
                raise RuntimeError("configuration backup contains an unsafe session link")
            if candidate.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, 0o700)
            elif candidate.is_file():
                atomic_copy(candidate, target, mode=0o600)
            else:
                raise RuntimeError("configuration backup contains an unsafe session entry")

    @staticmethod
    def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise ValueError("backup archive contains an unsafe path")
            if member.issym() or member.islnk():
                raise ValueError("backup archive contains a link")
        archive.extractall(destination, filter="data")

    def install_from_source(self, source_root: Path, config: ApplianceConfig) -> None:
        self.require_root()
        installer = source_root / "scripts/install.sh"
        if not installer.is_file():
            raise ValueError("Mnema source root does not contain scripts/install.sh")
        self.validate_host(config)
        environment = os.environ.copy()
        environment.update(
            {
                "MNEMA_ACTIVE_ROOT": str(config.storage.active_root),
                "MNEMA_BACKUP_ROOT": str(config.storage.backup_root),
                "MNEMA_SOURCE_ROOT": str(config.storage.source_root),
                "MNEMA_SFTPGO_USER": config.sftpgo.username,
            }
        )
        subprocess.run(  # noqa: S603 - fixed audited installer path and argument array
            [str(installer)],
            check=True,
            cwd=source_root,
            env=environment,
        )
        atomic_write(self.paths.config, dump_config(config), mode=0o640)
        atomic_write(self.paths.environment, render_environment(config), mode=0o600)
        self._write_icloud_units(config)
        self._reconcile_icloud_timer(config)
        self.restart()
        self.runner.run(["curl", "--fail", "--silent", "http://127.0.0.1:8080/healthz"])
        self.verify_services(config)
        self.runtime_command("cold-storage-check")
        self.apply_runtime_policy(config)
        if config.service.start_at_boot:
            self.enable()
        else:
            self.disable()

    def uninstall_plan(self) -> tuple[str, ...]:
        return (
            "stop and disable Mnema, SMART, and iCloud timer units",
            "retain /opt/mnema application files",
            "retain /var/lib/mnema database and service state",
            "retain /etc/mnema secrets",
            "retain both archive storage filesystems",
        )

    def uninstall_runtime(self) -> None:
        self.require_root()
        if self.is_active():
            self.stop()
        self.disable()

    def update_from_archive(self, archive: Path, expected_sha256: str) -> str:
        self.require_root()
        if not archive.is_file() or not archive.is_absolute():
            raise ValueError("release archive must be an existing absolute path")
        actual_sha256 = self._sha256(archive)
        if actual_sha256 != expected_sha256.lower():
            raise ValueError("release archive SHA-256 does not match")
        release = actual_sha256[:12]
        previous = self.paths.install_root.with_name(f"mnema.previous-{release}")
        if previous.exists():
            raise ValueError(f"rollback directory already exists: {previous}")
        was_active = self.is_active()
        with (
            self.locked(),
            tempfile.TemporaryDirectory(
                prefix=".mnema-release-",
                dir=self.paths.install_root.parent,
            ) as directory,
        ):
            staged = Path(directory)
            with tarfile.open(archive, "r:gz") as release_archive:
                self._safe_extract(release_archive, staged)
            for required in ("Dockerfile", "compose.yaml", "pyproject.toml", "src"):
                if not (staged / required).exists():
                    raise RuntimeError(f"release archive is missing {required}")
            if self.paths.environment.is_file():
                shutil.copy2(self.paths.environment, staged / ".env")
            secrets_link = staged / "secrets"
            if not secrets_link.exists():
                secrets_link.symlink_to(self.paths.secrets)
            self.runner.run(
                ["docker", "compose", "-f", str(staged / "compose.yaml"), "config", "--quiet"]
            )
            self.runner.run(["docker", "tag", "mnema:0.1.0", f"mnema:rollback-{release}"])
            self.runner.run(["docker", "build", "--tag", f"mnema:release-{release}", str(staged)])
            self.runner.run(["docker", "tag", f"mnema:release-{release}", "mnema:0.1.0"])
            if was_active:
                self.stop()
            backup = self.paths.data_root / "backups" / f"mnema-config-pre-{release}.tar.gz"
            backup.parent.mkdir(parents=True, exist_ok=True)
            self.backup(backup)
            os.replace(self.paths.install_root, previous)
            os.replace(staged, self.paths.install_root)
            try:
                if was_active:
                    self.start()
                    self.runner.run(["curl", "--fail", "--silent", "http://127.0.0.1:8080/healthz"])
            except Exception:
                failed = self.paths.install_root.with_name(f"mnema.failed-{release}")
                os.replace(self.paths.install_root, failed)
                os.replace(previous, self.paths.install_root)
                self.runner.run(["docker", "tag", f"mnema:rollback-{release}", "mnema:0.1.0"])
                if was_active:
                    self.start()
                raise
            metadata = {
                "release": release,
                "previous": str(previous),
                "rollback_image": f"mnema:rollback-{release}",
            }
            atomic_write(
                self.paths.data_root / "previous-release.json",
                json.dumps(metadata, sort_keys=True) + "\n",
                mode=0o600,
            )
        return release

    def rollback(self) -> str:
        self.require_root()
        metadata_path = self.paths.data_root / "previous-release.json"
        if not metadata_path.is_file():
            raise RuntimeError("no previous release metadata exists")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise RuntimeError("previous release metadata is invalid")
        previous = Path(str(metadata.get("previous", "")))
        rollback_image = str(metadata.get("rollback_image", ""))
        release = str(metadata.get("release", "unknown"))
        if (
            previous.parent != self.paths.install_root.parent
            or not previous.name.startswith("mnema.previous-")
            or not previous.is_dir()
            or not rollback_image.startswith("mnema:rollback-")
        ):
            raise RuntimeError("previous release metadata contains unsafe values")
        failed = self.paths.install_root.with_name(f"mnema.rolled-back-{release}")
        if failed.exists():
            raise RuntimeError(f"rollback target already exists: {failed}")
        was_active = self.is_active()
        with self.locked():
            if was_active:
                self.stop()
            os.replace(self.paths.install_root, failed)
            os.replace(previous, self.paths.install_root)
            self.runner.run(["docker", "tag", rollback_image, "mnema:0.1.0"])
            try:
                if was_active:
                    self.start()
                    self.runner.run(["curl", "--fail", "--silent", "http://127.0.0.1:8080/healthz"])
            except Exception:
                os.replace(self.paths.install_root, previous)
                os.replace(failed, self.paths.install_root)
                if was_active:
                    self.start()
                raise
            metadata_path.unlink()
        return release

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _sqlite_healthy(path: Path) -> bool:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        return bool(row == ("ok",))
