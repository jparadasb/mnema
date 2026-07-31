from __future__ import annotations

from typing import Any, Protocol

import anyio
import jwt
from jwt import PyJWKClient
from starlette.responses import PlainTextResponse


class AccessTokenValidator(Protocol):
    def validate(self, token: str) -> dict[str, Any]: ...


class CloudflareAccessValidator:
    def __init__(self, team_domain: str, audience: str) -> None:
        self.team_domain = team_domain.rstrip("/")
        self.audience = audience
        self.jwks = PyJWKClient(
            f"{self.team_domain}/cdn-cgi/access/certs",
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=300,
            timeout=10,
        )

    def validate(self, token: str) -> dict[str, Any]:
        signing_key = self.jwks.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.team_domain,
            leeway=30,
            options={"require": ["aud", "exp", "iat", "iss", "sub", "type"]},
        )
        if not isinstance(payload, dict):
            raise jwt.InvalidTokenError("Cloudflare Access payload is not an object")
        if payload.get("type") != "app":
            raise jwt.InvalidTokenError("Cloudflare Access token is not an application token")
        return payload


class CloudflareAccessMiddleware:
    def __init__(self, app: Any, validator: AccessTokenValidator) -> None:
        self.app = app
        self.validator = validator

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        token = headers.get(b"cf-access-jwt-assertion", b"").decode(
            "ascii",
            errors="ignore",
        )
        if not token:
            await self._forbidden(scope, receive, send)
            return
        try:
            await anyio.to_thread.run_sync(self.validator.validate, token)
        except Exception:
            await self._forbidden(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _forbidden(scope: Any, receive: Any, send: Any) -> None:
        response = PlainTextResponse("Cloudflare Access authentication required", status_code=403)
        await response(scope, receive, send)
