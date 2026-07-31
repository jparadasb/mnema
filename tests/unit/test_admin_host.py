import hashlib
import json
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

from mnema.admin.config import (
    ApplianceConfig,
    CloudflareConfig,
    ICloudConfig,
    ServiceConfig,
    SFTPGoConfig,
    dump_config,
    render_environment,
)
from mnema.admin.host import ApplianceManager, AppliancePaths, CommandRunner


class FakeRunner(CommandRunner):
    def __init__(self, *, fail_config: bool = False) -> None:
        self.arguments: list[list[str]] = []
        self.fail_config = fail_config

    def run(
        self,
        arguments: list[str] | tuple[str, ...],
        *,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output
        values = list(arguments)
        self.arguments.append(values)
        if values[:2] == ["findmnt", "-n"]:
            target = values[-1]
            value = "active-uuid\n" if target.endswith("active") else "backup-uuid\n"
            return subprocess.CompletedProcess(values, 0, value, "")
        if values[:3] == ["systemctl", "is-active", "--quiet"]:
            return subprocess.CompletedProcess(values, 3, "", "")
        if "ps" in values and "--services" in values:
            return subprocess.CompletedProcess(
                values,
                0,
                "web\nworker\nsftpgo\nminio\n",
                "",
            )
        if self.fail_config and values[-2:] == ["config", "--quiet"]:
            raise subprocess.CalledProcessError(1, values)
        return subprocess.CompletedProcess(values, 0, "", "")


class EndpointRunner(CommandRunner):
    def __init__(
        self,
        *,
        running: tuple[str, ...] = ("web", "sftpgo"),
        routes: object = ({"dev": "eth0"},),
        interfaces: object = (
            {
                "ifname": "eth0",
                "addr_info": [{"family": "inet", "local": "192.168.10.109"}],
            },
        ),
        docker_returncode: int = 0,
    ) -> None:
        self.running = running
        self.routes = routes
        self.interfaces = interfaces
        self.docker_returncode = docker_returncode

    def run(
        self,
        arguments: list[str] | tuple[str, ...],
        *,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output
        values = list(arguments)
        if values[:3] == ["docker", "compose", "-f"]:
            return subprocess.CompletedProcess(
                values,
                self.docker_returncode,
                "\n".join(self.running),
                "",
            )
        if values == ["ip", "-json", "-4", "route", "show", "default"]:
            return subprocess.CompletedProcess(values, 0, json.dumps(self.routes), "")
        if values == ["ip", "-json", "-4", "address", "show", "up"]:
            return subprocess.CompletedProcess(values, 0, json.dumps(self.interfaces), "")
        raise AssertionError(f"unexpected command: {values}")


def appliance_paths(tmp_path: Path) -> AppliancePaths:
    install = tmp_path / "install"
    install.mkdir()
    (install / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    return AppliancePaths(
        config=tmp_path / "etc/config.yaml",
        secrets=tmp_path / "etc/secrets",
        install_root=install,
        data_root=tmp_path / "data",
        service=tmp_path / "mnema.service",
        lock=tmp_path / "run/admin.lock",
    )


def configured_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_config: bool = False,
) -> tuple[ApplianceManager, ApplianceConfig, FakeRunner]:
    active = tmp_path / "active"
    backup = tmp_path / "backup"
    source = tmp_path / "source"
    for path in (active, backup, source):
        path.mkdir()
    config = ApplianceConfig.model_validate(
        {
            "storage": {
                "active_root": active,
                "backup_root": backup,
                "source_root": source,
            }
        }
    )
    runner = FakeRunner(fail_config=fail_config)
    manager = ApplianceManager(appliance_paths(tmp_path), runner)
    monkeypatch.setattr(manager, "require_root", lambda: None)
    return manager, config, runner


def endpoint_manager(
    tmp_path: Path,
    config: ApplianceConfig,
    runner: EndpointRunner,
) -> ApplianceManager:
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = appliance_paths(tmp_path)
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(dump_config(config), encoding="utf-8")
    return ApplianceManager(paths, runner)


def test_save_config_validates_and_renders_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, config, runner = configured_manager(tmp_path, monkeypatch)

    manager.save_config(config)

    assert manager.config() == config
    assert manager.paths.environment.read_text(encoding="utf-8") == render_environment(config)
    assert any(arguments[0:2] == ["docker", "compose"] for arguments in runner.arguments)


def test_save_config_rolls_back_on_compose_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, config, _ = configured_manager(tmp_path, monkeypatch, fail_config=True)
    manager.paths.config.parent.mkdir(parents=True)
    manager.paths.config.write_text(dump_config(ApplianceConfig()), encoding="utf-8")
    manager.paths.environment.write_text("PREVIOUS=true\n", encoding="utf-8")

    with pytest.raises(subprocess.CalledProcessError):
        manager.save_config(config)

    assert manager.paths.environment.read_text(encoding="utf-8") == "PREVIOUS=true\n"


def test_secret_update_rolls_back_with_failed_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, config, _ = configured_manager(tmp_path, monkeypatch, fail_config=True)
    secret = manager.paths.secrets / "cloudflare_tunnel_token"
    secret.parent.mkdir(parents=True)
    secret.write_text("previous-token\n", encoding="utf-8")

    with pytest.raises(subprocess.CalledProcessError):
        manager.save_config(config, secret_updates={secret: "replacement-token\n"})

    assert secret.read_text(encoding="utf-8") == "previous-token\n"


def test_release_update_and_rollback_use_verified_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, config, _ = configured_manager(tmp_path, monkeypatch)
    manager.paths.config.parent.mkdir(parents=True)
    manager.paths.data_root.mkdir(parents=True)
    manager.paths.secrets.mkdir(parents=True)
    manager.paths.config.write_text(dump_config(config), encoding="utf-8")
    manager.paths.environment.write_text(render_environment(config), encoding="utf-8")
    with sqlite3.connect(manager.paths.data_root / "mnema.db") as connection:
        connection.execute("CREATE TABLE proof (value TEXT)")
    release_root = tmp_path / "release"
    (release_root / "src").mkdir(parents=True)
    for name in ("Dockerfile", "compose.yaml", "pyproject.toml"):
        (release_root / name).write_text(f"{name}\n", encoding="utf-8")
    (release_root / "src/module.py").write_text("VALUE = 1\n", encoding="utf-8")
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for candidate in release_root.iterdir():
            output.add(candidate, arcname=candidate.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    release = manager.update_from_archive(archive.resolve(), digest)

    assert release == digest[:12]
    assert (manager.paths.install_root / "src/module.py").is_file()
    assert manager.rollback() == release
    assert (manager.paths.install_root / "compose.yaml").read_text(encoding="utf-8") == (
        "services: {}\n"
    )


def test_backup_and_restore_preserve_config_secrets_and_consistent_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, config, _ = configured_manager(tmp_path, monkeypatch)
    manager.paths.config.parent.mkdir(parents=True)
    manager.paths.data_root.mkdir(parents=True)
    manager.paths.secrets.mkdir(parents=True)
    manager.paths.config.write_text(dump_config(config), encoding="utf-8")
    manager.paths.environment.write_text(render_environment(config), encoding="utf-8")
    secret = manager.paths.secrets / "mnema_cold_key"
    secret.write_bytes(b"k" * 32)
    database = manager.paths.data_root / "mnema.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE proof (value TEXT)")
        connection.execute("INSERT INTO proof VALUES ('before')")
    backup = (tmp_path / "mnema-config-backup.tar.gz").resolve()

    manager.backup(backup)
    secret.write_bytes(b"x" * 32)
    manager.paths.environment.write_text("BROKEN=true\n", encoding="utf-8")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE proof SET value = 'after'")
    manager.restore_config(backup)

    assert secret.read_bytes() == b"k" * 32
    assert manager.paths.environment.read_text(encoding="utf-8") == render_environment(config)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM proof").fetchone() == ("before",)


def test_cloudflare_configuration_requires_public_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, config, _ = configured_manager(tmp_path, monkeypatch)
    cloudflare = CloudflareConfig(
        enabled=True,
        team_domain="https://mnema.cloudflareaccess.com",
        audience="audience",
        admin_hostname="admin.example.com",
    )

    with pytest.raises(RuntimeError, match="cloudflared"):
        manager.verify_services(config.model_copy(update={"cloudflare": cloudflare}))


def test_service_endpoints_report_local_web_and_lan_sftp(tmp_path: Path) -> None:
    config = ApplianceConfig()
    report = endpoint_manager(tmp_path, config, EndpointRunner()).service_endpoints()

    assert [endpoint.as_dict() for endpoint in report.endpoints] == [
        {
            "service": "mnema-web",
            "url": "http://127.0.0.1:8080",
            "scope": "localhost",
            "state": "running",
        },
        {
            "service": "sftp",
            "url": "sftp://mnema-user@192.168.10.109:2022",
            "scope": "lan",
            "state": "running",
        },
    ]
    assert report.warnings == ("Web is localhost-only. Use: ssh -L 8080:127.0.0.1:8080 USER@HOST",)


def test_service_endpoints_report_cloudflare_and_recovery_without_secrets(
    tmp_path: Path,
) -> None:
    config = ApplianceConfig(
        cloudflare=CloudflareConfig(
            enabled=True,
            team_domain="https://mnema.cloudflareaccess.com",
            audience="secret-audience",
            admin_hostname="admin.example.com",
        )
    )
    runner = EndpointRunner(running=("web", "sftpgo", "web-public", "cloudflared"))

    payload = endpoint_manager(tmp_path, config, runner).service_endpoints().as_dict()

    assert payload["services"] == [
        {
            "service": "mnema-web",
            "url": "https://admin.example.com",
            "scope": "internet",
            "state": "running",
        },
        {
            "service": "mnema-recovery-web",
            "url": "http://127.0.0.1:8080",
            "scope": "localhost",
            "state": "running",
        },
        {
            "service": "sftp",
            "url": "sftp://mnema-user@192.168.10.109:2022",
            "scope": "lan",
            "state": "running",
        },
    ]
    serialized = json.dumps(payload)
    assert "secret-audience" not in serialized
    assert "cloudflareaccess.com" not in serialized
    assert "tunnel_token" not in serialized


def test_service_endpoints_use_explicit_binds_and_unknown_runtime(
    tmp_path: Path,
) -> None:
    config = ApplianceConfig(
        service=ServiceConfig(local_bind_address="192.168.20.10"),
        sftpgo=SFTPGoConfig(username="archive.user", bind_address="127.0.0.1"),
    )
    runner = EndpointRunner(docker_returncode=1)

    report = endpoint_manager(tmp_path, config, runner).service_endpoints()

    assert [(item.url, item.scope, item.state) for item in report.endpoints] == [
        ("http://192.168.20.10:8080", "lan", "unknown"),
        ("sftp://archive.user@127.0.0.1:2022", "localhost", "unknown"),
    ]
    assert report.warnings == ()


def test_service_endpoints_handle_multiple_or_missing_lan_addresses(
    tmp_path: Path,
) -> None:
    config = ApplianceConfig(
        service=ServiceConfig(
            local_bind_address="0.0.0.0"  # noqa: S104 - test wildcard bind
        ),
    )
    runner = EndpointRunner(
        routes=({"dev": "eth0"}, {"dev": "wlan0"}),
        interfaces=(
            {
                "ifname": "eth0",
                "addr_info": [
                    {"family": "inet", "local": "192.168.10.109"},
                    {"family": "inet", "local": "169.254.10.1"},
                ],
            },
            {
                "ifname": "wlan0",
                "addr_info": [{"family": "inet", "local": "192.168.20.109"}],
            },
            {
                "ifname": "docker0",
                "addr_info": [{"family": "inet", "local": "172.17.0.1"}],
            },
        ),
    )

    report = endpoint_manager(tmp_path, config, runner).service_endpoints()

    assert [item.url for item in report.endpoints] == [
        "http://192.168.10.109:8080",
        "http://192.168.20.109:8080",
        "sftp://mnema-user@192.168.10.109:2022",
        "sftp://mnema-user@192.168.20.109:2022",
    ]

    missing = endpoint_manager(
        tmp_path / "missing",
        config,
        EndpointRunner(routes=()),
    ).service_endpoints()
    assert missing.endpoints == ()
    assert missing.warnings == (
        "mnema-web: no usable LAN IPv4 address detected",
        "sftp: no usable LAN IPv4 address detected",
    )


def test_icloud_timer_arms_only_after_first_explicit_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, config, runner = configured_manager(tmp_path, monkeypatch)
    icloud = ICloudConfig(enabled=True, apple_id="archive@example.com", daily_at="03:00")
    configured = config.model_copy(update={"icloud": icloud})

    manager.save_config(configured)

    assert "OnCalendar=*-*-* 03:00:00" in manager.paths.icloud_timer.read_text(encoding="utf-8")
    assert ["systemctl", "disable", "--now", "mnema-icloud.timer"] in runner.arguments

    manager.paths.icloud_session.mkdir(parents=True)
    manager.icloud_sync(scheduled=False)

    assert manager.paths.icloud_schedule_armed.read_text(encoding="utf-8") == "armed\n"
    assert ["systemctl", "enable", "--now", "mnema-icloud.timer"] in runner.arguments
    assert any("icloud-sync-internal" in arguments for arguments in runner.arguments)
