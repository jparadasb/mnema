from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from mnema.config import Settings
from mnema.file_provider.auth import (
    exchange_pairing_code,
    rotate_refresh_token,
    secret_key,
    verify_access_token,
)
from mnema.file_provider.service import (
    ROOT_ID,
    bootstrap_roots,
    create_upload,
    decode_cursor,
    encode_cursor,
    project_verified_archives,
    seal_upload,
    upload_path,
)
from mnema.jobs import Database
from mnema.jobs.models import (
    ArchiveItem,
    FileProviderChange,
    FileProviderItem,
    FileProviderItemKind,
    FileProviderItemStatus,
    FileProviderUpload,
    FileProviderUploadStatus,
    RuntimeSetting,
)


class PairRequest(BaseModel):
    code: str = Field(min_length=20, max_length=256)
    device_name: str = Field(min_length=1, max_length=100)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class UploadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    content_type: str = Field(default="application/octet-stream", max_length=255)
    sha256: str | None = None


def _item_payload(item: FileProviderItem) -> dict[str, Any]:
    capabilities = ["read", "enumerate"] if item.kind == FileProviderItemKind.FOLDER else ["read"]
    if item.id == "inbox":
        capabilities.append("addFile")
    return {
        "id": item.id,
        "parentId": item.parent_id,
        "name": item.name,
        "kind": item.kind.value.lower(),
        "size": item.size,
        "contentType": item.content_type,
        "contentVersion": item.content_version,
        "metadataVersion": str(item.metadata_version),
        "modifiedAt": item.modified_at.isoformat(),
        "status": item.status.value.lower(),
        "error": item.error_message,
        "capabilities": capabilities,
    }


def _bearer(value: str | None) -> str:
    if value is None or not value.startswith("Bearer "):
        raise HTTPException(401, "bearer token required")
    return value.removeprefix("Bearer ").strip()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _file_chunks(path: Path, start: int, length: int) -> Iterator[bytes]:
    remaining = length
    with path.open("rb") as stream:
        stream.seek(start)
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _content_range(value: str | None, size: int) -> tuple[int, int, int]:
    if value is None:
        return 0, size - 1, 200
    if not value.startswith("bytes=") or "," in value:
        raise HTTPException(416, "unsupported byte range")
    start_text, separator, end_text = value.removeprefix("bytes=").partition("-")
    if separator != "-" or not start_text.isdigit():
        raise HTTPException(416, "invalid byte range")
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start >= size or end < start or end >= size:
        raise HTTPException(416, "byte range is outside file")
    return start, end, 206


