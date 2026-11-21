"""Regression checks for the bot's geometry, mode signal and replay handling."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from arena_geometry import Arena, Prism
from ground_navigation import Crossing, Influence, NavGraph, jet_energy
from strategy_bot import Bot, PRACTICE, intercept


ROOT = Path(__file__).resolve().parent


class StrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with gzip.open(ROOT / "check/stream.jsonl.gz", "rt", encoding="utf-8") as stream:
            cls.hello = json.loads(next(stream))
            cls.replay = [json.loads(line) for line in stream]
        cls.hello["map"]["dir"] = str(ROOT / "arena")
        cls.graph = NavGraph(Arena(ROOT / "arena"), cls.hello["config"]["mech"])

    def make_bot(self):
        with patch("ground_navigation.NavGraph", return_value=self.graph):
            return Bot(self.hello)

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

    def test_early_deaths_and_beams_do_not_change_practice_mode(self):
        bot = self.make_bot()
        obs = deepcopy(self.replay[0])
        x, y, z = obs["self"]["pos"]
        target = [x - 65, y, z]
        obs.update(alive=13, t=20, events=[],
                   mechs=[{"id": 12, "kind": "mech", "pos": target, "aim": [0, 0]}],
                   beams=[{"by": 11, "kind": "laser", "path": [[x, y, z], target], "age": 0}])
        bot.act(obs)
        self.assertFalse(bot.pvp)

    def test_projectile_approach_and_height(self):
        bot = self.make_bot()
        obs = deepcopy(self.replay[0])
        obs["self"].update(pos=[0, 0, 0], vel=[0, 0, 0])
        obs["projectiles"] = [{"pos": [45, 1, 5], "vel": [-200, 0, 0], "kind": "gun"}]
        with patch.object(bot.arena, "walkable", return_value=True):
            field = Influence(bot.arena, bot.config, obs, [])
            direction = bot.navigation.emergency_direction(obs, field)
            self.assertIsNotNone(direction)
            self.assertGreater(abs(direction[1]), 6)
            obs["projectiles"][0]["pos"][2] = 35
            self.assertIsNone(bot.navigation.emergency_direction(obs, field))

    def test_kiting_uses_weapon_range(self):
        bot = self.make_bot()
        me = deepcopy(self.replay[0]["self"])
        me["pos"] = [0, 0, 0]
        enemy = {"pos": [32, 0, 0]}
        self.assertEqual(bot.ideal_range(me, enemy, PRACTICE), 72)
        stats = self.hello["config"]["weapons"]["lightning"]
        me["weapons"][1] = {"kind": "lightning", "stats": stats, "cooldown": 0}
        self.assertEqual(bot.ideal_range(me, enemy, PRACTICE), 29)
        enemy["weapons"] = ["lightning"]
        self.assertEqual(bot.ideal_range(me, enemy, PRACTICE), 55)

    def test_fires_both_slots_when_their_aim_overlaps(self):
        bot = self.make_bot()
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
        bot = self.make_bot()
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
        bot = self.make_bot()
        me = deepcopy(self.replay[0]["self"])
        laser = self.hello["config"]["weapons"]["laser"]
        me["weapons"][1] = {"kind": "laser", "stats": laser, "cooldown": 0, "charge": laser["charge"]}
        me["aim"] = [0, 0]
        action = {}
        with patch.object(bot, "aim", return_value=(0, 0, 0.04, 0.08)):
            bot.attack(me, {}, action)
        self.assertEqual(action["fire"], [True, False])

    def test_laser_releases_at_full_charge_and_when_target_disappears(self):
        bot = self.make_bot()
        me = deepcopy(self.replay[0]["self"])
        stats = self.hello["config"]["weapons"]["laser"]
        laser = {"kind": "laser", "stats": stats, "cooldown": 0, "charge": stats["charge"]}
        me["weapons"] = [None, laser]
        me["aim"] = [0, 0]
        action = {}
        with patch.object(bot, "aim", return_value=(0.2, 0, 0.03, 0.05)):
            bot.attack(me, {}, action)
            self.assertEqual(action["fire"], [False, False])
            laser["charge"] = stats["charge"] / 2
            bot.attack(me, {}, action)
            self.assertEqual(action["fire"], [False, True])
        with patch.object(bot, "aim", return_value=None):
            bot.attack(me, {}, action)
            self.assertEqual(action["fire"], [False, False])

    def test_ground_boost_reserves_energy_except_at_close_range(self):
        bot = self.make_bot()
        me = deepcopy(self.replay[0]["self"])
        me["energy"] = 66
        with patch.object(bot.arena, "walkable", return_value=True):
            self.assertFalse(bot.navigation.drive(me, (0, 0), sprint=True)["boost"])
            self.assertTrue(bot.navigation.drive(me, (0, 0), sprint=True, emergency=True)["boost"])
            me["energy"] = 70
            self.assertTrue(bot.navigation.drive(me, (0, 0), sprint=True)["boost"])

    def test_precomputed_graph_routes_out_of_a_u_shaped_wall(self):
        def wall(left, bottom, right, top):
            return Prism({"base": 0, "top": 15, "holes": [],
                          "ring": [left, bottom, right, bottom, right, top, left, top]})

        arena = SimpleNamespace(
            bounds={"minX": -64, "maxX": 64, "minY": -64, "maxY": 64},
            ground=lambda x, y: 0,
            prisms=[wall(-14, -14, -10, 28), wall(10, -14, 14, 28), wall(-14, -14, 14, -10)],
        )
        graph = NavGraph(arena, self.hello["config"]["mech"])
        bot = self.make_bot()
        bot.navigation.graph = graph
        obs = deepcopy(self.replay[0])
        obs["self"]["pos"] = [0, 0, 0]
        start, finish = (0, 0), (0, -32)
        influence = Influence(arena, bot.config, obs, [])
        path, node = bot.navigation.search(graph.nearest(start, False), obs, influence,
                                           "travel", finish, None, None)
        self.assertEqual(graph.xyz[node][:2], finish)
        self.assertGreater(max(graph.xyz[cell][1] for cell in path), 28)
        off_path = {(0, y) for y in range(-20, -2, 4)}
        self.assertFalse(any(graph.xyz[cell][:2] in off_path for cell in path))

    def test_recovery_holds_cover_but_still_respects_zone(self):
        bot = self.make_bot()
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
        bot = self.make_bot()
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
              patch.object(bot.navigation, "control", return_value=({"move": [0, 1]}, False)) as control,
              patch.object(bot, "aim", return_value=(0, 0, 0.04, 0.04))):
            action = bot.act(obs)
            self.assertEqual(control.call_args.args[2], tuple(pickup["pos"][:2]))
            self.assertGreater(action["move"][1], 0.99)
            self.assertEqual(action["fire"], [True, False])
            enemy["pos"] = [x, y + 65, z]
            self.assertIsNone(bot.resource_goal(obs, [pickup], [enemy]))

    def test_resource_route_prefers_laser_or_lightning_to_mortar(self):
        bot = self.make_bot()
        obs = deepcopy(self.replay[0])
        x, y, z = obs["self"]["pos"]
        mortar = {"kind": "mortar", "pos": [x, y + 20, z]}
        lightning = {"kind": "lightning", "pos": [x, y + 48, z]}
        with patch.object(bot.arena, "solid_between", return_value=False):
            self.assertEqual(bot.resource_goal(obs, [mortar, lightning], []), tuple(lightning["pos"][:2]))

    def test_scout_keeps_waypoint_while_torso_scans(self):
        bot = self.make_bot()
        obs = deepcopy(self.replay[0])
        waypoint = bot.scout_goal(obs)
        bot.scan += 2
        obs["t"] += 4
        self.assertEqual(bot.scout_goal(obs), waypoint)
        obs["t"] += 4
        self.assertNotEqual(bot.scout_goal(obs), waypoint)

    def test_tactical_utility_prefers_cover_and_sticky_target(self):
        bot = self.make_bot()
        nav = bot.navigation
        graph = SimpleNamespace(xyz=[(0, 0, 0), (4, 0, 0), (18, 0, 0)], cover=[0]*3, count=3)
        nav.graph = graph
        obs = deepcopy(self.replay[0])
        influence = SimpleNamespace(
            enemies=[(-55, 0, 0, 25, None, 8)],
            zone=obs["zone"],
            risk=lambda point: 0,
            exposure=lambda point, height, target: (int(point[0] == 0), False),
        )
        cost = {0: 0, 1: 6, 2: 17}
        nav.destination = 1
        chosen = nav.choose([0, 1, 2], cost, 0, obs, influence, "retreat", None, None, {})
        self.assertEqual(chosen, 1)

    def test_tactical_search_routes_around_costly_nodes_within_local_radius(self):
        bot = self.make_bot()
        arena = SimpleNamespace(bounds={"minX": -90, "maxX": 90, "minY": -90, "maxY": 90},
                                ground=lambda x, y: 0, prisms=[])
        nav = bot.navigation
        graph = nav.graph = NavGraph(arena, bot.config["mech"])
        obs = deepcopy(self.replay[0])
        obs["self"]["pos"] = [-12, 0, 0]
        target_node = graph.nearest((12, 0), False)
        field = SimpleNamespace(risk=lambda p: 30 if abs(p[0]) <= 4 and abs(p[1]) <= 8 else 0)
        with patch.object(nav, "choose", return_value=target_node) as choose:
            start = graph.nearest((-12, 0), False)
            route, end = nav.search(start, obs, field, "retreat", (12, 0), None, None)
        self.assertEqual(end, target_node)
        self.assertEqual(route[0], start)
        self.assertTrue(all(field.risk(graph.xyz[node]) == 0 for node in route))
        expanded = choose.call_args.args[0]
        self.assertLessEqual(len(expanded), 720)
        self.assertTrue(all((graph.xyz[node][0] + 12)**2 + graph.xyz[node][1]**2 <= 70**2
                            for node in expanded))

    def test_graph_contains_roofs_and_directed_climb_and_drop_edges(self):
        graph = self.graph
        self.assertTrue(graph.roofs)
        portal = next(iter(graph.portals.values()))
        self.assertLessEqual(portal.rise, 22)
        self.assertIn((portal.roof, portal.crossing + portal.rise * 0.65), graph.edges[portal.ground])
        self.assertIn((portal.ground, portal.crossing + 3), graph.edges[portal.roof])

    def test_portal_rechecks_energy_jumps_and_crosses_only_above_roof(self):
        bot = self.make_bot()
        nav, graph = bot.navigation, self.graph
        obs = deepcopy(self.replay[0])
        me = obs["self"]
        portal = next(p for p in graph.portals.values() if jet_energy(p, me["stats"]) + 15 < 100)
        launch, roof = graph.xyz[portal.ground], graph.xyz[portal.roof]
        me.update(pos=list(launch), vel=[0, 0, 0], energy=15)
        self.assertIsNone(nav.traverse(obs, Crossing(portal, True, 0)))
        me["energy"] = 100
        crossing = Crossing(portal, True, 0)
        self.assertTrue(nav.traverse(obs, crossing)["jump"])
        obs["t"] = 0.2
        me.update(pos=[*launch[:2], roof[2] + 0.5], grounded=False)
        action = nav.traverse(obs, crossing)
        self.assertTrue(action["boost"])
        self.assertEqual(action["move"], [0, 0])
        me["pos"][2] = roof[2] + 1
        action = nav.traverse(obs, crossing)
        self.assertGreater(abs(action["move"][0]) + abs(action["move"][1]), 0.5)
        me.update(pos=list(roof), grounded=True)
        self.assertIsNone(nav.traverse(obs, crossing))
        self.assertFalse(nav.traverse(obs, Crossing(portal, False, 0))["boost"])
        obs["t"] = 1
        me.update(pos=list(launch), grounded=True)
        stalled = Crossing(portal, True, 1)
        self.assertTrue(nav.traverse(obs, stalled)["jump"])
        obs["t"] = 1.6
        self.assertIsNone(nav.traverse(obs, stalled))
        self.assertGreater(nav.banned[portal], obs["t"])

    def test_close_but_occluded_enemy_does_not_break_recovery(self):
        bot = self.make_bot()
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
        bot = self.make_bot()
        for obs in self.replay:
            if obs["type"] == "obs":
                with self.subTest(tick=obs["tick"]):
                    self.assertEqual(bot.act(obs)["tick"], obs["tick"])


if __name__ == "__main__":
    unittest.main()
