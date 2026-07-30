from __future__ import annotations

import json
import unittest

from services.card_service import CardService
from services.reply_service import ReplyService


class ReplyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_p2p_is_direct_without_bot_lookup(self):
        class Feishu:
            async def get_bot_open_id(self):
                raise AssertionError("p2p must not need bot identity")

        service = ReplyService.__new__(ReplyService)
        service.feishu = Feishu()
        self.assertTrue(await service.is_direct_to_bot("p2p", []))

    async def test_group_fails_closed_when_bot_lookup_fails(self):
        class Feishu:
            async def get_bot_open_id(self):
                raise RuntimeError("temporary API error")

        service = ReplyService.__new__(ReplyService)
        service.feishu = Feishu()
        self.assertFalse(await service.is_direct_to_bot("group", []))


class ReplyParsingTests(unittest.TestCase):
    def test_parses_normal_and_double_encoded_json(self):
        normal = '{"reply":"难绷","speaker_name":"小明"}'
        self.assertEqual(
            ReplyService.parse_llm_content(normal), ("难绷", "小明", True)
        )
        double_encoded = json.dumps(normal, ensure_ascii=False)
        self.assertEqual(
            ReplyService.parse_llm_content(double_encoded),
            ("难绷", "小明", True),
        )

    def test_recovers_reply_when_later_json_field_is_malformed(self):
        malformed = (
            '{"reply": "发就发呗，反正你们开心就行。", '
            '"speaker_name": " : ""}'
        )
        reply, speaker_name, structured = ReplyService.parse_llm_content(malformed)
        self.assertEqual(reply, "发就发呗，反正你们开心就行。")
        self.assertEqual(speaker_name, "")
        self.assertFalse(structured)


class CardPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_v2_payload_is_a_safe_noop(self):
        service = CardService.__new__(CardService)
        result = await service.handle_card_action(
            {"action": {"value": {"pet_id": 1, "action": "feed"}}}, None
        )
        self.assertEqual(result["toast"]["type"], "info")
        self.assertIn("新", result["toast"]["content"])


if __name__ == "__main__":
    unittest.main()
