"""Closed-loop local Practice matches on the supplied arena."""
import argparse
from collections import Counter, deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
import gzip
from itertools import repeat
import json
import math
from pathlib import Path
import random
from time import perf_counter

from arena_geometry import Arena
from canary_path import StaticRoutes
from .practice import PracticeBot, VARIANTS
from .ballistics import advance, angle, horizontal, raised, unit
from .native import Control, State, Stats, Vec, World
from .opponents import (DEFAULT_WAVES, PRACTICE_SEEDS, PRACTICE_TIERS,
                        Opponent, SquadDirector)
from .replay import write_replay


@dataclass
class Mech:
    id: int
    state: State
    hp: float
    max_hp: float
    stats: dict
    native: Stats
    weapons: list
    controller: object = None
    role: str = "house"
    brain: object = None
    upgrades: dict = field(default_factory=dict)
    aim: list = field(default_factory=lambda: [0., 0.])
    action: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    damage_clock: float = -10.
    last_step: float = -10.
    laser_lock: float = 0.
    cubes: int = 0
    kills: int = 0
    place: int = 0
    death: float = 0.
    offer: dict = None
    roof_seconds: float = 0.
    damage_sources: Counter = field(default_factory=Counter)
    damage_dealt: float = 0.
    mech_damage_dealt: float = 0.
    weak_escapes: int = 0
    weak_rotations: int = 0
    weakened_by: int = -1
    last_mech_hit: float = -math.inf
    roof_since: float = -math.inf
    idle_roof_seconds: float = 0.
    spawn_position: tuple = ()
    spawn_distance_3_5: float = 0.
    spawn_distance_peak_3_5: float = 0.
    first_sidearm: tuple = None
    first_laser: float = None
    laser_actual_starts: int = 0
    laser_shots: int = 0
    laser_full_shots: int = 0

    @property
    def pos(self):
        return self.state.pos.as_tuple()

    def public(self):
        return {"id": self.id, "kind": "mech", "pos": self.pos, "vel": self.state.vel.as_tuple(),
                "aim": self.aim, "hp": self.hp, "max_hp": self.max_hp, "cubes": self.cubes,
                "weapons": [w["kind"] for w in self.weapons if w],
                "charging": any(w and w.get("charge", 0) > 0 for w in self.weapons)}


