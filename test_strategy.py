"""Regression checks for the bot's geometry, mode signal and replay handling."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from arena_geometry import Prism, RoofEntry
from strategy_bot import Bot, PRACTICE, intercept


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

    def test_unexplained_deaths_do_not_change_practice_mode(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        obs.update(alive=12, t=20, events=[])
        bot.update_situation(obs)
        self.assertFalse(bot.pvp)

    def test_projectile_approach_and_height(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        obs["self"].update(pos=[0, 0, 0], vel=[0, 0, 0])
        obs["projectiles"] = [{"pos": [45, 1, 5], "vel": [-200, 0, 0], "kind": "gun"}]
        self.assertLess(bot.projectile_threat(obs), 0)
        obs["projectiles"][0]["pos"][2] = 35
        self.assertIsNone(bot.projectile_threat(obs))

    def test_kiting_uses_weapon_range(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        me["pos"] = [0, 0, 0]
        self.assertLess(bot.kite_goal(me, {"pos": [40, 0, 0]}, PRACTICE)[0], 0)
        self.assertGreater(bot.kite_goal(me, {"pos": [95, 0, 0]}, PRACTICE)[0], 0)
        stats = self.hello["config"]["weapons"]["lightning"]
        me["weapons"][1] = {"kind": "lightning", "stats": stats, "cooldown": 0}
        self.assertGreater(bot.kite_goal(me, {"pos": [32, 0, 0]}, PRACTICE)[0], 0)

    def test_fires_both_slots_when_their_aim_overlaps(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        stats = self.hello["config"]["weapons"]["lightning"]
        me["weapons"][1] = {"kind": "lightning", "stats": stats, "cooldown": 0}
        me["aim"] = [0, 0]
        action = {}
        with (patch.object(bot, "aim", return_value=(0, 0, 0.04, 0.08)),
              patch.object(bot, "actual_ray_hits", return_value=True)):
            bot.attack(me, {}, action)
            self.assertEqual(action["fire"], [True, True])
            me["energy"] = stats["energy"] - 1
            bot.attack(me, {}, action)
            self.assertEqual(action["fire"], [True, False])

    def test_ready_mortar_can_take_priority_over_gun(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        mortar = self.hello["config"]["weapons"]["mortar"]
        me["weapons"][1] = {"kind": "mortar", "stats": mortar, "cooldown": 0}
        me["aim"] = [0, 0.03]
        action = {}
        with (patch.object(bot, "aim", side_effect=[(0, 0, 0.04, 0.04), (0, 0.06, 0.04, 0.04)]),
              patch.object(bot, "actual_ray_hits", return_value=True)):
            bot.attack(me, {}, action)
        self.assertEqual(action["aim"], [0, 0.06])
        self.assertEqual(action["fire"], [False, True])

    def test_full_laser_releases_while_aligned_gun_can_fire(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        laser = self.hello["config"]["weapons"]["laser"]
        me["weapons"][1] = {"kind": "laser", "stats": laser, "cooldown": 0, "charge": laser["charge"]}
        me["aim"] = [0, 0]
        action = {}
        with (patch.object(bot, "aim", return_value=(0, 0, 0.04, 0.08)),
              patch.object(bot, "actual_ray_hits", return_value=True)):
            bot.attack(me, {}, action)
        self.assertEqual(action["fire"], [True, False])

    def test_actual_gun_ray_accounts_for_moving_cylinder_and_cover(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        me.update(pos=[0, 0, 0], vel=[0, 18, 0], aim=[0, 0])
        gun = me["weapons"][0]
        enemy = {"id": 12, "kind": "mech", "pos": [100, 0, 0], "vel": [0, 15, 0]}
        with patch.object(bot.arena, "clear_shot", return_value=True):
            self.assertFalse(bot.actual_ray_hits(me, enemy, gun))
            me["aim"] = list(bot.aim(me, enemy, gun)[:2])
            self.assertTrue(bot.actual_ray_hits(me, enemy, gun))
            me["vel"] = [0, -18, 0]
            self.assertTrue(bot.actual_ray_hits(me, enemy, gun))
        with patch.object(bot.arena, "clear_shot", return_value=False):
            self.assertFalse(bot.actual_ray_hits(me, enemy, gun))

    def test_near_laser_charge_wins_sound_priority_and_persists(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        obs.update(mechs=[], sounds=[
            {"kind": "step", "dist": "near", "bearing": 0.2, "age": 0},
            {"kind": "laser_charge", "dist": "near", "bearing": 2.1, "age": 0.1},
        ])
        self.assertEqual(bot.act(obs)["aim"], [2.1, 0])
        obs.update(t=0.5, sounds=[])
        self.assertEqual(bot.act(obs)["aim"], [2.1, 0])

    def test_recovery_holds_cover_but_still_respects_zone(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        me["hp"] = me["max_hp"] * 0.45
        x, y, z = me["pos"]
        enemy = {"id": 12, "kind": "mech", "pos": [x - 65, y, z],
                 "hp": 1400, "max_hp": 1400, "aim": [0, 0]}
        obs.update(mechs=[enemy], drops=[], cubes=[], projectiles=[], pickups=[])
        with patch.object(bot.arena, "solid_between", return_value=True):
            action = bot.act(obs)
        self.assertFalse(any(action["fire"]))
        self.assertNotIn("move", action)
        with patch.object(bot.arena, "solid_between", return_value=False):
            self.assertIn("move", bot.act(obs))
        me["in_zone"] = False
        self.assertIn("move", bot.act(obs))

    def test_can_move_to_weapon_while_still_shooting(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        x, y, z = me["pos"]
        me["aim"] = [0, 0]
        pickup = {"id": 8000, "kind": "laser", "pos": [x, y + 52, z]}
        enemy = {"id": 12, "kind": "mech", "pos": [x + 65, y, z],
                 "hp": 1400, "max_hp": 1400, "aim": [0, 0]}
        obs.update(mechs=[enemy], pickups=[pickup], cubes=[], drops=[], projectiles=[])
        with (patch.object(bot.arena, "solid_between", return_value=False),
              patch.object(bot.arena, "walkable", return_value=True),
              patch.object(bot, "actual_ray_hits", return_value=True),
              patch.object(bot, "aim", return_value=(0, 0, 0.04, 0.04))):
            action = bot.act(obs)
            self.assertGreater(action["move"][1], 0.99)
            self.assertEqual(action["fire"], [True, False])
            enemy["pos"] = [x, y + 65, z]
            self.assertIsNone(bot.resource_goal(obs, [pickup], [enemy]))

    def test_scout_keeps_waypoint_while_torso_scans(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        waypoint = bot.scout_goal(obs)
        bot.scan += 2
        obs["t"] += 4
        self.assertEqual(bot.scout_goal(obs), waypoint)
        obs["t"] += 4
        self.assertNotEqual(bot.scout_goal(obs), waypoint)

    def test_kiting_reverses_tangent_without_giving_up_range(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        me["pos"] = [0, 0, 0]
        target = {"pos": [65, 0, 0]}
        a = bot.kite_goal(me, target, PRACTICE, 0)
        b = bot.kite_goal(me, target, PRACTICE, 0.78)
        self.assertAlmostEqual(a[0], b[0])
        self.assertAlmostEqual(a[1], -b[1])

    def test_roof_geometry_finds_clear_entry_and_respects_energy(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        me["pos"] = [-240, -125, bot.arena.ground(-240, -125)]
        roofs = list(bot.arena.roof_entries(me))
        self.assertTrue(roofs)
        self.assertLess(roofs[0].top - me["pos"][2], 40)
        self.assertLess(roofs[0].energy, me["energy"])
        me["energy"] = 15
        self.assertFalse(list(bot.arena.roof_entries(me)))

    def test_roof_escape_jumps_then_climbs_before_crossing(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        me.update(pos=[0, 0, 0], vel=[0, 0, 0])
        obstacle = RoofEntry((0, 0), (12, 0), (19, 0), 15, 60)
        enemies = [{"pos": [-25, 0, 0]}]
        action = {}
        with (patch.object(bot.arena, "roof_entries", return_value=[obstacle]),
              patch.object(bot.arena, "solid_between", return_value=True)):
            self.assertTrue(bot.roof_escape(obs, enemies, action, True, False))
        self.assertTrue(action["jump"])
        me.update(pos=[0, 0, 7], grounded=False)
        action.clear()
        self.assertTrue(bot.roof_escape(obs, enemies, action, True, False))
        self.assertTrue(action["boost"])
        self.assertEqual(action["move"], [0, 0])
        me["pos"][2] = 16
        self.assertTrue(bot.roof_escape(obs, enemies, action, True, False))
        self.assertEqual(action["move"], [1, 0])
        me.update(pos=[19, 0, 15], grounded=True)
        self.assertFalse(bot.roof_escape(obs, enemies, {}, True, False))

    def test_close_but_occluded_enemy_does_not_break_recovery(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        me["hp"] = me["max_hp"] * 0.4
        x, y, z = me["pos"]
        obs["mechs"] = [{"id": 12, "kind": "mech", "pos": [x - 9, y, z], "aim": [0, 0]}]
        with (patch.object(bot.arena, "clear_shot", return_value=False),
              patch.object(bot, "attack", wraps=bot.attack) as attack):
            bot.act(obs)
        self.assertIsNone(attack.call_args.args[1])

    def test_recorded_observations_without_wrapper_fallback(self):
        bot = Bot(self.hello)
        for obs in self.replay:
            if obs["type"] == "obs":
                with self.subTest(tick=obs["tick"]):
                    self.assertEqual(bot.act(obs)["tick"], obs["tick"])


if __name__ == "__main__":
    unittest.main()
