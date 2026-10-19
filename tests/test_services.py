from __future__ import annotations

import json
import typing
import unittest

from services.card_service import CardService
from services.reply_service import ReplyService


class ReplyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_p2p_is_direct_without_bot_lookup(self):
        class Feishu:
            async def get_bot_open_id(self):
                raise AssertionError("p2p must not need bot identity")

        service = ReplyService.__new__(ReplyService)
        service.feishu = typing.cast(typing.Any, Feishu())
        self.assertTrue(await service.is_direct_to_bot("p2p", []))

    async def test_group_fails_closed_when_bot_lookup_fails(self):
        class Feishu:
            async def get_bot_open_id(self):
                raise RuntimeError("temporary API error")

        service = ReplyService.__new__(ReplyService)
        service.feishu = typing.cast(typing.Any, Feishu())
        self.assertFalse(await service.is_direct_to_bot("group", []))


class ReplyParsingTests(unittest.TestCase):
    def test_parses_normal_and_double_encoded_json(self):
        normal = (
            '{"reply_mode":"reaction","reply":"难绷",'
            '"speaker_name":"小明"}'
        )
        self.assertEqual(
            ReplyService.parse_llm_content(normal),
            ("难绷", "小明", "reaction", True),
        )
        double_encoded = json.dumps(normal, ensure_ascii=False)
        self.assertEqual(
            ReplyService.parse_llm_content(double_encoded),
            ("难绷", "小明", "reaction", True),
        )

    def test_legacy_or_invalid_mode_is_kept_compatible_as_unknown(self):
        legacy = '{"reply":"难绷","speaker_name":"小明"}'
        self.assertEqual(
            ReplyService.parse_llm_content(legacy),
            ("难绷", "小明", "unknown", True),
        )
        invalid = '{"reply_mode":"long","reply":"展开说说"}'
        self.assertEqual(
            ReplyService.parse_llm_content(invalid),
            ("展开说说", "", "unknown", True),
        )

    def test_recovers_reply_when_later_json_field_is_malformed(self):
        malformed = (
            '{"reply_mode": "substantive", '
            '"reply": "发就发呗，反正你们开心就行。", '
            '"speaker_name": " : ""}'
        )
        reply, speaker_name, reply_mode, structured = (
            ReplyService.parse_llm_content(malformed)
        )
        self.assertEqual(reply, "发就发呗，反正你们开心就行。")
        self.assertEqual(speaker_name, "")
        self.assertEqual(reply_mode, "substantive")
        self.assertFalse(structured)


class CardPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_v2_payload_is_a_safe_noop(self):
        service = CardService.__new__(CardService)
        result = await service.handle_card_action(
            {"action": {"value": {"pet_id": 1, "action": "feed"}}}, None
        )
        self.assertEqual(result["toast"]["type"], "info")
        self.assertIn("新", result["toast"]["content"])


