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
        with self.assertLogs("dafeiyu", level="WARNING") as captured:
            await self.c.services.reply.handle_message_event(event("@_bot 你好"))
        logs = "\n".join(captured.output)
        self.assertIn("JSONDecodeError", logs)
        self.assertIn("Expecting value", logs)
        self.assertIn("line=1 column=1 position=0 chars=23", logs)
        self.assertNotIn("internal malformed json", logs)
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
        self.assertTrue(self.c.services.autonomous.cooling_down(state, time.time()))

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


class DisplayNameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = container(str(Path(self.tmp.name) / "new.db"))
        self.cid = await self.c.conversation_repo.get_or_create_conversation("chat-fixture")

    async def asyncTearDown(self):
        await self.c.runtime.close()
        self.tmp.cleanup()

    def prompt(self):
        return "\n".join(m["content"] for m in self.c.llm.calls[-1])

    async def test_group_display_name_is_cached_presentation_not_identity(self):
        self.c.feishu.member_names = {"alice": "小夏"}
        await self.c.services.reply.handle_message_event(event("@_bot 你好"))
        await self.c.services.reply.handle_message_event(
            event("@_bot 还在吗", message_id="message-2")
        )
        self.assertEqual(self.c.feishu.member_calls, ["chat-fixture"])
        self.assertIn("[群友 小夏 @ 你]", self.prompt())
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual({r["sender_name"] for r in rows if r["role"] == "user"}, {"小夏"})
        self.assertEqual(await self.c.services.memory.identities.aliases(self.cid), [])

    async def test_missing_name_or_legacy_placeholder_is_never_offered_as_a_name(self):
        self.c.feishu.fail = "members"
        await self.c.message_repo.append_message(
            self.cid, "user", "早上好", sender_name="群友-lice", sender_open_id="alice"
        )
        await self.c.services.reply.handle_message_event(event("@_bot 你好"))
        self.assertIn("[群友（名字未知） @ 你]", self.prompt())
        self.assertNotIn("群友-", self.prompt())
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual(rows[-2]["sender_name"], "")

    async def test_p2p_and_looked_up_names_do_not_enter_content(self):
        self.c.feishu.member_names = {"alice": "小夏", "other": "小明"}
        await self.c.services.incoming.parse(event("你好", chat_type="p2p", mention=False))
        self.assertEqual(self.c.feishu.member_calls, [])
        data = event("@_bot 你看 @_other")
        data["message"]["mentions"].append({"key": "@_other", "id": {"open_id": "other"}})
        message = await self.c.services.incoming.parse(data)
        self.assertNotIn("小明", message.text)
        self.assertNotIn("小明", message.evidence_text)

    async def test_slow_member_lookup_is_cancelled_and_reply_still_runs(self):
        cancelled = asyncio.Event()

        async def members(chat_id):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.c.feishu.chat_member_names = members
        with patch("dafeiyu.services.incoming.MEMBER_NAMES_TIMEOUT_SEC", 0.01):
            await asyncio.wait_for(
                self.c.services.reply.handle_message_event(event("@_bot 你好")), 1
            )
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.c.feishu.calls[-1][0], "reply")
        self.assertIn("名字未知", self.prompt())
        # Timeout is cached too; following messages need no external lookup.
        self.c.feishu.chat_member_names = AsyncMock(side_effect=AssertionError("cache miss"))
        await self.c.services.reply.handle_message_event(event("@_bot 再来", message_id="second"))



class ProcessingFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = container(str(Path(self.tmp.name) / "new.db"))
        self.cid = await self.c.conversation_repo.get_or_create_conversation("chat-fixture")

    async def asyncTearDown(self):
        await self.c.runtime.close()
        self.tmp.cleanup()

    async def finish_feedback(self):
        await asyncio.gather(*list(self.c.runtime.background_tasks))

    async def test_reaction_spans_processing_and_is_removed_after_reply(self):
        seen = []
        original = self.c.feishu.delete_reaction

        async def delete(message_id, reaction_id):
            seen.append([c[0] for c in self.c.feishu.calls])
            await original(message_id, reaction_id)

        self.c.feishu.delete_reaction = delete
        await self.c.services.reply.handle_message_event(event("@_bot 你好"))
        await self.finish_feedback()
        self.assertEqual(
            self.c.feishu.reactions,
            [("add", "message-1", "Typing"), ("delete", "message-1", "reaction-message-1")],
        )
        self.assertEqual(seen, [["identity", "reply"]])

    async def test_observed_and_fallback_messages_get_no_reaction(self):
        await self.c.services.reply.handle_message_event(event("随便聊聊", mention=False))
        await self.c.services.reply.handle_message_event(
            event({"file_key": "f"}, message_type="file", message_id="message-2")
        )
        self.assertEqual(self.c.feishu.reactions, [])

    async def test_reaction_failure_never_blocks_reply(self):
        self.c.feishu.fail = "reaction"
        await self.c.services.reply.handle_message_event(event("@_bot 你好"))
        self.assertEqual(self.c.feishu.calls[-1][0], "reply")
        self.assertEqual(self.c.feishu.reactions, [])

    async def test_reaction_is_cleared_when_generation_or_delivery_fails(self):
        self.c.llm.fail = True
        await self.c.services.reply.handle_message_event(event("@_bot 你好"))
        await self.finish_feedback()
        self.c.llm.fail = False
        self.c.feishu.fail = "reply"
        await self.c.services.reply.handle_message_event(
            event("@_bot 再来", message_id="message-2")
        )
        await self.finish_feedback()
        self.assertEqual(
            [r[:2] for r in self.c.feishu.reactions],
            [
                ("add", "message-1"),
                ("delete", "message-1"),
                ("add", "message-2"),
                ("delete", "message-2"),
            ],
        )

    async def test_style_runs_alongside_identity_routing_and_recall_keeps_resolution(self):
        style_started = asyncio.Event()
        recalled = []

        async def style(*args, **kwargs):
            style_started.set()
            return "", []

        async def resolve(*args, **kwargs):
            await asyncio.wait_for(style_started.wait(), 1)
            return {"resolved"}, "\n【路由】"

        async def recall(conversation_id, *, query, subject_ids):
            recalled.append(subject_ids)
            return ""

        self.c.services.style.render_examples_block = style
        self.c.services.memory.identities.resolve_subjects = resolve
        self.c.services.memory.build_recall_block = recall
        reply = await self.c.services.reply.generate(self.cid, [await self.message()], set())
        self.assertEqual(reply.text, "在呢")
        self.assertEqual(recalled, [{"resolved"}])
        self.assertIn("【路由】", self.c.llm.calls[-1][0]["content"])

    async def test_recall_failure_cancels_style_retrieval(self):
        cancelled = asyncio.Event()

        async def style(*args, **kwargs):
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def recall(*args, **kwargs):
            await asyncio.sleep(0)
            raise RuntimeError("fixture recall failure")

        self.c.services.style.render_examples_block = style
        self.c.services.memory.build_recall_block = recall
        with self.assertRaises(RuntimeError):
            await self.c.services.reply.generate(self.cid, [await self.message()], set())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.c.llm.calls, [])

    async def test_slow_reaction_add_and_delete_do_not_hold_conversation_lock(self):
        original_add = self.c.feishu.add_reaction
        original_delete = self.c.feishu.delete_reaction
        release_add, release_delete = asyncio.Event(), asyncio.Event()
        delete_started = asyncio.Event()

        async def add(*args):
            await release_add.wait()
            return await original_add(*args)

        async def delete(*args):
            delete_started.set()
            await release_delete.wait()
            await original_delete(*args)

        self.c.feishu.add_reaction = add
        self.c.feishu.delete_reaction = delete
        try:
            await asyncio.wait_for(
                self.c.services.reply.handle_message_event(event("@_bot 第一条")), 1
            )
            self.assertFalse(self.c.runtime.chat_lock(self.cid).locked())
            release_add.set()
            await asyncio.wait_for(delete_started.wait(), 1)
            await asyncio.wait_for(
                self.c.services.reply.handle_message_event(
                    event("@_bot 第二条", message_id="second")
                ), 1
            )
            self.assertEqual(sum(call[0] == "reply" for call in self.c.feishu.calls), 2)
            self.assertFalse(self.c.runtime.chat_lock(self.cid).locked())
        finally:
            release_add.set()
            release_delete.set()
            await self.finish_feedback()
        self.assertEqual(sum(r[0] == "delete" for r in self.c.feishu.reactions), 2)

    async def test_reaction_add_and_delete_have_bounded_waits(self):
        async def hang(*args):
            await asyncio.Event().wait()

        with patch("dafeiyu.services.reply_service.REACTION_TIMEOUT_SEC", 0.01):
            self.c.feishu.add_reaction = hang
            self.assertEqual(await self.c.services.reply._acknowledge("message"), "")
            self.c.feishu.delete_reaction = hang
            added = asyncio.create_task(asyncio.sleep(0, result="reaction"))
            await asyncio.wait_for(
                self.c.services.reply._clear_acknowledgement("message", added), 1
            )

    async def test_style_failure_cancels_recall_without_waiting_for_it(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def recall(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def style(*args, **kwargs):
            await started.wait()
            raise RuntimeError("fixture style failure")

        self.c.services.reply._recall = recall
        self.c.services.style.render_examples_block = style
        with self.assertRaisesRegex(RuntimeError, "fixture style failure"):
            await asyncio.wait_for(
                self.c.services.reply.generate(self.cid, [await self.message()], set()), 1
            )
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.c.llm.calls, [])

    async def test_generation_cancellation_stops_both_retrieval_branches(self):
        started = [asyncio.Event(), asyncio.Event()]
        cancelled = [asyncio.Event(), asyncio.Event()]

        async def branch(index, *args, **kwargs):
            started[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled[index].set()

        self.c.services.reply._recall = lambda *a, **kw: branch(0, *a, **kw)
        self.c.services.style.render_examples_block = lambda *a, **kw: branch(1, *a, **kw)
        task = asyncio.create_task(
            self.c.services.reply.generate(self.cid, [await self.message()], set())
        )
        try:
            await asyncio.wait_for(asyncio.gather(*(s.wait() for s in started)), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(all(s.is_set() for s in cancelled))

    async def message(self):
        return await self.c.services.incoming.parse(event("@_bot 小林喜欢什么"))
