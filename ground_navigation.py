"""Precomputed 2.5D routes and observation-driven movement."""
from collections import deque
from dataclasses import dataclass
from heapq import heappop, heappush, nlargest
from itertools import islice, pairwise
import math
from time import perf_counter

STEP = 4
DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def gap(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def edge_clear(x, y, prism, radius):
    for ring in prism.rings:
        for (ax, ay), (bx, by) in pairwise(ring):
            dx, dy = bx - ax, by - ay
            length = dx * dx + dy * dy
            if length:
                t = max(0, min(1, ((x - ax) * dx + (y - ay) * dy) / length))
                if (x - ax - dx * t) ** 2 + (y - ay - dy * t) ** 2 < radius * radius:
                    return False
    return True


@dataclass(frozen=True)
class Portal:
    ground: int
    roof: int
    rise: float
    crossing: float


class NavGraph:
    def __init__(self, arena, stats):
        self.arena = arena
        border = stats["radius"] + 0.85
        self.origin = tuple(math.ceil((arena.bounds["min" + a] + border) / STEP) * STEP for a in ("X", "Y"))
        self.width, self.height = (
            math.floor((arena.bounds["max" + a] - border - origin) / STEP) + 1
            for a, origin in zip(("X", "Y"), self.origin)
        )
        self.count = self.width * self.height
        self.xyz = []
        for cell in range(self.count):
            x, y = self.xy_world(cell)
            self.xyz.append((x, y, arena.ground(x, y)))
        ground_open = bytearray([1]) * self.count
        heights = {}
        for prism in arena.prisms:
            for cell in self.cells(prism.bounds, border):
                x, y, z = self.xyz[cell]
                rise = prism.top - z
                blocks = prism.base < z + stats["height"] and rise > stats["step_up"]
                serves_roof = prism.kind in ("building", "bridge") and 6 <= rise <= 22
                if not blocks and not serves_roof:
                    continue
                inside = prism.inside(x, y)
                clear = edge_clear(x, y, prism, border)
                if blocks and (inside or not clear):
                    ground_open[cell] = 0
                if serves_roof and inside and clear and prism.top > heights.get(cell, -math.inf):
                    heights[cell] = prism.top
        self.ground_open = ground_open
        self.roofs = {}
        for cell, height in sorted(heights.items()):
            x, y, _ = self.xyz[cell]
            if any(arena.solid_between((x + dx, y + dy, height + 0.08),
                                       (x + dx, y + dy, height + stats["height"]))
                   for dx, dy in ((0, 0), (border, 0), (-border, 0), (0, border), (0, -border))):
                continue
            self.roofs[cell] = len(self.xyz)
            self.xyz.append((x, y, height))
        self.edges = [[] for _ in self.xyz]
        self.cover = [0.0] * len(self.xyz)
        self.portals = {}
        self.build_walk_edges(stats)
        self.build_portals(stats, border)

    def xy_world(self, cell):
        return (self.origin[0] + (cell % self.width) * STEP,
                self.origin[1] + (cell // self.width) * STEP)

    def cell_at(self, ix, iy):
        if 0 <= ix < self.width and 0 <= iy < self.height:
            return iy * self.width + ix
        return None

    def indices(self, point):
        return (round((point[0] - self.origin[0]) / STEP),
                round((point[1] - self.origin[1]) / STEP))

    def cells(self, bounds, margin=0):
        a, b, c, d = bounds
        x0, y0 = self.origin
        for iy in range(max(0, math.ceil((b - margin - y0) / STEP)),
                        min(self.height - 1, math.floor((d + margin - y0) / STEP)) + 1):
            for ix in range(max(0, math.ceil((a - margin - x0) / STEP)),
                            min(self.width - 1, math.floor((c + margin - x0) / STEP)) + 1):
                yield iy * self.width + ix

    def layer(self, cell, roof):
        if cell is None:
            return None
        return self.roofs.get(cell) if roof else cell if self.ground_open[cell] else None

    def build_walk_edges(self, stats):
        slope = math.tan(math.radians(stats["max_slope_deg"]))
        for layer in (False, True):
            for cell in self.roofs if layer else range(self.count):
                node = self.layer(cell, layer)
                if node is None:
                    continue
                ix, iy = cell % self.width, cell // self.width
                x, y, z = self.xyz[node]
                open_count = 0
                for dx, dy in DIRECTIONS:
                    neighbor = self.layer(self.cell_at(ix + dx, iy + dy), layer)
                    if neighbor is None:
                        continue
                    step = STEP * (math.sqrt(2) if dx and dy else 1)
                    rise = abs(self.xyz[neighbor][2] - z)
                    limit = stats["step_up"] if layer else max(stats["step_up"], slope * step)
                    if rise > limit:
                        continue
                    if dx and dy:
                        sides = (self.layer(self.cell_at(ix + dx, iy), layer),
                                 self.layer(self.cell_at(ix, iy + dy), layer))
                        if any(side is None or abs(self.xyz[side][2] - z) > limit for side in sides):
                            continue
                    if not layer and rise > stats["step_up"]:
                        nx, ny, nz = self.xyz[neighbor]
                        mid_z = self.arena.ground((x + nx) / 2, (y + ny) / 2)
                        if max(abs(mid_z - z), abs(nz - mid_z)) > max(stats["step_up"], slope * step / 2):
                            continue
                    self.edges[node].append((neighbor, step * (1 + 0.22 * rise / step)))
                    open_count += 1
                self.cover[node] = (8 - open_count) * 0.45 if not layer else min(6, open_count) * 0.5

        # Peel leaves to mark finite branches and penalize narrow connections.
        degree = [len(edges) for edges in self.edges]
        leaves = deque(i for i, count in enumerate(degree) if count == 1)
        pruned = bytearray(len(degree))
        while leaves:
            node = leaves.popleft()
            if pruned[node]:
                continue
            pruned[node] = 1
            for other, _ in self.edges[node]:
                degree[other] -= 1
                if degree[other] == 1:
                    leaves.append(other)
        for node, edges in enumerate(self.edges):
            self.edges[node] = [(other, length * (1 + (0.7 if pruned[other] else 0)
                                                   + 0.1 * max(0, 5 - len(self.edges[other]))))
                                for other, length in edges]

    def build_portals(self, stats, border):
        for cell, roof_node in self.roofs.items():
            ix, iy = cell % self.width, cell // self.width
            x, y, top = self.xyz[roof_node]
            for dx, dy in DIRECTIONS[:4]:
                next_roof = self.layer(self.cell_at(ix + dx, iy + dy), True)
                if next_roof is not None and abs(self.xyz[next_roof][2] - top) <= stats["step_up"]:
                    continue
                for stride in range(2, 5):
                    ground_node = self.layer(self.cell_at(ix + dx * stride, iy + dy * stride), False)
                    if ground_node is None:
                        continue
                    gx, gy, gz = self.xyz[ground_node]
                    rise = top - gz
                    if not 6 <= rise <= 22:
                        continue
                    if any(self.arena.solid_between((gx + ox, gy + oy, gz + 0.1),
                                                     (gx + ox, gy + oy, top + stats["height"] + 0.75))
                           for ox, oy in ((0, 0), (border, 0), (-border, 0), (0, border), (0, -border))):
                        continue
                    if any(self.arena.solid_between((gx - dy * side, gy + dx * side, top + lift),
                                                     (x - dy * side, y + dx * side, top + lift))
                           for side in (0, border, -border) for lift in (0.75, stats["height"])):
                        continue
                    step = stride * STEP
                    portal = Portal(ground_node, roof_node, rise, step)
                    self.portals[ground_node, roof_node] = portal
                    self.portals[roof_node, ground_node] = portal
                    self.edges[ground_node].append((roof_node, step + rise * 0.65))
                    self.edges[roof_node].append((ground_node, step + 3))
                    break

    def nearest(self, point, layer=None, reachable=None):
        ix, iy = self.indices(point)
        choices = []
        for y in range(iy - 2, iy + 3):
            for x in range(ix - 2, ix + 3):
                cell = self.cell_at(x, y)
                for roof in (False, True) if layer is None else (layer,):
                    node = self.layer(cell, roof)
                    if node is None:
                        continue
                    delta_z = abs(self.xyz[node][2] - point[2]) if len(point) == 3 else 0
                    if delta_z <= 4:
                        choices.append((gap(self.xyz[node], point) + 3 * delta_z, node))
        for _, node in sorted(choices):
            if reachable is None or reachable(self.xyz[node]):
                return node
        return None


def jet_energy(portal, stats):
    climb = (portal.rise + 0.75) / stats["max_rise_speed"]
    cross = math.sqrt(2 * portal.crossing / stats["air_accel"])
    return stats["jet_drain"] * (climb + cross + 0.25) + 4


class Influence:
    def __init__(self, arena, config, obs, enemies):
        self.arena, self.zone, self.now = arena, obs["zone"], obs["t"]
        self.enemies = []
        for enemy in enemies:
            x, y, z = enemy["pos"]
            size = 1 + config["cubes"]["size_per_cube"] * enemy.get("cubes", 0)
            muzzle = z + config["mech"]["muzzle_height"] * size
            eyewidth = 48 if "lightning" in enemy.get("weapons", ()) else 25
            laser = None
            if enemy.get("charging"):
                yaw, pitch = enemy["aim"]
                laser = math.cos(yaw), math.sin(yaw), math.tan(pitch)
            self.enemies.append((x, y, muzzle, eyewidth, laser, enemy["id"]))
        self.blasts = []
        gravity = config["weapons"]["mortar"]["gravity"]
        for shell in obs["projectiles"]:
            if shell["kind"] != "mortar":
                continue
            x, y, z = shell["pos"]
            vx, vy, vz = shell["vel"]
            t = 0.0
            for _ in range(2):
                height = arena.ground(x + vx * t, y + vy * t)
                t = (vz + math.sqrt(max(0, vz * vz + 2 * gravity * (z - height)))) / gravity
                t = max(0, min(t, 2.2))
            if t > 0:
                self.blasts.append((x + vx * t, y + vy * t, height))

    def zone_cost(self, point):
        zone = self.zone
        outside = gap(point, zone["center"]) - zone["radius"]
        if outside > 0:
            return 5 + min(16, outside * 0.3)
        if self.now >= zone["shrink_start"] - 9:
            return min(6, max(0, gap(point, zone["next_center"]) - zone["next_radius"]) * 0.07)
        return 0

    def risk(self, point):
        x, y, z = point
        danger = self.zone_cost(point)
        for ex, ey, muzzle, reach, laser, _ in self.enemies:
            dx, dy = x - ex, y - ey
            separation = math.hypot(dx, dy)
            danger += 5 * max(0, 1 - separation / reach)
            if laser is not None:
                ax, ay, slope = laser
                along = dx * ax + dy * ay
                if 0 < along < 280 and abs(dx * ay - dy * ax) < 5 and abs(z + 5 - muzzle - along * slope) < 7:
                    danger += 10
        for bx, by, bz in self.blasts:
            if abs(z - bz) < 9 and (bx - x) ** 2 + (by - y) ** 2 < 100:
                danger += 20
        return danger

    def exposure(self, point, own_height, target_id=None):
        x, y, z = point
        other_count, sees_target = 0, False
        for ex, ey, muzzle, _, _, eid in self.enemies:
            if not self.arena.clear_shot((ex, ey, muzzle), (x, y, z + own_height)):
                continue
            if eid == target_id:
                sees_target = True
            else:
                other_count += 1
        return other_count, sees_target


@dataclass
class Crossing:
    portal: Portal
    up: bool
    began: float
    jumped_at: float | None = None
    crossing: bool = False
    peak: float = -math.inf
    progress_at: float = -math.inf


class Navigation:
    def __init__(self, arena, config):
        self.arena, self.config = arena, config
        self.graph = NavGraph(arena, config["mech"])
        self.path = deque()
        self.destination = None
        self.plan_key = None
        self.plan_at = 0.0
        self.crossing = None
        self.banned = {}
        match = config["match"]
        self.step = match["decide_every"] / match["tick_hz"]

    def search(self, start, obs, influence, mode, goal, target, ideal):
        graph, me = self.graph, obs["self"]
        travel = mode == "travel"
        deadline = perf_counter() + 0.014
        queue = [(0.0, 0.0, start)]
        reached = {start: 0.0}
        used_energy = {start: 0.0}
        previous = {}
        risk = {}
        settled = []
        closest = start
        for_count = 1400 if travel else 720
        while queue and len(settled) < for_count:
            if len(settled) % 32 == 0 and perf_counter() >= deadline:
                break
            _, walked, node = heappop(queue)
            if walked != reached[node]:
                continue
            settled.append(node)
            if travel:
                if gap(graph.xyz[node], goal) < gap(graph.xyz[closest], goal):
                    closest = node
                if gap(graph.xyz[node], goal) < 3:
                    break
            for other, weight in graph.edges[node]:
                point = graph.xyz[other]
                if not travel and gap(me["pos"], point) > 70:
                    continue
                energy = used_energy[node]
                portal = graph.portals.get((node, other))
                if portal is not None:
                    if self.banned.get(portal, -math.inf) > obs["t"]:
                        continue
                    if other >= graph.count:
                        if travel:
                            continue
                        energy += jet_energy(portal, me["stats"])
                        if me["energy"] < energy + 15:
                            continue
                if other not in risk:
                    risk[other] = influence.risk(point)
                new_cost = walked + weight * (1 + risk[other])
                if new_cost < reached.get(other, math.inf):
                    reached[other], previous[other], used_energy[other] = new_cost, node, energy
                    heuristic = gap(point, goal) if travel else 0
                    heappush(queue, (new_cost + heuristic, new_cost, other))

        if not travel and settled:
            closest = self.choose(settled, reached, start, obs, influence, mode, target, ideal, risk)
        path = deque([closest])
        while path[0] in previous:
            path.appendleft(previous[path[0]])
        return path, closest

    def choose(self, nodes, cost, start, obs, influence, mode, target, ideal, risk):
        graph = self.graph
        me = obs["self"]
        center_height = me["stats"]["height"] / 2
        target_id = target["id"] if target else None
        ranked = {}
        scores = {}
        for node in nodes:
            x, y, z = graph.xyz[node]
            if node != start and me["in_zone"] and gap((x, y), influence.zone["center"]) > influence.zone["radius"]:
                continue
            previous = self.destination
            sticky = (12 if previous is not None and abs(graph.xyz[previous][2] - z) <= 2
                      and gap(graph.xyz[previous], (x, y)) <= 8 else 0)
            danger = risk[node] if node in risk else influence.risk((x, y, z))
            if mode == "retreat":
                separation = min((gap((x, y), enemy) for enemy in influence.enemies), default=80)
                utility = min(140, separation) * 0.62 - cost[node] * 0.48
            else:
                utility = -1.65 * abs(gap((x, y), target["pos"]) - ideal) - cost[node] * 0.5
                utility += 1.05 * min(14, gap(graph.xyz[start], (x, y)))
            utility += sticky + graph.cover[node] - 3 * danger
            if node >= graph.count:
                utility += 5
            scores[node] = utility
            # Spatial buckets keep the short list from sampling one building corner only.
            key = (x // 12, y // 12, node >= graph.count)
            if key not in ranked or utility > ranked[key][0]:
                ranked[key] = (utility, node)
        candidates = nlargest(20, ranked.values())
        required = {start, self.destination} & scores.keys()
        required.difference_update(node for _, node in candidates)
        candidates[:0] = [(scores[node], node) for node in sorted(required)]
        deadline = perf_counter() + 0.006
        best = (-math.inf, start)
        for score, node in candidates:
            others, visible = influence.exposure(graph.xyz[node], center_height, target_id)
            if mode == "retreat":
                score -= 1000 if others else 0
                score -= 16 * others
            else:
                score += 33 if visible else -28
                score -= 24 * others
            if score > best[0]:
                best = score, node
            if perf_counter() >= deadline:
                break
        return best[1]

    def drive(self, me, point, sprint=False, emergency=False, avoid=True, stop=False):
        x, y, _ = me["pos"]
        dx, dy = point[0] - x, point[1] - y
        length = math.hypot(dx, dy)
        stats = me["stats"]
        if length < 0.18:
            return {"move": [0, 0]}
        vx, vy = me["vel"][:2]
        accel = stats["ground_accel"] if me["grounded"] else stats["air_accel"]
        running = (sprint and me["grounded"] and length > 6 and
                   me["energy"] - self.step * stats["boost_drain"] >= (24 if emergency else 65))
        limit = stats["boost_speed"] if running else stats["walk_speed"]
        speed = min(limit, math.sqrt(2 * accel * max(0, length - 0.35))) if stop else limit
        ux, uy = dx / length, dy / length
        tangent = -uy * vx + ux * vy
        correction = max(-0.6 * speed, min(0.6 * speed, tangent * 0.8))
        yaw = math.atan2(uy * speed - ux * correction, ux * speed + uy * correction)
        if avoid and me["grounded"]:
            lookahead = min(length, max(2.2, speed * 0.32))
            for turn in (0, 0.42, -0.42, 0.9, -0.9):
                if self.arena.walkable(me, yaw + turn, lookahead):
                    yaw += turn
                    break
            else:
                return {"move": [0, 0]}
        magnitude = speed / limit
        return {"move": [math.cos(yaw) * magnitude, math.sin(yaw) * magnitude], "boost": running}

    def traverse(self, obs, crossing):
        me, now = obs["self"], obs["t"]
        pos, stats = me["pos"], me["stats"]
        graph = self.graph
        portal = crossing.portal
        launch, landing = graph.xyz[portal.ground], graph.xyz[portal.roof]
        destination = landing if crossing.up else launch
        if (crossing.crossing and me["grounded"] and abs(pos[2] - destination[2]) < 1.1
                and gap(pos, destination) < 3.2):
            return None
        if now - crossing.began > 8:
            self.banned[portal] = now + 6
            return None
        if not crossing.up:
            crossing.crossing = True
            return self.drive(me, launch, avoid=False)

        if crossing.jumped_at is None:
            if me["energy"] < jet_energy(portal, stats) + 15:
                self.banned[portal] = now + 3
                return None
            if now - crossing.began >= 0.3 and (not me["grounded"] or gap(pos, launch) > 2.2):
                self.banned[portal] = now + 6
                return None
            if gap(pos, launch) > 2.2:
                return self.drive(me, launch)
            if not me["grounded"]:
                return self.drive(me, launch, avoid=False, stop=True)
            radius = stats["radius"] + 0.35
            nx = (launch[1] - landing[1]) / portal.crossing
            ny = (landing[0] - launch[0]) / portal.crossing
            vx, vy = me["vel"][:2]
            tangent = vx * nx + vy * ny
            direction = 1 if tangent >= 0 else -1
            takeoff_move = [nx * direction, ny * direction]
            inward = max(0, vx * ny - vy * nx)
            drift = min(3, inward ** 2 / (2 * stats["air_accel"]))
            check_positions = (pos[:2], (pos[0] + ny * drift, pos[1] - nx * drift))
            if any(self.arena.solid_between((x + dx, y + dy, pos[2] + 0.12),
                                            (x + dx, y + dy, landing[2] + stats["height"] + 0.75))
                   for x, y in check_positions
                   for dx, dy in ((radius, 0), (-radius, 0), (0, radius), (0, -radius))):
                if now - crossing.began + 1e-6 >= 0.3:
                    self.banned[portal] = now + 6
                    return None
                return {"move": takeoff_move}
            for offset in (-radius, radius):
                ox, oy = nx * offset, ny * offset
                if any(self.arena.solid_between((launch[0] + ox, launch[1] + oy, landing[2] + lift),
                                                (landing[0] + ox, landing[1] + oy, landing[2] + lift))
                       for lift in (0.75, stats["height"])):
                    self.banned[portal] = now + 6
                    return None
            crossing.jumped_at = now
            crossing.progress_at = now
            crossing.peak = pos[2]
            # Keep tangential motion while avoiding an input directly into the eave.
            return {"move": takeoff_move, "jump": True, "boost": True}

        if me["grounded"] and not crossing.crossing:
            if now - crossing.jumped_at + 1e-6 >= 0.3:
                self.banned[portal] = now + 6
                return None
            vx, vy = me["vel"][:2]
            length = math.hypot(vx, vy)
            return {"move": [vx / length, vy / length] if length > 0.2 else [0, 0],
                    "jump": True, "boost": True}

        if me["energy"] < 1 and pos[2] < landing[2] + 0.7:
            self.banned[portal] = now + 6
            return None
        if not crossing.crossing and pos[2] <= landing[2] + 0.72:
            if pos[2] > crossing.peak + 0.12:
                crossing.peak = pos[2]
                crossing.progress_at = now
            if now - crossing.progress_at > 0.55:
                self.banned[portal] = now + 6
                return None
            action = self.drive(me, launch, avoid=False, stop=True) if gap(pos, launch) > 0.55 else {"move": [0, 0]}
            action["boost"] = not me["grounded"]
            return action
        if crossing.crossing and pos[2] < landing[2] - 0.5:
            self.banned[portal] = now + 6
            return None
        crossing.crossing = True
        action = self.drive(me, landing, avoid=False, stop=True)
        action["boost"] = not me["grounded"] and gap(pos, landing) > 1.25 and me["energy"] >= 1
        return action

    def follow_point(self, me):
        graph = self.graph
        last = self.path[0]
        for node in islice(self.path, 1, 5):
            if (last, node) in graph.portals:
                break
            point = graph.xyz[node]
            if (gap(me["pos"], point) > 12 or abs(point[2] - me["pos"][2]) > 2.5
                    or not self.reachable(me, point)):
                break
            last = node
        return graph.xyz[last]

    def strafe_point(self, obs, target):
        me, zone = obs["self"], obs["zone"]
        x, y, _ = me["pos"]
        tx, ty, _ = target["pos"]
        side = -1 if int(obs["t"] / 0.6 + 1e-6) % 2 else 1
        tangent = math.atan2(ty - y, tx - x) + side * math.pi / 2
        for yaw in (tangent, tangent + math.pi):
            point = (x + 8 * math.cos(yaw), y + 8 * math.sin(yaw))
            if me["in_zone"] and gap(point, zone["center"]) > zone["radius"]:
                continue
            if not me["grounded"] or self.reachable(me, point):
                return point
        return None

    def emergency_direction(self, obs, influence):
        me = obs["self"]
        x, y, z = me["pos"]
        vx, vy, vz = me["vel"]
        danger = None
        for shot in obs["projectiles"]:
            px, py, pz = shot["pos"]
            sx, sy, sz = shot["vel"]
            rx, ry = sx - vx, sy - vy
            speed2 = rx * rx + ry * ry
            if speed2 < 1:
                continue
            dx, dy = px - x, py - y
            t = -(dx * rx + dy * ry) / speed2
            if not 0 < t < 0.35:
                continue
            col_x, col_y = dx + t * rx, dy + t * ry
            height = pz - z + (sz - vz) * t
            if shot["kind"] == "mortar":
                height -= self.config["weapons"]["mortar"]["gravity"] * t * t / 2
            if math.hypot(col_x, col_y) < me["stats"]["radius"] + 0.65 and 0 <= height <= me["stats"]["height"]:
                if danger is None or t < danger[0]:
                    side = 1 if ry * col_x - rx * col_y >= 0 else -1
                    danger = t, math.atan2(rx * side, -ry * side)
        if danger is None:
            for ex, ey, muzzle, _, laser, _ in influence.enemies:
                if laser is None:
                    continue
                ax, ay, slope = laser
                along = (x - ex) * ax + (y - ey) * ay
                cross = (x - ex) * ay - (y - ey) * ax
                if (0 < along < 145 and abs(cross) < 4.5 and abs(z + 5 - muzzle - along * slope) < 6
                        and self.arena.clear_shot((ex, ey, muzzle), (x, y, z + 5))):
                    danger = 0, math.atan2(ax, -ay) + (math.pi if cross > 0 else 0)
                    break
        if danger is None:
            return None
        yaw = danger[1]
        candidates = []
        for angle in (yaw, yaw + math.pi, yaw + 0.55, yaw - 0.55):
            if not me["grounded"] or self.arena.walkable(me, angle, 5):
                point = (x + 8 * math.cos(angle), y + 8 * math.sin(angle), z)
                candidates.append((influence.risk(point), point[:2]))
        return min(candidates, default=(0, None))[1]

    def control(self, obs, enemies, goal, mode, target=None, ideal=None, recently_hit=False, urgent=False):
        me, now = obs["self"], obs["t"]
        pos = me["pos"]
        influence = Influence(self.arena, self.config, obs, enemies)
        if self.crossing is not None:
            action = (None if urgent and self.crossing.up and self.crossing.jumped_at is None
                      else self.traverse(obs, self.crossing))
            if action is not None:
                return action, True
            self.crossing = None
            self.path.clear()
            self.plan_at = 0
        near = gap(pos, enemies[0]["pos"]) if enemies else math.inf
        dodge = self.emergency_direction(obs, influence)
        if dodge:
            return self.drive(me, dodge, sprint=True, emergency=near < 20), False
        if mode == "hold":
            self.path.clear()
            return {}, False
        if mode == "travel" and gap(pos, goal) < 1.5:
            self.path.clear()
            return {}, False
        if mode == "retreat" and not recently_hit and near > 26 and me["grounded"] and gap(pos, goal) < 2:
            count, _ = influence.exposure(pos, me["stats"]["height"] / 2)
            if count == 0:
                self.path.clear()
                return {}, False
        key = (mode, target["id"] if target else None)
        if key != self.plan_key:
            self.plan_key = key
            self.path.clear()
            self.destination = None
            self.plan_at = 0
        lost = self.path and gap(pos, self.graph.xyz[self.path[0]]) > 19
        if me["grounded"] and (now >= self.plan_at or lost):
            self.plan_at = now + (0.55 if mode != "travel" else 0.8)
            start = self.graph.nearest(pos, reachable=lambda point: self.reachable(me, point))
            if start is not None:
                new_path, self.destination = self.search(start, obs, influence, mode, goal, target, ideal)
                self.path = new_path
        while len(self.path) >= 2:
            here, following = self.path[0], self.path[1]
            here_pos = self.graph.xyz[here]
            portal = self.graph.portals.get((here, following))
            if portal is not None:
                arrival = 2.2 if here == portal.ground else 1.25
                if gap(pos, here_pos) > arrival:
                    break
                self.crossing = Crossing(portal, here == portal.ground, now)
                action = self.traverse(obs, self.crossing)
                if action is not None:
                    return action, True
                self.crossing = None
                self.path.clear()
                break
            following_pos = self.graph.xyz[following]
            ahead = (gap(pos, following_pos) + 1 < gap(pos, here_pos) and
                     self.reachable(me, following_pos))
            if (gap(pos, here_pos) >= 2.4 and not ahead) or abs(pos[2] - here_pos[2]) > 2:
                break
            self.path.popleft()
        if mode == "kite" and (
            self.destination is None or gap(pos, self.graph.xyz[self.destination]) < 6
        ):
            strafe = self.strafe_point(obs, target)
            if strafe is not None:
                return self.drive(me, strafe, avoid=False), False
        stop_at_shelter = False
        if self.path:
            point = self.follow_point(me)
            if mode == "travel" and len(self.path) == 1 and gap(point, goal) < 5 and self.reachable(me, goal):
                point = goal
            elif mode == "retreat" and point == self.graph.xyz[self.path[-1]] and gap(pos, point) < 8:
                stop_at_shelter = influence.exposure(point, me["stats"]["height"] / 2)[0] == 0
        else:
            point = goal
        sprint = urgent or (mode == "retreat" and near < 90)
        return self.drive(me, point, sprint=sprint, emergency=near < 20, stop=stop_at_shelter), False

    def reachable(self, me, point):
        pos = me["pos"]
        dx, dy = point[0] - pos[0], point[1] - pos[1]
        distance = math.hypot(dx, dy)
        return (distance < 0.2 or self.arena.walkable(me, math.atan2(dy, dx), distance))
