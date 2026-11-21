"""Mech Royale strategy for the mechfight/1 game protocol."""
from dataclasses import dataclass
import math

from arena_geometry import Arena
from ground_navigation import Navigation
import mechfight as mf


@dataclass(frozen=True)
class Tactics:
    retreat_hp: float
    recover_hp: float
    gun_distance: float
    laser_distance: float
    mortar_distance: float
    lightning_distance: float


@dataclass(frozen=True)
class Shot:
    slot: int
    yaw: float
    pitch: float
    yaw_margin: float
    pitch_margin: float
    priority: int
    trigger: bool

    def aligned(self, yaw, pitch):
        return (abs(angle_error(self.yaw, yaw)) <= self.yaw_margin
                and abs(self.pitch - pitch) <= self.pitch_margin)


PRACTICE = Tactics(0.56, 0.88, 72, 115, 82, 29)
FREE_FOR_ALL = Tactics(0.66, 0.88, 88, 135, 98, 32)


def angle_error(goal, current):
    return math.remainder(goal - current, math.tau)


def bearing(start, end):
    return math.atan2(end[1] - start[1], end[0] - start[0])


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def intercept(offset, velocity, speed, barrel=0):
    """Return the earliest positive intercept for a constant-velocity target."""
    a = sum(v * v for v in velocity) - speed * speed
    b = 2 * (sum(p * v for p, v in zip(offset, velocity)) - speed * barrel)
    c = sum(p * p for p in offset) - barrel * barrel
    if c <= 0:
        return 0
    if abs(a) < 1e-9:
        return -c / b if b < 0 else None
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    root = math.sqrt(disc)
    return min((t for t in ((-b - root) / (2 * a), (-b + root) / (2 * a))
                if t > 0), default=None)


