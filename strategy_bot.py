"""Mech Royale strategy for the mechfight/1 game protocol."""
from dataclasses import dataclass
import math
from time import perf_counter

from arena_geometry import Arena, cells
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


@dataclass
class Peek:
    target: int
    cover: tuple[float, float]
    exposed: tuple[float, float]
    aim: tuple[float, float]
    began: float
    saw_charge: bool = False
    returning: bool = False


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
        self.uncovered_since = None
        self.cover_scan_cursor = 0
        self.recovery_waypoint = None
        self.pillar_memory = None
        self.pillar_memory_until = -math.inf
        self.pillar_blocker = None
        self.frame_time = 0.0
        self.collect_until = -math.inf
        self.laser_full_since = None
        self.laser_unsafe = False
        self.focus_id = None
        self.peek = None
        self.peek_precharge = False
        self.scan = hello["slot"] * math.tau / match["mechs"]
        self.sound_bearing = None
        self.sound_until = -math.inf
        self.scout_waypoint = None
        self.scout_until = -math.inf
        self.scout_turn = 0
        self.turn_side = 1 if hello["slot"] % 2 else -1
        self.weave_offset = hello["slot"] * 0.083
        self.orbit_side = self.turn_side
        self.orbit_until = -math.inf
        self.roof = None
        self.roof_phase = "approach"
        self.roof_deadline = -math.inf
        self.roof_retry = -math.inf

    def update_situation(self, obs, enemies):
        now = obs["t"]
        for event in obs["events"]:
            kind = event["type"]
            if kind in ("damaged", "hit"):
                self.last_combat = now
            if kind == "kill":
                self.kills.add(event["target"])
                self.last_combat = now
                self.collect_until = now + 3
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

    def aim(self, me, target, weapon, check_path=True):
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
        if check_path:
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

    def laser_ray_hits(self, me, target, weapon):
        kind = target.get("kind", "crate")
        dimensions = self.config[{"mech": "mech", "npc": "npc"}.get(kind, "crates")]
        scale = 1 + self.config["cubes"]["size_per_cube"] * target.get("cubes", 0) if kind == "mech" else 1
        radius, height = dimensions["radius"] * scale, dimensions["height"] * scale
        yaw, pitch = me["aim"]
        direction = math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)
        x, y, z = me["pos"]
        muzzle = x, y, z + me["stats"]["muzzle_height"]
        barrel = me["stats"]["radius"] + 0.5
        origin = tuple(p + barrel * d for p, d in zip(muzzle, direction))
        px, py, pz = (p - q for p, q in zip(origin, target["pos"]))
        dx, dy, dz = direction
        a, b = dx * dx + dy * dy, px * dx + py * dy
        c = px * px + py * py - radius * radius
        lo, hi = 0.0, weapon["stats"]["range"]
        if a > 1e-9:
            disc = b * b - a * c
            if disc < 0:
                return False
            root = math.sqrt(disc)
            lo, hi = max(lo, (-b - root) / a), min(hi, (-b + root) / a)
        elif c > 0:
            return False
        if abs(dz) > 1e-9:
            enter, leave = sorted((-pz / dz, (height - pz) / dz))
            lo, hi = max(lo, enter), min(hi, leave)
        elif not 0 <= pz <= height:
            return False
        return lo <= hi and self.arena.clear_shot(muzzle, tuple(p + lo * d for p, d in zip(origin, direction)))

    def attack(self, me, target, action):
        fire = [False, False]
        action["fire"] = fire
        if target is None:
            self.laser_full_since = None
            return
        yaw, pitch = me["aim"]
        options = []
        laser_owns_aim = False
        for slot, weapon in enumerate(me["weapons"]):
            if weapon is None:
                continue
            kind, stats = weapon["kind"], weapon["stats"]
            if kind == "lightning" and me["energy"] < stats["energy"]:
                continue
            if kind == "laser":
                charge = weapon.get("charge", 0)
                full = charge + 1e-6 >= stats["charge"]
                if full and self.laser_full_since is None:
                    self.laser_full_since = self.frame_time
                elif not full:
                    self.laser_full_since = None
                if self.laser_unsafe:
                    continue
                laser_owns_aim = charge >= min(0.50, stats["charge"])
                ready = weapon["cooldown"] <= self.turn_step
                ray_clear = full and self.laser_ray_hits(me, target, weapon)
                hold = (charge > 0 and not ray_clear and
                        (not full or self.frame_time - self.laser_full_since + 1e-6 < 1.8))
                fire[slot] = hold
                if ray_clear:
                    options.append(Shot(slot, yaw, pitch, 0.0, 0.0, 100, False))
                    continue
            solution = self.aim(me, target, weapon, check_path=False) if (
                kind == "laser" and laser_owns_aim
            ) else self.aim(me, target, weapon)
            if solution is None:
                if kind == "laser" and ready and charge == 0 and self.peek_precharge:
                    fire[slot] = True
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
                trigger = ready and charge == 0
                priority = 100 if charge else 56
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
            if kind == "laser" and laser_owns_aim:
                priority = 100
            elif not ready or (kind != "laser" and not close_to_alignment):
                priority -= 70
            options.append(Shot(slot, desired_yaw, desired_pitch, yaw_margin,
                                pitch_margin, priority, trigger))
        if options:
            primary = max(options, key=lambda shot: shot.priority)
            if laser_owns_aim and me["weapons"][primary.slot]["kind"] != "laser":
                return
            action["aim"] = [primary.yaw, primary.pitch]
            for shot in options:
                if shot.trigger and (
                    shot is primary or (
                        me["weapons"][shot.slot]["kind"] != "laser"
                        and shot.aligned(primary.yaw, primary.pitch)
                    )
                ):
                    fire[shot.slot] = True

    def mortar_impact(self, shot, gravity):
        x, y, z = shot["pos"]
        vx, vy, vz = shot["vel"]

        def height_above_ground(t):
            return z + vz * t - gravity * t * t / 2 - self.arena.ground(x + vx * t, y + vy * t)

        if height_above_ground(0) <= 0:
            return None
        for step in range(1, 21):
            hi = step / 10
            if height_above_ground(hi) > 0:
                continue
            lo = (step - 1) / 10
            for _ in range(8):
                mid = (lo + hi) / 2
                if height_above_ground(mid) > 0:
                    lo = mid
                else:
                    hi = mid
            return hi, (x + vx * hi, y + vy * hi)
        return None

    def projectile_threat(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        vx, vy, vz = me["vel"]
        radius = me["stats"]["radius"]
        best = None
        landing = None
        mortar = self.config["weapons"]["mortar"]
        for shot in obs["projectiles"]:
            px, py, pz = shot["pos"]
            sx, sy, sz = shot["vel"]
            dx, dy = px - x, py - y
            shell = shot["kind"] == "mortar"
            if shell:
                speed = sx * sx + sy * sy
                closest_t = max(0, min(2, -(dx * sx + dy * sy) / speed)) if speed else 0
                if math.hypot(dx + sx * closest_t, dy + sy * closest_t) <= 9.5:
                    impact = self.mortar_impact(shot, mortar["gravity"])
                    if impact is not None and distance((x, y), impact[1]) <= 9.5:
                        if landing is None or impact[0] < landing[0]:
                            escape = bearing(impact[1], (x, y)) if distance((x, y), impact[1]) > 0.2 else (
                                math.atan2(sy, sx) + self.turn_side * math.pi / 2
                            )
                            landing = impact[0], escape
            rx, ry = sx - vx, sy - vy
            speed_squared = rx * rx + ry * ry
            if speed_squared < 1:
                continue
            t = -(dx * rx + dy * ry) / speed_squared
            if not 0 < t < (0.45 if shell else 0.85):
                continue
            closest = dx + rx * t, dy + ry * t
            clearance = radius + (mortar["splash_radius"] if shell else 1.2)
            height = pz - z + (sz - vz) * t - (mortar["gravity"] * t * t / 2 if shell else 0)
            if math.hypot(*closest) >= clearance or not -0.5 <= height <= me["stats"]["height"] + 0.5:
                continue
            side = 1 if ry * closest[0] - rx * closest[1] >= 0 else -1
            dodge = math.atan2(rx * side, -ry * side)
            if best is None or t < best[0]:
                best = t, dodge
        if landing is not None:
            return landing[1]
        if best is not None:
            return best[1]
        return None

    def planning_circle(self, obs):
        zone = obs["zone"]
        if obs["t"] >= zone["shrink_start"] - 14:
            return zone["next_center"], zone["next_radius"]
        return zone["center"], zone["radius"]

    def safe_destination(self, obs, goal, margin=16):
        center, radius = self.planning_circle(obs)
        extent = max(0, radius - margin)
        d = distance(center, goal)
        if d <= extent:
            return goal
        return tuple(c + (p - c) * extent / d for c, p in zip(center, goal))

    def orbit_goal(self, obs, goal, enemies, threat):
        me = obs["self"]
        pos, now = me["pos"], obs["t"]
        center, radius = self.planning_circle(obs)
        bounds = self.arena.bounds
        left, right = bounds["minX"] + 18, bounds["maxX"] - 18
        bottom, top = bounds["minY"] + 18, bounds["maxY"] - 18

        def margin(point):
            x, y = point[:2]
            return min(radius - distance(center, point), x - left, right - x, y - bottom, top - y)

        if margin(pos) >= 85 and margin(goal) >= 35:
            return goal
        anchor = (max(left, min(right, center[0])), max(bottom, min(top, center[1])))
        room = radius - distance(center, anchor)
        if room <= 0:
            return goal
        # Move the projected center just inside the rectangle without leaving the disk.
        middle = ((left + right) / 2, (bottom + top) / 2)
        nudge = min(0.02, room / (2 * max(1, distance(anchor, middle))))
        ox, oy = origin = tuple(a + (m - a) * nudge for a, m in zip(anchor, middle))
        offset_x, offset_y = ox - center[0], oy - center[1]
        radial = distance(origin, pos)
        if radial < 1:
            return goal

        def track(angle):
            dx, dy = math.cos(angle), math.sin(angle)
            cross_x = ((right if dx > 0 else left) - ox) / dx if abs(dx) > 1e-9 else math.inf
            cross_y = ((top if dy > 0 else bottom) - oy) / dy if abs(dy) > 1e-9 else math.inf
            projection = offset_x * dx + offset_y * dy
            cross_circle = -projection + math.sqrt(
                max(0, projection * projection + radius * radius - offset_x ** 2 - offset_y ** 2)
            )
            edge = min(cross_x, cross_y, cross_circle)
            return edge - min(35, edge * 0.45)

        def accepts(point):
            return (left <= point[0] <= right and bottom <= point[1] <= top
                    and distance(point, origin) <= radial + 8
                    and distance(point, center) <= max(0, radius - 16) and distance(pos, point) >= 4)

        out = bearing(origin, pos)
        away = threat + math.pi
        advance = min(math.pi / 2, 42 / max(track(out), 1))
        candidates = {}
        locked = now < self.orbit_until
        first = self.orbit_side if locked else self.turn_side
        sides = (first, -first)
        for side in sides:
            yaw = out + side * math.pi / 2
            dx, dy = math.cos(yaw), math.sin(yaw)
            angle = out + side * advance
            step_radius = min(track(angle), radial + 8)
            point = None
            for attempt in range(6):
                radial = step_radius - attempt * 12
                if radial <= 0:
                    break
                candidate = (ox + radial * math.cos(angle), oy + radial * math.sin(angle))
                point = self.orbit_node(me, candidate, accepts)
                if point is not None:
                    break
            if point is None:
                continue
            if locked:
                if side != self.orbit_side:
                    self.orbit_side, self.orbit_until = side, now + 6
                return point
            step = bearing(pos, point)
            walkway = sum(self.arena.walkable(me, step, reach) for reach in (8, 20, 40))
            crowd = sum(max(0, 1 - distance((pos[0] + dx * 40, pos[1] + dy * 40), enemy["pos"]) / 40)
                        for enemy in enemies)
            score = math.cos(angle_error(step, away)) + 0.55 * walkway - crowd
            candidates[side] = (score, point)
        if not candidates:
            return goal
        self.orbit_side = max(candidates, key=lambda side: candidates[side][0])
        self.orbit_until = now + 6
        return candidates[self.orbit_side][1]

    def orbit_node(self, me, point, accepts):
        gap = distance(me["pos"], point)
        if accepts(point) and self.arena.walkable(me, bearing(me["pos"], point), gap):
            return point
        return None

    def weave_side(self, now):
        phase = (now + self.weave_offset) % 1.05
        return self.turn_side if phase < 0.45 else -self.turn_side

    def kite_goal(self, me, target, tactics, now=0, zone_radius=math.inf, execute=False):
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
                    preferred.append((10 if zone_radius <= 100 and gap < stats["range"] * 0.8 else 4,
                                      min(tactics.lightning_distance, stats["range"] * 0.72)))
            elif kind == "laser":
                ideal = 120 if zone_radius > 140 else tactics.laser_distance
                preferred.append((8, min(ideal, stats["range"] * 0.66)))
            elif kind == "mortar":
                preferred.append((4, max(tactics.mortar_distance, stats["splash_radius"] * 3)))
            elif kind == "gun":
                dangerous = target.get("hp", 0) > me["hp"] or "lightning" in target.get("weapons", ())
                ideal = 105 if dangerous and zone_radius > 100 else tactics.gun_distance
                preferred.append((6, min(ideal, stats["range"] * 0.66)))
        ideal = max(preferred, default=(0, tactics.gun_distance))[1]
        if not preferred:
            ideal *= 1.4
        if execute:
            ideal = 45
        # Blend strafe and range correction smoothly across the preferred band.
        correction = max(-1, min(1, (gap - ideal) / tactics.kite_band))
        side = self.weave_side(now) if 24 < gap < 155 else self.turn_side
        yaw = facing + side * (1 - correction) * math.pi / 2
        return pos[0] + 32 * math.cos(yaw), pos[1] + 32 * math.sin(yaw)

    def recovery_sightlines(self, me, enemy):
        pos = me["pos"]
        return (
            (self.mech_point(enemy, self.config["mech"][height]),
             (*pos[:2], pos[2] + me["stats"][height]))
            for height in ("muzzle_height", "eye_height")
        )

    def recovery_exposed(self, me, enemy):
        return any(not self.arena.solid_between(start, end)
                   for start, end in self.recovery_sightlines(me, enemy))

    def covered_for_recovery(self, me, enemies):
        return all(distance(me["pos"], enemy["pos"]) > 16 and not self.recovery_exposed(me, enemy)
                   for enemy in enemies)

    def pillar_goal(self, obs, enemies):
        me = obs["self"]
        pos = me["pos"]
        now = obs["t"]
        pursuers = [e for e in enemies if distance(pos, e["pos"]) <= 80]
        if pursuers:
            start, end = next(self.recovery_sightlines(me, pursuers[0]))
            nearby = dict.fromkeys(p for cell in cells(min(start[0], end[0]), min(start[1], end[1]),
                                                      max(start[0], end[0]), max(start[1], end[1]))
                                   for p in self.arena.bins.get(cell, ()))
            pillars = []
            for prism in nearby:
                if prism.kind in ("building", "bridge", "stadium") and prism.intersects(start, end):
                    left, bottom, right, top = prism.bounds
                    center = ((left + right) / 2, (bottom + top) / 2)
                    pillars.append((distance(pos, center), center, prism))
            self.pillar_blocker = min(pillars, key=lambda item: item[0])[1:] if pillars else None
            self.pillar_memory = tuple(sum(e["pos"][i] for e in pursuers) / len(pursuers) for i in range(2))
            self.pillar_memory_until = now + 6
        elif self.pillar_blocker is not None and now < self.pillar_memory_until:
            sound = min((s for s in obs["sounds"] if s["dist"] == "near" and s["age"] <= 0.6
                         and s["kind"] in ("step", "boost", "laser_charge")),
                        key=lambda s: s["age"], default=None)
            if sound is not None:
                angle = sound["bearing"]
                self.pillar_memory = (pos[0] + 30 * math.cos(angle), pos[1] + 30 * math.sin(angle))
        else:
            return None
        if self.pillar_blocker is None:
            return None
        center, pillar = self.pillar_blocker
        centroid = self.pillar_memory
        if distance(center, centroid) < 1 or distance(center, pos) < 1:
            return None
        away = bearing(centroid, center)
        radial = bearing(center, pos)
        error = angle_error(away, radial)
        if abs(error) <= 0.12:
            return None
        tangent = radial + math.copysign(math.pi / 2, error)
        safe_goal, best_error = None, abs(error) - 0.03
        for offset in (0, 28, -28, 56, -56, 82, -82):
            yaw = tangent + math.radians(offset)
            dx, dy = math.cos(yaw), math.sin(yaw)
            point = (pos[0] + dx * 6, pos[1] + dy * 6)
            gap = abs(angle_error(away, bearing(center, point)))
            if (gap >= best_error or distance(point, self.safe_destination(obs, point)) > 0.1
                    or not self.arena.walkable(me, yaw, 6)):
                continue
            for x, y in ((pos[0] + dx * 3, pos[1] + dy * 3), point):
                z = self.arena.ground(x, y)
                next_me = dict(me, pos=(x, y, z))
                if (abs(z - pos[2]) > me["stats"]["step_up"]
                        or (pursuers and not all(pillar.intersects(a, b)
                                for a, b in self.recovery_sightlines(next_me, pursuers[0])))
                        or not self.covered_for_recovery(next_me, enemies)):
                    break
            else:
                safe_goal, best_error = point, gap
        boost = bool(pursuers and distance(pos, pursuers[0]["pos"]) < 28 and abs(error) > 0.45
                     and me["energy"] > 35)
        return (safe_goal, boost) if safe_goal is not None else None

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

    def weapon_order(self, radius):
        if radius <= 100:
            return {"mortar": 1, "gun": 2, "laser": 3, "lightning": 4}
        return {"mortar": 1, "gun": 2, "lightning": 3, "laser": 4}

    def replacement_slot(self, weapons, kind, order, zone_radius=math.inf, enemy_distance=math.inf):
        eligible = [
            i for i, old in enumerate(weapons)
            if order[kind] > order[old["kind"]] and not old.get("charge", 0)
            and (old["kind"] != "gun"
                 or ({kind, weapons[1 - i]["kind"]} == {"laser", "lightning"}
                     and (zone_radius <= 100 or enemy_distance < 55)))
        ]
        return min(eligible, key=lambda i: order[weapons[i]["kind"]], default=None)

    def loot(self, obs, enemies, action, opening=False):
        me = obs["self"]
        pos, weapons = me["pos"], me["weapons"]
        reach = self.config["loot"]["pickup_radius"]
        held = {weapon["kind"] for weapon in weapons if weapon}
        order = self.weapon_order(obs["zone"]["radius"])
        laser_threat = any("laser" in enemy.get("weapons", ()) for enemy in enemies)
        mortar_slot = next((i for i, weapon in enumerate(weapons)
                            if weapon and weapon["kind"] == "mortar"), None)
        candidates = [p for p in obs["pickups"]
                      if p["kind"] not in held and abs(p["pos"][2] - pos[2]) <= 4
                      and not (laser_threat and p["kind"] == "mortar")]
        candidates.sort(key=lambda item: (-order[item["kind"]], distance(pos, item["pos"])))
        if laser_threat and mortar_slot is not None:
            action["drop"] = mortar_slot
            return candidates
        near = min((distance(pos, enemy["pos"]) for enemy in enemies), default=math.inf)
        for item in candidates:
            if distance(pos, item["pos"]) > reach:
                continue
            if None not in weapons:
                if enemies and not opening and not (item["kind"] == "lightning" and near < 55):
                    continue
                slot = self.replacement_slot(weapons, item["kind"], order, obs["zone"]["radius"], near)
                if slot is None:
                    continue
                action["drop"] = slot
            action["take"] = item["id"]
            break
        return candidates

    def cube_reach(self, me, below_cap):
        cubes = me["cubes"]
        if cubes < self.config["cubes"]["stat_cap"]:
            return below_cap
        return 10 if cubes == 14 else 0

    def resource_goal(self, obs, pickups, enemies, opening=False):
        me = obs["self"]
        pos = me["pos"]
        needs_weapon = None in me["weapons"]
        cube_reach = self.cube_reach(me, 43)
        options = []
        if needs_weapon or opening:
            order = self.weapon_order(obs["zone"]["radius"])
            preferred = max(order, key=order.get)
            near = min((distance(pos, enemy["pos"]) for enemy in enemies), default=math.inf)
            for pickup in pickups:
                kind = pickup["kind"]
                if needs_weapon or self.replacement_slot(me["weapons"], kind, order,
                                                         obs["zone"]["radius"], near) is not None:
                    value = 96 + 10 * order[kind] + (40 if kind == preferred else 0)
                    options.append((pickup["pos"], value, 140 if opening else 105))
        if cube_reach:
            for cube in obs["cubes"]:
                if abs(cube["pos"][2] - pos[2]) <= 4:
                    options.append((cube["pos"], 65, cube_reach))

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

    def opening_farm(self, obs, enemies, goal, finishing_only=False):
        pos = obs["self"]["pos"]
        nx, ny = (goal[0] - pos[0], goal[1] - pos[1]) if goal is not None else (0, 0)
        length = math.hypot(nx, ny)
        candidates = []
        for item in (*obs["crates"], *(a for a in obs["mechs"] if a["kind"] == "npc")):
            x, y, z = item["pos"]
            dx, dy = x - pos[0], y - pos[1]
            gap = math.hypot(dx, dy)
            if (gap > 110 or abs(z - pos[2]) > 6 or
                    (finishing_only and (gap >= 45 or item.get("hp", math.inf) > 140))):
                continue
            if any(distance(item["pos"], enemy["pos"]) < 38 for enemy in enemies):
                continue
            if length > 2 and (abs(dx * ny - dy * nx) > 21 * length
                               or not -5 * length <= dx * nx + dy * ny <= (length + 12) * length):
                continue
            candidates.append((gap + item.get("hp", 300) * 0.08, item))
        return min(candidates, key=lambda option: option[0])[1] if candidates else None

    def farm_route_safe(self, pos, goal, enemies):
        dx, dy = goal[0] - pos[0], goal[1] - pos[1]
        length_squared = dx * dx + dy * dy
        for enemy in enemies:
            ex, ey = enemy["pos"][0] - pos[0], enemy["pos"][1] - pos[1]
            fraction = max(0, min(1, (ex * dx + ey * dy) / length_squared)) if length_squared else 0
            if math.hypot(ex - fraction * dx, ey - fraction * dy) < 80:
                return False
        return True

    def midgame_farm(self, obs, enemies, goal):
        me = obs["self"]
        pos = me["pos"]
        heading = bearing(pos, goal) if distance(pos, goal) >= 2 else None
        candidates = [
            item for item in (*obs["crates"], *(a for a in obs["mechs"] if a["kind"] == "npc"))
            if distance(pos, item["pos"]) <= 38 and abs(item["pos"][2] - pos[2]) <= 6
        ]
        candidates.sort(key=lambda item: distance(pos, item["pos"]) + 0.03 * item.get("hp", 300)
                        - (8 if item.get("kind") == "npc" else 0))
        farm = next((item for item in candidates
                     if any(self.aim(me, item, w) is not None for w in me["weapons"] if w)), None)
        cubes = [
            cube["pos"] for cube in obs["cubes"]
            if distance(pos, cube["pos"]) <= 38 and abs(cube["pos"][2] - pos[2]) <= 4
            and (heading is None or abs(angle_error(bearing(pos, cube["pos"]), heading)) <= math.pi / 4)
            and distance(cube["pos"], self.safe_destination(obs, cube["pos"][:2])) <= 1
            and self.farm_route_safe(pos, cube["pos"], enemies)
        ]
        return farm, min(cubes, key=lambda point: distance(pos, point), default=None)

    def can_execute(self, me, target, enemies):
        if target is None or me["hp"] <= 600:
            return False
        hp = target.get("hp", math.inf)
        if not (hp < 550 or hp < 0.38 * target.get("max_hp", 1400)):
            return False
        blockers = [
            enemy for enemy in enemies
            if enemy["id"] != target["id"] and enemy.get("hp", math.inf) > 650
            and min(distance(me["pos"], enemy["pos"]), distance(target["pos"], enemy["pos"])) < 55
        ]
        return not blockers or (
            distance(me["pos"], target["pos"]) < 70 and hp < 350
            and self.isolated(me, me["pos"], blockers)
        )

    def mech_point(self, enemy, height):
        x, y, z = enemy["pos"]
        scale = 1 + self.config["cubes"]["size_per_cube"] * enemy.get("cubes", 0)
        return x, y, z + height * scale

    def select_target(self, me, candidates):
        if not candidates:
            self.focus_id = None
            return None
        if len(candidates) == 1:
            self.focus_id = candidates[0]["id"]
            return candidates[0]
        pos = me["pos"]
        muzzle = (*pos[:2], pos[2] + me["stats"]["muzzle_height"])
        mech = self.config["mech"]
        reach = max((w["stats"].get("range", 0) for w in me["weapons"] if w
                     and w["kind"] != "mortar"), default=0)

        def score(enemy):
            gap = distance(pos, enemy["pos"])
            hp = enemy.get("hp", 1400)
            shot = gap <= reach and self.arena.clear_shot(muzzle, self.mech_point(enemy, mech["height"] / 2))
            sticky = 35 if enemy["id"] == self.focus_id and shot else 0
            return hp / 12 + 0.42 * gap + (0 if shot else 65) - sticky

        target = min(candidates, key=score)
        self.focus_id = target["id"]
        return target

    def isolated(self, me, point, others):
        """Test incoming lines at torso and muzzle height against other opponents."""
        muzzle_height = self.config["mech"]["muzzle_height"]
        return all(
            distance(point, enemy["pos"]) > 25
            and all(self.arena.solid_between(self.mech_point(enemy, muzzle_height),
                                            (*point[:2], point[2] + height))
                    for height in (me["stats"]["height"] / 2, me["stats"]["muzzle_height"]))
            for enemy in others
        )

    def threat_sound(self, obs):
        now = obs["t"]
        distance_order = {"near": 3, "mid": 2, "far": 1}
        danger_order = {"laser_charge": 4, "gun": 3, "boost": 2, "step": 1}
        candidates = [s for s in obs["sounds"] if s["kind"] in danger_order and s["age"] <= 0.6
                      and (s["dist"] == "near" or s["kind"] in ("gun", "laser_charge"))]
        if candidates:
            sound = max(candidates, key=lambda s: (distance_order.get(s["dist"], 0),
                                                   danger_order[s["kind"]], -s["age"]))
            self.sound_bearing = sound["bearing"]
            self.sound_until = now + 0.65 - sound["age"]
        return self.sound_bearing if now <= self.sound_until else None

    def choose_upgrade(self, me, offer):
        held = {weapon["kind"] for weapon in me["weapons"] if weapon}

        def priority(choice):
            key = choice["id"]
            if key == "armor":
                return 100
            kind = key.split("_", 1)[0]
            if kind in ("laser", "gun") and kind in held:
                return 65 if kind == "laser" else 60
            if key in ("servos", "reactor", "jets", "capacitor"):
                return 45
            if key in ("nanites", "plating"):
                return 35
            if key == "sensors":
                return 20
            return 0

        choices = offer["choices"]
        return max(range(len(choices)), key=lambda index: priority(choices[index]))

    def choose_supply(self, obs, enemies):
        me = obs["self"]
        pos = me["pos"]
        for drop in sorted(obs["drops"], key=lambda d: distance(pos, d["pos"])):
            gap = distance(pos, drop["pos"])
            if drop["lands_in"] >= 8 or gap >= 180:
                continue
            center = drop["pos"][:2]
            if distance(center, self.safe_destination(obs, center)) > 0.25:
                continue
            nearby = [e for e in enemies if distance(center, e["pos"]) <= 110]
            exposed = (nearby and not (len(nearby) == 1 and distance(center, nearby[0]["pos"]) > 65
                                       and self.isolated(me, drop["pos"], nearby)))
            if not exposed and drop["holder"] in (None, me["id"]):
                return drop, None, pos[:2] if gap <= drop["radius"] - 1 else center
            if gap > 80:
                continue
            contest = next((e for e in nearby if e["id"] == drop["holder"]),
                           min(nearby, key=lambda e: distance(center, e["pos"]), default=None))
            yaw = bearing(center, pos) if gap > 0.1 else me["aim"][0] + math.pi
            edge = drop["radius"] + 7
            fallback = None
            for offset in (0, 45, -45, 90, -90, 135, -135, 180):
                angle = yaw + math.radians(offset)
                point = (center[0] + edge * math.cos(angle), center[1] + edge * math.sin(angle))
                if distance(point, self.safe_destination(obs, point)) > 0.25:
                    continue
                fallback = fallback or point
                ground = self.arena.ground(*point)
                if self.isolated(me, (*point, ground), nearby):
                    return drop, contest, point
            if gap <= edge and fallback is not None:
                return drop, contest, fallback
        return None

    def corner_position(self, obs, target, others, weapon):
        me = obs["self"]
        x, y, z = me["pos"]
        toward = bearing(me["pos"], target["pos"])
        deadline = perf_counter() + 0.012
        best = None
        for gap in (12, 22, 32):
            for offset in (90, -90, 45, -45, 135, -135, 0, 180):
                if perf_counter() >= deadline:
                    return best[1:] if best else None
                yaw = toward + self.turn_side * math.radians(offset)
                ex, ey = x + gap * math.cos(yaw), y + gap * math.sin(yaw)
                ground = self.arena.ground(ex, ey)
                point = (ex, ey, ground)
                if (abs(ground - z) > me["stats"]["step_up"]
                        or distance(point, self.safe_destination(obs, point[:2])) > 1
                        or not self.arena.walkable(me, yaw, gap)
                        or not self.isolated(me, point, others)):
                    continue
                solution = self.aim(dict(me, pos=point), target, weapon)
                if solution is None:
                    continue
                score = gap + abs(distance(point, target["pos"]) - distance(me["pos"], target["pos"])) * 0.25
                if best is None or score < best[0]:
                    best = score, point[:2], solution[:2]
            if best is not None:
                break
        return best[1:] if best else None

    def recovery_cover(self, obs, enemies, away):
        me = obs["self"]
        x, y, z = me["pos"]
        slope = math.tan(math.radians(me["stats"]["max_slope_deg"]))

        def usable(point, gap, yaw):
            height = self.arena.ground(*point)
            if (distance(point, self.safe_destination(obs, point)) > 1
                    or abs(height - z) > gap * slope):
                return False
            landing = dict(me, pos=(*point, height))
            return (self.covered_for_recovery(landing, enemies)
                    and self.arena.walkable(landing, yaw, me["stats"]["radius"]))

        deadline = perf_counter() + 0.003
        if self.recovery_waypoint is not None:
            point = self.recovery_waypoint
            gap = distance(me["pos"], point)
            if 2 <= gap <= 32 and usable(point, gap, bearing(me["pos"], point)):
                return point
            self.recovery_waypoint = None
        offsets = (0, 23, -23, 46, -46, 69, -69, 92, -92, 115, -115)
        for index in range(self.cover_scan_cursor, 3 * len(offsets)):
            if index > self.cover_scan_cursor and perf_counter() >= deadline:
                self.cover_scan_cursor = index
                return None
            ring, angle = divmod(index, len(offsets))
            gap = (12, 22, 32)[ring]
            yaw = away + self.turn_side * math.radians(offsets[angle])
            point = (x + gap * math.cos(yaw), y + gap * math.sin(yaw))
            if usable(point, gap, yaw):
                self.cover_scan_cursor = 0
                self.recovery_waypoint = point
                return point
        self.cover_scan_cursor = 0
        return None

    def cover_peek(self, obs, target, others=(), corner=None):
        me, now = obs["self"], obs["t"]
        pos = me["pos"]
        laser = next((w for w in me["weapons"] if w and w["kind"] == "laser"), None)
        if (laser is None or target is None or self.laser_unsafe or not me["grounded"]
                or not 70 < distance(pos, target["pos"]) < 180):
            return None
        if self.peek is not None and self.peek.target != target["id"]:
            self.peek = None
        if self.peek is not None and others and not self.peek.returning:
            landing = (*self.peek.exposed, self.arena.ground(*self.peek.exposed))
            if not self.isolated(me, landing, others):
                self.peek.returning = True
        if self.peek is None:
            if laser["cooldown"] > self.turn_step or laser.get("charge", 0) > 0:
                return None
            x, y, z = pos
            tx, ty, tz = target["pos"]
            eye = z + me["stats"]["muzzle_height"]
            if not self.arena.solid_between((x, y, eye), (tx, ty, tz + 4.5)):
                return None
            if others:
                if corner is not None:
                    self.peek = Peek(target["id"], (x, y), *corner, now)
            else:
                self.peek = self.single_peek(obs, target, laser)
        peek = self.peek
        if peek is None:
            return None
        charge = laser.get("charge", 0)
        if ((peek.saw_charge and charge == 0)
                or (not peek.saw_charge and now - peek.began > 0.6) or now - peek.began > 2.6):
            peek.returning = True
        if peek.returning:
            if distance(pos, peek.cover) < 1.8 or now - peek.began > 4:
                return None
            return peek.cover, peek.aim
        peek.saw_charge |= charge > 0
        self.peek_precharge = True
        threshold = max(laser["stats"].get("min_charge", 0.15), laser["stats"]["charge"] - 0.38)
        return (peek.exposed if charge >= threshold else peek.cover), peek.aim

    def single_peek(self, obs, target, laser):
        me = obs["self"]
        x, y, z = me["pos"]
        toward = bearing(me["pos"], target["pos"])
        for gap in (5, 8):
            for side in (self.turn_side, -self.turn_side):
                yaw = toward + side * math.pi / 2
                if not self.arena.walkable(me, yaw, gap):
                    continue
                point = (x + gap * math.cos(yaw), y + gap * math.sin(yaw))
                if distance(point, self.safe_destination(obs, point)) > 1:
                    continue
                ground = self.arena.ground(*point)
                if abs(ground - z) <= me["stats"]["step_up"]:
                    solution = self.aim(dict(me, pos=[*point, ground]), target, laser)
                    if solution:
                        return Peek(target["id"], (x, y), point, solution[:2], obs["t"])
        return None

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
        self.frame_time = now
        self.peek_precharge = False
        enemies = sorted((a for a in obs["mechs"] if a["kind"] == "mech"),
                         key=lambda a: distance(pos, a["pos"]))
        self.update_situation(obs, enemies)
        hp = me["hp"] / me["max_hp"]
        tactics = self.free_for_all if self.pvp else self.practice
        if hp < tactics.retreat_hp:
            self.recovering = True
        if hp >= tactics.recover_hp:
            self.recovering = False
        if not self.recovering:
            self.pillar_memory = self.pillar_blocker = None
            self.pillar_memory_until = -math.inf

        nearest = enemies[0] if enemies else None
        near_dist = distance(pos, nearest["pos"]) if nearest else math.inf
        self.laser_unsafe = any(distance(pos, e["pos"]) <= 45 and "lightning" in e.get("weapons", ())
                                for e in enemies)
        recently_hit = now - self.last_damage < 2
        threat_bearing = bearing(pos, nearest["pos"]) if nearest else (
            self.last_damage_bearing if recently_hit else None
        )
        active_enemies = [e for e in enemies if distance(pos, e["pos"]) < 115]
        outnumbered = len(active_enemies) >= 3 and near_dist > 22
        disengage = self.recovering or outnumbered
        candidates = [e for e in enemies if not self.pvp or distance(pos, e["pos"]) < 40
            or (recently_hit and abs(angle_error(bearing(pos, e["pos"]), self.last_damage_bearing)) < 0.5)
            or e.get("hp", math.inf) < 0.26 * e.get("max_hp", 1400)
            or self.other_fight_until.get(e["id"], -1) > now]
        target = self.select_target(me, candidates)
        enemy_target = target
        recover_covered = (self.recovering and me["grounded"]
                           and self.covered_for_recovery(me, enemies))
        if recover_covered:
            target = None
        if not self.recovering or recover_covered:
            self.uncovered_since = None
        elif self.uncovered_since is None:
            self.uncovered_since = now
        uncovered_for = now - self.uncovered_since if self.uncovered_since is not None else 0
        zone = obs["zone"]
        zone_goal = self.safe_destination(obs, pos[:2])
        urgent_zone = not me["in_zone"] or distance(pos, zone_goal) > me["stats"]["walk_speed"] * max(
            2, zone["shrink_end"] - now - 7
        )
        recovery_route = None
        if (not urgent_zone and self.recovering and not recover_covered
                and threat_bearing is not None and me["grounded"]):
            recovery_route = self.recovery_cover(obs, enemies, threat_bearing + math.pi)
        else:
            self.cover_scan_cursor = 0
            self.recovery_waypoint = None
        recovery_stalled = uncovered_for >= 2.5 and not self.cover_scan_cursor
        exposed_close = []
        if not urgent_zone and recovery_route is None and not self.cover_scan_cursor and uncovered_for >= 2:
            exposed_close = [e for e in enemies if distance(pos, e["pos"]) <= 45 and self.recovery_exposed(me, e)]
        fight_close = bool(exposed_close)
        if fight_close and target not in exposed_close:
            target = self.select_target(me, exposed_close)
        execute = (not self.recovering or recovery_stalled) and self.can_execute(me, target, enemies)
        quick_finish = (execute and target.get("hp", math.inf) < 350
                        and distance(pos, target["pos"]) < 70 and self.arena.clear_shot(
                            (*pos[:2], pos[2] + me["stats"]["muzzle_height"]),
                            self.mech_point(target, self.config["mech"]["height"] / 2)))

        action: mf.Action = {"tick": obs["tick"]}
        opening = (now < 70 and (None in me["weapons"] or me["cubes"] < 5)
                   and near_dist > 55 and not disengage)
        if obs["offer"]:
            action["pick"] = self.choose_upgrade(me, obs["offer"])
        loot = self.loot(obs, enemies, action, opening)
        if "drop" in action and "take" not in action:
            me = dict(me, weapons=list(me["weapons"]))
            me["weapons"][action["drop"]] = None

        goal = zone_goal
        quiet = False
        developing = False
        peek_plan = None
        corner = None
        pillar_step = None
        pillar_boost = False
        supply = None
        cube_reach = self.cube_reach(me, 42)
        kill_cubes = [cube for cube in obs["cubes"] if now < self.collect_until and near_dist >= 45
                      and distance(pos, cube["pos"]) <= cube_reach and cube_reach > 0
                      and abs(cube["pos"][2] - pos[2]) <= 4]
        if not urgent_zone:
            resources = None
            if not opening and not self.pvp and not disengage and not recently_hit and near_dist > 42:
                resources = self.resource_goal(obs, loot, enemies)
            if (not recover_covered and recovery_route is None and not self.cover_scan_cursor
                    and not fight_close and not quick_finish and not recovery_stalled):
                supply = self.choose_supply(obs, enemies)
            others = [e for e in active_enemies if target is not None and e["id"] != target["id"]]
            if (supply is None and recovery_route is None and not fight_close
                    and (not self.recovering or recovery_stalled) and len(active_enemies) >= 2
                    and target is not None and not quick_finish and not kill_cubes):
                if me["grounded"] and (self.peek is None or self.peek.target != target["id"]):
                    weapon = next((w for kind in ("laser", "gun", "lightning", "mortar")
                                   for w in me["weapons"] if w and w["kind"] == kind
                                   and not (kind == "laser" and self.laser_unsafe)), None)
                    if weapon is not None:
                        corner = self.corner_position(obs, target, others, weapon)
                peek_plan = self.cover_peek(obs, target, others, corner)
            if recover_covered:
                pillar = self.pillar_goal(obs, enemies)
                if pillar is not None:
                    pillar_step, pillar_boost = pillar
                    goal = pillar_step
            elif recovery_route is not None:
                goal = recovery_route
                execute = False
            elif fight_close:
                yaw = bearing(pos, target["pos"]) + self.weave_side(now) * math.pi / 2
                goal = (pos[0] + 24 * math.cos(yaw), pos[1] + 24 * math.sin(yaw))
                execute = False
            elif quick_finish:
                goal = self.kite_goal(me, target, tactics, now, zone["radius"], execute=True)
            elif supply is not None:
                execute = False
                _, contest, goal = supply
                if contest is not None:
                    target = contest
            elif peek_plan or corner:
                execute = False
                goal = (peek_plan or corner)[0]
            elif execute:
                goal = self.kite_goal(me, target, tactics, now, zone["radius"], execute=True)
            elif kill_cubes:
                goal = min(kill_cubes, key=lambda cube: distance(pos, cube["pos"]))["pos"][:2]
            elif threat_bearing is not None and (
                (disengage and not (recovery_stalled and target is not None))
                or (not opening and self.pvp and target is None and near_dist < 70)
            ):
                away = threat_bearing + math.pi + 0.33 * self.weave_side(now)
                goal = (pos[0] + 40 * math.cos(away), pos[1] + 40 * math.sin(away))
                goal = self.orbit_goal(obs, goal, enemies, threat_bearing)
            elif opening:
                developing = True
                resources = self.resource_goal(obs, loot, enemies, opening=True)
                farm = self.opening_farm(obs, enemies, resources)
                if resources is not None:
                    goal = resources
                elif farm is not None and distance(pos, farm["pos"]) > 23:
                    goal = farm["pos"][:2]
                elif nearest is not None and near_dist < 90:
                    away = threat_bearing + math.pi + 0.45 * self.turn_side
                    goal = (pos[0] + 55 * math.cos(away), pos[1] + 55 * math.sin(away))
                else:
                    goal = self.scout_goal(obs)
                finish = farm if target is None else self.opening_farm(obs, enemies, goal, finishing_only=True)
                if finish is not None:
                    target = finish
            elif resources is not None:
                goal = resources
            elif target is not None:
                if not disengage and len(active_enemies) <= 1:
                    peek_plan = self.cover_peek(obs, target)
                goal = peek_plan[0] if peek_plan else self.kite_goal(me, target, tactics, now, zone["radius"])
                if peek_plan is None:
                    facing = bearing(pos, target["pos"])
                    if math.cos(angle_error(bearing(pos, goal), facing)) < -1e-6:
                        goal = self.orbit_goal(obs, goal, enemies, facing)
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
        if (not urgent_zone and not disengage and supply is None and not execute
                and peek_plan is None and corner is None):
            single_gun = len([w for w in me["weapons"] if w and w["kind"] == "gun"]) == 1 and None in me["weapons"]
            laser = None
            if single_gun and near_dist >= 80:
                laser = min((p for p in loot if p["kind"] == "laser" and distance(pos, p["pos"]) <= 30
                             and distance(p["pos"], self.safe_destination(obs, p["pos"][:2])) <= 1
                             and self.farm_route_safe(pos, p["pos"], enemies)),
                            key=lambda p: distance(pos, p["pos"]), default=None)
            if laser is not None:
                goal = laser["pos"][:2]
                if distance(pos, goal) <= self.config["loot"]["pickup_radius"]:
                    action["take"] = laser["id"]
                else:
                    action.pop("take", None)
            elif (me["cubes"] < self.config["cubes"]["stat_cap"] and hp > 0.65 and near_dist > 95
                  and not recently_hit and self.farm_route_safe(pos, self.safe_destination(obs, goal), enemies)):
                farm, cube = self.midgame_farm(obs, enemies, goal)
                if farm is not None:
                    target = farm
                if cube is not None:
                    goal = cube[:2]
        if distance(pos, goal) < 2 and target is None and not disengage and not self.pvp and supply is None:
            goal = self.scout_goal(obs)
        if peek_plan is None:
            self.peek = None
        goal = self.safe_destination(obs, goal)
        # Dash back across the current circle boundary before pursuing the next circle.
        if not me["in_zone"] and distance(pos, goal) < 2:
            goal = zone["center"]
        dodge = self.projectile_threat(obs)
        if dodge is None and nearest and nearest.get("charging") and near_dist < 145:
            aim_error = angle_error(bearing(nearest["pos"], pos), nearest["aim"][0])
            if abs(aim_error) < 0.16:
                dodge = threat_bearing + self.turn_side * math.pi / 2
        if dodge is not None:
            self.peek_precharge = False
        cover_from = None
        if nearest and disengage and not urgent_zone and dodge is None:
            tx, ty, tz = nearest["pos"]
            cover_from = (tx, ty, tz + self.config["mech"]["muzzle_height"])
        escaping = disengage and not (peek_plan or corner or fight_close
                                       or (recovery_stalled and target is not None and recovery_route is None))
        if pillar_step is not None and dodge is None:
            yaw = bearing(pos, goal)
            action["move"] = [math.cos(yaw), math.sin(yaw)]
            action["boost"] = pillar_boost
        else:
            self.move(me, goal, action, dodge, urgent_zone or (escaping and near_dist < 95),
                      quiet and dodge is None and not urgent_zone, cover_from if escaping else None)
        boost_route = ((developing and distance(pos, goal) > 9)
                       or (execute and not urgent_zone and distance(pos, target["pos"]) > 50)
                       or (supply is not None and supply[1] is None and distance(pos, goal) > 24))
        if (boost_route and dodge is None and me["grounded"]
                and math.hypot(*action.get("move", (0, 0))) > 0.25):
            action["boost"] = me["energy"] - me["stats"]["boost_drain"] * self.turn_step >= 30
        if recovery_route is not None and dodge is None and "move" in action:
            action["boost"] = me["energy"] >= me["stats"]["boost_drain"] * self.turn_step

        if threat_bearing is None:
            self.scan = math.remainder(self.scan + 0.055, math.tau)
            sound = self.threat_sound(obs)
            look = bearing(pos, supply[0]["pos"]) if supply is not None else self.scan
            if sound is not None:
                look = sound
                target = None
        else:
            self.sound_until = -math.inf
            look = threat_bearing
        action["aim"] = list(peek_plan[1]) if peek_plan else [look, 0.0]
        default_aim = action["aim"]
        self.attack(me, target, action)
        if (target is not None and target.get("kind") != "mech"
                and any(distance(pos, enemy["pos"]) <= 220
                        and abs(angle_error(bearing(pos, enemy["pos"]), action["aim"][0])) > math.radians(65)
                        for enemy in enemies)):
            target = enemy_target
            action["aim"] = default_aim
            self.attack(me, target, action)
        if (escaping or urgent_zone) and threat_bearing is not None:
            shooter = next((e for e in enemies if e.get("charging") and distance(pos, e["pos"]) <= 200), None)
            front = bearing(pos, shooter["pos"]) if shooter else (
                self.last_damage_bearing if recently_hit else threat_bearing
            )
            front_arc = math.radians(self.config["mech"]["front_arc_deg"]) / 2
            if abs(angle_error(action["aim"][0], front)) >= front_arc - 0.1:
                action["aim"] = [front, 0.0]
                action["fire"] = [False, False]
        gun = next((w for w in me["weapons"] if w and w["kind"] == "gun"), None)
        if gun and not gun["reload"] and gun["ammo"] < gun["stats"]["mag"]:
            action["reload"] = gun["ammo"] == 0 or (target is None and (near_dist > 65 or disengage))
        mode = "FFA" if self.pvp else "practice"
        recovering = self.recovering and now - self.last_combat >= me["stats"]["regen_delay"]
        state = "healing" if recovering else "retreat" if escaping else "active"
        mf.draw.label(f"{mode} | {state}")
        return action


if __name__ == "__main__":
    mf.run(Bot)
