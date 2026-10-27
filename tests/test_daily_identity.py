from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from tests.helpers import container, event

NOW = 2_000_000_000.0


def evidence(mid, quote, relation="self"):
    return {"message_id": mid, "quote": quote, "relation": relation}


def alias(subject, name, mid, quote, relation="self", status="confirmed"):
    return {
        "subject_id": subject,
        "alias": name,
        "status": status,
        "evidence": [evidence(mid, quote, relation)],
    }


def fact(subject, mid, quote, what, *, key="", supersedes=(), relation="self"):
    return {
        "kind": "reality",
        "subject_ids": [subject],
        "who": "小林",
        "what": what,
        "evidence": [evidence(mid, quote, relation)],
        "profile_key": key,
        "profile_category": "preference" if key else "",
        "confidence": "confirmed",
        "supersedes": list(supersedes),
    }


class DailyIdentityTests(unittest.IsolatedAsyncioTestCase):
    """Offline contract/control-flow tests, not a claim about live model interpretation."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "identity.db")
        self.c = container(self.path)
        self.cid = await self.c.conversation_repo.get_or_create_conversation("chat-fixture")
        self.configure()

    def configure(self, **options):
        self.service = self.c.services.consolidation
        self.service.config = replace(
            self.c.config,
            consolidation={
                **self.c.config.consolidation,
                "hour": 0,
                "retry_sec": 0,
                "max_calls_per_day": 50,
                "max_context_chars": 16000,
                **options,
            },
        )

    async def asyncTearDown(self):
        await self.c.runtime.close()
        self.tmp.cleanup()

    async def add(self, text, sender="alice", *, cid=None, **kwargs):
        return await self.c.message_repo.append_message(
            self.cid if cid is None else cid,
            "user",
            text,
            sender_name="小林",
            sender_open_id=sender,
            source_message_id=f"platform-{sender}-{text[:8]}",
            ts=NOW - 60,
            **kwargs,
        )

    async def run_daily(self, *, aliases=(), cards=(), now=NOW):
        self.c.llm.responses.append(
            json.dumps({"aliases": list(aliases), "cards": list(cards)}, ensure_ascii=False)
        )
        return await self.service.maybe_consolidate(self.cid, now)

    def state(self):
        with self.c.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM consolidation_state WHERE conversation_id=?", (self.cid,)
            ).fetchone()
            return dict(row) if row else None

    def request(self):
        return json.loads(self.c.llm.calls[-1][-1]["content"])

    async def aliases(self, cid=None):
        return await self.service.identities.aliases(self.cid if cid is None else cid)

    async def test_same_name_accounts_remain_distinct_and_source_metadata_survives(self):
        a = await self.add("小林在呢", "alice")
        b = await self.add("我也是小林，不过是另一位", "bob")
        self.assertTrue(
            await self.run_daily(
                aliases=[
                    alias("alice", "小林", a, "小林在呢"),
                    alias("bob", "小林", b, "我也是小林"),
                ]
            )
        )
        rows = await self.aliases()
        self.assertEqual({r["subject_open_id"] for r in rows}, {"alice", "bob"})
        request = self.request()
        self.assertEqual([r["id"] for r in request["events"]], [a, b])
        self.assertTrue(
            all(
                r["ts"] == NOW - 60 and r["source_message_id"].startswith("platform-")
                for r in request["events"]
            )
        )
        self.assertEqual(self.state()["until_id"], b)
        self.assertFalse(self.c.feishu.calls)

    async def test_natural_mention_nickname_is_learned_without_intro_command(self):
        mid = await self.add("@小林 午饭给你留好了", mentions=({"open_id": "bob", "name": "小林"},))
        self.assertTrue(
            await self.run_daily(aliases=[alias("bob", "小林", mid, "午饭给你留好了", "mention")])
        )
        self.assertEqual((await self.aliases())[0]["subject_open_id"], "bob")
        self.assertNotIn("我叫", self.request()["events"][0]["content"])
        block = await self.c.services.memory.build_recall_block(self.cid, subject_ids={"bob"})
        self.assertIn("小林", block)
        self.assertIn("bob", block)

    async def test_quote_is_not_rebound_to_reporter_and_wrong_self_subject_is_rejected(self):
        mid = await self.add(
            "小明说：“我叫小林。”", "alice", mentions=({"open_id": "bob", "name": "小明"},)
        )
        self.assertFalse(
            await self.run_daily(aliases=[alias("bob", "小林", mid, "我叫小林", "self")])
        )
        self.assertEqual(await self.aliases(), [])
        self.assertEqual(self.state()["until_id"], 0)
        # A contextual uncertain claim is allowed without misbinding Alice.
        self.assertTrue(
            await self.run_daily(
                aliases=[alias("bob", "小林", mid, "我叫小林", "context", "uncertain")], now=NOW + 1
            )
        )
        rows = await self.aliases()
        self.assertEqual(
            [(r["subject_open_id"], r["status"]) for r in rows], [("bob", "uncertain")]
        )
        self.assertIn("引文不是现实身份依据", self.c.llm.calls[-1][0]["content"])

    async def test_third_party_fact_has_distinct_subject_and_reporter(self):
        mid = await self.add(
            "@小林 他喜欢茶", "alice", mentions=({"open_id": "bob", "name": "小林"},)
        )
        self.assertTrue(
            await self.run_daily(cards=[fact("bob", mid, "他喜欢茶", "喜欢茶", relation="mention")])
        )
        card = (await self.c.memory_repo.recent_cards(self.cid, 10))[0]
        self.assertEqual(card["subject_ids"], ["bob"])
        self.assertEqual(card["reporter_ids"], ["alice"])
        self.assertEqual(card["kind"], "hearsay")

    async def test_rename_retains_old_alias_and_original_message_label(self):
        first = await self.add("阿林收到", "alice")
        self.assertTrue(await self.run_daily(aliases=[alias("alice", "阿林", first, "阿林收到")]))
        second = await self.add("以后用小鱼，阿林这个称呼不用了", "alice")
        self.assertTrue(
            await self.run_daily(
                aliases=[
                    alias("alice", "小鱼", second, "以后用小鱼"),
                    alias("alice", "阿林", second, "阿林这个称呼不用了", status="retired"),
                ],
                now=NOW + 86400,
            )
        )
        rows = await self.aliases()
        self.assertEqual(
            {(r["alias"], r["status"]) for r in rows}, {("阿林", "retired"), ("小鱼", "confirmed")}
        )
        with self.c.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM identity_aliases").fetchone()[0], 3)
            self.assertEqual(
                conn.execute("SELECT sender_name FROM messages WHERE id=?", (first,)).fetchone()[0],
                "小林",
            )

    async def test_observer_and_failed_batched_reply_are_both_available_to_daily(self):
        await self.c.services.reply.handle_message_event(
            event("晚饭还是乌龙茶", mention=False, sender="observer")
        )
        await self.c.services.observer.flush_all()
        items = [
            await self.c.services.incoming.parse(
                event("@_bot 第一位来了", sender="alice", message_id="direct-a")
            ),
            await self.c.services.incoming.parse(
                event("@_bot 第二位也来了", sender="bob", message_id="direct-b")
            ),
        ]
        self.c.feishu.fail = "reply"
        await self.c.services.reply._reply_batch(self.cid, items)
        self.assertTrue(await self.run_daily())
        rows = self.request()["events"]
        self.assertEqual({r["sender_open_id"] for r in rows}, {"observer", "alice", "bob"})
        self.assertEqual({r["source"] for r in rows}, {"observer", "direct"})
        with self.c.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_names").fetchone()[0], 0)

    async def test_restart_and_unchanged_events_are_idempotent(self):
        mid = await self.add("小林来了")
        self.assertTrue(await self.run_daily(aliases=[alias("alice", "小林", mid, "小林来了")]))
        await self.c.runtime.close()
        self.c = container(self.path)
        self.configure()
        self.assertFalse(await self.service.maybe_consolidate(self.cid, NOW + 1))
        self.assertFalse(await self.service.maybe_consolidate(self.cid, NOW + 86400))
        self.assertEqual(self.c.llm.calls, [])
        self.assertEqual(len(await self.aliases()), 1)

    async def test_dream_diary_and_pure_images_cannot_supply_real_identity_evidence(self):
        for source in ("dream", "diary"):
            await self.c.message_repo.append_message(
                self.cid, "assistant", "小林其实是王子", source=source
            )
        await self.add("【图片自动描述：姓名小林】", content_kind="image", evidence_text="")
        self.assertFalse(await self.service.maybe_consolidate(self.cid, NOW))
        self.assertEqual(self.c.llm.calls, [])
        mid = await self.add(
            "看看照片 【图片描述：王子小林】", content_kind="post", evidence_text="看看照片"
        )
        self.assertFalse(
            await self.run_daily(aliases=[alias("alice", "王子小林", mid, "王子小林")], now=NOW + 1)
        )
        self.assertEqual(self.request()["events"][0]["content"], "看看照片")
        self.assertEqual(await self.aliases(), [])

    async def test_compressed_raw_history_remains_available_to_daily(self):
        mid = await self.add("我偏爱绿茶")
        with self.c.db.connect() as conn:
            conn.execute("UPDATE conversations SET summary_until_id=? WHERE id=?", (mid, self.cid))
        self.assertTrue(
            await self.run_daily(cards=[fact("alice", mid, "偏爱绿茶", "偏爱绿茶", key="饮品偏好")])
        )
        self.assertEqual(self.request()["events"][0]["id"], mid)

    async def test_new_messages_during_model_call_are_not_swallowed_by_snapshot(self):
        first = await self.add("第一段")
        original = self.c.llm.chat_json
        new_id = None

        async def during(messages, **kwargs):
            nonlocal new_id
            new_id = await self.add("模型调用期间新来的第二段", "bob")
            return await original(messages, **kwargs)

        self.c.llm.chat_json = during
        self.assertTrue(await self.run_daily())
        self.assertEqual(self.state()["until_id"], first)
        self.assertLess(first, new_id)
        self.c.llm.chat_json = original
        self.assertFalse(await self.service.maybe_consolidate(self.cid, NOW + 1))
        self.assertTrue(await self.run_daily(now=NOW + 86400))
        self.assertEqual([r["id"] for r in self.request()["events"]], [new_id])

    async def test_invalid_mixed_batch_cannot_partially_persist_or_advance(self):
        mid = await self.add("小林喜欢茶")
        valid = alias("alice", "小林", mid, "小林喜欢茶")
        bad = fact("invented", mid, "喜欢茶", "喜欢茶")
        self.assertFalse(await self.run_daily(aliases=[valid], cards=[bad]))
        self.assertEqual(self.state()["until_id"], 0)
        self.assertEqual(await self.aliases(), [])
        self.assertEqual(await self.c.memory_repo.recent_cards(self.cid, 10), [])
        self.assertTrue(await self.run_daily(aliases=[valid], now=NOW + 1))

    async def test_database_failure_rolls_back_alias_cards_and_checkpoint(self):
        mid = await self.add("小林喜欢茶")
        with self.c.db.connect() as conn:
            conn.execute(
                "CREATE TRIGGER stop_memory BEFORE INSERT ON memories BEGIN SELECT RAISE(ABORT, 'fixture disk failure'); END"
            )
        self.assertFalse(
            await self.run_daily(
                aliases=[alias("alice", "小林", mid, "小林喜欢茶")],
                cards=[fact("alice", mid, "喜欢茶", "喜欢茶")],
            )
        )
        self.assertEqual(self.state()["until_id"], 0)
        self.assertEqual(await self.aliases(), [])
        with self.c.db.connect() as conn:
            conn.execute("DROP TRIGGER stop_memory")
        self.assertTrue(
            await self.run_daily(aliases=[alias("alice", "小林", mid, "小林喜欢茶")], now=NOW + 1)
        )

    async def test_timeout_does_not_advance_and_consumes_only_bounded_attempt(self):
        await self.add("今晚继续项目")
        self.configure(timeout_sec=0.01)
        cancelled = asyncio.Event()

        async def slow(*args, **kwargs):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        self.c.llm.chat_json = slow
        self.assertFalse(await self.service.maybe_consolidate(self.cid, NOW))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.state()["until_id"], 0)
        self.assertEqual(self.state()["calls_today"], 1)

    async def test_failure_backoff_and_daily_budget_survive_restart(self):
        await self.add("重试依据")
        self.configure(retry_sec=100, max_calls_per_day=2)
        self.c.llm.fail = True
        self.assertFalse(await self.service.maybe_consolidate(self.cid, NOW))
        self.assertFalse(await self.service.maybe_consolidate(self.cid, NOW + 99))
        self.assertEqual(len(self.c.llm.calls), 1)
        self.assertFalse(await self.service.maybe_consolidate(self.cid, NOW + 100))
        self.assertEqual(len(self.c.llm.calls), 2)
        await self.c.runtime.close()
        self.c = container(self.path)
        self.configure(retry_sec=100, max_calls_per_day=2)
        self.assertFalse(await self.service.maybe_consolidate(self.cid, NOW + 200))
        self.assertEqual(self.c.llm.calls, [])
        self.assertTrue(await self.run_daily(now=NOW + 86400))

    async def test_large_message_fragments_are_bounded_and_reassembled_without_gaps(self):
        text = "阿林回复了这句。" * 5000
        mid = await self.add(text)
        fragments = []

        async def model(messages, **kwargs):
            self.c.llm.calls.append(messages)
            self.assertLessEqual(sum(len(m["content"]) for m in messages), 16000)
            row = json.loads(messages[-1]["content"])["events"][0]
            fragments.append(row)
            return json.dumps(
                {"aliases": [alias("alice", "阿林", mid, row["content"][:16])], "cards": []},
                ensure_ascii=False,
            )

        self.c.llm.chat_json = model
        for index in range(20):
            if not await self.service.maybe_consolidate(self.cid, NOW + index):
                break
        self.assertGreater(len(fragments), 1)
        self.assertEqual("".join(r["content"] for r in fragments), text)
        self.assertEqual(
            [r["start_offset"] for r in fragments],
            [sum(len(p["content"]) for p in fragments[:i]) for i in range(len(fragments))],
        )
        self.assertEqual((self.state()["until_id"], self.state()["next_offset"]), (mid, 0))
        self.assertEqual(len(await self.aliases()), 1)

    async def test_message_count_cap_never_advances_beyond_included_rows(self):
        self.configure(max_messages=2)
        ids = [await self.add(f"消息-{i}") for i in range(5)]
        self.assertTrue(await self.run_daily())
        self.assertEqual(self.state()["until_id"], ids[1])
        self.assertTrue(await self.run_daily(now=NOW + 1))
        self.assertEqual([r["id"] for r in self.request()["events"]], ids[2:4])
        self.assertTrue(await self.run_daily(now=NOW + 2))
        self.assertEqual(self.state()["until_id"], ids[-1])

    async def test_daily_work_does_not_hold_or_require_chat_lock(self):
        await self.add("悄悄记住这件事")
        async with self.c.runtime.chat_lock(self.cid):
            self.assertTrue(await self.run_daily())
        self.assertFalse(self.c.feishu.calls)

    async def test_cross_chat_sources_and_facts_cannot_leak(self):
        other = await self.c.conversation_repo.get_or_create_conversation("private-other")
        forbidden = await self.add("私聊中的秘密偏好", cid=other)
        local = await self.add("本群话题")
        self.assertFalse(
            await self.run_daily(aliases=[alias("alice", "秘密称呼", forbidden, "秘密偏好")])
        )
        self.assertEqual([r["id"] for r in self.request()["events"]], [local])
        self.assertEqual(await self.aliases(), [])
        self.assertNotIn("私聊", json.dumps(self.request(), ensure_ascii=False))
        with self.c.db.connect() as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT * FROM consolidation_state WHERE conversation_id=?", (other,)
                ).fetchone()
            )

    async def test_profile_correction_versions_history_and_survives_restart(self):
        first = await self.add("我一直喜欢乌龙茶")
        self.assertTrue(
            await self.run_daily(
                cards=[fact("alice", first, "喜欢乌龙茶", "喜欢乌龙茶", key="饮品偏好")]
            )
        )
        old_id = (await self.c.memory_repo.subject_cards(self.cid, {"alice"}))[0]["id"]
        second = await self.add("纠正一下，现在更爱红茶了")
        self.assertTrue(
            await self.run_daily(
                cards=[
                    fact(
                        "alice",
                        second,
                        "现在更爱红茶",
                        "现在更爱红茶",
                        key="饮品偏好",
                        supersedes=[old_id],
                    )
                ],
                now=NOW + 86400,
            )
        )
        await self.c.runtime.close()
        self.c = container(self.path)
        self.configure()
        current = await self.c.memory_repo.subject_cards(self.cid, {"alice"})
        self.assertEqual([r["what"] for r in current], ["现在更爱红茶"])
        with self.c.db.connect() as conn:
            old = conn.execute("SELECT * FROM memories WHERE id=?", (old_id,)).fetchone()
            self.assertEqual(old["what"], "喜欢乌龙茶")
            self.assertEqual(old["superseded_by"], current[0]["id"])
        block = await self.c.services.memory.build_recall_block(self.cid, subject_ids={"alice"})
        self.assertIn("红茶", block)
        self.assertNotIn("乌龙茶", block)

    async def test_profile_retrieval_uses_current_and_mentioned_ids_not_whole_group(self):
        a = await self.add("我喜欢红茶", "alice")
        b = await self.add("我喜欢绿茶", "bob")
        c = await self.add("我喜欢咖啡", "carol")
        self.assertTrue(
            await self.run_daily(
                cards=[
                    fact("alice", a, "喜欢红茶", "ALICE红茶", key="饮品偏好"),
                    fact("bob", b, "喜欢绿茶", "BOB绿茶", key="饮品偏好"),
                    fact("carol", c, "喜欢咖啡", "CAROL咖啡", key="饮品偏好"),
                ]
            )
        )
        self.c.services.memory.recall_relevant_cards = AsyncMock(
            return_value=await self.c.memory_repo.recent_cards(self.cid, 10)
        )
        block = await self.c.services.memory.build_recall_block(
            self.cid, query="喝什么", subject_ids={"alice", "bob"}
        )
        self.assertIn("ALICE红茶", block)
        self.assertIn("BOB绿茶", block)
        self.assertNotIn("CAROL咖啡", block)
        unknown = await self.c.services.memory.build_recall_block(self.cid, subject_ids={"unknown"})
        self.assertNotIn("红茶", unknown)
        self.assertNotIn("绿茶", unknown)
        self.assertNotIn("咖啡", unknown)

    async def test_profile_retrieval_is_bounded_even_for_many_facts(self):
        ids = [await self.add(f"偏好{i}") for i in range(20)]
        cards = [
            fact("alice", mid, f"偏好{i}", f"FACT-{i:02d}", key=f"偏好键{i}")
            for i, mid in enumerate(ids)
        ]
        self.assertTrue(await self.run_daily(cards=cards))
        block = await self.c.services.memory.build_recall_block(self.cid, subject_ids={"alice"})
        self.assertLessEqual(block.count("FACT-"), 8)
        self.assertLessEqual(len(block), 16000)
        self.assertNotIn("FACT-00", block)

    async def test_cross_subject_profile_supersession_is_rejected_atomically(self):
        first = await self.add("我的偏好是茶", "bob")
        self.assertTrue(
            await self.run_daily(cards=[fact("bob", first, "偏好是茶", "BOB茶", key="饮品偏好")])
        )
        old_id = (await self.c.memory_repo.subject_cards(self.cid, {"bob"}))[0]["id"]
        second = await self.add(
            "我的偏好是咖啡", "alice", mentions=({"open_id": "bob", "name": "小林"},)
        )
        self.assertFalse(
            await self.run_daily(
                cards=[
                    fact(
                        "alice",
                        second,
                        "偏好是咖啡",
                        "ALICE咖啡",
                        key="饮品偏好",
                        supersedes=[old_id],
                    )
                ],
                now=NOW + 86400,
            )
        )
        self.assertEqual(self.state()["until_id"], first)
        self.assertEqual(
            (await self.c.memory_repo.subject_cards(self.cid, {"bob"}))[0]["what"], "BOB茶"
        )
        self.assertEqual(await self.c.memory_repo.subject_cards(self.cid, {"alice"}), [])

    async def test_malformed_contract_matrix_never_moves_checkpoint(self):
        await self.add("只保留真实证据")
        malformed = [
            "not json",
            "[]",
            "null",
            "{}",
            '{"aliases":null,"cards":[]}',
            '{"aliases":[],"cards":false}',
            '{"aliases":[null],"cards":[]}',
            '{"aliases":[],"cards":[{}]}',
            '{"aliases":[],"cards":[],"merge_ids":["alice","bob"]}',
        ]
        for index, response in enumerate(malformed):
            with self.subTest(response=response):
                self.c.llm.responses.append(response)
                self.assertFalse(await self.service.maybe_consolidate(self.cid, NOW + index))
                self.assertEqual(self.state()["until_id"], 0)
                self.assertEqual(await self.aliases(), [])
                self.assertEqual(await self.c.memory_repo.recent_cards(self.cid, 10), [])

    async def test_ongoing_profile_expires_but_audit_row_remains(self):
        mid = await self.add("最近在做一个鲸鱼项目")
        card = fact("alice", mid, "最近在做", "鲸鱼项目进行中", key="当前项目")
        card["profile_category"] = "current_event"
        self.assertTrue(await self.run_daily(cards=[card]))
        current = (await self.c.memory_repo.subject_cards(self.cid, {"alice"}))[0]
        self.assertEqual(current["expires_at"], NOW - 60 + 30 * 86400)
        with self.c.db.connect() as conn:
            conn.execute("UPDATE memories SET expires_at=1 WHERE id=?", (current["id"],))
        self.assertEqual(await self.c.memory_repo.subject_cards(self.cid, {"alice"}), [])
        self.assertEqual(await self.c.memory_repo.recent_cards(self.cid, 10), [])
        with self.c.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT what FROM memories WHERE id=?", (current["id"],)).fetchone()[
                    0
                ],
                "鲸鱼项目进行中",
            )

    async def test_existing_profile_is_not_silently_overwritten_without_supersession(self):
        first = await self.add("我喜欢红茶")
        self.assertTrue(
            await self.run_daily(
                cards=[fact("alice", first, "喜欢红茶", "喜欢红茶", key="饮品偏好")]
            )
        )
        second = await self.add("现在喜欢绿茶")
        self.assertTrue(
            await self.run_daily(
                cards=[fact("alice", second, "喜欢绿茶", "喜欢绿茶", key="饮品偏好")],
                now=NOW + 86400,
            )
        )
        self.assertEqual(self.state()["until_id"], second)
        rows = await self.c.memory_repo.subject_cards(self.cid, {"alice"})
        self.assertEqual(
            {(r["what"], r["confidence"]) for r in rows},
            {("喜欢红茶", "confirmed"), ("喜欢绿茶", "uncertain")},
        )
        self.assertTrue(all(r["superseded_by"] is None for r in rows))

    async def test_unknown_subject_can_remain_unresolved_hearsay_without_binding(self):
        mid = await self.add("听说小林最近很忙，具体是哪位我不确定")
        card = {
            "kind": "hearsay",
            "subject_ids": [],
            "who": "身份待确认的小林",
            "what": "传闻有人最近忙，主体不明确",
            "evidence": [evidence(mid, "具体是哪位我不确定", "context")],
        }
        self.assertTrue(await self.run_daily(cards=[card]))
        self.assertEqual(await self.aliases(), [])
        stored = (await self.c.memory_repo.recent_cards(self.cid, 10))[0]
        self.assertEqual(stored["subject_ids"], [])
        self.assertEqual(stored["kind"], "hearsay")

    async def test_populated_alias_and_profile_context_still_leaves_room_for_new_events(self):
        mid = await self.add("新的一天继续讨论")
        with self.c.db.connect() as conn:
            for index in range(80):
                conn.execute(
                    "INSERT INTO identity_aliases(conversation_id,subject_open_id,alias,status,evidence,created_at,source_until_id) VALUES(?,?,?,?,?,?,?)",
                    (
                        self.cid,
                        "alice",
                        f"别名{index}" + "长" * 60,
                        "uncertain",
                        "[]",
                        NOW - 100,
                        mid,
                    ),
                )
            for index in range(40):
                conn.execute(
                    "INSERT INTO memories(conversation_id,kind,what,created_at,subject_ids,origin,profile_key,profile_category) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        self.cid,
                        "reality",
                        f"资料{index}" + "长" * 250,
                        NOW - 100,
                        '["alice"]',
                        "daily",
                        f"键{index}",
                        "interest",
                    ),
                )
        self.assertTrue(await self.run_daily())
        self.assertEqual(self.request()["events"][0]["id"], mid)
        self.assertLessEqual(sum(len(m["content"]) for m in self.c.llm.calls[-1]), 16000)

    async def test_known_profile_for_correction_is_not_hidden_by_newer_generic_cards(self):
        first = await self.add("喜欢红茶")
        self.assertTrue(
            await self.run_daily(
                cards=[fact("alice", first, "喜欢红茶", "喜欢红茶", key="饮品偏好")]
            )
        )
        old_id = (await self.c.memory_repo.subject_cards(self.cid, {"alice"}))[0]["id"]
        with self.c.db.connect() as conn:
            for index in range(70):
                conn.execute(
                    "INSERT INTO memories(conversation_id,kind,what,created_at,subject_ids,origin) VALUES(?,?,?,?,?,?)",
                    (self.cid, "hearsay", f"普通事件{index}", NOW, '["alice"]', "daily"),
                )
        second = await self.add("现在改喝绿茶了")
        original = self.c.llm.chat_json

        async def model(messages, **kwargs):
            request = json.loads(messages[-1]["content"])
            self.assertIn(old_id, [p["id"] for p in request["known_profiles"]])
            return await original(messages, **kwargs)

        self.c.llm.chat_json = model
        self.assertTrue(
            await self.run_daily(
                cards=[
                    fact(
                        "alice", second, "改喝绿茶", "改喝绿茶", key="饮品偏好", supersedes=[old_id]
                    )
                ],
                now=NOW + 86400,
            )
        )

    async def test_ingress_preserves_platform_time_mentions_and_human_post_evidence(self):
        post = event(
            {
                "title": "看看",
                "content": [
                    [
                        {"tag": "text", "text": "@_bot 小林这张图"},
                        {"tag": "at", "user_id": "bob", "user_name": "小林"},
                        {"tag": "img", "image_key": "photo-key"},
                    ]
                ],
            },
            message_type="post",
            message_id="source-post-id",
        )
        post["message"]["create_time"] = "1700000000123"
        await self.c.services.reply.handle_message_event(post)
        self.assertTrue(await self.run_daily())
        row = self.request()["events"][0]
        self.assertEqual(row["source_message_id"], "source-post-id")
        self.assertEqual(row["ts"], 1700000000.123)
        self.assertIn({"open_id": "bob", "name": "小林"}, row["mentions"])
        self.assertIn("小林这张图", row["content"])
        self.assertNotIn("蓝色的玩具", row["content"])
        raw = await self.c.message_repo.recent_messages(self.cid, 10)
        self.assertIn("蓝色的玩具", raw[0]["content"])

    async def test_single_successful_reply_speaker_name_does_not_mutate_identity(self):
        self.c.llm.responses.append(
            json.dumps({"reply_mode": "reaction", "reply": "欢迎", "speaker_name": "小林"})
        )
        await self.c.services.reply.handle_message_event(event("@_bot 叫我小林吧"))
        with self.c.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_names").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM identity_aliases").fetchone()[0], 0)
        rows = await self.c.message_repo.recent_messages(self.cid, 10)
        mid = rows[0]["id"]
        self.assertTrue(await self.run_daily(aliases=[alias("alice", "小林", mid, "叫我小林吧")]))
        self.assertEqual((await self.aliases())[0]["alias"], "小林")

    async def test_third_party_cannot_retire_confirmed_profile_but_subject_can_correct_it(self):
        first = await self.add("我喜欢茶", "alice")
        self.assertTrue(
            await self.run_daily(cards=[fact("alice", first, "喜欢茶", "喜欢茶", key="饮品偏好")])
        )
        tea_id = (await self.c.memory_repo.subject_cards(self.cid, {"alice"}))[0]["id"]
        report = await self.add(
            "@小林 她现在喜欢咖啡", "bob", mentions=({"open_id": "alice", "name": "小林"},)
        )
        attempted = fact(
            "alice",
            report,
            "她现在喜欢咖啡",
            "喜欢咖啡",
            key="饮品偏好",
            relation="mention",
            supersedes=[tea_id],
        )
        self.assertFalse(await self.run_daily(cards=[attempted], now=NOW + 86400))
        self.assertEqual(self.state()["until_id"], first)
        attempted["supersedes"] = []
        self.assertTrue(await self.run_daily(cards=[attempted], now=NOW + 86401))
        rows = await self.c.memory_repo.subject_cards(self.cid, {"alice"})
        self.assertEqual(
            {(r["what"], r["confidence"], r["kind"]) for r in rows},
            {("喜欢茶", "confirmed", "reality"), ("喜欢咖啡", "uncertain", "hearsay")},
        )
        self_report = await self.add("我确认改喝咖啡了", "alice")
        self.assertTrue(
            await self.run_daily(
                cards=[
                    fact(
                        "alice",
                        self_report,
                        "确认改喝咖啡",
                        "确认改喝咖啡",
                        key="饮品偏好",
                        supersedes=[r["id"] for r in rows],
                    )
                ],
                now=NOW + 2 * 86400,
            )
        )
        current = await self.c.memory_repo.subject_cards(self.cid, {"alice"})
        self.assertEqual(
            [(r["what"], r["confidence"]) for r in current], [("确认改喝咖啡", "confirmed")]
        )
        self.assertEqual(current[0]["reporter_ids"], ["alice"])

    async def test_repeated_identical_profile_claim_is_idempotent(self):
        first = await self.add("我喜欢红茶")
        self.assertTrue(
            await self.run_daily(
                cards=[fact("alice", first, "喜欢红茶", "喜欢红茶", key="饮品偏好")]
            )
        )
        second = await self.add("今天还是喜欢红茶")
        self.assertTrue(
            await self.run_daily(
                cards=[fact("alice", second, "喜欢红茶", "喜欢红茶", key="饮品偏好")],
                now=NOW + 86400,
            )
        )
        self.assertEqual(self.state()["until_id"], second)
        with self.c.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)

    async def test_legacy_unverified_text_can_supply_pending_but_not_confirmed_identity(self):
        mid = await self.add("旧记录里叫我小林", source="imported")
        with self.c.db.connect() as conn:
            conn.execute("UPDATE messages SET evidence_text=NULL WHERE id=?", (mid,))
        self.assertFalse(await self.run_daily(aliases=[alias("alice", "小林", mid, "叫我小林")]))
        self.assertTrue(self.request()["events"][0]["legacy_unverified"])
        self.assertEqual(self.state()["until_id"], 0)
        self.assertTrue(
            await self.run_daily(
                aliases=[alias("alice", "小林", mid, "叫我小林", status="uncertain")],
                cards=[fact("alice", mid, "叫我小林", "旧称呼待核实")],
                now=NOW + 1,
            )
        )
        self.assertEqual((await self.aliases())[0]["status"], "uncertain")
        card = (await self.c.memory_repo.recent_cards(self.cid, 10))[0]
        self.assertEqual((card["kind"], card["confidence"]), ("hearsay", "uncertain"))
