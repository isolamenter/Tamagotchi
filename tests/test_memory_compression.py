from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from tests.helpers import container


class MemoryCompressionTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real service/repository boundary without network or live models."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = container(str(Path(self.tmp.name) / "memory.db"))
        self.cid = await self.c.conversation_repo.get_or_create_conversation("memory-review")
        self.service = self.c.services.memory
        self.service.config = replace(self.c.config, buffer_keep=2)
        self.c.llm.embed_text = AsyncMock(return_value=[1.0, 0.0, 0.0])
        self.ids = []
        for text in ["早", "早啊", "哈哈", "今天准备做什么", "先吃饭"]:
            self.ids.append(await self.c.message_repo.append_message(self.cid, "user", text))

    async def asyncTearDown(self):
        await self.c.runtime.close()
        self.tmp.cleanup()

    async def compress(self, response):
        self.c.llm.responses.append(response)
        await self.service.compress_conversation_memory(self.cid)

    async def state(self):
        return await self.c.memory_repo.get_compress_context(self.cid)

    async def test_explicit_empty_result_advances_checkpoint_without_creating_cards(self):
        await self.compress('{"cards": []}')
        until, failures, attempted, pending = await self.state()
        self.assertEqual(until, self.ids[2])
        self.assertEqual(failures, 0)
        self.assertGreater(attempted, 0)
        self.assertEqual([row["id"] for row in pending], self.ids[3:])
        self.assertEqual(await self.c.memory_repo.recent_cards(self.cid, 10), [])
        self.c.llm.embed_text.assert_not_called()
        # Advancing the prompt window must not delete the persisted transcript.
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual(len(rows), 5)
        await self.service.compress_conversation_memory(self.cid)
        self.assertEqual(len(self.c.llm.calls), 1)

    async def test_empty_success_resets_old_failures_instead_of_creating_fallback(self):
        with self.c.db.connect() as conn:
            conn.execute(
                "UPDATE conversations SET compress_fail_count = 4, last_compress_attempt = 0 "
                "WHERE id = ?",
                (self.cid,),
            )
        await self.compress('{"cards": []}')
        until, failures, _, _ = await self.state()
        self.assertEqual((until, failures), (self.ids[2], 0))
        self.assertEqual(await self.c.memory_repo.recent_cards(self.cid, 10), [])

    async def test_invalid_contract_never_counts_as_intentional_empty(self):
        malformed = [
            "not json",
            "[]",
            "null",
            "{}",
            '{"cards": null}',
            '{"cards": false}',
            '{"cards": 0}',
            '{"cards": ""}',
            '{"cards": {}}',
            '{"cards": [null]}',
            '{"cards": ["hello"]}',
            '{"cards": [{}]}',
            '{"cards": [{"what": "  "}]}',
            '{"cards": [{"what": 7}]}',
            '{"cards": [{"what": "event", "who": []}]}',
            '{"cards": [{"what": "event", "kind": "unknown"}]}',
        ]
        for index, response in enumerate(malformed):
            with self.subTest(response=response):
                self.cid = await self.c.conversation_repo.get_or_create_conversation(
                    f"invalid-{index}"
                )
                for _ in range(5):
                    await self.c.message_repo.append_message(self.cid, "user", "保留原文")
                await self.compress(response)
                until, failures, _, pending = await self.state()
                self.assertEqual((until, failures), (0, 1))
                self.assertEqual(len(pending), 5)
                self.assertEqual(await self.c.memory_repo.recent_cards(self.cid, 10), [])

    async def test_mixed_valid_and_invalid_cards_cannot_partially_advance_checkpoint(self):
        await self.compress(json.dumps({"cards": [{"what": "likes tea"}, {"what": ""}]}))
        until, failures, _, _ = await self.state()
        self.assertEqual((until, failures), (0, 1))
        self.assertEqual(await self.c.memory_repo.recent_cards(self.cid, 10), [])

    async def test_nonempty_success_saves_cards_and_schedules_embeddings(self):
        await self.compress(json.dumps({"cards": [{"what": "likes tea", "kind": "reality"}]}))
        await asyncio.sleep(0)
        until, failures, _, pending = await self.state()
        self.assertEqual((until, failures), (self.ids[2], 0))
        self.assertEqual(len(pending), 2)
        self.assertEqual(
            (await self.c.memory_repo.recent_cards(self.cid, 10))[0]["what"], "likes tea"
        )
        self.c.llm.embed_text.assert_awaited_once()

    async def test_empty_acknowledgement_is_scoped_to_one_conversation(self):
        other = await self.c.conversation_repo.get_or_create_conversation("other")
        await self.c.message_repo.append_message(other, "user", "keep this")
        await self.compress('{"cards": []}')
        until, failures, _, pending = await self.c.memory_repo.get_compress_context(other)
        self.assertEqual((until, failures, len(pending)), (0, 0, 1))

    async def test_model_error_preserves_history_for_retry(self):
        self.c.llm.fail = True
        await self.service.compress_conversation_memory(self.cid)
        until, failures, _, pending = await self.state()
        self.assertEqual((until, failures, len(pending)), (0, 1, 5))

    async def test_save_error_preserves_history_for_retry(self):
        self.c.memory_repo.save_compressed_cards = AsyncMock(
            side_effect=RuntimeError("disk failure")
        )
        await self.compress('{"cards": []}')
        until, failures, _, pending = await self.state()
        self.assertEqual((until, failures, len(pending)), (0, 1, 5))

    async def test_real_failures_still_use_durable_fallback_after_fifth_failure(self):
        with self.c.db.connect() as conn:
            conn.execute(
                "UPDATE conversations SET compress_fail_count = 4, last_compress_attempt = 0 "
                "WHERE id = ?",
                (self.cid,),
            )
        await self.compress('{"cards": null}')
        until, failures, _, pending = await self.state()
        self.assertEqual((until, failures, len(pending)), (self.ids[2], 0, 2))
        cards = await self.c.memory_repo.recent_cards(self.cid, 10)
        self.assertEqual(len(cards), 3)
        self.assertTrue(all(card["kind"] == "hearsay" for card in cards))

    async def test_repository_requires_explicit_empty_acknowledgement(self):
        await self.c.memory_repo.save_compressed_cards(self.cid, [], self.ids[2])
        self.assertEqual((await self.state())[0], 0)
        await self.c.memory_repo.save_compressed_cards(
            self.cid, [{"what": ""}], self.ids[2], allow_empty=True
        )
        self.assertEqual((await self.state())[0], 0)
        await self.c.memory_repo.save_compressed_cards(self.cid, [], self.ids[2], allow_empty=True)
        self.assertEqual((await self.state())[0], self.ids[2])

    async def test_transaction_rolls_back_cards_if_later_insert_fails(self):
        with self.assertRaises(ValueError):
            await self.c.memory_repo.save_compressed_cards(
                self.cid,
                [{"what": "first"}, {"what": "second", "kind": "invalid"}],
                self.ids[2],
            )
        self.assertEqual((await self.state())[0], 0)
        self.assertEqual(await self.c.memory_repo.recent_cards(self.cid, 10), [])

    async def test_later_batch_after_empty_result_still_remembers_meaningful_event(self):
        await self.compress('{"cards": []}')
        for text in ["我叫小明", "我喜欢乌龙茶"]:
            await self.c.message_repo.append_message(self.cid, "user", text)
        await self.compress('{"cards": [{"what": "群友先吃饭", "kind": "reality"}]}')
        until, failures, _, pending = await self.state()
        self.assertEqual((until, failures), (self.ids[4], 0))
        self.assertEqual([row["content"] for row in pending], ["我叫小明", "我喜欢乌龙茶"])
        self.assertEqual(len(await self.c.memory_repo.recent_cards(self.cid, 10)), 1)

    async def test_backoff_for_repeated_real_failures_is_preserved(self):
        import time

        with self.c.db.connect() as conn:
            conn.execute(
                "UPDATE conversations SET compress_fail_count = 3, last_compress_attempt = ? "
                "WHERE id = ?",
                (time.time(), self.cid),
            )
        await self.service.compress_conversation_memory(self.cid)
        self.assertEqual(self.c.llm.calls, [])
        until, failures, _, pending = await self.state()
        self.assertEqual((until, failures, len(pending)), (0, 3, 5))
