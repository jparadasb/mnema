import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from mnema.admin import cli as admin_cli
from mnema.admin.config import ApplianceConfig
from mnema.admin.host import ServiceEndpoint, ServiceEndpointReport
from mnema.cli import app


def test_unified_cli_exposes_appliance_command_tree() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--help"])
    configure = runner.invoke(app, ["configure", "--help"])
    cold_storage = runner.invoke(app, ["configure", "cold-storage", "--help"])
    config = runner.invoke(app, ["config", "--help"])
    icloud = runner.invoke(app, ["icloud", "--help"])

    assert result.exit_code == 0
    for command in (
        "install",
        "start",
        "stop",
        "restart",
        "urls",
        "backup",
        "restore",
        "uninstall",
    ):
        assert command in result.stdout
    assert configure.exit_code == 0
    assert "cold-storage" in configure.stdout
    assert "cloudflare" in configure.stdout
    assert "icloud" in configure.stdout
    assert cold_storage.exit_code == 0
    assert "--provider" in cold_storage.stdout
    assert "--region" in cold_storage.stdout
    assert config.exit_code == 0
    assert "validate" in config.stdout
    assert icloud.exit_code == 0
    for command in ("auth", "preview", "sync", "status"):
        assert command in icloud.stdout


def test_urls_supports_human_and_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    report = ServiceEndpointReport(
        endpoints=(
            ServiceEndpoint(
                service="mnema-web",
                url="https://admin.example.com",
                scope="internet",
                state="running",
            ),
        ),
        warnings=("Example warning",),
    )

    class FakeManager:
        def service_endpoints(self) -> ServiceEndpointReport:
            return report

    monkeypatch.setattr(admin_cli, "_manager", FakeManager)
    runner = CliRunner()

    human = runner.invoke(app, ["urls"])
    machine = runner.invoke(app, ["urls", "--json"])

    assert human.exit_code == 0
    assert "https://admin.example.com" in human.stdout
    assert "running" in human.stdout
    assert json.loads(machine.stdout) == report.as_dict()


def test_scaleway_configuration_derives_endpoint_and_copies_secret_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = tmp_path / "access"
    secret = tmp_path / "secret"
    access.write_text("test-access\n", encoding="utf-8")
    secret.write_text("test-secret\n", encoding="utf-8")

    class FakeManager:
        def __init__(self) -> None:
            self.paths = SimpleNamespace(secrets=tmp_path / "installed-secrets")
            self.saved: ApplianceConfig | None = None
            self.secret_updates: dict[Path, str] | None = None

        def config(self) -> ApplianceConfig:
            return ApplianceConfig()

        def save_config(
            self,
            config: ApplianceConfig,
            *,
            secret_updates: dict[Path, str] | None = None,
        ) -> None:
            self.saved = config
            self.secret_updates = secret_updates

    manager = FakeManager()
    monkeypatch.setattr(admin_cli, "_manager", lambda: manager)

    result = CliRunner().invoke(
        app,
        [
            "configure",
            "cold-storage",
            "--provider",
            "scaleway",
            "--region",
            "nl-ams",
            "--s3-bucket",
            "mnema-archive",
            "--s3-access-key-file",
            str(access),
            "--s3-secret-key-file",
            str(secret),
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert manager.saved is not None
    assert manager.saved.cold_storage.provider == "scaleway"
    assert manager.saved.cold_storage.s3_region == "nl-ams"
    assert manager.saved.cold_storage.s3_endpoint_url == "https://s3.nl-ams.scw.cloud"
    assert manager.secret_updates == {
        tmp_path / "installed-secrets/s3_access_key": "test-access\n",
        tmp_path / "installed-secrets/s3_secret_key": "test-secret\n",
    }
    assert "test-access" not in result.stdout
    assert "test-secret" not in result.stdout
