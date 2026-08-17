from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mnema.config import Settings
from mnema.security.cloudflare import CloudflareAccessValidator
from mnema.web.app import create_app


class FakeAccessValidator:
    def __init__(self, expected: str) -> None:
        self.expected = expected

    def validate(self, token: str) -> dict[str, Any]:
        if token != self.expected:
            raise ValueError("invalid token")
        return {"sub": "test-user"}


@pytest.mark.asyncio
async def test_public_web_fails_closed_without_valid_cloudflare_token(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'cloudflare.sqlite'}",
        secret_key_file=tmp_path / "none",
        cloudflare_access_required=True,
        cloudflare_team_domain="https://mnema.cloudflareaccess.com",
        cloudflare_audience="test-audience",
    )
    application = create_app(
        settings,
        cloudflare_validator=FakeAccessValidator("valid-assertion"),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=("172.20.0.9", 51234)),
        base_url="https://admin.example.com",
    ) as client:
        missing = await client.get("/healthz")
        invalid = await client.get(
            "/healthz",
            headers={"Cf-Access-Jwt-Assertion": "invalid"},
        )
        valid = await client.get(
            "/healthz",
            headers={"Cf-Access-Jwt-Assertion": "valid-assertion"},
        )
        protected = await client.get("/setup")

    assert missing.status_code == 403
    assert invalid.status_code == 403
    assert valid.status_code == 200
    assert protected.status_code == 403


@pytest.mark.asyncio
async def test_public_web_answers_the_container_healthcheck_over_loopback(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'cloudflare-probe.sqlite'}",
        secret_key_file=tmp_path / "none",
        cloudflare_access_required=True,
        cloudflare_team_domain="https://mnema.cloudflareaccess.com",
        cloudflare_audience="test-audience",
    )
    application = create_app(
        settings,
        cloudflare_validator=FakeAccessValidator("valid-assertion"),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=("127.0.0.1", 51234)),
        base_url="http://127.0.0.1:8080",
    ) as client:
        health = await client.get("/healthz")
        other = await client.get("/setup")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    # The exemption is scoped to the probe endpoint, not to loopback callers.
    assert other.status_code == 403


@pytest.mark.asyncio
async def test_local_web_does_not_require_cloudflare_token(tmp_path: Path) -> None:
    application = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'local.sqlite'}",
            secret_key_file=tmp_path / "none",
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://localhost",
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200


def test_cloudflare_validator_checks_signature_issuer_audience_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    validator = CloudflareAccessValidator(
        "https://mnema.cloudflareaccess.com",
        "expected-audience",
    )
    monkeypatch.setattr(
        validator.jwks,
        "get_signing_key_from_jwt",
        lambda _token: SimpleNamespace(key=private_key.public_key()),
    )
    now = datetime.now(UTC)
    claims = {
        "aud": ["expected-audience"],
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "iss": "https://mnema.cloudflareaccess.com",
        "sub": "user-id",
        "type": "app",
    }
    valid = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})

    assert validator.validate(valid)["sub"] == "user-id"

    wrong_audience = jwt.encode(
        claims | {"aud": ["other"]},
        private_key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    expired = jwt.encode(
        claims | {"exp": now - timedelta(minutes=1)},
        private_key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    with pytest.raises(jwt.InvalidAudienceError):
        validator.validate(wrong_audience)
    with pytest.raises(jwt.ExpiredSignatureError):
        validator.validate(expired)
