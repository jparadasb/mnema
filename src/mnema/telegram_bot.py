from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mnema.config import Settings
from mnema.diagnostics.health import startup_checks
from mnema.file_provider.auth import create_pairing_code
from mnema.file_provider.service import project_verified_archives
from mnema.jobs import Database
from mnema.jobs.models import FileProviderDevice

logger = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, payload: Mapping[str, Any], *, timeout: float = 40) -> Any:
        body = json.dumps(dict(payload)).encode("utf-8")
        request = Request(  # noqa: S310 - URL is constructed from a fixed HTTPS base
            f"{self._base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed Telegram URL
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise TelegramAPIError("Telegram API request failed")
        return result.get("result")

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def send_photo(self, chat_id: int, photo: bytes, caption: str) -> Any:
        boundary = f"mnema-{time.monotonic_ns()}"
        parts: list[bytes] = []
        for name, value in (("chat_id", str(chat_id)), ("caption", caption)):
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode(),
                    b"\r\n",
                ]
            )
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="photo"; filename="mnema-pairing.png"\r\n',
                b"Content-Type: image/png\r\n\r\n",
                photo,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = Request(  # noqa: S310 - URL is constructed from a fixed HTTPS base
            f"{self._base_url}/sendPhoto",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urlopen(request, timeout=40) as response:  # noqa: S310 - fixed Telegram URL
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise TelegramAPIError("Telegram API request failed")
        return result.get("result")


def _keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Status", "callback_data": "status"},
                {"text": "Health", "callback_data": "health"},
            ],
            [{"text": "URLs", "callback_data": "urls"}],
            [
                {"text": "Pair device", "callback_data": "pair"},
                {"text": "Devices", "callback_data": "devices"},
            ],
            [
                {"text": "Project archives", "callback_data": "project"},
                {"text": "Dry run", "callback_data": "dry-run"},
            ],
        ]
    }


def _menu_text() -> str:
    return (
        "Mnema administration\n\n"
        "Choose an operation below. Pairing codes expire after ten minutes and can be used once."
    )


def _pairing_link(settings: Settings, code: str, device_name: str) -> str:
    return "mnema://pair?" + urlencode(
        {"server": settings.file_provider_public_url, "code": code, "device": device_name}
    )