class Bot:
    name = "mech-royale"

    def __init__(self, hello: mf.Hello, practice=PRACTICE, free_for_all=FREE_FOR_ALL):
        self.config = hello["config"]
        self.arena = Arena(hello["map"]["dir"])
        self.navigation = Navigation(self.arena, self.config)
        self.practice, self.free_for_all = practice, free_for_all
        match = self.config["match"]
        self.physics_step = 1 / match["tick_hz"]
        self.turn_step = match["decide_every"] * self.physics_step
        self.last_combat = -math.inf
        self.last_damage_bearing = None
        self.last_damage = -math.inf
        self.pvp = False
        self.recovering = False
        self.scan = hello["slot"] * math.tau / match["mechs"]
        self.scout_waypoint = None
        self.scout_until = -math.inf
        self.scout_turn = 0
        self.turn_side = 1 if hello["slot"] % 2 else -1

    def update_situation(self, obs):
        now = obs["t"]
        for event in obs["events"]:
            kind = event["type"]
            if kind in ("damaged", "hit", "kill"):
                self.last_combat = now
            if kind == "damaged" and event["bearing"] is not None:
                self.last_damage_bearing = event["bearing"]
                self.last_damage = now

    def aim(self, me, target, weapon):
        kind, stats = weapon["kind"], weapon["stats"]
        target_pos = target["pos"]
        target_kind = target.get("kind", "crate")
        if target_kind == "mech":
            size = 1 + self.config["cubes"]["size_per_cube"] * target.get("cubes", 0)
            dimensions = self.config["mech"]
        else:
            size = 1
            dimensions = self.config["npc" if target_kind == "npc" else "crates"]
        x, y, z = me["pos"]
        muzzle = z + (me["stats"]["height"] + 0.5 if kind == "mortar"
                      else me["stats"]["muzzle_height"])
        start = x, y, muzzle
        target_height = dimensions["height"] * size
        aim_height = min(0.8, target_height / 2) if kind == "mortar" else target_height / 2
        offset = target_pos[0] - x, target_pos[1] - y, target_pos[2] + aim_height - muzzle
        velocity = target.get("vel", (0, 0, 0))
        if kind == "gun":
            flight = intercept(offset, velocity, stats["speed"], me["stats"]["radius"] + 0.5)
            if flight is None:
                return None
        elif kind == "mortar":
            flight = math.hypot(*offset[:2]) / stats["speed"]
        else:
            flight = 0

        # Refine the low arc for target motion; use the live shell stats.
        gravity = stats["gravity"] if kind == "mortar" else 0
        for _ in range(4 if gravity else 0):
            dx, dy, dz = (p + v * flight for p, v in zip(offset, velocity))
            horizontal = math.hypot(dx, dy)
            speed_squared = stats["speed"] ** 2
            disc = speed_squared ** 2 - gravity * (gravity * horizontal ** 2 + 2 * dz * speed_squared)
            if disc < 0 or horizontal < 2 * stats["splash_radius"]:
                return None
            tan_pitch = (gravity * horizontal ** 2 + 2 * dz * speed_squared) / (
                horizontal * (speed_squared + math.sqrt(disc))
            )
            flight = horizontal * math.hypot(1, tan_pitch) / stats["speed"]
        dx, dy, dz = (p + v * flight for p, v in zip(offset, velocity))
        horizontal = math.hypot(dx, dy)
        reach = stats.get("range", stats.get("speed", 0) * stats.get("ttl", 0))
        if math.hypot(horizontal, dz) > reach or (gravity and flight > stats["ttl"]):
            return None
        yaw = math.atan2(dy, dx)
        pitch = math.atan2(dz + gravity * flight ** 2 / 2, horizontal)
        if not me["stats"]["min_pitch"] <= pitch <= me["stats"]["max_pitch"]:
            return None
        previous = start
        segments = 6 if gravity else 1
        for step in range(1, segments + 1):
            t = step / segments
            point = (x + dx * t, y + dy * t,
                     muzzle + dz * t + gravity * flight ** 2 * t * (1 - t) / 2)
            if not self.arena.clear_shot(previous, point):
                return None
            previous = point
        yaw_margin = min(0.22, math.atan2(dimensions["radius"] * size * 0.85, max(1, horizontal)))
        pitch_margin = min(0.22, math.atan2(target_height * 0.38, max(1, math.hypot(horizontal, dz))))
        if kind == "mortar":
            pitch_margin = min(pitch_margin, yaw_margin)
        return yaw, pitch, yaw_margin, pitch_margin

    def attack(self, me, target, action):
        fire = [False, False]
        action["fire"] = fire
        if target is None:
            return
        yaw, pitch = me["aim"]
        options = []
        for slot, weapon in enumerate(me["weapons"]):
            if weapon is None:
                continue
            kind, stats = weapon["kind"], weapon["stats"]
            if kind == "lightning" and me["energy"] < stats["energy"]:
                continue
            solution = self.aim(me, target, weapon)
            if solution is None:
                continue
            desired_yaw, desired_pitch, yaw_margin, pitch_margin = solution
            if kind == "lightning":
                yaw_margin = math.radians(stats["cone_deg"]) / 2
                pitch_margin = math.inf
            yaw_error = abs(angle_error(desired_yaw, yaw))
            pitch_error = abs(desired_pitch - pitch)
            aligned = yaw_error <= yaw_margin and pitch_error <= pitch_margin
            ready = (weapon["cooldown"] <= self.turn_step and not weapon.get("reload", 0)
                     and weapon.get("ammo", 1) > 0)
            if kind == "laser":
                charging = weapon.get("charge", 0)
                # An absent shot solution leaves the trigger released even while charging.
                trigger = ready and charging < stats["charge"] - 1e-6
                priority = 100 if charging else 56
            elif kind == "lightning":
                trigger = ready and aligned
                priority = 85 if target.get("charging") else 62
            else:
                trigger = ready and aligned
                priority = 66 if kind == "mortar" else 50
            close_to_alignment = (
                yaw_error <= yaw_margin + me["stats"]["turn_rate"] * self.turn_step
                and pitch_error <= pitch_margin + me["stats"]["pitch_rate"] * self.turn_step
            )
            if not ready or (kind != "laser" and not close_to_alignment):
                priority -= 70
            options.append(Shot(slot, desired_yaw, desired_pitch, yaw_margin,
                                pitch_margin, priority, trigger))
        if options:
            primary = max(options, key=lambda shot: shot.priority)
            action["aim"] = [primary.yaw, primary.pitch]
            for shot in options:
                if shot.trigger and (
                    shot is primary or (
                        me["weapons"][shot.slot]["kind"] != "laser"
                        and shot.aligned(primary.yaw, primary.pitch)
                    )
                ):
                    fire[shot.slot] = True

    def safe_destination(self, obs, goal, margin=16):
        zone = obs["zone"]
        current = zone["center"]
        upcoming = zone["next_center"]
        active = obs["t"] >= zone["shrink_start"] - 14
        center = upcoming if active else current
        radius = zone["next_radius"] if active else zone["radius"]
        extent = max(0, radius - margin)
        d = distance(center, goal)
        if d <= extent:
            return goal
        return tuple(c + (p - c) * extent / d for c, p in zip(center, goal))

    def ideal_range(self, me, target, tactics):
        gap = distance(me["pos"], target["pos"])
        preferred = []
        for weapon in me["weapons"]:
            if weapon is None:
                continue
            kind, stats = weapon["kind"], weapon["stats"]
            if kind == "lightning":
                if me["energy"] >= stats["energy"]:
                    preferred.append((10 if gap < stats["range"] * 0.8 else 1,
                                      min(tactics.lightning_distance, stats["range"] * 0.72)))
            elif kind == "laser":
                preferred.append((7, min(tactics.laser_distance, stats["range"] * 0.66)))
            elif kind == "mortar":
                preferred.append((4, max(tactics.mortar_distance, stats["splash_radius"] * 3)))
            elif kind == "gun":
                preferred.append((6, min(tactics.gun_distance, stats["range"] * 0.66)))
        ideal = max(preferred, default=(0, tactics.gun_distance))[1]
        if not preferred:
            ideal *= 1.4
        if "lightning" in target.get("weapons", ()):
            ideal = max(55, ideal)
        return ideal

    def loot(self, obs, enemies, action):
        me = obs["self"]
        pos, weapons = me["pos"], me["weapons"]
        reach = self.config["loot"]["pickup_radius"]
        held = {weapon["kind"] for weapon in weapons if weapon}
        order = {"gun": 1, "mortar": 2, "laser": 3, "lightning": 4}
        if obs["zone"]["radius"] > 85:
            order["laser"], order["lightning"] = 4, 3
        candidates = [p for p in obs["pickups"]
                      if p["kind"] not in held and abs(p["pos"][2] - pos[2]) <= 4]
        candidates.sort(key=lambda item: (-order[item["kind"]], distance(pos, item["pos"])))
        for item in candidates:
            if distance(pos, item["pos"]) > reach:
                continue
            if None not in weapons:
                if enemies:
                    continue
                eligible = [
                    i for i, old in enumerate(weapons)
                    if order[item["kind"]] > order[old["kind"]] and not old.get("charge", 0)
                    and (old["kind"] != "gun"
                         or {item["kind"], weapons[1 - i]["kind"]} == {"laser", "lightning"})
                ]
                if not eligible:
                    continue
                action["drop"] = min(eligible, key=lambda i: order[weapons[i]["kind"]])
            action["take"] = item["id"]
            break
        return candidates

    def resource_goal(self, obs, pickups, enemies):
        me = obs["self"]
        pos = me["pos"]
        needs_weapon = None in me["weapons"]
        needs_cubes = me["cubes"] < self.config["cubes"]["stat_cap"]
        options = []
        if needs_weapon:
            for pickup in pickups:
                bonus = {"laser": 36, "lightning": 32, "mortar": 0}.get(pickup["kind"])
                if bonus is not None:
                    options.append((pickup["pos"], 99 + bonus, 105))
        if needs_cubes:
            for cube in obs["cubes"]:
                if abs(cube["pos"][2] - pos[2]) <= 4:
                    options.append((cube["pos"], 65, 43))

        ranked = []
        for point, value, reach in options:
            gap = distance(pos, point)
            if 1 < gap <= reach and distance(point, self.safe_destination(obs, point[:2])) < 2:
                ranked.append((value - gap * 0.7, point, gap))
        best = None
        for score, point, gap in sorted(ranked, key=lambda option: option[0], reverse=True)[:8]:
            dx, dy = point[0] - pos[0], point[1] - pos[1]
            clearance = math.inf
            for enemy in enemies:
                ex, ey = enemy["pos"][0] - pos[0], enemy["pos"][1] - pos[1]
                fraction = max(0, min(1, (ex * dx + ey * dy) / (gap * gap)))
                clearance = min(clearance, math.hypot(ex - fraction * dx, ey - fraction * dy))
            if clearance < 34:
                continue
            if self.arena.solid_between((pos[0], pos[1], pos[2] + 4),
                                        (point[0], point[1], point[2] + 4)):
                score -= 32
            score -= max(0, 80 - clearance) * 0.55
            if score > 15 and (best is None or score > best[0]):
                best = score, tuple(point[:2])
        return best[1] if best else None

    def scout_goal(self, obs):
        pos = obs["self"]["pos"]
        now = obs["t"]
        if (self.scout_waypoint is not None and now < self.scout_until
                and distance(pos, self.scout_waypoint) > 8):
            return self.scout_waypoint
        center = obs["zone"]["next_center"]
        distance_to_center = distance(pos, center)
        if distance_to_center > 105:
            offset = 0.22 + 0.48 * (self.scout_turn % 3)
            yaw = bearing(pos, center) + self.turn_side * offset
        else:
            yaw = self.scan + self.scout_turn * 2.39996
        x, y = pos[0] + 85 * math.cos(yaw), pos[1] + 85 * math.sin(yaw)
        bounds = self.arena.bounds
        waypoint = (max(bounds["minX"] + 12, min(x, bounds["maxX"] - 12)),
                    max(bounds["minY"] + 12, min(y, bounds["maxY"] - 12)))
        self.scout_waypoint = self.safe_destination(obs, waypoint)
        self.scout_until = now + 7.5
        self.scout_turn += 1
        return self.scout_waypoint

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        pos = me["pos"]
        now = obs["t"]
        enemies = sorted((a for a in obs["mechs"] if a["kind"] == "mech"),
                         key=lambda a: distance(pos, a["pos"]))
        self.update_situation(obs)
        hp = me["hp"] / me["max_hp"]
        tactics = self.free_for_all if self.pvp else self.practice
        if hp < tactics.retreat_hp:
            self.recovering = True
        if hp >= tactics.recover_hp:
            self.recovering = False

        nearest = enemies[0] if enemies else None
        near_dist = distance(pos, nearest["pos"]) if nearest else math.inf
        recently_hit = now - self.last_damage < 2
        threat_bearing = bearing(pos, nearest["pos"]) if nearest else (
            self.last_damage_bearing if recently_hit else None
        )
        active_enemies = [e for e in enemies if distance(pos, e["pos"]) < 115]
        disengage = self.recovering or (len(active_enemies) >= 3 and near_dist > 22)
        target = next((e for e in sorted(enemies, key=lambda a: (
            distance(pos, a["pos"]) + 75 * a.get("hp", 1400) / a.get("max_hp", 1400)
        )) if not self.pvp or distance(pos, e["pos"]) < 40
            or (recently_hit and abs(angle_error(bearing(pos, e["pos"]), self.last_damage_bearing)) < 0.5)
            or e.get("hp", math.inf) < 0.26 * e.get("max_hp", 1400)), None)
        if disengage:
            # Only shoot through recovery when a close attacker can still hit us.
            target = None
            if nearest and near_dist < 20 and (recently_hit or near_dist < 10):
                tx, ty, tz = nearest["pos"]
                muzzle = self.config["mech"]["muzzle_height"]
                if self.arena.clear_shot((tx, ty, tz + muzzle),
                                         (pos[0], pos[1], pos[2] + me["stats"]["height"] / 2)):
                    target = nearest

        action: mf.Action = {"tick": obs["tick"]}
        if obs["offer"]:
            order = ("armor", "nanites", "servos", "plating", "reactor", "jets", "sensors",
                     "gun_mag", "laser_charge", "gun_damage", "lightning_range")
            ranks = {key: len(order) - i for i, key in enumerate(order)}
            action["pick"] = max(range(len(obs["offer"]["choices"])),
                                 key=lambda i: ranks.get(obs["offer"]["choices"][i]["id"], 0))
        loot = self.loot(obs, enemies, action)
        zone = obs["zone"]
        zone_goal = self.safe_destination(obs, pos[:2])
        urgent_zone = not me["in_zone"] or distance(pos, zone_goal) > me["stats"]["walk_speed"] * max(
            2, zone["shrink_end"] - now - 7
        )

        goal = zone_goal
        movement = "travel"
        if not urgent_zone:
            resources = None
            if not disengage and not recently_hit and near_dist > 42:
                resources = self.resource_goal(obs, loot, enemies)
            drops = [d for d in obs["drops"] if d["lands_in"] == 0 and d["holder"] in (None, me["id"])
                     and hp < 0.78 and not enemies and not recently_hit and distance(pos, d["pos"]) < 42]
            if drops:
                goal = min(drops, key=lambda d: distance(pos, d["pos"]))["pos"][:2]
            elif disengage:
                if enemies:
                    movement = "retreat"
                elif recently_hit and threat_bearing is not None:
                    away = threat_bearing + math.pi
                    goal = (pos[0] + 36 * math.cos(away), pos[1] + 36 * math.sin(away))
                elif distance(pos, zone_goal) < 2:
                    movement = "hold"
            elif resources is not None:
                goal = resources
            elif target is not None:
                movement = "kite"
            elif not enemies and me["cubes"] < self.config["cubes"]["stat_cap"] and not recently_hit:
                farm = [a for a in (*obs["crates"], *obs["mechs"])
                        if a.get("kind") != "mech" and distance(pos, a["pos"]) < 120]
                target = min(farm, key=lambda a: distance(pos, a["pos"]), default=None)
                if target is not None:
                    goal = target["pos"][:2] if distance(pos, target["pos"]) > 28 else pos[:2]
        if movement == "travel" and not urgent_zone and distance(pos, goal) < 2 and target is None:
            goal = self.scout_goal(obs)
        goal = self.safe_destination(obs, goal)
        if not me["in_zone"] and distance(pos, goal) < 2:
            goal = zone["center"]
        ideal = self.ideal_range(me, target, tactics) if movement == "kite" else None
        controls, crossing = self.navigation.control(
            obs, enemies, goal, movement, target if movement == "kite" else None,
            ideal, recently_hit, urgent_zone,
        )
        action.update(controls)
        if crossing:
            target = None

        if threat_bearing is None:
            self.scan = math.remainder(self.scan + 0.055, math.tau)
            sounds = [s for s in obs["sounds"] if s["dist"] == "near"
                      and s["kind"] in ("gun", "laser_charge", "lightning", "boost")]
            look = sounds[0]["bearing"] if sounds else self.scan
        else:
            look = threat_bearing
        action["aim"] = [look, 0.0]
        self.attack(me, target, action)
        gun = next((w for w in me["weapons"] if w and w["kind"] == "gun"), None)
        if gun and not gun["reload"] and gun["ammo"] < gun["stats"]["mag"]:
            action["reload"] = gun["ammo"] == 0 or (target is None and (near_dist > 65 or disengage))
        mode = "FFA" if self.pvp else "practice"
        recovering = self.recovering and now - self.last_combat >= me["stats"]["regen_delay"]
        state = "roof transfer" if crossing else "healing" if recovering else movement
        mf.draw.label(f"{mode} | {state}")
        return action


if __name__ == "__main__":
    mf.run(Bot)
