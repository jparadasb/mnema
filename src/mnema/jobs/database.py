from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from mnema.jobs.models import Base


def create_database_engine(url: str) -> Engine:
    engine = create_engine(url, future=True)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_database_engine(url)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        columns = {column["name"] for column in inspect(self.engine).get_columns("archive_items")}
        if "cold_archived_at" not in columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE archive_items ADD COLUMN cold_archived_at DATETIME")
                )
                connection.execute(
                    text(
                        "UPDATE archive_items SET cold_archived_at = ("
                        "SELECT MAX(created_at) FROM audit_events "
                        "WHERE audit_events.archive_item_id = archive_items.id "
                        "AND audit_events.to_state = 'COLD_ARCHIVED')"
                    )
                )

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def integrity_check(self) -> bool:
        with self.engine.connect() as connection:
            result = connection.execute(text("PRAGMA integrity_check")).scalar_one()
            return bool(result == "ok")

    def close(self) -> None:
        self.engine.dispose()

    def __del__(self) -> None:
        self.close()
