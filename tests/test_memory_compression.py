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

    async def oversized_message(self):
        self.cid = await self.c.conversation_repo.get_or_create_conversation("oversized")
        text = ('长消息\n"引用"\\\x01' * 10000) + "末尾约定"
        mid = await self.c.message_repo.append_message(
            self.cid, "user", text, sender_open_id="alice", source_message_id="long-source"
        )
        for i in range(4):
            await self.c.message_repo.append_message(self.cid, "user", f"后续消息{i}")
        return mid, text

    async def test_legacy_names_are_filtered_from_normal_and_fragmented_compression(self):
        for large in (False, True):
            with self.subTest(fragmented=large):
                cid = await self.c.conversation_repo.get_or_create_conversation(f"names-{large}")
                text = "今天想喝茶" * (20000 if large else 1)
                mid = await self.c.message_repo.append_message(
                    cid, "user", text, sender_name="群友-lice", sender_open_id="alice"
                )
                await self.c.message_repo.append_message(
                    cid, "user", "好呀", sender_name="小明", sender_open_id="bob"
                )
                await self.c.message_repo.append_message(cid, "user", "保留一条")
                self.service.config = replace(self.service.config, buffer_keep=0)
                batches = []

                async def model(messages, batches=batches, **kwargs):
                    chunk = messages[-1]["content"]
                    rows = json.JSONDecoder().raw_decode(chunk[chunk.index("["):])[0]
                    batches.append(rows)
                    self.assertNotIn("群友-lice", chunk)
                    return '{"cards": []}'

                self.c.llm.chat_json = model
                await self.service.compress_conversation_memory(cid)
                self.assertTrue(batches)
                self.assertEqual(batches[0][0]["sender_name"], "")
                if large:
                    self.assertGreater(len(batches), 1)
                else:
                    self.assertEqual(batches[0][1]["sender_name"], "小明")
                with self.c.db.connect() as conn:
                    stored = conn.execute(
                        "SELECT sender_name,content FROM messages WHERE id=?", (mid,)
                    ).fetchone()
                self.assertEqual((stored["sender_name"], stored["content"]), ("群友-lice", text))

    async def test_oversized_source_is_fully_compressed_then_later_messages_can_progress(self):
        mid, text = await self.oversized_message()
        fragments = []

        async def model(messages, **kwargs):
            self.assertLessEqual(
                sum(len(m["content"]) for m in messages),
                self.service.config.consolidation["max_context_chars"],
            )
            chunk = messages[-1]["content"].split("\n", 1)[-1]
            # The configured template surrounds the JSON with prose.
            rows = json.JSONDecoder().raw_decode(chunk[chunk.index("["):])[0]
            fragments.extend(rows)
            return json.dumps({"cards": [{"what": "保留约定", "source_ids": [mid]}]})

        self.c.llm.chat_json = model
        await self.service.compress_conversation_memory(self.cid)
        self.assertGreater(len(fragments), 1)
        self.assertEqual("".join(r["content"] for r in fragments), text)
        self.assertEqual(
            [r["start_offset"] for r in fragments],
            [sum(len(p["content"]) for p in fragments[:i]) for i in range(len(fragments))],
        )
        self.assertTrue(fragments[-1]["content_complete"])
        self.assertEqual((await self.state())[0], mid)
        cards = await self.c.memory_repo.recent_cards(self.cid, 20)
        self.assertTrue(cards)
        self.assertTrue(all(c["evidence"][0]["source_message_id"] == "long-source" for c in cards))
        self.c.llm.chat_json = AsyncMock(return_value='{"cards": []}')
        await self.service.compress_conversation_memory(self.cid)
        self.assertGreater((await self.state())[0], mid)
        self.assertEqual(len(await self.c.message_repo.recent_messages(self.cid, 20)), 5)

    async def test_failed_later_fragment_never_saves_partial_cards_or_advances(self):
        mid, _ = await self.oversized_message()
        self.c.llm.chat_json = AsyncMock(side_effect=[
            json.dumps({"cards": [{"what": "第一段约定", "source_ids": [mid]}]}),
            '{"cards": [null]}',
        ])
        await self.service.compress_conversation_memory(self.cid)
        self.assertEqual(self.c.llm.chat_json.await_count, 2)
        self.assertEqual((await self.state())[:2], (0, 1))
        self.assertEqual(await self.c.memory_repo.recent_cards(self.cid, 20), [])
        self.c.llm.chat_json = AsyncMock(return_value='{"cards": []}')
        await self.service.compress_conversation_memory(self.cid)
        self.assertEqual((await self.state())[:2], (mid, 0))

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