class ImageRoutingTests(unittest.IsolatedAsyncioTestCase):
    def _ret(self, value):
        async def _inner(*a, **k):
            return value
        return _inner()

    def _service(self, **stubs):
        import typing
        from runtime import RuntimeState
        from types import SimpleNamespace
        service = ReplyService.__new__(ReplyService)
        service.runtime = RuntimeState()
        service.config = typing.cast(typing.Any, SimpleNamespace(
            observer_flush_max_count=50,
            reply_min_interval_sec=0,
            compress_threshold=200,
            card_enabled=False,
            gameplay_enabled=False,
            fallback_replies={"non_text": "NT", "empty_text": "ET",
                              "quiet_hours": "Q", "empty_llm": "E",
                              "llm_error_template": "ERR {error_class}"},
        ))
        service.feishu = typing.cast(typing.Any, stubs["feishu"])
        service.llm = typing.cast(typing.Any, stubs.get("llm"))
        service.pet_repo = typing.cast(typing.Any, stubs.get("pet_repo"))
        service.message_repo = typing.cast(typing.Any, stubs.get("message_repo"))
        service.observer_service = typing.cast(typing.Any, stubs.get("observer"))
        service.state_domain = stubs.get("state")  # type: ignore[assignment]
        service.pet_domain = stubs.get("pet_domain")  # type: ignore[assignment]
        service.card_service = stubs.get("card")  # type: ignore[assignment]
        service.resolve_user_name = lambda open_id: self._ret("阿明")
        return service

    def _observer(self, service):
        flushed = []

        class Observer:
            async def flush_observer_buffer(self, pet_id):
                flushed.append(pet_id)
        service.observer_service = Observer()
        return flushed

    async def test_group_image_without_mention_goes_to_observer_buffer(self):
        from types import SimpleNamespace
        service = self._service(
            feishu=SimpleNamespace(
                download_image=lambda mid, key: self._ret(b"\xff\xd8fake"),
                get_bot_open_id=lambda: self._ret("bot"),
            ),
            llm=SimpleNamespace(describe_image=lambda b: self._ret("一只猫")),
            pet_repo=SimpleNamespace(find_pet=lambda chat: self._ret(7)),
        )
        self._observer(service)
        await service.handle_message_event({
            "message": {"message_id": "m1", "chat_id": "c1", "chat_type": "group",
                        "message_type": "image", "mentions": [],
                        "content": '{"image_key": "k1"}'},
            "sender": {"sender_id": {"open_id": "u1"}},
        })
        buf = service.runtime.observer_buffer[7]
        self.assertEqual(len(buf), 1)
        self.assertIn("一只猫", buf[0]["content"])

    async def test_group_image_caption_failure_still_recorded_as_placeholder(self):
        from types import SimpleNamespace
        service = self._service(
            feishu=SimpleNamespace(
                download_image=lambda mid, key: self._ret(None),
            ),
            llm=SimpleNamespace(describe_image=lambda b: self._ret(None)),
            pet_repo=SimpleNamespace(find_pet=lambda chat: self._ret(7)),
        )
        service.is_direct_to_bot = lambda chat_type, mentions: self._ret(False)
        self._observer(service)
        await service.handle_message_event({
            "message": {"message_id": "m1", "chat_id": "c1", "chat_type": "group",
                        "message_type": "image",
                        "mentions": [{"id": {"open_id": "bot"}}],
                        "content": '{"image_key": "k1"}'},
            "sender": {"sender_id": {"open_id": "u1"}},
        })
        buf = service.runtime.observer_buffer[7]
        self.assertIn("没看清", buf[0]["content"])

    async def test_post_text_is_extracted(self):
        from types import SimpleNamespace
        service = self._service(
            feishu=SimpleNamespace(),
            llm=SimpleNamespace(),
            pet_repo=SimpleNamespace(find_pet=lambda chat: self._ret(7)),
        )
        from domain.pet import PetDomain
        service.pet_domain = PetDomain.__new__(PetDomain)
        service.is_direct_to_bot = lambda chat_type, mentions: self._ret(False)
        self._observer(service)
        post = (
            '{"title": "", "content": ['
            '[{"tag": "text", "text": "hello "}, {"tag": "at", "user_name": "bob"}, '
            '{"tag": "text", "text": " world"}], '
            '[{"tag": "a", "text": "link", "href": "http://x"}]]}'
        )
        await service.handle_message_event({
            "message": {"message_id": "m1", "chat_id": "c1", "chat_type": "group",
                        "message_type": "post", "mentions": [],
                        "content": post},
            "sender": {"sender_id": {"open_id": "u1"}},
        })
        buf = service.runtime.observer_buffer[7]
        self.assertIn("hello", buf[0]["content"])
        self.assertIn("@bob", buf[0]["content"])
        self.assertIn("link", buf[0]["content"])

    async def test_post_with_image_uses_caption(self):
        from types import SimpleNamespace
        service = self._service(
            feishu=SimpleNamespace(
                download_image=lambda mid, key: self._ret(b"\xff\xd8fake"),
            ),
            llm=SimpleNamespace(describe_image=lambda b: self._ret("一只猫")),
            pet_repo=SimpleNamespace(find_pet=lambda chat: self._ret(7)),
        )
        from domain.pet import PetDomain
        service.pet_domain = PetDomain.__new__(PetDomain)
        service.is_direct_to_bot = lambda chat_type, mentions: self._ret(False)
        self._observer(service)
        post = (
            '{"title": "t", "content": ['
            '[{"tag": "text", "text": "看这个 "}, '
            '{"tag": "img", "image_key": "k1"}]]}'
        )
        await service.handle_message_event({
            "message": {"message_id": "m1", "chat_id": "c1", "chat_type": "group",
                        "message_type": "post", "mentions": [],
                        "content": post},
            "sender": {"sender_id": {"open_id": "u1"}},
        })
        buf = service.runtime.observer_buffer[7]
        self.assertIn("看这个", buf[0]["content"])
        self.assertIn("一只猫", buf[0]["content"])

    async def test_text_mention_without_key_gets_name_suffix(self):
        # 飞书只回传 mentions 不插 @_user_X 占位符时：@目标补到后缀，谁@的由 sender 区分
        from types import SimpleNamespace
        from domain.pet import PetDomain
        service = self._service(
            feishu=SimpleNamespace(),
            llm=SimpleNamespace(),
            pet_repo=SimpleNamespace(find_pet=lambda chat: self._ret(7)),
        )
        service.pet_domain = PetDomain.__new__(PetDomain)
        service.is_direct_to_bot = lambda chat_type, mentions: self._ret(False)
        service.resolve_user_name = lambda open_id: self._ret(
            {"ou_a": "两面派"}.get(open_id, "群友-x")
        )
        self._observer(service)
        await service.handle_message_event({
            "message": {"message_id": "m1", "chat_id": "c1", "chat_type": "group",
                        "message_type": "text",
                        "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_a"}}],
                        "content": '{"text": "我是罕见"}'},
            "sender": {"sender_id": {"open_id": "ou_b"}},
        })
        buf = service.runtime.observer_buffer[7]
        self.assertEqual(buf[0]["content"], "我是罕见 @两面派")

    async def test_text_mention_key_inline_replaces_with_name(self):
        from types import SimpleNamespace
        from domain.pet import PetDomain
        service = self._service(
            feishu=SimpleNamespace(),
            llm=SimpleNamespace(),
            pet_repo=SimpleNamespace(find_pet=lambda chat: self._ret(7)),
        )
        service.pet_domain = PetDomain.__new__(PetDomain)
        service.is_direct_to_bot = lambda chat_type, mentions: self._ret(False)
        self._observer(service)
        await service.handle_message_event({
            "message": {"message_id": "m1", "chat_id": "c1", "chat_type": "group",
                        "message_type": "text",
                        "mentions": [{"key": "@_user_1", "name": "二极管",
                                      "id": {"open_id": "ou_a"}}],
                        "content": '{"text": "@_user_1 你是谁"}'},
            "sender": {"sender_id": {"open_id": "ou_b"}},
        })
        buf = service.runtime.observer_buffer[7]
        self.assertEqual(buf[0]["content"], "@二极管 你是谁")

    async def test_p2p_image_download_failure_still_replies_with_placeholder(self):
        from types import SimpleNamespace
        sent = []

        async def boom(mid, key):
            raise RuntimeError("403")
        service = self._service(
            feishu=SimpleNamespace(download_image=boom),
            llm=SimpleNamespace(describe_image=lambda b: self._ret("x")),
        )

        async def fake_send(chat_type, chat_id, message_id, text):
            sent.append(text)
        service._send_response = fake_send
        service.is_direct_to_bot = lambda chat_type, mentions: self._ret(True)
        service.state_domain = SimpleNamespace(in_quiet_hours=lambda ts: False)  # type: ignore[assignment]
        service.pet_domain = SimpleNamespace(  # type: ignore[assignment]
            clean_text=lambda raw, mentions: raw,
        )
        service.pet_repo = typing.cast(typing.Any, SimpleNamespace(
            get_or_create_pet=lambda chat: self._ret(9),
            load_pet_state=lambda pet: self._ret({}),
        ))
        service.message_repo = SimpleNamespace(  # type: ignore[assignment]
            append_message=lambda *a, **k: self._ret(1),
            count_unsummarized=lambda pet: self._ret(0),
        )

        async def fake_llm(pet_id: int, user_text: str, **k):
            self.assertIn("没看清", user_text)
            return ("看到啦", {}, "")
        service.call_llm_with_memory = fake_llm  # type: ignore[method-assign]
        service.mark_replied = lambda pet_id: self._ret(None)
        self._observer(service)
        await service.handle_message_event({
            "message": {"message_id": "m1", "chat_id": "c1", "chat_type": "p2p",
                        "message_type": "image", "mentions": [],
                        "content": '{"image_key": "k1"}'},
            "sender": {"sender_id": {"open_id": "u1"}},
        })
        self.assertEqual(sent, ["看到啦"])


class ProactiveEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def _ret(self, value):
        async def _inner(*a, **k):
            return value
        return _inner()

    async def test_skips_when_not_enough_human_messages(self):
        from services.autonomous_service import AutonomousService
        from types import SimpleNamespace
        service = AutonomousService.__new__(AutonomousService)
        service.config = SimpleNamespace(proactive_min_new_msgs=3)
        service.message_repo = SimpleNamespace(
            recent_messages=lambda pet, limit: self._ret([
                {"role": "assistant", "content": "bot reply"},
                {"role": "user", "content": "hello 1"},
            ])
        )
        res = await service.evaluate_proactive_speaking(1, {})
        self.assertIsNone(res)

    async def test_returns_reason_and_topic_when_worth_speaking(self):
        from services.autonomous_service import AutonomousService
        from types import SimpleNamespace
        service = AutonomousService.__new__(AutonomousService)
        service.config = SimpleNamespace(
            proactive_min_new_msgs=2,
            proactive_eval_prompt="eval_prompt",
        )
        service.message_repo = SimpleNamespace(
            recent_messages=lambda pet, limit: self._ret([
                {"role": "user", "content": "大家觉得这个架构怎么样", "ts": 100},
                {"role": "user", "content": "感觉有点过度设计", "ts": 101},
            ])
        )
        service.llm = SimpleNamespace(
            chat_json=lambda msgs, **k: self._ret(
                json.dumps({"should_speak": True, "reason": "架构讨论值得吐槽", "topic": "架构设计"})
            )
        )
        res = await service.evaluate_proactive_speaking(1, {})
        self.assertEqual(res, ("架构讨论值得吐槽", "架构设计"))

    async def test_suppresses_same_unanswered_topic(self):
        from services.autonomous_service import AutonomousService
        from types import SimpleNamespace
        service = AutonomousService.__new__(AutonomousService)
        service.config = SimpleNamespace(
            proactive_min_new_msgs=2,
            proactive_eval_prompt="eval_prompt",
        )
        service.message_repo = SimpleNamespace(
            recent_messages=lambda pet, limit: self._ret([
                {"role": "user", "content": "msg 1", "ts": 100},
                {"role": "user", "content": "msg 2", "ts": 101},
            ])
        )
        service.llm = SimpleNamespace(
            chat_json=lambda msgs, **k: self._ret(
                json.dumps({"should_speak": True, "reason": "继续问", "topic": "旧话题"})
            )
        )
        state = {
            "last_proactive_ts": 50,
            "last_proactive_answered": False,
            "last_proactive_topic": "旧话题",
        }
        res = await service.evaluate_proactive_speaking(1, state)
        self.assertIsNone(res)

    async def test_includes_assistant_replies_in_context(self):
        from services.autonomous_service import AutonomousService
        from types import SimpleNamespace
        service = AutonomousService.__new__(AutonomousService)
        service.config = SimpleNamespace(
            proactive_min_new_msgs=2,
            proactive_eval_prompt="eval_prompt",
        )
        service.message_repo = SimpleNamespace(
            recent_messages=lambda pet, limit: self._ret([
                {"role": "user", "content": "问题 1", "ts": 100, "sender_name": "张三"},
                {"role": "assistant", "content": "回答 1", "ts": 101},
                {"role": "user", "content": "新消息 1", "ts": 102, "sender_name": "李四"},
                {"role": "user", "content": "新消息 2", "ts": 103, "sender_name": "王五"},
            ])
        )
        captured_dialogue = []
        async def _mock_chat(msgs, **k):
            captured_dialogue.append(msgs[1]["content"])
            return json.dumps({"should_speak": True, "reason": "接梗", "topic": "新话题"})
        service.llm = SimpleNamespace(chat_json=_mock_chat)
        # 上次直接回复发生在 ts=101
        state = {"last_reply_ts": 101, "last_proactive_ts": 0}
        res = await service.evaluate_proactive_speaking(1, state)
        self.assertEqual(res, ("接梗", "新话题"))
        self.assertTrue(len(captured_dialogue) > 0)
        self.assertIn("[小苍蝇]: 回答 1", captured_dialogue[0])
        self.assertIn("[张三]: 问题 1", captured_dialogue[0])
        self.assertIn("[李四]: 新消息 1", captured_dialogue[0])

    async def test_skips_when_no_new_msgs_after_reply(self):
        from services.autonomous_service import AutonomousService
        from types import SimpleNamespace
        service = AutonomousService.__new__(AutonomousService)
        service.config = SimpleNamespace(
            proactive_min_new_msgs=2,
            proactive_eval_prompt="eval_prompt",
        )
        service.message_repo = SimpleNamespace(
            recent_messages=lambda pet, limit: self._ret([
                {"role": "user", "content": "提问", "ts": 100},
                {"role": "assistant", "content": "回答", "ts": 101},
            ])
        )
        # 上次直接回复发生在 ts=101，之后没有任何新人类消息
        state = {"last_reply_ts": 101, "last_proactive_ts": 0}
        res = await service.evaluate_proactive_speaking(1, state)
        self.assertIsNone(res)


