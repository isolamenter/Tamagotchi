from __future__ import annotations

import calendar
import unittest

from config import load_config
from domain.card import CardDomain
from domain.memory import MemoryDomain
from domain.state import StateDomain


def make_config(db_path: str = "state.db"):
    return load_config(
        {
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
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

    def test_delta_clamp_and_band(self):
        current = {key: 50.0 for key in self.config.state_numeric_keys}
        updated = self.state.apply_delta(current, {"hunger": 999, "mood": -999})
        self.assertEqual(updated["hunger"], 80.0)
        self.assertEqual(updated["mood"], 25.0)
        self.assertEqual(self.state.state_band("hunger", 90), "hunger_extreme_high")
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
        pet_state["hunger"] = 90.0
        self.assertIn("feed", self.card.pick_card_actions(pet_state))

        text = self.card.compose_card_text("hello", ["one"], "two")
        self.assertIn("hello", text)
        self.assertIn("one", text)
        self.assertIn("two", text)

        click_ts = {"u1": 1.0, "u2": 999.0, "bad": "old-shape"}
        self.assertEqual(self.card.prune_card_click_ts(click_ts, 1000.0), {"u2": 999.0})


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


if __name__ == "__main__":
    unittest.main()