def _pairing_qr(link: str) -> bytes:
    import qrcode  # type: ignore[import-untyped]

    image = qrcode.make(link)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class TelegramBot:
    def __init__(self, settings: Settings, database: Database, api: TelegramAPI) -> None:
        self.settings = settings
        self.database = database
        self.api = api
        self.pending_pairing: dict[int, float] = {}

    def authorized(self, update: Mapping[str, Any]) -> bool:
        callback = update.get("callback_query")
        callback_mapping: Mapping[str, Any] = callback if isinstance(callback, Mapping) else {}
        message = update.get("message") or callback_mapping.get("message")
        source_message = update.get("message") or callback_mapping.get("message")
        message_sender = (
            source_message.get("from", {}) if isinstance(source_message, Mapping) else {}
        )
        sender_source = message_sender or callback_mapping.get("from", {})
        sender: Mapping[str, Any] = sender_source if isinstance(sender_source, Mapping) else {}
        return (
            isinstance(message, Mapping)
            and message.get("chat", {}).get("type") == "private"
            and sender.get("id") in self.settings.telegram_allowed_user_ids
        )

    def _status(self) -> str:
        with self.database.session() as session:
            items = session.query(FileProviderDevice).count()
        return f"Mnema is running. Paired devices: {items}. Safety lock remains enabled."

    def _devices(self) -> str:
        with self.database.session() as session:
            devices = (
                session.query(FileProviderDevice)
                .order_by(FileProviderDevice.created_at)
                .all()
            )
            if not devices:
                return "No paired devices."
            return "\n".join(
                f"• {device.name} — {'revoked' if device.revoked_at else 'active'} ({device.id})"
                for device in devices
            )

    def _pair(self, chat_id: int, device_name: str) -> None:
        if not self.settings.file_provider_enabled:
            self.api.send_message(chat_id, "File Provider is disabled in Mnema configuration.")
            return
        with self.database.session() as session:
            code = create_pairing_code(session)
        link = _pairing_link(self.settings, code, device_name)
        self.api.send_message(
            chat_id,
            f"Pairing token for {device_name}:\n\n{code}\n\n"
            "This token expires in 10 minutes and is single-use. "
            "Delete this message after pairing.\n\n"
            f"Open Mnema directly: {link}",
        )
        try:
            self.api.send_photo(chat_id, _pairing_qr(link), "Scan this QR code in Mnema.")
        except (ImportError, OSError, TelegramAPIError):
            logger.warning("Unable to send Telegram pairing QR; text link was sent")

    async def handle(self, update: Mapping[str, Any]) -> None:
        if not self.authorized(update):
            return
        callback = update.get("callback_query")
        if callback:
            callback_id = callback.get("id")
            if callback_id:
                await asyncio.to_thread(
                    self.api.call,
                    "answerCallbackQuery",
                    {"callback_query_id": callback_id},
                )
            chat_id = int(callback["message"]["chat"]["id"])
            action = str(callback.get("data", ""))
            if action == "pair":
                self.pending_pairing[chat_id] = time.monotonic() + 300
                self.api.send_message(
                    chat_id,
                    "Reply with the device name to create a pairing token.",
                )
            else:
                await self._action(chat_id, action)
            return
        message = update.get("message", {})
        chat_id = int(message["chat"]["id"])
        text = str(message.get("text", "")).strip()
        if chat_id in self.pending_pairing:
            expires = self.pending_pairing.pop(chat_id)
            if time.monotonic() <= expires and text and not text.startswith("/"):
                await asyncio.to_thread(self._pair, chat_id, text[:80])
                return
        if text in {"/start", "/help"}:
            self.api.send_message(chat_id, _menu_text(), _keyboard())
        elif text == "/pair":
            self.pending_pairing[chat_id] = time.monotonic() + 300
            self.api.send_message(chat_id, "Reply with the device name to create a pairing token.")
        elif text:
            await self._action(chat_id, text.removeprefix("/").split()[0])

    async def _action(self, chat_id: int, action: str) -> None:
        if action in {"menu", "start", "help"}:
            self.api.send_message(chat_id, _menu_text(), _keyboard())
        elif action == "status":
            self.api.send_message(chat_id, self._status(), _keyboard())
        elif action == "health":
            health = startup_checks(
                self.database,
                self.settings.active_root,
                self.settings.backup_root,
                self.settings.staging_root,
                smart_health_file=self.settings.smart_health_file,
                require_smart_health=self.settings.require_smart_health,
                recover_expired_jobs=False,
            )
            self.api.send_message(
                chat_id,
                f"Health: {'OK' if health.healthy else 'FAILED'}\n"
                f"SQLite: {'OK' if health.sqlite_healthy else 'FAILED'}\n"
                f"Partial files: {len(health.partial_files)}",
                _keyboard(),
            )
        elif action == "urls":
            self.api.send_message(
                chat_id,
                f"File Provider: {self.settings.file_provider_public_url or 'disabled'}",
                _keyboard(),
            )
        elif action == "devices":
            self.api.send_message(chat_id, self._devices(), _keyboard())
        elif action == "project":
            self.api.send_message(
                chat_id,
                "Projecting verified archives changes the File Provider index. Continue?",
                {
                    "inline_keyboard": [
                        [{"text": "Confirm project", "callback_data": "project-confirm"}]
                    ]
                },
            )
        elif action == "project-confirm":
            with self.database.session() as session:
                projected = project_verified_archives(session)
            self.api.send_message(chat_id, f"Projected verified archives: {projected}", _keyboard())
        elif action == "dry-run":
            try:
                from mnema.cli import _test_workflow
                from mnema.policies import evaluate_policy

                workflow = _test_workflow(self.settings)
                page = await workflow.source.discover()
                decisions = [
                    evaluate_policy(item, workflow.policy, now=datetime.now(UTC))
                    for item in page.objects
                ]
                eligible = sum(decision.eligible for decision in decisions)
                self.api.send_message(
                    chat_id,
                    f"Dry run complete: {len(decisions)} discovered, {eligible} eligible.\n"
                    "No files were changed.",
                    _keyboard(),
                )
            except Exception:
                logger.exception("Telegram dry-run failed")
                self.api.send_message(
                    chat_id,
                    "Dry run could not be completed safely.",
                    _keyboard(),
                )
        elif action == "pair":
            self.pending_pairing[chat_id] = time.monotonic() + 300
            self.api.send_message(
                chat_id,
                "Reply with the device name to create a pairing token.",
            )
        else:
            self.api.send_message(
                chat_id,
                "Unknown command. Use /start to open the Mnema menu.",
                _keyboard(),
            )

    async def run(self) -> None:
        offset: int | None = None
        self.api.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Open Mnema administration"},
                    {"command": "pair", "description": "Pair a device"},
                ]
            },
        )
        while True:
            payload: dict[str, Any] = {
                "timeout": 25,
                "allowed_updates": ["message", "callback_query"],
            }
            if offset is not None:
                payload["offset"] = offset
            try:
                updates = await asyncio.to_thread(self.api.call, "getUpdates", payload, timeout=35)
                for update in updates or []:
                    offset = int(update["update_id"]) + 1
                    await self.handle(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Telegram polling cycle failed")
                await asyncio.sleep(5)


async def run_bot() -> None:
    settings = Settings()
    if not settings.telegram_enabled:
        raise RuntimeError("Telegram bot is disabled")
    token = settings.telegram_bot_token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("Telegram bot token file is empty")
    database = Database(settings.database_url)
    database.create_schema()
    try:
        await TelegramBot(settings, database, TelegramAPI(token)).run()
    finally:
        database.close()
