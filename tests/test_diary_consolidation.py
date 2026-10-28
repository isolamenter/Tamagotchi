from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.helpers import container, fact

NOW = datetime(2026, 10, 26, 11, tzinfo=UTC).timestamp()
DATE = "2026-10-26"


class DiaryConsolidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "diary.db")
        self.c = container(self.path)
        self.cid = await self.c.conversation_repo.get_or_create_conversation("diary-fixture")
        self.daily = self.c.services.consolidation
        self.daily.config = replace(
            self.c.config, consolidation={**self.c.config.consolidation, "retry_sec": 0}
        )
        self.event = next(e for e in self.c.config.scheduled_events if e["kind"] == "diary")

    async def asyncTearDown(self):
        await self.c.runtime.close()
        self.tmp.cleanup()

    async def add(self, text="我喜欢红茶", **kwargs):
        return await self.c.message_repo.append_message(
            self.cid,
            "user",
            text,
            sender_open_id="alice",
            ts=kwargs.pop("ts", NOW - 30),
            **kwargs,
        )

    def draft(self, mid, quote="我喜欢红茶", text="今天听你说喜欢红茶，我也想捧杯热茶歇一会儿。"):
        return {"text": text, "evidence": [{"message_id": mid, "quote": quote}]}

    async def consolidate(self, diary=None, cards=(), now=NOW):
        self.c.llm.responses.append(
            json.dumps({"aliases": [], "cards": list(cards), "diary": diary})
        )
        return await self.daily.maybe_consolidate(self.cid, now)

    async def test_same_call_saves_grounded_memory_and_diary_without_sending(self):
        mid = await self.add()
        self.assertTrue(
            await self.consolidate(self.draft(mid), [fact("alice", mid, "我喜欢红茶", "喜欢红茶")])
        )
        self.assertEqual(len(self.c.llm.calls), 1)
        self.assertEqual(len(await self.c.memory_repo.recent_cards(self.cid, 10)), 1)
        stored = await self.daily.identities.diary(self.cid, DATE)
        self.assertEqual(stored["text"], self.draft(mid)["text"])
        self.assertFalse(self.c.feishu.calls)

    async def test_model_references_database_id_while_storage_keeps_platform_id(self):
        mid = await self.add(source_message_id="synthetic-message-1")
        self.assertTrue(
            await self.consolidate(self.draft(mid), [fact("alice", mid, "我喜欢红茶", "喜欢红茶")])
        )
        event = json.loads(self.c.llm.calls[0][-1]["content"])["events"][0]
        self.assertEqual(event["id"], mid)
        self.assertNotIn("source_message_id", event)
        cards = await self.c.memory_repo.recent_cards(self.cid, 10)
        self.assertEqual(cards[0]["evidence"][0]["source_message_id"], "synthetic-message-1")

    async def test_batches_publish_only_when_complete_and_keep_earlier_candidate(self):
        self.daily.config = replace(
            self.daily.config,
            consolidation={**self.daily.config.consolidation, "max_messages": 1},
        )
        mid = await self.add()
        last = await self.add("随便聊两句")
        self.assertTrue(await self.consolidate(self.draft(mid)))
        self.assertIsNone(await self.daily.identities.diary(self.cid, DATE))
        # Restart preserves an unfinished candidate without exposing it for publication.
        restarted = container(self.path)
        try:
            self.assertIsNone(
                await restarted.services.consolidation.identities.diary(self.cid, DATE)
            )
        finally:
            await restarted.runtime.close()
        self.assertTrue(await self.consolidate(now=NOW + 1))
        self.assertEqual(
            (await self.daily.identities.diary(self.cid, DATE))["text"], self.draft(mid)["text"]
        )
        with self.c.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT until_id FROM consolidation_state").fetchone()[0], last
            )

    async def test_old_backlog_cannot_become_todays_diary(self):
        mid = await self.add(ts=NOW - 86400)
        self.assertFalse(await self.consolidate(self.draft(mid)))
        request = json.loads(self.c.llm.calls[0][-1]["content"])
        self.assertFalse(request["events"][0]["diary_eligible"])
        self.assertIsNone(await self.daily.identities.diary(self.cid, DATE))
        self.assertTrue(await self.consolidate(now=NOW + 1))

    async def test_invalid_diary_or_database_failure_rolls_back_memories_and_progress(self):
        mid = await self.add()
        card = fact("alice", mid, "我喜欢红茶", "喜欢红茶")
        bad = self.draft(mid, quote="根本不存在的原文")
        self.assertFalse(await self.consolidate(bad, [card]))
        with self.c.db.connect() as conn:
            conn.execute(
                "CREATE TRIGGER reject_diary BEFORE INSERT ON sys_cache "
                "WHEN NEW.key LIKE 'daily_diary:%' BEGIN SELECT RAISE(ABORT,'disk'); END"
            )
        self.assertFalse(await self.consolidate(self.draft(mid), [card], now=NOW + 1))
        self.assertEqual(await self.c.memory_repo.recent_cards(self.cid, 10), [])
        self.assertIsNone(await self.daily.identities.diary(self.cid, DATE))
        with self.c.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT until_id FROM consolidation_state").fetchone()[0], 0
            )

    async def test_empty_diary_still_commits_memory_and_does_not_reuse_yesterday(self):
        mid = await self.add()
        self.assertTrue(await self.consolidate(self.draft(mid)))
        mid = await self.add("我喜欢绿茶", ts=NOW + 86400 - 30)
        self.assertTrue(
            await self.consolidate(
                cards=[fact("alice", mid, "我喜欢绿茶", "喜欢绿茶")], now=NOW + 86400
            )
        )
        self.assertEqual(
            (await self.c.memory_repo.recent_cards(self.cid, 10))[0]["what"], "喜欢绿茶"
        )
        self.assertIsNone(await self.daily.identities.diary(self.cid, "2026-10-27"))
        self.assertIsNone(await self.daily.identities.diary(self.cid, DATE))

    async def test_manual_publication_after_restart_uses_no_model_and_keeps_scope(self):
        mid = await self.add()
        self.assertTrue(await self.consolidate(self.draft(mid)))
        restarted = container(self.path)
        try:
            auto = restarted.services.autonomous
            other = await restarted.conversation_repo.get_or_create_conversation("other")
            with self.assertRaises(ValueError):
                await auto.scheduled_speak(other, "other", self.event, DATE)
            text, _ = await auto.scheduled_speak(
                self.cid, "diary-fixture", self.event, DATE, mark_date=False
            )
            self.assertEqual(text, self.draft(mid)["text"])
            self.assertEqual(restarted.llm.calls, [])
            state = await restarted.conversation_repo.load_conversation_state(self.cid)
            self.assertEqual(state["last_diary_date"], "")
            rows = await restarted.message_repo.recent_messages(self.cid, 10)
            self.assertEqual(rows[-1]["source"], "diary")
        finally:
            await restarted.runtime.close()

    async def test_auto_publication_needs_only_participation_call_and_marks_after_send(self):
        mid = await self.add()
        self.assertTrue(await self.consolidate(self.draft(mid)))
        self.c.llm.responses.append('{"should_speak":true,"reason":"适合回顾","topic":"茶"}')
        auto = self.c.services.autonomous
        with patch("dafeiyu.services.autonomous_service.time.time", return_value=NOW):
            self.assertTrue(await auto.tick_conversation(self.cid, "diary-fixture", NOW))
            self.assertFalse(await auto.tick_conversation(self.cid, "diary-fixture", NOW))
        self.assertEqual(len(self.c.llm.calls), 2)  # one consolidation, one participation decision
        state = await self.c.conversation_repo.load_conversation_state(self.cid)
        self.assertEqual(state["last_diary_date"], DATE)
        self.assertEqual(len([x for x in self.c.feishu.calls if x[0] == "card"]), 1)

    async def test_send_failure_keeps_saved_diary_and_does_not_mark_sent(self):
        mid = await self.add()
        self.assertTrue(await self.consolidate(self.draft(mid)))
        self.c.feishu.fail = "card"
        with self.assertRaises(RuntimeError):
            await self.c.services.autonomous.scheduled_speak(
                self.cid, "diary-fixture", self.event, DATE
            )
        self.assertIsNotNone(await self.daily.identities.diary(self.cid, DATE))
        self.assertEqual(
            (await self.c.conversation_repo.load_conversation_state(self.cid))["last_diary_date"],
            "",
        )
        self.assertEqual(len(self.c.llm.calls), 1)

    async def test_diary_is_never_new_identity_evidence(self):
        mid = await self.add()
        self.assertTrue(await self.consolidate(self.draft(mid)))
        await self.c.services.autonomous.scheduled_speak(
            self.cid, "diary-fixture", self.event, DATE, mark_date=False
        )
        next_mid = await self.add("今天继续聊天", ts=NOW + 86400 - 30)
        self.assertTrue(await self.consolidate(now=NOW + 86400))
        events = json.loads(self.c.llm.calls[-1][-1]["content"])["events"]
        self.assertEqual([r["id"] for r in events], [next_mid])

    async def test_tick_flushes_received_observer_messages_before_freezing_snapshot(self):
        first = await self.add()
        self.c.runtime.observer_buffer[self.cid] = [
            {
                "content": "还有刚收到的讨论",
                "sender_name": "小林",
                "open_id": "alice",
                "ts": NOW - 20,
            }
        ]
        self.c.llm.responses.extend(
            [
                '{"aliases":[],"cards":[],"diary":null}',
                '{"should_speak":false}',
            ]
        )
        with patch("dafeiyu.services.autonomous_service.time.time", return_value=NOW):
            self.assertFalse(
                await self.c.services.autonomous.tick_conversation(self.cid, "diary-fixture", NOW)
            )
        events = json.loads(self.c.llm.calls[0][-1]["content"])["events"]
        self.assertEqual([e["content"] for e in events], ["我喜欢红茶", "还有刚收到的讨论"])
        self.assertGreater(events[-1]["id"], first)

    async def test_failed_observer_flush_does_not_freeze_an_incomplete_daily_snapshot(self):
        await self.add()
        self.c.runtime.observer_buffer[self.cid] = [
            {
                "content": "还没写入的讨论",
                "sender_name": "小林",
                "open_id": "alice",
                "ts": NOW,
            }
        ]
        with (
            patch.object(
                self.c.message_repo,
                "append_observer_batch",
                AsyncMock(side_effect=RuntimeError("fixture write failure")),
            ),
            self.assertLogs("dafeiyu", level="ERROR"),
        ):
            self.assertEqual(await self.c.services.observer.flush_observer_buffer(self.cid), 0)
        self.assertFalse(await self.daily.maybe_consolidate(self.cid, NOW))
        self.assertEqual(self.c.llm.calls, [])
        with self.c.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT * FROM consolidation_state").fetchone())

    async def test_diary_participation_checks_saved_content_instead_of_ordinary_interjection(self):
        mid = await self.add()
        self.assertTrue(await self.consolidate(self.draft(mid)))
        self.c.llm.responses.append('{"should_speak":false}')
        with patch("dafeiyu.services.autonomous_service.time.time", return_value=NOW):
            self.assertFalse(
                await self.c.services.autonomous.tick_conversation(self.cid, "diary-fixture", NOW)
            )
        prompt = self.c.llm.calls[-1][-1]["content"]
        self.assertIn(self.draft(mid)["text"], prompt)
        self.assertIn("只判断这项内容是否适合此刻发布", prompt)
        self.assertFalse(self.c.feishu.calls)
