"""Regression checks for the bot's geometry, mode signal and replay handling."""
from copy import deepcopy
import gzip
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from arena_geometry import Prism, RoofEntry, cells
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

    def test_only_unexplained_early_death_changes_mode(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        obs.update(alive=15, t=10, events=[{"type": "kill", "target": 8}], beams=[])
        bot.update_situation(obs, [])
        self.assertFalse(bot.pvp)
        obs.update(alive=14, t=20, events=[])
        bot.update_situation(obs, [])
        self.assertTrue(bot.pvp)

    def test_early_bullet_cpa_and_mortar_landing_priority(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        me.update(pos=[0, 0, 0], vel=[0, 0, 0])
        bullet = {"kind": "gun", "pos": [-165, me["stats"]["radius"] + 0.9, 4],
                  "vel": [252, 0, 0]}
        obs["projectiles"] = [bullet]
        self.assertIsNotNone(bot.projectile_threat(obs))
        shell = {"kind": "mortar", "pos": [-70, 0, 24.75], "vel": [48, 0, 0]}
        obs["projectiles"].append(shell)
        with patch.object(bot.arena, "ground", return_value=0):
            impact = bot.mortar_impact(shell, 22)
            self.assertAlmostEqual(impact[0], 1.5, places=2)
            self.assertAlmostEqual(abs(bot.projectile_threat(obs)), math.pi)

    def test_weave_alternates_short_and_long_intervals(self):
        bot = Bot(self.hello)
        bot.weave_offset = 0
        self.assertEqual(bot.weave_side(0.44), bot.turn_side)
        self.assertEqual(bot.weave_side(0.46), -bot.turn_side)
        self.assertEqual(bot.weave_side(1.04), -bot.turn_side)
        self.assertEqual(bot.weave_side(1.06), bot.turn_side)

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
        self.assertGreater(bot.kite_goal(me, {"pos": [32, 0, 0]}, PRACTICE, zone_radius=90)[0], 0)

    def test_fires_both_slots_when_their_aim_overlaps(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        stats = self.hello["config"]["weapons"]["lightning"]
        me["weapons"][1] = {"kind": "lightning", "stats": stats, "cooldown": 0}
        me["aim"] = [0, 0]
        action = {}
        with patch.object(bot, "aim", return_value=(0, 0, 0.04, 0.08)):
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
        with patch.object(bot, "aim", side_effect=[(0, 0, 0.04, 0.04), (0, 0.06, 0.04, 0.04)]):
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
              patch.object(bot, "laser_ray_hits", return_value=True)):
            bot.attack(me, {}, action)
        self.assertEqual(action["fire"], [True, False])

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
        obs.update(t=70, mechs=[enemy], pickups=[pickup], cubes=[], drops=[], projectiles=[])
        obs["zone"].update(shrink_start=140, shrink_end=180)
        with (patch.object(bot.arena, "solid_between", return_value=False),
              patch.object(bot.arena, "walkable", return_value=True),
              patch.object(bot, "aim", return_value=(0, 0, 0.04, 0.04))):
            action = bot.act(obs)
            self.assertGreater(action["move"][1], 0.99)
            self.assertEqual(action["fire"], [True, False])
            enemy["pos"] = [x, y + 65, z]
            self.assertIsNone(bot.resource_goal(obs, [pickup], [enemy]))

    def test_opening_runs_to_laser_and_prefers_a_crate_to_a_distant_mech(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        x, y, z = me["pos"]
        laser = {"id": 8000, "kind": "laser", "pos": [x, y + 58, z]}
        lightning = {"id": 8001, "kind": "lightning", "pos": [x, y + 29, z]}
        crate = {"id": 1000, "pos": [x, y + 24, z], "hp": 125}
        enemy = {"id": 12, "kind": "mech", "pos": [x + 66, y, z], "aim": [0, 0]}
        obs.update(t=10, pickups=[lightning, laser], crates=[crate],
                   mechs=[enemy], sounds=[], projectiles=[])
        with (patch.object(bot.arena, "solid_between", return_value=False),
              patch.object(bot.arena, "walkable", return_value=True),
              patch.object(bot, "attack") as attack,
              patch.object(bot, "move", wraps=bot.move) as move):
            action = bot.act(obs)
            self.assertEqual(move.call_args.args[1], tuple(laser["pos"][:2]))
            self.assertEqual(attack.call_args.args[1], crate)
            self.assertTrue(action["boost"])
            crate["hp"] = 300
            bot.act(obs)
            self.assertEqual(attack.call_args.args[1], enemy)
            enemy["pos"] = [x + 50, y, z]
            bot.act(obs)
            self.assertEqual(attack.call_args.args[1], enemy)

    def test_midgame_farms_with_far_enemy_and_prioritizes_nearby_laser(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        x, y, z = me["pos"]
        obs["zone"].update(center=[x, y], next_center=[x, y], radius=400, next_radius=320)
        foe = {"id": 12, "kind": "mech", "pos": [x + 160, y, z], "hp": 1400, "aim": [0, 0]}
        crate = {"id": 1001, "pos": [x + 20, y + 3, z], "hp": 300}
        obs.update(t=100, mechs=[foe], crates=[crate], cubes=[], drops=[], pickups=[],
                   sounds=[], projectiles=[], events=[], beams=[])
        with (patch.object(bot.arena, "clear_shot", return_value=True),
              patch.object(bot.arena, "walkable", return_value=True),
              patch.object(bot, "attack") as attack):
            bot.act(obs)
            self.assertIs(attack.call_args.args[1], crate)
            me["cubes"] = 10
            bot.act(obs)
            self.assertIs(attack.call_args.args[1], foe)
            foe["pos"][0] = x + 85
            obs["pickups"] = [{"id": 8001, "kind": "laser", "pos": [x, y - 25, z]}]
            self.assertLess(bot.act(obs)["move"][1], -0.9)

    def test_big_circle_keeps_range_with_laser_or_underarmed_gun(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        me["pos"] = [0, 0, 0]
        charged_enemy = {"pos": [90, 0, 0], "hp": 1000, "weapons": ["lightning"]}
        self.assertLess(bot.kite_goal(me, charged_enemy, PRACTICE)[0], 0)
        weapons = self.hello["config"]["weapons"]
        me["weapons"] = [{"kind": k, "stats": weapons[k]} for k in ("laser", "lightning")]
        self.assertLess(bot.kite_goal(me, {"pos": [105, 0, 0]}, PRACTICE)[0], 0)
        self.assertGreater(bot.kite_goal(me, {"pos": [135, 0, 0]}, PRACTICE)[0], 0)
        self.assertGreater(bot.kite_goal(me, {"pos": [175, 0, 0]}, PRACTICE)[0], 0)
        self.assertEqual(max(bot.weapon_order(90), key=bot.weapon_order(90).get), "lightning")

    def test_scout_keeps_waypoint_while_torso_scans(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        waypoint = bot.scout_goal(obs)
        bot.scan += 2
        obs["t"] += 4
        self.assertEqual(bot.scout_goal(obs), waypoint)
        obs["t"] += 4
        self.assertNotEqual(bot.scout_goal(obs), waypoint)

    def test_orbit_at_outer_edge_preserves_direction_for_six_seconds(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        obs["self"]["pos"] = [-184, 0, 0]
        obs["zone"].update(center=[0, 0], next_center=[0, 0], radius=200, next_radius=200)
        obs["t"] = 80
        enemy = {"pos": [-90, 0, 0]}
        with patch.object(bot.arena, "walkable", side_effect=lambda me, yaw, reach: math.sin(yaw) > 0):
            goal = bot.orbit_goal(obs, (-224, 0), [enemy], 0)
        self.assertGreater(goal[1], 30)
        self.assertEqual(bot.safe_destination(obs, goal), goal)
        with patch.object(bot.arena, "walkable", side_effect=lambda me, yaw, reach: math.sin(yaw) < 0):
            obs["t"] = 85.9
            self.assertEqual(bot.orbit_goal(obs, (-224, 0), [enemy], 0), goal)
            obs["t"] = 86
            self.assertLess(bot.orbit_goal(obs, (-224, 0), [enemy], 0)[1], -30)

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

    def test_enemy_within_16_metres_prevents_cover_hold(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        me["hp"] = me["max_hp"] * 0.4
        x, y, z = me["pos"]
        obs["mechs"] = [{"id": 12, "kind": "mech", "pos": [x - 9, y, z], "aim": [0, 0]}]
        with (patch.object(bot.arena, "clear_shot", return_value=False),
              patch.object(bot, "attack", wraps=bot.attack) as attack):
            action = bot.act(obs)
        self.assertIsNotNone(attack.call_args.args[1])
        self.assertEqual(action["fire"], [False, False])

    def test_blocked_enemy_at_38_metres_keeps_pillar_step_silent(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        me.update(hp=me["max_hp"] * 0.4, pos=[-15, 5, 0], grounded=True, energy=100)
        obs["zone"].update(center=[0, 0], next_center=[0, 0], radius=400, next_radius=320)
        enemy = {"id": 12, "kind": "mech", "pos": [23, 4, 0], "aim": [0, 0]}
        prism = Prism({"kind": "building", "base": -1, "top": 18,
                       "ring": [-8, -9, 8, -9, 8, 9, -8, 9], "holes": []})
        bins = {cell: [prism] for cell in cells(*prism.bounds)}
        obs.update(t=95, drops=[], projectiles=[], beams=[], sounds=[], mechs=[enemy],
                   events=[{"type": "damaged", "bearing": 0}])
        bot.uncovered_since = 90
        with (patch.object(bot.arena, "bins", bins),
              patch.object(bot.arena, "ground", return_value=0),
              patch.object(bot, "attack", wraps=bot.attack) as attack):
            action = bot.act(obs)
        self.assertIsNone(bot.uncovered_since)
        self.assertIsNone(attack.call_args.args[1])
        self.assertEqual(action["fire"], [False, False])
        self.assertFalse(action["boost"])
        self.assertLess(action["move"][1], -0.8)

    def test_full_laser_waits_for_visible_ray_but_releases_after_grace_or_near_lightning(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        laser = self.hello["config"]["weapons"]["laser"]
        me["weapons"] = [None, {"kind": "laser", "stats": laser, "cooldown": 0, "charge": laser["charge"]}]
        action = {}
        with (patch.object(bot, "laser_ray_hits", return_value=False),
              patch.object(bot, "aim", return_value=None)):
            bot.frame_time = 10
            bot.attack(me, {}, action)
            self.assertEqual(action["fire"], [False, True])
            bot.frame_time = 11.8
            bot.attack(me, {}, action)
            self.assertEqual(action["fire"], [False, False])
            bot.frame_time = 10.5
            bot.laser_unsafe = True
            bot.attack(me, {}, action)
            self.assertEqual(action["fire"], [False, False])

    def test_empty_slot_accepts_mortar_and_open_circle_keeps_gun_with_laser(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        pos = me["pos"]
        obs["pickups"] = [{"id": 200, "kind": "mortar", "pos": pos}]
        action = {}
        bot.loot(obs, [], action, opening=True)
        self.assertEqual(action["take"], 200)
        me["weapons"][1] = {"kind": "laser"}
        obs["pickups"] = [{"id": 201, "kind": "lightning", "pos": pos}]
        action = {}
        bot.loot(obs, [], action, opening=True)
        self.assertNotIn("take", action)
        bot.loot(obs, [{"pos": [pos[0] + 45, pos[1], pos[2]]}], action)
        self.assertEqual((action["take"], action["drop"]), (201, 0))

    def test_execute_closes_range_but_checks_second_healthy_mech(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        me["pos"] = [0, 0, 0]
        weapon = self.hello["config"]["weapons"]["laser"]
        me["weapons"][1] = {"kind": "laser", "stats": weapon}
        target = {"id": 12, "kind": "mech", "pos": [70, 0, 0], "hp": 500}
        self.assertTrue(bot.can_execute(me, target, [target]))
        self.assertGreater(bot.kite_goal(me, target, PRACTICE, execute=True)[0], 0)
        self.assertLess(bot.kite_goal(me, target, PRACTICE)[0], 0)
        self.assertFalse(bot.can_execute(me, target, [target, {"id": 13, "pos": [90, 0, 0], "hp": 900}]))

    def test_sound_selects_nearest_band_then_highest_threat_and_retains_bearing(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        obs.update(mechs=[], sounds=[
            {"kind": "laser_charge", "dist": "mid", "bearing": 1.1, "age": 0},
            {"kind": "step", "dist": "near", "bearing": 0.2, "age": 0},
            {"kind": "gun", "dist": "near", "bearing": -2, "age": 0.1},
        ])
        self.assertEqual(bot.act(obs)["aim"], [-2, 0])
        obs.update(t=0.4, sounds=[])
        self.assertEqual(bot.act(obs)["aim"], [-2, 0])
        obs["t"] = 1
        self.assertIsNone(bot.threat_sound(obs))

    def test_upgrade_priority_prefers_armor_then_held_weapon_before_mobility(self):
        bot = Bot(self.hello)
        me = self.replay[0]["self"]
        offer = {"choices": [{"id": key} for key in ("servos", "gun_damage", "armor")]}
        self.assertEqual(bot.choose_upgrade(me, offer), 2)
        offer["choices"].pop()
        self.assertEqual(bot.choose_upgrade(me, offer), 1)

    def test_cover_peek_precharges_then_crosses_and_returns(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        x, y, z = me["pos"]
        weapon = {"kind": "laser", "stats": self.hello["config"]["weapons"]["laser"],
                  "charge": 0, "cooldown": 0}
        me["weapons"] = [None, weapon]
        target = {"id": 12, "kind": "mech", "pos": [x + 105, y, z]}
        with (patch.object(bot.arena, "solid_between", return_value=True),
              patch.object(bot.arena, "ground", return_value=z),
              patch.object(bot.arena, "walkable", return_value=True),
              patch.object(bot, "aim", return_value=(0, 0, 0.03, 0.03))):
            start = bot.cover_peek(obs, target)
        self.assertEqual(start[0], (x, y))
        with patch.object(bot, "aim", return_value=None):
            action = {}
            bot.attack(me, target, action)
            self.assertTrue(action["fire"][1])
        obs["t"] = 0.4
        weapon["charge"] = 0.4
        exposed = bot.cover_peek(obs, target)[0]
        self.assertNotEqual(exposed, (x, y))
        me["pos"] = [*exposed, z]
        obs["t"] = 1.4
        weapon["charge"] = 0
        weapon["cooldown"] = 1.65
        self.assertEqual(bot.cover_peek(obs, target)[0], (x, y))

    def test_supply_requires_clear_ring_and_approaches_contest_from_outside(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        x, y, z = obs["self"]["pos"]
        drop = {"id": 20, "pos": [x + 50, y, z], "radius": 8, "lands_in": 8, "holder": None}
        obs["drops"] = [drop]
        self.assertIsNone(bot.choose_supply(obs, []))
        drop["lands_in"] = 7.9
        self.assertEqual(bot.choose_supply(obs, []), (drop, None, drop["pos"][:2]))
        foe = {"id": 12, "pos": [x + 126, y, z]}
        with patch.object(bot.arena, "solid_between", return_value=True):
            self.assertEqual(bot.choose_supply(obs, [foe]), (drop, None, drop["pos"][:2]))
        with patch.object(bot.arena, "solid_between", return_value=False):
            self.assertIsNone(bot.choose_supply(obs, [foe]))
            obs["self"]["pos"] = drop["pos"][:]
            _, contest, goal = bot.choose_supply(obs, [foe])
            self.assertEqual(contest, foe)
            self.assertGreater(math.dist(goal, drop["pos"][:2]), drop["radius"])

    def test_quick_finish_and_covered_recovery_override_supply(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        x, y, z = obs["self"]["pos"]
        obs["zone"].update(center=[x, y], next_center=[x, y], radius=400, next_radius=320)
        obs.update(t=95, projectiles=[], beams=[], events=[], sounds=[], mechs=[
            {"id": 12, "kind": "mech", "pos": [x + 55, y, z], "hp": 200, "aim": [0, 0]},
        ])
        drop = {"id": 20, "pos": [x, y - 75, z], "radius": 8, "lands_in": 0, "holder": None}
        with (patch.object(bot, "choose_supply", return_value=(drop, None, drop["pos"][:2])) as supply,
              patch.object(bot.arena, "walkable", return_value=True),
              patch.object(bot.arena, "clear_shot", return_value=True)):
            action = bot.act(obs)
            self.assertGreater(action["move"][0], 0)
            obs["self"]["hp"] = 450
            with patch.object(bot, "covered_for_recovery", return_value=True):
                self.assertEqual(bot.act(obs)["fire"], [False, False])
            supply.assert_not_called()

    def test_late_charge_takes_turret_from_moving_targets_gun_lead(self):
        bot = Bot(self.hello)
        me = deepcopy(self.replay[0]["self"])
        me.update(pos=[0, 0, 0], aim=[0.045, 0])
        target = {"id": 12, "kind": "mech", "pos": [120, 0, 0], "vel": [0, 11.7, 0]}
        stats = self.hello["config"]["weapons"]["laser"]
        laser = {"kind": "laser", "stats": stats, "charge": 0.55, "cooldown": 1}
        me["weapons"][1] = laser
        with patch.object(bot.arena, "clear_shot", return_value=True):
            action = {}
            bot.attack(me, target, action)
            self.assertAlmostEqual(action["aim"][0], 0)
            self.assertEqual(action["fire"], [False, True])
            laser["charge"] = stats["charge"]
            me["aim"] = action["aim"]
            bot.attack(me, target, action)
            self.assertFalse(action["fire"][1])
            laser["charge"] = 0
            bot.attack(me, target, action)
            self.assertGreater(action["aim"][0], 0.03)

    def test_focus_persists_and_multi_enemy_corner_must_block_other_shooter(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        me["pos"] = [0, 0, 0]
        target = {"id": 12, "kind": "mech", "pos": [90, 0, 0], "hp": 250}
        other = {"id": 13, "kind": "mech", "pos": [50, 0, 0], "hp": 500}
        with patch.object(bot.arena, "clear_shot", return_value=True):
            self.assertIs(bot.select_target(me, [other, target]), target)
            other["hp"] = 240
            self.assertIs(bot.select_target(me, [other, target]), target)
        with (patch.object(bot, "safe_destination", side_effect=lambda obs, point: point),
              patch.object(bot.arena, "walkable", return_value=True),
              patch.object(bot.arena, "ground", return_value=0),
              patch.object(bot.arena, "clear_shot", return_value=True),
              patch.object(bot.arena, "solid_between", side_effect=lambda start, end: end[1] < -3)):
            goal, _ = bot.corner_position(obs, target, [other], me["weapons"][0])
            self.assertLess(goal[1], -3)

    def test_mortar_drops_without_swap_and_cube_detours_stop_at_cap(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        me["weapons"][1] = {"kind": "mortar"}
        obs["pickups"] = [{"id": 200, "kind": "laser", "pos": me["pos"]}]
        action = {}
        bot.loot(obs, [{"pos": me["pos"], "weapons": ["laser"]}], action)
        self.assertEqual(action, {"drop": 1})
        for count, reach in ((9, 42), (10, 0), (14, 10), (15, 0), (20, 0)):
            me["cubes"] = count
            self.assertEqual(bot.cube_reach(me, 42), reach)

    def test_retreat_keeps_front_toward_recent_shooter_and_bypasses_roof_transfer(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        x, y, z = obs["self"]["pos"]
        obs["self"]["hp"] = 500
        obs["zone"].update(center=[x, y], next_center=[x, y], radius=400, next_radius=320)
        obs.update(t=95, drops=[], projectiles=[], beams=[], sounds=[], events=[
            {"type": "damaged", "bearing": math.pi},
        ], mechs=[
            {"id": 12, "kind": "mech", "pos": [x + 65, y, z], "hp": 1000, "aim": [0, 0]},
        ])
        with (patch.object(bot.arena, "walkable", return_value=True),
              patch.object(bot, "attack", side_effect=lambda me, target, action: action.update(
                  aim=[0, 0], fire=[True, False])),
              patch.object(bot, "roof_escape") as roof):
            action = bot.act(obs)
        self.assertEqual(action["aim"], [math.pi, 0])
        self.assertEqual(action["fire"], [False, False])
        roof.assert_not_called()

    def test_stalled_recovery_boosts_to_cover_then_fights_a_close_pursuer(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        x, y, z = obs["self"]["pos"]
        obs["self"].update(hp=500, energy=60, grounded=True)
        obs["zone"].update(center=[x, y], next_center=[x, y], radius=400, next_radius=320)
        foe = {"id": 12, "kind": "mech", "pos": [x + 60, y, z], "hp": 1000, "aim": [0, 0]}
        obs.update(t=95, drops=[], projectiles=[], beams=[], sounds=[], events=[], mechs=[foe])
        with (patch.object(bot, "covered_for_recovery", return_value=False),
              patch.object(bot, "recovery_exposed", return_value=True),
              patch.object(bot, "recovery_cover", return_value=(x - 22, y)) as cover,
              patch.object(bot.arena, "walkable", return_value=True)):
            bot.act(obs)
            cover.assert_not_called()
            obs["t"] = 97.5
            action = bot.act(obs)
            self.assertTrue(action["boost"])
            self.assertLess(action["move"][0], -0.9)
            foe["pos"][0] = x + 40
            action = bot.act(obs)
            self.assertFalse(action["boost"])
            self.assertGreater(abs(action["move"][1]), 0.9)
            cover.assert_called_once()

    def test_stalled_recovery_counterattack_uses_exposed_pursuer(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        x, y, z = obs["self"]["pos"]
        obs["self"].update(hp=500, energy=60, grounded=True)
        obs["zone"].update(center=[x, y], next_center=[x, y], radius=400, next_radius=320)
        blocked = {"id": 12, "kind": "mech", "pos": [x + 30, y, z], "hp": 200, "aim": [0, 0]}
        exposed = {"id": 13, "kind": "mech", "pos": [x, y + 40, z], "hp": 1000, "aim": [0, 0]}
        obs.update(t=95, drops=[], projectiles=[], beams=[], sounds=[], events=[], mechs=[blocked, exposed])
        bot.uncovered_since = 92
        with (patch.object(bot, "recovery_exposed", side_effect=lambda me, e: e is exposed),
              patch.object(bot.arena, "walkable", return_value=True),
              patch.object(bot, "attack") as attack):
            action = bot.act(obs)
        self.assertIs(attack.call_args.args[1], exposed)
        self.assertGreater(abs(action["move"][0]), 0.9)

    def test_drop_route_and_armor_pick_continue_when_aim_follows_sound(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        x, y, z = obs["self"]["pos"]
        obs["zone"].update(center=[x, y], next_center=[x, y], radius=400, next_radius=320)
        obs.update(t=88, mechs=[], projectiles=[], drops=[
            {"id": 20, "pos": [x + 50, y, z], "radius": 8, "lands_in": 7, "holder": None},
        ], sounds=[
            {"kind": "laser_charge", "dist": "near", "bearing": 2.2, "age": 0},
        ], offer={"choices": [{"id": "gun_damage"}, {"id": "armor"}]})
        with patch.object(bot.arena, "walkable", return_value=True):
            action = bot.act(obs)
        self.assertEqual(action["pick"], 1)
        self.assertGreater(action["move"][0], 0.9)
        self.assertEqual(action["aim"], [2.2, 0])

    def test_laser_hold_expires_across_act_calls_without_scouting(self):
        bot = Bot(self.hello)
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        x, y, z = me["pos"]
        stats = self.hello["config"]["weapons"]["laser"]
        me["weapons"][1] = {"kind": "laser", "stats": stats, "charge": stats["charge"], "cooldown": 0}
        obs["zone"].update(center=[x, y], next_center=[x, y], radius=400, next_radius=320)
        obs.update(t=90, projectiles=[], mechs=[
            {"id": 12, "kind": "mech", "pos": [x + 110, y, z], "hp": 1000, "aim": [0, 0]},
        ])
        with (patch.object(bot.arena, "walkable", return_value=True),
              patch.object(bot.arena, "solid_between", return_value=False),
              patch.object(bot, "laser_ray_hits", return_value=False),
              patch.object(bot, "aim", return_value=None)):
            self.assertTrue(bot.act(obs)["fire"][1])
            obs["t"] = 91.8
            self.assertFalse(bot.act(obs)["fire"][1])

    def test_recorded_observations_without_wrapper_fallback(self):
        bot = Bot(self.hello)
        for obs in self.replay:
            if obs["type"] == "obs":
                with self.subTest(tick=obs["tick"]):
                    self.assertEqual(bot.act(obs)["tick"], obs["tick"])


if __name__ == "__main__":
    unittest.main()
