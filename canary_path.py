"""Ground-only snapshot of the layered graph's static walk topology."""
from collections import deque
from heapq import heappop, heappush
from itertools import islice, pairwise
import math
from time import perf_counter

STEP = 4
DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


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


class StaticRoutes:
    def __init__(self, arena, stats, timed_search=True):
        self.arena = arena
        self.timed_search = timed_search
        border = stats["radius"] + 0.85
        self.x0 = math.ceil((arena.bounds["minX"] + border) / STEP) * STEP
        self.y0 = math.ceil((arena.bounds["minY"] + border) / STEP) * STEP
        self.width = math.floor((arena.bounds["maxX"] - border - self.x0) / STEP) + 1
        self.height = math.floor((arena.bounds["maxY"] - border - self.y0) / STEP) + 1
        count = self.width * self.height
        self.xyz = []
        for cell in range(count):
            x = self.x0 + cell % self.width * STEP
            y = self.y0 + cell // self.width * STEP
            self.xyz.append((x, y, arena.ground(x, y)))
        self.open = bytearray([1]) * count
        prisms = dict.fromkeys(prism for group in arena.bins.values() for prism in group)
        for prism in prisms:
            left, bottom, right, top = prism.bounds
            for iy in range(max(0, math.ceil((bottom - border - self.y0) / STEP)),
                            min(self.height - 1, math.floor((top + border - self.y0) / STEP)) + 1):
                for ix in range(max(0, math.ceil((left - border - self.x0) / STEP)),
                                min(self.width - 1, math.floor((right + border - self.x0) / STEP)) + 1):
                    cell = iy * self.width + ix
                    x, y, z = self.xyz[cell]
                    if prism.base < z + stats["height"] and prism.top - z > stats["step_up"]:
                        if prism.inside(x, y) or not edge_clear(x, y, prism, border):
                            self.open[cell] = 0
        self.edges = [[] for _ in range(count)]
        self.build_edges(stats)
        self.path = deque()
        self.goal = None
        self.retry = 0
        self.steps = 0

    def node_at(self, ix, iy):
        if 0 <= ix < self.width and 0 <= iy < self.height:
            cell = iy * self.width + ix
            if self.open[cell]:
                return cell
        return None

    def indices(self, point):
        return round((point[0] - self.x0) / STEP), round((point[1] - self.y0) / STEP)

    def build_edges(self, stats):
        slope = math.tan(math.radians(stats["max_slope_deg"]))
        for cell, (x, y, z) in enumerate(self.xyz):
            if not self.open[cell]:
                continue
            ix, iy = cell % self.width, cell // self.width
            for dx, dy in DIRECTIONS:
                other = self.node_at(ix + dx, iy + dy)
                if other is None:
                    continue
                step = STEP * (math.sqrt(2) if dx and dy else 1)
                nx, ny, nz = self.xyz[other]
                rise = abs(nz - z)
                limit = max(stats["step_up"], slope * step)
                if rise > limit:
                    continue
                if dx and dy:
                    sides = (self.node_at(ix + dx, iy), self.node_at(ix, iy + dy))
                    if any(side is None or abs(self.xyz[side][2] - z) > limit for side in sides):
                        continue
                if rise > stats["step_up"]:
                    mid_z = self.arena.ground((x + nx) / 2, (y + ny) / 2)
                    if max(abs(mid_z - z), abs(nz - mid_z)) > max(stats["step_up"], slope * step / 2):
                        continue
                self.edges[cell].append((other, step * (1 + 0.22 * rise / step)))
        degree = [len(edges) for edges in self.edges]
        leaves = deque(i for i, amount in enumerate(degree) if amount == 1)
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

    def closest(self, point, accepts=None):
        ix, iy = self.indices(point)
        ix, iy = min(self.width - 1, max(0, ix)), min(self.height - 1, max(0, iy))
        candidates = []
        for y in range(iy - 2, iy + 3):
            for x in range(ix - 2, ix + 3):
                cell = self.node_at(x, y)
                if cell is not None:
                    distance = math.dist(point[:2], self.xyz[cell][:2])
                    candidates.append((distance, cell))
        for _, cell in sorted(candidates):
            if accepts is None or accepts(self.xyz[cell]):
                return cell
        return None

    def clear_grid_line(self, start, goal):
        distance = math.dist(start[:2], goal)
        count = max(1, math.ceil(distance / 2))
        dx, dy = (goal[0] - start[0]) / count, (goal[1] - start[1]) / count
        previous = None
        for i in range(count + 1):
            node = self.node_at(*self.indices((start[0] + dx * i, start[1] + dy * i)))
            if node is None:
                return False
            if previous is not None and previous != node and not any(
                other == node for other, _ in self.edges[previous]
            ):
                return False
            previous = node
        return True

    def astar(self, start, end):
        goal = self.xyz[end][:2]
        queue = [(0.0, 0.0, start)]
        best = start
        parent = {}
        cost = {start: 0.0}
        deadline = perf_counter() + 0.009
        expanded = 0
        while queue and expanded < 1800:
            _, walked, node = heappop(queue)
            if walked != cost[node]:
                continue
            if math.dist(self.xyz[node][:2], goal) < math.dist(self.xyz[best][:2], goal):
                best = node
            if node == end:
                break
            expanded += 1
            if self.timed_search and expanded % 16 == 0 and perf_counter() >= deadline:
                break
            for other, weight in self.edges[node]:
                next_cost = walked + weight
                if next_cost < cost.get(other, math.inf):
                    cost[other] = next_cost
                    parent[other] = node
                    heappush(queue, (next_cost + math.dist(self.xyz[other][:2], goal), next_cost, other))
        path = deque([best])
        while path[0] in parent:
            path.appendleft(parent[path[0]])
        return path

    def direction_point(self, me, goal):
        self.steps += 1
        pos = me["pos"]
        goal = tuple(goal[:2])
        if math.dist(pos[:2], goal) < 6 or self.clear_grid_line(pos, goal):
            self.path.clear()
            return goal

        def reachable(point):
            dx, dy = point[0] - pos[0], point[1] - pos[1]
            distance = math.hypot(dx, dy)
            return distance < 0.2 or self.arena.walkable(me, math.atan2(dy, dx), distance)

        goal_changed = self.goal is None or math.dist(goal, self.goal) >= 12
        if self.steps >= self.retry and (not self.path or goal_changed
                                         or math.dist(pos[:2], self.xyz[self.path[0]][:2]) > 14):
            self.retry = self.steps + 5
            self.goal = goal
            start, finish = self.closest(pos, reachable), self.closest(goal)
            self.path = self.astar(start, finish) if start is not None and finish is not None else deque()
        while len(self.path) > 1:
            first, second = (self.xyz[cell] for cell in islice(self.path, 0, 2))
            if (math.dist(pos[:2], first[:2]) <= 2.2
                    or (math.dist(pos[:2], second[:2]) + 1 < math.dist(pos[:2], first[:2]) and reachable(second))):
                self.path.popleft()
            else:
                break
        if not self.path:
            return goal
        point = self.xyz[self.path[0]]
        for cell in islice(self.path, 1, 4):
            candidate = self.xyz[cell]
            if math.dist(pos[:2], candidate[:2]) > 12 or not reachable(candidate):
                break
            point = candidate
        if math.dist(pos[:2], point[:2]) < 1.3:
            self.path.clear()
            return goal
        return point[:2]
