from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from domain.style import StyleDomain
from repositories.sqlite import Database
from repositories.style_repo import StyleEmbeddingRepository
from services.style_service import StyleService
from tests.test_domain import make_config


class BatchLLM:
    def __init__(self, *, fail: bool = False, inconsistent: bool = False):
        self.fail = fail
        self.inconsistent = inconsistent
        self.calls: list[tuple[str, int]] = []

    async def embed_texts(self, texts, *, purpose=""):
        self.calls.append((purpose, len(texts)))
        if self.fail:
            return None
        vectors = [[1.0, float(index % 3), 0.5] for index in range(len(texts))]
        if self.inconsistent and len(vectors) > 1:
            vectors[-1] = [1.0, 0.0]
        return vectors

    async def embed_text(self, _text, *, purpose=""):
        if self.fail:
            return None
        return [1.0, 0.0, 0.0]


class StyleCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(str(Path(self.tmp.name) / "state.db"))
        self.db = Database(self.config)
        self.db.init_db()
        self.repo = StyleEmbeddingRepository(self.db)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_initializes_in_batches_and_reuses_complete_cache(self):
        llm = BatchLLM()
        service = StyleService(
            self.config, StyleDomain(self.config), self.repo, llm
        )
        await service.initialize()
        self.assertTrue(service.ready)
        self.assertEqual([size for _purpose, size in llm.calls], [50, 50, 15])
        with self.db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM style_embeddings"
            ).fetchone()[0]
            dimensions = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT dimension FROM style_embeddings"
                ).fetchall()
            }
        self.assertEqual(count, 115)
        self.assertEqual(dimensions, {3})

        cached_llm = BatchLLM(fail=True)
        cached_service = StyleService(
            self.config, StyleDomain(self.config), self.repo, cached_llm
        )
        await cached_service.initialize()
        self.assertTrue(cached_service.ready)
        self.assertEqual(cached_llm.calls, [])

    async def test_failed_refresh_keeps_last_known_good_cache_and_other_data(self):
        service = StyleService(
            self.config, StyleDomain(self.config), self.repo, BatchLLM()
        )
        await service.initialize()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO sys_cache(key, val, expires_at) VALUES ('keep', 'yes', 0)"
            )
            before = conn.execute(
                "SELECT example_id, content_hash FROM style_embeddings "
                "ORDER BY example_id"
            ).fetchall()

        self.config.style_corpus["examples"][0]["context"] += "（已修改）"
        failed = StyleService(
            self.config, StyleDomain(self.config), self.repo, BatchLLM(fail=True)
        )
        with self.assertRaises(RuntimeError):
            await failed.initialize()

        with self.db.connect() as conn:
            after = conn.execute(
                "SELECT example_id, content_hash FROM style_embeddings "
                "ORDER BY example_id"
            ).fetchall()
            keep = conn.execute(
                "SELECT val FROM sys_cache WHERE key = 'keep'"
            ).fetchone()[0]
        self.assertEqual([tuple(row) for row in after], [tuple(row) for row in before])
        self.assertEqual(keep, "yes")

    async def test_inconsistent_dimensions_do_not_replace_cache(self):
        service = StyleService(
            self.config,
            StyleDomain(self.config),
            self.repo,
            BatchLLM(inconsistent=True),
        )
        with self.assertRaises(RuntimeError):
            await service.initialize()
        with self.db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM style_embeddings"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    async def test_uninitialized_service_uses_lexical_fallback(self):
        service = StyleService(
            self.config,
            StyleDomain(self.config),
            self.repo,
            BatchLLM(fail=True),
        )
        block = await service.render_examples_block("这个游戏你玩过吗")
        self.assertIn("没玩过", block)


if __name__ == "__main__":
    unittest.main()
