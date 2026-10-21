from __future__ import annotations

import unittest

from dafeiyu.domain.style import StyleDomain
from tests.helpers import make_config


class ConversationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.style = StyleDomain(make_config())

    def test_plain_character_name_never_selects_body_defense_even_with_high_semantic_score(self):
        item = next(e for e in self.style.examples if e["intent"] == "body_defense")
        selected = self.style.select_examples(
            "大肥鱼你好", query_vector=[1.0, 0.0], vectors={self.style.example_id(item): [1.0, 0.0]}
        )
        self.assertTrue(all(e["intent"] != "body_defense" for e in selected))
        for query in ["你好大肥鱼", "大肥鱼你好", "大肥鱼，我又胖了"]:
            with self.subTest(query=query):
                self.assertTrue(
                    all(
                        e["intent"] != "body_defense"
                        for e in self.style.select_examples(
                            query,
                            query_vector=[1.0, 0.0],
                            vectors={self.style.example_id(item): [1.0, 0.0]},
                        )
                    )
                )
        self.assertEqual(self.style.select_examples("你又胖了")[0]["intent"], "body_defense")

    def test_other_person_body_comment_does_not_trigger_robot_defense(self):
        self.assertFalse(
            any(
                e["intent"] == "body_defense"
                for e in self.style.select_examples("我又胖了，准备少吃点")
            )
        )

    def test_technical_complaint_and_explicit_help_use_different_examples(self):
        card = next(e for e in self.style.example_cards if e["scene"] == "debug_timeout")
        vectors = {self.style.example_card_id(card): [1.0, 0.0]}
        self.assertEqual(
            self.style.select_example_cards(
                "今天被这个超时 bug 搞累了", query_vector=[1.0, 0.0], vectors=vectors
            ),
            [],
        )
        self.assertEqual(
            self.style.select_example_cards(
                "帮我分析这个超时 bug", query_vector=[1.0, 0.0], vectors=vectors
            )[0]["scene"],
            "debug_timeout",
        )

    def test_care_discards_semantically_similar_teasing(self):
        card = next(e for e in self.style.example_cards if e["scene"] == "caught_slacking")
        self.assertEqual(
            self.style.select_example_cards(
                "今天很难受，不想听建议",
                query_vector=[1.0, 0.0],
                vectors={self.style.example_card_id(card): [1.0, 0.0]},
            ),
            [],
        )

    def test_recently_used_example_is_not_injected_again(self):
        selected = self.style.select_examples("你好")
        self.assertTrue(selected)
        self.assertEqual(
            self.style.select_examples("你好", exclude_ids={selected[0]["_example_id"]}), []
        )