class Simulation:
    def __init__(self, hello, arena, seed, mode, variant, seconds, waves=DEFAULT_WAVES, capture=False):
        self.hello, self.cfg = hello, hello["config"]
        if mode != "practice":
            raise ValueError("only Practice is supported")
        self.variant, self.seconds = variant, seconds
        self.recent_hits = deque()
        self.death_audit = None
        self.medium_gun_shots = 0
        self.medium_gun_hits = 0
        self.rng = random.Random(seed)
        self.capture = capture
        self.director = SquadDirector(waves)
        self.active = set()
        self.probe = None
        self.dead_this_tick = []
        self.activations = {}
        self.seed = seed
        self.world = World(arena, self.cfg["mech"])
        self.dt = 1 / self.cfg["match"]["tick_hz"]
        self.t = 0.
        self.tick = int(round(self.t / self.dt))
        self.seconds += self.t
        self.shots, self.beams, self.sounds, self.frames = [], [], [], []
        self.pickups, self.cubes, self.crates, self.drones, self.drops = [], [], [], [], []
        self.history = []
        self.next_id = 5000
        self.count = self.cfg["match"]["mechs"]
        self.zones = self.make_zones()
        self.rng.uniform(0, math.tau)  # Preserve the seed sequence of the original Practice map.
        spawn_center = self.zone()["center"]
        slots = []
        match = self.cfg["match"]
        zone = self.zone()
        bounds = self.world.data["bounds"]
        margin = match["arena_margin"]
        stats = self.world.stats
        for _ in range(12):
            slots.clear()
            for _ in range(2400):
                x = self.rng.uniform(bounds["minX"] + margin, bounds["maxX"] - margin)
                y = self.rng.uniform(bounds["minY"] + margin, bounds["maxY"] - margin)
                pos = (x, y, self.world.ground(x, y))
                if horizontal(pos, spawn_center) > zone["radius"] * match["spawn_zone_fraction"]:
                    continue
                if any(horizontal(pos, other) < match["spawn_min_separation"] for other in slots):
                    continue
                if self.world.spawnable(pos, stats):
                    slots.append(pos)
                    if len(slots) == self.count:
                        break
            if len(slots) == self.count:
                break
        else:
            raise RuntimeError("could not find 16 separated spawns")
        # Give the opening opponent the shortest approach, then use tier order.
        slots[1:] = sorted(slots[1:], key=lambda p: horizontal(slots[0], p))
        roles = [variant, *PRACTICE_TIERS]
        self.mecks = []
        self.routes = StaticRoutes(Arena(arena), self.cfg["mech"], timed_search=False)
        for i, pos in enumerate(slots):
            base = dict(self.cfg["mech"])
            suite = [self.weapon("gun"), None]
            mech = Mech(i, State(Vec(*pos), Vec(), base["energy"], 10., 1), base["hp"], base["hp"],
                        base, Stats.from_dict(base), suite)
            mech.aim = [math.atan2(spawn_center[1]-pos[1], spawn_center[0]-pos[0]), 0.]
            mech.spawn_position = pos
            mech.role = roles[i]
            if mech.role in PRACTICE_TIERS:
                mech.brain = Opponent(self, mech)
            self.mecks.append(mech)
        self.mecks[0].controller = self.controller(variant, 0)
        self.scene()

    def controller(self, variant, slot):
        hello = dict(self.hello, slot=slot)
        return PracticeBot(hello, self.world, self.routes, variant)

    def weapon(self, kind, equip=False):
        stats = self.cfg["weapons"][kind]
        weapon = {"kind": kind, "stats": dict(stats),
                  "cooldown": max(self.cfg["loot"]["equip_s"], stats["cooldown"]) if equip else 0.}
        if kind == "gun":
            weapon.update(ammo=stats["mag"], reload=0.)
        if kind == "laser":
            weapon.update(charge=0., held=0.)
        return weapon

    def make_zones(self):
        spec = self.cfg["zone"]
        center, radius = spec["start_center"], spec["start_radius"]
        phases, clock = [], 0.
        for phase in spec["phases"]:
            n = phase["radius"]
            direction = self.rng.uniform(0, math.tau)
            spread = self.rng.uniform(.12, .50) * (radius-n)
            following = (center[0] + spread * math.cos(direction), center[1] + spread * math.sin(direction))
            start = clock + phase["hold_s"]
            end = start + phase["shrink_s"]
            phases.append((start, end, center, following, radius, n, phase["dps"]))
            clock, center, radius = end, following, n
        return phases

    def zone(self):
        start, end, center, following, r, n, dps = next(
            (p for p in self.zones if self.t <= p[1]), self.zones[-1])
        fraction = min(1, max(0, (self.t-start)/(end-start)))
        return {"center": tuple(a+(b-a)*fraction for a,b in zip(center, following)),
                "radius": r+(n-r)*fraction, "next_center": following, "next_radius": n,
                "shrink_start": start, "shrink_end": end, "dps": dps}

    def open_point(self, desired=None, separation=0, reserved=(), ring=None, scale=1):
        b = self.world.data["bounds"]
        actor = dict(self.cfg["mech"])
        actor["radius"] *= scale
        actor["height"] *= scale
        stats = Stats.from_dict(actor)
        for _ in range(600):
            if desired:
                x = desired[0]+self.rng.uniform(-40,40)
                y = desired[1]+self.rng.uniform(-40,40)
            else:
                x = self.rng.uniform(b["minX"]+20, b["maxX"]-20)
                y = self.rng.uniform(b["minY"]+20, b["maxY"]-20)
            if not (b["minX"]+9<x<b["maxX"]-9 and b["minY"]+9<y<b["maxY"]-9):
                continue
            z = self.world.ground(x,y)
            p = (x,y,z)
            if ring is not None and not 90 <= horizontal(p, ring) <= 130:
                continue
            if any(horizontal(p,q)<separation for q in reserved):
                continue
            if self.world.spawnable(p, stats):
                return p
        raise RuntimeError("could not place a ground entity")

    def id(self):
        self.next_id += 1
        return self.next_id

    def scene(self):
        for i in range(self.cfg["crates"]["count"]):
            pos = self.open_point()
            self.crates.append({"id": 1000+i, "pos": pos, "hp": self.cfg["crates"]["hp"]})
        npc = self.cfg["npc"]
        for i in range(npc["count"]):
            pos = self.open_point()
            self.drones.append({"id": 100+i, "kind": "npc", "pos": pos, "home": pos,
                                "hp": npc["hp"], "cooldown": 0., "attacker": None, "vel": (0,0,0)})
        for i in range(self.cfg["loot"]["floor_count"]):
            self.pickups.append({"id": self.id(), "kind": ("gun", "laser", "mortar", "lightning")[1+i%3],
                                 "pos": self.open_point()})

    def dimensions(self, item):
        if isinstance(item, Mech):
            return item.stats["radius"], item.stats["height"]
        stats = self.cfg["npc"] if item.get("kind") == "npc" else self.cfg["crates"]
        return stats["radius"], stats["height"]

    def sound(self, kind, pos, source):
        self.sounds.append((self.t, kind, pos, source))

    def visible(self, mech, pos):
        v = self.cfg["vision"]
        distance = horizontal(mech.pos, pos)
        if distance > v["range"] + mech.stats["vision_bonus"]:
            return False
        yaw = math.atan2(pos[1]-mech.pos[1], pos[0]-mech.pos[0])
        if distance > v["near_range"] + mech.stats["near_range_bonus"] and abs(angle(yaw, mech.aim[0])) > math.radians(v["fov_deg"]/2):
            return False
        return self.world.vision(raised(mech.pos, mech.stats["eye_height"]), pos) > .18

    def hear(self, mech, at, kind, point):
        sound = self.cfg["sound"]
        d = horizontal(mech.pos, point)
        if d > sound["range"].get(kind, 0):
            return None
        band = "near" if d < sound["near"] else "mid" if d < sound["mid"] else "far"
        # Stable event jitter gives repeated observations of one sound the same bearing.
        phase = at * 538.1 + point[0] * 4.13 + point[1] * 2.17 + mech.id * 8.9
        error = math.radians(sound["bearing_noise_deg"]) * mech.stats["hearing"] * math.sin(phase)
        return {"kind": kind, "dist": band, "age": self.t - at,
                "bearing": math.atan2(point[1] - mech.pos[1], point[0] - mech.pos[0]) + error}

    def obs(self, me, alive, zone):
        known = {}
        for other in alive:
            if other is me:
                continue
            if self.visible(me, raised(other.pos, other.stats["eye_height"])):
                me.memory[other.id] = self.t
            if self.t-me.memory.get(other.id,-99) <= self.cfg["vision"]["memory_s"]:
                known[other.id] = other.public()
        crates = [c for c in self.crates if c["hp"]>0 and self.visible(me, raised(c["pos"], 1.2))]
        for other in self.drones:
            if other["hp"]<=0:
                continue
            if self.visible(me, raised(other["pos"], 2)):
                me.memory[other["id"]] = self.t
            if self.t-me.memory.get(other["id"],-99) <= self.cfg["vision"]["memory_s"]:
                known[other["id"]] = {k: other[k] for k in ("id","kind","pos","hp","vel")}
        sounds = [heard for at, kind, point, source in self.sounds if source != me.id
                  if (heard := self.hear(me, at, kind, point)) is not None]
        me_view = dict(me.public(), stats=me.stats, weapons=me.weapons, energy=me.state.energy,
                       max_energy=me.stats["energy"], grounded=bool(me.state.grounded),
                       in_zone=horizontal(me.pos,zone["center"])<=zone["radius"], upgrades=list(me.upgrades))
        return {"type":"obs","tick":self.tick,"t":self.t,"alive":len(alive),"self":me_view,
                "mechs":list(known.values()),"crates":crates,
                "pickups":[p for p in self.pickups if self.visible(me, raised(p["pos"],1.2))],
                "cubes":[p for p in self.cubes if self.visible(me, raised(p["pos"],.6))],
                "projectiles":[{k:p[k] for k in ("id","kind","pos","vel")} for p in self.shots
                               if p["by"]!=me.id and self.visible(me,p["pos"])],
                "beams":[b for b in self.beams if horizontal(me.pos,b["path"][0])<240],
                "sounds":sounds,"events":me.events,"zone":zone,"offer":me.offer,
                "drops":[dict(d,lands_in=max(0,d["land"]-self.t)) for d in self.drops]}

    def set_actions(self, alive, zone):
        squad = [m for m in alive if m.id != 0]
        previously_relieved = {m.id for m in squad if m.brain.relief}
        active, lock, bearing = self.director.update(self, squad, self.mecks[0])
        self.mecks[0].weak_rotations += sum(m.brain.relief and m.id not in previously_relieved
                                           and m.weakened_by == 0 for m in squad)
        for m in alive:
            if m.id in active and m.id not in self.activations:
                self.activations[m.id] = {"slot": m.id, "role": m.role, "time": round(self.t, 2),
                                          "cubes": m.cubes, "hp_fraction": round(m.hp / m.max_hp, 3),
                                          "weapons": m.public()["weapons"]}
        self.active = active
        for mech in alive:
            if mech.controller:
                before=perf_counter()
                obs=self.obs(mech,alive,zone)
                ready=perf_counter()
                mech.action=mech.controller.act(obs)
                end=perf_counter()
                self.history.append(((end-before)*1000, (end-ready)*1000))
            elif mech.brain:
                target = self.mecks[0] if mech.id in active else None
                shared = lock if target is not None else None
                if shared is not None and mech.role == "expert":
                    mech.brain.fire_model.position = mech.pos
                    mech.brain.fire_model.track(self.t, [shared.public()])
                mech.action = mech.brain.act(target, shared, zone, self.director.roles.get(mech.id, 0), bearing)
        for mech in alive:
            mech.events=[]
            self.take(mech)

    def take(self, mech):
        action=mech.action
        item=next((p for p in self.pickups if p["id"]==action.get("take")),None)
        drop=action.get("drop")
        if item and horizontal(mech.pos,item["pos"])<=self.cfg["loot"]["pickup_radius"] and abs(mech.pos[2]-item["pos"][2])<=4:
            slot = drop if drop in (0,1) else next((i for i,w in enumerate(mech.weapons) if w is None),None)
            if slot is not None and not any(w and w["kind"]==item["kind"] for w in mech.weapons):
                old=mech.weapons[slot]
                if old is None or old.get("charge",0)==0:
                    if old:
                        self.pickups.append({"id":self.id(),"kind":old["kind"],"pos":mech.pos})
                    mech.weapons[slot]=self.weapon(item["kind"],True)
                    if mech.first_sidearm is None and item["kind"] in ("laser", "lightning"):
                        mech.first_sidearm = (round(self.t, 2), item["kind"])
                    if mech.first_laser is None and item["kind"] == "laser":
                        mech.first_laser = round(self.t, 2)
                    self.pickups.remove(item)
        elif "take" in action:
            reason = "range" if item is not None else "gone"
            mech.events.append({"type": "take_failed", "id": action["take"], "reason": reason})
        elif drop in (0,1):
            w=mech.weapons[drop]
            if w and w.get("charge",0)==0:
                self.pickups.append({"id":self.id(),"kind":w["kind"],"pos":mech.pos})
                mech.weapons[drop]=None
        if mech.offer:
            if self.t > mech.offer["expires"]:
                mech.offer = None
            elif "pick" in action:
                choices = mech.offer["choices"]
                choice = next(c for c in self.cfg["upgrades"]
                              if c["id"] == choices[int(action["pick"]) % len(choices)]["id"])
                mech.upgrades[choice["id"]] = mech.upgrades.get(choice["id"], 0) + 1
                for effect in choice["effects"]:
                    group, *path = effect["path"].split(".")
                    if group == "mech":
                        stats, key = mech.stats, path[0]
                    else:
                        kind, key = path
                        w = next((w for w in mech.weapons if w and w["kind"] == kind), None)
                        if w is None:
                            continue
                        stats = w["stats"]
                    before = stats[key]
                    stats[key] = before * effect.get("mul", 1) + effect.get("add", 0)
                    if group == "mech" and key == "hp":
                        self.update_hp(mech)
                    if group == "mech" and key == "energy":
                        mech.state.energy += stats[key] - before
                mech.native = Stats.from_dict(mech.stats)
                mech.offer = None

    def shootable(self, source):
        if 0 < source < self.count:
            mechs=[self.mecks[0]] if self.mecks[0].hp>0 and source in self.active else []
        else:
            mechs=[m for m in self.mecks if m.id!=source and m.hp>0]
        return mechs+[p for p in (*self.crates,*self.drones) if p["hp"]>0 and p["id"]!=source]

    def ray_target(self, start, end, source):
        wall=self.world.ray(start,end)
        ray_end=advance(start,tuple(b-a for a,b in zip(start,end)),wall) if wall<=1 else end
        targets=[]
        reach = horizontal(start, ray_end)
        for item in self.shootable(source):
            pos=item.pos if isinstance(item,Mech) else item["pos"]
            r,h=self.dimensions(item)
            if abs(pos[0]-start[0]) > reach+r or abs(pos[1]-start[1]) > reach+r:
                continue
            hit=self.world.body_hit(start,ray_end,pos,r,h)
            if math.isfinite(hit):
                targets.append((hit,item))
        if not targets:
            return None,ray_end,wall<=1
        fraction,item=min(targets,key=lambda a:a[0])
        return item,advance(start,tuple(b-a for a,b in zip(start,ray_end)),fraction),True

    def hurt(self, target, amount, source, kind, origin=None):
        shooter=self.mecks[source] if 0<=source<self.count else None
        if isinstance(target,Mech):
            if target.hp<=0:
                return
            bearing=None if origin is None else math.atan2(origin[1]-target.pos[1],origin[0]-target.pos[0])
            rear=bearing is not None and abs(angle(bearing,target.aim[0]))>math.radians(target.stats["front_arc_deg"]/2)
            if kind!="zone":
                amount*=1-target.stats.get("armor",0)
            if rear:
                amount*=target.stats["rear_mult"]
            if kind=="laser" and any(w and w["kind"]=="mortar" for w in target.weapons):
                amount*=1+self.cfg["weapons"]["laser"]["cookoff"]
            if kind=="lightning":
                for w in target.weapons:
                    if w and w["kind"]=="laser":
                        w["charge"]=0.
                        target.laser_lock=self.t+self.cfg["weapons"]["lightning"]["short_s"]
            before_hp = target.hp
            target.hp -= amount
            if shooter and shooter is not target:
                shooter.mech_damage_dealt += min(before_hp, amount)
                shooter.last_mech_hit = self.t
                if target.hp < max(550, .40 * target.max_hp) <= before_hp:
                    target.weakened_by = shooter.id
            if target.controller:
                target.damage_sources[kind]+=amount
                self.record_damage(target, shooter, kind, min(before_hp, amount))
            target.damage_clock=self.t
            target.events.append({"type":"damaged","amount":amount,"weapon":kind,"bearing":bearing,"by":source,"rear":rear})
            ident=target.id
            if target.hp<=0:
                self.dead_this_tick.append(target)
                self.sound("death", target.pos, ident)
                target.death=self.t
                if shooter and shooter is not target:
                    shooter.kills+=1
                    shooter.events.append({"type":"kill","target":ident})
                self.scatter(target.pos,target.cubes+self.cfg["cubes"]["death_drop_extra"])
                for w in target.weapons:
                    if w and w["kind"]!="gun":
                        self.pickups.append({"id":self.id(),"kind":w["kind"],"pos":target.pos})
        else:
            if target["hp"]<=0:
                return
            before_hp = target["hp"]
            target["hp"] -= amount
            ident=target["id"]
            if target.get("kind")=="npc":
                target["attacker"]=source
            if target["hp"]<=0:
                self.scatter(target["pos"],2 if target.get("kind")=="npc" else 1)
                if target.get("kind")!="npc" and self.rng.random()<self.cfg["loot"]["crate_weapon_chance"]:
                    self.pickups.append({"id":self.id(),"kind":self.rng.choice(("mortar","laser","lightning")),
                                         "pos":target["pos"]})
        if shooter:
            if shooter is not target:
                shooter.damage_dealt += min(before_hp, amount)
            shooter.damage_clock=self.t
            shooter.events.append({"type":"hit","target":ident,"amount":amount,"weapon":kind})

    def record_damage(self, target, shooter, kind, amount):
        while self.recent_hits and self.recent_hits[0][0] < self.t - 2:
            self.recent_hits.popleft()
        self.recent_hits.append((self.t, shooter.id if shooter else None, kind, amount))
        if target.hp > 0:
            return
        damage = Counter()
        attackers = set()
        for _, source, weapon, value in self.recent_hits:
            damage[weapon] += value
            if source not in (None, target.id):
                attackers.add(source)
        self.death_audit = {
            "weapons": [w["kind"] for w in target.weapons if w],
            "fatal": kind,
            "fatal_tier": shooter.role if shooter and shooter is not target else None,
            "fatal_gap": round(math.dist(shooter.pos, target.pos), 1) if shooter else None,
            "attackers_2s": len(attackers),
            "damage_2s": {weapon: round(value, 1) for weapon, value in damage.items()},
        }

    def scatter(self,pos,count):
        for _ in range(count):
            point=advance(pos,unit(self.rng.random()*math.tau,0),self.rng.uniform(0,2.6))
            self.cubes.append({"id":self.id(),"pos":point})

    def update_hp(self, mech):
        c = self.cfg["cubes"]
        new_hp = mech.stats["hp"] * (1 + c["hp_per_cube"] * min(mech.cubes, c["stat_cap"]))
        mech.hp += new_hp - mech.max_hp
        mech.max_hp = new_hp

    def gain(self, mech, count):
        old = mech.cubes
        mech.cubes += count
        c = self.cfg["cubes"]
        self.update_hp(mech)
        scale = 1 + c["size_per_cube"] * mech.cubes
        for key in ("radius", "height", "eye_height", "muzzle_height"):
            mech.stats[key] = self.cfg["mech"][key] * scale
        mech.native = Stats.from_dict(mech.stats)
        if (mech.controller or mech.brain) and any(old < n <= mech.cubes for n in c["upgrade_thresholds"]):
            kinds = {w["kind"] for w in mech.weapons if w}
            choices = [u for u in self.cfg["upgrades"] if
                       (not u.get("requires") or u["requires"] in kinds)
                       and mech.upgrades.get(u["id"], 0) < u["max"]]
            if choices:
                mech.offer = {"id": self.id(), "choices": self.rng.sample(choices, min(len(choices), 3)),
                              "expires": self.t + self.cfg["bot"]["offer_timeout_s"]}

    def projectile(self, kind, source, start, direction, stats, damage):
        self.shots.append({"id":self.id(),"kind":kind,"by":source,"pos":start,
                           "vel":advance((0,0,0),direction,stats["speed"]), "damage":damage, "stats":stats,
                           "ttl":stats.get("ttl",stats.get("range",230)/stats["speed"])})
        if source == 0 and kind == "gun":
            target = self.mecks[0].controller.gun_target
            if target is not None and 0 < target < self.count and self.mecks[target].role == "medium":
                self.medium_gun_shots += 1
                self.shots[-1]["intended_medium"] = target

    def weapons(self, mech):
        action=mech.action
        desired=action.get("aim",mech.aim)
        yaw,pitch=mech.aim
        yaw+=max(-mech.stats["turn_rate"]*self.dt,min(mech.stats["turn_rate"]*self.dt,angle(desired[0],yaw)))
        pitch+=max(-mech.stats["pitch_rate"]*self.dt,min(mech.stats["pitch_rate"]*self.dt,desired[1]-pitch))
        pitch=max(mech.stats["min_pitch"],min(mech.stats["max_pitch"],pitch))
        mech.aim=[math.remainder(yaw,math.tau),pitch]
        cube_bonus=1+self.cfg["cubes"]["damage_per_cube"]*min(mech.cubes,self.cfg["cubes"]["stat_cap"])
        for i,w in enumerate(mech.weapons):
            if not w:
                continue
            kind,stats=w["kind"],w["stats"]
            w["cooldown"]=max(0,w["cooldown"]-self.dt)
            requested=action.get("fire",(False,False))[i]
            damage=stats["damage"]*cube_bonus
            if kind=="gun":
                if w["reload"]>0:
                    w["reload"]=max(0,w["reload"]-self.dt)
                    if w["reload"]==0: w["ammo"]=stats["mag"]
                if (w["ammo"]==0 or (action.get("reload") and w["ammo"]<stats["mag"])) and w["reload"]==0:
                    w["reload"]=stats["reload"]
                if not requested or w["reload"] or w["cooldown"]>0:
                    continue
                w["ammo"]-=1
            elif kind=="laser":
                if self.t<mech.laser_lock or w["cooldown"]>0:
                    continue
                if requested and w["held"]<stats["hold_s"]:
                    if w["charge"]==0:
                        mech.laser_actual_starts += 1
                        self.sound("laser_charge",mech.pos,mech.id)
                    if w["charge"]>=stats["charge"]:
                        w["held"]+=self.dt
                    w["charge"]=min(stats["charge"],w["charge"]+self.dt)
                    continue
                if w["charge"]<=0:
                    continue
                mech.laser_shots += 1
                mech.laser_full_shots += w["charge"] >= stats["charge"] - 1e-5
                charge=max(stats["min_charge"],w["charge"])
                damage*=stats["min_frac"]+(1-stats["min_frac"])*charge/stats["charge"]
                w.update(charge=0.,held=0.)
            elif not requested or w["cooldown"]>0:
                continue
            if kind=="lightning":
                if mech.state.energy<stats["energy"]:
                    continue
                mech.state.energy-=stats["energy"]
                mech.state.idle=0
            w["cooldown"]=stats["cooldown"]
            direction=unit(yaw,pitch)
            origin=raised(mech.pos,mech.stats["height"]+.5 if kind=="mortar" else mech.stats["muzzle_height"])
            start=advance(origin,direction,mech.stats["radius"]+.5)
            self.sound(kind,mech.pos,mech.id)
            if math.isfinite(self.world.ray(origin,start)):
                continue
            if kind in ("mortar","gun"):
                if kind=="gun":
                    spread=math.radians(stats["spread_deg"])
                    direction=unit(yaw+self.rng.gauss(0,spread),pitch+self.rng.gauss(0,spread))
                self.projectile(kind,mech.id,start,direction,stats,damage)
            elif kind=="laser":
                target,end,_=self.ray_target(start,advance(start,direction,stats["range"]),mech.id)
                self.beams.append({"by":mech.id,"kind":kind,"path":[start,end]})
                if target:
                    self.hurt(target,damage,mech.id,kind,start)
            else:
                self.lightning(mech,start,yaw,damage,stats)

    def lightning(self, mech, start, yaw, damage, stats):
        current=start
        used=set()
        path=[start]
        for step in range(stats["chain"]+1):
            candidates=[]
            for item in self.shootable(mech.id):
                ident=item.id if isinstance(item,Mech) else item["id"]
                if ident in used:
                    continue
                _,height=self.dimensions(item)
                target=raised(item.pos if isinstance(item,Mech) else item["pos"],height*.5)
                d=math.dist(current,target)
                if d>(stats["range"] if step==0 else stats["chain_range"]):
                    continue
                direction=math.atan2(target[1]-current[1],target[0]-current[0])
                if step==0 and abs(angle(direction,yaw))>math.radians(stats["cone_deg"]/2):
                    continue
                if not math.isfinite(self.world.ray(current,target)):
                    candidates.append((d,ident,target,item))
            if not candidates:
                break
            _,ident,end,item=min(candidates,key=lambda c:c[0])
            used.add(ident)
            self.hurt(item,damage,mech.id,"lightning",current)
            path.append(end)
            current=end
            damage*=stats["chain_falloff"]
        if len(path)>1:
            self.beams.append({"by":mech.id,"kind":"lightning","path":path})

    def bullets(self):
        active=[]
        for shot in self.shots:
            a,v=shot["pos"],shot["vel"]
            stats=shot["stats"]
            gravity=stats.get("gravity",0.)
            b=raised(advance(a,v,self.dt),-gravity*self.dt*self.dt/2)
            target,point,collision=self.ray_target(a,b,shot["by"])
            if target is not None:
                if isinstance(target, Mech) and shot.get("intended_medium") == target.id:
                    self.medium_gun_hits += 1
                self.hurt(target,shot["damage"],shot["by"],shot["kind"],a)
            if collision and shot["kind"]=="mortar":
                radius=stats["splash_radius"]
                victims=self.shootable(shot["by"])
                if shot["by"]<self.count and self.mecks[shot["by"]].hp>0:
                    victims.append(self.mecks[shot["by"]])
                for item in victims:
                    _,height=self.dimensions(item)
                    pos=raised(item.pos if isinstance(item,Mech) else item["pos"],height*.35)
                    d=math.dist(pos,point)
                    if d>radius or math.isfinite(self.world.ray(raised(point,.1),pos)):
                        continue
                    fraction=max(stats["splash_edge"],1-d/radius)
                    self.hurt(item,stats["splash"]*fraction*(.5 if isinstance(item,Mech) and item.id==shot["by"] else 1),
                              shot["by"],"mortar",point)
                    if isinstance(item,Mech) and item.hp>0:
                        step=unit(math.atan2(pos[1]-point[1],pos[0]-point[0]),0)
                        item.state.vel=Vec(*raised(advance(item.state.vel.as_tuple(),step,stats["knock"]*fraction),
                                                  stats["knock_up"]*fraction))
                        item.state.grounded=0
                        item.state.energy=max(0,item.state.energy-stats["drain"]*fraction)
                self.sound("explosion",point,shot["by"])
            if not collision and shot["ttl"]>self.dt:
                shot.update(pos=b,vel=raised(v,-gravity*self.dt),ttl=shot["ttl"]-self.dt)
                active.append(shot)
        self.shots=active

    def drones_step(self, alive):
        cfg=self.cfg["npc"]
        for drone in self.drones:
            if drone["hp"]<=0:
                continue
            drone["cooldown"]=max(0,drone["cooldown"]-self.dt)
            source=raised(drone["pos"],2.5)
            near=[m for m in alive if m.hp>0 and horizontal(m.pos,drone["pos"])<
                  (cfg["deaggro_range"] if m.id==drone["attacker"] else cfg["aggro_range"])]
            target=min(near,key=lambda m:horizontal(m.pos,drone["pos"]),default=None)
            if target is None or drone["cooldown"]>0:
                continue
            point=raised(target.pos,target.stats["height"]*.45)
            if math.isfinite(self.world.ray(source,point)):
                continue
            direction=tuple((b-a)/max(.1,math.dist(source,point)) for a,b in zip(source,point))
            self.projectile("npc",drone["id"],source,direction,{"speed":cfg["projectile_speed"],"range":110},cfg["damage"])
            self.sound("npc_gun",drone["pos"],drone["id"])
            drone["cooldown"]=cfg["cooldown"]

    def supplies(self, alive, zone):
        cfg=self.cfg["drops"]
        called=int(round(self.t/self.dt))
        if any(called==round(t/self.dt) for t in cfg["times"]):
            point=self.open_point(zone["next_center"])
            self.drops.append({"id":self.id(),"pos":point,"radius":cfg["radius"],"land":self.t+cfg["fall_s"],
                               "holder":None,"progress":0.,"capture_s":cfg["capture_s"]})
        for drop in self.drops[:]:
            if self.t<drop["land"]:
                continue
            holders=[m for m in alive if m.hp>0 and horizontal(m.pos,drop["pos"])<drop["radius"]]
            if not holders:
                drop.update(holder=None,progress=0.)
            elif len(holders)==1:
                mech=holders[0]
                if drop["holder"]!=mech.id:
                    drop.update(holder=mech.id,progress=0.)
                drop["progress"]+=self.dt
                if drop["progress"]>=drop["capture_s"]:
                    self.gain(mech,cfg["cubes"])
                    mech.hp,mech.state.energy=mech.max_hp,mech.stats["energy"]
                    self.drops.remove(drop)

    def frame(self):
        zone=self.zone()
        alive=[m for m in self.mecks if m.hp>0]
        if self.tick%self.cfg["match"]["decide_every"]==0:
            self.set_actions(alive,zone)
            self.beams=[]
        for m in alive:
            if m.hp<=0:
                continue
            action=m.action
            move=action.get("move",(0,0))
            scale = m.stats["charge_move_mult"] if any(w and w.get("charge", 0) > 0 for w in m.weapons) else 1.
            self.world.step(m.state,m.native,Control(*move,action.get("boost",False),action.get("jump",False)),
                            self.dt,scale)
            if m.controller and self.tick < round(3.5 / self.dt):
                m.spawn_distance_3_5 = horizontal(m.pos, m.spawn_position)
                m.spawn_distance_peak_3_5 = max(m.spawn_distance_peak_3_5, m.spawn_distance_3_5)
            if m.controller and m.state.grounded and self.world.on_roof(m.pos):
                m.roof_seconds += self.dt
                if m.roof_since == -math.inf:
                    m.roof_since = self.t
                if m.hp > .80 * m.max_hp and self.t - max(m.roof_since, m.last_mech_hit) >= 3.5:
                    m.idle_roof_seconds += self.dt
            else:
                m.roof_since = -math.inf
            if horizontal(m.pos,zone["center"])>zone["radius"]:
                self.hurt(m,zone["dps"]*self.dt,-1,"zone")
            elif self.t-m.damage_clock>=m.stats["regen_delay"]:
                m.hp=min(m.max_hp,m.hp+m.stats["regen_per_s"]*self.dt)
                if m.weakened_by >= 0 and m.hp >= max(550, .40 * m.max_hp):
                    self.mecks[m.weakened_by].weak_escapes += 1
                    m.weakened_by = -1
            if self.t-m.last_step>=.6 and math.hypot(m.state.vel.x,m.state.vel.y)>5:
                m.last_step=self.t
                self.sound("boost" if action.get("boost") else "step",m.pos,m.id)
        for m in alive:
            if m.hp>0:
                self.weapons(m)
        self.drones_step(alive)
        self.bullets()
        for cube in self.cubes[:]:
            candidate=min((m for m in alive if m.hp>0 and abs(m.pos[2]-cube["pos"][2])<=4),
                          key=lambda m:(horizontal(m.pos,cube["pos"]),m.id),default=None)
            if candidate and horizontal(candidate.pos,cube["pos"])<=self.cfg["cubes"]["pickup_radius"]:
                self.gain(candidate,1)
                self.cubes.remove(cube)
        self.supplies(alive,zone)
        self.sounds=[s for s in self.sounds if self.t-s[0]<=.65]
        if self.dead_this_tick:
            place = 1 + sum(m.hp > 0 for m in self.mecks)
            for m in self.dead_this_tick:
                m.place = place
            self.dead_this_tick.clear()
        if self.director and self.probe is None and self.t + self.dt >= 160:
            player = self.mecks[0]
            self.probe = {"kills": player.kills, "score": self.count - player.place if player.place else self.count - sum(m.hp > 0 for m in self.mecks),
                          "hp_fraction": round(max(0, player.hp) / player.max_hp, 3)}
        if self.capture and self.tick%4==0:
            self.frames.append({"t":round(self.t,2),"zone":[*zone["center"],zone["radius"]],
                                "mechs":[[m.id,*map(lambda x:round(x,2),m.pos),round(m.hp),m.kills,round(m.state.energy),
                                          self.variant if m.controller else m.role] for m in alive if m.hp>0],
                                "shots":[p["pos"] for p in self.shots],
                                "beams":[b["path"] for b in self.beams]})
        self.tick+=1
        self.t=self.tick*self.dt

    def run(self):
        while self.t<self.seconds and sum(m.hp>0 for m in self.mecks)>1:
            if self.mecks[0].hp<=0:
                break
            self.frame()
        living = sorted((m for m in self.mecks if m.hp > 0), key=lambda m: (-m.hp, m.id))
        if self.t >= self.cfg["match"]["duration_s"] or len(living) == 1:
            for place, m in enumerate(living, 1):
                m.place = place
        m = self.mecks[0]
        label = self.variant
        stats = {"role": label, "slot": m.id, "kills": m.kills, "place": m.place or None,
                 "points": self.count - m.place if m.place else None,
                 "time": round(m.death if m.hp <= 0 else self.t, 2),
                 "eliminated": m.hp <= 0, "cubes": m.cubes, "roof_seconds": round(m.roof_seconds, 2),
                 "damage_taken": {kind: round(value, 1) for kind, value in m.damage_sources.items()},
                 "damage_dealt": round(m.damage_dealt, 1), "mech_damage_dealt": round(m.mech_damage_dealt, 1),
                 "weak_escapes": m.weak_escapes, "weak_rotations": m.weak_rotations,
                 "idle_roof_seconds": round(m.idle_roof_seconds, 2),
                 "spawn_distance_3_5": round(m.spawn_distance_3_5, 2),
                 "spawn_distance_peak_3_5": round(m.spawn_distance_peak_3_5, 2),
                 "first_sidearm": m.first_sidearm, "first_laser": m.first_laser, "laser_actual_starts": m.laser_actual_starts,
                 "laser_shots": m.laser_shots, "laser_full_shots": m.laser_full_shots}
        if self.director:
            stats["probe_160"] = self.probe
            if self.probe is None and (m.hp <= 0 or m.place):
                stats["probe_160"] = {"kills": m.kills, "score": self.count - m.place,
                                      "hp_fraction": round(max(0, m.hp) / m.max_hp, 3)}
            stats["first_activations"] = list(self.activations.values())
        if self.history:
            total, actions = (sorted(values) for values in zip(*self.history))
            rank = math.ceil(len(actions) * .99) - 1
            stats.update(p99_ms=round(total[rank], 2), max_ms=round(total[-1], 2),
                         bot_p99_ms=round(actions[rank], 2), bot_max_ms=round(actions[-1], 2),
                         decision_frames=len(actions))
        stats.update(death_audit=self.death_audit, medium_gun_shots=self.medium_gun_shots,
                     medium_gun_hits=self.medium_gun_hits,
                     plugin_calls=m.controller.plugin_calls)
        result = {label: stats}
        return result


