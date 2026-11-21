"""Checks for the Practice wave and mode boundaries."""
import gzip
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from .arena_sim import Simulation
from .opponents import PRACTICE_TIERS, SquadDirector


class ArenaSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with gzip.open("check/stream.jsonl.gz", "rt", encoding="utf-8") as stream:
            cls.hello = json.loads(next(stream))
        cls.hello["map"]["dir"] = str(Path("arena").resolve())

    def simulation(self, mode):
        sim = Simulation(self.hello, Path("arena"), 17, mode, "p406", 2)
        self.addCleanup(sim.world.close)
        return sim

    def test_director_wave_boundaries(self):
        director = SquadDirector()
        for now, kills, want in [
            (44.9, 0, 1), (45., 0, 2), (0., 2, 2), (119.9, 0, 2),
            (120., 0, 3), (0., 3, 3), (0., 5, 4), (190., 0, 5),
            (0., 8, 6), (249.9, 9, 6), (250., 0, 15),
        ]:
            with self.subTest(now=now, kills=kills):
                self.assertEqual(director.limit(now, kills), want)

    def test_practice_shares_vision_and_relaxes_only_navigation_after_expiry(self):
        sim = self.simulation("practice")
        player, *squad = sim.mecks
        self.assertEqual(tuple(m.role for m in squad), PRACTICE_TIERS)
        with patch.object(sim, "visible", side_effect=lambda m, _: m is squad[-1]):
            active, lock, _ = sim.director.update(sim, squad, player)
        self.assertEqual(active, {1})
        self.assertIs(lock, player)
        with patch.object(sim, "visible", return_value=False):
            sim.t = 5
            self.assertIs(sim.director.update(sim, squad, player)[1], player)
            sim.t = 5.1
            active, lock, _ = sim.director.update(sim, squad, player)
            self.assertEqual(active, {1})
            self.assertIsNone(lock)
        sim.active = active
        self.assertEqual([m.id for m in sim.shootable(1) if hasattr(m, "state")], [0])
        self.assertFalse(any(hasattr(m, "state") for m in sim.shootable(2)))
        sim.t = 250
        squad[8].hp = .3 * squad[8].max_hp
        active, _, _ = sim.director.update(sim, squad, player)
        self.assertEqual(len(active), 15)
        self.assertTrue(squad[8].brain.relief)
        self.assertEqual(set(sim.director.roles.values()), {-1, 0, 1})

    def test_practice_player_retains_frozen_ground_controller(self):
        sim = self.simulation("practice")
        player = sim.mecks[0]
        self.assertEqual(player.controller.practice.laser_distance, 78)
        self.assertEqual(player.controller.practice.retreat_hp, .38)
        self.assertEqual(len(player.controller.act(sim.obs(player, sim.mecks, sim.zone()))["fire"]), 2)

    def test_non_practice_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "only Practice"):
            self.simulation("finals-ffa")
