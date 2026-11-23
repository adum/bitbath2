"""Supply: the reference bot for the strategic ruleset (STRATEGIC.md).

It fights as vanguard does, and adds what the supply rules need:

- It builds with each city's own bread, keeping some back to feed the
  units around the city.
- It takes resource sites as well as cities: before the army marches, the
  nearest free capturers go for the sites it doesn't hold, richer first.
- It hauls wheat. Wagons fetch it from its sites that aren't next to one of
  its cities, which deliver by themselves, and unload it in the nearest
  city, where it becomes bread. It builds about one wagon for every two
  such sites.
- It keeps the army fed. A unit whose rations won't last the walk to bread
  goes to eat, and other wagons carry bread from cities to where units are
  going hungry.

With --no-haul it never hauls wheat, so tournaments can measure what
hauling is worth. In games without supply it plays exactly as vanguard.

Usage: python bots/supply_bot.py [--no-haul]
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vanguard_bot as vb  # noqa: E402
import bitbath as bb  # noqa: E402

# A unit goes to eat when its rations would last fewer than this many turns
# beyond the walk to bread.
SPARE = 4
# Bread a city keeps back from building, to feed the units around it.
KEEP = 60
# A hauler waits for this much wheat before it fetches a load, unless the
# site holds less in all.
MIN_LOAD = 200
# A feeding wagon reloads at a city when its bread falls below this.
REFILL = 100
# A unit is hungry enough for a wagon to come to it when it has eaten this
# many rations since it last refilled.
HUNGRY = 8
# How far, in turns, a capturer goes for a site.
SITE_TURNS = 8
# At most this share of the army hunts sites at once.
SITE_SHARE = 0.25
# Wagons are worth a lot and can't fight back: they weigh danger this many
# times as heavily as other units.
WAGON_CAUTION = 1.5
# Most wagons it keeps.
MAX_WAGONS = 30
# The share of turns a unit is assumed to move, and to attack, when working
# out how much bread the army eats.
MOVING, ATTACKING = 0.6, 0.1
# One feeding wagon for every this many hungry units.
FEED_GROUP = 6
# It keeps no more units than this share of what its bread income can feed.
FED_SHARE = 1.0


class SupplyBot(vb.VanguardBot):
    def __init__(self, haul=True):
        super().__init__()
        self.haul = haul

    def on_init(self):
        super().on_init()
        self.supply = self.rules.supply
        if not self.supply:
            return
        c = self.rules.constants
        self.ration_max, self.ration_cost, self.cargo_max = c["ration_max"], c["ration_cost"], c["cargo_max"]
        self.wagon = next((code for code, t in self.types.items() if t.carry), None)
        self.site_field = {sid: self.distance_field([h], blocked=set()) for sid, h, *_ in self.map.sites}
        self.jobs = {}      # wagon id -> ("haul", site id) or ("feed", None)
        self.busy = set()   # units the supply plans move this turn
        self.food_field = {}

    # Vanguard's phases, with the supply plans around them.

    def ready(self):
        units = super().ready()
        if not self.supply:
            return units
        return [u for u in units if u.id not in self.busy and not self.types[u.type].carry]

    def capture_in_place(self):
        if self.supply:
            self.busy = set()
            self.jobs = {w: j for w, j in self.jobs.items() if w in self.state.units}
            self.feed_army()
        super().capture_in_place()

    def rush_cities(self):
        super().rush_cities()
        if self.supply:
            self.rush_sites()

    def advance(self):
        if self.supply:
            self.seek_sites()
        super().advance()
        if self.supply:
            self.run_wagons()

    # Moving.

    def reach(self, u, block_cities=True):
        """Vanguard's reach, on a leash: a unit ends a move only where it could
        still walk back to bread before its rations run out, unless the move
        takes it closer to bread. Wagons that carry bread move the leash
        forward."""
        out = super().reach(u, block_cities)
        if not self.supply or not self.food_field or self.types[u.type].carry:
            return out
        field, move = self.food_field, self.types[u.type].move
        here = field.get(u.hex)
        if here is None:
            return out
        budget = (u.rations - self.meal(moved=True) - SPARE) / 2 * move
        return {h: p for h, p in out.items()
                if h == u.hex or field.get(h, 1 << 30) <= budget or field.get(h, 1 << 30) < here}

    def walk(self, u, field, caution=1):
        """Moves u to the hex it can reach that is nearest the goals of field,
        weighing the enemy's reach. Returns whether enemies came into view."""
        best = None
        for h, dirs in self.reach(u).items():
            d = field.get(h)
            if d is None:
                continue
            key = (-d - caution * self.risk_weight * min(6, self.risk(h, u.type)), -len(dirs))
            if best is None or key > best[0]:
                best = (key, dirs)
        return bool(best and best[1]) and self.go(u, best[1])

    def turns(self, u, field):
        d = field.get(u.hex)
        return None if d is None else d / self.types[u.type].move

    # Food.

    def food_spots(self):
        """Hexes where a unit refills at the start of the next turn: on or
        next to one of our cities or wagons with bread to spare."""
        s, enough = self.state, 3 * self.ration_cost
        sources = [c.hex for c in s.my_cities() if c.bread >= enough]
        sources += [u.hex for u in s.my_units() if self.types[u.type].carry and u.bread >= enough]
        spots = set()
        for h in sources:
            spots.update(n for n in [h] + bb.neighbors(h) if n in self.defense)
        return spots

    def feed_army(self):
        """Units whose rations won't last the walk to bread go and eat. Those
        already where they will refill stay there."""
        spots = self.food_spots()
        self.food_field = self.distance_field(spots) if spots else {}
        if not spots:
            return
        self.cover = self.coverage()
        for u in self.state.my_units():
            t = self.types[u.type]
            if t.carry or u.fresh or u.acted:
                continue
            walk = self.turns(u, self.food_field)
            if walk is None or u.rations > 2 * math.ceil(walk) + SPARE:
                continue
            self.busy.add(u.id)
            if self.can_capture(u):
                self.capture(u)
            elif walk > 0 and not self.out_of_time():
                self.walk(u, self.food_field)

    # Sites.

    def rush_sites(self):
        """Capturers that can reach a site we don't hold this turn take it,
        unless the enemy is much stronger there."""
        me = self.player
        for site in self.state.sites:
            if site.owner == me or site.hex in self.enemy_hex or self.out_of_time():
                continue
            if self.threat.get(site.hex, 0) > vb.CAUTION * (self.support.get(site.hex, 0) + 100):
                continue
            field = self.site_field[site.id]
            near = [u for u in self.ready() if u.mp > 0 and self.types[u.type].capture
                    and field.get(u.hex, 1 << 30) <= u.mp]
            for u in sorted(near, key=lambda u: (-u.hp, u.id)):
                path = self.reach(u).get(site.hex)
                if path is not None:
                    self.go(u, path)
                    if self.can_capture(u):
                        self.capture(u)
                    break

    def seek_sites(self):
        """Sends the nearest free capturers toward the sites we don't hold,
        one each, richer sites first."""
        self.refresh()
        self.cover = self.coverage(True)
        s, me = self.state, self.player
        free = [u for u in self.ready() if u.mp > 0 and self.types[u.type].capture]
        army = sum(1 for u in s.my_units() if not self.types[u.type].carry)
        budget = max(2, int(army * SITE_SHARE))
        plans = []
        for site in s.sites:
            if site.owner == me or self.threat.get(site.hex, 0) > 300:
                continue
            field = self.site_field[site.id]
            for u in free:
                turns = self.turns(u, field)
                if turns is not None and turns <= SITE_TURNS:
                    plans.append((turns - site.output / 20, site.id, u.id))
        plans.sort()
        sent, used = set(), set()
        for _, sid, uid in plans:
            if budget <= 0 or self.out_of_time():
                break
            if sid in sent or uid in used or uid not in s.units:
                continue
            sent.add(sid)
            used.add(uid)
            budget -= 1
            u = s.units[uid]
            self.busy.add(uid)
            self.walk(u, self.site_field[sid])
            if self.can_capture(u):
                self.capture(u)

    # Wagons.

    def haul_sites(self):
        """Our sites that don't deliver by themselves and have no enemy on them."""
        s = self.state
        ours = {c.hex for c in s.my_cities()}
        return [x for x in s.my_sites()
                if x.hex not in self.enemy_hex and not any(n in ours for n in bb.neighbors(x.hex))]

    def hungry_units(self):
        """Our units that have eaten a while and aren't where they will refill."""
        spots = self.food_spots()
        return [u for u in self.state.my_units()
                if not self.types[u.type].carry and u.rations <= self.ration_max - HUNGRY and u.hex not in spots]

    def wagons_wanted(self):
        hauls = len(self.haul_sites()) if self.haul else 0
        feeds = 1 + len(self.hungry_units()) // FEED_GROUP
        return min(MAX_WAGONS, (hauls + 1) // 2 + feeds)

    def income(self):
        """Bread a turn: what our cities make, plus the wheat of the sites
        that deliver by themselves, and of the others if it hauls."""
        s = self.state
        ours = {c.hex for c in s.my_cities()}
        made = self.rules.constants.get("city_bread", 0) * len(ours)
        for x in s.my_sites():
            if self.haul or any(n in ours for n in bb.neighbors(x.hex)):
                made += x.output
        return made

    def units_fed(self):
        """How many units the bread income can feed, each eating its base
        meal plus moving and attacking some of the time."""
        c = self.rules.constants
        eats = c.get("eat_base", 0) + MOVING * c.get("eat_move", 0) + ATTACKING * c.get("eat_attack", 0)
        return int(FED_SHARE * self.income() / max(1e-9, eats * self.ration_cost))

    def run_wagons(self):
        s = self.state
        wagons = [u for u in s.my_units() if self.types[u.type].carry and not u.fresh]
        if not wagons:
            return
        self.refresh()
        self.cover = self.coverage(True)
        sites = self.haul_sites() if self.haul else []
        site_ids = {x.id for x in sites}
        # Keep jobs that still make sense: a hauler with a load finishes its
        # trip, and a feeder with bread keeps feeding.
        hauled = {}
        for w in wagons:
            job = self.jobs.get(w.id)
            if job and job[0] == "haul" and (job[1] in site_ids or w.wheat):
                hauled[job[1]] = hauled.get(job[1], 0) + 1
            elif job and (job[0] == "haul" or w.bread < REFILL):
                self.jobs.pop(w.id)
        # Free wagons take the sites nobody hauls from, then feed the army.
        feeders = sum(1 for w in wagons if self.jobs.get(w.id, ("", 0))[0] == "feed")
        hungry = bool(self.hungry_units())
        for w in wagons:
            if w.id in self.jobs:
                continue
            open_sites = [x for x in sites if x.id not in hauled]
            if open_sites and (feeders > 0 or not hungry):
                best = max(open_sites, key=lambda x: (self.load_worth(w, x), -x.id))
                self.jobs[w.id] = ("haul", best.id)
                hauled[best.id] = hauled.get(best.id, 0) + 1
            else:
                self.jobs[w.id] = ("feed", None)
                feeders += 1
        targets = []
        for w in sorted(wagons, key=lambda w: w.id):
            if self.out_of_time():
                return
            if w.id not in s.units:
                continue
            kind, sid = self.jobs[w.id]
            if kind == "haul":
                self.haul_from(w, sid)
            else:
                self.feed_from(w, targets)

    def load_worth(self, w, site):
        """Wheat per turn a trip from w to site and back would bring."""
        there = self.turns(w, self.site_field[site.id])
        if there is None:
            return -1
        back = min([self.city_field[c.id].get(site.hex, 1 << 30) for c in self.state.my_cities()], default=1 << 30)
        trip = there + back / self.types[w.type].move + 2
        return min(self.cargo_max, site.cap, site.wheat + site.output * there) / trip

    def nearest_city(self, w, need=0):
        """Our city nearest w that has `need` bread to spare and no enemy on it."""
        cities = [c for c in self.state.my_cities() if c.hex not in self.enemy_hex and c.bread - KEEP >= need]
        known = [(self.city_field[c.id].get(w.hex), c.id, c) for c in cities]
        known = [k for k in known if k[0] is not None]
        return min(known)[2] if known else None

    def haul_from(self, w, sid):
        s = self.state
        site = s.sites[sid]
        if w.wheat and (w.wheat >= min(self.cargo_max, MIN_LOAD) or site.owner != self.player or site.wheat == 0):
            city = self.nearest_city(w)
            if city is None:
                return
            if w.hex != city.hex:
                self.walk(w, self.city_field[city.id], WAGON_CAUTION)
            if w.hex == city.hex:
                self.unload(w)
                self.jobs.pop(w.id, None)   # take a new job next turn
            return
        if site.owner != self.player:
            self.jobs.pop(w.id, None)
            return
        if w.hex != site.hex:
            self.walk(w, self.site_field[sid], WAGON_CAUTION)
        # On the site, it waits for a load worth the trip, guarding the pile.
        if w.hex == site.hex and site.wheat >= min(MIN_LOAD, site.cap):
            self.load(w)

    def feed_from(self, w, targets):
        """Carries bread to the hungriest group of units out of reach of
        bread, reloading at a city when it runs low."""
        s = self.state
        hungry = self.hungry_units()
        if w.bread < REFILL:
            if not hungry:
                return
            city = self.nearest_city(w, need=REFILL * 2)
            if city is None:
                return
            if w.hex != city.hex:
                self.walk(w, self.city_field[city.id], WAGON_CAUTION)
            if w.hex == city.hex:
                self.load(w, min(self.cargo_max - w.wheat - w.bread, city.bread - KEEP))
            return
        # The hungriest group not already served by another wagon.
        best = None
        for u in hungry:
            if any(bb.distance(u.hex, t) <= 3 for t in targets):
                continue
            weight = sum(self.ration_max - v.rations for v in hungry if bb.distance(u.hex, v.hex) <= 2)
            key = (weight, -bb.distance(w.hex, u.hex), -u.id)
            if best is None or key > best[0]:
                best = (key, u.hex)
        if best is None:
            return
        targets.append(best[1])
        goal = [h for h in [best[1]] + bb.neighbors(best[1]) if h in self.defense]
        self.walk(w, self.distance_field(goal), WAGON_CAUTION)

    def report(self):
        """A line in the replay about this turn's supply."""
        s = self.state
        units = [u for u in s.my_units() if not self.types[u.type].carry]
        jobs = [j[0] for w, j in self.jobs.items() if w in s.units]
        self.debug_text("supply: %d units, %d busy, %d on half rations, %d starving; wagons %d hauling, %d feeding; "
                        "%d bread in cities, %d a turn, feeds %d units" % (
                            len(units), len(self.busy), sum(1 for u in units if u.rations <= self.ration_max // 2),
                            sum(1 for u in units if u.rations == 0), jobs.count("haul"), jobs.count("feed"),
                            sum(c.bread for c in s.my_cities()), self.income(), self.units_fed()))

    # Building.

    def build_all(self):
        if not self.supply:
            return super().build_all()
        self.report()
        s, types = self.state, self.types
        self.sync()
        scores = self.unit_scores()
        if self.wagon:
            scores.pop(self.wagon, None)    # wagons are built by need, not to fight
        for code, t in types.items():
            if t.naval:
                scores.pop(code, None)      # ships need a port, and a plan for them
        top = max(scores.values()) or 1
        share = {code: (score / top) ** vb.SHARE_POWER for code, score in scores.items()}
        count = dict.fromkeys(types, 0)
        for u in s.my_units():
            count[u.type] += 1
        targets = [c for c in s.cities if c.owner != self.player]

        def urgency(city):
            near = min([self.city_field[c.id].get(city.hex, 1 << 30) for c in targets], default=0)
            return (-self.threat.get(city.hex, 0), near, city.id)

        cities = [c for c in sorted(s.my_cities(), key=urgency)
                  if not (c.built or c.hex in self.enemy_hex or self.occ.get(c.hex, 0) >= self.limit)]
        wagons = self.wagons_wanted() if self.wagon else 0
        # Units beyond what the bread income can feed would only starve.
        room = self.units_fed() - len(s.my_units())
        for city in cities:
            if room <= 0:
                break
            budget = city.bread - KEEP
            if self.wagon and count[self.wagon] < wagons and budget >= types[self.wagon].cost \
                    and not self.threat.get(city.hex, 0):
                if self.build(city, self.wagon):
                    count[self.wagon] += 1
                    room -= 1
                    self.occ[city.hex] = self.occ.get(city.hex, 0) + 1
                continue
            total = sum(n for code, n in count.items() if code in share)
            can_capture = sum(n for code, n in count.items() if code in share and types[code].capture)
            options = [code for code in share if types[code].cost <= budget and share[code] >= vb.MIN_SHARE]
            if can_capture * 2 < total + 1 or total < vb.EARLY_ARMY:
                options = [code for code in options if types[code].capture] or options
            if not options:
                continue
            code = max(options, key=lambda c: (share[c] / (count[c] + 1), share[c]))
            if self.build(city, code):
                count[code] += 1
                room -= 1
                self.occ[city.hex] = self.occ.get(city.hex, 0) + 1


if __name__ == "__main__":
    bb.run(SupplyBot(haul="--no-haul" not in sys.argv[1:]))