class EventInterjectTests(unittest.IsolatedAsyncioTestCase):
    def _ret(self, value):
        async def _inner(*a, **k):
            return value
        return _inner()

    async def test_event_interject_triggers_proactive_speak(self):
        from runtime import RuntimeState
        from types import SimpleNamespace
        service = ReplyService.__new__(ReplyService)
        service.runtime = RuntimeState()
        service.config = SimpleNamespace(
            event_interject_debounce_sec=0.0,
            proactive_cooldown_sec=600,
            unanswered_cooldown_sec=1800,
        )
        service.state_domain = SimpleNamespace(in_quiet_hours=lambda ts: False)
        service.pet_repo = SimpleNamespace(
            load_pet_state=lambda pet: self._ret({
                "last_proactive_ts": 0.0,
                "last_reply_ts": 0.0,
                "last_proactive_answered": True,
            })
        )
        service.observer_service = SimpleNamespace(
            flush_observer_buffer=lambda pet: self._ret(0)
        )
        spoken = []
        async def _speak(pet, chat, reason, topic=""):
            spoken.append((reason, topic))
            return ("reply", {})
        service.autonomous_service = SimpleNamespace(
            evaluate_proactive_speaking=lambda pet, state: self._ret(("群友暴论", "暴论话题")),
            proactive_speak=_speak,
        )
        await service.maybe_event_interject(1, "c1")
        self.assertEqual(spoken, [("群友暴论", "暴论话题")])

    async def test_event_interject_suppressed_when_under_unanswered_cooldown(self):
        import time
        from runtime import RuntimeState
        from types import SimpleNamespace
        service = ReplyService.__new__(ReplyService)
        service.runtime = RuntimeState()
        now = time.time()
        service.config = SimpleNamespace(
            event_interject_debounce_sec=0.0,
            proactive_cooldown_sec=600,
            unanswered_cooldown_sec=1800,
        )
        service.state_domain = SimpleNamespace(in_quiet_hours=lambda ts: False)
        # 上次主动发言才过去 1000 秒，但因为无人回应需要 1800 秒
        service.pet_repo = SimpleNamespace(
            load_pet_state=lambda pet: self._ret({
                "last_proactive_ts": now - 1000,
                "last_reply_ts": 0.0,
                "last_proactive_answered": False,
            })
        )
        spoken = []
        service.autonomous_service = SimpleNamespace(
            evaluate_proactive_speaking=lambda pet, state: self._ret(("理由", "话题")),
            proactive_speak=lambda pet, chat, reason, topic="": spoken.append((reason, topic)),
        )
        await service.maybe_event_interject(1, "c1")
        self.assertEqual(spoken, [])

    async def test_event_interject_suppressed_when_recently_replied(self):
        import time
        from runtime import RuntimeState
        from types import SimpleNamespace
        service = ReplyService.__new__(ReplyService)
        service.runtime = RuntimeState()
        now = time.time()
        service.config = SimpleNamespace(
            event_interject_debounce_sec=0.0,
            proactive_cooldown_sec=600,
            unanswered_cooldown_sec=1800,
            proactive_reply_cooldown_sec=60.0,
        )
        service.state_domain = SimpleNamespace(in_quiet_hours=lambda ts: False)
        # 刚直接回复过（20 秒前），处于 60 秒冷静期内
        service.pet_repo = SimpleNamespace(
            load_pet_state=lambda pet: self._ret({
                "last_proactive_ts": 0.0,
                "last_reply_ts": now - 20,
                "last_proactive_answered": True,
            })
        )
        spoken = []
        service.autonomous_service = SimpleNamespace(
            evaluate_proactive_speaking=lambda pet, state: self._ret(("理由", "话题")),
            proactive_speak=lambda pet, chat, reason, topic="": spoken.append((reason, topic)),
        )
        await service.maybe_event_interject(1, "c1")
        self.assertEqual(spoken, [])

    async def test_event_interject_allowed_after_reply_cooldown_expires(self):
        import time
        from runtime import RuntimeState
        from types import SimpleNamespace
        service = ReplyService.__new__(ReplyService)
        service.runtime = RuntimeState()
        now = time.time()
        service.config = SimpleNamespace(
            event_interject_debounce_sec=0.0,
            proactive_cooldown_sec=600,
            unanswered_cooldown_sec=1800,
            proactive_reply_cooldown_sec=60.0,
        )
        service.state_domain = SimpleNamespace(in_quiet_hours=lambda ts: False)
        service.pet_repo = SimpleNamespace(
            load_pet_state=lambda pet: self._ret({
                "last_proactive_ts": 0.0,
                "last_reply_ts": now - 70,  # 已经过了 60 秒冷静期
                "last_proactive_answered": True,
            })
        )
        service.observer_service = SimpleNamespace(
            flush_observer_buffer=lambda pet: self._ret(0)
        )
        spoken = []
        async def _mock_speak(pet, chat, reason, topic=""):
            spoken.append((reason, topic))
        service.autonomous_service = SimpleNamespace(
            evaluate_proactive_speaking=lambda pet, state: self._ret(("群友暴论", "话题")),
            proactive_speak=_mock_speak,
        )
        await service.maybe_event_interject(1, "c1")
        self.assertEqual(spoken, [("群友暴论", "话题")])


