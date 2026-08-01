from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from mnema.jobs.models import FileProviderDevice, FileProviderPairingCode, utcnow


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    device_id: str


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def secret_key(path: Path) -> str:
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
    else:
        value = os.getenv("MNEMA_SECRET_KEY", "")
    if len(value) < 32:
        raise RuntimeError("File Provider token signing key is unavailable")
    return value


def create_pairing_code(session: Session) -> str:
    code = secrets.token_urlsafe(32)
    session.add(
        FileProviderPairingCode(
            id=str(uuid.uuid4()),
            code_hash=_hash(code),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    return code


def _access_token(key: str, device_id: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": device_id,
            "aud": "mnema-file-provider",
            "iat": now,
            "exp": now + timedelta(minutes=15),
            "scope": "files:read files:upload",
            "type": "access",
        },
        key,
        algorithm="HS256",
    )


def exchange_pairing_code(
    session: Session,
    *,
    code: str,
    device_name: str,
    key: str,
) -> IssuedTokens:
    now = datetime.now(UTC)
    pairing = session.scalar(
        select(FileProviderPairingCode).where(FileProviderPairingCode.code_hash == _hash(code))
    )
    if pairing is None or pairing.consumed_at is not None or _aware(pairing.expires_at) <= now:
        raise PermissionError("pairing code is invalid or expired")
    refresh = secrets.token_urlsafe(48)
    device = FileProviderDevice(
        id=str(uuid.uuid4()),
        name=device_name.strip()[:100] or "iPhone",
        refresh_token_hash=_hash(refresh),
        refresh_expires_at=now + timedelta(days=90),
    )
    pairing.consumed_at = now
    session.add(device)
    session.flush()
    return IssuedTokens(_access_token(key, device.id), refresh, 900, device.id)


def rotate_refresh_token(session: Session, *, refresh: str, key: str) -> IssuedTokens:
    now = datetime.now(UTC)
    device = session.scalar(
        select(FileProviderDevice).where(FileProviderDevice.refresh_token_hash == _hash(refresh))
    )
    if device is None or device.revoked_at is not None or _aware(device.refresh_expires_at) <= now:
        raise PermissionError("refresh token is invalid or expired")
    replacement = secrets.token_urlsafe(48)
    device.refresh_token_hash = _hash(replacement)
    device.refresh_expires_at = now + timedelta(days=90)
    device.last_used_at = now
    return IssuedTokens(_access_token(key, device.id), replacement, 900, device.id)


def verify_access_token(session: Session, token: str, key: str) -> FileProviderDevice:
    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=["HS256"],
            audience="mnema-file-provider",
            options={"require": ["sub", "aud", "iat", "exp", "scope", "type"]},
        )
    except jwt.PyJWTError as error:
        raise PermissionError("access token is invalid") from error
    if payload.get("type") != "access" or payload.get("scope") != "files:read files:upload":
        raise PermissionError("access token scope is invalid")
    device = session.get(FileProviderDevice, str(payload["sub"]))
    if device is None or device.revoked_at is not None:
        raise PermissionError("device is revoked")
    device.last_used_at = utcnow()
    return device


def revoke_device(session: Session, device_id: str) -> bool:
    device = session.get(FileProviderDevice, device_id)
    if device is None:
        return False
    device.revoked_at = utcnow()
    return True
