from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import BrowserType, Playwright, expect, sync_playwright

from mnema.config import Settings
from mnema.web.app import create_app

ADMIN_PASSWORD = "browser-test-password"  # noqa: S105 - synthetic E2E credential
ONBOARDING_TOKEN = "browser-test-onboarding-token"  # noqa: S105 - synthetic E2E token


def browser_executable(browser_type: BrowserType) -> str | None:
    configured = os.getenv("MNEMA_E2E_CHROMIUM")
    if configured:
        return configured
    system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if system_chromium:
        return system_chromium
    bundled = Path(browser_type.executable_path)
    return str(bundled) if bundled.is_file() else None


@contextmanager
def running_application(settings: Settings) -> Iterator[str]:
    application = create_app(settings)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("Mnema E2E server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def launch_browser(playwright: Playwright) -> object:
    executable = browser_executable(playwright.chromium)
    if executable is None:
        pytest.skip("install Playwright Chromium or set MNEMA_E2E_CHROMIUM")
    return playwright.chromium.launch(
        executable_path=executable,
        headless=True,
        args=["--no-sandbox"],
    )


@pytest.mark.e2e
def test_complete_mobile_administration_journey(tmp_path: Path) -> None:
    shared_memory = Path("/dev/shm")  # noqa: S108 - deliberate second disposable filesystem
    if not shared_memory.is_dir() or os.stat(shared_memory).st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("E2E setup needs two disposable filesystem device identities")

    active = tmp_path / "active"
    source = tmp_path / "source"
    config = tmp_path / "config"
    for directory in (active, source, config):
        directory.mkdir()
    staging = active / ".mnema-staging"
    staging.mkdir()
    (source / "browser-proof.txt").write_text("Mnema browser archive proof", encoding="utf-8")
    onboarding = config / "onboarding-token"
    onboarding.write_text(ONBOARDING_TOKEN, encoding="utf-8")
    cold_key = config / "cold-key"
    cold_key.write_bytes(b"e" * 32)

    with tempfile.TemporaryDirectory(prefix="mnema-e2e-backup-", dir=shared_memory) as backup:
        settings = Settings(
            database_url=f"sqlite:///{config / 'e2e.sqlite'}",
            active_root=active,
            backup_root=Path(backup),
            staging_root=staging,
            source_root=source,
            secret_key_file=config / "missing-session-key",
            cold_encryption_key_file=cold_key,
            onboarding_token_file=onboarding,
        )
        with running_application(settings) as base_url, sync_playwright() as playwright:
            browser = launch_browser(playwright)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(f"{base_url}/setup")
            expect(page.get_by_role("heading", name="Setup")).to_be_visible()
            expect(page.locator(".warning")).to_contain_text("different mounted devices")
            page.get_by_label("One-time onboarding token").fill(ONBOARDING_TOKEN)
            page.get_by_label("Administrator name").fill("Browser administrator")
            page.get_by_label("Password (12+ characters)").fill(ADMIN_PASSWORD)
            page.get_by_label("Active storage path").fill(str(active))
            page.get_by_label("Backup storage path").fill(backup)
            page.get_by_role("button", name="Save").click()
            expect(page.get_by_role("status")).to_contain_text("Deletion remains paused")

            page.goto(f"{base_url}/login")
            page.get_by_label("Password").fill(ADMIN_PASSWORD)
            page.get_by_role("button", name="Login").click()
            expect(page).to_have_url(f"{base_url}/dashboard")
            expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
            expect(page.get_by_text("Healthy", exact=True).first).to_be_visible()

            page.get_by_role("link", name="Policies").click()
            page.get_by_label("Archive after days").fill("0")
            page.get_by_label("Stability window hours").fill("0")
            page.get_by_label("Quarantine days").fill("7")
            page.get_by_label("Dry-run only").uncheck()
            page.get_by_role("button", name="Save policy").click()
            expect(page.get_by_role("status")).to_contain_text("Policy saved")

            page.get_by_role("link", name="Sources").click()
            expect(page.get_by_text("browser-proof.txt")).to_be_visible()
            page.get_by_role("button", name="Archive eligible files").click()
            expect(page).to_have_url(f"{base_url}/archive")
            expect(page.get_by_text("QUARANTINED")).to_be_visible()

            page.get_by_role("link", name="Restores").click()
            page.get_by_role("button", name="Test local").click()
            expect(page.get_by_role("status")).to_contain_text("SHA-256 verified")
            page.get_by_role("button", name="Test remote").click()
            expect(page.get_by_role("status")).to_contain_text("SHA-256 verified")

            page.get_by_role("link", name="Settings").click()
            page.get_by_role("button", name="Emergency pause deletion").click()
            expect(page.get_by_role("status")).to_contain_text("Deletion paused")
            expect(page.get_by_text("Disabled", exact=True)).to_be_visible()
            expect(page.get_by_text("Active", exact=True)).to_be_visible()

            page.get_by_role("link", name="Audit").click()
            expect(page.get_by_text("emergency_pause")).to_be_visible()
            assert page.viewport_size == {"width": 390, "height": 844}
            browser.close()
