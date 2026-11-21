"""Local opponents for the practice squad only."""
from copy import copy
from dataclasses import dataclass, field
import math

from .ballistics import OpponentAim, advance, angle, horizontal, raised, unit


TIERS = ("easy", "medium", "hard", "expert")
PRACTICE_TIERS = ("easy",) * 4 + ("medium",) * 4 + ("hard",) * 4 + ("expert",) * 3
PRACTICE_SEEDS = (17, 29, 43, 59, 71, 83, 97, 109, 127, 139, 151, 167)
DEFAULT_WAVES = (45, 120, 190, 250)


@dataclass
class SquadDirector:
    waves: tuple = DEFAULT_WAVES
    last_seen: float = -math.inf
    roles: dict = field(default_factory=dict)

    def limit(self, now, kills):
        pair, surround, pressure, everyone = self.waves
        if now >= everyone:
            return 15
        if kills >= 6 or now >= pressure:
            return 5 + (kills >= 8)
        if kills >= 3 or now >= surround:
            return 3 + (kills >= 5)
        return 2 if kills >= 1 or now >= pair else 1

    def update(self, sim, hunters, player):
        now = sim.t
        point = raised(player.pos, player.stats["eye_height"])
        if any(sim.visible(m, point) for m in hunters):
            self.last_seen = now
        locked = player if now - self.last_seen <= sim.cfg["vision"]["memory_s"] else None
        limit = self.limit(now, player.kills)
        for m in hunters:
            if m.role in ("hard", "expert"):
                if m.hp < .35 * m.max_hp:
                    m.brain.relief = True
                elif m.hp >= .72 * m.max_hp:
                    m.brain.relief = False
        # Fill slots by tier. A healthy reserve replaces a hurt front-line mech.
        ready = [m for m in hunters if not m.brain.relief]
        active = (ready + [m for m in hunters if m.brain.relief])[:limit]
        hard = [m for m in active if m.role == "hard" and not m.brain.relief]
        self.roles = {m.id: role for m, role in zip(hard, (0, -1, 1, 0))}
        lead = hard[0] if hard else None
        bearing = math.atan2(lead.pos[1] - player.pos[1], lead.pos[0] - player.pos[0]) if lead else 0.
        return {m.id for m in active}, locked, bearing


