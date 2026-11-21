"""Gun and mortar aiming shared by the Practice opponents."""
from collections import deque
import math


def horizontal(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def angle(a, b):
    return math.remainder(a - b, math.tau)


def unit(yaw, pitch):
    return math.cos(yaw) * math.cos(pitch), math.sin(yaw) * math.cos(pitch), math.sin(pitch)


def advance(p, v, time):
    return tuple(a + b * time for a, b in zip(p, v))


def raised(point, height):
    return point[0], point[1], point[2] + height


class OpponentAim:
    def __init__(self, config, world):
        self.cfg = config
        self.world = world
        self.tracks = {}
        self.position = None

    def track(self, t, enemies):
        for e in enemies:
            history = self.tracks.setdefault(e["id"], deque(maxlen=8))
            history.append((t, e["pos"], e.get("vel", (0, 0, 0))))
        expired = []
        for key, history in self.tracks.items():
            while history and t - history[0][0] > .8:
                history.popleft()
            if not history:
                expired.append(key)
        for key in expired:
            del self.tracks[key]

    def velocity(self, target):
        current = target.get("vel", (0, 0, 0))
        history = self.tracks.get(target["id"])
        if not history or len(history) < 5 or self.position is None:
            return current
        dx, dy = (target["pos"][i] - self.position[i] for i in range(2))
        d = math.hypot(dx, dy)
        if d < 1:
            return current
        lateral = [(-dy * vel[0] + dx * vel[1]) / d for _, _, vel in history]
        signs = [value > 0 for value in lateral if abs(value) > .85]
        if sum(a != b for a, b in zip(signs, signs[1:])) < 2:
            return current
        now, position, _ = history[-1]
        at, past, _ = min(list(history)[:-1], key=lambda item: abs(now - item[0] - .6))
        span = now - at
        if span < .4:
            return current
        return tuple(.35 * current[i] + .65 * (position[i] - past[i]) / span for i in range(3))

    def dimensions(self, target):
        cfg = self.cfg
        kind = target.get("kind", "crate")
        if kind == "mech":
            stats = cfg["mech"]
            size = 1 + cfg["cubes"]["size_per_cube"] * target.get("cubes", 0)
        else:
            stats = cfg["npc"] if kind == "npc" else cfg["crates"]
            size = 1
        return tuple(stats.get(key, default) * size for key, default in (
            ("radius", 1.5), ("height", 3), ("eye_height", 3), ("muzzle_height", 2)))

    def shot(self, me, target, weapon):
        kind, stats = weapon["kind"], weapon["stats"]
        radius, height, _, _ = self.dimensions(target)
        target_z = min(.8, height * .5) if kind == "mortar" else height * .5
        origin = raised(me["pos"], me["stats"]["height"] + .5 if kind == "mortar" else me["stats"]["muzzle_height"])
        gun_offset = me["stats"]["radius"] + .5
        center = raised(target["pos"], target_z)
        velocity = self.velocity(target)

        def line(p):
            dx, dy, dz = (a - b for a, b in zip(p, origin))
            return math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))

        if kind == "mortar":
            speed, gravity = stats["speed"], stats["gravity"]
            choices = []
            for sign in (-1, 1):
                time = horizontal(origin, center) / speed
                pitch, yaw = 0., 0.
                for _ in range(4):
                    aim = advance(center, velocity, time)
                    d = horizontal(origin, aim)
                    dz = aim[2] - origin[2]
                    discr = speed ** 4 - gravity * (gravity * d * d + 2 * dz * speed * speed)
                    if d < 15 or discr < 0:
                        break
                    pitch = math.atan((speed * speed + sign * math.sqrt(discr)) / (gravity * d))
                    yaw, _ = line(aim)
                    time = max(0, (d / math.cos(pitch) - gun_offset) / speed)
                else:
                    if not me["stats"]["min_pitch"] <= pitch <= me["stats"]["max_pitch"] or time > stats["ttl"]:
                        continue
                    direction = unit(yaw, pitch)
                    muzzle = advance(origin, direction, gun_offset)
                    points = [muzzle]
                    for i in range(1, 13):
                        t = time * i / 12
                        points.append(raised(advance(muzzle, direction, t * speed), -gravity * t * t / 2))
                    if all(not math.isfinite(hit) for hit in self.world.rays([origin, *points[:-1]], points)):
                        choices.append((time, yaw, pitch, advance(target["pos"], velocity, time)))
            return min(choices) if choices else None

        time = 0.
        if kind == "gun":
            for _ in range(4):
                time = max(0., (math.dist(origin, advance(center, velocity, time)) - gun_offset) / stats["speed"])
        aim = advance(center, velocity, time)
        d = math.dist(origin, aim)
        yaw, pitch = line(aim)
        if d > stats["range"] + gun_offset or not me["stats"]["min_pitch"] <= pitch <= me["stats"]["max_pitch"]:
            return None
        if math.isfinite(self.world.ray(origin, aim)):
            return None
        return time, yaw, pitch, advance(target["pos"], velocity, time)
