"""Brigantine: the naval bot with an opening of its own, for the Islets map.

Naval takes its port and then saves up for its first ship, which comes out
around round 10. Brigantine plays the first rounds to a plan, by the way that
takes the fewest turns, and then goes on as Naval does:

- The port. On the first turn the wagon loads all the bread in the home
  city. It and the infantry then go to the neutral port on the home island,
  each by the way that takes it the fewest turns, and the infantry take the
  port.
- The first ship. The wagon unloads in the port, which builds a ship with
  that bread on the first turn it can: the turn after the port falls, since
  a city builds nothing on the turn it's taken. In half its games the ship is
  a galley, and in the other half a transport.
- The galley goes hunting, for a transport above all. It makes for the water
  by the middle island on the enemy's side, where a transport after the wheat
  would lie, and then for the port on the enemy's island, where the enemy's
  ships are built. It follows any ship it sees, and strikes as Naval's
  galleys do. The infantry and the wagon are Naval's from the turn the galley
  is built.
- The transport takes the infantry and the wagon. The infantry board on the
  turn it's built, the wagon on the next, and it sails that same turn, so
  the infantry can land the turn it arrives. In two of three such games it
  sails for the middle island: the infantry land by the sites, the wagon
  after them, and from there it's Naval's ferry, with the infantry as the
  guards. In the third it sails for the port on the enemy's island first. On
  the way in it stops where it can see into the port. If no one stands in
  it, the infantry land on it and take it at once, and the wagon lands
  behind them. If someone stands in it, or more strength is near it than
  the transport carries half again, or a warship is in sight, it turns for
  the middle island with the moves it has left.

The opening ends when its units are ashore, when its ship is lost, or after
round 16, whichever comes first, and everything is Naval's from then on.
Unless its home is on a small island of its own with a neutral port to take,
as on Islets and the islands map, it plays as Naval from the start.

Usage: python bots/brigantine_bot.py [--opening galley|island|port] [--no-bombard] [--no-haul]

--opening picks the opening instead of rolling for it, for tournaments that
measure one of them.
"""

import heapq
import os
import random
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import naval_bot as nb  # noqa: E402
import bitbath as bb  # noqa: E402

LINES = ("galley", "island", "port")
# The share of games in which the first ship is a galley. In the rest it's a
# transport.
GALLEY_SHARE = 0.5
# The share of the games with a transport in which it sails for the middle
# island. In the rest it tries the port on the enemy's island first.
ISLAND_SHARE = 2 / 3
# The opening is given up after this round, wherever it has got to.
OPENING_ROUNDS = 16
# A transport that could sail waits this many turns for units still ashore.
BOARD_WAIT = 2
# The hunter looks along the coast of the middle island within this many
# hexes' sail of its nearest point to the enemy's port.
BEAT_WIDTH = 2
FAR = 1 << 30


