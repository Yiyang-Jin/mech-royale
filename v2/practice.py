"""Practice controls and parameter comparisons."""
from copy import copy
from dataclasses import replace

from practice_bot import LaserCoverBot
from probe_bot import CloseFightProbe
from strategy_bot import Bot


VARIANTS = ("p406", "retreat56", "range92-retreat45", "laser-cover")


class PracticeBot(LaserCoverBot):
    def __init__(self, hello, world, routes, variant):
        Bot.__init__(self, hello)
        self.static_routes = copy(routes)
        self.static_routes.path = copy(routes.path)
        self.world = world
        self.variant = variant
        self.gun_target = None
        self.enable_laser_cover()
        distance, retreat = {"retreat56": (78, .56), "range92-retreat45": (92, .45)}.get(variant, (78, .38))
        self.practice = replace(self.practice, laser_distance=distance, retreat_hp=retreat)

    def act(self, obs):
        action = LaserCoverBot.act(self, obs) if self.variant == "laser-cover" else CloseFightProbe.act(self, obs)
        if not any(weapon and weapon["kind"] == "gun" and fire
                   for weapon, fire in zip(obs["self"]["weapons"], action.get("fire", ()))):
            self.gun_target = None
        return action

    def attack(self, me, target, action):
        self.gun_target = target["id"] if target and target.get("kind") == "mech" else None
        super().attack(me, target, action)
