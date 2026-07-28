from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mnema.adapters.nas.sftpgo import SFTPGoAPIClient


class RecordingSFTPGo(SFTPGoAPIClient):
    def __init__(self, tmp_path: Path, responses: list[tuple[int, bytes]]) -> None:
        key_file = tmp_path / "api-key"
        key_file.write_text("test-key", encoding="utf-8")
        super().__init__("http://sftpgo:8080", key_file)
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, bytes]:
        self.requests.append((method, path, payload))
        return self.responses.pop(0)


@pytest.fixture
def inline_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    async def inline(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)


@pytest.mark.asyncio
async def test_create_user_is_idempotent(
    tmp_path: Path,
    inline_threads: None,
) -> None:
    client = RecordingSFTPGo(tmp_path, [(200, b"{}")])

    await client.create_user("jose", "temporary-password", "/srv/mnema-active")

    assert client.requests == [("GET", "/api/v2/users/jose", None)]


@pytest.mark.asyncio
async def test_create_user_limits_home_to_active_mount(
    tmp_path: Path,
    inline_threads: None,
) -> None:
    client = RecordingSFTPGo(tmp_path, [(404, b""), (201, b"{}")])

    await client.create_user("jose", "temporary-password", "/srv/mnema-active")

    payload = client.requests[1][2]
    assert payload
    assert payload["home_dir"] == "/srv/mnema-active"
    assert payload["filesystem"] == {"provider": 0}


@pytest.mark.asyncio
async def test_create_user_rejects_unsafe_input(
    tmp_path: Path,
    inline_threads: None,
) -> None:
    client = RecordingSFTPGo(tmp_path, [])

    with pytest.raises(ValueError):
        await client.create_user("../escape", "temporary-password", "/srv/mnema-active")
    with pytest.raises(ValueError):
        await client.create_user("jose", "temporary-password", "/data/backup")
