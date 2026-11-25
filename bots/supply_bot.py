"""Supply: the reference bot for the strategic ruleset (STRATEGIC.md).

It fights as vanguard does, and adds what the supply rules need:

- It builds with each city's own bread, keeping some back to feed the
  units around the city.
- It hauls wheat. Sites have no owners, so its wagons fetch the wheat of
  every site on its side of the map, no farther from its cities than from
  the enemy's, and unload it in the nearest city, where it becomes bread.
  It builds about one wagon for every two such sites.
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
# It keeps no more units than this share of what its bread income can feed,
# plus what its stockpile would feed for STOCK_TURNS turns.
FED_SHARE = 1.0
STOCK_TURNS = 100


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

    def advance(self):
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

    # Wagons.

    def haul_sites(self):
        """The sites on our side of the map, with no enemy on them: those a
        wagon can walk to from one of our cities, and no farther than from
        the enemy's nearest city."""
        s, far = self.state, 1 << 30

        def near(cities, x):
            return min((self.city_field[c.id].get(x.hex, far) for c in cities), default=far)

        ours = s.my_cities()
        theirs = [c for c in s.cities if c.owner not in (0, self.player)]
        return [x for x in s.sites
                if x.hex not in self.enemy_hex and near(ours, x) < far and near(ours, x) <= near(theirs, x)]

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
        """Bread a turn: what our cities make, plus, if it hauls, the wheat
        the sites it hauls from grow."""
        made = self.rules.constants.get("city_bread", 0) * len(self.state.my_cities())
        if self.haul:
            made += sum(x.output for x in self.haul_sites())
        return made

    def units_fed(self):
        """How many units the bread income can feed, each eating its base
        meal plus moving and attacking some of the time, and as many more as
        the bread stockpiled in our cities and wagons would feed for
        STOCK_TURNS turns. Without those, a player with a small income would
        keep a small army however much bread it had saved."""
        s, k = self.state, self.rules.constants
        eats = k.get("eat_base", 0) + MOVING * k.get("eat_move", 0) + ATTACKING * k.get("eat_attack", 0)
        stock = sum(c.bread for c in s.my_cities()) + sum(u.bread for u in s.my_units())
        return int((FED_SHARE * self.income() + stock / STOCK_TURNS) / max(1e-9, eats * self.ration_cost))

    def supply_wagons(self):
        """The wagons that haul and feed: all that can move."""
        return [u for u in self.state.my_units() if self.types[u.type].carry and not u.fresh]

    def run_wagons(self):
        s = self.state
        wagons = self.supply_wagons()
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
            # Hauling comes first while no wagon hauls: without the wheat,
            # there's soon no bread to feed with.
            if open_sites and (feeders > 0 or not hungry or not hauled):
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
                self.haul_from(w, sid, sid in site_ids)
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
        """Our city nearest w with no enemy on it and, if need is above 0,
        that much bread to spare beyond what it keeps, preferring one with no
        enemy next to it either, such as a port no enemy ship blockades. With
        need 0, any city will do, however little bread it holds, as when
        unloading wheat."""
        cities = [c for c in self.state.my_cities()
                  if c.hex not in self.enemy_hex and (need <= 0 or c.bread - self.keep(c) >= need)]
        known = [(any(n in self.enemy_hex for n in bb.neighbors(c.hex)), self.city_field[c.id].get(w.hex), c.id, c)
                 for c in cities]
        known = [k for k in known if k[1] is not None]
        return min(known)[3] if known else None

    def enter(self, w, city):
        """Walks wagon w toward city, keeping clear of the enemy, and into it,
        making room there if our units fill it. Once the city is in reach it
        goes straight in, past any enemy beside it: a wagon that kept clear of
        the galleys blockading a port would never get in."""
        if w.hex == city.hex:
            return
        if bb.distance(w.hex, city.hex) == 1:
            self.make_room(city, self.city_field[city.id])
        path = self.reach(w).get(city.hex)
        if path:
            self.go(w, path)
        else:
            self.walk(w, self.city_field[city.id], WAGON_CAUTION)

    def make_room(self, city, field):
        """If our units fill the city, moves one that hasn't acted off it, to
        the hex it can reach nearest the goals of field, so a wagon can come
        in. Without this, units with nothing left to do can sit on the city
        a wagon brings wheat to, and it never gets in."""
        s = self.state
        if self.occ.get(city.hex, 0) < self.limit:
            return
        cities = {c.hex for c in s.cities}
        for u in sorted(s.my_units(), key=lambda u: u.id):
            if u.hex != city.hex or u.carrier or self.types[u.type].carry or not self.can_leave(u):
                continue
            options = [(field.get(h, 1 << 30), len(dirs), dirs) for h, dirs in self.reach(u).items()
                       if dirs and h not in cities]
            if options:
                self.go(u, min(options)[2])
                return

    def can_leave(self, u):
        """Whether make_room may move u off a city."""
        return True

    def haul_from(self, w, sid, open_):
        """Fetches wheat from site sid, or brings a load home. open_ is
        whether we still haul from the site."""
        s = self.state
        site = s.sites[sid]
        if w.wheat and (w.wheat >= min(self.cargo_max, MIN_LOAD) or not open_ or site.wheat == 0):
            city = self.nearest_city(w)
            if city is None:
                return
            self.enter(w, city)
            if w.hex == city.hex:
                self.unload(w)
                self.jobs.pop(w.id, None)   # take a new job next turn
            return
        if not open_:
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
            self.enter(w, city)
            if w.hex == city.hex:
                self.load(w, min(self.cargo_max - w.wheat - w.bread, city.bread - self.keep(city)))
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

    def keep(self, city):
        """Bread a city holds back from building and from wagons, to feed the
        units around it."""
        return KEEP

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
            budget = city.bread - self.keep(city)
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
