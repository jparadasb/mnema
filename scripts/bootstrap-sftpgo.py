#!/usr/bin/env python3
"""Bootstrap local SFTPGo admin, scoped API key, and first NAS user."""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TOKEN_PATTERN = re.compile(
    rb'name="_form_token"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="_form_token"'
)


def read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 12:
        raise ValueError(f"secret in {path} is too short")
    return value


def request_json(
    endpoint: str,
    path: str,
    *,
    method: str = "GET",
    authorization: str | None = None,
    api_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | list[Any]]:
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    if api_key:
        headers["X-SFTPGO-API-KEY"] = api_key
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    request = urllib.request.Request(  # noqa: S310 - endpoint validated by main
        f"{endpoint}/api/v2{path}",
        method=method,
        headers=headers,
        data=data,
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - endpoint validated by main
            request,
            timeout=15,
        ) as response:
            body = response.read()
            return response.status, json.loads(body or b"{}")
    except urllib.error.HTTPError as error:
        body = error.read()
        return error.code, json.loads(body or b"{}")


def bootstrap_admin(
    endpoint: str,
    username: str,
    password: str,
) -> None:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(
        f"{endpoint}/web/admin/setup",
        timeout=15,
    ) as response:
        html = response.read()
        final_path = urllib.parse.urlsplit(response.url).path
    if final_path != "/web/admin/setup":
        return
    match = TOKEN_PATTERN.search(html)
    if match is None:
        raise RuntimeError("SFTPGo setup form token was not found")
    form_token = (match.group(1) or match.group(2)).decode()
    form = urllib.parse.urlencode(
        {
            "_form_token": form_token,
            "username": username,
            "password": password,
            "confirm_password": password,
            "description": "Mnema service administrator",
            "language": "en",
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - endpoint validated by main
        f"{endpoint}/web/admin/setup",
        data=form,
        method="POST",
    )
    with opener.open(request, timeout=15) as response:
        if response.status not in {200, 303}:
            raise RuntimeError(f"SFTPGo setup failed with HTTP {response.status}")


def bearer_token(endpoint: str, username: str, password: str) -> str:
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    status, payload = request_json(
        endpoint,
        "/token",
        authorization=f"Basic {credentials}",
    )
    if status != 200 or not isinstance(payload, dict) or "access_token" not in payload:
        raise RuntimeError(f"SFTPGo admin authentication failed with HTTP {status}")
    return f"Bearer {payload['access_token']}"


def enable_api_key_auth(endpoint: str, username: str, bearer: str) -> None:
    encoded = urllib.parse.quote(username, safe="")
    status, payload = request_json(
        endpoint,
        f"/admins/{encoded}",
        authorization=bearer,
    )
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"SFTPGo admin lookup failed with HTTP {status}")
    filters = payload.setdefault("filters", {})
    if not isinstance(filters, dict):
        raise RuntimeError("SFTPGo admin filters have an invalid shape")
    if filters.get("allow_api_key_auth") is True:
        return
    filters["allow_api_key_auth"] = True
    payload.pop("password", None)
    status, _ = request_json(
        endpoint,
        f"/admins/{encoded}",
        method="PUT",
        authorization=bearer,
        payload=payload,
    )
    if status != 200:
        raise RuntimeError(f"SFTPGo admin update failed with HTTP {status}")


def provision_api_key(
    endpoint: str,
    username: str,
    bearer: str,
    output: Path,
) -> str:
    if output.is_file():
        return read_secret(output)
    status, payload = request_json(
        endpoint,
        "/apikeys",
        authorization=bearer,
    )
    if status != 200 or not isinstance(payload, list):
        raise RuntimeError(f"SFTPGo API-key listing failed with HTTP {status}")
    if any(entry.get("name") == "mnema-provisioner" for entry in payload):
        raise RuntimeError("SFTPGo API key exists but local secret file is missing")
    status, created = request_json(
        endpoint,
        "/apikeys",
        method="POST",
        authorization=bearer,
        payload={
            "name": "mnema-provisioner",
            "scope": 1,
            "admin": username,
            "description": "Scoped key for Mnema SFTPGo user provisioning",
        },
    )
    if status != 201 or not isinstance(created, dict) or "key" not in created:
        raise RuntimeError(f"SFTPGo API-key creation failed with HTTP {status}")
    key = str(created["key"])
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(f"{key}\n")
    return key


def provision_user(
    endpoint: str,
    username: str,
    password: str,
    api_key: str,
) -> None:
    encoded = urllib.parse.quote(username, safe="")
    status, existing = request_json(endpoint, f"/users/{encoded}", api_key=api_key)
    desired = {
        "status": 1,
        "username": username,
        "password": password,
        "home_dir": "/srv/mnema-active",
        "permissions": {"/": ["*"]},
        "filesystem": {"provider": 0},
    }
    if status == 200:
        if not isinstance(existing, dict):
            raise RuntimeError("SFTPGo user response has an invalid shape")
        existing.update(desired)
        status, _ = request_json(
            endpoint,
            f"/users/{encoded}?disconnect=1",
            method="PUT",
            api_key=api_key,
            payload=existing,
        )
        if status != 200:
            raise RuntimeError(f"SFTPGo user update failed with HTTP {status}")
        return
    if status != 404:
        raise RuntimeError(f"SFTPGo user lookup failed with HTTP {status}")
    status, _ = request_json(
        endpoint,
        "/users",
        method="POST",
        api_key=api_key,
        payload=desired,
    )
    if status != 201:
        raise RuntimeError(f"SFTPGo user creation failed with HTTP {status}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:8081")
    parser.add_argument("--admin-username", default="mnema-provisioner")
    parser.add_argument("--admin-password-file", required=True, type=Path)
    parser.add_argument("--api-key-file", required=True, type=Path)
    parser.add_argument("--user", required=True)
    parser.add_argument("--user-password-file", required=True, type=Path)
    args = parser.parse_args()
    endpoint = args.endpoint.rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("SFTPGo endpoint must be HTTP or HTTPS")
    admin_password = read_secret(args.admin_password_file)
    user_password = read_secret(args.user_password_file)
    bootstrap_admin(endpoint, args.admin_username, admin_password)
    bearer = bearer_token(endpoint, args.admin_username, admin_password)
    enable_api_key_auth(endpoint, args.admin_username, bearer)
    api_key = provision_api_key(endpoint, args.admin_username, bearer, args.api_key_file)
    provision_user(endpoint, args.user, user_password, api_key)
    print(f"SFTPGo provisioned administrator={args.admin_username} user={args.user}")


if __name__ == "__main__":
    main()
