from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from config import load_config
from domain.memory import MemoryDomain
from domain.pet import PetDomain
from domain.state import StateDomain
from repositories.memory_repo import MemoryRepository
from repositories.card_repo import CardRepository
from repositories.message_repo import MessageRepository
from repositories.pet_repo import PetRepository
from repositories.sqlite import Database
from repositories.system_repo import SystemRepository
from runtime import RuntimeState
from services.memory_service import MemoryService


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
        self.cards = CardRepository(self.db)

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

    async def test_card_contract_claims_are_global_or_per_person_by_mode(self):
        pet_id = await self.pets.get_or_create_pet("oc_cards")
        now = time.time()
        await self.cards.create_instance(
            card_id="need-1", pet_id=pet_id, mode="need", need_id="n", need_round=0,
            built_at=now, expires_at=now + 60, max_settlements=1,
            base_text="need", action_keys=["feed"], img_key=None,
        )
        await self.cards.mark_announced("need-1", "om_need")
        self.assertEqual(await self.cards.claim("need-1", "u1", "feed", now), "settle")
        self.assertEqual(await self.cards.claim("need-1", "u1", "feed", now), "duplicate")
        self.assertEqual(await self.cards.claim("need-1", "u2", "feed", now), "social")

        await self.cards.create_instance(
            card_id="scheduled-1", pet_id=pet_id, mode="scheduled", need_id="", need_round=0,
            built_at=now, expires_at=now + 60, max_settlements=3,
            base_text="scheduled", action_keys=["goodnight"], img_key=None,
        )
        await self.cards.mark_announced("scheduled-1", "om_scheduled")
        self.assertEqual(await self.cards.claim("scheduled-1", "u1", "goodnight", now), "settle")
        self.assertEqual(await self.cards.claim("scheduled-1", "u2", "goodnight", now), "settle")
        self.assertEqual(await self.cards.claim("scheduled-1", "u3", "goodnight", now), "settle")
        self.assertEqual(await self.cards.claim("scheduled-1", "u4", "goodnight", now), "social")
        self.assertEqual(await self.cards.claim("scheduled-1", "u1", "goodnight", now), "duplicate")

    async def test_old_state_is_normalized_without_losing_values(self):
        pet_id = await self.pets.get_or_create_pet("oc_old_state")
        await self.pets.update_pet_state(pet_id, {"satiety": 17, "custom": "keep"})
        state = await self.pets.load_pet_state(pet_id)
        self.assertAlmostEqual(state["satiety"], 17.0, places=4)
        self.assertEqual(state["custom"], "keep")
        self.assertIn("last_social_ts", state)
        self.assertIn("last_free_card_ts", state)

    async def test_five_compression_failures_create_deterministic_cards(self):
        pet_id = await self.pets.get_or_create_pet("oc_memory_fallback")

        class NoEmbedLLM:
            async def embed_text(self, text):
                return None

        service = MemoryService(
            self.config,
            self.runtime,
            self.memory_domain,
            PetDomain(self.config),
            self.memory,
            NoEmbedLLM(),
        )
        await service._record_compress_failure(
            pet_id,
            compress_fail_count=4,
            new_until_id=7,
            rows=[
                {"role": "user", "sender_name": "小明", "content": "今天吃了苹果"},
                {"role": "assistant", "sender_name": "", "content": "听起来很好吃"},
            ],
        )
        summary_until_id, fail_count, _, _ = await self.memory.get_compress_context(pet_id)
        self.assertEqual(summary_until_id, 7)
        self.assertEqual(fail_count, 0)
        cards = await self.memory.recent_cards(pet_id, 10)
        self.assertEqual({card["what"] for card in cards}, {"今天吃了苹果", "听起来很好吃"})

if __name__ == "__main__":
    unittest.main()
