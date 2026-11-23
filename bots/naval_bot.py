"""Naval: the reference bot for the strategic ruleset's ships (STRATEGIC.md §6).

It plays as the supply bot does on land, and adds a navy:

- Expeditions. Its destinations are the cities on landmasses where it holds
  no city, and its own cities on landmasses where the enemy still holds
  some, which need reinforcing. Transports wait off its ports while land
  units walk over and board: first units with nothing left to take on
  their own landmass, then a share of the rest. Loaded transports wait for
  each other and sail as a convoy, all for one destination at a time, so
  that they land together. Units land on the city if they can, or as near
  it as they can, and take it. The empty transports go back for more.
- Galleys. Each turn a galley strikes the best target it can reach: a
  transport above all, since everything aboard sinks with it, then enemy
  galleys it would beat, then enemy units on the coast. With nothing to
  hit, it escorts a loaded transport, or blockades the nearest enemy port,
  which stops the port's bread and catches what sails out. It goes back to
  port to heal and eat when it must.
- It builds transports for the idle part of its army while there are
  destinations, and galleys to go with them and to answer enemy ships.

With --no-bombard its galleys never attack land units, so tournaments can
measure what bombardment is worth. In games without ships it plays exactly
as the supply bot.

Usage: python bots/naval_bot.py [--no-bombard] [--no-haul]
"""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import supply_bot as sb  # noqa: E402
import bitbath as bb  # noqa: E402

# Most transports and galleys it keeps.
MAX_TRANSPORTS = 8
MAX_GALLEYS = 6
# A transport that has some units aboard is ready after waiting this many
# turns for the rest of its crew.
WAIT = 4
# Ready transports sail together once this many are ready, or once one has
# waited this many turns.
CONVOY, CONVOY_WAIT = 3, 6
# Crews come from at most this many turns' walk from the port.
CREW_TURNS = 12
# Besides idle units, at most this share of the land army goes on
# expeditions at once.
CREW_SHARE = 0.25
# A galley goes back to port below this share of its HP, or when its
# rations would last fewer than this many turns beyond the trip home.
HOME_HP = 0.5
SEA_SPARE = 6
# Enemy ships seen within this many rounds count when deciding what to build.
SHIP_MEMORY = 20
# An invasion goes nowhere the enemy strength is more than this many times
# what the convoy carries.
ODDS = 1.5
# A port keeps this much more bread for each ship near it, and ships dock
# where there's at least this much to eat.
PORT_KEEP = 150
# A wagon stocks a port on a sea that goes somewhere when the port holds less
# bread than this, from a city that holds at least STOCK_FROM.
PORT_STOCK, STOCK_FROM = 600, 1500


