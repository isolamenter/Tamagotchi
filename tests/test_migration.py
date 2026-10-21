from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from dafeiyu.repositories.sqlite import Database
from dafeiyu.tools.migrate import migrate


def legacy_fixture(path: Path):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(
            Path(__file__).with_name("fixtures").joinpath("schema_v1.sql").read_text()
        )
        for cid, chat, marker in [(4, "old-chat-a", 12), (9, "old-chat-b", 0)]:
            state = json.dumps(
                {
                    "last_reply_ts": 123.0,
                    "last_proactive_topic": "虚构旧话题",
                    "last_proactive_answered": False,
                    "active_need": {"kind": "hungry"},
                    "satiety": 80,
                }
            )
            conn.execute(
                "INSERT INTO pets(id,chat_id,born_at,summary_until_id,state_json) VALUES(?,?,?,?,?)",
                (cid, chat, 100.0, marker, state),
            )
        for mid, cid, role, text, observer in [
            (11, 4, "user", "虚构旧用户消息", 0),
            (12, 4, "assistant", "虚构旧回答", 0),
            (41, 9, "user", "虚构旧旁听", 1),
        ]:
            conn.execute(
                "INSERT INTO messages(id,pet_id,role,content,ts,sender_name,sender_open_id,is_observer) VALUES(?,?,?,?,?,?,?,?)",
                (mid, cid, role, text, 120.0, "甲", "fixture-open-id", observer),
            )
        for mid, cid, kind in [(2, 4, "reality"), (7, 9, "hearsay"), (8, 4, "imagination")]:
            conn.execute(
                "INSERT INTO memory_cards(id,pet_id,kind,what,created_at) VALUES(?,?,?,?,?)",
                (mid, cid, kind, "虚构旧记忆", 125.0),
            )
        conn.execute("INSERT INTO user_names VALUES('fixture-open-id','甲',100)")
        conn.execute("INSERT INTO event_dedup VALUES('fixture-event',100)")
        conn.execute("INSERT INTO sys_cache VALUES('credential-cache','fixture-secret',NULL)")
        conn.execute(
            "INSERT INTO card_instances(card_id,pet_id,message_id,mode,built_at,expires_at,announced,base_text) VALUES('content',4,'message','content',100,200,1,'虚构旧日记')"
        )
        conn.execute(
            "INSERT INTO card_instances(card_id,pet_id,mode,built_at,expires_at) VALUES('obsolete-need',4,'need',100,200)"
        )


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = Path(self.tmp.name) / "old.db"
        self.target = Path(self.tmp.name) / "v2.db"
        legacy_fixture(self.source)
        self.before = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def test_backfill_preserves_data_and_links_without_old_schema_or_credentials(self):
        report = migrate(self.source, self.target)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)
        self.assertEqual(report["tables"]["messages"]["target_count"], 3)
        self.assertEqual(report["tables"]["memories"]["target_count"], 3)
        self.assertEqual(report["excluded_non_content_or_unpublished_cards"], 1)
        self.assertEqual(report["memory_vectors_to_rebuild"], 3)
        self.assertNotIn("fixture-secret", json.dumps(report))
        with Database(self.target).connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())
            row = conn.execute("SELECT * FROM conversations WHERE id=4").fetchone()
            self.assertEqual(row["summary_until_id"], 12)
            self.assertEqual(row["last_reply_ts"], 123.0)
            self.assertEqual(row["last_proactive_answered"], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT conversation_id,content,source FROM messages WHERE id=41"
                ).fetchone()[:],
                (9, "虚构旧旁听", "observer"),
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sys_cache").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0], 0
            )
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertTrue(
                {"pets", "card_claims", "card_instances", "memory_cards"}.isdisjoint(tables)
            )
        Database(self.target).init_db()

    def test_existing_destination_is_never_overwritten(self):
        self.target.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            migrate(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"keep")
        with self.assertRaises(ValueError):
            migrate(self.source, self.source)

    def test_invalid_foreign_key_rolls_back_entire_backfill(self):
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("UPDATE memory_cards SET pet_id=999 WHERE id=2")
        with self.assertRaises(sqlite3.IntegrityError):
            migrate(self.source, self.target)
        with Database(self.target).connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
        with self.assertRaisesRegex(RuntimeError, "not schema 2"):
            Database(self.target).init_db()

    def test_unknown_memory_kind_is_not_silently_reclassified(self):
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("UPDATE memory_cards SET kind='unknown' WHERE id=2")
        with self.assertRaisesRegex(ValueError, "unknown memory kind"):
            migrate(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_bad_compression_marker_rolls_back(self):
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("UPDATE pets SET summary_until_id=41 WHERE id=4")
        with self.assertRaisesRegex(RuntimeError, "marker"):
            migrate(self.source, self.target)
        with Database(self.target).connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)


class IntegerStateMigrationTests(unittest.TestCase):
    def test_integer_timestamps_are_preserved_with_real_column_affinity(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "old.db", Path(directory) / "new.db"
            legacy_fixture(source)
            with closing(sqlite3.connect(source)) as conn, conn:
                conn.execute("UPDATE pets SET state_json=?", (json.dumps({"last_reply_ts": 123}),))
            migrate(source, target)
            with Database(target).connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT last_reply_ts FROM conversations WHERE id=4").fetchone()[
                        0
                    ],
                    123.0,
                )