class BrigantineBot(nb.NavalBot):
    def __init__(self, line=None, haul=True, bombard=True):
        super().__init__(haul, bombard)
        self.line = line        # the opening: one of LINES, or None to roll for it

    def on_init(self):
        super().on_init()
        self.stage = None       # where the opening has got to: "port", "board", "sail", "home"; None once it's over
        self.held = set()       # the opening's units, which the rest of the bot leaves alone
        self.troops = None      # the infantry that take the port, by id
        self.cart = None        # the wagon that carries the home city's bread
        self.ship = None        # the opening's transport
        self.hunter = None      # the opening's galley, while it hunts
        self.beat = []          # where the hunter looks, in turn: lists of water hexes
        self.loaded = False
        self.waited = 0
        if not getattr(self, "naval", False) or not self.transport or not self.galley:
            return
        m, me = self.map, self.player
        homes = [(cid, h) for cid, h, owner in m.cities if owner == me]
        theirs = [(cid, h) for cid, h, owner in m.cities if owner not in (0, me)]
        if len(homes) != 1 or not theirs:
            return
        self.home_id, home = homes[0]
        land = self.land.get(home)
        their_land = self.land.get(theirs[0][1])
        if their_land == land or land not in self.home_island:
            return      # not a small island of its own, as on Islets: Naval's game from the start
        # The port: the neutral port on the home island that's the shortest walk.
        ports = [(self.city_field[cid].get(home, FAR), cid) for cid, h, owner in m.cities
                 if owner == 0 and self.land.get(h) == land and self.port_seas[cid]]
        if not ports or min(ports)[0] >= FAR:
            return
        self.port_id = min(ports)[1]
        seas = self.port_seas[self.port_id]
        # The middle island: the landmass with no city whose sites grow the
        # most, among those a ship from the port can reach.
        grown = {}
        for sid, h, output, _ in m.sites:
            isle = self.land.get(h)
            if any(self.land.get(c) == isle for _, c, _ in m.cities):
                continue
            if any(self.sea[w] in seas for w in self.landings(("site", sid))):
                grown[isle] = grown.get(isle, 0) + output
        self.isle = max(grown, key=lambda i: (grown[i], -i)) if grown else None
        # The enemy's port: the port on its island nearest its home, a neutral
        # one for choice, which it has yet to take.
        rivals = [(owner != 0, self.city_field[cid].get(theirs[0][1], FAR), cid) for cid, h, owner in m.cities
                  if owner != me and self.land.get(h) == their_land and self.port_seas[cid] & seas]
        self.rival_id = min(rivals)[2] if rivals else None
        line = self.line
        if line not in LINES:
            if random.random() < GALLEY_SHARE:
                line = "galley"
            else:
                line = "island" if random.random() < ISLAND_SHARE else "port"
        if line == "port" and self.rival_id is None:
            line = "island"
        if line == "island" and self.isle is None:
            line = "port" if self.rival_id is not None else "galley"
        self.line = line
        self.bound = line       # where the transport is bound: "island" or "port"
        self.stage = "port"
        # The hunter's beat: the middle island's coast nearest the enemy's
        # port, then the water off that port.
        off = []
        if self.rival_id is not None:
            rival = m.cities[self.rival_id][1]
            off = [n for n in bb.neighbors(rival) if self.sea.get(n) in seas]
        if self.isle is not None and off:
            field = self.sea_field(off)
            shore = {w: field[w] for w in self.coast if w in field and self.sea[w] in seas
                     and any(self.land.get(n) == self.isle for n in bb.neighbors(w))}
            if shore:
                low = min(shore.values())
                self.beat.append([w for w, d in shore.items() if d <= low + BEAT_WIDTH])
        if off:
            self.beat.append(off)

    # Keeping the opening's units out of the rest of the bot's hands.

    def capture_in_place(self):
        if self.stage is not None:
            try:
                self.opening()
            except bb.GameOver:
                raise
            except Exception:
                traceback.print_exc()   # a bug in the opening shouldn't cost the rest of the game
                self.finish()
            self.refresh()
        super().capture_in_place()

    def feed_army(self):
        # The supply bot clears its list of busy units just before this, and
        # every later plan of the turn leaves the units on it alone.
        super().feed_army()
        self.busy |= self.held

    def supply_wagons(self):
        return [w for w in super().supply_wagons() if w.id not in self.held]

    def run_transports(self):
        # The opening's transport isn't a convoy's yet: Naval would dock it
        # and wait for a crew.
        units = self.state.units
        hidden = {i: units.pop(i) for i in list(self.held) if i in units and self.types[units[i].type].transport}
        try:
            super().run_transports()
        finally:
            units.update(hidden)

    # The opening.

    def opening(self):
        s, me = self.state, self.player
        if self.troops is None:
            land = [u for u in s.my_units() if not self.types[u.type].naval]
            self.troops = sorted(u.id for u in land if self.types[u.type].capture)
            carts = sorted(u.id for u in land if self.types[u.type].carry)
            self.cart = carts[0] if carts else None
            self.held = set(self.troops) | set(carts[:1])
        troops = [s.units[i] for i in self.troops if i in s.units]
        cart = s.units.get(self.cart)
        port, home = s.cities[self.port_id], s.cities[self.home_id]
        ship = s.units.get(self.ship)
        if s.round > OPENING_ROUNDS or home.owner != me or (self.ship is not None and ship is None):
            return self.finish()
        if self.stage == "port":
            if not troops or port.owner not in (0, me):
                return self.finish()
            self.to_port(troops, cart, port, home)
            ship = s.units.get(self.ship)
        if self.stage == "board":
            aboard = self.embark(ship, troops, cart)
            if not ship.fresh:
                if aboard or (self.waited >= BOARD_WAIT and s.passengers(ship)):
                    self.stage = "sail"
                    for u in troops + [cart]:
                        if u is not None and u.carrier != ship.id:
                            self.held.discard(u.id)     # left behind, for Naval
                else:
                    self.waited += 1
        if self.stage == "sail":
            if self.bound == "port":
                self.raid(ship)
            else:
                self.take_island(ship)
        if self.stage == "home":
            self.come_home(ship)
        if self.stage is not None:
            self.debug_text("opening: %s, stage %s%s" % (
                self.line, self.stage, ", bound for the island" if self.bound != self.line else ""))

    def finish(self):
        """Ends the opening: everything is Naval's from here. A transport still
        out goes to Naval as one that has sailed, which lands its units at
        the best place it finds or brings them home."""
        t = self.state.units.get(self.ship)
        if t is not None:
            phase = "sail" if self.state.passengers(t) else "return"
            self.voyages[t.id] = {"phase": phase, "port": self.port_id, "waited": 0}
        if self.stage is not None:
            self.debug_text("opening: %s, over in round %d" % (self.line, self.state.round))
        self.stage, self.held = None, set()

    def fastest(self, u, goal):
        """The move, as dirs, that leaves u the fewest turns from goal, counting
        the move points a turn wastes when the next hex costs more than it
        has left. "" to stay where it is, and None if there's no way."""
        reach = self.reach(u, block_cities=False)
        if goal in reach:
            return reach[goal]
        move = self.types[u.type].move
        near = self.distance_field([goal], blocked=set())
        # From each hex it could end this turn on, a search by (turns begun,
        # move points spent in the last one); ties go to the start nearest
        # the goal.
        best, heap = {}, []
        for h in reach:
            best[h] = (0, move, near.get(h, FAR))
            heap.append((0, move, near.get(h, FAR), h, h))
        heapq.heapify(heap)
        while heap:
            turns, used, tie, h, start = heapq.heappop(heap)
            if (turns, used, tie) > best[h]:
                continue
            if h == goal:
                return reach[start]
            for _, n, cost in self.nbrs[h]:
                if n in self.enemy_hex or cost > move:
                    continue
                label = (turns, used + cost, tie) if used + cost <= move else (turns + 1, cost, tie)
                if n not in best or label < best[n]:
                    best[n] = label
                    heapq.heappush(heap, label + (n, start))
        return None

    def hurry(self, u, goal):
        dirs = self.fastest(u, goal)
        if dirs:
            self.go(u, dirs)

    def to_port(self, troops, cart, port, home):
        """The wagon takes the home city's bread, it and the infantry go to the
        port, the infantry take it, and it builds the first ship."""
        s, me = self.state, self.player
        if cart is not None and not self.loaded and cart.hex == home.hex:
            self.loaded = True
            if home.bread > 0:
                self.load(cart)     # all of it: the port builds the first ship with it
        for u in sorted(troops, key=lambda u: u.id):
            if u.hex != port.hex:
                self.hurry(u, port.hex)
            if port.owner != me and self.can_capture(u):
                self.capture(u)
        if cart is not None:
            if cart.hex != port.hex:
                self.hurry(cart, port.hex)
            if cart.hex == port.hex and port.owner == me and (cart.bread or cart.wheat):
                self.unload(cart)
        code = self.galley if self.line == "galley" else self.transport
        cost = self.types[code].cost
        if port.owner != me:
            return
        if cart is None and port.bread < cost:
            return self.finish()    # no bread is coming
        if port.built or port.bread < cost or self.launch_hex(port) is None:
            return
        reply = self.build(port, code)
        if not reply:
            return
        self.sync()
        built = int(reply.values[0])
        if code == self.galley:
            self.hunter = built
            return self.finish()
        self.ship, self.stage, self.waited = built, "board", 0
        self.held.add(built)

    def embark(self, t, troops, cart):
        """Puts aboard t whoever can get there this turn, and walks the others
        up to it. Returns whether everyone is aboard."""
        s = self.state
        crew = troops + ([cart] if cart is not None else [])
        for u in crew:
            if u.carrier or u.fresh or u.acted or u.mp <= 0:
                continue
            reach = self.reachable(u)
            first = self.transport_with_room(t.hex)
            if t.hex in reach and first is not None and first.id == t.id:
                self.go(u, reach[t.hex])
                continue
            goals = [n for n in bb.neighbors(t.hex) if n in self.defense]
            if goals and u.hex not in goals:
                self.walk(u, self.distance_field(goals))
        return bool(crew) and all(u.id in s.units and u.carrier == t.id for u in crew)

    def take_island(self, t):
        """Sails t to the middle island and lands everyone by its sites, the
        infantry first. Once they're ashore, t is Naval's ferry for the
        island, the wagon its hauler, and the infantry the guards."""
        s = self.state
        isle = self.isle
        if isle is None:
            return self.turn_home(t)
        spots = self.isle_landings(isle)
        goals = [w for w in spots if self.sea[w] == self.sea.get(t.hex)]
        aboard = s.passengers(t)
        ours = sum(self.worth(u) for u in s.my_units()
                   if not self.types[u.type].carry and not self.types[u.type].naval
                   and (u.carrier == t.id or (not u.carrier and self.land.get(u.hex) == isle)))
        foes = sum(self.worth(e) for e in s.enemy_units()
                   if self.land.get(e.hex) == isle and not e.carrier and not self.types[e.type].naval)
        if not goals or not aboard or not ours or foes > nb.ODDS * ours + 100:
            return self.turn_home(t)
        if t.hex not in spots:
            self.sail(t, self.sea_field(goals), self.danger())
            if t.id not in s.units:
                return self.finish()
        if t.hex not in spots:
            return
        sites = [x for x in s.sites if self.land.get(x.hex) == isle]
        for u in sorted(s.passengers(t), key=lambda u: (-u.hp, u.id)):
            if self.types[u.type].carry or self.out_of_time():
                continue
            options = {h: p for h, p in self.reachable(u).items() if self.land.get(h) == isle}
            if not options:
                continue
            h = min(options, key=lambda h: (min(self.site_field[x.id].get(h, FAR) for x in sites),
                                            self.threat.get(h, 0), h))
            self.go(u, options[h])
            if not u.carrier:
                self.tally["landed"] += 1
                self.held.discard(u.id)     # ashore, it's a guard: Naval's to fight and to post
        self.claims = {}
        for w in s.passengers(t):
            if self.types[w.type].carry and isle in self.haul_isles():
                self.hauler_land(w, isle)
        if any(not self.types[u.type].carry for u in s.passengers(t)):
            return      # someone has still to land
        wagons = [w for w in s.my_units() if w.id == self.cart and (w.carrier == t.id or self.land.get(w.hex) == isle)]
        if any(w.carrier for w in wagons):
            return      # the wagon boarded this turn, or has no one ashore yet to haul for
        self.ferries[t.id] = {"isle": isle, "port": self.port_id, "phase": "wait", "waited": 0}
        self.voyages.pop(t.id, None)
        for w in wagons:
            self.haulers[w.id] = isle
        self.debug_text("opening: %s, ashore on the island in round %d" % (self.line, s.round))
        self.stage, self.held = None, set()

    def forlorn(self, city, troops):
        """Whether landing on the enemy's port isn't worth trying: someone
        stands in it, more strength is near it than half again what we'd
        land, or a warship is in sight."""
        s = self.state
        strength = sum(self.worth(u) for u in troops)
        if city.owner == self.player or city.hex in self.enemy_hex:
            return True
        if self.threat.get(city.hex, 0) > nb.ODDS * strength:
            return True
        return any(self.types[e.type].naval and any(self.rules.damage[e.type].values()) for e in s.enemy_units())

    def raid(self, t):
        """Sails t to the port on the enemy's island, stopping where it sees
        into the port, and lands the infantry on it if that's worth trying.
        If not, it turns for the middle island with the moves it has left."""
        s, me = self.state, self.player
        city = s.cities[self.rival_id]
        sea = self.sea.get(t.hex)
        spots = [n for n in bb.neighbors(city.hex) if self.sea.get(n) == sea]

        def troops():
            return [u for u in s.passengers(t) if self.types[u.type].capture]

        def to_island():
            self.bound = "island"
            self.take_island(t)

        if not spots or not troops() or self.forlorn(city, troops()):
            return to_island()
        if t.hex not in spots:
            field = self.sea_field(spots)
            if bb.distance(t.hex, city.hex) > 2:
                # First to where it sees into the port, on its way in.
                danger = self.danger()
                looks = {h: p for h, p in self.reachable(t).items()
                         if bb.distance(h, city.hex) == 2 and h in field}
                if looks:
                    h = min(looks, key=lambda h: (field[h], h in danger, len(looks[h]), h))
                    self.go(t, looks[h])
                    if t.id not in s.units:
                        return self.finish()
                    if self.forlorn(city, troops()):
                        return to_island()
            self.sail(t, field, self.danger())
            if t.id not in s.units:
                return self.finish()
            if self.forlorn(city, troops()):
                return to_island()
        if t.hex not in spots:
            return
        landed = False
        for u in sorted(troops(), key=lambda u: (-u.hp, u.id)):
            reach = self.reachable(u)
            if city.hex not in reach:
                continue
            self.go(u, reach[city.hex])
            if u.carrier:
                continue    # someone hidden stood in the way
            landed = True
            self.tally["landed"] += 1
            self.held.discard(u.id)
            if self.can_capture(u):
                self.capture(u)
        if not landed:
            return
        # The infantry are Naval's army now.
        self.debug_text("opening: port, landed on the enemy's port in round %d%s" % (
            s.round, ", and took it" if city.owner == me else ""))
        if city.owner == me:
            # The wagon goes ashore behind them, to haul for the port and feed
            # them, and the transport is one of Naval's, docked there.
            for w in s.passengers(t):
                reach = self.reachable(w)
                if city.hex in reach:
                    self.go(w, reach[city.hex])
            if not s.passengers(t):
                self.voyages[t.id] = {"phase": "gather", "port": city.id, "waited": 0}
                self.stage, self.held = None, set()
                return
        self.turn_home(t)

    def turn_home(self, t):
        self.stage = "home"     # opening() goes on to come_home() this same turn

    def come_home(self, t):
        """Brings t back to the port and puts whoever is aboard ashore there."""
        s = self.state
        v = {"port": self.port_id}
        if not t.fresh and self.dock(t, v):
            self.land_home(t)
            if not s.passengers(t):
                self.voyages[t.id] = {"phase": "gather", "port": v["port"], "waited": 0}
                self.debug_text("opening: %s, home in round %d" % (self.line, s.round))
                self.stage, self.held = None, set()

    # The galley's hunt.

    def patrol(self, g):
        if g.id != self.hunter or not self.hunt(g):
            super().patrol(g)

    def hunt(self, g):
        """The hunter's move when it has nothing to strike: after a ship in
        sight, a transport for choice, else along its beat. Returns whether
        it's still hunting."""
        s = self.state
        sea = self.sea.get(g.hex)
        ships = [e for e in s.enemy_units() if self.types[e.type].naval and self.sea.get(e.hex) == sea]
        if ships:
            e = min(ships, key=lambda e: (not self.types[e.type].transport, bb.distance(e.hex, g.hex), e.id))
            goals = [n for n in bb.neighbors(e.hex) if n in self.sea]
            if goals and g.hex not in goals:
                self.sail(g, self.distance_field(goals, naval=True), self.shore_danger())
            return True
        while self.beat:
            goals = [h for h in self.beat[0] if self.sea.get(h) == sea]
            if (goals and min(bb.distance(g.hex, h) for h in goals) > 1
                    and self.sail(g, self.sea_field(goals), self.shore_danger())):
                return True
            self.beat.pop(0)    # it's there and has seen what lies there, or it can't get nearer
        self.hunter = None
        return False


if __name__ == "__main__":
    args = sys.argv[1:]
    line = None
    if "--opening" in args:
        line = args[args.index("--opening") + 1] if args.index("--opening") + 1 < len(args) else ""
        if line not in LINES:
            sys.exit("--opening takes one of: %s" % ", ".join(LINES))
    bb.run(BrigantineBot(line, haul="--no-haul" not in args, bombard="--no-bombard" not in args))
