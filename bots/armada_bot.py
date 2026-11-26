"""Armada: a bot for the strategic ruleset's island maps (STRATEGIC.md §3).

It wins at sea first and on land afterward:

- The middle island's wheat. Its first wagon carries the home city's bread
  to the port, which builds a transport at once. From then on the transport
  ferries that wagon between the port and the sites out at sea, which grow
  more than everything on the home island together. One infantry goes
  along and stands on a site there, which keeps enemy wagons off it. A
  second wagon hauls the home site's wheat to the port. No wagon goes to
  an island where enemy land units were seen lately.
- A blockade. Its galleys take station on the water next to the enemy's
  port. A port launches ships onto those hexes, so a ship it builds is sunk
  before it can move, or can't be built at all. The galleys bombard what
  stands on the coast while they wait, and go home to heal and eat.
- The invasion. Everything else it builds is infantry and archers, which
  wait by the port. When two transports are full they sail together, land
  beside the nearest enemy city, strike as they land, and take it. The
  transports go back for more until the enemy has no city left.

It builds everything at the port, since that is where units embark, and it
reads the map and the rules from the init message. It needs supply and
ships: under other rules it only walks at the nearest city.

Usage: python bots/armada_bot.py
"""

import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kits", "python"))
import bitbath as bb  # noqa: E402

INF = float("inf")

# A wagon in the middle goes on loading while a site there holds MIN_PILE,
# and once it carries a FAIR_LOAD, only while one holds BIG_PILE.
MIN_PILE = 100
FAIR_LOAD = 400
BIG_PILE = 250
# The home site's wagon waits for this much wheat before it loads.
HOME_LOAD = 240
# Galleys it keeps, and the HP below which one goes home to heal.
GALLEYS = 3
GALLEY_HOME_HP = 45
# Transports for the invasion, and the land units it keeps for it.
LIFTS = 2
ARMY = 14
# The army's make-up, over and over.
ARMY_ORDER = "IAI"
# After enemy land units are seen on an island with sites, no wagon goes
# there for this many rounds.
SITE_SCARE = 15
# Bread the port keeps back from building, to feed what stands around it.
KEEP = 40
# On an island with enemy cities, an attack is worth making even at this loss.
ASSAULT_BAR = -40
# A transport with someone aboard sails after waiting this many turns for more.
WAIT = 4


