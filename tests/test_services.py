from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dafeiyu.domain.reply import GeneratedReply
from tests.helpers import container, event


class ReplyContractTests(unittest.TestCase):
    def test_valid_contract(self):
        reply = GeneratedReply.parse('{"reply_mode":"reaction","reply":"确实","meme_tag":"smug"}')
        self.assertEqual(reply.text, "确实")
        self.assertEqual(reply.meme_tag, "smug")

    def test_old_double_encoded_or_malformed_contract_is_rejected(self):
        for value in [
            '{"reply":"hello"}',
            '"{\\"reply\\":\\"hello\\"}"',
            '{"reply_mode":"normal","reply":""}',
            "not json",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                GeneratedReply.parse(value)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = container(str(Path(self.tmp.name) / "new.db"))
        self.cid = await self.c.conversation_repo.get_or_create_conversation("chat-fixture")

    async def asyncTearDown(self):
        await self.c.runtime.close()
        self.tmp.cleanup()

    async def test_p2p_needs_no_identity_lookup(self):
        message = await self.c.services.incoming.parse(
            event("你好", chat_type="p2p", mention=False)
        )
        self.assertTrue(message.direct)
        self.assertFalse(self.c.feishu.calls)

    async def test_bot_address_is_not_chat_content(self):
        message = await self.c.services.incoming.parse(event("@_bot 帮我看报错"))
        self.assertEqual(message.text, "帮我看报错")
        self.assertTrue(message.direct)

    async def test_unknown_bot_identity_stays_observer(self):
        self.c.feishu.fail = "identity"
        message = await self.c.services.incoming.parse(event("@_bot 你好"))
        self.assertFalse(message.direct)

    async def test_other_mentions_and_real_teasing_are_preserved(self):
        data = event("@_bot 你又胖了 @_other")
        data["message"]["mentions"].append(
            {"key": "@_other", "name": "小明", "id": {"open_id": "other"}}
        )
        message = await self.c.services.incoming.parse(data)
        self.assertEqual(message.text, "你又胖了 @小明")

    async def test_image_caption_and_failure_placeholder(self):
        message = await self.c.services.incoming.parse(
            event({"image_key": "key"}, message_type="image")
        )
        self.assertIn("蓝色的玩具", message.text)
        self.c.feishu.fail = "download"
        message = await self.c.services.incoming.parse(
            event({"image_key": "key"}, message_type="image")
        )
        self.assertIn("没看清", message.text)

    async def test_post_extracts_text_image_and_human_mention(self):
        data = {
            "title": "看看",
            "content": [
                [
                    {"tag": "at", "user_id": "bot-id", "user_name": "大肥鱼"},
                    {"tag": "text", "text": "这张图"},
                    {"tag": "img", "image_key": "key"},
                    {"tag": "at", "user_id": "other", "user_name": "小明"},
                ]
            ],
        }
        message = await self.c.services.incoming.parse(event(data, message_type="post"))
        self.assertNotIn("@大肥鱼", message.text)
        self.assertIn("@小明", message.text)
        self.assertIn("蓝色的玩具", message.text)

    async def test_observer_flush_keeps_source_and_human_identity(self):
        await self.c.services.reply.handle_message_event(event("随便聊聊", mention=False))
        await self.c.services.observer.flush_all()
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_observer"])
        self.assertEqual(rows[0]["source"], "observer")

    async def test_direct_reply_is_persisted_after_delivery(self):
        await self.c.services.reply.handle_message_event(event("@_bot 你好"))
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual(
            [(r["role"], r["content"]) for r in rows],
            [("user", "你好"), ("assistant", "在呢")],
        )
        self.assertEqual(self.c.feishu.calls[-1][0], "reply")

    async def test_direct_reply_enables_search_only_for_gemini(self):
        for provider in ("gemini", "openai"):
            with self.subTest(provider=provider):
                self.c.services.reply.config = replace(self.c.config, llm_provider=provider)
                with patch.object(
                    self.c.llm, "chat_json", AsyncMock(wraps=self.c.llm.chat_json)
                ) as chat:
                    await self.c.services.reply.handle_message_event(event("@_bot 你好"))
                    self.assertEqual(chat.await_count, 1)
                    self.assertEqual(chat.call_args.kwargs["web_search"], provider == "gemini")
                    system = "\n".join(
                        m["content"] for m in chat.call_args.args[0] if m["role"] == "system"
                    )
                    self.assertEqual(self.c.config.web_search_prompt in system, provider == "gemini")

    async def test_concurrent_messages_are_not_duplicated_or_attributed_to_last_user(
        self,
    ):
        self.c.services.reply.config = replace(self.c.config, reply_debounce_sec=0.01)
        await asyncio.gather(
            self.c.services.reply.handle_message_event(event("@_bot 第一段", sender="alice")),
            self.c.services.reply.handle_message_event(
                event("@_bot 第二段", sender="bob", message_id="message-2")
            ),
        )
        request = self.c.llm.calls[0]
        users = [m["content"] for m in request if m["role"] == "user"]
        self.assertEqual(sum("第一段" in m for m in users), 1)
        self.assertEqual(sum("第二段" in m for m in users), 1)
        self.assertTrue(any("lice" in m and "第一段" in m for m in users))
        self.assertTrue(any("bob" in m and "第二段" in m for m in users))
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual(sum(r["role"] == "assistant" for r in rows), 1)

    async def test_malformed_model_output_never_leaks_to_chat(self):
        self.c.llm.responses.append("internal malformed json")
        await self.c.services.reply.handle_message_event(event("@_bot 你好"))
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertNotIn("internal", rows[-1]["content"])

    async def test_text_failure_never_sends_meme_or_records_assistant(self):
        self.c.feishu.fail = "text"
        with self.assertRaises(RuntimeError):
            await self.c.services.delivery.send(self.cid, "chat-fixture", "hello", meme_tag="smug")
        self.assertEqual(await self.c.message_repo.recent_messages(self.cid, 20), [])
        self.assertFalse(self.c.feishu.calls)

    async def test_meme_failures_keep_successful_text(self):
        for failure in ["upload", "image"]:
            self.c.llm.responses.append('{"allow":true,"reason":"matched"}')
            self.c.feishu.fail = failure
            await self.c.services.delivery.send(
                self.cid,
                "chat-fixture",
                "开饭！",
                mode="reaction",
                meme_tag="eat_rice",
                meme_evidence="来吃饭",
                meme_user_text="来吃饭",
            )
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["meme_tag"] == "" for r in rows))

    async def test_consecutive_approved_memes_are_sent_and_upload_is_cached(self):
        self.c.llm.responses.extend(['{"allow":true,"reason":"matched"}'] * 11)
        for index in range(11):
            with patch("dafeiyu.services.delivery.time.time", return_value=1000 + index * 301):
                await self.c.services.delivery.send(
                    self.cid,
                    "chat-fixture",
                    "开饭！",
                    mode="reaction",
                    meme_tag="eat_rice",
                    meme_evidence="来吃饭",
                    meme_user_text="来吃饭",
                )
        calls = self.c.feishu.calls
        self.assertEqual(sum(c[0] == "image" for c in calls), 11)
        self.assertEqual(sum(c[0] == "upload" for c in calls), 1)

    async def test_substantive_reply_has_no_meme(self):
        await self.c.services.delivery.send(
            self.cid,
            "chat-fixture",
            "开饭！",
            mode="substantive",
            meme_tag="eat_rice",
            meme_evidence="来吃饭",
            meme_user_text="来吃饭",
        )
        self.assertEqual([call[0] for call in self.c.feishu.calls], ["text"])

    async def test_content_card_has_no_old_game_actions(self):
        await self.c.services.delivery.send(
            self.cid, "chat-fixture", "今天记一件事", as_card=True, source="diary"
        )
        card = self.c.feishu.calls[0][2]
        self.assertNotIn("action", str(card))
        with self.c.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM published_cards").fetchone()[0], 1)

    async def test_cooldown_and_no_new_humans_skip_interjection(self):
        state = await self.c.conversation_repo.load_conversation_state(self.cid)
        self.assertIsNone(
            await self.c.services.autonomous.evaluate_proactive_speaking(self.cid, state)
        )
        self.assertFalse(self.c.llm.calls)
        state["last_reply_ts"] = time.time()
        self.assertTrue(self.c.services.autonomous._cooling_down(state, time.time()))

    async def test_evaluator_sees_own_reply_and_honors_unanswered_topic(self):
        for role, text in [
            ("assistant", "刚说过"),
            ("user", "第一句"),
            ("user", "第二句"),
        ]:
            await self.c.message_repo.append_message(self.cid, role, text)
        state = await self.c.conversation_repo.load_conversation_state(self.cid)
        self.c.llm.responses.append(
            json.dumps({"should_speak": True, "reason": "能接梗", "topic": "同一话题"})
        )
        result = await self.c.services.autonomous.evaluate_proactive_speaking(self.cid, state)
        self.assertEqual(result, ("能接梗", "同一话题"))
        self.assertIn("刚说过", self.c.llm.calls[-1][-1]["content"])
        state.update(last_proactive_topic="同一话题", last_proactive_answered=False)
        self.c.llm.responses.append(
            json.dumps({"should_speak": True, "reason": "接着说", "topic": "同一话题"})
        )
        self.assertIsNone(
            await self.c.services.autonomous.evaluate_proactive_speaking(self.cid, state)
        )

    async def test_locked_conversation_skips_autonomous_work(self):
        async with self.c.runtime.chat_lock(self.cid):
            self.assertFalse(
                await self.c.services.autonomous.maybe_interject(self.cid, "chat-fixture")
            )
            self.assertFalse(
                await self.c.services.autonomous.tick_conversation(self.cid, "chat-fixture")
            )
        self.assertFalse(self.c.llm.calls)

    async def test_background_tasks_are_cancelled_on_close(self):
        task = self.c.runtime.spawn(asyncio.sleep(100))
        await self.c.runtime.close()
        self.assertTrue(task.cancelled())
