from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2


class Database:
    """The runtime accepts only schema 2; importing old data is an offline task."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version == SCHEMA_VERSION:
                required = {
                    "conversations",
                    "messages",
                    "memories",
                    "memory_embeddings",
                    "style_embeddings",
                    "user_names",
                    "event_dedup",
                    "sys_cache",
                    "published_cards",
                }
                if not required <= tables:
                    raise RuntimeError("Incomplete 2.0 database schema")
                return
            if tables or version:
                raise RuntimeError(
                    "Database is not schema 2. Run python -m dafeiyu.tools.migrate with a new destination file first."
                )
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(Path(__file__).with_name("schema.sql").read_text())
