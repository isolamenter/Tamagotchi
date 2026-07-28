from __future__ import annotations

import sqlite3
import unittest

from domain.memory import MemoryDomain
from scripts_reembed import load_cards, prepare_embeddings, replace_card_embeddings
from tests.test_domain import make_config


class FakeEmbedder:
    def __init__(self, fail_text: str = ""):
        self.fail_text = fail_text

    async def embed_text(self, text: str) -> list[float] | None:
        if self.fail_text and self.fail_text in text:
            return None
        return [float(len(text)), 1.0]


class ReembedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE memory_cards (
                id INTEGER PRIMARY KEY, pet_id INTEGER, when_text TEXT, who TEXT,
                what TEXT, vibe TEXT, hooks TEXT
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY, pet_id INTEGER, kind TEXT, source_id INTEGER,
                content TEXT, vec BLOB, ts REAL
            );
            INSERT INTO memory_cards VALUES (1, 7, 'today', 'A', 'likes apples', '', 'fruit');
            INSERT INTO memory_cards VALUES (2, 7, 'today', 'B', 'likes pears', '', 'fruit');
            INSERT INTO embeddings VALUES (1, 7, 'card', 1, 'old', X'0000', 1);
            INSERT INTO embeddings VALUES (2, 7, 'card', 1, 'duplicate', X'0000', 1);
            INSERT INTO embeddings VALUES (3, 7, 'other', 99, 'keep', X'0000', 1);
            """
        )
        self.memory_domain = MemoryDomain(make_config())

    def tearDown(self):
        self.conn.close()

    async def test_rebuilds_one_embedding_per_card_and_keeps_other_kinds(self):
        cards = load_cards(self.conn)
        prepared, dimension = await prepare_embeddings(
            cards, FakeEmbedder(), self.memory_domain
        )
        replace_card_embeddings(self.conn, prepared, now=123.0)

        rows = self.conn.execute(
            "SELECT kind, source_id, content, length(vec) AS bytes, ts "
            "FROM embeddings ORDER BY kind, source_id"
        ).fetchall()
        self.assertEqual(dimension, 2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["kind"], "card")
        self.assertEqual(rows[0]["source_id"], 1)
        self.assertIn("likes apples", rows[0]["content"])
        self.assertEqual(rows[0]["bytes"], 8)
        self.assertEqual(rows[0]["ts"], 123.0)
        self.assertEqual(rows[2]["kind"], "other")

    async def test_prepare_failure_leaves_existing_embeddings_untouched(self):
        cards = load_cards(self.conn)
        with self.assertRaises(RuntimeError):
            await prepare_embeddings(
                cards,
                FakeEmbedder(fail_text="pears"),
                self.memory_domain,
                tries=1,
            )

        contents = [
            row[0]
            for row in self.conn.execute(
                "SELECT content FROM embeddings WHERE kind = 'card' ORDER BY id"
            )
        ]
        self.assertEqual(contents, ["old", "duplicate"])


if __name__ == "__main__":
    unittest.main()
