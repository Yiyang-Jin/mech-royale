"""Time decisions under synthetic match load, using the real arena and fixture rules."""
from copy import deepcopy
import gzip
import json
import math
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategy_bot import Bot


def read_cases(hello, observation, arena):
    for index in range(48):
        obs = deepcopy(observation)
        me = obs["self"]
        x, y = -240 + index % 8 * 53, -110 + index // 8 * 41
        z = arena.ground(x, y)
        kind = ("laser", "mortar", "lightning")[index % 3]
        me.update(pos=[x, y, z], hp=me["max_hp"] * (0.44 if index % 4 == 0 else 1))
        me["weapons"][1] = {"kind": kind, "stats": hello["config"]["weapons"][kind], "cooldown": 0}
        obs.update(events=[], sounds=[], beams=[], pickups=[], mechs=[], projectiles=[], drops=[])
        for i in range(15):
            yaw = index + i * math.tau / 15
            radius = 17 if i == 0 and index % 4 == 1 else 75 if i == 0 else 120 + i * 3
            mx, my = x + radius * math.cos(yaw), y + radius * math.sin(yaw)
            obs["mechs"].append({
                "id": (me["id"] + i + 1) % 16, "kind": "mech",
                "pos": [mx, my, arena.ground(mx, my)],
                "aim": [yaw + math.pi, 0], "vel": [6 * math.sin(yaw), -5 * math.cos(yaw), 0],
                "hp": 1250, "max_hp": 1400, "charging": i == 0 and index % 4 == 2,
            })
        for i in range(30):
            yaw = index + i * math.tau / 30
            dx, dy = math.cos(yaw), math.sin(yaw)
            miss = 1 if index % 4 == 3 else 12
            obs["projectiles"].append({
                "id": i + 100, "kind": "gun",
                "pos": [x + 55 * dx - miss * dy, y + 55 * dy + miss * dx, z + 4],
                "vel": [-250 * dx, -250 * dy, 0],
            })
        obs["drops"] = [
            {"id": 20000 + i, "pos": [x + i * 20, y + 30, z], "radius": 8,
             "lands_in": 0, "capture_s": 4, "progress": 0, "holder": None}
            for i in range(3)
        ]
        yield obs


def main():
    with gzip.open(ROOT / "check/stream.jsonl.gz", "rt", encoding="utf-8") as stream:
        hello, observation = (json.loads(next(stream)) for _ in range(2))
    hello["map"]["dir"] = str(ROOT / "arena")
    bot = Bot(hello)
    cases = list(read_cases(hello, observation, bot.arena))
    times = []
    for frame in range(1200):
        obs = cases[frame % len(cases)]
        obs.update(t=10 + frame * 0.1, tick=200 + 2 * frame)
        # Revisit all decision paths independently of health hysteresis in earlier cases.
        bot.pvp = frame % 5 == 0
        bot.recovering = False
        start = perf_counter()
        action = bot.act(obs)
        times.append((perf_counter() - start) * 1000)
        if action["tick"] != obs["tick"]:
            raise ValueError("wrong tick in stress run")
    times.sort()
    p99 = times[math.ceil(len(times) * 0.99) - 1]
    print(f"{len(times)} decisions | p99 {p99:.2f} ms | max {times[-1]:.2f} ms")
    if p99 >= 25 or times[-1] >= 50:
        raise SystemExit("decision latency exceeded budget")
    if sys.platform.startswith("linux"):
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(f"process peak RSS {rss:.1f} MiB")
        if rss >= 300:
            raise SystemExit("memory exceeded budget")


if __name__ == "__main__":
    main()
