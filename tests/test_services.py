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


if __name__ == "__main__":
    unittest.main()
