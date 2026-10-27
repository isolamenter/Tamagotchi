from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3


class Database:
    """The runtime accepts only schema 3; importing old data is an offline task."""

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
                    "consolidation_state",
                    "identity_aliases",
                    "proactive_evaluations",
                }
                if not required <= tables:
                    raise RuntimeError("Incomplete schema-3 database schema")
                required_columns = {
                    "messages": {"source_message_id", "mentions_json", "content_kind", "evidence_text"},
                    "memories": {"subject_ids", "reporter_ids", "evidence", "origin", "profile_key", "confidence", "expires_at", "superseded_by"},
                    "consolidation_state": {"until_id", "next_offset", "target_id", "run_date", "calls_today"},
                }
                for table, columns in required_columns.items():
                    present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if not columns <= present:
                        raise RuntimeError("Incomplete schema-3 database columns")
                return
            if tables or version:
                raise RuntimeError(
                    "Database is not schema 3. Run python -m dafeiyu.tools.migrate with a new destination file first."
                )
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(Path(__file__).with_name("schema.sql").read_text())
