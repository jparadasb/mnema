from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from sqlalchemy import select

from mnema.config import Settings
from mnema.domain.states import ArchiveState
from mnema.file_provider import create_file_provider_app
from mnema.file_provider.auth import create_pairing_code
from mnema.file_provider.service import project_verified_archives
from mnema.jobs.models import (
    ArchiveItem,
    FileProviderDevice,
    FileProviderItem,
    FileProviderUpload,
    Job,
)

DOWNLOAD_CONTENT = b"Mnema simulator download proof"
UPLOAD_CONTENT = b"Mnema simulator upload proof"
DOWNLOAD_NAME = "mnema-e2e-download.txt"
UPLOAD_NAME = "mnema-e2e-upload.txt"
APP_BUNDLE_ID = "com.jparadasb.mnema"
FIXTURE_BUNDLE_ID = "com.jparadasb.mnema.e2e-fixture"
FILES_BUNDLE_ID = "com.apple.DocumentsApp"


@dataclass(frozen=True)
class Simulator:
    udid: str
    name: str
    platform_version: str


def run(*arguments: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argument arrays only
        arguments,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def required_path(variable: str) -> Path:
    value = os.getenv(variable)
    if value and Path(value).is_dir():
        return Path(value).resolve()
    message = f"{variable} must point to a built simulator .app bundle"
    if os.getenv("MNEMA_IOS_E2E_REQUIRED") == "1":
        pytest.fail(message)
    pytest.skip(message)


def require_automation_dependencies() -> None:
    missing = [name for name in ("appium", "selenium") if importlib.util.find_spec(name) is None]
    if not missing:
        return
    message = f"install iOS E2E dependencies: {', '.join(missing)}"
    if os.getenv("MNEMA_IOS_E2E_REQUIRED") == "1":
        pytest.fail(message)
    pytest.skip(message)


def prebuild_webdriveragent(root: Path) -> Path:
    project = Path(
        "node_modules/appium-xcuitest-driver/node_modules/"
        "appium-webdriveragent/WebDriverAgent.xcodeproj"
    ).resolve()
    if not project.is_file() and not project.is_dir():
        pytest.fail("the pinned WebDriverAgent project is unavailable; run npm ci")
    configured = os.getenv("MNEMA_E2E_WDA_DERIVED_DATA")
    derived_data = Path(configured).resolve() if configured else root / "wda-derived"
    runner = derived_data / "Build/Products/Debug-iphonesimulator/WebDriverAgentRunner-Runner.app"
    if runner.is_dir():
        return derived_data
    run(
        "xcodebuild",
        "build-for-testing",
        "-project",
        str(project),
        "-scheme",
        "WebDriverAgentRunner",
        "-destination",
        "generic/platform=iOS Simulator",
        "-derivedDataPath",
        str(derived_data),
        "IPHONEOS_DEPLOYMENT_TARGET=16.2",
        "GCC_TREAT_WARNINGS_AS_ERRORS=0",
        "COMPILER_INDEX_STORE_ENABLE=NO",
        timeout=300,
    )
    return derived_data


def free_port() -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    try:
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def create_test_certificate(directory: Path) -> tuple[Path, Path]:
    key = directory / "localhost-key.pem"
    certificate = directory / "localhost-cert.pem"
    config = directory / "openssl.cnf"
    config.write_text(
        """[req]
distinguished_name=subject
x509_extensions=extensions
prompt=no
[subject]
CN=localhost
[extensions]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,digitalSignature,keyEncipherment
subjectAltName=DNS:localhost,IP:127.0.0.1
""",
        encoding="utf-8",
    )
    run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-keyout",
        str(key),
        "-out",
        str(certificate),
        "-config",
        str(config),
    )
    return certificate, key


