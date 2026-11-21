"""Ground routing on a four-metre occupancy grid."""
from array import array
from collections import deque
from heapq import heappop, heappush
from itertools import pairwise
import math
from time import perf_counter


STEP = 4
NEIGHBORS = ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def near_edge(x, y, rings, clearance_squared):
    for ring in rings:
        for (ax, ay), (bx, by) in pairwise(ring):
            dx, dy = bx - ax, by - ay
            length_squared = dx * dx + dy * dy
            if not length_squared:
                continue
            t = max(0, min(1, ((x - ax) * dx + (y - ay) * dy) / length_squared))
            if (x - ax - dx * t) ** 2 + (y - ay - dy * t) ** 2 < clearance_squared:
                return True
    return False


class GroundGrid:
    def __init__(self, arena, stats):
        limits = arena.bounds
        clearance = stats["radius"] + 0.85
        self.x0 = math.ceil((limits["minX"] + clearance) / STEP) * STEP
        self.y0 = math.ceil((limits["minY"] + clearance) / STEP) * STEP
        self.width = math.floor((limits["maxX"] - clearance - self.x0) / STEP) + 1
        self.height = math.floor((limits["maxY"] - clearance - self.y0) / STEP) + 1
        size = self.width * self.height
        self.heights = array("f", (arena.ground(*self.point(i)) for i in range(size)))
        self.blocked = bytearray(size)
        for prism in arena.prisms:
            left, bottom, right, top = prism.bounds
            ix0 = max(0, math.ceil((left - clearance - self.x0) / STEP))
            iy0 = max(0, math.ceil((bottom - clearance - self.y0) / STEP))
            ix1 = min(self.width - 1, math.floor((right + clearance - self.x0) / STEP))
            iy1 = min(self.height - 1, math.floor((top + clearance - self.y0) / STEP))
            for iy in range(iy0, iy1 + 1):
                for ix in range(ix0, ix1 + 1):
                    cell = iy * self.width + ix
                    z = self.heights[cell]
                    if self.blocked[cell] or prism.top <= z + stats["step_up"] or prism.base >= z + stats["height"]:
                        continue
                    x, y = self.point(cell)
                    if prism.inside(x, y) or near_edge(x, y, prism.rings, clearance ** 2):
                        self.blocked[cell] = 1
        slope = math.tan(math.radians(stats["max_slope_deg"]))
        self.cardinal_rise = stats["step_up"] + STEP * slope
        self.diagonal_rise = stats["step_up"] + math.sqrt(2) * STEP * slope

    def point(self, cell):
        return self.x0 + (cell % self.width) * STEP, self.y0 + (cell // self.width) * STEP

    def xy(self, point):
        return round((point[0] - self.x0) / STEP), round((point[1] - self.y0) / STEP)

    def open(self, ix, iy):
        return 0 <= ix < self.width and 0 <= iy < self.height and not self.blocked[iy * self.width + ix]

    def nearest(self, point, reachable=None):
        ix, iy = self.xy(point)
        ix = min(self.width - 1, max(0, ix))
        iy = min(self.height - 1, max(0, iy))
        if self.open(ix, iy) and reachable is None:
            return iy * self.width + ix
        cells = [
            y * self.width + x
            for y in range(iy - 3, iy + 4)
            for x in range(ix - 3, ix + 4)
            if self.open(x, y)
        ]
        for cell in sorted(cells, key=lambda item: math.dist(self.point(item), point[:2])):
            if reachable is None or reachable(self.point(cell)):
                return cell
        return None

    def straight(self, start, end):
        count = max(1, math.ceil(math.dist(start[:2], end[:2]) / (STEP / 2)))
        dx, dy = (end[0] - start[0]) / count, (end[1] - start[1]) / count
        previous = None
        for i in range(count + 1):
            ix, iy = self.xy((start[0] + i * dx, start[1] + i * dy))
            if not self.open(ix, iy):
                return False
            if previous is not None and previous != (ix, iy):
                ax, ay = previous
                if not self.open(ix, ay) or not self.open(ax, iy):
                    return False
                # The half-cell sampling can take at most one grid step.
                rise = self.diagonal_rise if ix != ax and iy != ay else self.cardinal_rise
                if abs(self.heights[iy * self.width + ix] - self.heights[ay * self.width + ax]) > rise:
                    return False
            previous = ix, iy
        return True

    def route(self, start, finish):
        width = self.width
        finish_x, finish_y = finish % width, finish // width
        queue = [(0.0, 0.0, start)]
        parent = {}
        cost = {start: 0.0}
        best, best_h = start, math.inf
        deadline = perf_counter() + 0.012
        expanded = 0
        while queue:
            _, walked, cell = heappop(queue)
            if walked != cost[cell]:
                continue
            ix, iy = cell % width, cell // width
            h = math.hypot(ix - finish_x, iy - finish_y)
            if h < best_h:
                best, best_h = cell, h
            if cell == finish:
                break
            expanded += 1
            if expanded % 64 == 0 and perf_counter() >= deadline:
                break
            z = self.heights[cell]
            for dx, dy in NEIGHBORS:
                nx, ny = ix + dx, iy + dy
                if not self.open(nx, ny):
                    continue
                diagonal = dx != 0 and dy != 0
                if diagonal and (not self.open(nx, iy) or not self.open(ix, ny)):
                    continue
                next_cell = ny * width + nx
                rise = self.diagonal_rise if diagonal else self.cardinal_rise
                if abs(self.heights[next_cell] - z) > rise:
                    continue
                new_cost = walked + (math.sqrt(2) if diagonal else 1)
                if new_cost >= cost.get(next_cell, math.inf):
                    continue
                cost[next_cell] = new_cost
                parent[next_cell] = cell
                heappush(queue, (new_cost + 1.12 * math.hypot(nx - finish_x, ny - finish_y),
                                 new_cost, next_cell))
        path = deque()
        while best in parent:
            path.appendleft(self.point(best))
            best = parent[best]
        return path


class GroundNavigation:
    def __init__(self, arena, stats):
        self.arena = arena
        self.grid = GroundGrid(arena, stats)
        self.path = deque()
        self.destination = None
        self.retry_at = -math.inf

    def waypoint(self, me, goal, now):
        pos = me["pos"]
        if not me["grounded"] or abs(pos[2] - self.arena.ground(*pos[:2])) > 2:
            self.path.clear()
            return goal
        if math.dist(pos[:2], goal) < 4 or self.grid.straight(pos, goal):
            self.path.clear()
            return goal
        if now >= self.retry_at and (
            not self.path or math.dist(goal, self.destination) > 12 or math.dist(pos[:2], self.path[0]) > 18
        ):
            self.retry_at = now + 0.45
            self.destination = goal

            def reachable(point):
                dx, dy = point[0] - pos[0], point[1] - pos[1]
                return self.arena.walkable(me, math.atan2(dy, dx), math.hypot(dx, dy))

            start = self.grid.nearest(pos, reachable)
            finish = self.grid.nearest(goal)
            self.path.clear()
            if start is not None and finish is not None:
                self.path = self.grid.route(start, finish)
                anchor = self.grid.point(start)
                if self.path and math.dist(pos[:2], anchor) > 2.5:
                    self.path.appendleft(anchor)
        while self.path and math.dist(pos[:2], self.path[0]) <= 2.5:
            self.path.popleft()
        if not self.path:
            return goal
        while len(self.path) > 1 and math.dist(pos[:2], self.path[1]) < 23 and self.grid.straight(pos, self.path[1]):
            self.path.popleft()
        return self.path[0]
