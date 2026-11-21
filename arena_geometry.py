"""Static arena geometry used by the bot."""
from array import array
from collections import defaultdict
from itertools import pairwise
import json
import math
from pathlib import Path
import sys


CELL = 32


def contains(ring, x, y):
    inside = False
    for (ax, ay), (bx, by) in pairwise(ring):
        if (ay > y) != (by > y) and x < ax + (bx - ax) * (y - ay) / (by - ay):
            inside = not inside
    return inside


class Prism:
    def __init__(self, data):
        self.kind = data.get("kind")
        self.base, self.top = data["base"], data["top"]
        self.rings = []
        for flat in (data["ring"], *data["holes"]):
            ring = list(zip(flat[::2], flat[1::2]))
            if len(ring) >= 3:
                self.rings.append(ring + ring[:1])
        xs, ys = zip(*self.rings[0])
        self.bounds = min(xs), min(ys), max(xs), max(ys)

    def inside(self, x, y):
        return contains(self.rings[0], x, y) and not any(
            contains(hole, x, y) for hole in self.rings[1:]
        )

    def intersects(self, start, end):
        x, y, z = start
        dx, dy, dz = (b - a for a, b in zip(start, end))
        left, bottom, right, top = self.bounds
        lo, hi = 0.0, 1.0
        for origin, delta, lower, upper in (
            (x, dx, left, right), (y, dy, bottom, top), (z, dz, self.base, self.top)
        ):
            if abs(delta) < 1e-9:
                if not lower <= origin <= upper:
                    return False
                continue
            a, b = sorted(((lower - origin) / delta, (upper - origin) / delta))
            lo, hi = max(lo, a), min(hi, b)
            if lo > hi:
                return False
        cuts = [lo, hi]
        for ring in self.rings:
            for (ax, ay), (bx, by) in pairwise(ring):
                ex, ey = bx - ax, by - ay
                determinant = dx * ey - dy * ex
                if abs(determinant) < 1e-9:
                    continue
                t = ((ax - x) * ey - (ay - y) * ex) / determinant
                u = ((ax - x) * dy - (ay - y) * dx) / determinant
                if lo < t < hi and 0 <= u <= 1:
                    cuts.append(t)
        for a, b in pairwise(sorted(cuts)):
            t = (a + b) / 2
            px, py = x + t * dx, y + t * dy
            if self.inside(px, py):
                return True
        return False


def cells(left, bottom, right, top):
    for ix in range(math.floor(left / CELL), math.floor(right / CELL) + 1):
        for iy in range(math.floor(bottom / CELL), math.floor(top / CELL) + 1):
            yield ix, iy


class Arena:
    def __init__(self, directory):
        directory = Path(directory)
        with (directory / "v2/world.json").open(encoding="utf-8") as file:
            world = json.load(file)
        self.bounds = world["bounds"]
        terrain = world["terrain"]
        self.origin = terrain["minX"], terrain["minY"]
        self.step, self.scale = terrain["step"], terrain.get("scale", 0.01)
        self.nx, self.ny = terrain["nx"], terrain["ny"]
        self.heights = array("h")
        with (directory / "v2/terrain.i16").open("rb") as file:
            self.heights.fromfile(file, self.nx * self.ny)
        if sys.byteorder != "little":
            self.heights.byteswap()
        self.bins = defaultdict(list)
        self.prisms = []
        for data in world["prisms"]:
            prism = Prism(data)
            self.prisms.append(prism)
            for cell in cells(*prism.bounds):
                self.bins[cell].append(prism)

    def ground(self, x, y):
        gx = min(self.nx - 1, max(0, (x - self.origin[0]) / self.step))
        gy = min(self.ny - 1, max(0, (y - self.origin[1]) / self.step))
        ix, iy = min(int(gx), self.nx - 2), min(int(gy), self.ny - 2)
        tx, ty = gx - ix, gy - iy
        index = iy * self.nx + ix
        a, b = self.heights[index:index + 2]
        c, d = self.heights[index + self.nx:index + self.nx + 2]
        return self.scale * ((1 - ty) * (a + tx * (b - a)) + ty * (c + tx * (d - c)))

    def solid_between(self, start, end):
        x, y, _ = start
        ex, ey, _ = end
        nearby = set()
        for cell in cells(min(x, ex), min(y, ey), max(x, ex), max(y, ey)):
            nearby.update(self.bins.get(cell, ()))
        return any(prism.intersects(start, end) for prism in nearby)

    def clear_shot(self, start, end):
        if self.solid_between(start, end):
            return False
        # shortcut: terrain sampling can miss short grazing obstructions, refine if they matter.
        distance = math.dist(start[:2], end[:2])
        steps = max(1, math.ceil(distance / 3))
        x, y, z = start
        dx, dy, dz = (b - a for a, b in zip(start, end))
        return all(z + dz * t > self.ground(x + dx * t, y + dy * t)
                   for t in (i / steps for i in range(1, steps + 1)))

    def walkable(self, me, yaw, distance):
        x, y, z = me["pos"]
        stats = me["stats"]
        radius = stats["radius"]
        dx, dy = math.cos(yaw), math.sin(yaw)
        ex, ey = x + dx * distance, y + dy * distance
        bounds = self.bounds
        if not (bounds["minX"] + radius < ex < bounds["maxX"] - radius
                and bounds["minY"] + radius < ey < bounds["maxY"] - radius):
            return False
        if self.ground(ex, ey) - z > stats["step_up"] + distance * math.tan(
            math.radians(stats["max_slope_deg"])
        ):
            return False
        feet = z + stats["step_up"] + 0.02
        for lateral in (0, -radius, radius):
            ox, oy = -dy * lateral, dx * lateral
            for height in (feet, z + stats["height"] - 0.05):
                if self.solid_between((x + ox, y + oy, height),
                                      (ex + ox, ey + oy, height)):
                    return False
        return True
