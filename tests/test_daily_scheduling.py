from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.helpers import container


class DailySchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = container(str(Path(self.tmp.name) / "schedule.db"))
        self.cid = await self.c.conversation_repo.get_or_create_conversation("schedule")
        self.auto = self.c.services.autonomous
        self.daily = self.c.services.consolidation
        self.now = datetime(2026, 10, 26, 11, tzinfo=UTC).timestamp()  # 19:00 local
        self.daily.config = replace(
            self.c.config, consolidation={**self.c.config.consolidation, "enabled": False}
        )

    async def asyncTearDown(self):
        await self.c.runtime.close()
        self.tmp.cleanup()

    async def seed_diary(self):
        await self.c.system_repo.set_sys_cache(
            f"daily_diary:{self.cid}",
            json.dumps(
                {
                    "date": "2026-10-26",
                    "ready": True,
                    "text": "今天一起解决了小麻烦。",
                    "evidence": [],
                }
            ),
        )

    async def add(self, text="今天还想一起聊", ts=None):
        return await self.c.message_repo.append_message(
            self.cid,
            "user",
            text,
            sender_open_id="a",
            source="observer",
            ts=self.now if ts is None else ts,
        )

    async def tick(self, now=None):
        now = self.now if now is None else now
        with patch("dafeiyu.services.autonomous_service.time.time", return_value=now):
            return await self.auto.tick_conversation(self.cid, "schedule", now)

    async def test_no_events_means_no_model_and_no_outward_send(self):
        self.daily.config = self.c.config
        self.assertFalse(await self.tick())
        self.assertFalse(await self.tick(self.now + 600))
        self.assertEqual(self.c.llm.calls, [])
        self.assertEqual(self.c.feishu.calls, [])

    async def test_quiet_group_can_consolidate_old_new_events_without_publishing(self):
        self.daily.config = self.c.config
        await self.add(ts=self.now - 7200)
        self.c.llm.responses.append('{"aliases":[],"cards":[],"diary":null}')
        self.assertFalse(await self.tick())
        self.assertEqual(len(self.c.llm.calls), 1)
        self.assertEqual(self.c.feishu.calls, [])
        with self.c.db.connect() as conn:
            self.assertGreater(
                conn.execute("SELECT until_id FROM consolidation_state").fetchone()[0], 0
            )

    async def test_configured_diary_window_preserved_but_requires_semantic_permission(self):
        await self.seed_diary()
        await self.add()
        self.c.llm.responses.extend(
            [
                '{"should_speak":true,"reason":"大家在回顾今天，可自然分享","topic":"今日回顾"}',
            ]
        )
        self.assertEqual(
            {e["kind"]: e["hour"] for e in self.c.config.scheduled_events},
            {"dream": 10, "diary": 19},
        )
        self.assertTrue(await self.tick())
        state = await self.c.conversation_repo.load_conversation_state(self.cid)
        self.assertEqual(state["last_diary_date"], "2026-10-26")
        self.assertEqual(len([c for c in self.c.feishu.calls if c[0] == "card"]), 1)
        self.assertFalse(await self.tick(self.now + 600))

    async def test_idle_skip_does_not_pretend_sent_and_fresh_activity_can_qualify_later(self):
        await self.seed_diary()
        self.assertFalse(await self.tick())
        state = await self.c.conversation_repo.load_conversation_state(self.cid)
        self.assertEqual(state["last_diary_date"], "")
        await self.add(ts=self.now + 900)
        self.c.llm.responses.extend(
            [
                '{"should_speak":true,"reason":"新讨论适合回顾","topic":"回顾"}',
            ]
        )
        self.assertTrue(await self.tick(self.now + 900))

    async def test_negative_decision_is_persisted_across_restart_and_not_recharged(self):
        await self.add()
        self.c.llm.responses.append('{"should_speak":false}')
        self.assertFalse(await self.tick())
        self.assertFalse(await self.tick(self.now + 600))
        self.assertEqual(len(self.c.llm.calls), 1)
        restarted = container(str(self.c.db.path))
        try:
            restarted.services.consolidation.config = self.daily.config
            with patch(
                "dafeiyu.services.autonomous_service.time.time", return_value=self.now + 700
            ):
                self.assertFalse(
                    await restarted.services.autonomous.tick_conversation(
                        self.cid, "schedule", self.now + 700
                    )
                )
            self.assertEqual(restarted.llm.calls, [])
        finally:
            await restarted.runtime.close()

    async def test_one_fresh_human_message_can_qualify_without_literal_topic_gate(self):
        now = self.now - 3600  # ordinary active hours
        await self.add(ts=now)
        state = await self.c.conversation_repo.load_conversation_state(self.cid)
        state.update(last_proactive_topic="同词不同语义", last_proactive_answered=False)
        self.c.llm.responses.append(
            json.dumps({"should_speak": True, "reason": "新内容", "topic": "同词不同语义"})
        )
        with patch("dafeiyu.services.autonomous_service.time.time", return_value=now):
            self.assertEqual(
                await self.auto.evaluate_proactive_speaking(self.cid, state),
                ("新内容", "同词不同语义"),
            )
        self.assertIn("语义重复", self.c.llm.calls[-1][-1]["content"])

    async def test_ordinary_interjection_allowed_at_night_and_early_morning(self):
        for offset in (2, 7, 14):  # 21:00, next day 02:00 and 09:00 local
            now = self.now + offset * 3600
            with self.subTest(offset=offset):
                await self.add(ts=now)
                self.c.llm.responses.extend(
                    [
                        '{"should_speak":true,"reason":"有新内容可接话","topic":"新话题"}',
                        '{"reply_mode":"normal","reply":"我也来聊一句。"}',
                    ]
                )
                with patch("dafeiyu.services.autonomous_service.time.time", return_value=now):
                    if offset == 7:
                        self.assertTrue(await self.auto.maybe_interject(self.cid, "schedule"))
                    else:
                        self.assertTrue(await self.tick(now))
                # Cooldown has elapsed, but no new human message means no further calls.
                calls = len(self.c.llm.calls)
                self.assertFalse(await self.tick(now + 1801))
                self.assertEqual(len(self.c.llm.calls), calls)

    async def test_cooldowns_still_prevent_ordinary_outward_calls_at_night(self):
        now = self.now + 7200  # 21:00 local
        for recent_state in (
            {"last_reply_ts": now - 30},
            {"last_proactive_ts": now - 300, "last_proactive_answered": True},
            {"last_proactive_ts": now - 900, "last_proactive_answered": False},
        ):
            with self.subTest(state=recent_state):
                await self.c.conversation_repo.mutate_state(
                    self.cid,
                    lambda s, values=recent_state: {
                        **s,
                        "last_reply_ts": 0,
                        "last_proactive_ts": 0,
                        **values,
                    },
                )
                await self.add(ts=now)
                self.assertFalse(await self.tick(now))
                self.assertEqual(self.c.llm.calls, [])
                self.assertEqual(self.c.feishu.calls, [])

    async def test_failed_evaluation_retries_with_backoff_not_checkpoint_advance(self):
        await self.add()
        self.c.llm.chat_json = AsyncMock(side_effect=TimeoutError)
        self.assertFalse(await self.tick())
        self.assertFalse(await self.tick(self.now + 30))
        self.assertEqual(self.c.llm.chat_json.await_count, 1)
        status = await self.c.conversation_repo.evaluation_status(self.cid)
        self.assertEqual(status["until_id"], 0)
        self.assertFalse(await self.tick(self.now + 65))
        self.assertEqual(self.c.llm.chat_json.await_count, 2)

    async def test_message_within_maintenance_window_but_stale_is_not_activity(self):
        await self.add(ts=self.now - 600)
        self.assertFalse(await self.tick())
        self.assertEqual(self.c.llm.calls, [])
        self.assertEqual(self.c.feishu.calls, [])

    async def test_dream_window_participation_checks_intended_dream_share(self):
        now = self.now - 9 * 3600  # 10:00 local
        await self.add(ts=now)
        self.c.llm.responses.append('{"should_speak":false}')
        self.assertFalse(await self.tick(now))
        dream = next(e for e in self.c.config.scheduled_events if e["kind"] == "dream")
        prompt = self.c.llm.calls[-1][-1]["content"]
        self.assertIn(dream["instruction"], prompt)
        self.assertIn("只判断这项内容是否适合此刻发布", prompt)
        self.assertFalse(self.c.feishu.calls)
