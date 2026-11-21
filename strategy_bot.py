"""Mech Royale strategy for the mechfight/1 game protocol."""
from dataclasses import dataclass
import math

from arena_geometry import Arena
import mechfight as mf


@dataclass(frozen=True)
class Tactics:
    retreat_hp: float
    recover_hp: float
    gun_distance: float
    laser_distance: float
    mortar_distance: float
    lightning_distance: float
    kite_band: float = 14
    cover_hold_distance: float = 50


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
        self.practice, self.free_for_all = practice, free_for_all
        match = self.config["match"]
        self.physics_step = 1 / match["tick_hz"]
        self.turn_step = match["decide_every"] * self.physics_step
        self.alive = match["mechs"]
        self.starting_alive = self.alive
        self.first_shrink = self.config["zone"]["phases"][0]["hold_s"]
        self.kills = set()
        self.other_fight_until = {}
        self.last_combat = -math.inf
        self.last_damage_bearing = None
        self.last_damage = -math.inf
        self.pvp = False
        self.recovering = False
        self.scan = hello["slot"] * math.tau / match["mechs"]
        self.turn_side = 1 if hello["slot"] % 2 else -1

    def update_situation(self, obs, enemies):
        now = obs["t"]
        for event in obs["events"]:
            kind = event["type"]
            if kind in ("damaged", "hit"):
                self.last_combat = now
            if kind == "kill":
                self.kills.add(event["target"])
                self.last_combat = now
            if kind == "damaged" and event["bearing"] is not None:
                self.last_damage_bearing = event["bearing"]
                self.last_damage = now
        # Zone deaths can also happen in practice, so use early deaths only.
        if (obs["alive"] < self.alive and now < self.first_shrink
                and self.starting_alive - obs["alive"] > len(self.kills)):
            self.pvp = True
        self.alive = obs["alive"]
        own = obs["self"]["pos"]
        for beam in obs["beams"]:
            source = beam["by"]
            for point in beam["path"][1:]:
                # A beam ending near us is ambiguous even when another mech is nearby.
                if distance(point, own) < obs["self"]["stats"]["radius"] + 6:
                    continue
                for enemy in enemies:
                    if enemy["id"] == source:
                        continue
                    pos = enemy["pos"]
                    size = 1 + self.config["cubes"]["size_per_cube"] * enemy.get("cubes", 0)
                    stats = self.config["mech"]
                    if (distance(point, pos) <= stats["radius"] * size + 1
                            and pos[2] - 1 <= point[2] <= pos[2] + stats["height"] * size + 1):
                        self.pvp = True
                        self.other_fight_until[enemy["id"]] = now + 3
                        self.other_fight_until[source] = now + 3

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
        offset = target_pos[0] - x, target_pos[1] - y, target_pos[2] + dimensions["height"] * size / 2 - muzzle
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
        tolerance = min(0.22, math.atan2(dimensions["radius"] * size * 0.85, max(1, horizontal)))
        return yaw, pitch, tolerance

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
            solution = self.aim(me, target, weapon)
            if solution is None:
                continue
            desired_yaw, desired_pitch, tolerance = solution
            kind, stats = weapon["kind"], weapon["stats"]
            aligned = (abs(angle_error(desired_yaw, yaw)) <= tolerance
                       and abs(desired_pitch - pitch) <= tolerance)
            ready = weapon["cooldown"] <= self.turn_step and not weapon.get("reload", 0)
            if kind == "laser":
                charging = weapon.get("charge", 0)
                trigger = ready and (charging < stats["charge"] - self.physics_step / 2
                                     or (not aligned and charging < stats["charge"] + 0.2))
                priority = 90 if charging else 44
            elif kind == "lightning":
                aligned = abs(angle_error(desired_yaw, yaw)) <= math.radians(stats["cone_deg"]) / 2
                trigger = ready and aligned and me["energy"] >= stats["energy"]
                priority = 60 if target.get("charging") else 48
            else:
                trigger = ready and aligned and weapon.get("ammo", 1) > 0
                priority = 34 if kind == "mortar" else 40
            if not ready or (kind != "laser" and not aligned):
                priority -= 30
            options.append((priority, slot, desired_yaw, desired_pitch, trigger))
        if options:
            _, slot, desired_yaw, desired_pitch, trigger = max(options)
            action["aim"] = [desired_yaw, desired_pitch]
            fire[slot] = trigger

    def projectile_threat(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        vx, vy, vz = me["vel"]
        radius = me["stats"]["radius"]
        best = None
        for shot in obs["projectiles"]:
            px, py, pz = shot["pos"]
            sx, sy, sz = shot["vel"]
            dx, dy = px - x, py - y
            rx, ry = sx - vx, sy - vy
            speed_squared = rx * rx + ry * ry
            if speed_squared < 1:
                continue
            t = -(dx * rx + dy * ry) / speed_squared
            if not 0 < t < 0.45:
                continue
            closest = dx + rx * t, dy + ry * t
            shell = shot["kind"] == "mortar"
            mortar = self.config["weapons"]["mortar"]
            clearance = radius + (mortar["splash_radius"] if shell else 0.7)
            height = pz - z + (sz - vz) * t - (mortar["gravity"] * t * t / 2 if shell else 0)
            if math.hypot(*closest) > clearance or not -0.5 <= height <= me["stats"]["height"] + 0.5:
                continue
            side = 1 if ry * closest[0] - rx * closest[1] >= 0 else -1
            dodge = math.atan2(rx * side, -ry * side)
            if best is None or t < best[0]:
                best = t, dodge
        if best is not None:
            return best[1]
        return None

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

    def kite_goal(self, me, target, tactics):
        pos = me["pos"]
        gap = distance(pos, target["pos"])
        facing = bearing(pos, target["pos"])
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
        # Blend strafe and range correction smoothly across the preferred band.
        correction = max(-1, min(1, (gap - ideal) / tactics.kite_band))
        yaw = facing + self.turn_side * (1 - correction) * math.pi / 2
        return pos[0] + 32 * math.cos(yaw), pos[1] + 32 * math.sin(yaw)

    def covered_for_recovery(self, me, enemies, tactics):
        pos = me["pos"]
        eye = (*pos[:2], pos[2] + me["stats"]["eye_height"])
        muzzle = self.config["mech"]["muzzle_height"]
        for enemy in enemies:
            ex, ey, ez = enemy["pos"]
            if distance(pos, enemy["pos"]) < tactics.cover_hold_distance:
                return False
            scale = 1 + self.config["cubes"]["size_per_cube"] * enemy.get("cubes", 0)
            if not self.arena.solid_between((ex, ey, ez + muzzle * scale), eye):
                return False
        return True

    def move(self, me, goal, action, dodge, hurry, quiet, cover_from=None):
        d = distance(me["pos"], goal)
        if dodge is None and d < 2:
            return
        yaw = dodge if dodge is not None else bearing(me["pos"], goal)
        stats = me["stats"]
        magnitude = min(1, d / (stats["walk_speed"] * self.turn_step + 1))
        if dodge is not None:
            magnitude = 1
        if quiet:
            magnitude = min(magnitude, 0.94 * self.config["sound"]["step_speed"] / stats["walk_speed"])
        horizon = max(4, stats["walk_speed"] * magnitude * 0.38 + stats["radius"])
        offsets = (0, 30, -30, 63, -63, 100, -100, 150, -150)
        best_yaw = None
        for offset in offsets:
            candidate = yaw + self.turn_side * math.radians(offset)
            if self.arena.walkable(me, candidate, horizon):
                if best_yaw is None:
                    best_yaw = candidate
                if cover_from is None or abs(offset) > 63:
                    break
                x, y, z = me["pos"]
                after_move = (x + horizon * math.cos(candidate), y + horizon * math.sin(candidate),
                              z + stats["eye_height"])
                if self.arena.solid_between(cover_from, after_move):
                    best_yaw = candidate
                    break
        if best_yaw is None:
            # shortcut: a local escape jump can clear short ledges; add route planning for deep concavities.
            action["jump"] = me["grounded"] and me["energy"] > 35
            action["boost"] = not me["grounded"] and me["energy"] > 30
            return
        action["move"] = [math.cos(best_yaw) * magnitude, math.sin(best_yaw) * magnitude]
        action["boost"] = (hurry or dodge is not None) and me["grounded"] and me["energy"] > 24

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

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        pos = me["pos"]
        now = obs["t"]
        enemies = sorted((a for a in obs["mechs"] if a["kind"] == "mech"),
                         key=lambda a: distance(pos, a["pos"]))
        self.update_situation(obs, enemies)
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
            or e.get("hp", math.inf) < 0.26 * e.get("max_hp", 1400)
            or self.other_fight_until.get(e["id"], -1) > now), None)
        if disengage:
            # A distant hit restarts the heal timer even if it lands while retreating.
            target = nearest if near_dist < 20 else None

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
        quiet = False
        if not urgent_zone:
            drops = [d for d in obs["drops"] if d["lands_in"] == 0 and d["holder"] in (None, me["id"])
                     and hp < 0.78 and not enemies and not recently_hit
                     and distance(pos, d["pos"]) < 42]
            if drops:
                goal = min(drops, key=lambda d: distance(pos, d["pos"]))["pos"][:2]
            elif (self.recovering and me["grounded"] and not recently_hit
                  and self.covered_for_recovery(me, enemies, tactics)):
                goal = zone_goal
            elif threat_bearing is not None and (disengage or (self.pvp and target is None and near_dist < 70)):
                away = threat_bearing + math.pi + 0.22 * self.turn_side
                goal = (pos[0] + 40 * math.cos(away), pos[1] + 40 * math.sin(away))
            elif target is not None:
                goal = self.kite_goal(me, target, tactics)
            elif not disengage:
                cubes = [c for c in obs["cubes"] if abs(c["pos"][2] - pos[2]) < 4]
                cap = self.config["cubes"]["stat_cap"] if not self.pvp else 6
                if cubes and me["cubes"] < cap and not active_enemies:
                    goal = min(cubes, key=lambda c: distance(pos, c["pos"]))["pos"][:2]
                elif None in me["weapons"] and loot and not active_enemies:
                    goal = min(loot, key=lambda p: distance(pos, p["pos"]))["pos"][:2]
                elif not enemies and me["cubes"] < cap and not recently_hit:
                    farm = [a for a in (*obs["crates"], *obs["mechs"])
                            if a.get("kind") != "mech" and distance(pos, a["pos"]) < (60 if self.pvp else 120)]
                    target = min(farm, key=lambda a: distance(pos, a["pos"]), default=None)
                    if target is not None and distance(pos, target["pos"]) > 28:
                        goal = target["pos"][:2]
                elif self.pvp and near_dist > 70:
                    quiet = True
        if distance(pos, goal) < 2 and target is None and not disengage and not self.pvp:
            goal = (pos[0] + 25 * math.cos(self.scan), pos[1] + 25 * math.sin(self.scan))
        goal = self.safe_destination(obs, goal)
        # Dash back across the current circle boundary before pursuing the next circle.
        if not me["in_zone"] and distance(pos, goal) < 2:
            goal = zone["center"]
        dodge = self.projectile_threat(obs)
        if dodge is None and nearest and nearest.get("charging") and near_dist < 145:
            aim_error = angle_error(bearing(nearest["pos"], pos), nearest["aim"][0])
            if abs(aim_error) < 0.16:
                dodge = threat_bearing + self.turn_side * math.pi / 2
        cover_from = None
        if nearest and disengage and not urgent_zone and dodge is None:
            tx, ty, tz = nearest["pos"]
            cover_from = (tx, ty, tz + self.config["mech"]["muzzle_height"])
        self.move(me, goal, action, dodge, urgent_zone or (disengage and near_dist < 95),
                  quiet and dodge is None and not urgent_zone, cover_from)

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
        mf.draw.label(f"{mode} | {'healing' if recovering else 'retreat' if disengage else 'active'}")
        return action


if __name__ == "__main__":
    mf.run(Bot)
