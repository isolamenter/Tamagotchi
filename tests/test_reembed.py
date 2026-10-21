from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dafeiyu.domain.memory import MemoryDomain
from dafeiyu.repositories.sqlite import Database
from dafeiyu.tools.reembed import (
    load_cards,
    prepare_embeddings,
    replace_memory_embeddings,
)
from tests.helpers import make_config


class FakeEmbedder:
    def __init__(self, fail=False):
        self.fail = fail

    async def embed_texts(self, texts, *, purpose=""):
        return None if self.fail else [[1.0, 0.0] for _ in texts]


class ReembedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(str(Path(self.tmp.name) / "v2.db"))
        self.db = Database(self.config.db_path)
        self.db.init_db()
        self.md = MemoryDomain(self.config)
        with self.db.connect() as conn:
            conn.execute("INSERT INTO conversations(id,chat_id,created_at) VALUES(1,'fixture',0)")
            conn.execute(
                "INSERT INTO memories(id,conversation_id,kind,what,created_at) VALUES(1,1,'reality','虚构记忆',0)"
            )
            conn.execute(
                "INSERT INTO memory_embeddings VALUES(1,'old','old',1,'old',?,0)",
                (self.md.vec_pack([0.5]),),
            )
            self.snapshot = load_cards(conn)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_rebuild_records_model_identity_and_one_vector_per_memory(self):
        prepared, dimension = await prepare_embeddings(self.snapshot, FakeEmbedder(), self.md)
        with self.db.connect() as conn:
            replace_memory_embeddings(
                conn, prepared, self.snapshot, provider="new", model="new-model"
            )
            row = conn.execute("SELECT * FROM memory_embeddings").fetchone()
            self.assertEqual(
                (row["provider"], row["model"], row["dimension"]), ("new", "new-model", 2)
            )
            self.assertEqual(dimension, 2)

    async def test_embedding_failure_preserves_old_vectors(self):
        with self.assertRaises(RuntimeError):
            await prepare_embeddings(self.snapshot, FakeEmbedder(fail=True), self.md, tries=1)
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT model FROM memory_embeddings").fetchone()[0], "old"
            )

    async def test_concurrent_memory_change_aborts_vector_replacement(self):
        prepared, _ = await prepare_embeddings(self.snapshot, FakeEmbedder(), self.md)
        with self.db.connect() as conn:
            conn.execute("UPDATE memories SET what='新虚构内容' WHERE id=1")
        with self.db.connect() as conn:
            with self.assertRaisesRegex(RuntimeError, "Memories changed"):
                replace_memory_embeddings(
                    conn, prepared, self.snapshot, provider="new", model="new-model"
                )
            self.assertEqual(
                conn.execute("SELECT model FROM memory_embeddings").fetchone()[0], "old"
            )


class BatchReembedTests(unittest.IsolatedAsyncioTestCase):
    async def test_batches_use_document_task_and_preserve_card_order(self):
        class BatchEmbedder:
            def __init__(self):
                self.calls = []

            async def embed_texts(self, texts, *, purpose=""):
                self.calls.append((len(texts), purpose))
                return [[1.0, 0.0] for _ in texts]

        llm = BatchEmbedder()
        cards = [{"id": i, "what": f"虚构记忆{i}"} for i in range(1, 6)]
        prepared, _ = await prepare_embeddings(
            cards, llm, MemoryDomain(make_config()), batch_size=2
        )
        self.assertEqual(
            llm.calls,
            [(2, "retrieval_document"), (2, "retrieval_document"), (1, "retrieval_document")],
        )
        self.assertEqual([row[0] for row in prepared], [1, 2, 3, 4, 5])

    async def test_incomplete_batch_is_rejected_before_replacement(self):
        class WrongCount:
            async def embed_texts(self, texts, *, purpose=""):
                return []

        with self.assertRaises(RuntimeError):
            await prepare_embeddings(
                [{"id": 1, "what": "虚构记忆"}], WrongCount(), MemoryDomain(make_config()), tries=1
            )