class ArmadaBot(bb.Bot):
    def on_init(self):
        m, me = self.map, self.player
        self.home = next(h for _, h, o in m.cities if o == me)
        # Landmasses: every passable hex gets the number of its island.
        self.isle = {}
        n = 0
        for h in m.hexes():
            if m.passable(h) and h not in self.isle:
                self.isle[h] = n
                stack = [h]
                while stack:
                    for y in bb.neighbors(stack.pop()):
                        if m.passable(y) and y not in self.isle:
                            self.isle[y] = n
                            stack.append(y)
                n += 1
        self.home_isle = self.isle[self.home]
        self.coast = [h for h in self.isle if any(m.navigable(x) for x in bb.neighbors(h))]
        self.fields = {}
        self.est = {sid: 0 for sid, _, _, _ in m.sites}  # wheat we think each site holds
        self.ferry = self.midw = self.hauler = None
        self.roles = {}     # land unit id -> "garrison <city id>", "guard", or "army"
        self.mode = {}      # invasion transport id -> "load", "sail", or "back"
        self.waited = {}
        self.launched = False
        self.landing = {}
        self.scared = {}    # island -> the round enemy land units were last seen on it

    # Fields and movement.

    def land_to(self, h):
        """Move cost by land from every hex to h, ignoring units. Kept."""
        if h not in self.fields:
            self.fields[h] = self.distance_field([h], blocked=set())
        return self.fields[h]

    def sea_to(self, goals):
        """Move cost by sea from every hex to the nearest goal, around visible enemies."""
        return self.distance_field(goals, naval=True)

    def go(self, u, field, ok=None):
        """Moves u to the hex it can reach that is lowest in field, if that is
        lower than where it stands. Land units stay on land. Returns whether
        it moved."""
        if u.id not in self.state.units:
            return False
        naval = self.unit_type(u).naval
        here = field.get(u.hex, INF)
        best = None
        for h, dirs in self.reachable(u).items():
            if not naval and not self.map.passable(h):
                continue
            if ok is not None and not ok(h):
                continue
            key = (field.get(h, INF), len(dirs), h)
            if best is None or key < best[0]:
                best = (key, dirs)
        if best is None or best[0][0] >= here:
            return False
        return bool(self.move(u, best[1]))

    def board(self, u, t):
        """Moves u aboard transport t, or toward it. Returns whether it boarded."""
        if u.id not in self.state.units or u.carrier or u.mp <= 0:
            return False
        reach = self.reachable(u)
        if t.hex in reach:
            first = self.transport_with_room(t.hex)
            if first is not None and first.id == t.id:
                self.move(u, reach[t.hex])
                return u.carrier == t.id
            return False
        shore = [h for h in bb.neighbors(t.hex) if self.map.passable(h)]
        if shore:
            self.go(u, self.distance_field(shore, blocked=set()))
        return False

    def land(self, u, field, ok=None):
        """Lands u, which is aboard, on the hex next to its transport that is
        lowest in field. Returns whether it landed."""
        best = None
        for h, dirs in self.reachable(u).items():
            if ok is not None and not ok(h):
                continue
            key = (field.get(h, INF), h)
            if key[0] < INF and (best is None or key < best[0]):
                best = (key, dirs)
        if best is None:
            return False
        self.move(u, best[1])
        return not u.carrier

    # What is where.

    def mine(self, flag):
        return sorted((u for u in self.state.my_units() if flag in self.unit_type(u).flags), key=lambda u: u.id)

    def port(self):
        """Our port on the home island, or failing that any port of ours."""
        ports = [c for c in self.state.my_cities() if self.is_port(c)]
        ports.sort(key=lambda c: (self.isle[c.hex] != self.home_isle, c.id))
        return ports[0] if ports else None

    def dock(self, port):
        return [h for h in bb.neighbors(port.hex) if self.map.navigable(h)]

    def isle_of(self, u):
        return None if u.carrier else self.isle.get(u.hex)

    def foreign_cities(self, isle=None):
        return [c for c in self.state.cities if c.owner != self.player
                and (isle is None or self.isle[c.hex] == isle)]

    def mid_sites(self):
        """The sites only ships reach, on islands where the enemy holds no city."""
        held = {self.isle[c.hex] for c in self.state.cities if c.owner not in (0, self.player)}
        return [x for x in self.state.sites if self.isle[x.hex] != self.home_isle and self.isle[x.hex] not in held]

    def safe_sites(self):
        return [x for x in self.mid_sites()
                if self.state.round - self.scared.get(self.isle[x.hex], -99) > SITE_SCARE]

    def home_site(self):
        sites = [x for x in self.state.sites if self.isle[x.hex] == self.home_isle]
        field = self.land_to(self.home)
        return min(sites, key=lambda x: (field.get(x.hex, INF), x.id)) if sites else None

    def landing_for(self, goal, start):
        """The water hexes to sail to for landing as near goal as the coast
        allows: those whose shore is fewest moves from it."""
        key = goal
        if key not in self.landing:
            field = self.land_to(goal)
            best, out = INF, []
            for h in self.coast:
                d = field.get(h, INF)
                if d < best:
                    best, out = d, []
                if d == best and d < INF:
                    out.append(h)
            water = {x for h in out for x in bb.neighbors(h) if self.map.navigable(x)}
            self.landing[key] = sorted(water)
        return self.landing[key]

    # Fighting.

    def worth(self, utype, hp_lost, dies):
        return utype.cost * hp_lost / utype.max_hp + (utype.cost if dies else 0)

    def best_attack(self, u, move=True):
        """The best attack u can make this turn, as (score, target, hex, dirs), or None."""
        s = self.state
        if u.id not in s.units or not self.can_act(u) or u.carrier:
            return None
        t = self.unit_type(u)
        hurts = self.rules.damage.get(u.type, {})
        move = move and u.mp > 0 and not t.no_move_attack
        span = t.range_max + (u.mp if move else 0)
        near = [e for e in s.enemy_units()
                if not e.carrier and hurts.get(e.type, 0) > 0 and bb.distance(u.hex, e.hex) <= span]
        if not near:
            return None
        spots = {u.hex: ""}
        if move:
            enter = self.map.navigable if t.naval else self.map.passable
            spots.update((h, d) for h, d in self.reachable(u).items() if enter(h))
        best = None
        for e in near:
            et = self.unit_type(e)
            cargo = sum(self.unit_type(p).cost for p in s.passengers(e)) if et.transport else 0
            for r in range(t.range_min, t.range_max + 1):
                for h in bb.ring(e.hex, r):
                    if h not in spots:
                        continue
                    def_hp, att_hp = self.predict(u, e, h)
                    gain = self.worth(et, e.hp - def_hp, def_hp == 0) + cargo * (e.hp - def_hp) / et.max_hp
                    if et.naval and not t.naval:
                        gain *= 2  # only archers can keep ships off the coast
                    score = gain - self.worth(t, u.hp - att_hp, att_hp == 0)
                    key = (score, self.map.terrain(h).defense, -len(spots[h]))
                    if best is None or key > best[0]:
                        best = (key, e, h, spots[h])
        return best

    def fight(self, units, bar=0, move=True):
        """Makes the best attack any of the units has, then the next best, for
        as long as one scores above bar."""
        units = [u for u in units if not self.unit_type(u).transport and not self.unit_type(u).carry]
        while units:
            opts = [(o, u) for u in units for o in [self.best_attack(u, move)] if o]
            if not opts:
                return
            (key, e, h, dirs), u = max(opts, key=lambda x: x[0][0])
            if key[0] <= (bar(u) if callable(bar) else bar):
                units = [x for x in units if x is not u and x in [y for _, y in opts]]
                continue
            if h != u.hex:
                self.move(u, dirs)
            if u.hex == h and u.id in self.state.units:
                self.attack(u, e)
            units = [x for x in units if x is not u]

    def capture_all(self):
        for u in sorted(self.state.my_units(), key=lambda u: u.id):
            if self.can_capture(u):
                self.capture(u)

    # The turn.

    def on_turn(self, state):
        try:
            self.play(state)
        except bb.GameOver:
            raise
        except Exception:
            # Say so where the replay shows it, and play on next turn.
            for line in traceback.format_exc().splitlines()[-4:]:
                self.debug_text("error: " + line.strip())
            raise

    def play(self, state):
        if not (self.rules.supply and self.rules.naval):
            return self.plain_turn()
        self.take_stock()
        self.capture_all()
        self.run_galleys()
        self.run_ferry()
        self.run_lifts()
        self.capture_all()
        fighters = [u for u in state.my_units() if not self.unit_type(u).naval]
        self.fight([u for u in fighters if not self.roles.get(u.id, "").startswith("g")], self.bar)
        self.fight(fighters, move=False)
        self.capture_all()
        self.run_hauler()
        self.run_land()
        self.capture_all()
        self.fight([u for u in state.my_units() if u.id in state.units], move=False)
        self.build_units()
        self.report()

    def plain_turn(self):
        """Without supply and ships: take the nearest city, and nothing more."""
        self.capture_all()
        for u in self.state.my_units():
            cities = self.foreign_cities()
            if cities and self.unit_type(u).capture and self.can_act(u):
                c = min(cities, key=lambda c: (self.land_to(c.hex).get(u.hex, INF), c.id))
                self.go(u, self.land_to(c.hex))
        self.capture_all()
        types = self.rules.unit_types
        for c in self.state.my_cities():
            if "I" in types and not c.built and types["I"].cost <= self.state.gold:
                self.build(c, "I")

    def bar(self, u):
        return ASSAULT_BAR if self.foreign_cities(self.isle_of(u)) else 0

    def take_stock(self):
        """Updates the wheat estimates, and gives every unit its job."""
        s = self.state
        mine = s.my_units()
        for x in s.sites:
            self.est[x.id] = min(x.cap, self.est[x.id] + x.output)
            if any(bb.distance(u.hex, x.hex) <= 2 for u in mine) or \
                    any(bb.distance(c.hex, x.hex) <= self.rules.city_vision for c in s.my_cities()):
                self.est[x.id] = x.wheat
        for e in s.enemy_units():
            if not e.carrier and e.hex in self.isle:
                self.scared[self.isle[e.hex]] = s.round
        ids = {u.id for u in mine}
        wagons, ships = self.mine("carry"), self.mine("transport")
        if self.midw not in ids:
            self.midw = None
        if self.hauler not in ids:
            self.hauler = None
        for w in wagons:
            if w.id in (self.midw, self.hauler):
                continue
            if self.midw is None:
                self.midw = w.id
            elif self.hauler is None:
                self.hauler = w.id
        if self.ferry not in ids:
            self.ferry = ships[0].id if ships else None
        self.lifts = [t for t in ships if t.id != self.ferry]
        self.mode = {t.id: self.mode.get(t.id, "load") for t in self.lifts}
        self.galleys = [u for u in mine if self.unit_type(u).naval and not self.unit_type(u).transport]

        # Land units: one garrison for each city of ours, one guard for the
        # middle sites, and the rest are the army.
        self.roles = {uid: r for uid, r in self.roles.items() if uid in ids}
        land = [u for u in mine if not self.unit_type(u).naval and not self.unit_type(u).carry]
        port = self.port()
        posts = [c for c in s.cities if self.isle[c.hex] == self.home_isle]
        posts.sort(key=lambda c: (not self.map.is_port(c.hex), c.id))
        for u in sorted(land, key=lambda u: (self.unit_type(u).capture, u.id)):
            if u.id in self.roles:
                continue
            taken = set(self.roles.values())
            role = "army"
            if not self.unit_type(u).capture:
                if self.unit_type(u).skirmish:
                    role = "garrison %d" % next(c.id for c in s.cities if c.hex == self.home)
            else:
                for c in posts:
                    if "garrison %d" % c.id not in taken and (c.hex != self.home or len(land) > 3):
                        role = "garrison %d" % c.id
                        break
                else:
                    if "guard" not in taken and self.mid_sites() and not self.launched and len(land) <= 4:
                        role = "guard"
            self.roles[u.id] = role
        del port

    def units_with(self, role):
        return [u for u in self.state.my_units() if self.roles.get(u.id) == role]

    # The middle island's wheat.

    def run_ferry(self):
        s = self.state
        w, t = s.units.get(self.midw), s.units.get(self.ferry)
        port = self.port()
        if w is None:
            if t is not None and port is not None:
                self.go(t, self.sea_to(self.dock(port)))
            return
        if t is None or port is None:
            self.stock_port(w)
            return
        sites = [x for x in self.safe_sites() if not s.enemy_at(x.hex)]
        room = self.rules.cargo_max - w.wheat - w.bread
        least = BIG_PILE if w.wheat >= FAIR_LOAD else MIN_PILE
        rich = [x for x in sites if self.est[x.id] >= least] if room >= MIN_PILE else []
        guards = self.units_with("guard")

        # Boarding comes first, since the transport sails afterward.
        if not w.carrier:
            if self.isle[w.hex] == self.home_isle:
                if w.wheat or (w.bread and w.hex != port.hex and not rich):
                    self.deliver(w, port)
                elif rich and w.mp > 0:
                    self.board(w, t)
            elif rich:
                site = max(rich, key=lambda x: (x.hex == w.hex, self.est[x.id], -x.id))
                if w.hex != site.hex:
                    self.go(w, self.land_to(site.hex))
                if w.hex == site.hex and s.site_at(w.hex).wheat > 0:
                    self.load(w)
                    self.est[site.id] = site.wheat
            else:
                self.board(w, t)
        for g in guards:
            if not g.carrier and self.isle[g.hex] == self.home_isle and port.owner == self.player:
                if not self.foreign_cities(self.home_isle):
                    self.board(g, t)

        aboard = w.carrier == t.id
        homebound = aboard and (w.wheat > 0 or not rich)
        outbound = aboard and not homebound
        if outbound or (not aboard and self.isle[w.hex] != self.home_isle):
            if sites:
                anchor = min(sites, key=lambda x: x.id)
                self.go(t, self.sea_to(self.landing_for(anchor.hex, t.hex)))
        else:
            self.go(t, self.sea_to(self.dock(port)))

        if homebound and bb.distance(t.hex, port.hex) == 1:
            if self.land(w, {port.hex: 0}):
                self.deliver(w, port)
        elif outbound and sites:
            target = max(sites, key=lambda x: (self.est[x.id], -x.id))
            field = self.land_to(target.hex)
            if any(self.isle.get(h) == self.isle[target.hex] for h in bb.neighbors(t.hex)):
                if self.land(w, field) and w.hex == target.hex and target.wheat > 0:
                    self.load(w)
                    self.est[target.id] = target.wheat
                for g in s.passengers(t):
                    post = min(sites, key=lambda x: (x.id == target.id, x.id))
                    self.land(g, self.land_to(post.hex))

    def deliver(self, w, city):
        """Takes a wagon's goods to a city of ours and unloads them there."""
        if w.hex != city.hex:
            self.go(w, self.land_to(city.hex))
        if w.hex == city.hex and city.owner == self.player and (w.wheat or w.bread):
            self.unload(w)
            guards = [g for g in self.units_with("guard") if not g.carrier and self.isle[g.hex] != self.home_isle]
            if guards and w.id == self.midw and city.bread >= 200:
                need = sum(self.rules.ration_max - g.rations for g in guards) * self.rules.ration_cost
                if need > 0:
                    self.load(w, need + 20)

    def stock_port(self, w):
        """Before there is a transport: carries the home city's bread to the
        port, which has none of its own to build one with."""
        s = self.state
        home = s.city_at(self.home)
        ports = [c for c in s.cities if self.isle[c.hex] == self.home_isle and self.map.is_port(c.hex)]
        if not ports or w.carrier:
            return
        port = min(ports, key=lambda c: (self.land_to(self.home).get(c.hex, INF), c.id))
        if w.bread or w.wheat:
            self.deliver(w, port)
        elif w.hex == self.home and home.owner == self.player and home.bread >= 100:
            self.load(w)
        elif w.hex != port.hex and home.bread < 100:
            self.go(w, self.land_to(port.hex))
        elif w.hex != self.home and home.bread >= 150:
            self.go(w, self.land_to(self.home))

    def run_hauler(self):
        """The home site's wagon: loads when enough has piled up, and unloads in the port."""
        s = self.state
        w, site = s.units.get(self.hauler), self.home_site()
        if w is None or site is None or w.carrier:
            return
        city = self.port() or s.city_at(self.home)
        if city.owner != self.player:
            return
        if w.wheat or w.bread:
            self.deliver(w, city)
        else:
            if w.hex != site.hex and not s.enemy_at(site.hex):
                self.go(w, self.land_to(site.hex))
            if w.hex == site.hex and site.wheat >= min(HOME_LOAD, site.cap):
                self.load(w)
                self.est[site.id] = site.wheat

    # The navy.

    def stations(self):
        """The water next to each enemy port, where its ships would appear."""
        out = []
        for c in self.state.cities:
            if c.owner not in (0, self.player) and self.map.is_port(c.hex):
                out += [h for h in bb.neighbors(c.hex) if self.map.navigable(h)]
        return out

    def run_galleys(self):
        s = self.state
        port = self.port()
        home = self.dock(port) if port else []
        self.fight(self.galleys)
        free = self.stations()
        for g in sorted(self.galleys, key=lambda u: u.id):
            if g.id not in s.units or g.mp <= 0 or not self.can_act(g):
                continue
            docked = port is not None and bb.distance(g.hex, port.hex) == 1
            trip = self.sea_to(home).get(g.hex, INF) / self.unit_type(g).move if home else INF
            weak = g.hp < GALLEY_HOME_HP or (docked and g.hp < self.unit_type(g).max_hp - 10)
            hungry = g.rations < 2 * trip + 6 or (docked and g.rations < self.rules.ration_max - 10)
            if home and (weak or hungry):
                self.go(g, self.sea_to(home))
                continue
            if not free:
                # No enemy port: stay by our own, between it and the enemy.
                if home:
                    self.go(g, self.sea_to(home))
                continue
            fields = {h: self.sea_to([h]) for h in free}
            spot = min(free, key=lambda h: (fields[h].get(g.hex, INF), h))
            if len(free) > 1:
                free.remove(spot)
            self.go(g, fields[spot])
        self.fight([g for g in self.galleys if g.id in s.units], move=False)

    # The invasion.

    def target(self):
        """The city to invade next: an enemy port first, then the nearest city."""
        cities = [c for c in self.foreign_cities() if self.isle[c.hex] != self.home_isle]
        if not cities:
            return None
        return min(cities, key=lambda c: (c.owner == 0, not self.map.is_port(c.hex),
                                          bb.distance(c.hex, self.home), c.id))

    def run_lifts(self):
        s = self.state
        port, target = self.port(), self.target()
        if port is None:
            return
        dock = self.dock(port)
        ferry = s.units.get(self.ferry)
        berth = [h for h in dock if ferry is None or h != ferry.hex] or dock
        size = self.rules.passengers_max
        loading = [t for t in self.lifts if self.mode[t.id] == "load"]
        full = [t for t in loading if len(s.passengers(t)) >= size]
        if target is not None and loading:
            beachhead = any(self.isle_of(u) == self.isle[target.hex] for u in s.my_units())
            if len(full) == len(self.lifts) or (self.launched and full) or (beachhead and full):
                for t in full:
                    self.mode[t.id] = "sail"
                self.launched = True
            for t in loading:
                if self.mode[t.id] == "load" and self.launched and s.passengers(t):
                    self.waited[t.id] = self.waited.get(t.id, 0) + 1
                    if self.waited[t.id] > WAIT:
                        self.mode[t.id] = "sail"
        for t in self.lifts:
            mode = self.mode[t.id]
            if mode == "sail" and (target is None or not s.passengers(t)):
                mode = self.mode[t.id] = "back"
            if mode == "sail":
                self.waited[t.id] = 0
                goal = target.hex
                self.go(t, self.sea_to(self.landing_for(goal, t.hex)))
                isle = self.isle[goal]
                if any(self.isle.get(h) == isle for h in bb.neighbors(t.hex)):
                    field = self.land_to(goal)
                    near = min((field.get(h, INF) for h in bb.neighbors(t.hex)), default=INF)
                    best = min(field.get(h, INF) for h in self.coast if self.isle[h] == isle)
                    if near <= best + 2:
                        for u in sorted(s.passengers(t), key=lambda u: (not self.unit_type(u).capture, u.id)):
                            ok = None if self.unit_type(u).capture else (lambda h: h != goal)
                            self.land(u, field, ok)
                if not s.passengers(t):
                    self.mode[t.id] = "back"
            elif mode == "back":
                self.go(t, self.sea_to(berth))
                if t.hex in dock:
                    self.mode[t.id] = "load"
            else:
                self.go(t, self.sea_to(berth))

    def run_land(self):
        s = self.state
        port = self.port()
        loading = [t for t in self.lifts if self.mode[t.id] == "load" and port is not None
                   and bb.distance(t.hex, port.hex) == 1]
        stage = None
        for u in sorted(s.my_units(), key=lambda u: u.id):
            role = self.roles.get(u.id)
            if role is None or u.carrier or u.id not in s.units or not self.can_act(u) or u.mp <= 0:
                continue
            isle = self.isle[u.hex]
            foreign = self.foreign_cities(isle)
            capture = self.unit_type(u).capture
            if role.startswith("garrison"):
                city = s.cities[int(role.split()[1])]
                if u.hex != city.hex:
                    self.go(u, self.land_to(city.hex))
            elif role == "guard" and isle != self.home_isle:
                sites = [x for x in self.mid_sites() if self.isle[x.hex] == isle]
                w = s.units.get(self.midw)
                if sites:
                    post = min(sites, key=lambda x: (w is not None and x.hex == w.hex, x.id))
                    if u.hex not in [x.hex for x in sites]:
                        self.go(u, self.land_to(post.hex))
            elif foreign and (capture or role == "army"):
                # Take the nearest city here that isn't ours.
                city = min(foreign, key=lambda c: (self.land_to(c.hex).get(u.hex, INF), c.id))
                field = self.land_to(city.hex)
                if capture:
                    self.go(u, field)
                else:
                    reach = self.unit_type(u).range_max
                    held = s.enemy_at(city.hex)
                    self.go(u, field, lambda h: h != city.hex and (not held or bb.distance(h, city.hex) >= reach))
            elif role == "guard" and port is not None:
                if u.hex != port.hex:
                    self.go(u, self.land_to(port.hex))
            elif role == "army" and isle == self.home_isle and port is not None:
                for t in loading:
                    if self.board(u, t):
                        break
                else:
                    if stage is None:
                        shore = [h for x in self.dock(port) for h in bb.neighbors(x)
                                 if self.map.passable(h) and h != port.hex]
                        stage = self.distance_field(shore or [port.hex], blocked=set())
                    if u.id in s.units and not u.carrier and u.mp > 0:
                        self.go(u, stage, lambda h: h != port.hex)

    # Building.

    def build_units(self):
        s = self.state
        types = self.rules.unit_types
        port = self.port()
        mine = s.my_units()

        def count(code):
            return sum(1 for u in mine if u.type == code)

        land = [u for u in mine if not self.unit_type(u).naval and not self.unit_type(u).carry]
        army = [u for u in land if self.roles.get(u.id, "army") == "army"]
        for city in s.my_cities():
            if city.built or s.enemy_at(city.hex) or s.friends_at(city.hex) >= self.rules.stack_limit:
                continue
            want = []
            if city is port:
                if self.ferry is None:
                    want.append("T")
                if self.midw is None and self.safe_sites():
                    want.append("W")
                if not any(r == "garrison %d" % city.id for r in self.roles.values()):
                    want.append("I")
                if s.units.get(self.ferry) is not None and (self.midw is not None or not self.safe_sites()):
                    if count("G") < 2:
                        want.append("G")
                    if self.hauler is None and self.home_site() is not None:
                        want.append("W")
                    if count("G") < GALLEYS:
                        want.append("G")
                    if len(self.lifts) < LIFTS and len(army) >= 2 * (len(self.lifts) + 1):
                        want.append("T")
                    if len(army) < ARMY:
                        want.append(ARMY_ORDER[len(land) % len(ARMY_ORDER)])
            elif port is None or self.isle[city.hex] != self.home_isle:
                want.append("I")
            elif len(army) < ARMY and self.ferry is not None:
                want.append("I")
            for code in want[:1]:
                if code in types and types[code].naval and self.launch_hex(city) is None:
                    continue  # the water by the port is full
                if code in types and city.bread >= types[code].cost + (KEEP if code not in "TW" else 0):
                    if self.build(city, code):
                        mine = s.my_units()

    def report(self):
        s = self.state
        land = [u for u in s.my_units() if not self.unit_type(u).naval and not self.unit_type(u).carry]
        away = sum(1 for u in land if not u.carrier and self.isle[u.hex] != self.home_isle)
        self.debug_text("armada: %d bread, %d land units (%d abroad), %d galleys, %d transports, lifts %s, est %s" % (
            sum(c.bread for c in s.my_cities()), len(land), away, len(self.galleys),
            len(self.lifts) + (self.ferry is not None), sorted(self.mode.values()), sorted(self.est.values())))


if __name__ == "__main__":
    bb.run(ArmadaBot())
