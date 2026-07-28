from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Protocol

USERNAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


class SFTPGoProvisioningClient(Protocol):
    async def health(self) -> bool: ...

    async def create_user(self, username: str, password: str, active_root: str) -> None: ...


class SFTPGoAPIClient:
    """Minimal SFTPGo v2 client using a scoped admin API key."""

    def __init__(
        self,
        endpoint: str,
        api_key_file: Path,
        *,
        timeout_seconds: float = 10,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        parsed_endpoint = urllib.parse.urlsplit(self.endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
            raise ValueError("SFTPGo endpoint must be an HTTP or HTTPS URL")
        self.api_key_file = api_key_file
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-SFTPGO-API-KEY": self.api_key_file.read_text(encoding="utf-8").strip(),
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, bytes]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(  # noqa: S310 - endpoint scheme validated at init
            f"{self.endpoint}{path}",
            data=data,
            headers=self._headers(),
            method=method,
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - endpoint scheme validated at init
                request,
                timeout=self.timeout_seconds,
            ) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    async def health(self) -> bool:
        def request_health() -> int:
            with urllib.request.urlopen(  # noqa: S310 - endpoint scheme validated at init
                f"{self.endpoint}/healthz",
                timeout=self.timeout_seconds,
            ) as response:
                return int(response.status)

        try:
            status = await asyncio.to_thread(request_health)
        except (OSError, ValueError):
            return False
        return status == 200

    async def create_user(self, username: str, password: str, active_root: str) -> None:
        if not USERNAME.fullmatch(username):
            raise ValueError("invalid SFTPGo username")
        if len(password) < 12:
            raise ValueError("SFTPGo password must contain at least 12 characters")
        if active_root != "/srv/mnema-active":
            raise ValueError("SFTPGo user root must be the isolated active-storage mount")
        encoded_username = urllib.parse.quote(username, safe="")
        status, _ = await asyncio.to_thread(
            self._request,
            "GET",
            f"/api/v2/users/{encoded_username}",
        )
        if status == 200:
            return
        if status != 404:
            raise RuntimeError(f"SFTPGo user lookup failed with HTTP {status}")
        status, body = await asyncio.to_thread(
            self._request,
            "POST",
            "/api/v2/users",
            {
                "status": 1,
                "username": username,
                "password": password,
                "home_dir": active_root,
                "permissions": {"/": ["*"]},
                "filesystem": {"provider": 0},
            },
        )
        if status not in {200, 201}:
            message = body.decode(errors="replace")[:200]
            raise RuntimeError(f"SFTPGo user creation failed with HTTP {status}: {message}")
