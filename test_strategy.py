"""Regression checks for the bot's geometry, mode signal and replay handling."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import unittest

from arena_geometry import Prism
from strategy_bot import Bot, intercept


ROOT = Path(__file__).resolve().parent


class StrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with gzip.open(ROOT / "check/stream.jsonl.gz", "rt", encoding="utf-8") as stream:
            cls.hello = json.loads(next(stream))
            cls.replay = [json.loads(line) for line in stream]
        cls.hello["map"]["dir"] = str(ROOT / "arena")

    def test_prism_hole_and_bridge_clearance(self):
        prism = Prism({"base": 8, "top": 12, "ring": [0, 0, 10, 0, 10, 10, 0, 10],
                       "holes": [[4, 4, 6, 4, 6, 6, 4, 6]]})
        self.assertFalse(prism.intersects((-2, 5, 4), (12, 5, 4)))
        self.assertTrue(prism.intersects((-2, 5, 10), (12, 5, 10)))
        self.assertFalse(prism.intersects((5, 5, 7), (5, 5, 13)))
        self.assertTrue(prism.intersects((2, 5, 7), (2, 5, 13)))

    def test_intercept_accounts_for_muzzle_offset(self):
        self.assertAlmostEqual(intercept((100, 0, 0), (10, 0, 0), 100, 4), 96 / 90)
        self.assertIsNone(intercept((100, 0, 0), (110, 0, 0), 100))

    def test_only_unexplained_early_death_changes_mode(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        obs.update(alive=15, t=10, events=[{"type": "kill", "target": 8}], beams=[])
        bot.update_situation(obs, [])
        self.assertFalse(bot.pvp)
        obs.update(alive=14, t=20, events=[])
        bot.update_situation(obs, [])
        self.assertTrue(bot.pvp)

    def test_projectile_approach_and_height(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        obs["self"].update(pos=[0, 0, 0], vel=[0, 0, 0])
        obs["projectiles"] = [{"pos": [45, 1, 5], "vel": [-200, 0, 0], "kind": "gun"}]
        self.assertLess(bot.projectile_threat(obs), 0)
        obs["projectiles"][0]["pos"][2] = 35
        self.assertIsNone(bot.projectile_threat(obs))

    def test_recorded_observations_without_wrapper_fallback(self):
        bot = Bot(self.hello)
        for obs in self.replay:
            if obs["type"] == "obs":
                with self.subTest(tick=obs["tick"]):
                    self.assertEqual(bot.act(obs)["tick"], obs["tick"])


if __name__ == "__main__":
    unittest.main()
