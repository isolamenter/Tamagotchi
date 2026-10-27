from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from dafeiyu.domain.consolidation import validate_result
from dafeiyu.services.incoming import IncomingMessage
from tests.helpers import container

REPLY = json.dumps({"reply_mode": "normal", "reply": "小林更喜欢茶。"}, ensure_ascii=False)


class IdentityRetrievalTests(unittest.IsolatedAsyncioTestCase):
    """Semantic routing fixtures verify boundaries, not real-model interpretation quality."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = container(str(Path(self.tmp.name) / "retrieval.db"))
        self.cid = await self.c.conversation_repo.get_or_create_conversation("chat-fixture")
        self.identities = self.c.services.memory.identities
        self.c.services.style.render_examples_block = AsyncMock(return_value=("", []))

    async def asyncTearDown(self):
        await self.c.runtime.close()
        self.tmp.cleanup()

    def seed_alias(self, subject, name, *, status="confirmed", cid=None):
        with self.c.db.connect() as conn:
            source = conn.execute("SELECT COUNT(*) + 1 FROM identity_aliases").fetchone()[0]
            return conn.execute(
                "INSERT INTO identity_aliases(conversation_id,subject_open_id,alias,status,evidence,created_at,source_until_id) VALUES(?,?,?,?,?,?,?)",
                (
                    self.cid if cid is None else cid,
                    subject,
                    name,
                    status,
                    "[]",
                    time.time(),
                    source,
                ),
            ).lastrowid

    async def seed_profile(self, subject, what, *, cid=None, vector=None, key="偏好"):
        with self.c.db.connect() as conn:
            mid = conn.execute(
                "INSERT INTO memories(conversation_id,kind,what,who,created_at,subject_ids,reporter_ids,evidence,origin,profile_key,profile_category,confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.cid if cid is None else cid,
                    "reality",
                    what,
                    "小林",
                    time.time(),
                    json.dumps([subject]),
                    json.dumps([subject]),
                    json.dumps([{"source_message_id": f"source-{subject}", "ts": 1700000000.0}]),
                    "daily",
                    key,
                    "preference",
                    "confirmed",
                ),
            ).lastrowid
        if vector is not None:
            await self.c.memory_repo.store_embedding(mid, what, vector)
        return mid

    def incoming(self, text, *, mentions=()):
        return IncomingMessage(
            "current-message",
            "chat-fixture",
            "group",
            "alice",
            "阿丽",
            text,
            True,
            mentions=mentions,
            sent_at=time.time(),
        )

    async def route(self, response, *, current=None, history=None, fallback=None, timeout=1):
        self.c.llm.responses.append(response if isinstance(response, str) else json.dumps(response))
        return await self.identities.resolve_subjects(
            self.cid,
            self.c.llm,
            current or [{"sender_open_id": "alice", "text": "小林喜欢什么？", "mentions": []}],
            history or [],
            {"alice"} if fallback is None else fallback,
            timeout,
        )

    async def test_plain_name_without_mention_routes_only_relevant_person_profile(self):
        self.seed_alias("alice", "阿丽")
        self.seed_alias("bob", "小林")
        self.seed_alias("carol", "小陈")
        for subject in ("alice", "bob", "carol"):
            await self.seed_profile(subject, subject.upper() + "_PROFILE_FACT", vector=[1, 0, 0])
        self.c.llm.responses.extend(
            [json.dumps({"subject_ids": ["bob"], "ambiguous": False}), REPLY]
        )
        reply = await self.c.services.reply.generate(
            self.cid, [self.incoming("小林喜欢喝什么？")], set()
        )
        self.assertEqual(reply.text, "小林更喜欢茶。")
        self.assertEqual(len(self.c.llm.calls), 2)
        routing = json.loads(self.c.llm.calls[0][-1]["content"])
        self.assertEqual(routing["current"][0]["mentions"], [])
        self.assertNotIn("PROFILE_FACT", json.dumps(routing))
        prompt = self.c.llm.calls[1][0]["content"]
        self.assertIn("BOB_PROFILE_FACT", prompt)
        self.assertNotIn("ALICE_PROFILE_FACT", prompt)
        self.assertNotIn("CAROL_PROFILE_FACT", prompt)
        self.assertFalse(self.c.feishu.calls)

    async def test_grounded_pronoun_uses_recent_sender_and_mention_context(self):
        await self.seed_profile("bob", "BOB_PRONOUN_FACT", vector=[1, 0, 0])
        await self.c.message_repo.append_message(
            self.cid,
            "user",
            "@小林 刚说要去旅行",
            sender_open_id="alice",
            mentions=({"open_id": "bob", "name": "小林"},),
        )
        self.c.llm.responses.extend(
            [json.dumps({"subject_ids": ["bob"], "ambiguous": False}), REPLY]
        )
        await self.c.services.reply.generate(self.cid, [self.incoming("他最近喜欢什么？")], set())
        routing = json.loads(self.c.llm.calls[0][-1]["content"])
        self.assertIn("bob", routing["candidate_ids"])
        self.assertEqual(routing["recent"][0]["mentions"], [{"open_id": "bob", "name": "小林"}])
        self.assertIn("BOB_PRONOUN_FACT", self.c.llm.calls[1][0]["content"])

    async def test_duplicate_alias_ambiguity_never_selects_arbitrary_profile(self):
        self.seed_alias("bob", "小林")
        self.seed_alias("carol", "小林")
        await self.seed_profile("alice", "ALICE_ARBITRARY_FACT")
        await self.seed_profile("bob", "BOB_ARBITRARY_FACT")
        await self.seed_profile("carol", "CAROL_ARBITRARY_FACT")
        self.c.llm.responses.extend(
            [json.dumps({"subject_ids": ["bob"], "ambiguous": True}), REPLY]
        )
        await self.c.services.reply.generate(self.cid, [self.incoming("小林平时喝什么？")], set())
        prompt = self.c.llm.calls[1][0]["content"]
        self.assertNotIn("ARBITRARY_FACT", prompt)
        self.assertIn("人物指代尚不明确", prompt)
        self.assertIn("自然询问说的是哪位", prompt)

    async def test_unknown_and_cross_chat_subject_ids_are_rejected(self):
        self.seed_alias("bob", "小林")
        other = await self.c.conversation_repo.get_or_create_conversation("other-private-chat")
        self.seed_alias("foreign", "秘密人", cid=other)
        await self.seed_profile("foreign", "FOREIGN_PRIVATE_FACT", cid=other)
        for subject in ("invented", "foreign"):
            with self.subTest(subject=subject):
                ids, notice = await self.route({"subject_ids": [subject], "ambiguous": False})
                self.assertEqual(ids, {"alice"})
                self.assertIn("只采用明确当前", notice)
                payload = self.c.llm.calls[-1][-1]["content"]
                self.assertNotIn("foreign", payload)
                self.assertNotIn("秘密人", payload)
                self.assertNotIn("FOREIGN_PRIVATE_FACT", payload)

    async def test_malformed_router_contract_falls_back_to_current_and_mentions(self):
        self.seed_alias("carol", "小陈")
        malformed = [
            "no json",
            "[]",
            "{}",
            '{"subject_ids":"carol","ambiguous":false}',
            '{"subject_ids":["carol"],"ambiguous":"false"}',
            '{"subject_ids":[],"ambiguous":false,"extra":true}',
        ]
        for response in malformed:
            with self.subTest(response=response):
                ids, notice = await self.route(response, fallback={"alice", "bob"})
                self.assertEqual(ids, {"alice", "bob"})
                self.assertIn("人物路由暂不可用", notice)

    async def test_router_timeout_preserves_explicit_scope_without_group_profile_dump(self):
        for subject, name in (("bob", "小林"), ("carol", "小陈")):
            self.seed_alias(subject, name)
        for subject in ("alice", "bob", "carol"):
            await self.seed_profile(subject, subject.upper() + "_TIMEOUT_FACT")
        cancelled = asyncio.Event()

        async def model(messages, **kwargs):
            self.c.llm.calls.append(messages)
            if "只做本会话人物检索路由" in messages[0]["content"]:
                try:
                    await asyncio.sleep(60)
                finally:
                    cancelled.set()
            return REPLY

        self.c.llm.chat_json = model
        self.c.services.reply.config = replace(
            self.c.config,
            settings={**self.c.config.settings, "memory": {"identity_resolve_timeout_sec": 0.01}},
        )
        await self.c.services.reply.generate(
            self.cid,
            [
                self.incoming(
                    "@小林 给我们推荐饮料", mentions=({"open_id": "bob", "name": "小林"},)
                ),
                self.incoming("他最近喜欢什么？"),
            ],
            set(),
        )
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(self.c.llm.calls), 2)
        prompt = self.c.llm.calls[1][0]["content"]
        self.assertIn("ALICE_TIMEOUT_FACT", prompt)
        self.assertIn("BOB_TIMEOUT_FACT", prompt)
        self.assertNotIn("CAROL_TIMEOUT_FACT", prompt)
        self.assertIn("人物路由暂不可用", prompt)

    async def test_no_extra_routing_call_when_only_explicit_ids_are_available(self):
        self.seed_alias("alice", "阿丽")
        self.seed_alias("bob", "小林")
        result, notice = await self.identities.resolve_subjects(
            self.cid,
            self.c.llm,
            [
                {
                    "sender_open_id": "alice",
                    "text": "我们俩喝什么",
                    "mentions": [{"open_id": "bob", "name": "小林"}],
                }
            ],
            [],
            {"alice", "bob"},
        )
        self.assertEqual(result, {"alice", "bob"})
        self.assertEqual(notice, "")
        self.assertEqual(self.c.llm.calls, [])

    async def test_scoped_vector_ranking_before_topk_preserves_older_relevant_profile(self):
        wanted = await self.seed_profile(
            "alice", "ALICE_OLD_COFFEE", vector=[0.8, 0.6, 0], key="咖啡偏好"
        )
        for index in range(12):
            await self.seed_profile(
                "alice", f"ALICE_NEW_OTHER_{index}", vector=[0, 1, 0], key=f"其他{index}"
            )
        for index in range(6):
            await self.seed_profile(
                "bob", f"BOB_HIGHER_{index}", vector=[1, 0, 0], key=f"别人{index}"
            )
        selected = await self.c.memory_repo.score_cards(self.cid, [1, 0, 0], 6, {"alice"}, True)
        self.assertEqual(selected[0]["id"], wanted)
        self.assertTrue(all(r["subject_ids"] == ["alice"] for r in selected))
        block = await self.c.services.memory.build_recall_block(
            self.cid, query="喝咖啡吗", subject_ids={"alice"}
        )
        self.assertIn("ALICE_OLD_COFFEE", block)
        self.assertNotIn("BOB_HIGHER", block)

    async def test_recall_budget_keeps_complete_metadata_instead_of_cutting_json(self):
        for index in range(14):
            await self.seed_profile("alice", f"事实{index}：" + "长" * 2000, key=f"键{index}")
        for index in range(80):
            self.seed_alias("alice", f"别名{index}" + "长" * 60)
        block = await self.c.services.memory.build_recall_block(self.cid, subject_ids={"alice"})
        self.assertLessEqual(len(block), 16000)
        marker = "【归属元数据；无主体ID的旧记忆不得按同名认领】"
        parsed = 0
        for line in block.splitlines():
            if marker in line:
                metadata = json.loads(line.split(marker, 1)[1])
                self.assertEqual(metadata["subject_ids"], ["alice"])
                self.assertEqual(metadata["sources"], [{"id": "source-alice", "ts": 1700000000.0}])
                parsed += 1
        self.assertGreater(parsed, 0)
        self.assertLess(parsed, 8)
        alias_block = json.loads(block.splitlines()[-1])
        self.assertTrue(alias_block)
        self.assertTrue(all(row["subject_open_id"] == "alice" for row in alias_block))

    async def test_old_ongoing_event_is_not_reactivated_at_consolidation_time(self):
        now = time.time()
        old = now - 91 * 86400
        mid = await self.c.message_repo.append_message(
            self.cid, "user", "最近正在做鲸鱼项目", sender_open_id="alice", ts=old
        )
        service = self.c.services.consolidation
        service.config = replace(
            self.c.config, consolidation={**self.c.config.consolidation, "hour": 0, "retry_sec": 0}
        )
        card = {
            "kind": "reality",
            "subject_ids": ["alice"],
            "what": "鲸鱼项目进行中",
            "profile_key": "当前项目",
            "profile_category": "current_event",
            "confidence": "confirmed",
            "evidence": [{"message_id": mid, "quote": "正在做鲸鱼项目", "relation": "self"}],
        }
        self.c.llm.responses.append(
            json.dumps({"aliases": [], "cards": [card]}, ensure_ascii=False)
        )
        self.assertTrue(await service.maybe_consolidate(self.cid, now))
        self.assertEqual(await self.c.memory_repo.subject_cards(self.cid, {"alice"}), [])
        with self.c.db.connect() as conn:
            stored = conn.execute("SELECT expires_at,what FROM memories").fetchone()
            self.assertEqual(stored["expires_at"], old + 30 * 86400)
            self.assertEqual(stored["what"], "鲸鱼项目进行中")

    async def test_unobserved_alias_is_rejected_even_with_real_sender_and_quote(self):
        events = [
            {
                "id": 1,
                "sender_open_id": "alice",
                "content": "今天喝了茶",
                "source_message_id": "source-a",
                "ts": 1700000000,
                "start_offset": 0,
                "mentions": [],
            }
        ]
        result = {
            "aliases": [
                {
                    "subject_id": "alice",
                    "alias": "不存在的名字",
                    "status": "confirmed",
                    "evidence": [{"message_id": 1, "quote": "喝了茶", "relation": "self"}],
                }
            ],
            "cards": [],
        }
        with self.assertRaisesRegex(ValueError, "alias has no observed"):
            validate_result(json.dumps(result), events)
        events[0]["mentions"] = [{"open_id": "bob", "name": "小林"}]
        result["aliases"][0].update(subject_id="bob", alias="小林")
        result["aliases"][0]["evidence"][0]["relation"] = "mention"
        aliases, _ = validate_result(json.dumps(result), events)
        self.assertEqual(aliases[0]["subject_id"], "bob")

    async def test_uncertain_alias_does_not_replace_confirmed_history(self):
        confirmed = self.seed_alias("bob", "小林", status="confirmed")
        uncertain = self.seed_alias("bob", "小林", status="uncertain")
        self.assertGreater(uncertain, confirmed)
        aliases = await self.identities.aliases(self.cid, {"bob"})
        self.assertEqual(
            [(r["alias"], r["status"], r["id"]) for r in aliases],
            [("小林", "confirmed", confirmed)],
        )
        retired = self.seed_alias("bob", "小林", status="retired")
        self.seed_alias("bob", "小林", status="uncertain")
        aliases = await self.identities.aliases(self.cid, {"bob"})
        self.assertEqual([(r["status"], r["id"]) for r in aliases], [("retired", retired)])
        with self.c.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM identity_aliases").fetchone()[0], 4)

    async def test_unassigned_legacy_same_name_is_not_rendered_as_current_person_fact(self):
        await self.seed_profile("alice", "本人确认喜欢茶")
        with self.c.db.connect() as conn:
            conn.execute(
                "INSERT INTO memories(conversation_id,kind,who,what,created_at) VALUES(?,?,?,?,?)",
                (self.cid, "reality", "小林", "旧卡声称喜欢咖啡", time.time()),
            )
        block = await self.c.services.memory.build_recall_block(self.cid, subject_ids={"alice"})
        legacy_line = next(line for line in block.splitlines() if "旧卡声称" in line)
        self.assertIn("未归属历史线索（非当前个人事实）", legacy_line)
        self.assertIn('"subject_ids": []', legacy_line)
        self.assertIn('"confidence": "uncertain"', legacy_line)
        self.assertIn("本人确认喜欢茶", block)

    async def test_mentioned_addressee_is_not_assumed_to_be_requested_profile_subject(self):
        self.seed_alias("charlie", "小陈")
        self.c.llm.responses.append('{"subject_ids":["charlie"],"ambiguous":false}')
        subjects, notice = await self.identities.resolve_subjects(
            self.cid,
            self.c.llm,
            [
                {
                    "sender_open_id": "alice",
                    "text": "@小林，你知道小陈喜欢什么吗？",
                    "mentions": [{"open_id": "bob", "name": "小林"}],
                }
            ],
            [],
            {"alice", "bob"},
        )
        self.assertEqual(subjects, {"charlie"})
        self.assertEqual(notice, "")
        self.assertEqual(len(self.c.llm.calls), 1)
