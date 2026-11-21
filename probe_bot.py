from dataclasses import replace

from canary_bot import GroundCanary
from mechfight import run


class CloseFightProbe(GroundCanary):
    def __init__(self, hello):
        super().__init__(hello)
        self.clear_waves = True
        self.practice = replace(self.practice, laser_distance=78, retreat_hp=0.38)

    def kite_goal(self, me, target, tactics, now=0, zone_radius=float("inf"), execute=False, ideal_override=None):
        # 140 disables the wide-zone laser override while preserving the 100 m checks.
        if tactics is self.practice:
            zone_radius = min(zone_radius, 140)
        return super().kite_goal(me, target, tactics, now, zone_radius, execute, ideal_override)


if __name__ == "__main__":
    run(CloseFightProbe)
