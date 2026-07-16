from __future__ import annotations

import calendar
import unittest

from config import load_config
from domain.card import CardDomain
from domain.gameplay import GameplayDomain
from domain.memory import MemoryDomain
from domain.state import StateDomain
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

    def test_weekend_is_rest_time(self):
        self.assertTrue(self.state.in_quiet_hours(self.local_ts(2026, 1, 3, 12)))
        self.assertTrue(self.state.in_quiet_hours(self.local_ts(2026, 1, 4, 12)))
        self.assertFalse(self.state.in_quiet_hours(self.local_ts(2026, 1, 5, 12)))

    def test_weekend_rest_counts_as_quiet_decay_time(self):
        q_hours, a_hours = self.state.partition_hours(
            self.local_ts(2026, 1, 3, 10),
            self.local_ts(2026, 1, 3, 12),
        )
        self.assertEqual(q_hours, 2.0)
        self.assertEqual(a_hours, 0.0)

    def test_scheduled_events_skip_weekend_rest(self):
        service = AutonomousService(
            self.config,
            self.state,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        state = self.state.initial_state()
        self.assertIsNone(
            service.scheduled_event_due(state, self.local_ts(2026, 1, 3, 12))
        )
        self.assertIsNotNone(
            service.scheduled_event_due(state, self.local_ts(2026, 1, 5, 10))
        )
        # Restarting hours later must not replay a whole day's scheduled cards.
        self.assertIsNone(
            service.scheduled_event_due(state, self.local_ts(2026, 1, 5, 12))
        )

    def test_tick_fires_all_due_scheduled_events_one_tick(self):
        import asyncio

        service = AutonomousService(
            self.config,
            self.state,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        # 模拟「DB 里逐步累计 date_key 标记」：fake scheduled_speak 标记自己的 state_key 并回传整份状态
        db_state = dict(self.state.initial_state())
        calls: list[str] = []

        async def fake_scheduled(pet_id, chat_id, event, date_key, mark_date=True):
            calls.append(event["kind"])
            db_state[event["state_key"]] = date_key
            return "reply", dict(db_state)

        service.scheduled_speak = fake_scheduled
        # 定时事件只在一小时补发窗口内触发；19:00 这一 tick 只触发日记。
        now = self.local_ts(2026, 1, 5, 19)
        spoke = asyncio.run(
            service.tick_pet(1, "chat", current=dict(db_state), now=now)
        )
        self.assertTrue(spoke)
        self.assertEqual(calls, ["diary"])

    def test_state_band(self):
        self.assertEqual(self.state.state_band("satiety", 10), "satiety_extreme_low")
        self.assertEqual(self.state.state_band("mood", 10), "mood_extreme_low")

    def test_decay_updates_timestamp_and_bounds_values(self):
        stored = self.state.initial_state()
        stored["last_update_ts"] = 0.0
        decayed = self.state.decay_state(stored, 3600.0, pet_id=1)
        self.assertEqual(decayed["last_update_ts"], 3600.0)
        for key in self.config.state_numeric_keys:
            self.assertGreaterEqual(decayed[key], 0.0)
            self.assertLessEqual(decayed[key], 100.0)


class CardDomainTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.state = StateDomain(self.config)
        self.card = CardDomain(self.config, self.state)

    def test_card_actions_and_text_helpers(self):
        pet_state = {key: 50.0 for key in self.config.state_numeric_keys}
        pet_state["satiety"] = 10.0
        self.assertIn("feed", self.card.pick_card_actions(pet_state))

        text = self.card.compose_card_text("hello", ["one"], "two")
        self.assertIn("hello", text)
        self.assertIn("one", text)
        self.assertIn("two", text)

        card = self.card.build_pet_card(1, "hello", pet_state, action_keys=["feed"])
        value = card["elements"][-1]["actions"][0]["value"]
        self.assertEqual(value["v"], 2)
        self.assertIn("card_id", value)
        self.assertIn("expires_at", value)



class GameplayDomainTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.state = StateDomain(self.config)
        self.gameplay = GameplayDomain(self.config)

    def test_need_detection_prefers_extreme_severity(self):
        state = {
            **self.state.initial_state(),
            "satiety": 19,
            "energy": 5,
        }
        detected = self.gameplay.detect_need_kind(state, 1000.0)
        self.assertEqual(detected, ("sleepy", 2))

    def test_need_choice_resolves_state_and_cooldown(self):
        now = 1000.0
        state = {
            **self.state.initial_state(),
            "satiety": 10,
        }
        state, need = self.gameplay.maybe_create_need(state, now, pet_id=1)
        self.assertEqual(need["kind"], "hungry")

        result = self.gameplay.apply_choice(state, "feed", "群友-A", now + 1)
        self.assertEqual(result.state["active_need"], {})
        self.assertEqual(result.state["satiety"], 55)
        self.assertEqual(result.state["mood"], 74)
        self.assertEqual(result.state["affection"], 52)
        self.assertGreater(result.state["need_cooldowns"]["hungry"], now)
        detected = self.gameplay.detect_need_kind(result.state, now + 2)
        self.assertIsNone(detected)

    def test_free_card_action_uses_same_settlement_path(self):
        now = 1000.0
        state = {
            **self.state.initial_state(),
            "satiety": 10,
        }

        result = self.gameplay.apply_card_action(state, "feed", "群友-A", now)

        self.assertEqual(result.state["satiety"], 55)
        self.assertEqual(result.state["mood"], 74)
        self.assertEqual(result.state["affection"], 52)
        self.assertEqual(result.state["need_cooldowns"], {})

    def test_fixed_card_can_use_free_rule_without_resolving_need(self):
        now = 1000.0
        state = {
            **self.state.initial_state(),
            "active_need": self.gameplay.build_need("hungry", 1, now),
        }

        result = self.gameplay.apply_card_action(
            state, "goodnight", "系统", now, prefer_free=True
        )

        self.assertEqual(result.state["active_need"]["kind"], "hungry")
        self.assertEqual(result.state["mood"], 74)
        self.assertEqual(result.state["affection"], 53)

    def test_expired_need_escalates_and_keeps_the_problem(self):
        now = 1000.0
        state = {
            **self.state.initial_state(),
            "satiety": 10,
            "active_need": self.gameplay.build_need("hungry", 1, now - 3600),
        }
        state["active_need"]["expires_at"] = now - 1

        cleared = self.gameplay.expired_need_cleared(state, now)

        self.assertEqual(cleared["active_need"]["kind"], "hungry")
        self.assertEqual(cleared["active_need"]["severity"], 2)
        self.assertEqual(cleared["active_need"]["round"], 1)
        self.assertEqual(cleared["active_need"]["announced_card_id"], "")
        self.assertEqual(cleared["need_cooldowns"], {})

    def test_weak_need_choice_is_partial_and_continues_with_new_round(self):
        now = 1000.0
        state = {**self.state.initial_state(), "satiety": 10}
        state, _ = self.gameplay.maybe_create_need(state, now, pet_id=1)
        result = self.gameplay.apply_choice(state, "promise_food", "群友-A", now + 1)
        self.assertFalse(result.resolved)
        self.assertEqual(result.state["active_need"]["kind"], "hungry")
        self.assertEqual(result.state["active_need"]["round"], 1)
        self.assertEqual(result.state["active_need"]["announced_card_id"], "")

    def test_need_ttl_pauses_and_resumes(self):
        state = {**self.state.initial_state(), "active_need": self.gameplay.build_need("hungry", 1, 1000)}
        paused = self.gameplay.pause_need(state, 1100)
        self.assertEqual(
            paused["active_need"]["remaining_ttl_sec"],
            self.config.gameplay_need_ttl_sec - 100,
        )
        resumed = self.gameplay.resume_need(paused, 2000)
        self.assertEqual(
            resumed["active_need"]["expires_at"],
            2000 + self.config.gameplay_need_ttl_sec - 100,
        )


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
