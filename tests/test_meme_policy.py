from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dafeiyu.config import ROOT_DIR, _load_toml
from dafeiyu.domain.meme import MemeCatalog
from dafeiyu.domain.reply import GeneratedReply
from tests.helpers import container, event, make_config

APPROVED = '{"allow":true,"reason":"matched"}'


class MemeCatalogTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.catalog = self.config.meme_catalog

    def test_catalog_covers_assets_and_drives_prompt(self):
        self.assertEqual(len(self.catalog.scenes), 26)
        enabled = [s for s in self.catalog.scenes.values() if s["enabled"]]
        self.assertEqual(len(enabled), 23)
        self.assertEqual(
            {s["image"] for s in self.catalog.scenes.values()},
            {p.name for p in self.config.memes_dir.glob("*.png")},
        )
        for scene in enabled:
            self.assertIn(scene["tag"] + "：", self.config.json_output_prompt)
            self.assertGreaterEqual(len(scene["positive_examples"]), 2)
            self.assertGreaterEqual(len(scene["negative_examples"]), 3)
            for example in scene["positive_examples"]:
                self.assertEqual(
                    self.catalog.rejection_reason(
                        scene["tag"], example["evidence"], example["user"], example["reply"]
                    ),
                    "",
                )
        for tag in ["overload", "smug", "shocked"]:
            self.assertNotIn(tag + "：", self.config.json_output_prompt)
            self.assertEqual(
                self.catalog.rejection_reason(tag, "你好", "你好", "在呢"), "disabled_tag"
            )

    def test_catalog_rejects_duplicate_unsafe_and_missing_resources(self):
        original = _load_toml(ROOT_DIR / "config/meme_corpus.toml")
        for change in ["duplicate", "path", "missing", "examples", "caption", "empty"]:
            corpus = copy.deepcopy(original)
            if change == "duplicate":
                corpus["memes"].append(corpus["memes"][0])
            elif change == "path":
                corpus["memes"][0]["image"] = "../private.png"
            elif change == "missing":
                corpus["memes"][0].update(tag="missing", image="missing.png")
            elif change == "examples":
                corpus["memes"][0]["negative_examples"] = []
            elif change == "caption":
                corpus["memes"][0]["caption"] = ""
            else:
                corpus["memes"] = []
            with self.subTest(change=change), self.assertRaises(ValueError):
                MemeCatalog(corpus, self.config.memes_dir)

    def test_evidence_and_opt_out_are_hard_gates_context_defers_to_semantic_review(self):
        for tag, evidence, user, reason in [
            ("hello", "", "你好", "missing_evidence"),
            ("hello", "伪造证据", "你好", "missing_evidence"),
            ("unknown", "你好", "你好", "disabled_tag"),
            ("hello", "你好", "你好，别发图", "opt_out"),
            ("hello", "你好", "你好，只要文字", "opt_out"),
            ("hello", "你好", "你好，不要给我发送表情包", "opt_out"),
            ("hello", "你好", "你好，帮我分析数据库报错", ""),
            ("hello", "你好", "你好，我很难过", ""),
        ]:
            with self.subTest(user=user, tag=tag):
                self.assertEqual(self.catalog.rejection_reason(tag, evidence, user, "在呢"), reason)

    def test_natural_paraphrases_reach_semantic_review_without_fixed_reply(self):
        for tag, user, reply in [
            ("eat_rice", "大肥鱼，来吃饭吧，我给你留了一碗", "好呀，碗已经端好了，给我留一口！"),
            ("slack_off", "一起摸鱼吧", "好呀，我先趴一会儿。"),
            ("fat_defense", "你又胖了", "这不是胖，是算力储备！"),
            ("hug", "我有点难过，可以抱抱我吗", "来，抱抱你，我在呢。"),
            ("birthday", "今天我生日，祝我生日快乐吧", "生日快乐，愿你天天开心。"),
        ]:
            with self.subTest(tag=tag):
                self.assertEqual(self.catalog.rejection_reason(tag, user, user, reply), "")
                payload = json.loads(
                    self.catalog.review_messages(tag, user, user, reply)[1]["content"]
                )
                self.assertEqual(payload["user_message"], user)
                self.assertEqual(payload["final_reply"], reply)
                self.assertEqual(payload["scene"]["caption"], self.catalog.scenes[tag]["caption"])

    def test_invalid_optional_fields_keep_valid_text(self):
        for tag, evidence in [(None, None), ([], {}), ("hello", ["你好"])]:
            reply = GeneratedReply.parse(
                json.dumps(
                    dict(reply_mode="reaction", reply="在呢", meme_tag=tag, meme_evidence=evidence)
                )
            )
            self.assertEqual(reply.text, "在呢")
            self.assertEqual(reply.meme_evidence, "")


class MemeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = container(str(Path(self.tmp.name) / "isolated.db"))
        self.cid = await self.c.conversation_repo.get_or_create_conversation("chat-fixture")
        self.c.llm.chat_json = AsyncMock(return_value=APPROVED)

    async def asyncTearDown(self):
        await self.c.runtime.close()
        self.tmp.cleanup()

    def image_count(self):
        return sum(c[0] == "image" for c in self.c.feishu.calls)

    async def send(self, tag="eat_rice", **kwargs):
        scene = self.c.config.meme_catalog.scenes.get(
            tag, self.c.config.meme_catalog.scenes["eat_rice"]
        )
        example = (
            scene["positive_examples"][0]
            if scene["positive_examples"]
            else {"user": "你好", "reply": "在呢", "evidence": "你好"}
        )
        options = dict(
            mode="reaction",
            meme_tag=tag,
            meme_user_text=example["user"],
            meme_evidence=example["evidence"],
        )
        text = kwargs.pop("text", example["reply"])
        options.update(kwargs)
        return await self.c.services.delivery.send(self.cid, "chat-fixture", text, **options)

    async def test_every_enabled_scene_can_send_and_record_after_approval(self):
        for tag, scene in self.c.config.meme_catalog.scenes.items():
            if scene["enabled"]:
                message = await self.send(tag)
                with self.c.db.connect() as conn:
                    self.assertEqual(
                        conn.execute(
                            "SELECT meme_tag FROM messages WHERE id=?", (message,)
                        ).fetchone()[0],
                        tag,
                    )
        self.assertEqual(self.image_count(), 23)
        self.assertEqual(self.c.llm.chat_json.await_count, 23)

    async def test_consecutive_same_and_alternating_tags_have_no_cooldown_or_gap(self):
        with patch("dafeiyu.services.delivery.time.time", return_value=1000):
            for tag in ["eat_rice"] * 10 + ["hello", "tea"] * 5:
                await self.send(tag)
        self.assertEqual(self.image_count(), 20)
        self.assertEqual(sum(c[0] == "upload" for c in self.c.feishu.calls), 3)

    async def test_hard_rejections_never_call_reviewer_or_upload(self):
        for options in [
            dict(meme_evidence=""),
            dict(meme_evidence="伪造证据"),
            dict(meme_user_text="来吃饭，别发图", meme_evidence="来吃饭"),
            dict(source="proactive"),
            dict(source="dream"),
            dict(source="manual"),
            dict(mode="substantive"),
            dict(mode=""),
            dict(mode="invalid"),
        ]:
            await self.send(**options)
        await self.send("smug")
        await self.send("../unsafe")
        self.c.llm.chat_json.assert_not_awaited()
        self.assertEqual(self.image_count(), 0)

    async def test_rejected_review_preserves_text_and_logs_only_reason(self):
        self.c.llm.chat_json.return_value = '{"allow":false,"reason":"target_mismatch"}'
        with self.assertLogs("dafeiyu", level="INFO") as logs:
            await self.send(meme_user_text="他说来吃饭，不是在叫你", meme_evidence="来吃饭")
        self.assertIn("result=target_mismatch", "\n".join(logs.output))
        self.assertNotIn("他说来吃饭", "\n".join(logs.output))
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual(rows[-1]["meme_tag"], "")
        self.assertTrue(rows[-1]["content"])
        self.assertEqual(self.image_count(), 0)

    async def test_non_boolean_inconsistent_and_malformed_review_never_allow(self):
        for raw in [
            "{}",
            "[]",
            "not json",
            '{"allow":"true","reason":"matched"}',
            '{"allow":1,"reason":"matched"}',
            '{"allow":true,"reason":"ambiguous"}',
            '{"allow":false,"reason":"matched"}',
            '{"allow":true}',
        ]:
            self.c.llm.chat_json.return_value = raw
            await self.send()
        self.assertEqual(self.image_count(), 0)

    async def test_review_failure_timeout_and_retry_keep_text(self):
        self.c.llm.chat_json.side_effect = RuntimeError("fixture")
        await self.send()

        async def slow(*args, **kwargs):
            await asyncio.Event().wait()

        self.c.services.delivery.config = replace(self.c.config, meme_review_timeout_sec=0.001)
        self.c.llm.chat_json.side_effect = slow
        await self.send()
        self.c.llm.chat_json.side_effect = None
        await self.send()
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual([r["meme_tag"] for r in rows], ["", "", "eat_rice"])
        self.assertEqual(self.image_count(), 1)

    async def test_text_is_delivered_before_review_and_failure_prevents_review(self):
        async def approve(*args, **kwargs):
            self.assertEqual(self.c.feishu.calls[0][0], "text")
            self.assertEqual(self.image_count(), 0)
            return APPROVED

        self.c.llm.chat_json.side_effect = approve
        await self.send()
        self.c.llm.chat_json.reset_mock()
        self.c.feishu.fail = "text"
        with self.assertRaises(RuntimeError):
            await self.send()
        self.c.llm.chat_json.assert_not_awaited()

    async def test_image_failure_keeps_text_and_retry_is_available(self):
        for failure in ["upload", "image"]:
            self.c.feishu.fail = failure
            await self.send()
        self.c.feishu.fail = ""
        await self.send()
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual([r["meme_tag"] for r in rows], ["", "", "eat_rice"])
        self.assertEqual(self.image_count(), 1)

    async def test_no_candidate_has_no_review_or_forced_picture(self):
        await self.send(meme_tag="")
        self.c.llm.chat_json.assert_not_awaited()
        self.assertEqual(self.image_count(), 0)

    async def test_full_reply_pipeline_generates_then_reviews_exact_final_reply(self):
        reply = {
            "reply_mode": "normal",
            "reply": "在呢，刚好听见你叫我啦。",
            "meme_tag": "hello",
            "meme_evidence": "你好",
        }
        self.c.llm.chat_json.side_effect = [json.dumps(reply), APPROVED]
        await self.c.services.reply.handle_message_event(event("@_bot 你好"))
        self.assertEqual(self.c.llm.chat_json.await_count, 2)
        payload = json.loads(self.c.llm.chat_json.call_args_list[1].args[0][1]["content"])
        self.assertEqual(payload["final_reply"], reply["reply"])
        self.assertEqual(self.image_count(), 1)

    async def test_merged_batch_does_not_review_or_send_meme(self):
        self.c.llm.chat_json.return_value = json.dumps(
            {
                "reply_mode": "reaction",
                "reply": "开饭！",
                "meme_tag": "eat_rice",
                "meme_evidence": "来吃饭",
            }
        )
        items = [
            await self.c.services.incoming.parse(event("@_bot 来吃饭")),
            await self.c.services.incoming.parse(
                event("@_bot 你好", sender="bob", message_id="other")
            ),
        ]
        async with self.c.runtime.chat_lock(self.cid):
            await self.c.services.reply._reply_batch(self.cid, items)
        self.assertEqual(self.c.llm.chat_json.await_count, 1)
        self.assertEqual(self.image_count(), 0)

    async def test_concurrent_events_keep_existing_lock_and_allow_each_valid_reply(self):
        entered, release = asyncio.Event(), asyncio.Event()
        generated = 0

        async def respond(messages, **kwargs):
            nonlocal generated
            if "独立的梗图语境审核员" in messages[0]["content"]:
                return APPROVED
            generated += 1
            if generated == 1:
                entered.set()
                await release.wait()
            return json.dumps(
                {
                    "reply_mode": "reaction",
                    "reply": "开饭！",
                    "meme_tag": "eat_rice",
                    "meme_evidence": "来吃饭",
                }
            )

        self.c.llm.chat_json.side_effect = respond
        first = asyncio.create_task(
            self.c.services.reply.handle_message_event(event("@_bot 来吃饭"))
        )
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(
            self.c.services.reply.handle_message_event(event("@_bot 来吃饭", message_id="second"))
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        self.assertEqual(generated, 2)
        self.assertEqual(self.image_count(), 2)