def run_match(job, hello, args):
    variant, seed = job
    seconds = args.seconds if args.seconds is not None else hello["config"]["match"]["duration_s"]
    sim = Simulation(hello, args.arena, seed, args.mode, variant, seconds, args.waves, args.output is not None)
    try:
        result = sim.run()
        if args.output:
            write_replay(args.output / f"{args.mode}-{variant}-{seed}", sim.world.data, sim.frames, result)
        return seed, result
    finally:
        sim.world.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arena", type=Path, default=Path("arena"))
    parser.add_argument("--fixture", type=Path, default=Path("check/stream.jsonl.gz"))
    parser.add_argument("--mode", choices=("practice",), default="practice")
    parser.add_argument("--variant", choices=VARIANTS, action="append")
    parser.add_argument("--seed", type=int, action="append")
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--waves", nargs=4, type=float, default=DEFAULT_WAVES,
                        metavar=("PAIR", "SURROUND", "PRESSURE", "ALL"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    if tuple(sorted(set(args.waves))) != tuple(args.waves) or args.waves[0] < 0 or not all(map(math.isfinite, args.waves)):
        parser.error("--waves must be four strictly increasing nonnegative times")
    if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error("--seconds must be positive")
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    with gzip.open(args.fixture, "rt", encoding="utf-8") as fixture:
        hello = json.loads(next(fixture))
    hello["map"]["dir"] = str(args.arena.resolve())
    win, kills, points, death, probe = Counter(), {}, {}, {}, {}
    seeds = args.seed or PRACTICE_SEEDS
    variants = args.variant or ("laser-cover",)
    jobs = [(variant, seed) for variant in variants for seed in seeds]
    pool = ProcessPoolExecutor(max_workers=args.jobs) if args.jobs > 1 else None
    try:
        results = (pool.map(run_match, jobs, repeat(hello), repeat(args)) if pool else
                   (run_match(job, hello, args) for job in jobs))
        for seed, result in results:
            print(json.dumps({"seed": seed, "mode": args.mode, "waves": args.waves if args.mode == "practice" else None,
                              "results": result}), flush=True)
            for score in result.values():
                label = score["role"]
                win[label] += score["place"] == 1
                kills.setdefault(label, []).append(score["kills"])
                if score["eliminated"]:
                    death.setdefault(label, []).append(score["time"])
                if score["points"] is not None:
                    points.setdefault(label, []).append(score["points"])
                if score.get("probe_160") is not None:
                    probe.setdefault(label, []).append(score["probe_160"]["score"])
    finally:
        if pool:
            pool.shutdown()
    def means(groups):
        return {label: sum(values) / len(values) for label, values in groups.items()}

    mean_points = means(points)
    ranked = {label: len(scores) for label, scores in points.items()}
    gate_pass = len(set(seeds)) >= 12 and all(
        ranked.get(variant) == len(seeds) and mean_points[variant] >= 5.5 for variant in variants)
    print(json.dumps({"wins": dict(win), "samples": {k: len(v) for k, v in kills.items()},
                      "ranked": ranked, "mean_kills": means(kills), "mean_points": mean_points,
                      "mean_death_s": means(death), "mean_probe_160": means(probe), "gate_pass": gate_pass}),
          flush=True)
    if args.gate and not gate_pass:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