class NavalBot(sb.SupplyBot):
    def __init__(self, haul=True, bombard=True):
        super().__init__(haul)
        self.bombard = bombard

    def on_init(self):
        super().on_init()
        self.naval = self.supply and self.rules.naval
        if not self.naval:
            return
        m, c = self.map, self.rules.constants
        self.passengers_max = c.get("passengers_max", 0)
        self.transport = next((code for code, t in self.types.items() if t.transport), None)
        self.galley = next((code for code, t in self.types.items()
                            if t.naval and not t.transport and any(self.rules.damage[code].values())), None)
        # Landmasses and seas: each land hex's and water hex's area.
        self.land = self.areas(lambda h: m.passable(h))
        self.sea = self.areas(lambda h: m.navigable(h))
        # Water next to land, where transports put units ashore.
        self.coast = [h for h in self.sea if any(n in self.land for n in bb.neighbors(h))]
        # The seas next to each port.
        self.port_seas = {cid: {self.sea[n] for n in bb.neighbors(h) if n in self.sea} for cid, h, _ in m.cities}
        self.landing_cache = {}     # city id -> {water hex: walk from there to the city}
        self.sea_fields = {}        # frozenset of goal hexes -> distance field over water
        self.voyages = {}           # transport id -> {"phase", "port", "waited"}
        self.crew_of = {}           # land unit id -> the transport it's to board
        self.campaigns = {}         # sea -> the city convoys on it sail for
        self.ships_seen = {}        # enemy ship id -> round last seen
        self.tally = {"landed": 0, "sunk": 0, "bombarded": 0}
        self.stock = {}             # wagon id -> the port it carries bread to
        self.useful = set()         # seas with somewhere worth sailing to

    def areas(self, open_):
        """{hex: area number} over the hexes open_ accepts, by connected area."""
        out, n = {}, 0
        for start in self.map.hexes():
            if start in out or not open_(start):
                continue
            out[start] = n
            stack = [start]
            while stack:
                h = stack.pop()
                for x in bb.neighbors(h):
                    if x not in out and open_(x):
                        out[x] = n
                        stack.append(x)
            n += 1
        return out

    # Hooks into the supply bot's turn.

    def ready(self):
        units = super().ready()
        if not self.naval:
            return units
        return [u for u in units if u.id not in self.crew_of]

    def capture_in_place(self):
        super().capture_in_place()
        if self.naval:
            try:
                self.sea_turn()
            except bb.GameOver:
                raise
            except Exception:
                traceback.print_exc()   # a bug at sea shouldn't cost the land plan
            self.refresh()

    def build_all(self):
        if self.naval:
            self.sync()
            try:
                self.build_ships()
            except bb.GameOver:
                raise
            except Exception:
                traceback.print_exc()
        super().build_all()

    # Stocking ports with bread, for ships to be built and fed.

    def supply_wagons(self):
        wagons = super().supply_wagons()
        return [w for w in wagons if w.id not in self.stock] if self.naval else wagons

    def wagons_wanted(self):
        return super().wagons_wanted() + (len(self.stock) if self.naval else 0)

    def advance(self):
        if self.naval:
            try:
                self.run_stockers()
            except bb.GameOver:
                raise
            except Exception:
                traceback.print_exc()
        super().advance()

    def run_stockers(self):
        """One wagon per port in need carries bread to it from the richest
        city on the same landmass."""
        s = self.state
        self.stock = {w: c for w, c in self.stock.items() if w in s.units and s.cities[c].owner == self.player}
        needy = [c for c in self.ports() if self.port_seas[c.id] & self.useful and c.bread < PORT_STOCK
                 and c.id not in self.stock.values()]
        for port in needy:
            rich = [c for c in s.my_cities()
                    if c.bread >= STOCK_FROM and self.land.get(c.hex) == self.land.get(port.hex)]
            if not rich:
                continue
            wagons = [w for w in s.my_units() if self.types[w.type].carry and not w.fresh and w.id not in self.stock
                      and not w.wheat and self.land.get(w.hex) == self.land.get(port.hex)]
            if wagons:
                w = min(wagons, key=lambda w: (self.city_field[port.id].get(w.hex, 1 << 30), w.id))
                self.stock[w.id] = port.id
                self.jobs.pop(w.id, None)
        for wid, cid in sorted(self.stock.items()):
            if self.out_of_time():
                return
            w, port = s.units[wid], s.cities[cid]
            if w.fresh or w.acted:
                continue
            if w.bread >= min(self.cargo_max, 300):
                if w.hex != port.hex:
                    self.walk(w, self.city_field[port.id], sb.WAGON_CAUTION)
                if w.hex == port.hex:
                    self.unload(w)
                    del self.stock[wid]     # done: a new job next turn
                continue
            rich = [c for c in s.my_cities() if c.bread >= STOCK_FROM and self.land.get(c.hex) == self.land.get(w.hex)]
            if not rich:
                del self.stock[wid]
                continue
            src = min(rich, key=lambda c: (self.city_field[c.id].get(w.hex, 1 << 30), c.id))
            if w.hex != src.hex:
                self.walk(w, self.city_field[src.id], sb.WAGON_CAUTION)
            if w.hex == src.hex:
                self.load(w, min(self.cargo_max - w.bread, src.bread - self.keep(src)))

    def keep(self, city):
        # A port keeps bread for the ships nearby, which come back hungry.
        keep = super().keep(city)
        if self.naval and self.port_seas[city.id]:
            ships = sum(1 for u in self.state.my_units()
                        if self.types[u.type].naval and bb.distance(u.hex, city.hex) <= 3)
            keep += PORT_KEEP * ships
        return keep

    def hungry_units(self):
        # Units aboard eat once they land. Ships eat in port, and from wagons
        # on the shore next to them.
        out = [u for u in super().hungry_units() if not u.carrier and not self.types[u.type].naval]
        if self.naval:
            out += [u for u in self.state.my_units() if self.types[u.type].naval
                    and u.rations <= self.ration_max - sb.HUNGRY
                    and any(n in self.defense for n in bb.neighbors(u.hex))]
        return out

    # The turn at sea.

    def sea_turn(self):
        s = self.state
        for e in s.enemy_units():
            if self.types[e.type].naval:
                self.ships_seen[e.id] = s.round
        self.voyages = {t: v for t, v in self.voyages.items() if t in s.units}
        self.crew_of = {u: t for u, t in self.crew_of.items() if u in s.units and t in s.units}
        self.dests = self.destinations()
        ids = {c.id for c in self.dests}
        self.campaigns = {sea: cid for sea, cid in self.campaigns.items() if cid in ids}
        self.run_galleys()
        self.run_transports()
        self.run_crews()
        self.fleet_report()
        self.debug_text("navy: %d transports, %d galleys, %d destinations, campaigns %s, %d crew; landed %d, "
                        "sank %d ships, bombarded %d HP" % (
                            self.count(self.transport), self.count(self.galley), len(self.dests),
                            sorted(self.campaigns.values()),
                            len(self.crew_of), self.tally["landed"], self.tally["sunk"], self.tally["bombarded"]))

    def fleet_report(self):
        """A line in the replay on each ship: its job, the units aboard, where it
        is, its HP and rations, and then how many more units the bread
        income could feed."""
        s = self.state
        parts = []
        for t in sorted((u for u in s.my_units() if self.types[u.type].naval), key=lambda u: u.id):
            v = self.voyages.get(t.id, {})
            parts.append("%s%d %s %d@%d,%d %dhp %dr" % (t.type, t.id, v.get("phase", "-"), len(s.passengers(t)),
                                                       t.q, t.r, t.hp, t.rations))
        parts.append("room %d" % (self.units_fed() - len(s.my_units())))
        self.debug_text("fleet: " + "; ".join(parts))

    def count(self, code):
        return sum(1 for u in self.state.my_units() if u.type == code)

    def sea_field(self, goals):
        """Distance over water to the nearest of goals, as {hex: cost}."""
        key = frozenset(goals)
        if key not in self.sea_fields:
            self.sea_fields[key] = self.distance_field(goals, blocked=set(), naval=True)
        return self.sea_fields[key]

    def landings(self, cid):
        """{water hex: walk from the land next to it to city cid}, for the
        water from which units would land nearest the city."""
        if cid not in self.landing_cache:
            field = self.city_field[cid]
            best = {}
            for w in self.coast:
                d = min((field[n] for n in bb.neighbors(w) if n in field), default=None)
                if d is not None:
                    best[w] = d
            low = min(best.values(), default=0)
            self.landing_cache[cid] = {w: d for w, d in best.items() if d <= low + 2}
        return self.landing_cache[cid]

    def open_land(self):
        """Landmasses that hold cities we don't."""
        return {self.land.get(c.hex) for c in self.state.cities if c.owner != self.player}

    def destinations(self):
        """Cities we don't hold on landmasses where we hold none, and our
        cities on landmasses where the enemy still holds some, which a
        transport from one of our ports could reach."""
        s, me = self.state, self.player
        held = {self.land.get(c.hex) for c in s.my_cities()}
        contested = {self.land.get(c.hex) for c in s.cities if c.owner not in (0, me)}
        seas = set().union(*[self.port_seas[c.id] for c in s.my_cities()]) if s.my_cities() else set()
        out = []
        for c in s.cities:
            land = self.land.get(c.hex)
            if (c.owner != me and land not in held) or (c.owner == me and land in contested):
                if any(self.sea[w] in seas for w in self.landings(c.id)):
                    out.append(c)
        return out

    def ports(self, sea=None):
        """Our ports, or those on the given sea."""
        return [c for c in self.state.my_cities()
                if self.port_seas[c.id] and (sea is None or sea in self.port_seas[c.id])]

    def sail(self, ship, field, avoid=()):
        """Moves a ship to the hex it can reach nearest the goals of field,
        keeping out of the hexes in avoid where it can. Returns whether it
        moved."""
        options = self.reachable(ship)
        options[ship.hex] = ""
        best = None
        for h, dirs in options.items():
            d = field.get(h)
            if d is None:
                continue
            key = (d + (4 if h in avoid else 0), len(dirs))
            if best is None or key < best[0]:
                best = (key, dirs)
        if best is None or not best[1]:
            return False
        self.go(ship, best[1])
        return True

    def danger(self):
        """Water hexes visible enemy galleys could strike next turn."""
        out = set()
        for e in self.state.enemy_units():
            t = self.types[e.type]
            if t.naval and any(self.rules.damage[e.type].values()):
                out.update(bb.disk(e.hex, t.move + t.range_max))
        return out

    # Galleys.

    def run_galleys(self):
        s = self.state
        galleys = [u for u in s.my_units() if u.type == self.galley and not u.fresh and not u.acted]
        for g in sorted(galleys, key=lambda u: u.id):
            if self.out_of_time() or g.id not in s.units:
                continue
            t = self.types[g.type]
            if g.hp < t.max_hp * HOME_HP or g.rations < self.trip_home(g) + SEA_SPARE:
                if not self.strike(g, moving=False):
                    self.home(g)
                continue
            if not self.strike(g):
                self.patrol(g)

    def trip_home(self, ship):
        """Rations the trip back to port would cost."""
        goals = self.port_water(self.sea.get(ship.hex))
        d = self.sea_field(goals).get(ship.hex) if goals else None
        return 0 if d is None else 2 * (d // self.types[ship.type].move + 1)

    def port_water(self, sea):
        """The water next to our ports on a sea."""
        return [n for c in self.ports(sea) for n in bb.neighbors(c.hex) if self.sea.get(n) == sea]

    def home(self, ship):
        """Sails a ship to the water next to its nearest port with bread,
        where it heals and eats."""
        sea = self.sea.get(ship.hex)
        ports = self.ports(sea)
        fed = [c for c in ports if c.bread >= 2 * PORT_KEEP] or ports
        goals = [n for c in fed for n in self.port_water_of(c, sea)]
        if goals and ship.hex not in goals:
            self.sail(ship, self.sea_field(goals), self.danger())

    def strike(self, g, moving=True):
        """Attacks the best target the galley can reach. Returns whether it did."""
        s = self.state
        reach = self.reachable(g) if moving else {}
        reach[g.hex] = ""
        gt = self.types[g.type]
        damage = self.rules.damage[g.type]
        danger = self.danger()
        best = None
        for e in s.enemy_units():
            if e.carrier or not damage.get(e.type):
                continue
            et = self.types[e.type]
            if not et.naval and not self.bombard:
                continue
            spots = [h for h in reach if gt.in_range(bb.distance(h, e.hex))]
            if not spots:
                continue
            hit = self.damage(g.type, g.hp, e.type, e.hex, e.fort)
            left = max(0, e.hp - hit)
            gain = et.cost * (e.hp - left) / et.max_hp
            if left == 0:
                gain += et.cost * 0.5 + sum(self.worth(p) for p in s.passengers(e))
            loss = 0
            if left and et.in_range(1) and self.rules.damage[e.type].get(g.type):
                back = self.damage(e.type, left, g.type, g.hex)
                loss = gt.cost * min(back, g.hp) / gt.max_hp
            if not et.naval and (e.hex in self.city_at or any(bb.distance(c.hex, e.hex) <= 1 for c in s.my_cities())):
                gain += 40      # it's taking or threatening a city
            for h in spots:
                risk = 30 if h in danger and left != 0 else 0
                key = (gain - loss - risk, -len(reach[h]))
                if gain - loss - risk > 0 and (best is None or key > best[0]):
                    best = (key, e, h)
        if best is None:
            return False
        _, e, h = best
        if h != g.hex:
            self.go(g, reach[h])
            if g.hex != h or g.id not in s.units:
                return True     # cut short: it was ambushed or stopped
        before = e.hp
        reply = self.attack(g, e)
        if reply:
            after = int(reply.values[0])
            if self.types[e.type].naval:
                self.tally["sunk"] += after == 0
            else:
                self.tally["bombarded"] += before - after
        self.sync()
        return bool(reply)

    def patrol(self, g):
        """Escorts a loaded transport, else blockades the nearest enemy port,
        else heads for the enemy ships seen last."""
        s = self.state
        sea = self.sea.get(g.hex)
        laden = [t for t in s.my_units() if t.type == self.transport and s.passengers(t)
                 and self.sea.get(t.hex) == sea and self.voyages.get(t.id, {}).get("phase") == "sail"]
        if laden:
            t = min(laden, key=lambda t: (bb.distance(t.hex, g.hex), t.id))
            goals = [n for n in bb.neighbors(t.hex) if n in self.sea]
            if g.hex not in goals:
                self.sail(g, self.distance_field(goals, naval=True))
            return
        theirs = [c for c in s.cities if c.owner not in (0, self.player) and sea in self.port_seas[c.id]]
        if theirs:
            goals = [n for c in theirs for n in bb.neighbors(c.hex) if self.sea.get(n) == sea]
            if g.hex not in goals:
                self.sail(g, self.sea_field(goals))
            return
        ships = [e.hex for e in s.enemy_units() if self.types[e.type].naval and self.sea.get(e.hex) == sea]
        if ships:
            self.sail(g, self.distance_field([n for h in ships for n in bb.neighbors(h) if n in self.sea], naval=True))

    # Transports.

    def run_transports(self):
        s = self.state
        transports = sorted((u for u in s.my_units() if u.type == self.transport), key=lambda u: u.id)
        ready = []
        for t in transports:
            v = self.voyages.setdefault(t.id, {"phase": "gather", "port": None, "waited": 0})
            aboard = s.passengers(t)
            if v["phase"] == "gather":
                coming = [u for u, tid in self.crew_of.items() if tid == t.id and not s.units[u].carrier]
                if aboard and (len(aboard) >= self.passengers_max or v["waited"] >= WAIT or not coming):
                    v["phase"], v["waited"] = "ready", 0
                    for u in coming:
                        del self.crew_of[u]     # left behind: back to the land army
                else:
                    v["waited"] += 1 if aboard else 0
                    self.dock(t, v, move=not t.fresh)
            if v["phase"] == "ready":
                if aboard:
                    v["waited"] += 1
                    ready.append(t)
                else:
                    v["phase"], v["waited"] = "gather", 0
        # Ready transports sail together, once enough are ready or one has
        # waited long enough, if they have the rations for the trip there
        # and back. Those that don't wait in port to eat.
        if ready and (len(ready) >= min(CONVOY, len(transports))
                      or max(self.voyages[t.id]["waited"] for t in ready) >= CONVOY_WAIT):
            for t in ready:
                if t.rations >= self.voyage_rations(t):
                    self.voyages[t.id]["phase"] = "sail"
        for t in transports:
            if self.out_of_time():
                return
            v = self.voyages[t.id]
            if t.id not in s.units or t.fresh:
                continue
            if v["phase"] == "ready":
                self.dock(t, v)
            elif v["phase"] == "sail":
                cid = self.pick_target(t)
                if cid is not None:
                    self.voyage(t, cid)
                elif self.dock(t, v):
                    self.land_home(t)   # nowhere worth going: back to the land army
                if t.id in s.units and not s.passengers(t):
                    v["phase"] = "return"
            elif v["phase"] == "return" and self.dock(t, v):
                v["phase"], v["waited"] = "gather", 0

    def voyage_rations(self, t):
        """Rations a transport needs to sail to its sea's campaign, or the
        best destination it could reach, and back."""
        sea = self.sea.get(t.hex)
        cid = self.campaigns.get(sea)
        if cid is None:
            port = self.voyages[t.id]["port"]
            home = self.land.get(self.state.cities[port].hex) if port is not None else None
            cid = self.best_destination(t.hex, home, MAX_TRANSPORTS * self.passengers_max * 150)
        if cid is None:
            return SEA_SPARE
        d = self.sea_field([w for w in self.landings(cid) if self.sea[w] == sea]).get(t.hex)
        if d is None:
            return SEA_SPARE
        return 2 * (2 * d // self.types[t.type].move + 2) + SEA_SPARE

    def dock(self, t, v, move=True):
        """Sails a transport to the water next to its port, picking the
        nearest port if it has none. Returns whether it's there."""
        s = self.state
        sea = self.sea.get(t.hex)
        ports = self.ports(sea)
        if not ports:
            return False
        if v["port"] is None or v["port"] not in {c.id for c in ports} or s.cities[v["port"]].bread < PORT_KEEP:
            # The nearest port with plenty of bread, else the nearest.
            v["port"] = min(ports, key=lambda c: (c.bread < 2 * PORT_KEEP,
                                                  self.sea_field(self.port_water_of(c, sea)).get(t.hex, 1 << 30),
                                                  c.id)).id
        goals = self.port_water_of(s.cities[v["port"]], sea)
        if t.hex in goals or not move:
            return t.hex in goals
        self.sail(t, self.sea_field(goals), self.danger())
        return t.hex in goals

    def port_water_of(self, city, sea):
        return [n for n in bb.neighbors(city.hex) if self.sea.get(n) == sea]

    def pick_target(self, t):
        """The destination a sailing transport makes for: its sea's campaign,
        else the best one it can reach, which becomes the campaign."""
        s = self.state
        sea = self.sea.get(t.hex)
        if sea in self.campaigns:
            return self.campaigns[sea]
        port = self.voyages[t.id]["port"]
        home = self.land.get(s.cities[port].hex) if port is not None else None
        strength = sum(self.worth(u) for u in s.my_units() if u.carrier and self.sea.get(u.hex) == sea)
        best = self.best_destination(t.hex, home, strength)
        if best is not None:
            self.campaigns[sea] = best
        return best

    def best_destination(self, at, home, strength):
        """The destination worth the most for the trip from the water hex at,
        among those not on the landmass home, where units can walk, and those
        that units of the given strength could take. None if there's none."""
        s = self.state
        sea = self.sea.get(at)
        best = None
        for c in self.dests:
            if self.land.get(c.hex) == home:
                continue    # units can walk there
            spots = [w for w in self.landings(c.id) if self.sea[w] == sea]
            if not spots:
                continue
            if c.owner != self.player and self.threat.get(c.hex, 0) > ODDS * strength + 100:
                continue
            d = self.sea_field(spots).get(at)
            if d is None:
                continue
            if c.owner == self.player:
                value = sb.vb.ENEMY_VALUE   # reinforcing the fight for the enemy's land
            else:
                value = sb.vb.NEUTRAL_VALUE if c.owner == 0 else sb.vb.ENEMY_VALUE
                if any(bb.distance(x.hex, c.hex) == 1 for x in s.sites):
                    value *= 1.5
            key = (value / (d / self.types[self.transport].move + 2), -c.id)
            if best is None or key > best[0]:
                best = (key, c.id)
        return best and best[1]

    def voyage(self, t, cid):
        """Sails a loaded transport toward city cid's coast, and puts its
        units ashore once it's there."""
        spots = self.landings(cid)
        if t.hex not in spots:
            self.sail(t, self.sea_field([w for w in spots if self.sea[w] == self.sea[t.hex]]), self.danger())
        if t.id in self.state.units and t.hex in spots:
            self.land_all(t, cid)

    def land_all(self, t, cid):
        """Puts the units aboard ashore, as near city cid as they can get,
        capturers first, and takes what they land on."""
        s = self.state
        field = self.city_field[cid]
        aboard = sorted(s.passengers(t), key=lambda u: (not self.types[u.type].capture, -u.hp, u.id))
        for u in aboard:
            if self.out_of_time():
                return
            options = {h: d for h, d in self.reachable(u).items() if h in field}
            if not options:
                continue
            h = min(options, key=lambda h: (field[h], self.threat.get(h, 0), h))
            self.go(u, options[h])
            if not u.carrier:
                self.tally["landed"] += 1
                self.crew_of.pop(u.id, None)
                if self.can_capture(u):
                    self.capture(u)

    def land_home(self, t):
        """Puts the units aboard ashore next to the port, for the land army."""
        for u in self.state.passengers(t):
            options = self.reachable(u)
            if options:
                self.go(u, min(options.items(), key=lambda kv: (kv[0] not in self.city_at, kv[0]))[1])
                if not u.carrier:
                    self.crew_of.pop(u.id, None)

    # Crews.

    def run_crews(self):
        """Assigns land units to transports gathering a crew, and walks them
        over to board."""
        s = self.state
        gathering = [t for t in s.my_units() if t.type == self.transport
                     and self.voyages.get(t.id, {}).get("phase") == "gather" and self.voyages[t.id]["port"] is not None]
        if gathering and self.dests:
            open_land = self.open_land()
            army = [u for u in s.my_units() if not self.types[u.type].naval and not self.types[u.type].carry]
            # Units with nothing left to take on their landmass may all go;
            # of the rest, only a share.
            budget = int(len(army) * CREW_SHARE) - sum(
                1 for u in self.crew_of if self.land.get(s.units[u].hex) in open_land)
            busy = self.busy | set(self.crew_of)
            for t in gathering:
                port = s.cities[self.voyages[t.id]["port"]]
                sea = self.sea.get(t.hex)
                fleet = sum(1 for x in s.my_units() if x.type == self.transport and self.sea.get(x.hex) == sea)
                strength = fleet * self.passengers_max * self.types[self.transport].cost
                if self.best_destination(t.hex, self.land.get(port.hex), strength) is None:
                    continue    # nowhere on this sea is worth a crew
                field = self.city_field[port.id]
                want = self.passengers_max - len(s.passengers(t)) - sum(
                    1 for u, tid in self.crew_of.items() if tid == t.id and not s.units[u].carrier)
                free = [u for u in s.my_units()
                        if u.id not in busy and not u.carrier and not u.fresh and not u.acted and u.mp > 0
                        and not self.types[u.type].naval and not self.types[u.type].carry
                        and field.get(u.hex) is not None and field[u.hex] / self.types[u.type].move <= CREW_TURNS]
                free.sort(key=lambda u: (self.land.get(u.hex) in open_land, not self.types[u.type].capture,
                                         field[u.hex], u.id))
                for u in free:
                    if want <= 0:
                        break
                    if self.land.get(u.hex) in open_land:
                        if budget <= 0:
                            break
                        budget -= 1
                    self.crew_of[u.id] = t.id
                    busy.add(u.id)
                    want -= 1
        for uid, tid in sorted(self.crew_of.items()):
            if self.out_of_time():
                return
            u, t = s.units.get(uid), s.units.get(tid)
            if u is None or t is None or u.carrier or u.acted or u.fresh or u.mp <= 0:
                continue
            reach = self.reachable(u)
            if t.hex in reach:
                self.go(u, reach[t.hex])
                continue
            goals = [n for n in bb.neighbors(t.hex) if n in self.defense]
            if goals:
                self.walk(u, self.distance_field(goals))

    # Building.

    def build_ships(self):
        """Builds transports for the idle part of the army, and galleys to go
        with them and to answer enemy ships, at ports whose new ships would
        be on a sea that goes somewhere."""
        s = self.state
        if not self.transport:
            return
        self.dests = self.destinations()
        open_land = self.open_land()
        idle = sum(1 for u in s.my_units() if not self.types[u.type].naval and not self.types[u.type].carry
                   and self.land.get(u.hex) not in open_land)
        strength = MAX_TRANSPORTS * self.passengers_max * self.types[self.transport].cost
        # Seas with somewhere worth sailing to, from the ports on them.
        useful = set()
        for c in self.ports():
            for sea in self.port_seas[c.id]:
                if self.best_destination(self.port_water_of(c, sea)[0], self.land.get(c.hex), strength) is not None:
                    useful.add(sea)
        self.useful = useful
        # Seas where galleys have work: those, and those with enemy ports or ships.
        warlike = set(useful)
        for c in s.cities:
            if c.owner not in (0, self.player):
                warlike |= self.port_seas[c.id]
        recent = [e for e in s.enemy_units() if self.types[e.type].naval]
        warlike |= {self.sea.get(e.hex) for e in recent}
        seen = sum(1 for r in self.ships_seen.values() if s.round - r <= SHIP_MEMORY)
        want_t = min(MAX_TRANSPORTS, max(1, -(-idle // max(1, self.passengers_max)))) if useful else 0
        want_g = min(MAX_GALLEYS, (want_t + 1) // 2 + (seen + 1) // 2) if self.galley and warlike else 0
        have_t = sum(1 for u in s.my_units() if u.type == self.transport and self.sea.get(u.hex) in useful)
        have_g = self.count(self.galley)
        if have_t >= want_t and have_g >= want_g:
            return
        room = self.units_fed() - len(s.my_units())
        near = {c.id: sum(1 for u in s.my_units() if not self.types[u.type].naval and bb.distance(u.hex, c.hex) <= 8)
                for c in self.ports()}
        for c in sorted(self.ports(), key=lambda c: (-near[c.id], c.id)):
            if room <= 0:
                break
            launch = self.launch_hex(c)
            if c.built or c.hex in self.enemy_hex or launch is None:
                continue
            sea = self.sea.get(launch)
            budget = c.bread - sb.KEEP
            code = None
            if have_t < want_t and sea in useful and budget >= self.types[self.transport].cost:
                code = self.transport
            elif have_g < want_g and sea in warlike and budget >= self.types[self.galley].cost:
                code = self.galley
            if code and self.build(c, code):
                room -= 1
                if code == self.transport:
                    have_t += 1
                else:
                    have_g += 1

if __name__ == "__main__":
    args = sys.argv[1:]
    bb.run(NavalBot(haul="--no-haul" not in args, bombard="--no-bombard" not in args))