def create_file_provider_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    database = Database(settings.database_url)
    key = secret_key(settings.secret_key_file)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        database.create_schema()
        with database.session() as session:
            bootstrap_roots(session)
            project_verified_archives(session)
        yield
        database.close()

    app = FastAPI(
        title="Mnema File Provider API",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.database = database
    app.state.settings = settings

    def authorize(authorization: Annotated[str | None, Header()] = None) -> str:
        with database.session() as session:
            device = verify_access_token(session, _bearer(authorization), key)
            return device.id

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {
            "status": "ok"
            if settings.file_provider_enabled and database.integrity_check()
            else "disabled"
        }

    @app.post("/v1/auth/pair")
    async def pair(payload: PairRequest) -> dict[str, Any]:
        try:
            with database.session() as session:
                tokens = exchange_pairing_code(
                    session, code=payload.code, device_name=payload.device_name, key=key
                )
        except PermissionError as error:
            raise HTTPException(401, str(error)) from error
        return {
            "accessToken": tokens.access_token,
            "refreshToken": tokens.refresh_token,
            "expiresIn": tokens.expires_in,
            "deviceId": tokens.device_id,
        }

    @app.post("/v1/auth/refresh")
    async def refresh(payload: RefreshRequest) -> dict[str, Any]:
        try:
            with database.session() as session:
                tokens = rotate_refresh_token(session, refresh=payload.refresh_token, key=key)
        except PermissionError as error:
            raise HTTPException(401, str(error)) from error
        return {
            "accessToken": tokens.access_token,
            "refreshToken": tokens.refresh_token,
            "expiresIn": tokens.expires_in,
            "deviceId": tokens.device_id,
        }

    @app.get("/v1/account", dependencies=[Depends(authorize)])
    async def account() -> dict[str, str]:
        with database.session() as session:
            generation = session.get(RuntimeSetting, "file_provider_generation")
            if generation is None:
                raise HTTPException(503, "File Provider generation is unavailable")
            return {"rootItemId": ROOT_ID, "generation": generation.value}

    @app.get("/v1/items/{item_id}", dependencies=[Depends(authorize)])
    async def item(item_id: str) -> dict[str, Any]:
        with database.session() as session:
            found = session.get(FileProviderItem, item_id)
            if found is None:
                raise HTTPException(404, "item not found")
            return _item_payload(found)

    @app.get("/v1/items/{item_id}/children", dependencies=[Depends(authorize)])
    async def children(item_id: str, limit: int = 200, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        with database.session() as session:
            if session.get(FileProviderItem, item_id) is None:
                raise HTTPException(404, "parent item not found")
            rows = session.scalars(
                select(FileProviderItem)
                .where(FileProviderItem.parent_id == item_id)
                .order_by(FileProviderItem.name, FileProviderItem.id)
                .offset(offset)
                .limit(limit + 1)
            ).all()
            more = len(rows) > limit
            rows = rows[:limit]
            return {
                "items": [_item_payload(row) for row in rows],
                "nextOffset": offset + limit if more else None,
            }

    @app.get("/v1/items/{item_id}/content", dependencies=[Depends(authorize)])
    async def content(
        item_id: str, range_header: Annotated[str | None, Header(alias="Range")] = None
    ) -> Response:
        with database.session() as session:
            found = session.get(FileProviderItem, item_id)
            if found is None or found.archive_item_id is None:
                raise HTTPException(404, "file content not found")
            archive = session.get(ArchiveItem, found.archive_item_id)
            if (
                archive is None
                or archive.nas_path is None
                or found.status != FileProviderItemStatus.READY
            ):
                raise HTTPException(409, "file is not ready")
            path = Path(archive.nas_path)
            resolved = path.resolve(strict=True)
            if path.is_symlink() or not resolved.is_relative_to(settings.active_root.resolve()):
                raise HTTPException(409, "recorded content path is unsafe")
            size = resolved.stat().st_size
            start, end, status = _content_range(range_header, size)
            headers = {
                "Accept-Ranges": "bytes",
                "ETag": f'"{found.content_version}"',
                "Content-Length": str(end - start + 1),
            }
            if status == 206:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            return StreamingResponse(
                _file_chunks(resolved, start, end - start + 1),
                status_code=status,
                media_type=found.content_type,
                headers=headers,
            )

    @app.get("/v1/changes", dependencies=[Depends(authorize)])
    async def changes(cursor: str | None = None, limit: int = 500) -> dict[str, Any]:
        limit = max(1, min(limit, 1000))
        with database.session() as session:
            generation_row = session.get(RuntimeSetting, "file_provider_generation")
            if generation_row is None:
                raise HTTPException(503, "File Provider generation is unavailable")
            generation = generation_row.value
            sequence = 0
            if cursor:
                try:
                    cursor_generation, sequence = decode_cursor(cursor)
                except ValueError as error:
                    raise HTTPException(400, str(error)) from error
                if cursor_generation != generation:
                    return {
                        "changes": [],
                        "nextCursor": encode_cursor(generation, 0),
                        "resetRequired": True,
                        "more": False,
                    }
            rows = session.scalars(
                select(FileProviderChange)
                .where(FileProviderChange.sequence > sequence)
                .order_by(FileProviderChange.sequence)
                .limit(limit + 1)
            ).all()
            more = len(rows) > limit
            rows = rows[:limit]
            payload = []
            for row in rows:
                changed = session.get(FileProviderItem, row.item_id)
                payload.append(
                    {
                        "operation": row.operation.lower(),
                        "itemId": row.item_id,
                        "item": _item_payload(changed) if changed is not None else None,
                    }
                )
            last = rows[-1].sequence if rows else sequence
            return {
                "changes": payload,
                "nextCursor": encode_cursor(generation, last),
                "resetRequired": False,
                "more": more,
            }

    @app.post("/v1/uploads", dependencies=[Depends(authorize)], status_code=201)
    async def begin_upload(payload: UploadRequest) -> dict[str, Any]:
        try:
            with database.session() as session:
                upload = create_upload(
                    session,
                    settings,
                    name=payload.name,
                    size=payload.size,
                    content_type=payload.content_type,
                    sha256=payload.sha256,
                )
                return {
                    "uploadId": upload.id,
                    "itemId": upload.item_id,
                    "offset": 0,
                    "expiresAt": upload.expires_at.isoformat(),
                }
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error

    @app.patch("/v1/uploads/{upload_id}", dependencies=[Depends(authorize)])
    async def append_upload(
        upload_id: str,
        request: Request,
        upload_offset: Annotated[int, Header(alias="Upload-Offset", ge=0)],
    ) -> dict[str, int]:
        with database.session() as session:
            upload = session.get(FileProviderUpload, upload_id)
            if upload is None:
                raise HTTPException(404, "upload not found")
            if upload.status != FileProviderUploadStatus.OPEN or _aware(
                upload.expires_at
            ) <= datetime.now(UTC):
                raise HTTPException(409, "upload is closed or expired")
            if upload.received_size != upload_offset:
                raise HTTPException(409, f"expected offset {upload.received_size}")
            path = upload_path(settings, upload.id)
            if path.exists() and (path.is_symlink() or path.stat().st_size != upload_offset):
                raise HTTPException(409, "upload staging state is inconsistent")
            mode = "ab" if path.exists() else "xb"
            written = 0
            try:
                with path.open(mode) as output:
                    async for chunk in request.stream():
                        if upload_offset + written + len(chunk) > upload.expected_size:
                            raise HTTPException(413, "upload exceeds declared size")
                        output.write(chunk)
                        written += len(chunk)
                    output.flush()
                    import os

                    os.fsync(output.fileno())
            except Exception:
                if path.exists() and not path.is_symlink():
                    with path.open("r+b") as output:
                        output.truncate(upload_offset)
                        output.flush()
                        import os

                        os.fsync(output.fileno())
                raise
            upload.received_size += written
            return {"offset": upload.received_size}

    @app.post("/v1/uploads/{upload_id}/complete", dependencies=[Depends(authorize)])
    async def complete_upload(upload_id: str) -> dict[str, Any]:
        with database.session() as session:
            upload = session.get(FileProviderUpload, upload_id)
            if upload is None:
                raise HTTPException(404, "upload not found")
            if upload.status == FileProviderUploadStatus.SEALED:
                return {"itemId": upload.item_id, "status": "processing"}
            if (
                upload.status != FileProviderUploadStatus.OPEN
                or upload.received_size != upload.expected_size
            ):
                raise HTTPException(409, "upload is incomplete")
            upload.status = FileProviderUploadStatus.SEALING
        try:
            with database.session() as session:
                upload = session.get(FileProviderUpload, upload_id)
                if upload is None:
                    raise RuntimeError("upload disappeared while sealing")
                seal_upload(session, settings, upload)
                return {"itemId": upload.item_id, "status": "processing"}
        except (ValueError, OSError, RuntimeError) as error:
            raise HTTPException(409, str(error)) from error

    return app
