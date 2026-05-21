from __future__ import annotations

import tempfile
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


def make_config(db_path: str):
    return load_config(
        {
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
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
        self.pets = PetRepository(self.db, self.state_domain)
        self.messages = MessageRepository(self.db)
        self.system = SystemRepository(self.db)
        self.memory = MemoryRepository(self.db, self.memory_domain)

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
        self.assertIn("hunger", state)
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


if __name__ == "__main__":
    unittest.main()

