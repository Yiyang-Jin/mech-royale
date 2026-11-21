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
        self.pickup_radius = self.config["loot"]["pickup_radius"]
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
        self.sound_bearing = None
        self.sound_until = -math.inf
        self.velocity_tracks = {}
        self.velocity_time = -math.inf
        self.scout_waypoint = None
        self.scout_until = -math.inf
        self.scout_turn = 0
        self.turn_side = 1 if hello["slot"] % 2 else -1
        self.weave_offset = hello["slot"] * 0.083
        self.roof = None
        self.roof_phase = "approach"
        self.roof_deadline = -math.inf
        self.roof_retry = -math.inf

    def update_situation(self, obs):
        now = obs["t"]
        for event in obs["events"]:
            kind = event["type"]
            if kind in ("damaged", "hit", "kill"):
                self.last_combat = now
            if kind == "damaged" and event["bearing"] is not None:
                self.last_damage_bearing = event["bearing"]
                self.last_damage = now

    def target_dimensions(self, target):
        target_kind = target.get("kind", "crate")
        if target_kind == "mech":
            size = 1 + self.config["cubes"]["size_per_cube"] * target.get("cubes", 0)
            dimensions = self.config["mech"]
        else:
            size = 1
            dimensions = self.config["npc" if target_kind == "npc" else "crates"]
        return dimensions["radius"] * size, dimensions["height"] * size

    def observe_velocity(self, now, enemies):
        previous = self.velocity_tracks if now - self.velocity_time <= 0.3 else {}
        self.velocity_time = now
        self.velocity_tracks = {}
        for enemy in enemies:
            velocity = enemy.get("vel", (0, 0, 0))
            last = previous.get(enemy["id"])
            # Reset on a reversal instead of leading in the old direction.
            if last is not None and sum(a * b for a, b in zip(last[:2], velocity[:2])) > 0:
                velocity = tuple(0.7 * a + 0.3 * b for a, b in zip(last, velocity))
            self.velocity_tracks[enemy["id"]] = velocity

    def sound_direction(self, obs):
        now = obs["t"]
        priority = {"laser_charge": 4, "gun": 3, "boost": 2, "step": 1}
        near = [s for s in obs["sounds"] if s["dist"] == "near" and s["kind"] in priority and s["age"] <= 0.65]
        if near:
            sound = max(near, key=lambda s: (priority[s["kind"]], -s["age"]))
            self.sound_bearing = sound["bearing"]
            self.sound_until = now + 0.8 - sound["age"]
        return self.sound_bearing if now <= self.sound_until else None

    def aim(self, me, target, weapon):
        kind, stats = weapon["kind"], weapon["stats"]
        target_pos = target["pos"]
        target_radius, target_height = self.target_dimensions(target)
        x, y, z = me["pos"]
        muzzle = z + (me["stats"]["height"] + 0.5 if kind == "mortar"
                      else me["stats"]["muzzle_height"])
        start = x, y, muzzle
        aim_height = min(0.8, target_height / 2) if kind == "mortar" else target_height / 2
        offset = target_pos[0] - x, target_pos[1] - y, target_pos[2] + aim_height - muzzle
        velocity = target.get("vel", (0, 0, 0))
        if kind == "gun":
            velocity = self.velocity_tracks.get(target.get("id"), velocity)
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
        yaw_margin = min(0.22, math.atan2(target_radius * 0.85, max(1, horizontal)))
        pitch_margin = min(0.22, math.atan2(target_height * 0.38, max(1, math.hypot(horizontal, dz))))
        if kind == "mortar":
            pitch_margin = min(pitch_margin, yaw_margin)
        return yaw, pitch, yaw_margin, pitch_margin

    def actual_ray_hits(self, me, target, weapon):
        gun = weapon["kind"] == "gun"
        stats = weapon["stats"]
        speed = stats["speed"] if gun else 1.0
        target_vel = self.velocity_tracks.get(target.get("id"), target.get("vel", (0, 0, 0))) if gun else (0, 0, 0)
        x, y, z = me["pos"]
        muzzle = x, y, z + me["stats"]["muzzle_height"]
        yaw, pitch = me["aim"]
        unit = math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)
        barrel = me["stats"]["radius"] + 0.5
        origin = tuple(start + barrel * axis for start, axis in zip(muzzle, unit))
        px, py, pz = (start - feet for start, feet in zip(origin, target["pos"]))
        vx, vy, vz = (speed * axis - move for axis, move in zip(unit, target_vel))
        radius, height = self.target_dimensions(target)
        a = vx * vx + vy * vy
        b = px * vx + py * vy
        c = px * px + py * py - radius * radius
        lo, hi = 0.0, stats["range"] / speed
        if a > 1e-9:
            disc = b * b - a * c
            if disc < 0:
                return False
            root = math.sqrt(disc)
            lo, hi = max(lo, (-b - root) / a), min(hi, (-b + root) / a)
        elif c > 0:
            return False
        if abs(vz) > 1e-9:
            enter, leave = sorted((-pz / vz, (height - pz) / vz))
            lo, hi = max(lo, enter), min(hi, leave)
        elif not 0 <= pz <= height:
            return False
        if lo > hi:
            return False
        hit = tuple(start + speed * axis * lo for start, axis in zip(origin, unit))
        return self.arena.clear_shot(muzzle, hit)

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
            if aligned and kind in ("gun", "laser"):
                aligned = self.actual_ray_hits(me, target, weapon)
            ready = (weapon["cooldown"] <= self.turn_step and not weapon.get("reload", 0)
                     and weapon.get("ammo", 1) > 0)
            if kind == "laser":
                charging = weapon.get("charge", 0)
                trigger = ready and ((charging == 0 and aligned) or (charging > 0 and (
                    charging < stats["charge"] - self.physics_step / 2
                    or (not aligned and charging < stats["charge"] + 0.2)
                )))
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

    def weave_side(self, now):
        return self.turn_side * (-1 if int((now + self.weave_offset) / 0.78) % 2 else 1)

    def kite_goal(self, me, target, tactics, now=0):
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
        side = self.weave_side(now) if 24 < gap < 155 else self.turn_side
        yaw = facing + side * (1 - correction) * math.pi / 2
        return pos[0] + 32 * math.cos(yaw), pos[1] + 32 * math.sin(yaw)

    def covered_for_recovery(self, me, enemies, tactics):
        pos = me["pos"]
        eye = (*pos[:2], pos[2] + me["stats"]["eye_height"])
        muzzle = self.config["mech"]["muzzle_height"]
        for enemy in enemies:
            ex, ey, ez = enemy["pos"]
            if (distance(pos, enemy["pos"]) < tactics.cover_hold_distance
                    and pos[2] - ez < 8):
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

    def roof_escape(self, obs, enemies, action, enabled, urgent_zone):
        now, me = obs["t"], obs["self"]
        pos = me["pos"]
        if urgent_zone or now > self.roof_deadline:
            self.roof = None
        if urgent_zone:
            return False
        if self.roof is None:
            if not enabled or not enemies or now < self.roof_retry or not me["grounded"] or me["energy"] < 42:
                return False
            self.roof_retry = now + 2
            stats = me["stats"]
            muzzle = self.config["mech"]["muzzle_height"]
            candidates = []
            for roof in self.arena.roof_entries(me):
                if distance(roof.shelter, self.safe_destination(obs, roof.shelter)) > 2:
                    continue
                if any(distance(roof.launch, e["pos"]) < 19 for e in enemies):
                    continue
                eye = (*roof.shelter, roof.top + stats["eye_height"])
                cover = [self.arena.solid_between((e["pos"][0], e["pos"][1], e["pos"][2] + muzzle), eye)
                         for e in enemies]
                if cover[0]:
                    cost = distance(pos, roof.launch) + roof.energy * 0.22
                    candidates.append((cost - sum(cover) * 18, roof))
            if not candidates:
                return False
            self.roof = min(candidates, key=lambda item: item[0])[1]
            self.roof_phase = "approach"
            self.roof_deadline = now + 11
        roof = self.roof
        if self.roof_phase == "approach":
            gap = distance(pos, roof.launch)
            if gap > 2:
                self.move(me, roof.launch, action, None, False, False)
                if gap < 7 and "move" in action:
                    action["move"] = [component * 0.45 for component in action["move"]]
                return True
            if math.hypot(*me["vel"][:2]) > 2.5:
                action["move"] = [0, 0]
                return True
            if not me["grounded"] or me["energy"] < roof.energy - 2:
                self.roof = None
                return False
            self.roof_phase = "climb"
        if self.roof_phase == "climb" and pos[2] < roof.top + 0.8:
            if me["energy"] < 8:
                self.roof = None
                return False
            action.update(move=[0, 0], jump=me["grounded"], boost=not me["grounded"])
            return True

        self.roof_phase = "cross"
        if distance(pos, roof.shelter) < 2.5 and me["grounded"]:
            self.roof = None
            self.roof_retry = now + 5
            return False
        if pos[2] < roof.top - 0.5 or (me["energy"] < 2 and pos[2] < roof.top + 0.8):
            self.roof = None
            return False
        dx, dy = roof.landing[0] - roof.launch[0], roof.landing[1] - roof.launch[1]
        past_edge = dx * (pos[0] - roof.landing[0]) + dy * (pos[1] - roof.landing[1]) >= 0
        if distance(pos, roof.shelter) < 2.5:
            action["move"] = [0, 0]
        else:
            yaw = bearing(pos, roof.shelter)
            action["move"] = [math.cos(yaw), math.sin(yaw)]
        action["boost"] = not past_edge and not me["grounded"] and me["energy"] >= 2
        return True

    def loot(self, obs, enemies, action):
        me = obs["self"]
        pos, weapons = me["pos"], me["weapons"]
        reach = self.pickup_radius
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
                bonus = {"laser": 12, "lightning": 7, "mortar": 4}.get(pickup["kind"])
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

    def upgrade_choice(self, me, offer, enemy_distance):
        order = {"mortar": 1, "gun": 2, "lightning": 3, "laser": 4}
        if enemy_distance < 45:
            order["lightning"] = 5
        main = max((w["kind"] for w in me["weapons"] if w), key=order.get, default="gun")

        def rank(choice):
            upgrade = choice["id"]
            kind, _, effect = upgrade.partition("_")
            if upgrade == "armor":
                return 50
            if kind == main and effect in ("damage", "rate", "charge", "reload", "mag", "efficiency"):
                return 40 if effect in ("damage", "rate", "charge", "reload") else 39
            if upgrade in ("servos", "reactor", "jets", "capacitor"):
                return 30
            if upgrade in ("nanites", "plating"):
                return 25
            if upgrade == "sensors":
                return 20
            return 0

        return max(range(len(offer["choices"])), key=lambda i: rank(offer["choices"][i]))

    def rush_weapon(self, obs, loot):
        me = obs["self"]
        pos = me["pos"]
        if None not in me["weapons"]:
            return None
        usable = [pickup for pickup in loot if pickup["kind"] != "gun"
                  and distance(pos, pickup["pos"]) < 140
                  and distance(pickup["pos"], self.safe_destination(obs, pickup["pos"][:2])) < 2]
        pickup = min(usable, key=lambda p: (p["kind"] == "mortar", distance(pos, p["pos"])), default=None)
        return tuple(pickup["pos"][:2]) if pickup else None

    def farm_along_route(self, obs, goal):
        pos = obs["self"]["pos"]
        distance_to_goal = distance(pos, goal)
        if distance_to_goal < 2 or obs["self"]["cubes"] >= self.config["cubes"]["stat_cap"]:
            return None
        nx, ny = (goal[0] - pos[0]) / distance_to_goal, (goal[1] - pos[1]) / distance_to_goal
        drones = [actor for actor in obs["mechs"] if actor["kind"] == "npc"]
        if len(drones) != 1:
            drones = []
        candidates = []
        for item in (*obs["crates"], *drones):
            x, y, z = item["pos"]
            dx, dy = x - pos[0], y - pos[1]
            if (abs(z - pos[2]) < 6 and -3 <= dx * nx + dy * ny <= distance_to_goal + 3
                    and abs(dx * ny - dy * nx) < 15 and math.hypot(dx, dy) < 75):
                candidates.append(item)
        return min(candidates, key=lambda item: distance(pos, item["pos"]), default=None)

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        pos = me["pos"]
        now = obs["t"]
        enemies = sorted((a for a in obs["mechs"] if a["kind"] == "mech"),
                         key=lambda a: distance(pos, a["pos"]))
        self.update_situation(obs)
        self.observe_velocity(now, enemies)
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
            action["pick"] = self.upgrade_choice(me, obs["offer"], near_dist)
        loot = self.loot(obs, enemies, action)
        zone = obs["zone"]
        zone_goal = self.safe_destination(obs, pos[:2])
        urgent_zone = not me["in_zone"] or distance(pos, zone_goal) > me["stats"]["walk_speed"] * max(
            2, zone["shrink_end"] - now - 7
        )

        goal = zone_goal
        quiet = False
        rushing = False
        recover_covered = (self.recovering and me["grounded"] and not recently_hit
                           and self.covered_for_recovery(me, enemies, tactics))
        if not urgent_zone:
            resources = None
            rush = (self.rush_weapon(obs, loot) if not disengage and not recently_hit and near_dist >= 80
                    else None)
            if not self.pvp and not disengage and not recently_hit and near_dist > 42:
                resources = self.resource_goal(obs, loot, enemies)
            drops = [d for d in obs["drops"] if d["lands_in"] == 0 and d["holder"] in (None, me["id"])
                     and hp < 0.78 and not enemies and not recently_hit
                     and distance(pos, d["pos"]) < 42]
            if drops:
                goal = min(drops, key=lambda d: distance(pos, d["pos"]))["pos"][:2]
            elif recover_covered:
                goal = zone_goal
            elif threat_bearing is not None and (disengage or (self.pvp and target is None and near_dist < 70)):
                away = threat_bearing + math.pi + 0.33 * self.weave_side(now)
                goal = (pos[0] + 40 * math.cos(away), pos[1] + 40 * math.sin(away))
            elif rush is not None:
                goal = rush
                rushing = True
                farm_target = self.farm_along_route(obs, goal)
                if farm_target is not None:
                    target = farm_target
            elif resources is not None:
                goal = resources
            elif target is not None:
                goal = self.kite_goal(me, target, tactics, now)
            elif not disengage:
                cubes = [c for c in obs["cubes"] if abs(c["pos"][2] - pos[2]) < 4]
                cap = self.config["cubes"]["stat_cap"] if not self.pvp else 6
                if self.pvp and cubes and me["cubes"] < cap and not active_enemies:
                    goal = min(cubes, key=lambda c: distance(pos, c["pos"]))["pos"][:2]
                elif self.pvp and None in me["weapons"] and loot and not active_enemies:
                    goal = min(loot, key=lambda p: distance(pos, p["pos"]))["pos"][:2]
                elif not enemies and me["cubes"] < cap and not recently_hit:
                    farm = [a for a in (*obs["crates"], *obs["mechs"])
                            if a.get("kind") != "mech" and distance(pos, a["pos"]) < (60 if self.pvp else 120)]
                    target = min(farm, key=lambda a: distance(pos, a["pos"]), default=None)
                    if target is not None and distance(pos, target["pos"]) > 28:
                        goal = target["pos"][:2]
                elif self.pvp and near_dist > 70:
                    quiet = True
        if distance(pos, goal) < 2 and target is None and not disengage and not self.pvp and not rushing:
            goal = self.scout_goal(obs)
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
        roof_needed = (not self.pvp and not recover_covered and near_dist < 110
                       and (self.recovering or len(active_enemies) >= 2))
        climbing = self.roof_escape(obs, active_enemies, action, roof_needed, urgent_zone)
        if climbing:
            target = None
        else:
            self.move(me, goal, action, dodge, urgent_zone or (disengage and near_dist < 95),
                      quiet and dodge is None and not urgent_zone, cover_from)
            if (rushing and dodge is None and me["grounded"] and distance(pos, goal) > 2 * self.pickup_radius
                    and math.hypot(*action.get("move", (0, 0))) > 0.25):
                action["boost"] = me["energy"] - me["stats"]["boost_drain"] * self.turn_step >= 30

        if threat_bearing is None:
            self.scan = math.remainder(self.scan + 0.055, math.tau)
            sound = self.sound_direction(obs)
            look = self.scan if sound is None else sound
            if sound is not None:
                target = None
        else:
            self.sound_until = -math.inf
            look = threat_bearing
        action["aim"] = [look, 0.0]
        self.attack(me, target, action)
        gun = next((w for w in me["weapons"] if w and w["kind"] == "gun"), None)
        if gun and not gun["reload"] and gun["ammo"] < gun["stats"]["mag"]:
            action["reload"] = gun["ammo"] == 0 or (target is None and (near_dist > 65 or disengage))
        mode = "FFA" if self.pvp else "practice"
        recovering = self.recovering and now - self.last_combat >= me["stats"]["regen_delay"]
        state = "roof" if climbing else "healing" if recovering else "retreat" if disengage else "active"
        mf.draw.label(f"{mode} | {state}")
        return action


if __name__ == "__main__":
    mf.run(Bot)
