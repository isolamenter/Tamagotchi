from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from config import AppConfig


class Database:
    def __init__(self, config: AppConfig):
        self.config = config

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.config.db_path, timeout=10.0)
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
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT UNIQUE NOT NULL,
                    born_at REAL NOT NULL,
                    summary_until_id INTEGER NOT NULL DEFAULT 0,
                    state_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pet_id INTEGER NOT NULL REFERENCES pets(id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts REAL NOT NULL,
                    sender_name TEXT NOT NULL DEFAULT '',
                    sender_open_id TEXT NOT NULL DEFAULT '',
                    is_observer INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_messages_pet ON messages(pet_id, id);

                CREATE TABLE IF NOT EXISTS memory_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pet_id INTEGER NOT NULL REFERENCES pets(id),
                    when_text TEXT NOT NULL DEFAULT '',
                    who TEXT NOT NULL DEFAULT '',
                    what TEXT NOT NULL,
                    vibe TEXT NOT NULL DEFAULT '',
                    hooks TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    source_until_id INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_cards_pet ON memory_cards(pet_id, id);

                CREATE TABLE IF NOT EXISTS embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pet_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    vec BLOB NOT NULL,
                    ts REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_embed_pet ON embeddings(pet_id, kind);
                CREATE INDEX IF NOT EXISTS idx_embed_source ON embeddings(kind, source_id);

                CREATE TABLE IF NOT EXISTS style_embeddings (
                    example_id TEXT NOT NULL,
                    embedding_type TEXT NOT NULL DEFAULT 'catchphrase',
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    vec BLOB NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (example_id, provider, model)
                );

                CREATE INDEX IF NOT EXISTS idx_style_embed_model
                ON style_embeddings(provider, model);

                CREATE TABLE IF NOT EXISTS event_dedup (
                    event_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sys_cache (
                    key TEXT UNIQUE NOT NULL,
                    val TEXT,
                    expires_at REAL
                );

                CREATE TABLE IF NOT EXISTS user_names (
                    open_id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS card_instances (
                    card_id TEXT PRIMARY KEY,
                    pet_id INTEGER NOT NULL REFERENCES pets(id),
                    message_id TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL,
                    need_id TEXT NOT NULL DEFAULT '',
                    need_round INTEGER NOT NULL DEFAULT 0,
                    built_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    max_settlements INTEGER NOT NULL DEFAULT 1,
                    settlement_count INTEGER NOT NULL DEFAULT 0,
                    announced INTEGER NOT NULL DEFAULT 0,
                    base_text TEXT NOT NULL DEFAULT '',
                    action_keys_json TEXT NOT NULL DEFAULT '[]',
                    img_key TEXT NOT NULL DEFAULT '',
                    feedback_lines_json TEXT NOT NULL DEFAULT '[]',
                    version INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_card_instances_pet ON card_instances(pet_id, expires_at);

                CREATE TABLE IF NOT EXISTS card_claims (
                    card_id TEXT NOT NULL REFERENCES card_instances(card_id),
                    actor_open_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    settled_at REAL NOT NULL,
                    state_applied INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (card_id, actor_open_id)
                );
                """
            )
            style_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(style_embeddings)")
            }
            if "embedding_type" not in style_columns:
                # Existing deployments only stored the short-form corpus. Keep
                # those vectors reusable while adding a separate card index.
                conn.execute(
                    "ALTER TABLE style_embeddings ADD COLUMN embedding_type "
                    "TEXT NOT NULL DEFAULT 'catchphrase'"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_style_embed_type "
                "ON style_embeddings(provider, model, embedding_type)"
            )
            try:
                conn.execute("ALTER TABLE pets ADD COLUMN compress_fail_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE pets ADD COLUMN last_compress_attempt REAL DEFAULT 0.0")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN sender_open_id TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass
