"""Run the PvE controller with the measured short-range laser cover response."""
import math

from mechfight import run
from probe_bot import CloseFightProbe
from strategy_bot import angle_error, bearing, distance
from v2.native import State, Stats, World, Zone


class LaserCoverBot(CloseFightProbe):
    def __init__(self, hello):
        super().__init__(hello)
        self.world = World(hello["map"]["dir"], self.config["mech"])
        self.enable_laser_cover()

    def enable_laser_cover(self):
        self.cover_until = -math.inf
        self.cover_control = None
        self.plugin_calls = 0

    def act(self, obs):
        action = super().act(obs)
        self.apply_laser_cover(obs, action)
        return action

    def native_zone(self, obs):
        zone = obs["zone"]
        return Zone(*zone["center"][:2], zone["radius"], 0.)

    def apply_laser_cover(self, obs, action):
        me, now = obs["self"], obs["t"]
        if not me["grounded"] or not me["in_zone"] or action.get("jump"):
            return
        if now < self.cover_until:
            control = self.cover_control
        else:
            reach = self.config["weapons"]["laser"]["range"]
            attackers = [e for e in obs["mechs"] if e["kind"] == "mech" and e["charging"]
                         and distance(e["pos"], me["pos"]) <= reach
                         and abs(angle_error(bearing(e["pos"], me["pos"]), e["aim"][0])) < .45]
            if not attackers or self.projectile_threat(obs) is not None:
                return
            shooter = min(attackers, key=lambda enemy: distance(enemy["pos"], me["pos"]))
            source = self.mech_point(shooter, self.config["mech"]["muzzle_height"])
            scale = me["stats"]["charge_move_mult"] if any(
                weapon and weapon.get("charge", 0) > 0 for weapon in me["weapons"]) else 1.
            control = self.world.practice_laser_cover(
                State.from_obs(me, 10), Stats.from_dict(me["stats"]), self.native_zone(obs),
                source, self.physics_step, scale, False)
            if control is None:
                return
            self.cover_control = control
            self.cover_until = now + .4
            self.plugin_calls += 1
        action.update(move=[control.x, control.y], boost=bool(control.boost), jump=False, fire=[False, False])


if __name__ == "__main__":
    run(LaserCoverBot)
