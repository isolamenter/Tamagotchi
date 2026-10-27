from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from dafeiyu.domain.meme import MemeCatalog
from dafeiyu.domain.scene import advice_refused, example_allowed
from dafeiyu.domain.style import StyleDomain


def catalog_fixture():
    catalog = MemeCatalog.__new__(MemeCatalog)
    catalog.scenes = {
        "hello": {
            "enabled": True,
            "allowed_contexts": ["casual"],
            "caption": "你好",
        }
    }
    return catalog


def style_fixture():
    return StyleDomain(
        SimpleNamespace(
            style_corpus={
                "example_cards": {
                    "cards": [
                        {"mode": "reasoning", "scene": "debug", "response": "先核对日志。"},
                        {"mode": "correction", "scene": "wrong_fact", "response": "这点说错了。"},
                    ]
                }
            },
            style_retrieval={
                "semantic_threshold": 0.62,
                "example_card_semantic_threshold": 0.62,
                "second_semantic_threshold": 0.68,
                "second_max_gap": 0.05,
                "keyword_bonus": 0.04,
                "keyword_bonus_cap": 0.12,
                "context_messages": 2,
                "query_max_chars": 1500,
            },
        )
    )


class SemanticSceneGateTests(unittest.TestCase):
    def test_reasoning_and_correction_paraphrases_can_reach_semantic_ranking(self):
        for card, text in [
            ({"mode": "reasoning"}, "带我从头捋捋这个现象的来龙去脉"),
            ({"mode": "correction"}, "这段经历属于阿青，不属于我"),
            ({"mode": "reasoning"}, "这个 bug 让我很难受，帮我分析一下"),
        ]:
            with self.subTest(text=text):
                self.assertTrue(example_allowed(card, text, card=True))
        self.assertTrue(
            example_allowed({"intent": "debug"}, "这个 bug 让我很难受，帮我分析一下")
        )

    def test_semantic_scores_still_select_or_reject_normal_examples(self):
        style = style_fixture()
        card = style.example_cards[0]
        query = "带我从头捋捋这个现象的来龙去脉"
        self.assertEqual(style.select_example_cards(query), [])
        self.assertEqual(
            style.select_example_cards(
                query, query_vector=[1.0, 0.0], vectors={style.example_card_id(card): [0.0, 1.0]}
            ),
            [],
        )
        selected = style.select_example_cards(
            query, query_vector=[1.0, 0.0], vectors={style.example_card_id(card): [1.0, 0.0]}
        )
        self.assertEqual([item["scene"] for item in selected], ["debug"])

    def test_explicit_no_advice_blocks_reasoning_even_with_perfect_similarity(self):
        style = style_fixture()
        card = style.example_cards[0]
        for text in ["不想听建议", "别给我建议", "不用讲道理", "我只是想吐槽"]:
            with self.subTest(text=text):
                self.assertTrue(advice_refused(text))
                self.assertFalse(example_allowed({"intent": "debug"}, text))
                self.assertEqual(
                    style.select_example_cards(
                        text,
                        query_vector=[1.0, 0.0],
                        vectors={style.example_card_id(card): [1.0, 0.0]},
                    ),
                    [],
                )

    def test_distress_is_not_itself_an_explicit_refusal(self):
        self.assertFalse(advice_refused("有点难受，想听听你的建议"))
        self.assertTrue(
            example_allowed({"mode": "reasoning"}, "有点难受，想听听你的建议", card=True)
        )

    def test_body_target_and_care_teasing_protections_remain(self):
        body = {"mode": "teasing", "scene": "body_defense"}
        for text in ["大肥鱼你好", "我又胖了", "小林又胖了"]:
            with self.subTest(text=text):
                self.assertFalse(example_allowed(body, text, card=True))
        self.assertTrue(example_allowed(body, "你又胖了", card=True))
        for text in ["今天很难受", "我只是想吐槽", "帮我分析数据库报错"]:
            with self.subTest(text=text):
                self.assertFalse(example_allowed({"mode": "teasing"}, text, card=True))

    def test_context_is_sent_to_semantic_review_instead_of_lexical_veto(self):
        catalog = catalog_fixture()
        user = "你好，我很难过"
        self.assertEqual(catalog.rejection_reason("hello", "你好", user, "我在呢"), "")
        payload = json.loads(catalog.review_messages("hello", "你好", user, "我在呢")[1]["content"])
        self.assertEqual(payload["scene"]["allowed_contexts"], ["casual"])
        self.assertEqual(payload["user_message"], user)
        self.assertIn("有任何不确定就拒绝", catalog.review_messages("hello", "你好", user, "我在呢")[0]["content"])

    def test_image_opt_out_evidence_and_enabled_tag_remain_hard_gates(self):
        catalog = catalog_fixture()
        for evidence, user, reason in [
            ("", "你好", "missing_evidence"),
            ("假的", "你好", "missing_evidence"),
            ("你好", "你好，别发图", "opt_out"),
            ("你好", "你好，只要文字", "opt_out"),
        ]:
            with self.subTest(user=user, evidence=evidence):
                self.assertEqual(catalog.rejection_reason("hello", evidence, user, "在呢"), reason)
        self.assertEqual(catalog.rejection_reason("missing", "你好", "你好", "在呢"), "disabled_tag")


class SemanticMemeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from dafeiyu.services.delivery import DeliveryService

        self.delivery = DeliveryService.__new__(DeliveryService)
        self.delivery.config = SimpleNamespace(meme_catalog=catalog_fixture())
        self.delivery.messages = SimpleNamespace(set_meme_tag=AsyncMock())
        self.delivery._review_meme = AsyncMock(return_value="context_mismatch")
        self.delivery._send_meme = AsyncMock(return_value=True)

    async def test_semantic_rejection_keeps_image_unsent(self):
        await self.delivery._maybe_send_meme(
            1, "fixture", 1, "我在呢", "direct", "normal", "hello", "你好", "你好，我很难过"
        )
        self.delivery._review_meme.assert_awaited_once()
        self.delivery._send_meme.assert_not_awaited()
        self.delivery.messages.set_meme_tag.assert_not_awaited()

    async def test_substantive_reply_never_reaches_review_or_image_send(self):
        await self.delivery._maybe_send_meme(
            1, "fixture", 1, "先检查日志", "direct", "substantive", "hello", "你好", "你好，查下报错"
        )
        self.delivery._review_meme.assert_not_awaited()
        self.delivery._send_meme.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
