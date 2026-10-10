from __future__ import annotations

import calendar
import random
import unittest
from collections import Counter

from config import load_config
from domain.card import CardDomain
from domain.memory import MemoryDomain
from domain.pet import PetDomain
from domain.state import StateDomain
from domain.style import StyleDomain
from services.autonomous_service import AutonomousService


def make_config(db_path: str = "state.db"):
    return load_config(
        {
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_VERIFICATION_TOKEN": "verify-token",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_API_KEY": "key",
            "STATE_DB": db_path,
        }
    )


class StyleDomainTests(unittest.TestCase):
    def setUp(self):
        self.style = StyleDomain(make_config(), rng=random.Random(0))

    def test_selects_contextual_original_lines(self):
        examples = self.style.select_examples("你懂吗")
        self.assertEqual(examples[0]["response"], "不懂")

        reassurance = self.style.select_examples("这个方案能搞定吗，稳不稳")
        self.assertEqual(reassurance[0]["response"], "没问题")

    def test_no_match_does_not_inject_unrelated_style_material(self):
        self.assertEqual(self.style.select_examples("窗外的树叶晃了一下"), [])
        block = self.style.render_examples_block("窗外的树叶晃了一下")
        self.assertEqual(block, "")

    def test_scope_keeps_reply_examples_out_of_proactive_prompt(self):
        self.assertEqual(
            self.style.select_examples("你懂吗", scope="proactive"),
            [],
        )
        proactive = self.style.select_examples(
            "有点无聊，今晚有什么安排吗", scope="proactive"
        )
        self.assertEqual(proactive[0]["response"], "晚上有无")

    def test_rendered_block_marks_examples_as_style_not_facts(self):
        block = self.style.render_examples_block("晚上一起玩不玩")
        self.assertTrue(
            any(
                response in block
                for response in (
                    "晚上有无",
                    "晚上玩不玩",
                    "晚上玩吗",
                    "晚上你们玩不玩啊",
                    "晚上打不打",
                )
            )
        )
        self.assertIn("口头禅语料", block)
        self.assertIn("不要用口头禅承担事实和完整推理", block)

    def test_limit_zero_returns_no_examples(self):
        self.assertEqual(
            self.style.select_examples("晚上一起玩不玩", limit=0), []
        )

    def test_curated_corpus_has_100_general_grouped_entries(self):
        responses = []
        for example in self.style.examples:
            self.assertTrue(example.get("context"))
            self.assertTrue(example.get("keywords"))
            self.assertTrue(example.get("scopes"))
            self.assertTrue(example.get("intent"))
            self.assertIn(example.get("risk"), {"normal", "aggressive"})
            variants = self.style.response_variants(example)
            self.assertTrue(variants)
            self.assertEqual(
                example.get("source_count"), sum(weight for _text, weight in variants)
            )
            responses.extend(text for text, _weight in variants)
        self.assertEqual(len(self.style.examples), 100)
        self.assertEqual(len(responses), 324)
        self.assertEqual(len(responses), len(set(responses)))
        self.assertEqual(
            sum(int(example["source_count"]) for example in self.style.examples),
            622,
        )
        excluded_as_too_specific = {
            "问问lz",
            "买的宝马双r说是",
            "狗蛋沉迷工作",
            "到漕河泾了家人们",
            "昨天的富姐 晚上还约人一起出门喝酒的",
        }
        self.assertTrue(excluded_as_too_specific.isdisjoint(responses))
        cards = self.style.example_cards
        self.assertEqual(len(cards), 100)
        self.assertEqual(
            len({item["source_message_id"] for item in cards}), 100
        )
        self.assertTrue(all(item.get("response") for item in cards))
        self.assertTrue(
            all(isinstance(item.get("context"), list) for item in cards)
        )
        self.assertEqual(
            Counter(item["mode"] for item in cards),
            {
                "reasoning": 17,
                "correction": 17,
                "teasing": 17,
                "uncertainty": 17,
                "reaction": 16,
                "conversation": 16,
            },
        )

    def test_example_card_embedding_records_are_separate_from_catchphrases(self):
        records = self.style.corpus_records()
        by_type = {}
        for record in records:
            by_type[record["embedding_type"]] = (
                by_type.get(record["embedding_type"], 0) + 1
            )
        self.assertEqual(by_type, {"catchphrase": 100, "example_card": 100})

    def test_example_card_retrieval_and_rendering_teaches_structure_first(self):
        target = next(
            card
            for card in self.style.example_cards
            if card["source_message_id"] == 6458
        )
        card_id = self.style.example_card_id(target)
        cards = self.style.select_example_cards(
            "长距离骑车要不要中途休息",
            query_vector=[1.0, 0.0],
            vectors={card_id: [1.0, 0.0]},
        )
        self.assertEqual(cards[0]["source_message_id"], 6458)
        catchphrases = self.style.select_examples("确实", limit=1)
        block = self.style.render_selected(
            catchphrases, example_cards=cards
        )
        self.assertLess(block.index("第一环节"), block.index("第二环节"))
        self.assertIn("我前面：等我骑骑看", block)
        self.assertIn(
            "我的回复：感觉骑20km没啥问题 但是中间可能得休息一会", block
        )
        self.assertNotIn("source_message_id", block)

    def test_aggressive_example_card_requires_matching_current_context(self):
        target = next(
            card
            for card in self.style.example_cards
            if card["source_message_id"] == 25063
        )
        card_id = self.style.example_card_id(target)
        vectors = {card_id: [1.0, 0.0]}
        self.assertEqual(
            self.style.select_example_cards(
                "今天心情不好",
                query_vector=[1.0, 0.0],
                vectors=vectors,
            ),
            [],
        )
        for ordinary_context in (
            "最近工作压力很大",
            "垃圾分类怎么做",
            "网络攻击怎么防",
            "蔬菜怎么搭配",
            "傻瓜相机怎么用",
            "高压锅怎么炖肉",
        ):
            self.assertEqual(
                self.style.select_example_cards(
                    ordinary_context,
                    query_vector=[1.0, 0.0],
                    vectors=vectors,
                ),
                [],
            )
        selected = self.style.select_example_cards(
            "这个操作太菜了",
            query_vector=[1.0, 0.0],
            vectors=vectors,
        )
        self.assertEqual(selected[0]["source_message_id"], 25063)

    def test_grouped_variants_use_source_frequency_as_random_weight(self):
        class ChoiceSpy:
            def __init__(self):
                self.weights = None

            def choices(self, population, *, weights, k):
                self.weights = weights
                self.assertion = (population, k)
                return [population[-1]]

        spy = ChoiceSpy()
        style = StyleDomain(make_config(), rng=spy)
        example = style.examples[0]
        block = style.render_selected([example], scope="test")
        variants = style.response_variants(example)
        self.assertIn(variants[-1][0], block)
        self.assertEqual(spy.weights, [weight for _text, weight in variants])
        self.assertEqual(spy.assertion, ([text for text, _weight in variants], 1))

    def test_frequency_breaks_a_relevance_tie_between_groups(self):
        config = make_config()
        config.style_corpus["examples"] = [
            {
                "context": "低频",
                "keywords": ["同样"],
                "scopes": ["reply"],
                "intent": "agreement",
                "risk": "normal",
                "source_count": 2,
                "variants": [{"text": "低频原句", "source_count": 2}],
            },
            {
                "context": "高频",
                "keywords": ["同样"],
                "scopes": ["reply"],
                "intent": "agreement",
                "risk": "normal",
                "source_count": 9,
                "variants": [{"text": "高频原句", "source_count": 9}],
            },
        ]
        style = StyleDomain(config, rng=random.Random(0))
        selected = style.select_examples("同样", limit=1)
        self.assertEqual(selected[0]["response"], "高频原句")

    def test_aggressive_examples_require_matching_roast_context(self):
        roast = self.style.select_examples("这个操作也太菜了")
        roast_responses = [item["response"] for item in roast]
        self.assertIn("菜就多练", roast_responses)
        self.assertNotIn("那很厉害了", roast_responses)

        serious = self.style.select_examples("我今天心情很差")
        aggressive = {"菜就多练", "真抽你的", "傻狗", "人工智障"}
        self.assertTrue(aggressive.isdisjoint(item["response"] for item in serious))

        insult = self.style.select_examples("傻狗")
        self.assertEqual(insult[0]["response"], "傻狗")

    def test_direct_request_to_laugh_gets_a_laughter_scene(self):
        selected = self.style.select_examples("这个也太搞笑了")
        self.assertEqual(selected[0]["response"], "笑死")

    def test_base_style_has_adaptive_modes_without_character_limits(self):
        config = make_config()
        self.assertIn("你叫小苍蝇", config.pet_style_prompt)
        self.assertIn("苍蝇电子宠物", config.pet_style_prompt)
        self.assertIn("几个字已经说清就直接停", config.pet_style_prompt)
        self.assertIn("自然说成一段也没问题", config.pet_style_prompt)
        self.assertIn('"reply_mode": "normal"', config.json_output_prompt)
        self.assertIn("reaction", config.json_output_prompt)
        self.assertIn("substantive", config.json_output_prompt)
        self.assertNotIn("【原句语料】", config.pet_style_prompt)
        self.assertNotIn("【长度示例】", config.pet_style_prompt)
        self.assertNotRegex(config.pet_style_prompt, r"\d+\s*个汉字")
        self.assertNotRegex(config.pet_style_reinforcement, r"\d+\s*个汉字")
        self.assertEqual(config.reply_max_tokens, 350)
        self.assertEqual(config.reply_temperature, 0.5)

    def test_semantic_retrieval_matches_paraphrase_without_keyword(self):
        target = next(
            item for item in self.style.examples if item["response"] == "困"
        )
        vectors = {self.style.example_id(target): [1.0, 0.0]}
        selected = self.style.select_examples(
            "身体被掏空，只想躺平",
            query_vector=[1.0, 0.0],
            vectors=vectors,
        )
        self.assertEqual(selected[0]["response"], "困")

    def test_aggressive_semantic_match_still_requires_current_keyword(self):
        target = next(
            item
            for item in self.style.examples
            if "菜就多练" in self.style.response_texts(item)
        )
        vectors = {self.style.example_id(target): [1.0, 0.0]}
        selected = self.style.select_examples(
            "今天心情很差",
            query_vector=[1.0, 0.0],
            vectors=vectors,
        )
        self.assertNotIn("菜就多练", [item["response"] for item in selected])

    def test_second_example_must_share_intent_and_be_high_confidence(self):
        responses = {"笑死", "没绷住"}
        vectors = {
            self.style.example_id(item): [1.0, 0.0]
            for item in self.style.examples
            if item["response"] in responses
        }
        selected = self.style.select_examples(
            "这个画面",
            query_vector=[1.0, 0.0],
            vectors=vectors,
        )
        self.assertEqual({item["response"] for item in selected}, responses)

    def test_style_query_uses_two_user_messages_and_excludes_assistant(self):
        query = self.style.build_query(
            "啊？",
            [
                {"role": "user", "content": "第一条"},
                {"role": "assistant", "content": "旧文风回复"},
                {"role": "user", "content": "第二条"},
                {"role": "user", "content": "第三条"},
            ],
        )
        self.assertNotIn("第一条", query)
        self.assertNotIn("旧文风回复", query)
        self.assertIn("第二条", query)
        self.assertIn("第三条", query)
        self.assertTrue(query.endswith("当前消息：啊？"))

    def test_base_messages_keeps_all_users_and_latest_two_assistants(self):
        pet = PetDomain(make_config())
        history = []
        for index in range(6):
            history.extend(
                [
                    {"role": "user", "content": f"u{index}"},
                    {"role": "assistant", "content": f"a{index}"},
                ]
            )
        messages = pet.base_messages("system", history)
        contents = [item["content"] for item in messages]
        for index in range(6):
            self.assertIn(f"u{index}", "\n".join(contents))
        for index in range(4):
            self.assertNotIn(f"a{index}", contents)
        self.assertIn("a4", contents)
        self.assertEqual(contents[-1], "a5")


class StateDomainTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.state = StateDomain(self.config)

    def local_ts(self, year: int, month: int, day: int, hour: int) -> float:
        return float(calendar.timegm((year, month, day, hour - 8, 0, 0)))

    def test_quiet_hours_cross_midnight(self):
        self.assertTrue(self.state.in_quiet_hours(self.local_ts(2026, 1, 1, 20)))
        self.assertTrue(self.state.in_quiet_hours(self.local_ts(2026, 1, 2, 9)))
        self.assertFalse(self.state.in_quiet_hours(self.local_ts(2026, 1, 2, 12)))

    def test_initial_state_contains_expected_keys(self):
        st = self.state.initial_state()
        self.assertIn("last_update_ts", st)
        self.assertIn("last_reply_ts", st)
        self.assertIn("last_proactive_ts", st)
        self.assertIn("last_proactive_answered", st)
        self.assertNotIn("satiety", st)
        self.assertNotIn("mood", st)

    def test_normalize_clears_active_need(self):
        raw = {"active_need": {"kind": "hungry"}, "custom": "keep"}
        normalized = self.state.normalize_state(raw)
        self.assertNotIn("active_need", normalized)
        self.assertEqual(normalized["custom"], "keep")

    def test_public_state_returns_clean_dict(self):
        st = self.state.initial_state()
        pub = self.state.public_state(st)
        self.assertIn("last_update_ts", pub)
        self.assertIn("last_reply_ts", pub)
        self.assertNotIn("active_need", pub)


class CardDomainTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.card = CardDomain(self.config)

    def test_build_pet_card_elements(self):
        card = self.card.build_pet_card(1, "今天很开心", action_keys=["查看详情"])
        self.assertEqual(card["config"]["wide_screen_mode"], True)
        self.assertEqual(card["elements"][0]["tag"], "markdown")
        self.assertEqual(card["elements"][0]["content"], "今天很开心")
        actions = [el for el in card["elements"] if el.get("tag") == "action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["actions"][0]["text"]["content"], "查看详情")

    def test_compose_card_text(self):
        composed = self.card.compose_card_text("base", ["line1", "line2"])
        self.assertIn("base", composed)
        self.assertIn("↳ line1", composed)
        self.assertIn("↳ line2", composed)


class MemoryDomainTests(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryDomain(make_config())

    def test_vector_pack_and_recall_render(self):
        vec = [0.1, 0.2, 0.3]
        unpacked = self.memory.vec_unpack(self.memory.vec_pack(vec))
        self.assertEqual(len(unpacked), 3)
        self.assertAlmostEqual(unpacked[0], 0.1, places=5)
        self.assertGreater(self.memory.cosine([1.0, 0.0], [0.5, 0.5]), 0)

        block = self.memory.render_recall_block(
            [
                {"id": 2, "when": "later", "who": "A", "what": "second", "vibe": ""},
                {"id": 1, "when": "first", "who": "B", "what": "first", "vibe": ""},
            ]
        )
        self.assertLess(block.index("first"), block.index("second"))


class ConfigTests(unittest.TestCase):
    def test_missing_verification_token_fails_fast(self):
        with self.assertRaises(KeyError):
            load_config(
                {
                    "FEISHU_APP_ID": "app",
                    "FEISHU_APP_SECRET": "secret",
                    "OPENAI_BASE_URL": "https://example.invalid/v1",
                    "OPENAI_API_KEY": "key",
                    "STATE_DB": "state.db",
                }
            )

    def test_gemini_only_config_does_not_require_openai_keys(self):
        config = load_config(
            {
                "FEISHU_APP_ID": "app",
                "FEISHU_APP_SECRET": "secret",
                "FEISHU_VERIFICATION_TOKEN": "verify-token",
                "LLM_PROVIDER": "gemini",
                "GEMINI_API_KEY": "gemini-key",
                "STATE_DB": "state.db",
            }
        )
        self.assertEqual(config.llm_provider, "gemini")
        self.assertEqual(config.openai_api_key, "")


if __name__ == "__main__":
    unittest.main()

class MentionRenderTests(unittest.TestCase):
    def test_clean_text_replaces_key_with_name(self):
        from domain.pet import PetDomain
        from services.reply_service import ReplyService
        pet = PetDomain.__new__(PetDomain)
        svc = ReplyService.__new__(ReplyService)
        # key 在文本里 → 内联替换成 @名字
        self.assertEqual(
            pet.clean_text("@_user_1 你是谁", [{"key": "@_user_1", "name": "二极管"}]),
            "@二极管 你是谁",
        )
        # 文本已是 @名字 → 不动
        self.assertEqual(
            pet.clean_text("@小苍蝇 回答我", [{"key": "@_user_1", "name": "小苍蝇"}]),
            "@小苍蝇 回答我",
        )
        # 无名/无 key → 原样保留，不删信息
        self.assertEqual(
            pet.clean_text("@_user_1 hi", [{"key": "@_user_1"}]),
            "@_user_1 hi",
        )
        # key 不在文本里 → clean_text 不动，由后缀补全
        text = pet.clean_text("我是罕见", [{"key": "@_user_2", "name": "两面派"}])
        self.assertEqual(
            svc._append_missing_mentions(
                text,
                [{"key": "@_user_2", "name": "两面派", "id": {"open_id": "ou_other"}}],
                "ou_me",
            ),
            "我是罕见 @两面派",
        )
        # 文本已含 @名 → 不重复补；发送者自己 → 跳过
        self.assertEqual(
            svc._append_missing_mentions("@二极管 你是谁", [{"name": "二极管"}], "ou_me"),
            "@二极管 你是谁",
        )
        self.assertEqual(
            svc._append_missing_mentions(
                "hello", [{"name": "我", "id": {"open_id": "ou_me"}}], "ou_me"
            ),
            "hello",
        )
