"""Armada: a bot for the strategic ruleset's island maps (STRATEGIC.md §3).

It wins at sea first and on land afterward:

- The middle island's wheat. Its first wagon carries the home city's bread
  to the port, which builds a transport at once. From then on the transport
  ferries that wagon between the port and the sites out at sea, which grow
  more than everything on the home island together. A second wagon hauls
  the home site's wheat to the port.
- Guards. The starting infantry and scout sail with the wagon and stay on
  the middle island. They stand on the sites, which keeps enemy wagons off
  them, and hunt enemy units that land. If enemy units are ashore when the
  transport arrives, it pulls up beside one, a wagon for choice, and the
  guards strike as they land. The wagon stays aboard, or goes back aboard,
  while enemies that could hurt it are near, though with two guards about
  it puts up with one. For each enemy seen there the port builds another
  guard, and the transport takes them over. If the island is lost, the
  wagon hauls the home site's wheat instead.
- Galleys. They sink the transports they find, bombard enemy units on the
  shores of the middle island and of the home island, and otherwise take
  station on the water next to the enemy's port. A port launches ships onto
  those hexes, so a ship it builds is sunk before it can move, or can't be
  built at all. They go home to heal and eat.
- The invasion. Everything else it builds is infantry and archers, which
  wait by the port. When two transports are full they sail together, land
  beside the nearest enemy city, strike as they land, and take it. The
  transports go back for more until the enemy has no city left. While
  enemy units are on the home island, the army stays to fight them.

It builds nearly everything at the port, since that is where units embark,
and it reads the map and the rules from the init message. It needs supply
and ships: under other rules it only walks at the nearest city.

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
# Land units it keeps on the middle sites, one more for each enemy seen
# there, and the most it keeps.
GUARDS = 2
MAX_GUARDS = 6
# An enemy land unit seen on an island is thought to be there still for this
# many rounds, unless someone looks and finds it gone.
MEMORY = 20
# A wagon keeps this many hexes from enemy units that could hurt it. With two
# guards about and more than WAGON_BRAVE HP, it puts up with one.
WAGON_FEAR = 3
WAGON_BRAVE = 70
# The ferry goes home when an enemy warship is seen this near.
SHIP_FEAR = 7
# Galleys it keeps, one more for each enemy warship seen lately up to
# MAX_GALLEYS, and the HP below which one goes home to heal.
GALLEYS = 3
MAX_GALLEYS = 5
GALLEY_HOME_HP = 45
# Galleys that bombard one enemy unit at a time.
BOMBARD = 2
# Transports for the invasion, and the land units it keeps for it.
LIFTS = 2
ARMY = 14
# The army's make-up, over and over.
ARMY_ORDER = "IAI"
# Bread the port keeps back from building its army, to feed what stands around it.
KEEP = 40
# Where there are enemy cities to take or landed enemies to clear, an attack
# is worth making even at this loss.
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
        # Each water hex with land next to it, and that land.
        self.shores = {}
        for h in m.hexes():
            if m.navigable(h):
                land = [x for x in bb.neighbors(h) if x in self.isle]
                if land:
                    self.shores[h] = land
        self.fields = {}
        self.est = {sid: 0 for sid, _, _, _ in m.sites}  # wheat we think each site holds
        self.ferry = self.midw = self.hauler = None
        self.roles = {}     # land unit id -> "garrison <city id>", "guard", or "army"
        self.mode = {}      # invasion transport id -> "load", "sail", or "back"
        self.waited = {}
        self.launched = False
        self.foes = {}      # enemy land unit id -> (hex, round) it was last seen
        self.seen_now = set()
        self.warships = {}  # enemy warship id -> round it was last seen
        self.want_guards = 0

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

    def approach(self, u, hexes):
        """Moves u toward the nearest of some enemy hexes, as close as it can
        still attack from."""
        near = self.unit_type(u).range_min
        self.go(u, self.distance_field(hexes, blocked=set()),
                lambda h: min(bb.distance(h, e) for e in hexes) >= near)

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
        if u.moved:
            return False  # it boarded this turn
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

    def enemy_isles(self):
        return {self.isle[c.hex] for c in self.state.cities if c.owner not in (0, self.player)}

    def mid_sites(self):
        """The sites only ships reach, on islands where the enemy holds no city."""
        held = self.enemy_isles()
        return [x for x in self.state.sites if self.isle[x.hex] != self.home_isle and self.isle[x.hex] not in held]

    def home_site(self):
        sites = [x for x in self.state.sites if self.isle[x.hex] == self.home_isle]
        field = self.land_to(self.home)
        return min(sites, key=lambda x: (field.get(x.hex, INF), x.id)) if sites else None

    def landing_for(self, goal):
        """The water hexes to sail to for landing as near goal as the coast
        allows: those with free land beside them that is fewest moves from
        it, and better two such hexes than one, so that more can land."""
        field = self.land_to(goal)
        isle = self.isle[goal]
        taken = {e.hex for e in self.state.enemy_units() if not e.carrier}
        best, out = INF, []
        for x, land in self.shores.items():
            free = sorted(field.get(h, INF) for h in land if self.isle[h] == isle and h not in taken)
            if not free or free[0] == INF or x in taken:
                continue
            score = free[0] + (0 if len(free) > 1 else 2)
            if score < best:
                best, out = score, []
            if score == best:
                out.append(x)
        return sorted(out)

    def fighter(self, e):
        """Whether an enemy unit is a land unit, ashore, that can hurt something."""
        return (not e.carrier and e.hex in self.isle and not self.unit_type(e).naval
                and any(self.rules.damage.get(e.type, {}).values()))

    def threat(self, isle):
        """Where enemy land units stand on an island: those in view, and those
        seen there lately that haven't been seen to leave or die."""
        s = self.state
        out = []
        for e in s.enemy_units():
            if self.fighter(e):
                self.foes[e.id] = (e.hex, s.round)
                self.seen_now.add(e.id)
                if self.isle[e.hex] == isle:
                    out.append(e.hex)
        for eid, (h, _) in self.foes.items():
            if eid not in self.seen_now and self.isle[h] == isle:
                out.append(h)
        return out

    def mid_threat(self):
        return sum(len(self.threat(i)) for i in {self.isle[x.hex] for x in self.mid_sites()})

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
            cargo += e.wheat + e.bread
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

    def bar(self, u):
        isle = self.isle_of(u)
        if isle is None:
            return 0
        return ASSAULT_BAR if self.foreign_cities(isle) or self.threat(isle) else 0

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
        self.fight([u for u in fighters if not self.roles.get(u.id, "").startswith("garrison")], self.bar)
        self.fight(fighters, self.bar, move=False)
        self.capture_all()
        self.run_hauler()
        self.run_land()
        self.capture_all()
        self.fight([u for u in state.my_units() if u.id in state.units], self.bar, move=False)
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

    def take_stock(self):
        """Updates what it remembers, and gives every unit its job."""
        s = self.state
        mine = s.my_units()
        for x in s.sites:
            self.est[x.id] = min(x.cap, self.est[x.id] + x.output)
            if any(bb.distance(u.hex, x.hex) <= 2 for u in mine) or \
                    any(bb.distance(c.hex, x.hex) <= self.rules.city_vision for c in s.my_cities()):
                self.est[x.id] = x.wheat

        # Enemy land units: forget those gone from where they were last seen.
        self.seen_now = set()
        for e in s.enemy_units():
            t = self.unit_type(e)
            if t.naval and not t.transport:
                self.warships[e.id] = s.round
        for eid, (h, rnd) in list(self.foes.items()):
            sight = 2 if self.map.terrain(h).conceal == 0 else 1
            if eid in s.units or s.round - rnd > MEMORY or any(bb.distance(u.hex, h) <= sight for u in mine):
                del self.foes[eid]
        mid_threat = self.mid_threat()

        ids = {u.id for u in mine}
        wagons, ships = self.mine("carry"), self.mine("transport")
        if self.midw not in ids:
            self.midw = None
        if self.hauler not in ids:
            self.hauler = None
        for w in wagons:
            if w.id in (self.midw, self.hauler):
                continue
            if self.midw is None and not (mid_threat and self.hauler is None):
                self.midw = w.id
            elif self.hauler is None:
                self.hauler = w.id
        # With the middle island lost, its wagon hauls at home if no one does.
        w = s.units.get(self.midw)
        if (w is not None and self.hauler is None and mid_threat and not w.carrier
                and self.isle[w.hex] == self.home_isle and not w.wheat):
            self.hauler, self.midw = self.midw, None
        if self.ferry not in ids:
            self.ferry = ships[0].id if ships else None
        self.lifts = [t for t in ships if t.id != self.ferry]
        self.mode = {t.id: self.mode.get(t.id, "load") for t in self.lifts}
        self.galleys = [u for u in mine if self.unit_type(u).naval and not self.unit_type(u).transport]

        # Land units: the starting units guard the middle sites. After that
        # the port gets a garrison, then the guards their number, then the
        # home city its garrison, and the rest are the army.
        self.roles = {uid: r for uid, r in self.roles.items() if uid in ids}
        land = [u for u in mine if not self.unit_type(u).naval and not self.unit_type(u).carry]
        posts = [c for c in s.cities if self.isle[c.hex] == self.home_isle]
        posts.sort(key=lambda c: (not self.map.is_port(c.hex), c.id))
        self.want_guards = min(GUARDS + mid_threat, MAX_GUARDS) if self.mid_sites() else 0
        for u in sorted(land, key=lambda u: u.id):
            if u.id in self.roles:
                continue
            taken = list(self.roles.values())
            lacking = [c for c in posts if "garrison %d" % c.id not in taken]
            role = "army"
            if s.round <= 1 and self.mid_sites():
                role = "guard"
            elif self.unit_type(u).capture:
                here = [c for c in lacking if c.hex == u.hex]
                if here:
                    role = "garrison %d" % here[0].id
                elif lacking and self.map.is_port(lacking[0].hex):
                    role = "garrison %d" % lacking[0].id
                elif taken.count("guard") < self.want_guards:
                    role = "guard"
                elif lacking:
                    role = "garrison %d" % lacking[0].id
            self.roles[u.id] = role
        # Guards are needed now: the army's infantry at home fill the gaps.
        spare = [u for u in land if self.roles[u.id] == "army" and self.unit_type(u).capture
                 and not u.carrier and self.isle[u.hex] == self.home_isle]
        while spare and list(self.roles.values()).count("guard") < self.want_guards:
            self.roles[spare.pop(0).id] = "guard"

    def units_with(self, role):
        return [u for u in self.state.my_units() if self.roles.get(u.id) == role]

    # The middle island.

    def post_for(self, u, sites):
        """The site a guard stands on: the one with the fewest others on it."""
        def crowd(x):
            return sum(1 for v in self.state.my_units() if v.hex == x.hex and v is not u
                       and not v.carrier and not self.unit_type(v).carry)
        return min(sites, key=lambda x: (crowd(x), 0 if u.carrier else self.land_to(x.hex).get(u.hex, INF), x.id))

    def run_ferry(self):
        s = self.state
        w, t = s.units.get(self.midw), s.units.get(self.ferry)
        port = self.port()
        if t is None or port is None:
            if w is not None:
                self.stock_port(w)
            return
        dock = self.dock(port)
        sites = self.mid_sites()
        if not sites:
            self.go(t, self.sea_to(dock))
            return
        anchor = min(sites, key=lambda x: x.id)
        isle = self.isle[anchor.hex]
        sites = [x for x in sites if self.isle[x.hex] == isle]
        threat = self.threat(isle)
        guards = self.units_with("guard")
        waiting = [g for g in guards if not g.carrier and self.isle[g.hex] == self.home_isle]

        mates = [g for g in guards if not g.carrier and self.isle[g.hex] == isle and self.unit_type(g).capture]

        def safe(h):
            near = sum(1 for e in threat if bb.distance(h, e) <= WAGON_FEAR)
            return near == 0 or (near == 1 and len(mates) >= 2 and w is not None and w.hp > WAGON_BRAVE)

        def rich():
            """The sites worth the wagon's while that it can go to safely."""
            if w is None or self.rules.cargo_max - w.wheat - w.bread < MIN_PILE:
                return []
            least = BIG_PILE if w.wheat >= FAIR_LOAD else MIN_PILE
            return [x for x in sites if self.est[x.id] >= least and not s.enemy_at(x.hex) and safe(x.hex)]

        # Boarding comes first, since the transport sails afterward.
        if w is not None and not w.carrier:
            if self.isle[w.hex] == self.home_isle:
                if w.wheat or (w.bread and w.hex != port.hex and not rich()):
                    self.deliver(w, port)
                elif rich():
                    self.board(w, t)
            elif rich() and safe(w.hex):
                site = max(rich(), key=lambda x: (x.hex == w.hex, self.est[x.id], -x.id))
                if w.hex != site.hex:
                    self.go(w, self.land_to(site.hex), safe)
                if w.hex == site.hex and site.wheat > 0:
                    self.load(w)
                    self.est[site.id] = site.wheat
            else:
                self.board(w, t)  # nothing left to load, or an enemy too near
        if port.owner == self.player and not self.foreign_cities(self.home_isle):
            for g in waiting:
                self.board(g, t)

        aboard = s.passengers(t)
        g_aboard = [u for u in aboard if self.roles.get(u.id) == "guard" and self.unit_type(u).capture]
        ashore = [g for g in guards if not g.carrier and self.isle[g.hex] == isle and self.unit_type(g).capture]
        w_aboard = w is not None and w.carrier == t.id
        w_mid = w is not None and not w.carrier and self.isle[w.hex] == isle
        piles = [x for x in sites if self.est[x.id] >= MIN_PILE and not s.enemy_at(x.hex)]
        strong = len(g_aboard) + len(ashore) > len(threat)
        # Three reasons to be at the island: the wagon is there, or going
        # there, or guards are going to hold it or to take it back.
        holding = len(g_aboard) + len(ashore) >= len(threat) and bool(g_aboard or ashore)
        haul = w_aboard and not w.wheat and bool(piles) and (not threat or holding)
        left = [g for g in waiting if not g.carrier]
        relief = (bool(g_aboard) and (bool(threat) or not ashore) and strong
                  and (len(aboard) >= self.rules.passengers_max or not left))
        out = w_mid or haul or relief
        warships = [e for e in s.enemy_units() if self.unit_type(e).naval and not self.unit_type(e).transport
                    and bb.distance(e.hex, t.hex) <= SHIP_FEAR]
        if warships and not any(bb.distance(g.hex, t.hex) <= 2 for g in self.galleys):
            out = False
        at_isle = any(self.isle[h] == isle for h in self.shores.get(t.hex, []))
        if not out:
            self.go(t, self.sea_to(dock))
        elif not (w_mid and at_isle):
            # Land the wagon by the site it wants. While it is ashore, the
            # transport waits where it is, so that the wagon finds it.
            goal = max(rich(), key=lambda x: (self.est[x.id], -x.id)).hex if w_aboard and rich() else anchor.hex
            spots = self.landing_for(goal)
            if spots:
                self.go(t, self.sea_to(spots))
        if g_aboard:
            self.strike_landing(t, isle, strong)

        if any(self.isle[h] == isle for h in self.shores.get(t.hex, [])):
            for g in [u for u in s.passengers(t) if self.roles.get(u.id) == "guard"]:
                threat = self.threat(isle)
                if not threat:
                    self.land(g, self.land_to(self.post_for(g, sites).hex))
                elif self.unit_type(g).capture:
                    self.land(g, self.distance_field(threat, blocked=set()))
                # A scout stays aboard, and keeps watch, while enemies are about.
            threat = self.threat(isle)
            if w_aboard and w.carrier and not w.wheat and rich():
                site = max(rich(), key=lambda x: (self.est[x.id], -x.id))
                if self.land(w, self.land_to(site.hex), safe) and w.hex == site.hex and site.wheat > 0:
                    self.load(w)
                    self.est[site.id] = site.wheat
        elif w_aboard and w.carrier and not out and bb.distance(t.hex, port.hex) == 1:
            if self.land(w, {port.hex: 0}):
                self.deliver(w, port)

    def strike_landing(self, t, isle, strong):
        """With fighters aboard and enemy units ashore on the island: brings
        the transport beside one of them, a wagon for choice, and lands the
        fighters next to it. A unit that lands can still attack this turn."""
        s = self.state
        prey = [e for e in s.enemy_units() if not e.carrier and self.isle.get(e.hex) == isle
                and (strong or self.unit_type(e).carry)]
        fighters = [u for u in s.passengers(t) if self.unit_type(u).capture and not u.moved]
        if not prey or not fighters:
            return
        scouts = [u for u in s.passengers(t) if self.roles.get(u.id) == "guard" and u not in fighters and not u.moved]
        taken = {e.hex for e in s.enemy_units() if not e.carrier}
        spots = {t.hex: ""}
        if t.mp > 0 and self.can_act(t):
            spots.update(self.reachable(t))
        best = None
        for x, dirs in spots.items():
            for e in prey:
                beside = [h for h in self.shores.get(x, []) if h not in taken and bb.distance(h, e.hex) == 1]
                if beside:
                    key = (self.unit_type(e).carry, -self.map.terrain(e.hex).defense, min(len(beside), 2), -len(dirs))
                    if best is None or key > best[0]:
                        best = (key, x, dirs, e)
        if best is None:
            return
        _, x, dirs, e = best
        if x != t.hex:
            self.move(t, dirs)
        if t.hex == x:
            # A scout is worth risking to finish off a wagon.
            for u in fighters + (scouts if self.unit_type(e).carry else []):
                self.land(u, {h: 0 for h in self.shores[x]}, lambda h: bb.distance(h, e.hex) == 1)

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
        # Enemy units on shores that aren't the enemy's own: the middle
        # island's, and ours.
        held = self.enemy_isles()
        shore = {}
        for isle in set(self.isle.values()) - held:
            for h in self.threat(isle):
                water = [x for x in bb.neighbors(h) if self.map.navigable(x)]
                if water:
                    shore[h] = water
        busy = {h: 0 for h in shore}
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
            prey = [x for e in s.enemy_units() if self.unit_type(e).transport
                    for x in bb.neighbors(e.hex) if self.map.navigable(x)]
            if prey:
                self.go(g, self.sea_to(prey))
                continue
            open_ = [h for h in shore if busy[h] < BOMBARD]
            if open_:
                fields = {h: self.sea_to(shore[h]) for h in open_}
                h = min(open_, key=lambda h: (fields[h].get(g.hex, INF), h))
                if fields[h].get(g.hex, INF) < INF:
                    busy[h] += 1
                    self.go(g, fields[h])
                    continue
            if not free:
                # No enemy port: stay by our own.
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
        invaders = self.threat(self.home_isle)
        if invaders:
            # The army is needed at home: everyone ashore.
            field = self.distance_field(invaders, blocked=set())
            for t in loading:
                for u in s.passengers(t):
                    self.land(u, field)
        elif target is not None and loading:
            full = [t for t in loading if len(s.passengers(t)) >= size]
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
                spots = self.landing_for(goal)
                moved = bool(spots) and self.go(t, self.sea_to(spots))
                isle = self.isle[goal]
                # Land at the chosen spot, or wherever it got stuck on the way.
                if t.hex in spots or (not moved and any(self.isle.get(h) == isle for h in bb.neighbors(t.hex))):
                    field = self.land_to(goal)
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
        invaders = self.threat(self.home_isle)
        loading = [t for t in self.lifts if self.mode[t.id] == "load" and port is not None
                   and bb.distance(t.hex, port.hex) == 1 and not invaders]
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
            elif role == "guard" and isle != self.home_isle and not foreign:
                sites = [x for x in self.mid_sites() if self.isle[x.hex] == isle]
                threat = self.threat(isle)
                mates = [g for g in self.units_with("guard") if self.isle_of(g) == isle]
                if threat and capture and len(mates) >= len(threat):
                    self.approach(u, threat)
                elif sites:
                    post = self.post_for(u, sites)
                    if u.hex != post.hex:
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
            elif isle == self.home_isle and invaders and role == "army":
                self.approach(u, invaders)
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
        home = s.city_at(self.home)
        mine = s.my_units()

        def count(code):
            return sum(1 for u in mine if u.type == code)

        def garrisoned(city):
            return "garrison %d" % city.id in self.roles.values()

        land = [u for u in mine if not self.unit_type(u).naval and not self.unit_type(u).carry]
        army = [u for u in land if self.roles.get(u.id, "army") == "army"]
        guards = list(self.roles.values()).count("guard")
        mid_threat = self.mid_threat()
        seen = sum(1 for r in self.warships.values() if s.round - r <= 30)
        galleys = min(MAX_GALLEYS, max(GALLEYS, seen + 1))
        invaded = bool(self.threat(self.home_isle))
        for city in s.my_cities():
            if city.built or s.enemy_at(city.hex):
                continue
            want = []  # (unit, bread to keep back), most wanted first
            if city is port:
                if invaded:
                    want.append(("I", 0))
                if self.ferry is None:
                    want.append(("T", 0))
                if self.midw is None and self.mid_sites() and not mid_threat and self.ferry is not None:
                    want.append(("W", 0))
                if self.hauler is None and self.home_site() is not None and self.ferry is not None:
                    want.append(("W", 0))
                if not garrisoned(city):
                    want.append(("I", 0))
                if self.ferry is not None:
                    if guards < self.want_guards:
                        want.append(("I", 0))
                    if count("G") < 2:
                        want.append(("G", 0))
                    if home.owner == self.player and not garrisoned(home):
                        want.append(("I", 0))
                    if count("G") < galleys:
                        want.append(("G", 0))
                    if len(self.lifts) < LIFTS and len(army) >= 2 * (len(self.lifts) + 1):
                        want.append(("T", 0))
                    if len(army) < ARMY:
                        want.append((ARMY_ORDER[len(land) % len(ARMY_ORDER)], KEEP))
            elif port is None or self.isle[city.hex] != self.home_isle:
                want.append(("I", KEEP))
            elif not garrisoned(city) or invaded:
                want.append(("I", 0))
            elif len(army) < ARMY and self.ferry is not None:
                want.append(("I", KEEP))
            for code, keep in want[:1]:
                if code not in types:
                    continue
                if types[code].naval:
                    if self.launch_hex(city) is None:
                        continue  # the water by the port is full
                elif s.friends_at(city.hex) >= self.rules.stack_limit:
                    continue
                if city.bread >= types[code].cost + keep and self.build(city, code):
                    mine = s.my_units()

    def report(self):
        s = self.state
        land = [u for u in s.my_units() if not self.unit_type(u).naval and not self.unit_type(u).carry]
        away = sum(1 for u in land if not u.carrier and self.isle[u.hex] != self.home_isle)
        self.debug_text("armada: %d bread, %d land units (%d abroad, %d guards of %d), %d galleys, %d transports, "
                        "lifts %s, %d enemies remembered, wheat %s" % (
                            sum(c.bread for c in s.my_cities()), len(land), away,
                            list(self.roles.values()).count("guard"), self.want_guards, len(self.galleys),
                            len(self.lifts) + (self.ferry is not None), sorted(self.mode.values()),
                            len(self.foes), sorted(self.est.values())))


if __name__ == "__main__":
    bb.run(ArmadaBot())
