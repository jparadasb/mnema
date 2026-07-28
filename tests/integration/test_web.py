from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from mnema.config import Settings, SourcePolicy
from mnema.domain.factory import build_local_workflow
from mnema.jobs.models import RuntimeSetting
from mnema.security.auth import hash_password
from mnema.web.app import create_app


def csrf_from(response: httpx.Response) -> str:
    start = response.text.index('name="csrf" value="') + len('name="csrf" value="')
    return response.text[start : response.text.index('"', start)]


def authenticated_application(tmp_path: Path) -> tuple[Settings, object]:
    roots = {name: tmp_path / name for name in ("active", "backup", "staging", "source", "config")}
    for root in roots.values():
        root.mkdir()
    cold_key = tmp_path / "cold-key"
    cold_key.write_bytes(b"k" * 32)
    settings = Settings(
        database_url=f"sqlite:///{roots['config'] / 'web.sqlite'}",
        active_root=roots["active"],
        backup_root=roots["backup"],
        staging_root=roots["staging"],
        source_root=roots["source"],
        secret_key_file=tmp_path / "missing-secret",
        cold_encryption_key_file=cold_key,
    )
    application = create_app(settings)
    with application.state.database.session() as session:
        session.add_all(
            [
                RuntimeSetting(key="onboarding_complete", value="true"),
                RuntimeSetting(
                    key="admin_password_hash",
                    value=hash_password("a-safe-password"),
                ),
                RuntimeSetting(key="archive_policy", value=SourcePolicy().model_dump_json()),
                RuntimeSetting(key="global_deletion_enabled", value="false"),
                RuntimeSetting(key="safety_lock", value="true"),
            ]
        )
    return settings, application


async def login(client: httpx.AsyncClient) -> None:
    response = await client.get("/login")
    result = await client.post(
        "/login",
        data={"password": "a-safe-password", "csrf": csrf_from(response)},
    )
    assert result.status_code == 303


@pytest.mark.asyncio
async def test_setup_is_mobile_accessible_and_same_device_fails(tmp_path: Path) -> None:
    onboarding_token_file = tmp_path / "onboarding-token"
    onboarding_token_file.write_text("temporary-onboarding-token")
    application = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'web.sqlite'}",
            secret_key_file=tmp_path / "missing-secret",
            onboarding_token_file=onboarding_token_file,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/setup")
        assert response.status_code == 200
        assert 'name="viewport"' in response.text
        assert "for=" not in response.text or "<label" in response.text
        token = csrf_from(response)
        active = tmp_path / "active"
        backup = tmp_path / "backup"
        active.mkdir()
        backup.mkdir()
        failed = await client.post(
            "/setup",
            data={
                "admin_name": "admin",
                "password": "a-safe-password",
                "onboarding_token": "temporary-onboarding-token",
                "active_root": str(active),
                "backup_root": str(backup),
                "csrf": token,
            },
        )
        assert failed.status_code == 400


@pytest.mark.asyncio
async def test_security_headers_and_health(tmp_path: Path) -> None:
    application = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'health.sqlite'}",
            secret_key_file=tmp_path / "none",
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthz")
        assert response.json() == {"status": "ok"}
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_authenticated_policy_and_emergency_pause(tmp_path: Path) -> None:
    _, application = authenticated_application(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        await login(client)
        policies = await client.get("/policies")
        assert policies.status_code == 200
        saved = await client.post(
            "/policies",
            data={
                "archive_after_days": "45",
                "stability_window_hours": "48",
                "quarantine_days": "14",
                "minimum_file_size": "0",
                "maximum_file_size": "",
                "dry_run": "true",
                "manual_approval": "true",
                "csrf": csrf_from(policies),
            },
        )
        assert saved.status_code == 303
        settings_page = await client.get("/settings")
        paused = await client.post(
            "/settings/pause-deletion",
            data={"csrf": csrf_from(settings_page)},
        )
        assert paused.status_code == 303
        with application.state.database.session() as session:
            policy = session.get(RuntimeSetting, "archive_policy")
            safety_lock = session.get(RuntimeSetting, "safety_lock")
            assert policy and SourcePolicy.model_validate_json(policy.value).quarantine_days == 14
            assert safety_lock and safety_lock.value == "true"


@pytest.mark.asyncio
async def test_web_local_and_remote_restore_workflow(tmp_path: Path) -> None:
    settings, application = authenticated_application(tmp_path)
    source = settings.source_root / "proof.bin"
    source.write_bytes(b"web restore proof")
    old = datetime.now(UTC) - timedelta(days=2)
    source.touch()
    source.chmod(0o600)
    timestamp = old.timestamp()
    import os

    os.utime(source, (timestamp, timestamp))
    workflow = build_local_workflow(
        settings,
        policy=SourcePolicy(
            archive_after_days=0,
            stability_window_hours=0,
            quarantine_days=7,
            dry_run=False,
        ),
    )
    with application.state.database.session() as session:
        item = (await workflow.discover(session))[0]
        await workflow.archive(session, item)
        item_id = item.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        await login(client)
        restores = await client.get("/restores")
        token = csrf_from(restores)
        local = await client.post(
            f"/restores/{item_id}/local",
            data={"csrf": token},
        )
        assert local.status_code == 303
        restores = await client.get("/restores")
        remote = await client.post(
            f"/restores/{item_id}/remote",
            data={"csrf": csrf_from(restores)},
        )
        assert remote.status_code == 303
        with application.state.database.session() as session:
            restored = session.get(type(item), item_id)
            assert restored and restored.restore_test_status
            assert restored.restore_test_status.startswith("remote:verified:")