@contextmanager
def running_file_provider(
    root: Path, certificate: Path, key: Path
) -> Iterator[tuple[str, str, Any, Settings, dict[str, int]]]:
    active = root / "active"
    backup = root / "backup"
    source = root / "source"
    for directory in (active, backup, source):
        directory.mkdir()
    signing_key = root / "signing-key"
    signing_key.write_text("synthetic-simulator-signing-key-long-enough\n", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite:///{root / 'mnema.sqlite'}",
        active_root=active,
        backup_root=backup,
        staging_root=active / ".mnema-staging",
        source_root=source,
        secret_key_file=signing_key,
        file_provider_enabled=True,
        file_provider_public_url="https://localhost",
        file_provider_upload_root=active / ".mnema-file-provider",
        require_smart_health=False,
    )
    application = create_file_provider_app(settings)
    requests: dict[str, int] = {}

    @application.middleware("http")
    async def count_requests(request: Any, call_next: Any) -> Any:
        requests[request.url.path] = requests.get(request.url.path, 0) + 1
        return await call_next(request)

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
            ssl_certfile=str(certificate),
            ssl_keyfile=str(key),
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("Mnema File Provider E2E server did not start")

    stored = active / "e2e-download.txt"
    stored.write_bytes(DOWNLOAD_CONTENT)
    with application.state.database.session() as session:
        archive = ArchiveItem(
            source_provider="local_test",
            source_identifier="ios-e2e-download",
            original_path="e2e-download.txt",
            original_size=len(DOWNLOAD_CONTENT),
            original_modified_at=datetime.now(UTC),
            source_version="1",
            state=ArchiveState.ARCHIVED,
            plaintext_sha256=hashlib.sha256(DOWNLOAD_CONTENT).hexdigest(),
            nas_path=str(stored),
            cold_archived_at=datetime.now(UTC),
        )
        session.add(archive)
        session.flush()
        project_verified_archives(session)
        projected = session.scalar(
            select(FileProviderItem).where(FileProviderItem.archive_item_id == archive.id)
        )
        assert projected is not None
        projected.parent_id = "inbox"
        projected.name = DOWNLOAD_NAME
        for folder_id in ("archive-local", "archive-icloud", "archive-uploads", "archive"):
            folder = session.get(FileProviderItem, folder_id)
            assert folder is not None
            session.delete(folder)
        pairing_code = create_pairing_code(session)

    try:
        yield (
            f"https://127.0.0.1:{port}",
            pairing_code,
            application,
            settings,
            requests,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def available_ios_runtimes() -> list[dict[str, Any]]:
    payload = json.loads(run("xcrun", "simctl", "list", "runtimes", "--json").stdout)
    return [
        runtime
        for runtime in payload["runtimes"]
        if runtime.get("isAvailable", False)
        and runtime.get("identifier", "").startswith("com.apple.CoreSimulator.SimRuntime.iOS-")
    ]


def select_ios_runtime() -> dict[str, Any]:
    runtimes = available_ios_runtimes()
    configured = os.getenv("MNEMA_IOS_E2E_RUNTIME")
    if configured:
        selected = next(
            (runtime for runtime in runtimes if runtime["identifier"] == configured), None
        )
        if selected is None:
            pytest.fail(f"configured iOS simulator runtime is unavailable: {configured}")
        return selected
    if not runtimes:
        pytest.fail("no available iOS simulator runtime is installed")
    return max(
        runtimes,
        key=lambda runtime: tuple(int(part) for part in runtime["version"].split(".")),
    )


@contextmanager
def disposable_simulator(certificate: Path | None = None) -> Iterator[Simulator]:
    runtime = select_ios_runtime()
    device_type = os.getenv(
        "MNEMA_IOS_E2E_DEVICE_TYPE", "com.apple.CoreSimulator.SimDeviceType.iPhone-14"
    )
    name = f"Mnema E2E {os.getpid()}"
    created = run(
        "xcrun",
        "simctl",
        "create",
        name,
        device_type,
        runtime["identifier"],
    ).stdout.strip()
    try:
        run("xcrun", "simctl", "boot", created)
        run("xcrun", "simctl", "bootstatus", created, "-b", timeout=180)
        if certificate is not None:
            run("xcrun", "simctl", "keychain", created, "add-root-cert", str(certificate))
        run(
            "xcrun",
            "simctl",
            "spawn",
            created,
            "defaults",
            "write",
            "NSGlobalDomain",
            "AppleLanguages",
            "-array",
            "en",
        )
        yield Simulator(
            udid=created,
            name=os.getenv("MNEMA_IOS_E2E_DEVICE_NAME", "iPhone 14"),
            platform_version=runtime["version"],
        )
    finally:
        subprocess.run(  # noqa: S603 - exact test-created simulator identifier
            ["xcrun", "simctl", "shutdown", created],  # noqa: S607
            check=False,
            capture_output=True,
        )
        subprocess.run(  # noqa: S603 - exact test-created simulator identifier
            ["xcrun", "simctl", "delete", created],  # noqa: S607
            check=False,
            capture_output=True,
        )


@contextmanager
def running_appium(root: Path) -> Iterator[str]:
    executable = Path("node_modules/.bin/appium").resolve()
    if not executable.is_file():
        message = "run npm ci before the iOS E2E test"
        if os.getenv("MNEMA_IOS_E2E_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)
    port = free_port()
    log_path = root / "appium.log"
    environment = os.environ.copy()
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(  # noqa: S603 - repository-pinned executable
            [str(executable), "--port", str(port), "--base-path", "/wd/hub"],
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
        endpoint = f"http://127.0.0.1:{port}/wd/hub"
        deadline = time.monotonic() + 45
        while process.poll() is None and time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{endpoint}/status", timeout=1):  # noqa: S310
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        else:
            process.terminate()
            process.wait(timeout=10)
            pytest.fail(f"Appium did not start; see {log_path}")
        try:
            yield endpoint
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def click_named(driver: Any, name: str, timeout: float = 30) -> Any:
    from appium.webdriver.common.appiumby import AppiumBy
    from selenium.webdriver.support import expected_conditions as conditions
    from selenium.webdriver.support.ui import WebDriverWait

    element = WebDriverWait(driver, timeout).until(
        conditions.element_to_be_clickable((AppiumBy.ACCESSIBILITY_ID, name))
    )
    element.click()
    return element


def wait_for_text(driver: Any, identifier: str, expected: str, timeout: float = 30) -> None:
    from appium.webdriver.common.appiumby import AppiumBy
    from selenium.webdriver.support.ui import WebDriverWait

    def contains_expected(current: Any) -> bool:
        element = current.find_element(AppiumBy.ACCESSIBILITY_ID, identifier)
        value = element.get_attribute("value") or element.text or ""
        if value.startswith("Connection failed:"):
            raise AssertionError(value)
        return expected in value

    WebDriverWait(driver, timeout).until(contains_expected)


def click_if_present(driver: Any, name: str) -> bool:
    from appium.webdriver.common.appiumby import AppiumBy

    for element in driver.find_elements(AppiumBy.ACCESSIBILITY_ID, name):
        if element.is_displayed() and element.is_enabled():
            element.click()
            return True
    return False


def show_browse_root(driver: Any) -> None:
    """Return the browser to the Locations list.

    Files reopens wherever it was last left, and while a location's contents
    are showing there is no back button to escape with, so the sidebar entries
    this test navigates by are simply absent. Selecting the already-selected
    Browse tab pops that stack back to the list.
    """
    click_if_present(driver, "Browse")
    for _ in range(6):
        if not click_if_present(driver, "BackButton"):
            break
        time.sleep(0.4)
    click_if_present(driver, "Browse")
    time.sleep(0.5)


def wait_for_files_item(driver: Any, *location: str, name: str, timeout: float = 120) -> None:
    """Wait for a provider item to appear, reopening the folder while waiting.

    A folder opened before the provider finishes its first sync keeps showing
    the listing it was drawn with, so an item that arrives afterwards only
    becomes visible once the view is rebuilt.
    """
    from appium.webdriver.common.appiumby import AppiumBy

    deadline = time.monotonic() + timeout
    while True:
        if driver.find_elements(AppiumBy.ACCESSIBILITY_ID, name):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
        # Reopening is best effort: the sidebar is briefly unavailable while
        # the provider reloads, and failing there would hide what is missing.
        try_open_files_location(driver, *location)
    source = driver.page_source
    visible_names = sorted(set(re.findall(r'\bname="([^"]+)"', source)))
    pytest.fail(f"Files never listed {name}; visible elements: {visible_names}")


def try_open_files_location(driver: Any, *names: str, timeout: float = 30) -> bool:
    driver.execute_script("mobile: activateApp", {"bundleId": FILES_BUNDLE_ID})
    show_browse_root(driver)
    for index, name in enumerate(names):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Locations carry a stable identifier; their plain label is also
            # used by unrelated views such as the recents list.
            if index == 0 and click_if_present(driver, f"DOC.sidebar.item.{name}"):
                break
            if click_if_present(driver, name):
                break
            if index == 0:
                show_browse_root(driver)
            time.sleep(0.25)
        else:
            return False
    return True


def open_files_location(driver: Any, *names: str) -> None:
    if try_open_files_location(driver, *names):
        return
    source = driver.page_source
    visible_names = sorted(set(re.findall(r'\bname="([^"]+)"', source)))
    pytest.fail(f"Files location is unavailable: {names}; visible elements: {visible_names}")


def wait_for_upload(application: Any, settings: Settings, timeout: float = 45) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with application.state.database.session() as session:
            upload = session.scalar(
                select(FileProviderUpload)
                .join(FileProviderItem, FileProviderItem.id == FileProviderUpload.item_id)
                .where(FileProviderItem.name == UPLOAD_NAME)
            )
            if upload is not None and upload.received_size == len(UPLOAD_CONTENT):
                archive = session.get(ArchiveItem, upload.archive_item_id)
                assert archive is not None
                assert archive.plaintext_sha256 == hashlib.sha256(UPLOAD_CONTENT).hexdigest()
                staged = settings.staging_root / f"{archive.id}.partial"
                assert staged.read_bytes() == UPLOAD_CONTENT
                assert session.scalar(
                    select(Job).where(
                        Job.adapter == "file_provider", Job.payload["upload_id"] == upload.id
                    )
                )
                return archive.id
        time.sleep(0.25)
    pytest.fail("Files did not upload the fixture into Mnema Inbox")


def capture_simulator_logs(simulator: Simulator, destination: Path) -> None:
    result = run(
        "xcrun",
        "simctl",
        "spawn",
        simulator.udid,
        "log",
        "show",
        "--last",
        "5m",
        "--style",
        "compact",
        "--predicate",
        'process == "MnemaFileProvider" OR subsystem == "com.apple.FileProvider"',
    )
    destination.write_text(result.stdout, encoding="utf-8")


@pytest.mark.e2e
def test_ios_pairs_browses_downloads_and_uploads(tmp_path: Path) -> None:
    if sys.platform != "darwin":
        pytest.skip("WebDriverAgent requires macOS")
    require_automation_dependencies()
    app_path = required_path("MNEMA_E2E_IOS_APP")
    fixture_path = required_path("MNEMA_E2E_IOS_FIXTURE_APP")
    certificate, key = create_test_certificate(tmp_path)
    wda_derived_data = prebuild_webdriveragent(tmp_path)

    from appium import webdriver
    from appium.options.ios import XCUITestOptions
    from appium.webdriver.common.appiumby import AppiumBy

    with (
        running_file_provider(tmp_path, certificate, key) as backend,
        disposable_simulator(certificate) as simulator,
        running_appium(tmp_path) as appium_endpoint,
    ):
        base_url, pairing_code, application, settings, requests = backend
        run("xcrun", "simctl", "install", simulator.udid, str(fixture_path))
        options = XCUITestOptions().load_capabilities(
            {
                "platformName": "iOS",
                "appium:automationName": "XCUITest",
                "appium:udid": simulator.udid,
                "appium:deviceName": simulator.name,
                "appium:platformVersion": simulator.platform_version,
                "appium:app": str(app_path),
                "appium:noReset": True,
                "appium:autoAcceptAlerts": True,
                "appium:newCommandTimeout": 180,
                "appium:wdaLaunchTimeout": 120000,
                "appium:wdaStartupRetries": 1,
                "appium:derivedDataPath": str(wda_derived_data),
                "appium:usePrebuiltWDA": True,
            }
        )
        driver = webdriver.Remote(appium_endpoint, options=options)
        try:
            driver.find_element(AppiumBy.ACCESSIBILITY_ID, "mnema.server-url").send_keys(base_url)
            driver.find_element(AppiumBy.ACCESSIBILITY_ID, "mnema.pairing-code").send_keys(
                pairing_code
            )
            click_named(driver, "mnema.connect")
            wait_for_text(
                driver,
                "mnema.connection-status",
                "Connected. Your archive is available in Files.",
                timeout=45,
            )
            with application.state.database.session() as session:
                assert len(session.scalars(select(FileProviderDevice)).all()) == 1

            driver.terminate_app(APP_BUNDLE_ID)
            driver.activate_app(APP_BUNDLE_ID)
            driver.find_element(AppiumBy.ACCESSIBILITY_ID, "mnema.connected-toggle")

            open_files_location(driver, "Mnema", "Inbox")
            wait_for_files_item(driver, "Mnema", "Inbox", name=DOWNLOAD_NAME)
            click_named(driver, DOWNLOAD_NAME, timeout=60)
            deadline = time.monotonic() + 30
            while requests.get("/v1/items/1/content", 0) == 0 and time.monotonic() < deadline:
                time.sleep(0.2)
            assert any(path.endswith("/content") for path in requests)

            driver.execute_script(
                "mobile: pushFile",
                {
                    "remotePath": f"@{FIXTURE_BUNDLE_ID}:documents/{UPLOAD_NAME}",
                    "payload": base64.b64encode(UPLOAD_CONTENT).decode(),
                },
            )
            open_files_location(driver, "On My iPhone", "Mnema E2E Fixture")
            upload = driver.find_element(AppiumBy.ACCESSIBILITY_ID, UPLOAD_NAME)
            driver.execute_script("mobile: touchAndHold", {"elementId": upload.id, "duration": 1.5})
            click_named(driver, "Copy")
            open_files_location(driver, "Mnema", "Inbox")
            driver.execute_script("mobile: touchAndHold", {"x": 200, "y": 500, "duration": 1.5})
            click_named(driver, "Paste")
            uploaded_archive_id = wait_for_upload(application, settings)
            open_files_location(driver, "Mnema", "Inbox")
            uploaded = driver.find_element(AppiumBy.ACCESSIBILITY_ID, UPLOAD_NAME)
            driver.execute_script(
                "mobile: touchAndHold", {"elementId": uploaded.id, "duration": 1.5}
            )
            click_named(driver, "Delete")
            click_if_present(driver, "Delete")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                with application.state.database.session() as session:
                    archive = session.get(ArchiveItem, uploaded_archive_id)
                    if (
                        archive is not None
                        and archive.state == ArchiveState.TEST_CLEANED
                        and archive.audit_events[-1].actor == "e2e-cleanup"
                    ):
                        break
                time.sleep(0.25)
            else:
                pytest.fail("Files did not delete the isolated E2E upload")
        except BaseException:
            screenshot = tmp_path / "ios-e2e-failure.png"
            driver.get_screenshot_as_file(str(screenshot))
            (tmp_path / "ios-e2e-page-source.xml").write_text(driver.page_source, encoding="utf-8")
            capture_simulator_logs(simulator, tmp_path / "ios-e2e-system.log")
            # What the extension asked the appliance for separates a provider
            # that never called from one whose answers were not what Files
            # needed, which the screenshot alone cannot distinguish.
            (tmp_path / "ios-e2e-requests.json").write_text(
                json.dumps(dict(requests), indent=2, sort_keys=True), encoding="utf-8"
            )
            raise
        finally:
            driver.quit()


@pytest.mark.e2e
def test_ios_pairs_with_deployed_server(tmp_path: Path) -> None:
    server = os.getenv("MNEMA_E2E_REMOTE_SERVER")
    pairing_file = os.getenv("MNEMA_E2E_REMOTE_PAIRING_FILE")
    if not server or not pairing_file:
        pytest.skip("deployed-server E2E variables are not configured")
    if sys.platform != "darwin":
        pytest.skip("WebDriverAgent requires macOS")
    require_automation_dependencies()
    app_path = required_path("MNEMA_E2E_IOS_APP")
    pairing = json.loads(Path(pairing_file).read_text(encoding="utf-8"))["code"]
    wda_derived_data = prebuild_webdriveragent(tmp_path)

    from appium import webdriver
    from appium.options.ios import XCUITestOptions
    from appium.webdriver.common.appiumby import AppiumBy

    with disposable_simulator() as simulator, running_appium(tmp_path) as appium_endpoint:
        options = XCUITestOptions().load_capabilities(
            {
                "platformName": "iOS",
                "appium:automationName": "XCUITest",
                "appium:udid": simulator.udid,
                "appium:deviceName": simulator.name,
                "appium:platformVersion": simulator.platform_version,
                "appium:app": str(app_path),
                "appium:noReset": True,
                "appium:autoAcceptAlerts": True,
                "appium:newCommandTimeout": 180,
                "appium:wdaLaunchTimeout": 120000,
                "appium:wdaStartupRetries": 1,
                "appium:derivedDataPath": str(wda_derived_data),
                "appium:usePrebuiltWDA": True,
            }
        )
        driver = webdriver.Remote(appium_endpoint, options=options)
        try:
            driver.find_element(AppiumBy.ACCESSIBILITY_ID, "mnema.server-url").send_keys(server)
            driver.find_element(AppiumBy.ACCESSIBILITY_ID, "mnema.pairing-code").send_keys(pairing)
            click_named(driver, "mnema.connect")
            wait_for_text(
                driver,
                "mnema.connection-status",
                "Connected. Your archive is available in Files.",
                timeout=45,
            )
            open_files_location(driver, "Mnema")
        except Exception:
            driver.get_screenshot_as_file(str(tmp_path / "ios-remote-e2e-failure.png"))
            (tmp_path / "ios-remote-e2e-page-source.xml").write_text(
                driver.page_source, encoding="utf-8"
            )
            raise
        finally:
            driver.quit()
