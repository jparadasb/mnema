from pathlib import Path

from mnema.config import Settings
from mnema.telegram_bot import TelegramAPI, TelegramBot, _pairing_link


class StubAPI(TelegramAPI):
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        self.messages.append((chat_id, text))


def test_pairing_link_uses_mnema_scheme_and_encoded_server() -> None:
    settings = Settings(
        file_provider_enabled=True,
        file_provider_public_url="https://files.example.com",
        telegram_allowed_user_ids=(42,),
    )
    link = _pairing_link(settings, "one-time", "Hydra")
    assert link.startswith("mnema://pair?")
    assert "server=https%3A%2F%2Ffiles.example.com" in link
    assert "code=one-time" in link


def test_telegram_authorization_requires_private_allowlisted_chat(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'mnema.sqlite'}",
        telegram_enabled=True,
        telegram_allowed_user_ids=(42,),
    )
    from mnema.jobs import Database

    database = Database(settings.database_url)
    database.create_schema()
    try:
        bot = TelegramBot(settings, database, StubAPI())
        private = {"message": {"chat": {"type": "private"}, "from": {"id": 42}}}
        group = {"message": {"chat": {"type": "group"}, "from": {"id": 42}}}
        unknown = {"message": {"chat": {"type": "private"}, "from": {"id": 99}}}
        assert bot.authorized(private)
        assert not bot.authorized(group)
        assert not bot.authorized(unknown)
    finally:
        database.close()
