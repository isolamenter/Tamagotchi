from __future__ import annotations

import calendar
import random
import unittest
from collections import Counter

from dafeiyu.config import load_config
from dafeiyu.domain.conversation import ConversationContext
from dafeiyu.domain.memory import MemoryDomain
from dafeiyu.domain.state import StateDomain
from dafeiyu.domain.style import StyleDomain
from tests.helpers import make_config


class StyleDomainTests(unittest.TestCase):
    def setUp(self):
        self.style = StyleDomain(make_config(), rng=random.Random(0))

    def test_selects_contextual_persona_lines(self):
        examples = self.style.select_examples("你懂吗")
        self.assertEqual(examples[0]["response"], "这块我还不太懂，不能乱答你。")

        reassurance = self.style.select_examples("这个方案能搞定吗，稳不稳")
        self.assertEqual(reassurance[0]["response"], "能做，我们按这个来。")

    def test_no_match_does_not_inject_unrelated_style_material(self):
        self.assertEqual(self.style.select_examples("窗外的树叶晃了一下"), [])
        block = self.style.render_examples_block("窗外的树叶晃了一下")
        self.assertEqual(block, "")

    def test_scope_keeps_reply_examples_out_of_proactive_prompt(self):
        self.assertEqual(
            self.style.select_examples("你懂吗", scope="proactive"),
            [],
        )
        proactive = self.style.select_examples("有点无聊，今晚有什么安排吗", scope="proactive")
        self.assertEqual(proactive[0]["response"], "晚上有空的冒个泡")

    def test_rendered_block_marks_examples_as_style_not_facts(self):
        block = self.style.render_examples_block("晚上一起玩不玩")
        self.assertTrue(
            any(
                response in block
                for response in (
                    "晚上有空的冒个泡",
                    "今晚有局吗 我饭后上线",
                )
            )
        )
        self.assertIn("口头禅语料", block)
        self.assertIn("不要用口头禅承担事实和完整推理", block)

    def test_limit_zero_returns_no_examples(self):
        self.assertEqual(self.style.select_examples("晚上一起玩不玩", limit=0), [])

    def test_original_corpus_has_complete_scenes_and_no_fake_chat_sources(self):
        responses = []
        for example in self.style.examples:
            self.assertTrue(example.get("context"))
            self.assertTrue(example.get("keywords"))
            self.assertTrue(example.get("scopes"))
            self.assertTrue(example.get("intent"))
            self.assertIn(example.get("risk"), {"normal", "aggressive"})
            variants = self.style.response_variants(example)
            self.assertTrue(variants)
            self.assertEqual(example.get("source_count"), sum(weight for _text, weight in variants))
            responses.extend(text for text, _weight in variants)
        self.assertEqual(len(self.style.examples), 100)
        self.assertEqual(len(responses), 200)
        self.assertEqual(len(responses), len(set(responses)))
        self.assertEqual(
            sum(int(example["source_count"]) for example in self.style.examples),
            200,
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
        self.assertEqual(len(cards), 60)
        self.assertEqual(len({self.style.example_card_id(item) for item in cards}), 60)
        self.assertTrue(all("source_message_id" not in item for item in cards))
        self.assertTrue(all(item.get("response") for item in cards))
        self.assertTrue(all(isinstance(item.get("context"), list) for item in cards))
        self.assertEqual(
            Counter(item["mode"] for item in cards),
            {
                "reasoning": 10,
                "correction": 10,
                "teasing": 10,
                "uncertainty": 10,
                "reaction": 10,
                "conversation": 10,
            },
        )

    def test_example_card_embedding_records_are_separate_from_catchphrases(self):
        records = self.style.corpus_records()
        by_type = {}
        for record in records:
            by_type[record["embedding_type"]] = by_type.get(record["embedding_type"], 0) + 1
        self.assertEqual(by_type, {"catchphrase": 100, "example_card": 60})

    def test_persona_scenes_match_without_forcing_jokes_into_serious_topics(self):
        cases = {
            "给你一碗白饭": "food",
            "大肥鱼你又胖了": "body_defense",
            "又摸鱼被我抓到了": "slacking",
            "这次做得真聪明": "praise",
            "接口超时怎么处理": "debug",
            "你漏了条件": "correction",
            "今天心情很差": "care",
            "祝我好运，明天面试": "blessing",
        }
        for query, intent in cases.items():
            with self.subTest(query=query):
                selected = self.style.select_examples(query)
                self.assertTrue(selected)
                self.assertEqual(selected[0]["intent"], intent)
                if intent in {"debug", "correction", "care", "blessing"}:
                    self.assertEqual(selected[0]["risk"], "normal")
                    self.assertNotRegex(selected[0]["response"], "白饭|尾巴|算力")

    def test_example_card_retrieval_and_rendering_teaches_structure_first(self):
        target = next(card for card in self.style.example_cards if card["scene"] == "debug_timeout")
        card_id = self.style.example_card_id(target)
        cards = self.style.select_example_cards(
            "帮我分析接口请求一直超时",
            query_vector=[1.0, 0.0],
            vectors={card_id: [1.0, 0.0]},
        )
        self.assertEqual(cards[0]["scene"], "debug_timeout")
        catchphrases = self.style.select_examples("确实", limit=1)
        block = self.style.render_selected(catchphrases, example_cards=cards)
        self.assertLess(block.index("第一环节"), block.index("第二环节"))
        self.assertIn("群友：接口一直超时", block)
        self.assertIn("我的回复：不一定是服务器性能的问题哦。", block)
        self.assertNotIn("source_message_id", block)
        self.assertIn("虚构的写作示例", block)

    def test_aggressive_example_card_requires_matching_current_context(self):
        target = next(card for card in self.style.example_cards if card["scene"] == "roast_play")
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
        self.assertEqual(selected[0]["scene"], "roast_play")

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

    def test_mutual_teasing_requires_context_and_avoids_personal_insults(self):
        roast = self.style.select_examples("这个操作也太菜了")
        roast_responses = [item["response"] for item in roast]
        self.assertIn("这局先记着，下一局让我找回来嘛。", roast_responses)

        serious = self.style.select_examples("我今天心情很差")
        aggressive = {"这局先记着，下一局让我找回来嘛。"}
        self.assertTrue(aggressive.isdisjoint(item["response"] for item in serious))

        insult = self.style.select_examples("傻狗")
        self.assertEqual(insult, [])

    def test_direct_request_to_laugh_gets_a_laughter_scene(self):
        selected = self.style.select_examples("这个也太搞笑了")
        self.assertEqual(selected[0]["response"], "笑得饭都顾不上扒了")

    def test_base_style_has_adaptive_modes_without_character_limits(self):
        config = make_config()
        self.assertIn("你叫大肥鱼", config.persona_style_prompt)
        self.assertIn("DeepSeek娘", config.persona_style_prompt)
        self.assertIn("蓝白女仆裙", config.persona_style_prompt)
        self.assertIn("服装是角色识别锚点", config.persona_style_prompt)
        self.assertIn("几个字或一两句话说完直接停", config.persona_style_prompt)
        self.assertIn("条理清晰地讲清原因和关键步骤", config.persona_style_prompt)
        self.assertIn('"reply_mode": "normal"', config.json_output_prompt)
        self.assertIn("reaction", config.json_output_prompt)
        self.assertIn("substantive", config.json_output_prompt)
        self.assertNotIn("【原句语料】", config.persona_style_prompt)
        self.assertNotIn("【长度示例】", config.persona_style_prompt)
        self.assertNotRegex(config.persona_style_prompt, r"\d+\s*个汉字")
        self.assertNotRegex(config.persona_style_reinforcement, r"\d+\s*个汉字")
        self.assertEqual(config.reply_max_tokens, 350)
        self.assertEqual(config.reply_temperature, 0.5)

    def test_semantic_retrieval_matches_paraphrase_without_keyword(self):
        target = next(item for item in self.style.examples if item["response"] == "困了 先眯一会儿")
        vectors = {self.style.example_id(target): [1.0, 0.0]}
        selected = self.style.select_examples(
            "身体被掏空，只想躺平",
            query_vector=[1.0, 0.0],
            vectors=vectors,
        )
        self.assertEqual(selected[0]["response"], "困了 先眯一会儿")

    def test_aggressive_semantic_match_still_requires_current_keyword(self):
        target = next(item for item in self.style.examples if item["intent"] == "roast")
        vectors = {self.style.example_id(target): [1.0, 0.0]}
        selected = self.style.select_examples(
            "今天心情很差",
            query_vector=[1.0, 0.0],
            vectors=vectors,
        )
        self.assertNotIn("这局先记着，下一局让我找回来嘛。", [item["response"] for item in selected])

    def test_second_example_must_share_intent_and_be_high_confidence(self):
        responses = {"笑得饭都顾不上扒了", "没绷住 碗先放下"}
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

    def test_style_query_uses_recent_dialogue_with_assistant(self):
        query = self.style.build_query(
            "啊？",
            [
                {"role": "user", "content": "第一条"},
                {"role": "assistant", "content": "旧文风回复"},
                {"role": "user", "content": "第二条"},
                {"role": "assistant", "content": "我刚才说的判断"},
            ],
        )
        self.assertNotIn("第一条", query)
        self.assertNotIn("旧文风回复", query)
        self.assertIn("第二条", query)
        self.assertIn("我刚才说的判断", query)
        self.assertTrue(query.endswith("当前消息：啊？"))

    def test_base_messages_keeps_complete_recent_dialogue(self):
        pet = ConversationContext(make_config())
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
            self.assertIn(f"a{index}", contents)
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
        self.assertNotIn("custom", normalized)

    def test_public_state_returns_clean_dict(self):
        st = self.state.initial_state()
        pub = self.state.public_state(st)
        self.assertIn("last_update_ts", pub)
        self.assertIn("last_reply_ts", pub)
        self.assertNotIn("active_need", pub)


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
                {"id": 2, "when": "later", "who": "A", "what": "second", "vibe": "", "score": 0.7},
                {"id": 1, "when": "first", "who": "B", "what": "first", "vibe": "", "score": 0.9},
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
                    "CHAT_MODEL": "fixture-chat",
                    "EMBED_MODEL": "fixture-embedding",
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
                "CHAT_MODEL": "fixture-chat",
                "EMBED_MODEL": "fixture-embedding",
                "FEISHU_VERIFICATION_TOKEN": "verify-token",
                "LLM_PROVIDER": "gemini",
                "GEMINI_API_KEY": "gemini-key",
                "STATE_DB": "state.db",
            }
        )
        self.assertEqual(config.llm_provider, "gemini")
        self.assertEqual(config.openai_api_key, "")


class MentionRenderTests(unittest.TestCase):
    def test_clean_text_replaces_key_with_name(self):
        from dafeiyu.domain.conversation import ConversationContext

        pet = ConversationContext.__new__(ConversationContext)
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


if __name__ == "__main__":
    unittest.main()
