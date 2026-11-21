"""C ABI for the native arena, motion and laser-cover checks."""
from array import array
from ctypes import CDLL, POINTER, Structure, byref, c_double, c_int, c_int16, c_void_p
import json
from pathlib import Path
import sys


class Vec(Structure):
    _fields_ = [(name, c_double) for name in ("x", "y", "z")]

    def as_tuple(self):
        return self.x, self.y, self.z


class Stats(Structure):
    _fields_ = [(name, c_double) for name in (
        "radius", "height", "eye_height", "muzzle_height", "walk_speed", "boost_speed",
        "ground_accel", "air_accel", "gravity", "jump_speed", "jet_accel", "max_rise_speed",
        "step_up", "max_slope_deg", "energy", "energy_regen", "energy_regen_delay",
        "boost_drain", "jet_drain",
    )]

    @classmethod
    def from_dict(cls, stats):
        return cls(*(stats[name] for name, _ in cls._fields_))


class State(Structure):
    _fields_ = [("pos", Vec), ("vel", Vec), ("energy", c_double), ("idle", c_double), ("grounded", c_int)]

    @classmethod
    def from_obs(cls, me, idle=0):
        return cls(Vec(*me["pos"]), Vec(*me["vel"]), me["energy"], idle, me["grounded"])


class Control(Structure):
    _fields_ = [("x", c_double), ("y", c_double), ("boost", c_int), ("jump", c_int)]


class Zone(Structure):
    _fields_ = [(key, c_double) for key in ("x", "y", "radius", "dps")]


class GridPoint(Structure):
    _fields_ = [("pos", Vec), ("next", Vec), ("travel", c_double), ("roof", c_int)]


def _library():
    lib = CDLL(str(Path(__file__).with_name("core.so")))
    signatures = {
        "world_new": (c_void_p, POINTER(c_double), POINTER(c_double), c_int, c_int, POINTER(c_int16)),
        "world_free": (None, c_void_p),
        "world_prism": (None, c_void_p, c_double, c_double, POINTER(c_double), POINTER(c_int), c_int),
        "world_tree": (None, c_void_p, Vec, c_double, c_double, c_double, c_double),
        "world_build": (None, c_void_p, POINTER(Stats)),
        "world_ground": (c_double, c_void_p, c_double, c_double),
        "world_spawnable": (c_int, c_void_p, Vec, POINTER(Stats)),
        "world_on_roof": (c_int, c_void_p, Vec),
        "practice_laser_cover": (c_int, c_void_p, POINTER(Stats), POINTER(State), Zone, Vec,
                                 c_double, c_double, c_int, POINTER(Control)),
        "world_ray": (c_double, c_void_p, Vec, Vec),
        "world_vision": (c_double, c_void_p, Vec, Vec),
        "world_rays": (None, c_void_p, POINTER(Vec), POINTER(Vec), POINTER(c_double), c_int, c_int),
        "body_hit": (c_double, Vec, Vec, Vec, c_double, c_double),
        "step_mech": (None, c_void_p, POINTER(Stats), POINTER(State), POINTER(Control), c_double, c_double),
        "world_reach": (c_int, c_void_p, Vec, POINTER(Stats), POINTER(GridPoint), c_int, c_double),
    }
    for name, (result, *arguments) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, arguments
    return lib


class World:
    def __init__(self, directory, mech, build=True):
        directory = Path(directory) / "v2"
        self.data = json.loads((directory / "world.json").read_text(encoding="utf-8"))
        self.lib = _library()
        self.stats = Stats.from_dict(mech)
        bounds, layout = self.data["bounds"], self.data["terrain"]
        b = (c_double * 4)(*(bounds[n] for n in ("minX", "minY", "maxX", "maxY")))
        terrain = (c_double * 4)(layout["minX"], layout["minY"], layout["step"], layout.get("scale", 0.01))
        heights = array("h")
        heights.frombytes((directory / "terrain.i16").read_bytes())
        if sys.byteorder == "big":
            heights.byteswap()
        samples = (c_int16 * len(heights)).from_buffer(heights)
        self.ptr = self.lib.world_new(b, terrain, layout["nx"], layout["ny"], samples)
        for prism in self.data["prisms"]:
            rings = [prism["ring"], *prism["holes"]]
            flat = [xy for ring in rings for xy in ring]
            self.lib.world_prism(self.ptr, prism["base"], prism["top"],
                                 (c_double * len(flat))(*flat),
                                 (c_int * len(rings))(*(len(ring) // 2 for ring in rings)), len(rings))
        trees = self.data.get("trees", {})
        fields, rows = trees.get("fields", ()), trees.get("data", ())
        for i in range(0, len(rows), len(fields) or 1):
            t = dict(zip(fields, rows[i:i + len(fields)]))
            self.lib.world_tree(self.ptr, Vec(t["x"], t["y"], t["z"]),
                                t["crownR"], t["crownBase"], t["height"], t["trunkR"])
        if build:
            self.lib.world_build(self.ptr, byref(self.stats))

    def close(self):
        if self.ptr:
            self.lib.world_free(self.ptr)
            self.ptr = None

    def __del__(self):
        if getattr(self, "ptr", None):
            self.close()

    def ground(self, x, y):
        return self.lib.world_ground(self.ptr, x, y)

    def spawnable(self, p, stats):
        return bool(self.lib.world_spawnable(self.ptr, Vec(*p), byref(stats)))

    def on_roof(self, p):
        return bool(self.lib.world_on_roof(self.ptr, Vec(*p)))

    def ray(self, a, b):
        return self.lib.world_ray(self.ptr, Vec(*a), Vec(*b))

    def vision(self, a, b):
        return self.lib.world_vision(self.ptr, Vec(*a), Vec(*b))

    def rays(self, starts, ends, optical=False):
        if len(starts) != len(ends):
            raise ValueError("ray endpoint counts differ")
        n = len(starts)
        out = (c_double * n)()
        self.lib.world_rays(self.ptr, (Vec * n)(*(Vec(*p) for p in starts)),
                            (Vec * n)(*(Vec(*p) for p in ends)), out, n, optical)
        return list(out)

    def body_hit(self, a, b, p, radius, height):
        return self.lib.body_hit(Vec(*a), Vec(*b), Vec(*p), radius, height)

    def step(self, state, stats, control, dt=0.05, speed_scale=1):
        self.lib.step_mech(self.ptr, byref(stats), byref(state), byref(control), dt, speed_scale)

    def reach(self, pos, count=2000, radius=100):
        out = (GridPoint * count)()
        n = self.lib.world_reach(self.ptr, Vec(*pos), byref(self.stats), out, count, radius)
        return out[:n]

    def practice_laser_cover(self, state, stats, zone, source, dt, speed_scale, boost):
        control = Control()
        found = self.lib.practice_laser_cover(self.ptr, byref(stats), byref(state), zone,
                                              Vec(*source), dt, speed_scale, boost, byref(control))
        return control if found else None
