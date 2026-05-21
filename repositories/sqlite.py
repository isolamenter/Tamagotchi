from __future__ import annotations

import sqlite3

from config import AppConfig


class Database:
    def __init__(self, config: AppConfig):
        self.config = config

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

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
                """
            )
            try:
                conn.execute("ALTER TABLE pets ADD COLUMN compress_fail_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE pets ADD COLUMN last_compress_attempt REAL DEFAULT 0.0")
            except sqlite3.OperationalError:
                pass

