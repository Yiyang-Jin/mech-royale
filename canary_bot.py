"""Run the submitted 2.88 bot with a single ground-direction substitution."""
import math

from canary_path import StaticRoutes
from strategy_bot import Bot, mf


class GroundCanary(Bot):
    def __init__(self, hello):
        super().__init__(hello)
        self.static_routes = StaticRoutes(self.arena, hello["config"]["mech"])

    def move(self, me, goal, action, dodge, hurry, quiet, cover_from=None):
        pos = me["pos"]
        if (dodge is None and self.roof is None and me["grounded"]
                and abs(pos[2] - self.arena.ground(*pos[:2])) <= 2):
            x, y = self.static_routes.direction_point(me, goal)
            dx, dy = x - pos[0], y - pos[1]
            hop = math.hypot(dx, dy)
            distance = math.dist(pos[:2], goal[:2])
            if (x, y) != tuple(goal[:2]) and hop > 0 and distance > 0:
                # The parent still sees the original range and applies its own control rules.
                goal = (pos[0] + dx * distance / hop, pos[1] + dy * distance / hop)
        super().move(me, goal, action, dodge, hurry, quiet, cover_from)


if __name__ == "__main__":
    mf.run(GroundCanary)
