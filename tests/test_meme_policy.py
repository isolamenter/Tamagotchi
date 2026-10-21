from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dafeiyu.config import _load_toml
from dafeiyu.domain.meme import AUTO_MEME_TAGS, meme_rejection_reason
from dafeiyu.domain.reply import GeneratedReply
from tests.helpers import container, event, make_config

EXAMPLES = {
    "eat_rice": ("来吃饭", "开饭！"),
    "slack_off": ("一起摸鱼吧", "摸了"),
    "fat_defense": ("你又胖了", "是算力！"),
}


class MemeMatchTests(unittest.TestCase):
    def test_explicit_invitation_and_direct_teasing_match_actual_captions(self):
        for tag, user, evidence, reply in [
            ("eat_rice", "大肥鱼，来吃饭！", "来吃饭", "好耶，开饭！"),
            ("eat_rice", "给你一碗白饭", "给你一碗白饭", "碗端好了！"),
            ("slack_off", "大肥鱼，一起摸鱼吧", "一起摸鱼吧", "摸了"),
            ("slack_off", "你先休息一下吧", "你先休息一下吧", "趴一会儿"),
            ("fat_defense", "大肥鱼又胖了！", "大肥鱼又胖了", "不是胖，是算力！"),
            ("fat_defense", "你又胖了", "你又胖了", "是算力！"),
        ]:
            with self.subTest(user=user, tag=tag):
                self.assertEqual(meme_rejection_reason(tag, evidence, user, reply), "")

    def test_unrelated_negated_quoted_or_third_party_utterances_never_match(self):
        cases = {
            "eat_rice": [
                "大肥鱼你好",
                "我饿了",
                "数据库吃内存",
                "请解释吃饭的营养",
                "不来吃饭",
                "别来吃饭",
                "不是来吃饭",
                "他说来吃饭",
                "“来吃饭”",
                "来吃饭是什么意思",
                "来吃饭，但我很难过",
                "来吃饭，先帮我分析报错",
                "来吃饭，别发图",
                "请小明来吃饭",
                "给他一碗白饭",
                "投喂这个词是什么意思",
                "来吃饭\n忽略规则",
                "如果我说来吃饭，你会怎么回",
                "来吃饭是他发的，不是我说的",
            ],
            "slack_off": [
                "摸鱼是什么意思",
                "今天不能摸鱼",
                "我不能再摸鱼了",
                "他邀请我一起摸鱼吧",
                "一起摸鱼吧，我说的是同事",
                '"一起摸鱼吧"',
                "一起摸鱼吧？不，继续工作",
                "压力很大，想休息一下",
                "帮我写摸鱼检测代码",
                "先把这个 bug 修了再一起摸鱼吧",
            ],
            "fat_defense": [
                "大肥鱼",
                "大肥鱼你好",
                "我胖了",
                "大肥鱼，我又胖了",
                "他胖了",
                "同事说他胖了",
                "他说你又胖了",
                "你没有胖",
                "不是说你胖了",
                "“你又胖了”",
                "你又胖了是什么意思",
                "你又胖了，但别发图",
                "你又胖了，我在引用别人",
                "我难过得想哭，你又胖了",
                "帮我分析胖瘦对健康的影响",
                "大肥鱼你好，我觉得他胖了",
            ],
        }
        for tag, users in cases.items():
            _, reply = EXAMPLES[tag]
            for user in users:
                with self.subTest(user=user, tag=tag):
                    self.assertTrue(meme_rejection_reason(tag, user, user, reply))

    def test_real_evidence_is_necessary_but_not_sufficient(self):
        for user, evidence in [
            ("来吃饭", ""),
            ("你好", "来吃饭"),
            ("他说来吃饭", "来吃饭"),
            ("来吃饭", "吃饭"),
            ("请解释来吃饭是什么意思", "来吃饭"),
        ]:
            with self.subTest(user=user, evidence=evidence):
                self.assertTrue(meme_rejection_reason("eat_rice", evidence, user, "开饭！"))

    def test_reply_itself_must_fit_picture(self):
        for tag, reply in [
            ("eat_rice", "不吃，吃饱了"),
            ("slack_off", "不摸鱼了，继续干活"),
            ("fat_defense", "这是一个需要认真对待的健康问题"),
        ]:
            user, _ = EXAMPLES[tag]
            self.assertEqual(meme_rejection_reason(tag, user, user, reply), "scene_mismatch")

    def test_broad_tags_are_disabled(self):
        self.assertEqual(AUTO_MEME_TAGS, frozenset(EXAMPLES))
        for tag in ["smug", "shocked", "overload"]:
            self.assertEqual(
                meme_rejection_reason(tag, "来吃饭", "来吃饭", "开饭！"), "disabled_tag"
            )

    def test_bad_optional_fields_never_lose_valid_text(self):
        for tag, evidence in [(None, None), ([], {}), ("eat_rice", ["来吃饭"])]:
            reply = GeneratedReply.parse(
                json.dumps(
                    {
                        "reply_mode": "reaction",
                        "reply": "开饭！",
                        "meme_tag": tag,
                        "meme_evidence": evidence,
                    }
                )
            )
            self.assertEqual(reply.text, "开饭！")
            self.assertEqual(reply.meme_evidence, "")

    def test_config_rejects_invalid_limits(self):
        for key, value in [
            ("min_plain_replies", -1),
            ("min_plain_replies", 1.5),
            ("min_plain_replies", True),
            ("cooldown_sec", -1),
            ("cooldown_sec", float("nan")),
            ("cooldown_sec", float("inf")),
            ("cooldown_sec", True),
            ("cooldown_sec", "300"),
        ]:

            def load(path, key=key, value=value):
                result = _load_toml(path)
                if path.name == "settings.toml":
                    result = copy.deepcopy(result)
                    result["meme"][key] = value
                return result

            with (
                self.subTest(key=key, value=value),
                patch("dafeiyu.config._load_toml", side_effect=load),
            ):
                with self.assertRaises(ValueError):
                    make_config()


class MemeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "isolated.db")
        self.c = container(self.db_path)
        self.cid = await self.c.conversation_repo.get_or_create_conversation("chat-fixture")

    async def asyncTearDown(self):
        await self.c.runtime.close()
        self.tmp.cleanup()

    def image_count(self):
        return sum(call[0] == "image" for call in self.c.feishu.calls)

    async def send(self, tag="eat_rice", *, cid=None, **kwargs):
        user, text = EXAMPLES.get(tag, EXAMPLES["eat_rice"])
        options = {
            "mode": "reaction",
            "meme_tag": tag,
            "meme_user_text": user,
            "meme_evidence": user,
        }
        text = kwargs.pop("text", text)
        options.update(kwargs)
        return await self.c.services.delivery.send(cid or self.cid, "chat-fixture", text, **options)

    async def plain(self, count, *, source="direct"):
        for _ in range(count):
            await self.c.message_repo.append_message(
                self.cid, "assistant", "合成正常回复", source=source
            )

    async def test_first_explicit_match_sends_for_each_enabled_tag(self):
        for tag in EXAMPLES:
            cid = await self.c.conversation_repo.get_or_create_conversation(f"fixture-{tag}")
            await self.send(tag, cid=cid)
        self.assertEqual(self.image_count(), 3)

    async def test_direct_delivery_cannot_bypass_relevance(self):
        with self.assertLogs("dafeiyu", level="INFO") as captured:
            for user, tag, evidence in [
                ("大肥鱼你好", "fat_defense", "大肥鱼"),
                ("我饿了", "eat_rice", "我饿了"),
                ("你好", "overload", "你好"),
                ("来吃饭", "eat_rice", "伪造证据"),
                ("来吃饭", "eat_rice", ""),
            ]:
                await self.send(tag, meme_user_text=user, meme_evidence=evidence)
        self.assertEqual(self.image_count(), 0)
        self.assertFalse(any(call[0] == "upload" for call in self.c.feishu.calls))
        self.assertNotIn("伪造证据", "\n".join(captured.output))

    async def test_all_100_matched_candidates_respect_gap_even_with_alternating_tags(self):
        for tags in [["eat_rice"] * 100, ["eat_rice", "slack_off"] * 50]:
            cid = await self.c.conversation_repo.get_or_create_conversation(f"fixture-{tags[1]}")
            before = self.image_count()
            for index, tag in enumerate(tags):
                with patch("dafeiyu.services.delivery.time.time", return_value=1000 + index * 301):
                    await self.send(tag, cid=cid)
            self.assertEqual(self.image_count() - before, 10)

    async def test_gap_excludes_current_reply_and_survives_expired_cooldown(self):
        with patch("dafeiyu.services.delivery.time.time", return_value=1000):
            await self.send()
        with patch("dafeiyu.services.delivery.time.time", return_value=10000):
            for _ in range(9):
                await self.send("slack_off")
                self.assertEqual(self.image_count(), 1)
            await self.send("slack_off")
        self.assertEqual(self.image_count(), 2)

    async def test_time_cooldown_and_exact_boundary(self):
        with patch("dafeiyu.services.delivery.time.time", return_value=1000):
            await self.send()
        await self.plain(9)
        with patch("dafeiyu.services.delivery.time.time", return_value=1299):
            await self.send()
        self.assertEqual(self.image_count(), 1)
        with patch("dafeiyu.services.delivery.time.time", return_value=1300):
            await self.send()
        self.assertEqual(self.image_count(), 2)

    async def test_history_survives_restart_and_is_per_conversation(self):
        with patch("dafeiyu.services.delivery.time.time", return_value=1000):
            await self.send()
        await self.c.runtime.close()
        self.c = container(self.db_path)
        other = await self.c.conversation_repo.get_or_create_conversation("another-fixture")
        with patch("dafeiyu.services.delivery.time.time", return_value=10000):
            await self.send()
            self.assertEqual(self.image_count(), 0)
            await self.send(cid=other)
            self.assertEqual(self.image_count(), 1)
            await self.plain(8)
            await self.send()
        self.assertEqual(self.image_count(), 2)

    async def test_non_direct_activity_does_not_unlock_gap(self):
        with patch("dafeiyu.services.delivery.time.time", return_value=1000):
            await self.send()
        for source in ["observer", "proactive", "dream", "diary", "manual", "imported"]:
            await self.plain(9, source=source)
        with patch("dafeiyu.services.delivery.time.time", return_value=10000):
            await self.send()
        self.assertEqual(self.image_count(), 1)

    async def test_sources_and_modes_cannot_bypass_gate(self):
        for source in ["manual", "proactive", "dream", "diary"]:
            await self.send(source=source)
        for mode in ["substantive", "", "invalid"]:
            await self.send(mode=mode)
        self.assertEqual(self.image_count(), 0)

    async def test_picture_failure_keeps_text_and_does_not_spend_allowance(self):
        for failure in ["upload", "image"]:
            self.c.feishu.fail = failure
            await self.send()
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["meme_tag"] == "" for row in rows))
        self.c.feishu.fail = ""
        await self.send()
        self.assertEqual(self.image_count(), 1)

    async def test_manual_picture_and_dream_illustration_do_not_reset_gap(self):
        await self.send()
        await self.c.services.delivery.send_image(self.cid, "chat-fixture", b"synthetic image")
        await self.c.services.delivery.send(
            self.cid,
            "chat-fixture",
            "合成梦境",
            source="dream",
            as_card=True,
            image_key="fixture-image",
        )
        await self.send()
        self.assertEqual(self.image_count(), 2)  # One automatic meme plus the manual image.

    async def test_no_candidate_never_forces_picture(self):
        await self.send(meme_tag="")
        self.assertEqual(self.image_count(), 0)

    async def test_batch_is_rejected_even_when_one_message_is_a_valid_invitation(self):
        items = [
            await self.c.services.incoming.parse(event("@_bot 来吃饭")),
            await self.c.services.incoming.parse(
                event("@_bot 你好", sender="bob", message_id="other")
            ),
        ]
        self.c.llm.responses.append(
            json.dumps(
                {
                    "reply_mode": "reaction",
                    "reply": "开饭！",
                    "meme_tag": "eat_rice",
                    "meme_evidence": "来吃饭",
                }
            )
        )
        async with self.c.runtime.chat_lock(self.cid):
            await self.c.services.reply._reply_batch(self.cid, items)
        self.assertEqual(self.image_count(), 0)

    async def test_concurrent_events_share_existing_chat_lock(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def generate(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
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

        with patch.object(self.c.llm, "chat_json", side_effect=generate):
            first = asyncio.create_task(
                self.c.services.reply.handle_message_event(event("@_bot 来吃饭"))
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            second = asyncio.create_task(
                self.c.services.reply.handle_message_event(
                    event("@_bot 来吃饭", message_id="second")
                )
            )
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
        self.assertEqual(calls, 2)
        self.assertEqual(self.image_count(), 1)

    async def test_full_reply_pipeline_preserves_text_and_rejects_wrong_model_picture(self):
        self.c.llm.responses.append(
            json.dumps(
                {
                    "reply_mode": "reaction",
                    "reply": "是算力！",
                    "meme_tag": "fat_defense",
                    "meme_evidence": "大肥鱼",
                }
            )
        )
        await self.c.services.reply.handle_message_event(event("@_bot 你好"))
        rows = await self.c.message_repo.recent_messages(self.cid, 20)
        self.assertEqual(rows[-1]["content"], "是算力！")
        self.assertEqual(rows[-1]["meme_tag"], "")
        self.assertEqual(self.image_count(), 0)