class Opponent:
    def __init__(self, sim, mech):
        self.sim = sim
        self.mech = mech
        self.route = copy(sim.routes)
        self.route.path = copy(sim.routes.path)
        self.relief = False
        self.climb = None
        self.climb_until = 0.
        self.farm_goal = None
        if mech.role == "medium":
            self.loadout = ("laser", "lightning") if mech.id % 2 else ("lightning", "laser")
        else:
            self.loadout = {"easy": (), "hard": ("mortar", "laser"),
                            "expert": ("laser", "lightning", "mortar")}[mech.role]
        self.scan_at = 0.
        self.fire_model = OpponentAim(sim.cfg, sim.world)

    def steer(self, goal, boost=False, speed=1., simple=False):
        m = self.mech
        if not goal or horizontal(m.pos, goal) < .9:
            return {"move": [0., 0.]}
        if m.state.grounded and not simple:
            goal = self.route.direction_point({"pos": m.pos, "stats": m.stats, "grounded": True}, goal)
        yaw = math.atan2(goal[1] - m.pos[1], goal[0] - m.pos[0])
        return {"move": [speed * math.cos(yaw), speed * math.sin(yaw)], "boost": boost}

    def zone_goal(self, zone):
        sim, m = self.sim, self.mech
        # Go inward early enough to stay ahead of the next shrinking boundary.
        if sim.t >= zone["shrink_start"] - 12:
            center, radius = zone["next_center"], zone["next_radius"]
        else:
            center, radius = zone["center"], zone["radius"]
        return (*center, 0.) if horizontal(m.pos, center) > max(4., radius - 13) else None

    def farm(self, zone, retarget=False):
        sim, m = self.sim, self.mech
        go_in = self.zone_goal(zone)
        if go_in:
            return self.steer(go_in, m.state.energy > 26)
        if self.relief and sim.t - m.damage_clock < m.stats["regen_delay"] + 1:
            return {"move": [0., 0.]}
        def safe(p):
            return horizontal(p, zone["center"]) < zone["radius"] - 7
        if sim.t >= self.scan_at or retarget:
            self.scan_at = sim.t + .6
            choices = []
            good = self.loadout
            current = m.weapons[1]["kind"] if m.weapons[1] else None
            for p in sim.pickups:
                if p["kind"] in good and (current not in good or good.index(p["kind"]) < good.index(current)):
                    choices.append((p, "take", -24 + good.index(p["kind"]) * 12))
            if m.cubes < 8:
                choices.extend((p, "cube", -38) for p in sim.cubes)
                choices.extend((p, "farm", -18 if p.get("kind") == "npc" else 0)
                               for p in (*sim.crates, *sim.drones) if p["hp"] > 0)
            if m.role == "expert":
                choices.extend((p, "drop", -82) for p in sim.drops if p["land"] <= sim.t + 12)
            self.farm_goal = min(((p, task, weight) for p, task, weight in choices if safe(p["pos"])),
                                 key=lambda x: horizontal(m.pos, x[0]["pos"]) + x[2], default=None)
        if self.farm_goal is None:
            theta = m.id * 2.4 + sim.t * .022
            x, y = zone["center"]
            r = min(zone["radius"] * .64, 200)
            goal = (x + r * math.cos(theta), y + r * math.sin(theta))
            action = self.steer(goal)
            action["aim"] = [math.atan2(goal[1] - m.pos[1], goal[0] - m.pos[0]), 0.]
            return action
        item, task, _ = self.farm_goal
        goal = item["pos"]
        d = horizontal(m.pos, goal)
        if task == "farm":
            if item["hp"] <= 0:
                self.farm_goal = None
                return self.farm(zone, True)
            _, height = sim.dimensions(item)
            action, clear = self.gun(goal, height * .5, (0., 0., 0.))
            if not clear or d > 55:
                action.update(self.steer(goal, d > 90 and m.state.energy > 30))
            else:
                action["move"] = [0., 0.]
            return action
        action = self.steer(goal, d > 85 and m.state.energy > 32)
        action["aim"] = [math.atan2(goal[1] - m.pos[1], goal[0] - m.pos[0]), 0.]
        resources = {"take": sim.pickups, "cube": sim.cubes, "drop": sim.drops}
        if item not in resources[task]:
            self.farm_goal = None
        if task == "take" and d <= sim.cfg["loot"]["pickup_radius"]:
            action["take"] = item["id"]
            if m.weapons[1]:
                action["drop"] = 1
        return action

    def gun(self, pos, height, velocity, precision=1., noise=0.):
        sim, m = self.sim, self.mech
        d = horizontal(m.pos, pos)
        muzzle = raised(m.pos, m.stats["muzzle_height"])
        center = raised(pos, height)
        lead = d * precision / m.weapons[0]["stats"]["speed"]
        aim = advance(center, velocity, lead)
        yaw = math.atan2(aim[1] - muzzle[1], aim[0] - muzzle[0]) + noise
        pitch = math.atan2(aim[2] - muzzle[2], max(1., horizontal(m.pos, aim)))
        clear = not math.isfinite(sim.world.ray(muzzle, aim))
        reach = m.weapons[0]["stats"]["range"]
        return {"aim": [yaw, pitch], "fire": [
            clear and d <= reach and abs(angle(yaw, m.aim[0])) < .105 and abs(pitch - m.aim[1]) < .075, False]}, clear

    def close_on_roof(self, target):
        sim, m = self.sim, self.mech
        if self.climb and (sim.t >= self.climb_until or m.state.grounded):
            self.climb = None
        if not self.climb and m.state.grounded and m.state.energy >= 48:
            d = horizontal(m.pos, target.pos)
            if 3 < d < 22 and 5.8 <= target.pos[2] - m.pos[2] <= 14 and sim.world.on_roof(target.pos):
                roofs = [n.pos.as_tuple() for n in sim.world.reach(target.pos, 100, 21) if n.roof
                         and abs(n.pos.z - target.pos[2]) < .2]
                goal = min(roofs, key=lambda p: horizontal(m.pos, p), default=None)
                if goal and horizontal(m.pos, goal) < 16:
                    self.climb, self.climb_until = goal, sim.t + 3
                    return dict(self.steer(goal, True, .12, simple=True), jump=True)
        if self.climb:
            cleared = m.pos[2] >= self.climb[2] + .35
            return self.steer(self.climb, not cleared, 1. if cleared else .12, simple=True)
        return None

    def fight(self, target, lock, zone, flank=0, bearing=0.):
        sim, m = self.sim, self.mech
        d = horizontal(m.pos, target.pos)
        direct = math.atan2(target.pos[1] - m.pos[1], target.pos[0] - m.pos[0])
        favored = m.weapons[1]
        kind = favored["kind"] if favored else None
        close = kind == "lightning"
        move_goal = self.zone_goal(zone)
        climb = None
        if self.relief:
            move_goal = move_goal or advance(m.pos, unit(direct + math.pi, 0), 44)
        elif move_goal is None:
            if m.role == "easy":
                move_goal = target.pos if d > 29 else None
            else:
                weave = -1 if int((sim.t + m.id * .13) / .5) % 2 else 1
                ideal = 31 if close else 59 if m.role == "medium" else 72
                if flank and not close:
                    move_goal = advance(target.pos, unit(bearing + flank * math.radians(75), 0), ideal)
                    if horizontal(m.pos, move_goal) < 10:
                        move_goal = advance(m.pos, unit(direct + weave * math.pi / 2, 0), 11)
                elif close and d > 35:
                    move_goal = target.pos
                elif m.role == "expert" and sim.world.on_roof(target.pos) and not sim.world.on_roof(m.pos):
                    move_goal = target.pos
                    climb = self.close_on_roof(target)
                else:
                    step = direct + weave * (.40 if d > ideal + 12 else 2.20 if d < ideal - 14 else math.pi / 2)
                    move_goal = advance(m.pos, unit(step, 0), min(24, d))
        action = climb or self.steer(move_goal, m.state.energy > (9 if close else 28)
                                    and (self.relief or close and d > 35 or d > 95 and m.role != "easy"),
                                    .81 if m.role == "easy" else 1.)
        if lock is None or self.relief:
            action["aim"] = [direct, 0.] if lock is not None else [math.atan2(*reversed(action["move"])), 0.]
            return action

        velocity = target.state.vel.as_tuple()
        if m.role == "expert":
            velocity = self.fire_model.velocity(target.public())
        if m.role == "easy":
            noise = .09 * math.sin(sim.t * 5.7 + m.id) + .05 * math.sin(sim.t * 2.1)
            fire, clear = self.gun(target.pos, target.stats["height"] * .54, velocity, .29, noise)
            fire["fire"][0] &= int(sim.t * 2) % 3 != 2
        else:
            fire, clear = self.gun(target.pos, target.stats["height"] * .54, velocity)
        action.update(fire)
        muzzle = raised(m.pos, m.stats["muzzle_height"])
        center = raised(target.pos, target.stats["height"] * .5)
        pitch = math.atan2(center[2] - muzzle[2], max(1, d))
        if close:
            action["fire"][1] = d <= 40 and abs(angle(direct, m.aim[0])) < .22 and not math.isfinite(sim.world.ray(muzzle, center))
        elif kind == "laser" and (favored["cooldown"] <= .12 or favored["charge"] > 0):
            clear = not math.isfinite(sim.world.ray(muzzle, center))
            aligned = abs(angle(direct, m.aim[0])) < .045 and abs(pitch - m.aim[1]) < .045
            charged = favored["charge"] >= favored["stats"]["charge"] - 1e-5
            action["aim"] = [direct, pitch]
            action["fire"] = [False, d < favored["stats"]["range"] and not (charged and aligned and clear)]
        elif kind == "mortar" and d > 24 and (not clear or m.role != "medium") and favored["cooldown"] <= .16:
            model = self.fire_model
            solution = model.shot({"pos": m.pos, "stats": m.stats}, target.public(), favored)
            if solution:
                _, yaw, pitch, _ = solution
                action["aim"] = [yaw, pitch]
                action["fire"] = [False, abs(angle(yaw, m.aim[0])) < .052 and abs(pitch - m.aim[1]) < .05]
        return action

    def act(self, target, lock, zone, flank=0, bearing=0.):
        m = self.mech
        nearby_supply = m.role == "expert" and any(d["land"] < self.sim.t + 9 and
                                                   horizontal(m.pos, d["pos"]) < 55 for d in self.sim.drops)
        if target and (not nearby_supply or horizontal(m.pos, target.pos) <= 80):
            action = self.fight(target, lock, zone, flank, bearing)
        else:
            action = self.farm(zone)
        if m.offer:
            priority = {"armor": 12, "nanites": 11, "plating": 10, "gun_damage": 8, "servos": 7,
                        "laser_damage": 6, "lightning_range": 6, "mortar_reload": 6}
            action["pick"] = max(range(len(m.offer["choices"])),
                                 key=lambda i: priority.get(m.offer["choices"][i]["id"], 0))
        return action
