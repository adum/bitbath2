"""Naval: the reference bot for the strategic ruleset's ships (STRATEGIC.md §6).

It plays as the supply bot does on land, and adds a navy:

- Expeditions. Its destinations are the cities on landmasses where it holds
  no city, and its own cities on landmasses where the enemy still holds
  some, which need reinforcing. So are the resource sites on landmasses
  where it holds no city: those where it has no units yet, and those where
  enemy units have landed. Transports wait off its ports while land units walk
  over and board: first units with nothing left to take on their own
  landmass, then a share of the rest. Loaded transports wait for each other
  and sail as a convoy, all for one destination at a time, so that they
  land together. Units land on the city or site if they can, or as near it
  as they can, and take the city. The empty transports go back for more.
- Ferries. Sites have no owners, so on each landmass with sites where its
  units have landed and no one holds a city, the units stay by the sites as
  guards, and a transport ferries wagons between the sites and a port. The wagons take bread out for the guards,
  load wheat at the sites, and unload it in the port, where it becomes
  bread.
- Garrisons. If its home is on an island of 4 cities or fewer, expeditions
  leave 2 land units there for each city, so that an enemy landing doesn't
  find it empty, or 1 while it hasn't seen an enemy transport lately. When
  the garrison leaves no one to crew a transport, it builds the crew from
  its stockpiled bread.
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

import math
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
# Each this much bread in the ports on seas that go somewhere feeds one more
# transport, and one more ship beyond what the bread income can feed.
SHIP_BREAD = 600
# A wagon stocks a port on a sea that goes somewhere when the port holds less
# bread than this, from a city that can spare at least STOCK_FROM beyond
# what it keeps.
PORT_STOCK, STOCK_FROM = 1000, 800
# A wagon unloading wheat goes to such a port instead of the nearest city if
# it's no more than this much farther, in move points.
PORT_DETOUR = 6
# A transport that gives up waiting rests this many rounds before it takes
# on a new crew.
REST = 10
# A site that grows this much wheat a turn is worth as much as a neutral
# city to an expedition.
SITE_PAR = 100
# A ferry waits at the island this many turns for its wagons to fill up,
# and then leaves with those that have some wheat. Each wagon goes back
# aboard once it has less room left than MIN_ROOM.
FERRY_WAIT, MIN_ROOM = 6, 150
# Bread the first wagon of each ferry trip takes for each guard on the
# island, which the guards eat while it stands by them.
GUARD_BREAD = 60
# Expeditions leave GARRISON land units for each city on the home island,
# if it has HOME_ISLAND cities or fewer, so that an enemy landing doesn't
# find it empty. A larger home landmass has the land army to defend it.
# While no enemy transport has been seen for SHIP_MEMORY rounds, they leave
# CALM_GARRISON. A full garrison could take every land unit the bread
# income feeds, and then none would ever sail, even for the sites in the
# middle that would feed more.
GARRISON, CALM_GARRISON, HOME_ISLAND = 2, 1, 4
# When the garrison leaves no one to crew a transport, it builds the crew
# beyond what the bread income feeds, one unit for each this much bread it
# has stockpiled, in cities and wagons. Otherwise an army the income caps,
# all of it garrison, never sails again, even for the sites that would feed
# more.
CREW_BREAD = 400
# A troop transport that finds the water next to its port taken lies off the
# coast instead, at most this far from the port.
BERTH_RANGE = 4


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
        # The home island: the landmass the home city starts on, if it's small.
        homes = {self.land.get(h) for _, h, owner in m.cities if owner == self.player}
        self.home_island = {land for land in homes
                            if sum(1 for _, h, _ in m.cities if self.land.get(h) == land) <= HOME_ISLAND}
        # Water next to land, where transports put units ashore.
        self.coast = [h for h in self.sea if any(n in self.land for n in bb.neighbors(h))]
        # The seas next to each port.
        self.port_seas = {cid: {self.sea[n] for n in bb.neighbors(h) if n in self.sea} for cid, h, _ in m.cities}
        self.landing_cache = {}     # destination -> {water hex: walk from there to it}
        self.sea_fields = {}        # frozenset of goal hexes -> distance field over water
        self.voyages = {}           # transport id -> {"phase", "port", "waited"}
        self.crew_of = {}           # land unit id -> the transport it's to board
        self.campaigns = {}         # sea -> the destination convoys on it sail for
        self.ferries = {}           # transport id -> {"isle", "port", "phase", "waited"}
        self.haulers = {}           # wagon id -> the landmass whose sites it hauls from
        self.hauls_wanted = 0       # wagons the ferries want, for building
        self.ships_seen = {}        # enemy ship id -> round last seen
        self.transports_seen = {}   # enemy transport id -> round last seen
        self.tally = {"landed": 0, "sunk": 0, "bombarded": 0, "ferried": 0}
        self.stock = {}             # wagon id -> the port it carries bread to
        self.useful = set()         # seas with somewhere worth sailing to
        self.crew_short = 0         # crew a transport wants that the garrison leaves no one for

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

    def blockaded(self, city):
        """Whether enemy ships lie next to a port. A ship launched there would
        be sunk before it sailed, and wagons bringing it bread would be shot
        in the port."""
        return any(n in self.enemy_hex and n in self.sea for n in bb.neighbors(city.hex))

    def nearest_city(self, w, need=0):
        # Wheat goes to a port on a sea that goes somewhere, if the port holds
        # less than PORT_STOCK and isn't much farther than the nearest city.
        # On a small island no city gets STOCK_FROM ahead, so without this
        # the port never has the bread for a first ship.
        city = super().nearest_city(w, need)
        if not self.naval or need > 0 or city is None:
            return city
        far = self.city_field[city.id].get(w.hex, 1 << 30) + PORT_DETOUR
        ports = [c for c in self.ports() if self.port_seas[c.id] & self.useful and c.bread < PORT_STOCK
                 and c.hex not in self.enemy_hex and not any(n in self.enemy_hex for n in bb.neighbors(c.hex))
                 and self.city_field[c.id].get(w.hex, 1 << 30) <= far]
        return min(ports, key=lambda c: (self.city_field[c.id][w.hex], c.id)) if ports else city

    def supply_wagons(self):
        wagons = super().supply_wagons()
        if not self.naval:
            return wagons
        return [w for w in wagons if w.id not in self.stock and w.id not in self.haulers]

    def wagons_wanted(self):
        return super().wagons_wanted() + (len(self.stock) + self.hauls_wanted if self.naval else 0)

    def haul_sites(self):
        # Sites on landmasses where we hold no city are the ferries' to haul.
        sites = super().haul_sites()
        if not self.naval:
            return sites
        held = {self.land.get(c.hex) for c in self.state.my_cities()}
        return [x for x in sites if self.land.get(x.hex) in held]

    def units_fed(self):
        fed = super().units_fed()
        if self.naval and self.crew_short:
            s = self.state
            stock = sum(c.bread for c in s.my_cities()) + sum(u.bread for u in s.my_units())
            fed = max(fed, len(s.my_units()) + min(self.crew_short, stock // CREW_BREAD))
        return fed

    def income(self):
        # The sites of a landmass a ferry serves with wagons feed the army
        # too. Counting them before then would build an army the wheat never
        # comes in to feed.
        made = super().income()
        if self.naval and self.haul:
            served = {f["isle"] for f in self.ferries.values()} & set(self.haulers.values())
            made += sum(x.output for isle, sites in self.haul_isles().items() if isle in served for x in sites)
        return made

    def advance(self):
        if self.naval:
            try:
                self.run_stockers()
            except bb.GameOver:
                raise
            except Exception:
                traceback.print_exc()
        super().advance()
        if self.naval:
            try:
                self.guard_sites()
            except bb.GameOver:
                raise
            except Exception:
                traceback.print_exc()

    def run_stockers(self):
        """One wagon per port in need carries bread to it from the richest
        city on the same landmass."""
        s = self.state
        self.stock = {w: c for w, c in self.stock.items()
                      if w in s.units and s.cities[c].owner == self.player and not self.blockaded(s.cities[c])}
        needy = [c for c in self.ports() if self.port_seas[c.id] & self.useful and c.bread < PORT_STOCK
                 and c.id not in self.stock.values() and not self.blockaded(c)]
        for port in needy:
            rich = [c for c in s.my_cities()
                    if c.bread - self.keep(c) >= STOCK_FROM and self.land.get(c.hex) == self.land.get(port.hex)]
            if not rich:
                continue
            wagons = [w for w in s.my_units() if self.types[w.type].carry and not w.fresh and w.id not in self.stock
                      and w.id not in self.haulers and not w.wheat
                      and self.land.get(w.hex) == self.land.get(port.hex)]
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
                self.enter(w, port)
                if w.hex == port.hex:
                    self.unload(w)
                    del self.stock[wid]     # done: a new job next turn
                continue
            rich = [c for c in s.my_cities()
                    if c.bread - self.keep(c) >= STOCK_FROM and self.land.get(c.hex) == self.land.get(w.hex)]
            if not rich:
                del self.stock[wid]
                continue
            src = min(rich, key=lambda c: (self.city_field[c.id].get(w.hex, 1 << 30), c.id))
            self.enter(w, src)
            if w.hex == src.hex:
                amount = min(self.cargo_max - w.bread, src.bread - self.keep(src))
                if amount > 0:
                    self.load(w, amount)

    def can_leave(self, u):
        # A unit on its way to board a transport keeps going. Without
        # make_room, an army with nothing left to do on its island can sit
        # on the city that has the bread, and the port never gets any.
        return super().can_leave(u) and u.id not in self.crew_of

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
        # on the shore next to them. Guards eat from the ferries' wagons.
        out = [u for u in super().hungry_units() if not u.carrier and not self.types[u.type].naval]
        if self.naval:
            isles = self.haul_isles()
            out = [u for u in out if self.land.get(u.hex) not in isles]
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
            if self.types[e.type].transport:
                self.transports_seen[e.id] = s.round
        self.voyages = {t: v for t, v in self.voyages.items() if t in s.units and t not in self.ferries}
        self.crew_of = {u: t for u, t in self.crew_of.items() if u in s.units and t in s.units}
        self.dests = self.destinations()
        # A sea's campaign holds while its convoy sails, so that the convoy
        # lands together. The next convoy picks its own: one that failed to
        # take an enemy port shouldn't send every later one there too.
        sailing = {self.sea.get(t.hex) for t in s.my_units()
                   if self.voyages.get(t.id, {}).get("phase") == "sail"}
        self.campaigns = {sea: key for sea, key in self.campaigns.items() if key in self.dests and sea in sailing}
        self.run_galleys()
        self.run_transports()
        self.run_ferries()
        self.run_crews()
        self.fleet_report()
        self.debug_text("navy: %d transports, %d galleys, %d destinations, campaigns %s, %d crew; landed %d, "
                        "sank %d ships, bombarded %d HP; %d ferries, %d haulers, %d wheat ferried" % (
                            self.count(self.transport), self.count(self.galley), len(self.dests),
                            sorted(self.campaigns.values()),
                            len(self.crew_of), self.tally["landed"], self.tally["sunk"], self.tally["bombarded"],
                            len(self.ferries), len(self.haulers), self.tally["ferried"]))

    def fleet_report(self):
        """A line in the replay on each ship: its job, the units aboard, where it
        is, its HP and rations, and then how many more units the bread
        income could feed."""
        s = self.state
        parts = []
        for t in sorted((u for u in s.my_units() if self.types[u.type].naval), key=lambda u: u.id):
            v = self.voyages.get(t.id) or self.ferries.get(t.id) or {}
            phase = ("ferry " if t.id in self.ferries else "") + v.get("phase", "-")
            parts.append("%s%d %s %d@%d,%d %dhp %dr" % (t.type, t.id, phase, len(s.passengers(t)),
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

    # Destinations are keys: ("city", id) or ("site", id).

    def goal(self, key):
        """The city or site a destination key names."""
        kind, i = key
        return self.state.cities[i] if kind == "city" else self.state.sites[i]

    def goal_field(self, key):
        kind, i = key
        return self.city_field[i] if kind == "city" else self.site_field[i]

    def landings(self, key):
        """{water hex: walk from the land next to it to the destination}, for
        the water from which units would land nearest it."""
        if key not in self.landing_cache:
            field = self.goal_field(key)
            best = {}
            for w in self.coast:
                d = min((field[n] for n in bb.neighbors(w) if n in field), default=None)
                if d is not None:
                    best[w] = d
            low = min(best.values(), default=0)
            self.landing_cache[key] = {w: d for w, d in best.items() if d <= low + 2}
        return self.landing_cache[key]

    def open_land(self):
        """Landmasses that hold cities we don't."""
        return {self.land.get(c.hex) for c in self.state.cities if c.owner != self.player}

    def camps(self):
        """Landmasses where our land units stand ashore, not counting wagons."""
        return {self.land.get(u.hex) for u in self.state.my_units()
                if not u.carrier and not self.types[u.type].naval and not self.types[u.type].carry}

    def destinations(self):
        """Cities we don't hold on landmasses where we hold none, and our
        cities on landmasses where the enemy still holds some. Sites on
        landmasses where no one holds a city, which a ferry could haul from,
        where none of our land units stand yet or enemy land units do. Those
        a transport from one of our ports could reach."""
        s, me = self.state, self.player
        held = {self.land.get(c.hex) for c in s.my_cities()}
        contested = {self.land.get(c.hex) for c in s.cities if c.owner not in (0, me)}
        invaded = {self.land.get(e.hex) for e in s.enemy_units() if not e.carrier and not self.types[e.type].naval}
        camps = self.camps()
        seas = set().union(*[self.port_seas[c.id] for c in s.my_cities()]) if s.my_cities() else set()
        out = []
        for c in s.cities:
            land = self.land.get(c.hex)
            if (c.owner != me and land not in held) or (c.owner == me and land in contested):
                out.append(("city", c.id))
        for x in s.sites:
            land = self.land.get(x.hex)
            if land not in held and land not in contested and (land not in camps or land in invaded):
                out.append(("site", x.id))
        return [key for key in out if any(self.sea[w] in seas for w in self.landings(key))]

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

    def shore_danger(self):
        """Water hexes visible enemy land units that can hurt ships, such as
        archers, could shoot at next turn. A galley lying off an enemy port
        there is shot every turn and can't shoot back."""
        out = set()
        ships = [code for code, t in self.types.items() if t.naval]
        for e in self.state.enemy_units():
            t = self.types[e.type]
            if not t.naval and not e.carrier and any(self.rules.damage[e.type].get(c) for c in ships):
                out.update(h for h in bb.disk(e.hex, t.move + t.range_max) if h in self.sea)
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
                 and self.sea.get(t.hex) == sea and (self.voyages.get(t.id, {}).get("phase") == "sail"
                                                     or self.ferries.get(t.id, {}).get("phase") in ("out", "back"))]
        if laden:
            t = min(laden, key=lambda t: (bb.distance(t.hex, g.hex), t.id))
            goals = [n for n in bb.neighbors(t.hex) if n in self.sea]
            if g.hex not in goals:
                self.sail(g, self.distance_field(goals, naval=True))
            return
        theirs = [c for c in s.cities if c.owner not in (0, self.player) and sea in self.port_seas[c.id]]
        if theirs:
            # Off the port, or, where archers would shoot it there, as near
            # as it can lie out of their reach, to catch what sails out.
            shore = self.shore_danger()
            off = [n for c in theirs for n in bb.neighbors(c.hex) if self.sea.get(n) == sea]
            goals = [n for n in off if n not in shore]
            if not goals:
                near = self.sea_field(off)
                safe = [h for h in near if h not in shore and self.sea.get(h) == sea]
                low = min((near[h] for h in safe), default=None)
                goals = [h for h in safe if near[h] == low]
            if goals:
                if g.hex not in goals:
                    self.sail(g, self.sea_field(goals), shore)
                return
        ships = [e.hex for e in s.enemy_units() if self.types[e.type].naval and self.sea.get(e.hex) == sea]
        if ships:
            self.sail(g, self.distance_field([n for h in ships for n in bb.neighbors(h) if n in self.sea], naval=True))

    # Transports.

    def run_transports(self):
        s = self.state
        transports = sorted((u for u in s.my_units() if u.type == self.transport and u.id not in self.ferries),
                            key=lambda u: u.id)
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
                port = s.cities[v["port"]] if v["port"] is not None else None
                hungry = t.rations < self.voyage_rations(t) and (port is None or port.bread < PORT_KEEP)
                if aboard and (v["waited"] > CONVOY_WAIT + 4 or hungry) and not t.fresh:
                    # Waiting has cost too much food: everyone back ashore,
                    # where the cities feed them, and try again later.
                    self.land_home(t)
                    v["phase"], v["waited"], v["rest"] = "gather", 0, s.round + REST
                elif aboard:
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
                key = self.pick_target(t)
                if t.rations < self.trip_home(t) + SEA_SPARE:
                    # Running out of food: put everyone ashore wherever they
                    # can get, and go home.
                    self.land_anywhere(t, key)
                    if s.passengers(t) and self.dock(t, v):
                        self.land_home(t)
                elif key is not None:
                    self.voyage(t, key)
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
        key = self.campaigns.get(sea)
        if key is None:
            port = self.voyages[t.id]["port"]
            home = self.land.get(self.state.cities[port].hex) if port is not None else None
            key = self.best_destination(t.hex, home, MAX_TRANSPORTS * self.passengers_max * 150)
        if key is None:
            return SEA_SPARE
        d = self.sea_field([w for w in self.landings(key) if self.sea[w] == sea]).get(t.hex)
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
            if t.rations < self.ration_max // 3:
                # Hungry: the port with the most bread.
                v["port"] = max(ports, key=lambda c: (c.bread, -c.id)).id
            else:
                # The nearest port with plenty of bread, else the nearest.
                v["port"] = min(ports, key=lambda c: (c.bread < 2 * PORT_KEEP,
                                                      self.sea_field(self.port_water_of(c, sea)).get(t.hex, 1 << 30),
                                                      c.id)).id
        port = s.cities[v["port"]]
        if not move:
            return t.hex in self.port_water_of(port, sea)
        berths = self.berths(t, port, sea)
        if t.hex in berths:
            return True
        self.sail(t, self.sea_field(berths), self.danger())
        return t.hex in berths

    def berths(self, t, port, sea):
        """Where transport t docks at port: the water next to it, on a hex of
        its own if one is free. A unit boarding on a hex takes our
        lowest-numbered transport there with room, so wagons could board a
        troop transport and sail off to an invasion, or a crew the ferry.
        A troop transport that finds the port's water taken lies off the
        coast nearby, where its crew can still walk up and board, unless it's
        hungry, since ships eat only next to a port. Otherwise, at a port
        with a single hex of water, the transports there crowd onto it, and
        crews that won't board the ferry fill the port, where the ferry's
        wagons would unload."""
        s = self.state
        goals = self.port_water_of(port, sea)
        taken = {u.hex for u in s.my_units() if self.types[u.type].transport and u.id != t.id}
        free = [h for h in goals if h not in taken]
        if free or t.id in self.ferries or t.rations < self.ration_max // 3:
            return free or goals
        home = self.land.get(port.hex)
        coast = [h for h in self.coast if self.sea.get(h) == sea and h not in taken
                 and bb.distance(h, port.hex) <= BERTH_RANGE
                 and any(self.land.get(n) == home for n in bb.neighbors(h))]
        if not coast:
            return goals
        near = min(bb.distance(h, port.hex) for h in coast)
        return [h for h in coast if bb.distance(h, port.hex) == near]

    def port_water_of(self, city, sea):
        return [n for n in bb.neighbors(city.hex) if self.sea.get(n) == sea]

    def board(self, u, t):
        """Moves land unit u aboard transport t if it can get there this turn
        and t is the transport it would board there: our lowest-numbered one
        on the hex with room. Returns whether t's hex was in reach, in which
        case u waits by it if it couldn't board."""
        reach = self.reachable(u)
        if t.hex not in reach:
            return False
        first = self.transport_with_room(t.hex)
        if first is not None and first.id == t.id:
            self.go(u, reach[t.hex])
        return True

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
        for key in self.dests:
            x = self.goal(key)
            if self.land.get(x.hex) == home:
                continue    # units can walk there
            spots = [w for w in self.landings(key) if self.sea[w] == sea]
            if not spots:
                continue
            ours = key[0] == "city" and x.owner == self.player
            if not ours and self.threat.get(x.hex, 0) > ODDS * strength + 100:
                continue
            d = self.sea_field(spots).get(at)
            if d is None:
                continue
            if key[0] == "site":
                value = sb.vb.NEUTRAL_VALUE * x.output / SITE_PAR
            elif ours:
                value = sb.vb.ENEMY_VALUE   # reinforcing the fight for the enemy's land
            else:
                value = sb.vb.NEUTRAL_VALUE if x.owner == 0 else sb.vb.ENEMY_VALUE
                if any(bb.distance(y.hex, x.hex) == 1 for y in s.sites):
                    value *= 1.5
            rank = (value / (d / self.types[self.transport].move + 2), key)
            if best is None or rank > best[0]:
                best = (rank, key)
        return best and best[1]

    def voyage(self, t, key):
        """Sails a loaded transport toward a destination's coast, and puts its
        units ashore once it's there."""
        spots = self.landings(key)
        if t.hex not in spots:
            self.sail(t, self.sea_field([w for w in spots if self.sea[w] == self.sea[t.hex]]), self.danger())
        if t.id in self.state.units and t.hex in spots:
            self.land_all(t, key)

    def land_all(self, t, key):
        """Puts the units aboard ashore, as near the destination as they can
        get, capturers first, and takes what they land on."""
        s = self.state
        field = self.goal_field(key)
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

    def land_anywhere(self, t, key):
        """Puts the units aboard ashore on any land next to the transport,
        nearest the destination if it can."""
        field = self.goal_field(key) if key is not None else {}
        for u in self.state.passengers(t):
            options = self.reachable(u)
            if options:
                h = min(options, key=lambda h: (field.get(h, 1 << 30), self.threat.get(h, 0), h))
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

    # Ferries.

    def haul_isles(self):
        """{landmass: its sites}, for the landmasses with sites where our land
        units stand and no one holds a city, so only wagons carried by
        transport can bring their wheat home. Sites with an enemy on them
        don't count."""
        s = self.state
        cities = {self.land.get(c.hex) for c in s.cities if c.owner != 0}
        camps = self.camps()
        out = {}
        for x in s.sites:
            land = self.land.get(x.hex)
            if land in camps and land not in cities and x.hex not in self.enemy_hex:
                out.setdefault(land, []).append(x)
        return out

    def isle_landings(self, isle):
        """The water from which wagons land nearest the sites on a landmass."""
        return self.landings(("site", min(x.id for x in self.state.sites if self.land.get(x.hex) == isle)))

    def haulers_wanted(self, isle, port):
        """Wagons enough to bring home the wheat of the sites on a landmass,
        given the trip there from port and back."""
        sites = self.haul_isles().get(isle, [])
        field = self.sea_field(list(self.isle_landings(isle)))
        d = min((field.get(n, 1 << 30) for n in bb.neighbors(port.hex)), default=1 << 30)
        if not sites or d >= 1 << 30:
            return 0
        trip = 2 * (d / self.types[self.transport].move + 1) + 3
        need = sum(x.output for x in sites) * trip / (0.8 * self.cargo_max)
        return max(1, min(self.passengers_max, math.ceil(need)))

    def free_transport(self, isle):
        """A transport in port with no one aboard or coming, on a sea that
        reaches the landmass, to become its ferry. None if there's none."""
        s = self.state
        spots = list(self.isle_landings(isle))
        seas = {self.sea[w] for w in spots}
        coming = set(self.crew_of.values())
        options = []
        for t in s.my_units():
            v = self.voyages.get(t.id)
            if (t.type != self.transport or v is None or v["phase"] != "gather" or v["port"] is None
                    or s.passengers(t) or t.id in coming or self.sea.get(t.hex) not in seas):
                continue
            options.append((self.sea_field(spots).get(t.hex, 1 << 30), t.id))
        return s.units[min(options)[1]] if options else None

    def run_ferries(self):
        """Gives each landmass whose sites only a ferry can haul from a ferry and
        the wagons it needs, and runs the ferries."""
        s = self.state
        isles = self.haul_isles() if self.haul else {}
        self.ferries = {t: f for t, f in self.ferries.items() if t in s.units}
        self.haulers = {w: i for w, i in self.haulers.items() if w in s.units}
        served = {f["isle"] for f in self.ferries.values()}
        # The haulers of a landmass we've lost go back to the land army, but
        # any still over there wait for a ferry to fetch them.
        for wid, isle in list(self.haulers.items()):
            w = s.units[wid]
            if isle not in isles and isle not in served and not w.carrier and self.land.get(w.hex) != isle:
                del self.haulers[wid]
        for isle in sorted(isles):
            if isle not in served:
                t = self.free_transport(isle)
                if t is not None:
                    port = self.voyages.pop(t.id)["port"]
                    self.ferries[t.id] = {"isle": isle, "port": port, "phase": "dock", "waited": 0}
        self.claims = {}            # site id -> wheat the haulers mean to take this turn
        self.hauls_wanted = 0
        for tid in sorted(self.ferries):
            if self.out_of_time():
                return
            t, f = s.units[tid], self.ferries[tid]
            port = s.cities[f["port"]]
            if f["isle"] in isles and port.owner == self.player:
                want = self.haulers_wanted(f["isle"], port)
                self.hauls_wanted += want
                self.hire_haulers(f, want)
            if not t.fresh:
                self.run_ferry(t, f, isles)

    def hire_haulers(self, f, want):
        """Gives a ferry's landmass wagons from its port's landmass, up to
        want in all, nearest the port first."""
        s = self.state
        want -= sum(1 for i in self.haulers.values() if i == f["isle"])
        if want <= 0:
            return
        port = s.cities[f["port"]]
        home = self.land.get(port.hex)
        free = [w for w in s.my_units() if self.types[w.type].carry and not w.fresh and not w.carrier
                and w.id not in self.stock and w.id not in self.haulers and not w.wheat
                and self.land.get(w.hex) == home]
        free.sort(key=lambda w: (self.city_field[port.id].get(w.hex, 1 << 30), w.id))
        # One wagon at least stays home to haul there. Otherwise a ferry sunk
        # with every wagon aboard leaves the home island's cities making just
        # what its army eats, and never bread for a new wagon.
        home_wagons = sum(1 for x in s.my_units() if self.types[x.type].carry and not x.carrier
                          and x.id not in self.haulers and self.land.get(x.hex) == home)
        for w in free[:max(0, min(want, home_wagons - 1))]:
            self.haulers[w.id] = f["isle"]
            self.jobs.pop(w.id, None)

    def run_ferry(self, t, f, isles):
        """A ferry's turn. Docked in port, it waits while its wagons unload
        wheat, take bread for the guards, and board. Then it sails to the
        landmass and puts them ashore, waits there while they load wheat and
        come back aboard, and sails home, where they land in the port."""
        s = self.state
        isle = f["isle"]
        lost = isle not in isles

        def mine():
            return [s.units[w] for w, i in sorted(self.haulers.items()) if i == isle and w in s.units]

        def stranded():
            """Its wagons left on the landmass, when no enemy stands there to
            stop the ferry fetching them. A ferry held off by the enemy, or
            tired of waiting, sails home without them, and with no wagon
            aboard it would never sail for them again."""
            if any(self.land.get(e.hex) == isle and not e.carrier and not self.types[e.type].naval
                   for e in s.enemy_units()):
                return []
            return [w for w in mine() if not w.carrier and self.land.get(w.hex) == isle]

        if f["phase"] == "dock":
            there = self.dock(t, f)
            port = s.cities[f["port"]]
            home = self.land.get(port.hex)
            if lost:
                # Nothing to haul: everyone ashore, and the ferry goes back
                # to carrying expeditions, once it has fetched any wagons
                # still over there.
                if there:
                    self.land_home(t)
                if not s.passengers(t):
                    if stranded():
                        f["phase"], f["waited"] = "out", 0
                    else:
                        del self.ferries[t.id]
                        self.voyages[t.id] = {"phase": "gather", "port": f["port"], "waited": 0}
                        return
                else:
                    return
            else:
                for w in mine():
                    if not w.carrier and self.land.get(w.hex) == home:
                        self.hauler_home(w, t, port, there)
                aboard = [w for w in mine() if w.carrier == t.id]
                behind = [w for w in mine() if not w.carrier and self.land.get(w.hex) == home]
                if there and aboard and (not behind or f["waited"] >= WAIT):
                    f["phase"], f["waited"] = "out", 0
                elif there and not aboard and not behind and stranded():
                    f["phase"], f["waited"] = "out", 0
                elif aboard:
                    f["waited"] += 1
        if f["phase"] == "out":
            if lost and not stranded():
                f["phase"] = "back"
            else:
                spots = self.isle_landings(isle)
                goals = [w for w in spots if self.sea[w] == self.sea.get(t.hex)]
                if t.hex not in spots and goals:
                    self.sail(t, self.sea_field(goals), self.danger())
                if t.id in s.units and t.hex in spots:
                    for w in sorted(s.passengers(t), key=lambda u: u.id):
                        self.hauler_land(w, isle)
                    # Wagons that boarded this turn have no moves left to
                    # land with. On a short crossing the ferry gets there the
                    # same turn, and it waits offshore for them to land next
                    # turn: seeing no one ashore, it would sail them home.
                    left = s.passengers(t)
                    if left and all(w.mp <= 0 for w in left) and f["waited"] < 2:
                        f["waited"] += 1
                    else:
                        f["phase"], f["waited"] = "wait", 0
        if f["phase"] == "wait" and t.id in s.units:
            f["waited"] += 1
            for w in mine():
                if not w.carrier and self.land.get(w.hex) == isle:
                    self.hauler_island(w, t, f, lost)
            ashore = [w for w in mine() if not w.carrier and self.land.get(w.hex) == isle]
            if not ashore or f["waited"] > 2 * FERRY_WAIT or (lost and f["waited"] > 2):
                f["phase"] = "back"
        if f["phase"] == "back" and t.id in s.units:
            there = self.dock(t, f)
            # Kept off its port by the enemy's reach, a ferry with wagons
            # aboard goes in anyway after a couple of turns, so that they get
            # back to work even if it's lost, and the wheat at home doesn't
            # pile up untouched while they wait.
            if not there and s.passengers(t):
                f["held"] = f.get("held", 0) + 1
                if f["held"] >= 2:
                    goals = self.port_water_of(s.cities[f["port"]], self.sea.get(t.hex))
                    self.sail(t, self.sea_field(goals))
                    there = t.id in s.units and t.hex in goals
            if there:
                f["held"] = 0
                port = s.cities[f["port"]]
                for w in sorted(s.passengers(t), key=lambda u: u.id):
                    self.hauler_ashore(w, port)
                if not s.passengers(t):
                    f["phase"], f["waited"] = "dock", 0

    def richest(self, isle, w):
        """The site on the landmass with the most wheat not yet claimed this
        turn, the nearest to w on ties. Claims what w has room for there."""
        sites = self.haul_isles().get(isle)
        if not sites:
            return None
        claims = self.claims
        site = max(sites, key=lambda x: (x.wheat - claims.get(x.id, 0), -bb.distance(w.hex, x.hex), -x.id))
        claims[site.id] = claims.get(site.id, 0) + self.cargo_max - w.wheat - w.bread
        return site

    def load_site(self, w, site):
        if self.cargo_max - w.wheat - w.bread > 0 and site.wheat > 0:
            self.load(w)

    def guard_need(self, w):
        """Bread a hauler takes out: the first hauler of each landmass takes
        enough for the guards there, and the others take none."""
        isle = self.haulers.get(w.id)
        if w.id != min((wid for wid, i in self.haulers.items() if i == isle), default=None):
            return 0
        guards = sum(1 for u in self.state.my_units() if self.land.get(u.hex) == isle and not u.carrier
                     and not self.types[u.type].carry and not self.types[u.type].naval)
        return min(self.cargo_max // 2, GUARD_BREAD * guards)

    def take_bread(self, w, port):
        """Loads the bread a hauler in port should take out, if the port can
        spare enough to be worth it."""
        amount = min(self.guard_need(w) - w.bread, port.bread - self.keep(port),
                     self.cargo_max - w.wheat - w.bread)
        if amount >= 50:
            self.load(w, amount)

    def unload_wheat(self, w):
        if w.wheat:
            self.tally["ferried"] += w.wheat
            self.unload(w, w.wheat)

    def hauler_home(self, w, t, port, there):
        """A hauler on the port's landmass unloads any wheat in the port,
        takes bread for the guards, and boards the ferry once it's in port."""
        if w.fresh or w.mp <= 0:
            return
        if w.wheat or (self.guard_need(w) - w.bread >= 50 and port.bread - self.keep(port) >= 50):
            self.enter(w, port)
            if w.hex == port.hex:
                self.unload_wheat(w)
                self.take_bread(w, port)
            return
        if there and self.board(w, t):
            return
        goals = [n for n in [port.hex] + bb.neighbors(port.hex) if n in self.defense]
        if w.hex not in goals:
            self.walk(w, self.distance_field(goals), sb.WAGON_CAUTION)

    def hauler_land(self, w, isle):
        """Puts a hauler ashore as near as it can get to the site it'll load
        at, and loads there if it lands on it."""
        site = self.richest(isle, w)
        if site is None:
            return
        field = self.site_field[site.id]
        options = {h: p for h, p in self.reachable(w).items() if h in field}
        if not options:
            return
        h = min(options, key=lambda h: (field[h], h))
        self.go(w, options[h])
        if not w.carrier and w.hex == site.hex:
            self.load_site(w, site)

    def hauler_island(self, w, t, f, lost):
        """A hauler on the landmass loads wheat at the sites there, and goes
        back aboard once it's nearly full, or once the ferry has waited long
        enough and it has some."""
        if w.fresh or w.mp <= 0:
            return
        room = self.cargo_max - w.wheat - w.bread
        site = None if lost else self.richest(f["isle"], w)
        if site is not None and room >= MIN_ROOM and not (f["waited"] >= FERRY_WAIT and w.wheat):
            if w.hex != site.hex:
                self.walk(w, self.site_field[site.id], sb.WAGON_CAUTION)
            if w.hex == site.hex:
                self.load_site(w, site)
            return
        if self.board(w, t):
            return
        goals = [n for n in bb.neighbors(t.hex) if n in self.defense]
        if goals:
            self.walk(w, self.distance_field(goals), sb.WAGON_CAUTION)

    def hauler_ashore(self, w, port):
        """Lands a hauler from the ferry in port, where it unloads its wheat
        and takes bread for the next trip."""
        if w.fresh:
            return
        reach = self.reachable(w)
        if port.hex not in reach:
            self.make_room(port, self.city_field[port.id])
            reach = self.reachable(w)
        if port.hex in reach:
            self.go(w, reach[port.hex])
        elif reach:
            field = self.city_field[port.id]
            self.go(w, reach[min(reach, key=lambda h: (field.get(h, 1 << 30), h))])
        if not w.carrier and w.hex == port.hex:
            self.unload_wheat(w)
            self.take_bread(w, port)

    def guard_sites(self):
        """Units on landmasses whose sites the ferries haul from stand on or
        by those sites, where the ferries' wagons feed them."""
        isles = self.haul_isles()
        if not isles:
            return
        self.refresh()
        for u in self.ready():
            if u.mp <= 0 or self.out_of_time():
                continue
            sites = isles.get(self.land.get(u.hex))
            if not sites or any(bb.distance(u.hex, x.hex) <= 1 for x in sites):
                continue
            goals = [h for x in sites for h in [x.hex] + bb.neighbors(x.hex) if h in self.defense]
            self.walk(u, self.distance_field(goals))

    # Crews.

    def run_crews(self):
        """Assigns land units to transports gathering a crew, and walks them
        over to board."""
        s = self.state
        gathering = [t for t in s.my_units() if t.type == self.transport
                     and self.voyages.get(t.id, {}).get("phase") == "gather" and self.voyages[t.id]["port"] is not None
                     and self.voyages[t.id].get("rest", 0) <= s.round]
        self.crew_short = 0
        if gathering and self.dests:
            open_land = self.open_land()
            army = [u for u in s.my_units() if not self.types[u.type].naval and not self.types[u.type].carry]
            # Units with nothing left to take on their landmass may all go;
            # of the rest, only a share.
            budget = int(len(army) * CREW_SHARE) - sum(
                1 for u in self.crew_of if self.land.get(s.units[u].hex) in open_land)
            # Units each landmass can spare: on the home island, those beyond
            # its garrison.
            spare = {}
            for u in army:
                if not u.carrier and u.id not in self.crew_of:
                    spare[self.land.get(u.hex)] = spare.get(self.land.get(u.hex), 0) + 1
            calm = not any(s.round - r <= SHIP_MEMORY for r in self.transports_seen.values())
            for c in s.my_cities():
                if self.land.get(c.hex) in self.home_island:
                    spare[self.land.get(c.hex)] = spare.get(self.land.get(c.hex), 0) - (
                        CALM_GARRISON if calm else GARRISON)
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
                    land = self.land.get(u.hex)
                    if spare.get(land, 0) <= 0:
                        continue
                    if land in open_land:
                        if budget <= 0:
                            break
                        budget -= 1
                    spare[land] -= 1
                    self.crew_of[u.id] = t.id
                    busy.add(u.id)
                    want -= 1
                if want > 0 and spare.get(self.land.get(port.hex), 0) <= 0:
                    self.crew_short = max(self.crew_short, want)
        for uid, tid in sorted(self.crew_of.items()):
            if self.out_of_time():
                return
            u, t = s.units.get(uid), s.units.get(tid)
            if u is None or t is None or u.carrier or u.acted or u.fresh or u.mp <= 0:
                continue
            if self.board(u, t):
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
        # Seas with somewhere worth sailing to, from the ports on them, and
        # those that reach the landmasses only ferries can haul from.
        useful = set()
        for c in self.ports():
            for sea in self.port_seas[c.id]:
                if self.best_destination(self.port_water_of(c, sea)[0], self.land.get(c.hex), strength) is not None:
                    useful.add(sea)
        isles = self.haul_isles() if self.haul else {}
        ours = set().union(*[self.port_seas[c.id] for c in self.ports()]) if self.ports() else set()
        for isle in isles:
            useful |= {self.sea[w] for w in self.isle_landings(isle)} & ours
        self.useful = useful
        # Seas where galleys have work: those, and those with enemy ports or ships.
        warlike = set(useful)
        for c in s.cities:
            if c.owner not in (0, self.player):
                warlike |= self.port_seas[c.id]
        recent = [e for e in s.enemy_units() if self.types[e.type].naval]
        warlike |= {self.sea.get(e.hex) for e in recent}
        seen = sum(1 for r in self.ships_seen.values() if s.round - r <= SHIP_MEMORY)
        # No more transports than the ports on those seas can feed.
        stocked = sum(c.bread for c in self.ports() if self.port_seas[c.id] & useful)
        want_t = 0
        if useful:
            want_t = min(MAX_TRANSPORTS, max(1, -(-idle // max(1, self.passengers_max))), 1 + stocked // SHIP_BREAD)
        # Each landmass only ferries can haul from wants a ferry of its own.
        want_t = min(MAX_TRANSPORTS, want_t + len(isles))
        want_g = min(MAX_GALLEYS, (want_t + 1) // 2 + (seen + 1) // 2) if self.galley and warlike else 0
        have_t = sum(1 for u in s.my_units() if u.type == self.transport and self.sea.get(u.hex) in useful)
        have_g = self.count(self.galley)
        if have_t >= want_t and have_g >= want_g:
            return
        # Units beyond what the bread income feeds would starve, but ships can
        # live on the bread stocked in their ports too. Otherwise a player
        # that has taken all of a small island fills it with the army its
        # income feeds, and never builds a ship.
        room = self.units_fed() - len(s.my_units()) + stocked // SHIP_BREAD
        near = {c.id: sum(1 for u in s.my_units() if not self.types[u.type].naval and bb.distance(u.hex, c.hex) <= 8)
                for c in self.ports()}
        for c in sorted(self.ports(), key=lambda c: (-near[c.id], c.id)):
            if room <= 0:
                break
            launch = self.launch_hex(c)
            if c.built or c.hex in self.enemy_hex or launch is None or self.blockaded(c):
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
