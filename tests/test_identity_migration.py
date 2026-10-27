from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from dafeiyu.repositories.sqlite import SCHEMA_VERSION, Database
from dafeiyu.tools.migrate import migrate

# Frozen schema-2 source fixture, taken from the pre-change reviewed main SHA.
SCHEMA2 = """BEGIN IMMEDIATE;
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT UNIQUE NOT NULL CHECK(length(chat_id) > 0),
    title TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    summary_until_id INTEGER NOT NULL DEFAULT 0,
    compress_fail_count INTEGER NOT NULL DEFAULT 0,
    last_compress_attempt REAL NOT NULL DEFAULT 0,
    last_update_ts REAL NOT NULL DEFAULT 0,
    last_social_ts REAL NOT NULL DEFAULT 0,
    last_reply_ts REAL NOT NULL DEFAULT 0,
    last_proactive_ts REAL NOT NULL DEFAULT 0,
    last_dream_date TEXT NOT NULL DEFAULT '',
    last_diary_date TEXT NOT NULL DEFAULT '',
    last_proactive_topic TEXT NOT NULL DEFAULT '',
    last_proactive_answered INTEGER NOT NULL DEFAULT 1 CHECK(last_proactive_answered IN (0,1))
);
CREATE TABLE user_names (
    open_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    ts REAL NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    sender_open_id TEXT NOT NULL DEFAULT '',
    is_observer INTEGER NOT NULL DEFAULT 0 CHECK(is_observer IN (0,1)),
    source TEXT NOT NULL DEFAULT 'direct' CHECK(source IN ('direct','observer','proactive','dream','diary','manual','imported')),
    reply_mode TEXT NOT NULL DEFAULT '',
    meme_tag TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id,id);
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    kind TEXT NOT NULL CHECK(kind IN ('reality','hearsay','imagination')),
    when_text TEXT NOT NULL DEFAULT '',
    who TEXT NOT NULL DEFAULT '',
    what TEXT NOT NULL,
    vibe TEXT NOT NULL DEFAULT '',
    hooks TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    source_until_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_memories_conversation ON memories(conversation_id,id);
CREATE TABLE memory_embeddings (
    memory_id INTEGER PRIMARY KEY REFERENCES memories(id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK(dimension > 0),
    content TEXT NOT NULL,
    vec BLOB NOT NULL,
    updated_at REAL NOT NULL,
    CHECK(length(vec) = dimension * 4)
);
CREATE TABLE style_embeddings (
    example_id TEXT NOT NULL,
    embedding_type TEXT NOT NULL CHECK(embedding_type IN ('catchphrase','example_card')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK(dimension > 0),
    vec BLOB NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(example_id,provider,model),
    CHECK(length(vec) = dimension * 4)
);
CREATE TABLE event_dedup (event_id TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE sys_cache (key TEXT PRIMARY KEY, val TEXT NOT NULL, expires_at REAL);
CREATE TABLE published_cards (
    card_id TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    message_id TEXT NOT NULL,
    content TEXT NOT NULL,
    image_key TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
PRAGMA user_version = 2;
COMMIT;
"""


def schema2_fixture(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA2)
        conn.executemany(
            "INSERT INTO conversations(id,chat_id,title,created_at,summary_until_id) VALUES(?,?,?,?,?)",
            [(1, "group-one", "Group one", 10.0, 2), (2, "group-two", "Group two", 11.0, 0)],
        )
        conn.executemany(
            "INSERT INTO messages(id,conversation_id,role,content,ts,sender_name,sender_open_id,source,is_observer) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (1, 1, "user", "小明说他喜欢茶", 12.5, "同名", "user-a", "observer", 1),
                (2, 1, "assistant", "梦见同名成为鲸鱼", 13.5, "", "", "dream", 0),
                (3, 2, "user", "只在第二群提及", 14.5, "同名", "user-b", "direct", 0),
            ],
        )
        conn.executemany(
            "INSERT INTO memories(id,conversation_id,kind,who,what,created_at,source_until_id) VALUES(?,?,?,?,?,?,?)",
            [(1, 1, "hearsay", "小明", "喜欢茶", 15.0, 1),
             (2, 1, "imagination", "同名", "成为鲸鱼", 16.0, 2)],
        )
        conn.execute("INSERT INTO user_names VALUES('user-a','旧名',11.0)")
        conn.execute("INSERT INTO sys_cache VALUES('credential-cache','fixture-secret',NULL)")
        conn.execute("INSERT INTO event_dedup VALUES('event-one',12.5)")
        conn.execute(
            "INSERT INTO published_cards VALUES('card-one',1,'feishu-one','梦见同名成为鲸鱼','img-one',13.5)"
        )
        vector = struct.pack("<2f", 0.25, 0.75)
        conn.execute(
            "INSERT INTO memory_embeddings VALUES(1,'fixture','fixture-model',2,'喜欢茶',?,15.0)",
            (vector,),
        )
        conn.execute(
            "INSERT INTO style_embeddings VALUES('style-one','example_card','fixture','fixture-model','content-hash',2,?,15.0)",
            (vector,),
        )


class IdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = Path(self.tmp.name) / "schema2.db"
        self.target = Path(self.tmp.name) / "schema3.db"
        schema2_fixture(self.source)
        self.before = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema2_preserved_but_never_guesses_identity_for_old_cards(self):
        report = migrate(self.source, self.target)
        self.assertEqual(report["source_schema_version"], 2)
        self.assertEqual(report["schema_version"], SCHEMA_VERSION)
        self.assertEqual(report["memory_vectors_to_rebuild"], 1)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)
        self.assertNotIn("fixture-secret", json.dumps(report))
        with Database(self.target).connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sys_cache").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT name FROM user_names").fetchone()[0], "旧名")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM identity_aliases").fetchone()[0], 0)
            rows = conn.execute("SELECT subject_ids,reporter_ids,evidence,origin FROM memories").fetchall()
            self.assertEqual([tuple(row) for row in rows], [("[]", "[]", "[]", "legacy")] * 2)
            with closing(sqlite3.connect(self.source)) as old:
                for table, details in report["tables"].items():
                    columns = [r[1] for r in old.execute(f"PRAGMA table_info({table})")]
                    actual = conn.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
                    expected = old.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
                    self.assertEqual([tuple(row) for row in actual], expected, table)
                    self.assertEqual(details["source_count"], details["target_count"])
                    self.assertEqual(len(details["sha256"]), 64)
        Database(self.target).init_db()

    def test_existing_destination_and_source_are_never_overwritten(self):
        self.target.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            migrate(self.source, self.target)
        with self.assertRaises(ValueError):
            migrate(self.source, self.source)
        self.assertEqual(self.target.read_bytes(), b"keep")
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)

    def test_runtime_rejects_schema2_without_silent_in_place_migration(self):
        with self.assertRaisesRegex(RuntimeError, "not schema"):
            Database(self.source).init_db()
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)

    def test_cross_conversation_marker_rolls_back_and_leaves_unready_target(self):
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("UPDATE conversations SET summary_until_id=3 WHERE id=1")
        with self.assertRaisesRegex(RuntimeError, "marker"):
            migrate(self.source, self.target)
        with Database(self.target).connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertNotEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        with self.assertRaisesRegex(RuntimeError, "not schema"):
            Database(self.target).init_db()

    def test_foreign_key_corruption_is_rejected_before_target_creation(self):
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("UPDATE memory_embeddings SET memory_id=999")
        with self.assertRaisesRegex(ValueError, "foreign-key"):
            migrate(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_backfill_failure_rolls_back_all_data_and_stays_unready(self):
        from dafeiyu.tools import migrate as migration

        original = migration._insert

        def broken_insert(conn, table, records):
            original(conn, table, records)
            if table == "messages":
                raise OSError("fixture disk error")

        with patch.object(migration, "_insert", side_effect=broken_insert):
            with self.assertRaises(OSError):
                migrate(self.source, self.target)
        with Database(self.target).connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
            self.assertNotEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        with self.assertRaisesRegex(RuntimeError, "not schema"):
            Database(self.target).init_db()
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)

    def test_unknown_schema_version_is_not_accepted(self):
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(ValueError, "schema-1 or schema-2"):
            migrate(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_deleted_id_high_water_marks_are_preserved(self):
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("UPDATE sqlite_sequence SET seq=999 WHERE name='messages'")
        report = migrate(self.source, self.target)
        self.assertEqual(report["autoincrement_high_water_marks"]["messages"], 999)
        with Database(self.target).connect() as conn:
            row = conn.execute(
                "INSERT INTO messages(conversation_id,role,content,ts) VALUES(1,'user','new',20.0) RETURNING id"
            ).fetchone()
            self.assertEqual(row[0], 1000)

    def test_current_schema_cannot_be_accidentally_migrated_again(self):
        migrate(self.source, self.target)
        third = Path(self.tmp.name) / "third.db"
        with self.assertRaisesRegex(ValueError, "schema-1 or schema-2"):
            migrate(self.target, third)
        self.assertFalse(third.exists())

    def test_schema_initialization_failure_cannot_look_like_pristine_runtime_db(self):
        broken_schema = (
            "BEGIN IMMEDIATE; CREATE TABLE partial(id INTEGER); INVALID SQL; "
            f"PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;"
        )
        with patch("dafeiyu.tools.migrate.Path.read_text", return_value=broken_schema):
            with self.assertRaises(sqlite3.OperationalError):
                migrate(self.source, self.target)
        with self.assertRaisesRegex(RuntimeError, "not schema"):
            Database(self.target).init_db()
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)