class AutonomousTickTests(unittest.IsolatedAsyncioTestCase):
    def _ret(self, value):
        async def _inner(*a, **k):
            return value
        return _inner()

    async def test_tick_suppressed_when_recently_replied(self):
        import time
        from services.autonomous_service import AutonomousService
        from types import SimpleNamespace
        service = AutonomousService.__new__(AutonomousService)
        now = time.time()
        service.config = SimpleNamespace(
            proactive_cooldown_sec=600,
            unanswered_cooldown_sec=1800,
            proactive_reply_cooldown_sec=60.0,
            scheduled_events=(),
        )
        service.pick_scheduled_event = lambda pet, cur, t: None
        service.state_domain = SimpleNamespace(in_quiet_hours=lambda ts: False)
        service.runtime = None
        spoken = []
        service.evaluate_proactive_speaking = lambda pet, cur: self._ret(("理由", "话题"))
        service.proactive_speak = lambda pet, chat, reason, topic="": spoken.append((reason, topic))

        # 刚回复过（10 秒前），处于 60 秒冷却内
        state = {
            "last_proactive_ts": 0.0,
            "last_reply_ts": now - 10,
            "last_proactive_answered": True,
        }
        res = await service.tick_pet(1, "c1", current=state, now=now)
        self.assertFalse(res)
        self.assertEqual(spoken, [])

    async def test_tick_suppressed_when_chat_locked(self):
        import asyncio
        import time
        from runtime import RuntimeState
        from services.autonomous_service import AutonomousService
        from types import SimpleNamespace
        service = AutonomousService.__new__(AutonomousService)
        now = time.time()
        service.config = SimpleNamespace(
            proactive_cooldown_sec=600,
            unanswered_cooldown_sec=1800,
            proactive_reply_cooldown_sec=60.0,
            scheduled_events=(),
        )
        service.pick_scheduled_event = lambda pet, cur, t: None
        service.state_domain = SimpleNamespace(in_quiet_hours=lambda ts: False)
        service.runtime = RuntimeState()
        # 模拟当前 pet 正在回复锁定中
        lock = service.runtime.chat_locks.setdefault(1, asyncio.Lock())
        await lock.acquire()

        state = {
            "last_proactive_ts": 0.0,
            "last_reply_ts": 0.0,
            "last_proactive_answered": True,
        }
        res = await service.tick_pet(1, "c1", current=state, now=now)
        self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
