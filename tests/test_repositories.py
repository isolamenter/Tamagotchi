from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from dafeiyu.config import load_config
from dafeiyu.domain.conversation import ConversationContext
from dafeiyu.domain.memory import MemoryDomain
from dafeiyu.domain.state import StateDomain
from dafeiyu.repositories.conversation_repo import ConversationRepository
from dafeiyu.repositories.memory_repo import MemoryRepository
from dafeiyu.repositories.message_repo import MessageRepository
from dafeiyu.repositories.sqlite import Database
from dafeiyu.repositories.system_repo import SystemRepository
from dafeiyu.runtime import RuntimeState
from dafeiyu.services.memory_service import MemoryService


def make_config(db_path: str):
    return load_config(
        {
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
            "CHAT_MODEL": "fixture-chat",
            "EMBED_MODEL": "fixture-embedding",
            "FEISHU_VERIFICATION_TOKEN": "verify-token",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_API_KEY": "key",
            "STATE_DB": db_path,
        }
    )


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "state.db")
        self.config = make_config(self.db_path)
        self.state_domain = StateDomain(self.config)
        self.memory_domain = MemoryDomain(self.config)
        self.db = Database(self.config.db_path)
        self.db.init_db()
        self.runtime = RuntimeState()
        self.conversations = ConversationRepository(self.db, self.state_domain, self.runtime)
        self.messages = MessageRepository(self.db)
        self.system = SystemRepository(self.db)
        self.memory = MemoryRepository(self.db, self.memory_domain, self.config)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_conversation_message_context_and_event_dedup(self):
        conversation_id = await self.conversations.get_or_create_conversation("oc_test")
        same_id = await self.conversations.get_or_create_conversation("oc_test")
        self.assertEqual(conversation_id, same_id)

        msg_id = await self.messages.append_message(
            conversation_id, "user", "hello", sender_name="A"
        )
        history, state = await self.conversations.load_conversation_context(conversation_id)
        self.assertEqual(history[0]["id"], msg_id)
        self.assertEqual(history[0]["content"], "hello")
        self.assertIn("last_update_ts", state)
        self.assertEqual(await self.messages.count_unsummarized(conversation_id), 1)

        self.assertFalse(await self.system.check_and_register_event("evt-1"))
        self.assertTrue(await self.system.check_and_register_event("evt-1"))

    async def test_memory_repository_recent_and_scoring(self):
        conversation_id = await self.conversations.get_or_create_conversation("oc_test")
        inserted = await self.memory.save_compressed_cards(
            conversation_id,
            [
                {
                    "when": "now",
                    "who": "A",
                    "what": "likes apples",
                    "vibe": "",
                    "hooks": "",
                }
            ],
            new_until_id=0,
        )
        card_id = inserted[0][0]
        await self.memory.store_embedding(card_id, "likes apples", [1.0, 0.0])
        recent = await self.memory.recent_cards(conversation_id, 1)
        self.assertEqual(recent[0]["what"], "likes apples")
        scored = await self.memory.score_cards(conversation_id, [1.0, 0.0], 1)
        self.assertEqual(scored[0]["id"], card_id)

    async def test_compress_empty_cards_does_not_advance_until_id(self):
        conversation_id = await self.conversations.get_or_create_conversation("oc_compress")
        # 0 张可用卡：不推进 summary_until_id（否则消息静默丢失）
        inserted = await self.memory.save_compressed_cards(conversation_id, [], new_until_id=5)
        self.assertEqual(inserted, [])
        summary_until_id, *_ = await self.memory.get_compress_context(conversation_id)
        self.assertEqual(summary_until_id, 0)
        # 全是无效卡（空 what）也不推进
        inserted2 = await self.memory.save_compressed_cards(
            conversation_id, [{"what": ""}, "not-a-dict"], new_until_id=5
        )
        self.assertEqual(inserted2, [])
        summary_until_id, *_ = await self.memory.get_compress_context(conversation_id)
        self.assertEqual(summary_until_id, 0)
        # 有真卡才推进
        inserted3 = await self.memory.save_compressed_cards(
            conversation_id, [{"what": "real"}], new_until_id=5
        )
        self.assertEqual(len(inserted3), 1)
        summary_until_id, *_ = await self.memory.get_compress_context(conversation_id)
        self.assertEqual(summary_until_id, 5)

    async def test_mutate_state_serializes_concurrent_writers(self):
        import asyncio

        conversation_id = await self.conversations.get_or_create_conversation("oc_mutate")
        await self.conversations.update_conversation_state(
            conversation_id,
            {
                **self.state_domain.initial_state(),
                "last_social_ts": 50.0,
                "last_update_ts": time.time(),
            },
        )

        async def add_one():
            await self.conversations.mutate_state(
                conversation_id,
                lambda s: {
                    **s,
                    "last_social_ts": s["last_social_ts"] + 1.0,
                    "last_update_ts": time.time(),
                },
            )

        await asyncio.gather(*[add_one() for _ in range(20)])
        final = await self.conversations.load_conversation_state(conversation_id)
        # 串行化下 20 次 +1 全部生效（约 70）；若丢失更新会远低于此（接近 51）
        self.assertGreater(final["last_social_ts"], 69.0)

    async def test_old_state_is_normalized_without_losing_values(self):
        conversation_id = await self.conversations.get_or_create_conversation("oc_old_state")
        await self.conversations.update_conversation_state(
            conversation_id, {"custom": "keep", "active_need": {"kind": "hungry"}}
        )
        state = await self.conversations.load_conversation_state(conversation_id)
        self.assertNotIn("custom", state)
        self.assertNotIn("active_need", state)
        self.assertIn("last_update_ts", state)

    async def test_five_compression_failures_create_deterministic_cards(self):
        conversation_id = await self.conversations.get_or_create_conversation("oc_memory_fallback")

        class NoEmbedLLM:
            async def embed_text(self, text, **kwargs):
                return None

        service = MemoryService(
            self.config,
            self.runtime,
            self.memory_domain,
            ConversationContext(self.config),
            self.memory,
            NoEmbedLLM(),
        )
        await service._record_compress_failure(
            conversation_id,
            compress_fail_count=4,
            new_until_id=7,
            rows=[
                {"role": "user", "sender_name": "小明", "content": "今天吃了苹果"},
                {"role": "assistant", "sender_name": "", "content": "听起来很好吃"},
            ],
        )
        summary_until_id, fail_count, _, _ = await self.memory.get_compress_context(conversation_id)
        self.assertEqual(summary_until_id, 7)
        self.assertEqual(fail_count, 0)
        cards = await self.memory.recent_cards(conversation_id, 10)
        self.assertEqual(
            {card["what"] for card in cards}, {"聊天原文：今天吃了苹果", "聊天原文：听起来很好吃"}
        )


class MemoryModelIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_vector_upsert_and_same_dimension_model_switch_are_isolated(self):
        from dataclasses import replace

        from tests.helpers import container

        with tempfile.TemporaryDirectory() as directory:
            c = container(str(Path(directory) / "model.db"))
            cid = await c.conversation_repo.get_or_create_conversation("fixture")
            inserted = await c.memory_repo.save_compressed_cards(
                cid, [{"what": "虚构模型隔离测试"}], 0
            )
            memory_id = inserted[0][0]
            await c.memory_repo.store_embedding(memory_id, "old", [1.0, 0.0])
            await c.memory_repo.store_embedding(memory_id, "updated", [1.0, 0.0])
            with c.db.connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0], 1
                )
            self.assertTrue(await c.memory_repo.score_cards(cid, [1.0, 0.0], 3))
            other = MemoryRepository(
                c.db, c.memory_domain, replace(c.config, embed_model="other-model")
            )
            self.assertEqual(await other.score_cards(cid, [1.0, 0.0], 3), [])
            self.assertEqual((await other.embedding_status())["pending"], 1)


if __name__ == "__main__":
    unittest.main()
