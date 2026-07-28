from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from mnema.adapters.nas.sftpgo import SFTPGoAPIClient
from mnema.config import Settings
from mnema.config.settings import SourcePolicy
from mnema.diagnostics.health import startup_checks
from mnema.domain.factory import build_local_workflow
from mnema.domain.states import ArchiveState
from mnema.domain.storage import storage_identity, storage_is_separate
from mnema.jobs import Database
from mnema.jobs.models import ArchiveItem, AuditEvent, Job, RuntimeSetting
from mnema.policies import evaluate_policy
from mnema.security.auth import csrf_token, hash_password, verify_password
from mnema.web.i18n import strings

PAGES = (
    "dashboard",
    "sources",
    "storage",
    "policies",
    "jobs",
    "archive",
    "quarantine",
    "restores",
    "audit",
    "settings",
    "diagnostics",
)


class SecurityHeadersMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (
                            b"content-security-policy",
                            b"default-src 'self'; style-src 'self'; script-src 'self'; "
                            b"img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'",
                        ),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"permissions-policy",
                            b"camera=(), microphone=(), geolocation=()",
                        ),
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _secret(settings: Settings) -> str:
    if settings.secret_key_file.is_file():
        return settings.secret_key_file.read_text(encoding="utf-8").strip()
    return os.getenv("MNEMA_SECRET_KEY", secrets.token_urlsafe(48))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    database = Database(settings.database_url)
    database.create_schema()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        yield
        database.close()

    app = FastAPI(title="Mnema", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.database = database
    app.state.settings = settings
    templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent.parent / "static"),
        name="static",
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=_secret(settings),
        same_site="strict",
        https_only=os.getenv("MNEMA_SECURE_COOKIES", "0") == "1",
    )
    app.add_middleware(SecurityHeadersMiddleware)

    def context(request: Request, **extra: Any) -> dict[str, Any]:
        language = request.query_params.get("lang", "en")
        token = request.session.get("csrf")
        if not token:
            token = csrf_token()
            request.session["csrf"] = token
        return {
            "request": request,
            "text": strings(language),
            "language": language,
            "pages": PAGES,
            "csrf": token,
            "authenticated": bool(request.session.get("authenticated")),
            **extra,
        }

    def require_csrf(request: Request, token: str) -> None:
        expected = request.session.get("csrf")
        if not expected or not secrets.compare_digest(expected, token):
            raise HTTPException(403, "CSRF validation failed")

    def require_login(request: Request) -> None:
        if not request.session.get("authenticated"):
            raise HTTPException(401, "authentication required")

    def runtime_value(key: str, default: str) -> str:
        with database.session() as session:
            setting = session.get(RuntimeSetting, key)
            return setting.value if setting else default

    def set_runtime_value(key: str, value: str) -> None:
        with database.session() as session:
            setting = session.get(RuntimeSetting, key)
            if setting is None:
                session.add(RuntimeSetting(key=key, value=value))
            else:
                setting.value = value

    def archive_policy() -> SourcePolicy:
        value = runtime_value("archive_policy", "")
        if not value:
            return SourcePolicy()
        return SourcePolicy.model_validate_json(value)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok" if database.integrity_check() else "unhealthy"}

    @app.get("/setup", response_class=HTMLResponse)
    async def setup(request: Request) -> Response:
        with database.session() as session:
            complete = session.get(RuntimeSetting, "onboarding_complete")
        if complete is not None and complete.value == "true":
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "setup.html",
            context(request),
        )

    @app.post("/setup", response_class=HTMLResponse)
    async def setup_submit(
        request: Request,
        admin_name: str = Form(min_length=1, max_length=100),
        password: str = Form(min_length=12, max_length=256),
        onboarding_token: str = Form(min_length=16, max_length=256),
        active_root: str = Form(),
        backup_root: str = Form(),
        csrf: str = Form(),
    ) -> HTMLResponse:
        require_csrf(request, csrf)
        with database.session() as session:
            complete = session.get(RuntimeSetting, "onboarding_complete")
        if complete is not None and complete.value == "true":
            raise HTTPException(409, "onboarding is already complete")
        if not settings.onboarding_token_file.is_file():
            raise HTTPException(503, "onboarding token is unavailable")
        expected_token = settings.onboarding_token_file.read_text(encoding="utf-8").strip()
        if not secrets.compare_digest(expected_token, onboarding_token):
            raise HTTPException(403, "invalid onboarding token")
        active = Path(active_root)
        backup = Path(backup_root)
        if not active.is_dir() or not backup.is_dir():
            raise HTTPException(400, "storage paths must exist")
        if not storage_is_separate(storage_identity(active), storage_identity(backup)):
            raise HTTPException(400, "active and backup storage must use different devices")
        if not settings.source_root.is_dir():
            raise HTTPException(400, "local test source path must exist")
        workflow = build_local_workflow(settings)
        if not await workflow.cold.available():
            raise HTTPException(503, "test object storage is unavailable")
        with database.session() as session:
            values = {
                "admin_name": admin_name,
                "admin_password_hash": hash_password(password),
                "active_root": str(active.resolve()),
                "backup_root": str(backup.resolve()),
                "global_deletion_enabled": "false",
                "safety_lock": "true",
                "archive_policy": SourcePolicy().model_dump_json(),
                "onboarding_complete": "true",
            }
            for key, value in values.items():
                setting = session.get(RuntimeSetting, key)
                if setting is None:
                    session.add(RuntimeSetting(key=key, value=value))
                else:
                    setting.value = value
        return templates.TemplateResponse(
            request,
            "setup.html",
            context(request, saved=True),
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", context(request))

    @app.post("/login")
    async def login_submit(
        request: Request,
        password: str = Form(),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_csrf(request, csrf)
        with database.session() as session:
            setting = session.get(RuntimeSetting, "admin_password_hash")
        if setting is None or not verify_password(password, setting.value):
            raise HTTPException(401, "invalid credentials")
        request.session["authenticated"] = True
        request.session["csrf"] = csrf_token()
        return RedirectResponse("/dashboard", status_code=303)

    @app.post("/logout")
    async def logout(request: Request, csrf: str = Form()) -> RedirectResponse:
        require_login(request)
        require_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    async def page(request: Request) -> Response:
        if not request.session.get("authenticated"):
            return RedirectResponse("/login", status_code=303)
        page_name = request.url.path.removeprefix("/")
        extra: dict[str, Any] = {}
        with database.session() as session:
            counts = {
                "items": session.scalar(select(func.count()).select_from(ArchiveItem)) or 0,
                "jobs": session.scalar(select(func.count()).select_from(Job)) or 0,
                "audits": session.scalar(select(func.count()).select_from(AuditEvent)) or 0,
            }
            if page_name in {"archive", "quarantine", "restores"}:
                query = select(ArchiveItem).order_by(ArchiveItem.id.desc())
                if page_name == "quarantine":
                    query = query.where(ArchiveItem.state == ArchiveState.QUARANTINED)
                extra["items"] = session.scalars(query).all()
            if page_name == "audit":
                extra["events"] = session.scalars(
                    select(AuditEvent).order_by(AuditEvent.id.desc()).limit(200)
                ).all()
            if page_name == "jobs":
                extra["job_rows"] = session.scalars(
                    select(Job).order_by(Job.id.desc()).limit(200)
                ).all()
        if page_name == "sources":
            workflow = build_local_workflow(settings, policy=archive_policy())
            discovery = await workflow.source.discover()
            extra["preview"] = [
                {
                    "object": item,
                    "decision": evaluate_policy(item, workflow.policy),
                }
                for item in discovery.objects
            ]
        if page_name in {"dashboard", "storage", "diagnostics"}:
            health = startup_checks(
                database,
                settings.active_root,
                settings.backup_root,
                settings.staging_root,
                smart_health_file=settings.smart_health_file,
                require_smart_health=settings.require_smart_health,
                recover_expired_jobs=False,
            )
            extra["health"] = health
        if page_name == "storage":
            extra["sftpgo_healthy"] = await SFTPGoAPIClient(
                settings.sftpgo_endpoint_url,
                settings.sftpgo_api_key_file,
            ).health()
        if page_name == "policies":
            extra["policy"] = archive_policy()
        if page_name == "settings":
            extra["deletion_enabled"] = (
                runtime_value(
                    "global_deletion_enabled",
                    "false",
                )
                == "true"
            )
            extra["safety_lock"] = runtime_value("safety_lock", "true") == "true"
        return templates.TemplateResponse(
            request,
            "page.html",
            context(request, page_name=page_name, counts=counts, **extra),
        )

    for page_name in PAGES:
        app.add_api_route(
            f"/{page_name}",
            page,
            methods=["GET"],
            response_class=HTMLResponse,
            name=page_name,
        )

    @app.post("/policies")
    async def save_policy(
        request: Request,
        archive_after_days: int = Form(ge=0, le=36500),
        stability_window_hours: int = Form(ge=0, le=8760),
        quarantine_days: int = Form(ge=1, le=3650),
        minimum_file_size: int = Form(ge=0),
        maximum_file_size: str = Form(""),
        dry_run: bool = Form(False),
        manual_approval: bool = Form(False),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_login(request)
        require_csrf(request, csrf)
        maximum_size: int | None = None
        if maximum_file_size.strip():
            try:
                maximum_size = int(maximum_file_size)
            except ValueError as error:
                raise HTTPException(422, "maximum file size must be an integer") from error
            if maximum_size < 1:
                raise HTTPException(422, "maximum file size must be positive")
        policy = SourcePolicy(
            archive_after_days=archive_after_days,
            stability_window_hours=stability_window_hours,
            quarantine_days=quarantine_days,
            minimum_file_size=minimum_file_size,
            maximum_file_size=maximum_size,
            dry_run=dry_run,
            manual_approval=manual_approval,
            deletion_enabled=False,
        )
        set_runtime_value("archive_policy", policy.model_dump_json())
        return RedirectResponse("/policies?saved=1", status_code=303)

    @app.post("/sources/archive")
    async def archive_source(request: Request, csrf: str = Form()) -> RedirectResponse:
        require_login(request)
        require_csrf(request, csrf)
        policy = archive_policy()
        if policy.dry_run:
            raise HTTPException(409, "disable dry-run before starting an archive")
        workflow = build_local_workflow(settings, policy=policy)
        with database.session() as session:
            items = await workflow.discover(session)
            for item in items:
                if item.state in {
                    ArchiveState.ELIGIBLE,
                    ArchiveState.QUEUED,
                    ArchiveState.DOWNLOADING,
                    ArchiveState.LOCAL_STAGED,
                    ArchiveState.LOCAL_VERIFIED,
                    ArchiveState.LOCAL_COMMITTED,
                    ArchiveState.LOCAL_BACKUP_PENDING,
                    ArchiveState.LOCAL_BACKUP_VERIFIED,
                    ArchiveState.COLD_UPLOAD_PENDING,
                    ArchiveState.COLD_UPLOADED,
                    ArchiveState.COLD_VERIFIED,
                }:
                    await workflow.archive(session, item)
        return RedirectResponse("/archive", status_code=303)

    @app.post("/restores/{item_id}/{copy_name}")
    async def test_restore(
        request: Request,
        item_id: int,
        copy_name: str,
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_login(request)
        require_csrf(request, csrf)
        if copy_name not in {"local", "remote"}:
            raise HTTPException(404, "unknown restore copy")
        workflow = build_local_workflow(settings, policy=archive_policy())
        destination_root = settings.backup_root / "mnema-restore-tests"
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / (
            f"item-{item_id}-{copy_name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
        )
        with database.session() as session:
            item = session.get(ArchiveItem, item_id)
            if item is None:
                raise HTTPException(404, "archive item not found")
            if copy_name == "local":
                verified = await workflow.restore_local(item, destination)
            else:
                verified = await workflow.restore_remote(item, destination)
            item.restore_test_status = (
                f"{copy_name}:verified:{datetime.now(UTC).isoformat()}"
                if verified
                else f"{copy_name}:failed:{datetime.now(UTC).isoformat()}"
            )
            session.add(
                AuditEvent(
                    archive_item_id=item.id,
                    event_type="restore_test",
                    from_state=item.state,
                    to_state=item.state,
                    actor="administrator",
                    details={
                        "copy": copy_name,
                        "verified": verified,
                        "destination": str(destination),
                    },
                )
            )
            if not verified:
                raise HTTPException(409, "restore hash verification failed")
        return RedirectResponse("/restores?verified=1", status_code=303)

    @app.post("/settings/pause-deletion")
    async def emergency_pause(request: Request, csrf: str = Form()) -> RedirectResponse:
        require_login(request)
        require_csrf(request, csrf)
        set_runtime_value("global_deletion_enabled", "false")
        set_runtime_value("safety_lock", "true")
        with database.session() as session:
            session.add(
                AuditEvent(
                    event_type="emergency_pause",
                    actor="administrator",
                    details={"global_deletion_enabled": False, "safety_lock": True},
                )
            )
        return RedirectResponse("/settings?paused=1", status_code=303)

    return app


app = create_app()
