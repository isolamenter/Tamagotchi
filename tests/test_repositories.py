from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from config import load_config
from domain.memory import MemoryDomain
from domain.state import StateDomain
from repositories.memory_repo import MemoryRepository
from repositories.message_repo import MessageRepository
from repositories.pet_repo import PetRepository
from repositories.sqlite import Database
from repositories.system_repo import SystemRepository
from runtime import RuntimeState


def make_config(db_path: str):
    return load_config(
        {
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
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
        self.db = Database(self.config)
        self.db.init_db()
        self.runtime = RuntimeState()
        self.pets = PetRepository(self.db, self.state_domain, self.runtime)
        self.messages = MessageRepository(self.db)
        self.system = SystemRepository(self.db)
        self.memory = MemoryRepository(self.db, self.memory_domain, self.config)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_pet_message_context_and_event_dedup(self):
        pet_id = await self.pets.get_or_create_pet("oc_test")
        same_id = await self.pets.get_or_create_pet("oc_test")
        self.assertEqual(pet_id, same_id)

        msg_id = await self.messages.append_message(
            pet_id, "user", "hello", sender_name="A"
        )
        history, state = await self.pets.load_pet_context(pet_id)
        self.assertEqual(history[0]["id"], msg_id)
        self.assertEqual(history[0]["content"], "hello")
        self.assertIn("satiety", state)
        self.assertEqual(await self.messages.count_unsummarized(pet_id), 1)

        self.assertFalse(await self.system.check_and_register_event("evt-1"))
        self.assertTrue(await self.system.check_and_register_event("evt-1"))

    async def test_memory_repository_recent_and_scoring(self):
        pet_id = await self.pets.get_or_create_pet("oc_test")
        inserted = await self.memory.save_compressed_cards(
            pet_id,
            [{"when": "now", "who": "A", "what": "likes apples", "vibe": "", "hooks": ""}],
            new_until_id=0,
        )
        card_id = inserted[0][0]
        await self.memory.store_embedding(
            pet_id, "card", card_id, "likes apples", [1.0, 0.0]
        )
        recent = await self.memory.recent_cards(pet_id, 1)
        self.assertEqual(recent[0]["what"], "likes apples")
        scored = await self.memory.score_cards(pet_id, [1.0, 0.0], 1)
        self.assertEqual(scored[0]["id"], card_id)

    async def test_compress_empty_cards_does_not_advance_until_id(self):
        pet_id = await self.pets.get_or_create_pet("oc_compress")
        # 0 张可用卡：不推进 summary_until_id（否则消息静默丢失）
        inserted = await self.memory.save_compressed_cards(pet_id, [], new_until_id=5)
        self.assertEqual(inserted, [])
        summary_until_id, *_ = await self.memory.get_compress_context(pet_id)
        self.assertEqual(summary_until_id, 0)
        # 全是无效卡（空 what）也不推进
        inserted2 = await self.memory.save_compressed_cards(
            pet_id, [{"what": ""}, "not-a-dict"], new_until_id=5
        )
        self.assertEqual(inserted2, [])
        summary_until_id, *_ = await self.memory.get_compress_context(pet_id)
        self.assertEqual(summary_until_id, 0)
        # 有真卡才推进
        inserted3 = await self.memory.save_compressed_cards(
            pet_id, [{"what": "real"}], new_until_id=5
        )
        self.assertEqual(len(inserted3), 1)
        summary_until_id, *_ = await self.memory.get_compress_context(pet_id)
        self.assertEqual(summary_until_id, 5)

    async def test_mutate_state_serializes_concurrent_writers(self):
        import asyncio

        pet_id = await self.pets.get_or_create_pet("oc_mutate")
        await self.pets.update_pet_state(
            pet_id,
            {**self.state_domain.initial_state(), "satiety": 50.0, "last_update_ts": time.time()},
        )

        async def add_one():
            await self.pets.mutate_state(
                pet_id,
                lambda s: {**s, "satiety": s["satiety"] + 1.0, "last_update_ts": time.time()},
            )

        await asyncio.gather(*[add_one() for _ in range(20)])
        final = await self.pets.load_pet_state(pet_id)
        # 串行化下 20 次 +1 全部生效（约 70）；若丢失更新会远低于此（接近 51）
        self.assertGreater(final["satiety"], 69.0)

if __name__ == "__main__":
    unittest.main()
