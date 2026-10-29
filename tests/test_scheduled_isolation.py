from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dafeiyu.domain.reply import GeneratedReply
from dafeiyu.integrations.feishu_client import DeliveryRejected
from tests.helpers import container, event

YES = '{"should_speak":true,"reason":"适合分享","topic":"这次的梦境"}'
NO = '{"should_speak":false}'
DREAM = '{"reply_mode":"normal","reply":"梦到了白饭山","image_prompt":"白饭山"}'


class ScheduledIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "scheduled.db")
        self.c = container(self.path)
        self.cid = await self.c.conversation_repo.get_or_create_conversation("chat-fixture")
        self.now = datetime(2026, 10, 29, 2, tzinfo=UTC).timestamp()  # 10:00 local
        self.clock = patch(
            "dafeiyu.services.scheduled_service.time.time", side_effect=lambda: self.now
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.auto = self.c.services.autonomous
        self.scheduled = self.c.services.scheduled
        self.dream = next(e for e in self.c.config.scheduled_events if e["kind"] == "dream")
        self.date = "2026-10-29"

    async def asyncTearDown(self):
        await self.c.runtime.close()
        self.tmp.cleanup()

    async def add(self, text="聊一件今天的小事", cid=None):
        return await self.c.message_repo.append_message(
            cid or self.cid, "user", text, source="observer", sender_open_id="a", ts=self.now
        )

    async def job(self):
        return await self.scheduled._load(self.cid, "dream", self.date)

    def enable_images(self, *, card=True):
        config = replace(self.c.config, image_model="fake-image", card_enabled=card)
        self.auto.config = self.scheduled.config = config

    async def test_event_path_prioritizes_dream_and_updates_shared_state(self):
        mid = await self.add()
        self.c.llm.responses.extend([YES, DREAM])
        self.assertTrue(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        state = await self.c.conversation_repo.load_conversation_state(self.cid)
        self.assertEqual(state["last_dream_date"], self.date)
        self.assertEqual((await self.job())["until_id"], mid)
        self.assertEqual(state["last_proactive_topic"], "这次的梦境")
        self.assertFalse(state["last_proactive_answered"])
        self.assertTrue(self.auto.cooling_down(state, self.now + 601))
        rows = await self.c.message_repo.recent_messages(self.cid, 10)
        self.assertEqual(rows[-1]["source"], "dream")
        self.assertEqual(
            (await self.c.conversation_repo.evaluation_status(self.cid))["until_id"], 0
        )

    async def test_ordinary_rejection_cannot_consume_dream_activity(self):
        self.now -= 60  # 09:59
        await self.add()
        self.c.llm.responses.append(NO)
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.now += 120
        self.c.llm.responses.extend([YES, DREAM])
        self.assertTrue(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual(len(self.c.llm.calls), 3)

    async def test_dream_rejection_does_not_consume_ordinary_activity(self):
        mid = await self.add()
        self.c.llm.responses.append(NO)
        self.assertEqual(
            await self.scheduled.maybe_publish(self.cid, "chat-fixture"), (False, False)
        )
        self.assertEqual((await self.job())["until_id"], mid)
        self.c.llm.responses.append(YES)
        state = await self.c.conversation_repo.load_conversation_state(self.cid)
        self.assertIsNotNone(await self.auto.evaluate_proactive_speaking(self.cid, state))

    async def test_unready_diary_ordinary_rejection_does_not_consume_finished_diary(self):
        self.now += 9 * 3600
        await self.add()
        self.c.llm.responses.append(NO)
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        await self.c.system_repo.set_sys_cache(
            f"daily_diary:{self.cid}",
            json.dumps(
                {"date": self.date, "ready": True, "text": "今天听到了小事。", "evidence": []}
            ),
        )
        self.now += 65
        self.c.llm.responses.append(YES)
        self.assertTrue(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual(len(self.c.llm.calls), 2)

    async def test_generation_failure_retains_activity_for_retry(self):
        await self.add()
        self.c.llm.responses.extend([YES, "bad-json"])
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual((await self.job())["until_id"], 0)
        self.now += 30
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual(len(self.c.llm.calls), 2)
        self.now += 35
        self.c.llm.responses.extend([YES, DREAM])
        self.assertTrue(await self.auto.maybe_interject(self.cid, "chat-fixture"))

    async def test_explicit_send_rejection_retries_saved_dream_after_restart(self):
        await self.add()
        self.c.llm.responses.extend([YES, DREAM])
        self.c.feishu.fail = "card"
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual((await self.job())["status"], "ready")
        self.assertEqual((await self.job())["until_id"], 0)
        restarted = container(self.path)
        try:
            self.now += 65
            restarted.llm.responses.append(YES)
            self.assertTrue(
                await restarted.services.autonomous.maybe_interject(self.cid, "chat-fixture")
            )
            self.assertEqual(len(restarted.llm.calls), 1)  # Participation only, no regeneration.
            self.assertEqual(restarted.feishu.calls[0][1], "chat-fixture")
        finally:
            await restarted.runtime.close()

    async def test_unknown_send_result_blocks_auto_and_manual_replay_after_restart(self):
        await self.add()
        self.c.llm.responses.extend([YES, DREAM])
        self.c.feishu.send_card = AsyncMock(side_effect=TimeoutError)
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual((await self.job())["status"], "uncertain")
        restarted = container(self.path)
        try:
            self.now += 65
            await self.add()
            self.assertFalse(
                await restarted.services.autonomous.maybe_interject(self.cid, "chat-fixture")
            )
            with self.assertRaisesRegex(ValueError, "发送状态不明"):
                await restarted.services.scheduled.speak(
                    self.cid, "chat-fixture", self.dream, self.date
                )
            self.assertEqual(restarted.llm.calls, [])
            self.assertEqual(restarted.feishu.calls, [])
        finally:
            await restarted.runtime.close()

    async def test_crash_send_guard_is_not_replayed(self):
        await self.add()
        await self.scheduled._save(self.cid, "dream", {**await self.job(), "status": "sending"})
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual(self.c.llm.calls, [])

    async def test_record_failure_after_physical_send_never_repeats_card(self):
        await self.add()
        self.c.llm.responses.extend([YES, DREAM])
        with patch.object(self.c.message_repo, "append_message", AsyncMock(side_effect=OSError)):
            self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual((await self.job())["status"], "uncertain")
        self.now += 65
        await self.add()
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual(sum(c[0] == "card" for c in self.c.feishu.calls), 1)

    async def test_pending_direct_message_during_decision_prevents_generation(self):
        await self.add()

        async def decision(*args, **kwargs):
            self.c.runtime.chat_pending_messages[self.cid] = [object()]
            return YES

        self.c.llm.chat_json = decision
        self.auto.prepare_reply = AsyncMock()
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.auto.prepare_reply.assert_not_awaited()
        self.assertEqual(self.c.feishu.calls, [])
        self.assertEqual((await self.job())["until_id"], 0)

    async def test_direct_reply_can_finish_during_dream_generation(self):
        await self.add()
        started, release = asyncio.Event(), asyncio.Event()
        original = self.auto.prepare_reply

        async def prepare(*args, **kwargs):
            started.set()
            await release.wait()
            return await original(*args, **kwargs)

        self.auto.prepare_reply = prepare
        self.c.services.memory.identities.resolve_subjects = AsyncMock(return_value=({"alice"}, ""))
        self.c.llm.responses.extend([YES, '{"reply_mode":"normal","reply":"先回答你"}', DREAM])
        task = asyncio.create_task(self.auto.maybe_interject(self.cid, "chat-fixture"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(self.c.runtime.chat_lock(self.cid).locked())
            await asyncio.wait_for(
                self.c.services.reply.handle_message_event(event("@_bot 先回答我")), 1
            )
            self.assertTrue(any(c[0] == "reply" for c in self.c.feishu.calls))
            release.set()
            self.assertFalse(await task)
            self.assertFalse(any(c[0] == "card" for c in self.c.feishu.calls))
            self.assertEqual((await self.job())["text"], "梦到了白饭山")
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_image_preparation_is_unlocked_and_retains_text_on_cancellation(self):
        await self.add()
        self.enable_images()
        started = asyncio.Event()

        async def hang(*args):
            started.set()
            await asyncio.Event().wait()

        self.c.llm.generate_image = hang
        self.c.llm.responses.extend([YES, DREAM])
        task = asyncio.create_task(self.auto.maybe_interject(self.cid, "chat-fixture"))
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(self.c.runtime.chat_lock(self.cid).locked())
        self.assertEqual((await self.job())["text"], "梦到了白饭山")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        restarted = container(self.path)
        try:
            self.now += 65
            restarted.llm.responses.append(YES)
            self.assertTrue(
                await restarted.services.autonomous.maybe_interject(self.cid, "chat-fixture")
            )
            self.assertEqual(len(restarted.llm.calls), 1)
        finally:
            await restarted.runtime.close()

    async def test_expired_window_after_decision_prevents_generation(self):
        self.now += 3590
        await self.add()

        async def decision(*args, **kwargs):
            self.now += 120
            return YES

        self.c.llm.chat_json = decision
        self.auto.prepare_reply = AsyncMock()
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.auto.prepare_reply.assert_not_awaited()
        self.assertEqual(self.c.feishu.calls, [])

    async def test_window_and_activity_rechecked_after_image(self):
        self.now += 3590
        await self.add()
        self.enable_images()

        async def image(*args):
            self.now += 120
            return b"fake-image"

        self.c.llm.generate_image = image
        self.c.llm.responses.extend([YES, DREAM])
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual([c[0] for c in self.c.feishu.calls], ["upload"])
        self.assertEqual((await self.job())["status"], "ready")

    async def test_activity_expiration_after_generation_prevents_send(self):
        await self.add()

        async def prepare(*args, **kwargs):
            self.now += 181
            return GeneratedReply("已准备的梦", "normal"), frozenset()

        self.auto.prepare_reply = prepare
        self.c.llm.responses.append(YES)
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual(self.c.feishu.calls, [])
        self.assertEqual((await self.job())["status"], "ready")

    async def test_disabling_cards_skips_image_model_and_upload(self):
        await self.add()
        self.enable_images(card=False)
        self.c.llm.generate_image = AsyncMock(return_value=b"unused")
        self.c.llm.responses.extend([YES, DREAM])
        self.assertTrue(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.c.llm.generate_image.assert_not_awaited()
        self.assertEqual([c[0] for c in self.c.feishu.calls], ["text"])

    async def test_image_failure_still_delivers_text(self):
        await self.add()
        self.enable_images()
        self.c.llm.generate_image = AsyncMock(side_effect=TimeoutError)
        self.c.llm.responses.extend([YES, DREAM])
        self.assertTrue(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        card = self.c.feishu.calls[-1][2]
        self.assertEqual(len(card["elements"]), 1)

    async def test_concurrent_trigger_does_not_duplicate_dream(self):
        await self.add()
        started, release = asyncio.Event(), asyncio.Event()

        async def prepare(*args, **kwargs):
            started.set()
            await release.wait()
            return GeneratedReply("一场梦", "normal"), frozenset()

        self.auto.prepare_reply = prepare
        self.c.llm.responses.append(YES)
        task = asyncio.create_task(self.auto.maybe_interject(self.cid, "chat-fixture"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
            release.set()
            self.assertTrue(await task)
            self.assertEqual(sum(c[0] == "card" for c in self.c.feishu.calls), 1)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_periodic_slow_conversation_does_not_block_others_or_duplicate_tasks(self):
        other = await self.c.conversation_repo.get_or_create_conversation("other")
        await self.add()
        await self.add(cid=other)
        started, release, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def tick(cid, *args, **kwargs):
            if cid == self.cid:
                started.set()
                await release.wait()
            else:
                other_done.set()

        self.auto.tick_conversation = tick
        self.auto.consolidation = None
        try:
            await asyncio.wait_for(self.auto.tick_all_conversations(), 1)
            await asyncio.wait_for(asyncio.gather(started.wait(), other_done.wait()), 1)
            first = self.auto.conversation_tasks[self.cid]
            await self.auto.tick_all_conversations()
            self.assertIs(self.auto.conversation_tasks[self.cid], first)
        finally:
            release.set()
            await asyncio.gather(*self.auto.conversation_tasks.values())

    async def test_successful_diary_batches_finish_and_publish_within_window(self):
        self.now += 9 * 3600
        daily = self.c.services.consolidation
        daily.config = replace(
            self.c.config, consolidation={**self.c.config.consolidation, "max_messages": 1}
        )
        for i in range(4):
            await self.add(f"今天第{i}件小事")
        first = self.now

        async def model(messages, **kwargs):
            prompt = messages[-1]["content"]
            if "diary_date" not in prompt:
                return YES
            row = json.loads(prompt)["events"][0]
            return json.dumps(
                {
                    "aliases": [],
                    "cards": [],
                    "diary": {
                        "text": "听到了今天的小事。",
                        "evidence": [{"message_id": row["id"], "quote": row["content"]}],
                    },
                }
            )

        self.c.llm.chat_json = model
        for i in range(4):
            self.now = first + i * 600
            if i == 3:
                await self.add("刚刚还在聊天")
            await self.auto._consolidate_and_publish(self.cid, "chat-fixture")
        self.assertLess(self.now, first + 3600)
        state = await self.c.conversation_repo.load_conversation_state(self.cid)
        self.assertEqual(state["last_diary_date"], self.date)
        self.assertEqual(sum(c[0] == "card" for c in self.c.feishu.calls), 1)

    async def test_evaluation_timeout_does_not_consume_activity(self):
        await self.add()
        self.scheduled.config = replace(
            self.c.config,
            settings={
                **self.c.config.settings,
                "autonomous": {
                    **self.c.config.settings["autonomous"],
                    "scheduled_evaluation_timeout_sec": 0.01,
                },
            },
        )
        cancelled = asyncio.Event()

        async def hang(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.auto.participation_decision = hang
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertTrue(cancelled.is_set())
        self.assertEqual((await self.job())["until_id"], 0)

    async def test_denied_dream_can_still_allow_ordinary_conversation(self):
        await self.add()
        self.c.llm.responses.extend([NO, YES, '{"reply_mode":"normal","reply":"接着聊这件事"}'])
        self.assertTrue(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        rows = await self.c.message_repo.recent_messages(self.cid, 10)
        self.assertEqual(rows[-1]["source"], "proactive")
        self.assertEqual((await self.job())["status"], "pending")

    async def test_pending_message_during_send_guard_write_prevents_network_send(self):
        await self.add()
        self.c.llm.responses.extend([YES, DREAM])
        save = self.scheduled._save

        async def inject(cid, kind, job):
            await save(cid, kind, job)
            if job["status"] == "sending":
                self.c.runtime.chat_pending_messages[cid] = [object()]

        self.scheduled._save = inject
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual(self.c.feishu.calls, [])
        self.assertEqual((await self.job())["status"], "ready")
        self.c.runtime.chat_pending_messages.clear()
        self.scheduled._save = save
        self.now += 65
        self.c.llm.responses.append(YES)
        self.assertTrue(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual(len(self.c.llm.calls), 3)

    async def test_durable_guard_write_failure_never_starts_remote_send(self):
        await self.add()
        self.c.llm.responses.extend([YES, DREAM])
        save = self.scheduled._save

        async def fail_guard(cid, kind, job):
            if job["status"] == "sending":
                raise OSError("fixture write failure")
            await save(cid, kind, job)

        self.scheduled._save = fail_guard
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual(self.c.feishu.calls, [])
        self.assertEqual((await self.job())["status"], "ready")

    async def test_cancellation_during_send_retains_unknown_guard_and_releases_lock(self):
        await self.add()
        self.c.llm.responses.extend([YES, DREAM])
        started = asyncio.Event()

        async def send(*args):
            started.set()
            await asyncio.Event().wait()

        self.c.feishu.send_card = send
        task = asyncio.create_task(self.auto.maybe_interject(self.cid, "chat-fixture"))
        try:
            await asyncio.wait_for(started.wait(), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.c.runtime.chat_lock(self.cid).locked())
        self.assertIn((await self.job())["status"], {"sending", "uncertain"})
        self.now += 65
        self.c.feishu.send_card = AsyncMock()
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.c.feishu.send_card.assert_not_awaited()

    async def test_cross_day_during_preparation_does_not_send_yesterdays_content(self):
        await self.add()
        self.c.llm.responses.append(YES)

        async def prepare(*args, **kwargs):
            self.now += 86400
            return GeneratedReply("昨日的梦", "normal"), frozenset()

        self.auto.prepare_reply = prepare
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual(self.c.feishu.calls, [])
        # A new day starts a new task; it does not reuse yesterday's dream.
        new_job = await self.scheduled._load(self.cid, "dream", "2026-10-30")
        self.assertNotIn("text", new_job)
        self.assertEqual(new_job["until_id"], 0)

    async def test_generation_timeout_preserves_retry_and_releases_preparation_slot(self):
        await self.add()
        self.scheduled.config = replace(
            self.c.config,
            settings={
                **self.c.config.settings,
                "autonomous": {
                    **self.c.config.settings["autonomous"],
                    "scheduled_generation_timeout_sec": 0.01,
                },
            },
        )
        self.c.llm.responses.append(YES)
        original = self.auto.prepare_reply

        async def hang(*args, **kwargs):
            await asyncio.Event().wait()

        self.auto.prepare_reply = hang
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertFalse(self.scheduled.dream_limit.locked())
        self.assertEqual((await self.job())["until_id"], 0)
        self.auto.prepare_reply = original
        self.now += 65
        self.c.llm.responses.extend([YES, DREAM])
        self.assertTrue(await self.auto.maybe_interject(self.cid, "chat-fixture"))

    async def test_dream_waiting_for_preparation_slot_rechecks_window(self):
        await self.add()
        self.c.llm.responses.append(YES)
        self.auto.prepare_reply = AsyncMock()
        started = asyncio.Event()
        prepare = self.scheduled._prepare

        async def queued(*args, **kwargs):
            started.set()
            return await prepare(*args, **kwargs)

        self.scheduled._prepare = queued
        await self.scheduled.dream_limit.acquire()
        task = asyncio.create_task(self.auto.maybe_interject(self.cid, "chat-fixture"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.now += 3601
        finally:
            self.scheduled.dream_limit.release()
        self.assertFalse(await task)
        self.auto.prepare_reply.assert_not_awaited()
        self.assertEqual(self.c.feishu.calls, [])

    async def test_invalid_positive_decision_does_not_advance_either_watermark(self):
        await self.add()
        self.c.llm.responses.append('{"should_speak":true,"reason":"","topic":""}')
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual((await self.job())["until_id"], 0)
        self.assertEqual(
            (await self.c.conversation_repo.evaluation_status(self.cid))["until_id"], 0
        )

    async def test_manual_diary_preserves_automatic_task_mark_semantics(self):
        self.now += 9 * 3600
        await self.add()
        diary = next(e for e in self.c.config.scheduled_events if e["kind"] == "diary")
        await self.c.system_repo.set_sys_cache(
            f"daily_diary:{self.cid}",
            json.dumps({"date": self.date, "ready": True, "text": "今天的小事", "evidence": []}),
        )
        await self.scheduled.speak(self.cid, "chat-fixture", diary, self.date, mark_date=False)
        job = await self.scheduled._load(self.cid, "diary", self.date)
        self.assertEqual(job["status"], "ready")
        state = await self.c.conversation_repo.load_conversation_state(self.cid)
        self.assertEqual(state["last_diary_date"], "")
        self.assertFalse(state["last_proactive_answered"])
        # Shared cooldown prevents a second automatic send after a manual one.
        self.assertFalse(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        self.assertEqual(sum(c[0] == "card" for c in self.c.feishu.calls), 1)

    async def test_job_progress_is_conversation_scoped(self):
        await self.add()
        self.c.llm.responses.extend([YES, DREAM])
        self.assertTrue(await self.auto.maybe_interject(self.cid, "chat-fixture"))
        other = await self.c.conversation_repo.get_or_create_conversation("other")
        await self.add(cid=other)
        self.c.llm.responses.extend([YES, DREAM])
        self.assertTrue(await self.auto.maybe_interject(other, "other"))
        self.assertEqual([c[1] for c in self.c.feishu.calls], ["chat-fixture", "other"])
        metadata = await self.scheduled.status(self.cid)
        self.assertEqual(metadata["dream"]["status"], "sent")
        self.assertNotIn("text", metadata["dream"])
        self.assertNotIn("image_prompt", metadata["dream"])


class FeishuRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_business_rejection_is_distinct_from_ambiguous_response(self):
        from dafeiyu.integrations.feishu_client import FeishuClient
        from tests.helpers import make_config

        client = FeishuClient(make_config(), None, None)
        try:
            for name, payload in (("send_text", "text"), ("send_card", {})):
                with self.subTest(name=name):
                    client._authed_request = AsyncMock(return_value={"code": 230002})
                    with self.assertRaises(DeliveryRejected):
                        await getattr(client, name)("fake", payload)
                    client._authed_request = AsyncMock(return_value=None)
                    with self.assertRaises(RuntimeError) as failure:
                        await getattr(client, name)("fake", payload)
                    self.assertNotIsInstance(failure.exception, DeliveryRejected)
        finally:
            await client.close()
