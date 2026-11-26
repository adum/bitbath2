"""Breakwater, an amphibious assault and supply bot for Islets."""

from collections import Counter, defaultdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kits" / "python"))
import bitbath as bb

FAR = 10_000


class Breakwater(bb.Bot):
    def on_init(self):
        self.fields = {}
        self.archer_ranges = {}
        self.home = next(h for _, h, p in self.map.cities if p == self.player)
        self.enemy_home = next(h for _, h, p in self.map.cities if p and p != self.player)
        ports = [h for _, h, _ in self.map.cities if self.map.is_port(h)]
        self.port = next(h for h in ports if self.home in self.field(h))
        self.enemy_port = next(h for h in ports if self.enemy_home in self.field(h))
        self.home_site = next(h for _, h, _, _ in self.map.sites if h in self.field(self.home))
        self.middle = [
            h
            for _, h, _, _ in self.map.sites
            if h not in self.field(self.home) and h not in self.field(self.enemy_home)
        ]
        self.region = {}
        self.coast = defaultdict(list)
        for root in (self.home, self.enemy_home, self.middle[0]):
            for h in self.field(root):
                self.region[h] = root
                for sea in bb.neighbors(h):
                    if self.map.navigable(sea):
                        self.coast[root].append((sea, h))
        self.harvester = self.ferry = None
        self.sailing = set()
        self.last_pile = {}
        self.launched_transport = False
        self.launched_galley = False
        self.last_galleys = None
        self.saw_rival_shipping = False

    def field(self, goal, naval=False):
        key = goal, naval
        if key not in self.fields:
            self.fields[key] = self.distance_field([goal], blocked=(), naval=naval)
        return self.fields[key]

    def mine(self, code):
        return [u for u in self.state.my_units() if u.type == code]

    def approach(self, unit, goal, naval=False, *, caution=0):
        if not unit.mp or not self.can_act(unit) or unit.hex == goal:
            return
        field = self.field(goal, naval)
        options = self.reachable(unit)
        options[unit.hex] = ""

        def rank(h):
            return (field.get(h, FAR) + caution * self.danger[h], len(options[h]), h)

        dest = min(options, key=rank)
        if options[dest] and dest in field:
            self.move(unit, options[dest])

    def note_rival_shipping(self):
        if not self.saw_rival_shipping:
            self.saw_rival_shipping = any(
                e.type == "T" or (e.type == "W" and self.region.get(e.hex) == self.middle[0])
                for e in self.state.enemy_units()
            )

    def threat(self):
        """Estimate the attacks a ship would face before it could move again."""
        self.danger = Counter()
        self.note_rival_shipping()
        galleys = [(e.hex, e.hp) for e in self.state.enemy_units() if e.type == "G"]
        if galleys:
            self.last_galleys = self.state.round, galleys
        for e in self.state.enemy_units():
            if e.carrier:
                continue
            if e.type == "G":
                for h, cost in self.field(e.hex, True).items():
                    if cost <= self.unit_type(e).move + 1:
                        self.danger[h] += e.hp / 100
            elif e.type == "A":
                for h in self.archer_coverage(e.hex):
                    self.danger[h] += 0.65 * e.hp / 100

    def archer_coverage(self, origin):
        if origin not in self.archer_ranges:
            archer = self.rules.unit_types["A"]
            positions = (
                [origin]
                if archer.no_move_attack
                else [
                    h
                    for h in bb.disk(origin, archer.move)
                    if self.map.passable(h) and self.field(h).get(origin, FAR) <= archer.move
                ]
            )
            self.archer_ranges[origin] = {
                sea
                for h in positions
                for r in range(archer.range_min, archer.range_max + 1)
                for sea in bb.ring(h, r)
                if self.map.navigable(sea)
            }
        return self.archer_ranges[origin]

    def route(self, ship, goal, *, freight=False):
        """Pick a berth and beach together, accounting for both legs of a trip."""
        ground = self.field(goal)
        sea = self.field(ship.hex, True)
        occupied = {e.hex for e in self.state.enemy_units() if not e.carrier}
        full = Counter(u.hex for u in self.state.my_units() if not u.carrier)
        best = None
        for berth, beach in self.coast[self.region[goal]]:
            if berth not in sea or berth in occupied or beach in occupied:
                continue
            if full[beach] >= self.rules.stack_limit:
                continue
            cost = sea[berth] + (2.2 if freight else 1.55) * ground[beach]
            cost += 3.0 * self.danger[berth]
            if beach == goal:
                cost -= 7 if freight else 4
            choice = cost, sea[berth], berth, beach
            if best is None or choice < best:
                best = choice
        return best[2:] if best else None

    def board(self, unit, ship):
        if unit.carrier or not self.can_act(unit) or not unit.mp:
            return False
        if self.transport_with_room(ship.hex) is not ship:
            return False
        path = self.reachable(unit).get(ship.hex)
        if path:
            self.move(unit, path)
        return unit.carrier == ship.id

    def sail(self, ship, goal, passengers, *, freight=False):
        landing = self.route(ship, goal, freight=freight)
        if not landing:
            return
        berth, beach = landing
        self.approach(ship, berth, True, caution=1.5)
        for u in passengers:
            if not u.mp or u.carrier != ship.id:
                continue
            ground = self.field(goal)
            choices = [(ground[h], h, path) for h, path in self.reachable(u).items() if h in ground]
            if choices:
                cost, _, path = min(choices)
                if cost <= ground[beach] + (0 if freight else 1):
                    self.move(u, path)
                    if self.can_capture(u):
                        self.capture(u)

    def assign_ferry(self):
        wagons = self.mine("W")
        ships = self.mine("T")
        if self.harvester not in {u.id for u in wagons}:
            # Recover a wagon already across the water before sending another.
            wagon = min(wagons, key=lambda u: (self.region.get(u.hex) == self.home, u.id), default=None)
            self.harvester = wagon.id if wagon else None
        if self.ferry not in {u.id for u in ships}:
            ship = min(ships, key=lambda t: (len(self.state.passengers(t)), t.id), default=None)
            self.ferry = ship.id if ship else None

    def pile(self, h):
        site = self.state.site_at(h)
        turn, last = self.last_pile.get(h, (0, 0))
        return min(site.cap, last + site.output * (self.state.round - turn))

    def farming(self, wagon, destination):
        if wagon.wheat or wagon.bread:
            self.approach(wagon, destination)
            if wagon.hex == destination and self.state.city_at(destination).owner == self.player:
                self.unload(wagon)
        else:
            self.approach(wagon, self.home_site)
            if wagon.hex == self.home_site and self.state.site_at(wagon.hex).wheat:
                self.load(wagon)

    def top_up_wheat(self, wagon):
        """Take a second harvest when it is a short stop on the return voyage."""
        room = self.rules.constants["cargo_max"] - wagon.wheat - wagon.bread
        paths = self.reachable(wagon)
        paths[wagon.hex] = ""
        choices = [
            (min(room, self.state.site_at(h).wheat), h, paths[h])
            for h in self.middle
            if h in paths and not self.state.enemy_at(h)
        ]
        if not choices:
            return
        amount, site, path = max(choices)
        if amount >= max(self.rules.unit_types["I"].cost, wagon.wheat // 3):
            if path:
                self.move(wagon, path)
            if wagon.hex == site:
                self.load(wagon)
                self.last_pile[site] = self.state.round, self.state.site_at(site).wheat

    def land_wheat(self, ship, wagon, paths):
        """Get the load ashore now if the ferry can reach a friendly beach."""
        if not wagon.mp or not self.can_act(wagon):
            return False
        ground = self.field(self.port)
        landings = []
        for berth, beach in self.coast[self.home]:
            if berth not in paths or self.state.enemy_at(berth) or self.state.enemy_at(beach):
                continue
            if self.state.friends_at(beach) >= self.rules.stack_limit:
                continue
            cost = ground[beach] + 0.07 * len(paths[berth]) - 8 * (beach == self.port)
            landings.append((cost, berth, beach))
        if not landings:
            return False
        _, berth, beach = min(landings)
        if paths[berth]:
            self.move(ship, paths[berth])
        path = self.reachable(wagon).get(beach)
        if path:
            self.move(wagon, path)
        if wagon.carrier:
            return False
        if wagon.hex == self.port:
            self.unload(wagon)
        return True

    def protect_ferry(self, ship, wagon, *, pickup=False):
        """Choose a pickup berth or a return route under a visible or recently sighted galley."""
        if not self.last_galleys or self.state.round > 24:
            return False
        known = [(e.hex, e.hp) for e in self.state.enemy_units() if e.type == "G"]
        if not known and self.state.round - self.last_galleys[0] <= 2:
            known = self.last_galleys[1]
        if not known:
            return False
        paths = self.reachable(ship)
        paths[ship.hex] = ""
        sailing = self.field(ship.hex, True)
        home_berths = (h for h in bb.neighbors(self.port) if self.map.navigable(h))
        home_berth = min(home_berths, key=lambda h: sailing.get(h, FAR))
        to_port = self.field(home_berth, True)
        threats = [(self.field(h, True), hp) for h, hp in known]
        galley = self.rules.unit_types["G"]
        reach = galley.move + galley.range_max
        damage = self.rules.damage["G"]["T"]

        def risk(h):
            nearest = min(field.get(h, FAR) for field, _ in threats)
            incoming = sum(hp * damage / 100 for field, hp in threats if field.get(h, FAR) <= reach)
            return 100 * (incoming >= ship.hp) + 0.1 * incoming + 0.8 * max(0, 5 - nearest)

        if pickup:
            ground = self.field(wagon.hex)
            berths = []
            for berth, beach in self.coast[self.middle[0]]:
                if berth not in paths or self.state.enemy_at(berth) or self.state.enemy_at(beach):
                    continue
                # The final step onto the ferry also costs the wagon one movement point.
                if ground.get(beach, FAR) + 1 <= self.unit_type(wagon).move:
                    cost = risk(berth) + to_port.get(berth, FAR) + 0.12 * ground[beach]
                    berths.append((cost, len(paths[berth]), berth))
            if not berths:
                return False
            _, _, stop = min(berths)
        else:
            origin = ship.hex
            if self.land_wheat(ship, wagon, paths):
                return True
            if ship.hex != origin:
                paths = self.reachable(ship)
                paths[ship.hex] = ""
            stop = min(paths, key=lambda h: (to_port.get(h, FAR) + risk(h), len(paths[h]), h))
        if paths[stop]:
            self.move(ship, paths[stop])
        return True

    def freight(self, ship, wagon):
        port = self.state.city_at(self.port)
        if port.owner != self.player:
            return
        # On an outbound leg prefer a full, accessible site. Wheat left in fog
        # is estimated from the last visible stock and its public growth rate.
        sites = [h for h in self.middle if not self.state.enemy_at(h)] or self.middle
        site = max(sites, key=lambda h: self.pile(h) - 15 * bb.distance(wagon.hex, h))
        region = self.region.get(wagon.hex)
        if not wagon.carrier:
            if region == self.home and (wagon.wheat or wagon.bread):
                self.farming(wagon, self.port)
            if region == self.region[site] and wagon.wheat and wagon.mp:
                self.top_up_wheat(wagon)
            if region == self.region[site] and not wagon.wheat:
                self.approach(wagon, site)
                if wagon.hex in self.middle and self.state.site_at(wagon.hex).wheat:
                    self.load(wagon)
                    self.last_pile[wagon.hex] = self.state.round, self.state.site_at(wagon.hex).wheat
            if region == self.home:
                pickup = self.port
            else:
                pickup = wagon.hex
            if region == self.middle[0] and wagon.wheat:
                self.protect_ferry(ship, wagon, pickup=True)
            landing = self.route(ship, pickup, freight=True)
            if landing and not self.board(wagon, ship):
                berth, beach = landing
                self.approach(ship, berth, True, caution=1)
                if not self.board(wagon, ship) and (region == self.home or wagon.wheat):
                    self.approach(wagon, beach)
                    self.board(wagon, ship)
        if wagon.carrier != ship.id:
            return
        goal = self.port if wagon.wheat or wagon.bread else site
        if goal == site:
            self.board_escort(ship)
        if goal == self.port and self.protect_ferry(ship, wagon):
            return
        self.sail(ship, goal, self.state.passengers(ship), freight=True)
        if wagon.carrier:
            return
        if wagon.hex == self.port:
            self.unload(wagon)
        elif wagon.hex in self.middle and self.state.site_at(wagon.hex).wheat:
            self.load(wagon)
            self.last_pile[wagon.hex] = self.state.round, self.state.site_at(wagon.hex).wheat
        if self.region.get(wagon.hex) == self.middle[0]:
            self.protect_ferry(ship, wagon, pickup=True)

    def board_escort(self, ship):
        # A stranded enemy infantryman can still kill an unguarded wagon.
        # Count the island garrison before reserving seats on the next crossing.
        for code, wanted in (("I", 2), ("S", 1)):
            units = self.mine(code)
            guarding = sum(u.carrier == ship.id or self.region.get(u.hex) == self.middle[0] for u in units)
            for u in units:
                if guarding >= wanted:
                    break
                if self.region.get(u.hex) == self.home and self.board(u, ship):
                    guarding += 1

    def deliver_starting_bread(self, wagon, port):
        """Keep the launch money out of the troops' ration supply until the port can build."""
        paths = self.reachable(wagon)
        if not paths:
            return False
        if self.port in paths and port.owner == self.player and not port.built:
            self.move(wagon, paths[self.port])
            if wagon.hex == self.port:
                self.unload(wagon)
            return True

        paths[wagon.hex] = ""
        distance = self.field(self.port)
        stride = self.unit_type(wagon).move
        troops = [u for u in self.state.my_units() if u is not wagon]
        rules = self.rules.constants

        def rank(h):
            cost = distance.get(h, FAR)
            turns = max(1, (cost + stride - 1) // stride)
            refills = sum(
                rules["ration_max"] - u.rations + rules["eat_base"] + u.moved * rules["eat_move"]
                for u in troops
                if bb.distance(h, u.hex) <= 1
            )
            return turns, refills, bb.distance(h, self.port) <= 1, cost, len(paths[h]), h

        stop = min(paths, key=rank)
        if paths[stop]:
            self.move(wagon, paths[stop])
        return True

    def supply(self):
        self.assign_ferry()
        wagon = self.state.units.get(self.harvester)
        ship = self.state.units.get(self.ferry)
        port = self.state.city_at(self.port)
        home = self.state.city_at(self.home)
        if wagon and self.state.round == 1 and wagon.hex == self.home:
            # Pool the opening bread at the port to launch the galley and its ferry.
            if home.bread:
                self.load(wagon, home.bread)
        if wagon and wagon.bread and not self.launched_transport and wagon.hex != self.port:
            if self.deliver_starting_bread(wagon, port):
                return
        for w in self.mine("W"):
            if w is wagon and ship and port.owner == self.player:
                self.freight(ship, w)
                continue
            if w.carrier or self.region.get(w.hex) != self.home:
                continue
            if port.owner != self.player and w.bread:
                self.approach(w, self.port)
                continue
            destination = self.port if port.owner == self.player else self.home
            if home.owner == self.player and ship and home.bread < 150 and port.bread > 250:
                destination = self.home
            self.farming(w, destination)

    def capture_close(self):
        for c in self.state.cities:
            if c.owner == self.player or self.state.enemy_at(c.hex):
                continue
            for u in sorted(self.mine("I"), key=lambda u: (u.hex != c.hex, -u.hp, u.id)):
                if c.owner == self.player:
                    break
                if not self.can_act(u) or u.carrier:
                    continue
                if u.hex != c.hex and bb.distance(u.hex, c.hex) <= u.mp:
                    path = self.reachable(u).get(c.hex)
                    if path:
                        self.move(u, path)
                if u.hex == c.hex and self.can_capture(u):
                    self.capture(u)

    def fighting(self):
        """Choose a focus target accounting for armor, return fire and cargo."""
        skipped = set()
        while self.time_left() > 150:
            enemies = [e for e in self.state.enemy_units() if not e.carrier]
            groups = defaultdict(list)
            for u in self.state.my_units():
                if u.type not in ("I", "S", "G") or not self.can_act(u) or u.carrier or u.id in skipped:
                    continue
                nearby = [
                    e
                    for e in enemies
                    if self.rules.damage[u.type][e.type] and bb.distance(u.hex, e.hex) <= u.mp + 1
                ]
                if not nearby:
                    continue
                moves = self.reachable(u)
                moves[u.hex] = ""
                for e in nearby:
                    shots = [
                        h
                        for h in bb.ring(e.hex, 1)
                        if h in moves and self.map.terrain(h).cost_for(self.unit_type(u).naval)
                    ]
                    if not shots:
                        continue

                    def position(h):
                        defense = self.map.terrain(h).defense
                        if u.type == "G":
                            defense += 28 * (bb.distance(h, self.port) == 1) - 16 * self.danger[h]
                        return defense + 4 * (h == u.hex) - 0.1 * len(moves[h])

                    h = max(shots, key=position)
                    hit = self.damage(u.type, u.hp, e.type, e.hex, e.fort)
                    groups[e.id].append((hit, u.id, h, moves[h]))
            best = None
            for eid, group in groups.items():
                e = self.state.units[eid]
                weight = {"T": 2.0, "G": 2.7, "I": 1.2, "S": 0.65, "W": 2.1}.get(e.type, 1.2)
                if e.type == "T":
                    weight += 1.55 * len(self.state.passengers(e))
                elif e.type == "W":
                    weight += (e.wheat + e.bread) / 450
                if e.hex in (self.port, self.home, self.enemy_port, self.enemy_home):
                    weight += 0.45
                hp, gain = e.hp, 0
                group.sort(key=lambda item: (-item[0], item[1]))
                for n, (hit, uid, h, _) in enumerate(group, 1):
                    u = self.state.units[uid]
                    gain += min(hp, hit) * weight
                    hp -= hit
                    if hp <= 0:
                        gain += 46 * weight
                    elif self.unit_type(e).in_range(bb.distance(e.hex, h)):
                        back = self.damage(e.type, hp, u.type, h, u.fort if h == u.hex else 0)
                        gain -= min(u.hp, back) * self.unit_type(u).cost / 100
                    if u.type == "G" and self.danger[h] > 0 and e.type not in ("T", "G"):
                        gain -= 45 * self.danger[h] * (1 + (u.hp < 62))
                    score = gain / n
                    if best is None or score > best[0]:
                        best = score, group[0], e
                    if hp <= 0:
                        break
            if best is None or best[0] <= 0:
                return
            _, (_, uid, h, path), e = best
            u = self.state.units[uid]
            if path and not self.move(u, path):
                skipped.add(uid)
            elif u.hex == h and not self.attack(u, e):
                skipped.add(uid)

    def target_city(self):
        cities = [
            c for c in self.state.cities if c.owner != self.player and self.region[c.hex] == self.enemy_home
        ]

        def difficulty(c):
            defenders = sum(
                e.hp / 100 / (1 + bb.distance(c.hex, e.hex))
                for e in self.state.enemy_units()
                if not e.carrier and not self.unit_type(e).naval
            )
            return 2.5 * defenders + 2.5 * self.state.enemy_at(c.hex) - 2 * (c.hex == self.enemy_port)

        return min(cities, key=difficulty).hex if cities else self.enemy_home

    def assault_beaches(self, ship, cargo, cities):
        """Score each berth by its best beach, nearby defenders, cover and the walk to a city."""
        enemies = [e for e in self.state.enemy_units() if not e.carrier]
        occupied = {e.hex for e in enemies}
        visible = {h for u in [ship, *cargo] for h in self.unit_sight(u)}
        defenders = [
            e
            for e in enemies
            if e.type in ("I", "S", "A", "P", "K") and self.region.get(e.hex) == self.enemy_home
        ]
        city_cost = {}
        for city in cities:
            resistance = sum(e.hp / 100 / (1 + bb.distance(e.hex, city)) for e in defenders)
            city_cost[city] = resistance + 2 * (city in occupied) + 1.5 * (city == self.enemy_home)

        beaches = {}
        for berth, beach in self.coast[self.enemy_home]:
            if (
                berth in occupied
                or beach in occupied
                or self.state.friends_at(beach) >= self.rules.stack_limit
            ):
                continue
            cost = min(
                self.field(city)[beach] / 2.5 + city_cost[city] - 0.6 * (beach == city) for city in cities
            )
            cost -= 0.055 * self.map.terrain(beach).defense
            if beach not in visible:
                cost += 3 * max(0, 3 - bb.distance(beach, self.enemy_port))
            choice = cost, beach
            if berth not in beaches or choice < beaches[berth]:
                beaches[berth] = choice
        return beaches

    def land_under_fire(self, ship, cargo):
        """Unload at a reachable beach, or keep the loaded transport outside a lethal galley volley."""
        threats = [(e.hex, e.hp) for e in self.state.enemy_units() if e.type == "G"]
        if not threats and self.last_galleys and self.state.round - self.last_galleys[0] <= 2:
            threats = self.last_galleys[1]
        if not threats:
            return False
        galleys = [(self.field(h, True), hp) for h, hp in threats]
        if min(field.get(ship.hex, FAR) for field, _ in galleys) > 13:
            return False
        cities = [
            c.hex
            for c in self.state.cities
            if c.owner != self.player and self.region[c.hex] == self.enemy_home
        ]
        if not cities:
            return False
        beaches = self.assault_beaches(ship, cargo, cities)
        if not beaches:
            return False

        paths = self.reachable(ship)
        paths[ship.hex] = ""
        available = [(cost, len(paths[h]), h, beach) for h, (cost, beach) in beaches.items() if h in paths]
        if available and any(u.mp and self.can_act(u) for u in cargo):
            _, _, berth, beach = min(available)
            if paths[berth]:
                self.move(ship, paths[berth])
            for u in cargo:
                land = self.reachable(u)
                if beach in land:
                    self.move(u, land[beach])
                else:
                    # Movement can reveal a garrison on the chosen beach.
                    alternatives = [
                        (min(self.field(city)[h] for city in cities), h)
                        for h in land
                        if self.region.get(h) == self.enemy_home
                    ]
                    if alternatives:
                        self.move(u, land[min(alternatives)[1]])
                if self.can_capture(u):
                    self.capture(u)
            return True

        speed = self.unit_type(ship).move
        galley = self.rules.unit_types["G"]
        strike_range = galley.move + galley.range_max
        damage = self.rules.damage["G"][ship.type]
        approaches = [
            (self.field(berth, True), 0.35 * min(4, max(0, cost))) for berth, (cost, _) in beaches.items()
        ]

        def exposure(h):
            incoming = sum(damage * hp / 100 for field, hp in galleys if field.get(h, FAR) <= strike_range)
            coast = min(field.get(h, FAR) / speed + cost for field, cost in approaches)
            clearance = min(field.get(h, FAR) for field, _ in galleys)
            return (
                coast + 50 * (incoming >= ship.hp) + incoming / 35 - 0.04 * min(clearance, 8),
                len(paths[h]),
                h,
            )

        stop = min(paths, key=exposure)
        if paths[stop]:
            self.move(ship, paths[stop])
        return True

    def invasions(self):
        defenders = [
            e for e in self.state.enemy_units() if not e.carrier and self.region.get(e.hex) == self.home
        ]
        for ship in self.mine("T"):
            if ship.id == self.ferry:
                continue
            cargo = self.state.passengers(ship)
            if not cargo:
                self.sailing.discard(ship.id)
            if ship.id not in self.sailing:
                landing = self.route(ship, self.port)
                if not landing:
                    continue
                berth, beach = landing
                candidates = sorted(
                    (
                        u
                        for u in self.state.my_units()
                        if u.type in ("I", "S") and not u.carrier and self.region.get(u.hex) == self.home
                    ),
                    key=lambda u: (u.type != "I", self.field(beach).get(u.hex, FAR), -u.hp),
                )
                if defenders:
                    candidates = candidates[max(2, len(defenders)) :]
                for u in candidates:
                    self.board(u, ship)
                cargo = self.state.passengers(ship)
                required = 2
                if sum(u.type == "I" for u in cargo) < required:
                    self.approach(ship, berth, True)
                    for u in candidates:
                        if not self.board(u, ship) and not u.carrier:
                            self.approach(u, beach)
                            self.board(u, ship)
                    cargo = self.state.passengers(ship)
                if sum(u.type == "I" for u in cargo) >= required:
                    self.sailing.add(ship.id)
            if ship.id in self.sailing:
                # Units can board a newly built ship and join it again just before it leaves.
                if bb.distance(ship.hex, self.port) <= 2:
                    for u in self.state.my_units():
                        if u.type in ("I", "S") and not u.carrier and self.region.get(u.hex) == self.home:
                            self.board(u, ship)
                    cargo = self.state.passengers(ship)
                if not self.land_under_fire(ship, cargo):
                    self.sail(ship, self.target_city(), cargo)

    def watch_channel(self, galley):
        """Intercept visible transports, then cover the return route from central wheat to port."""
        self.note_rival_shipping()
        sailing = self.field(galley.hex, True)
        transports = [e for e in self.state.enemy_units() if e.type == "T"]
        if transports:
            target = transports[0].hex
        else:
            berths = (h for h in bb.neighbors(self.enemy_port) if self.map.navigable(h))
            rival = min(berths, key=lambda h: sailing.get(h, FAR))
            # Stay on the crossing longer when a transport or a central wagon confirms traffic.
            blockade_round = 8 if self.saw_rival_shipping else 5
            if blockade_round <= self.state.round <= 12:
                # A returning transport must pass its own harbor before its wheat can pay for a galley.
                target = rival
            else:
                site = min(self.middle, key=lambda h: bb.distance(h, self.enemy_port))
                from_port = self.field(rival, True)
                to_wheat = self.field(site)

                def crossing(coast):
                    berth, beach = coast
                    return from_port.get(berth, FAR) + 2 * to_wheat[beach], sailing.get(berth, FAR)

                target, _ = min(self.coast[self.middle[0]], key=crossing)
        forward = self.field(target, True)
        paths = self.reachable(galley)
        paths[galley.hex] = ""
        dest = min(paths, key=lambda h: (forward.get(h, FAR), h != galley.hex))
        if paths[dest]:
            self.move(galley, paths[dest])

    def patrol(self):
        for u in self.mine("G"):
            if not self.can_act(u) or not u.mp:
                continue
            if self.state.round <= 14 and not self.last_galleys:
                self.watch_channel(u)
                continue
            goal = self.enemy_port
            if u.hp <= 57 or u.rations < 11:
                if self.state.city_at(self.port).owner == self.player:
                    goal = self.port
            # The enemy's launch hexes are both its way out and its supply line.
            berths = [h for h in bb.neighbors(goal) if self.map.navigable(h) and not self.state.enemy_at(h)]
            if berths:
                closest = min(berths, key=lambda h: self.field(h, True).get(u.hex, FAR))
                self.approach(u, closest, True)

    def march(self):
        target = self.target_city()
        for u in self.state.my_units():
            if u.type not in ("I", "S") or u.carrier or not self.can_act(u):
                continue
            region = self.region.get(u.hex)
            if region == self.home:
                if u.type == "S" and not self.launched_transport:
                    paths = self.reachable(u)
                    staging = [
                        h
                        for h in self.field(self.home)
                        if 2 <= bb.distance(h, self.port) <= 3
                        and bb.distance(h, self.home) > 1
                        and (h == u.hex or h in paths)
                    ]
                    if staging:
                        stop = min(
                            staging, key=lambda h: (bb.distance(h, self.enemy_port), bb.distance(h, u.hex))
                        )
                        if stop != u.hex:
                            self.move(u, paths[stop])
                    continue
                if self.state.city_at(self.home).owner != self.player:
                    goal = self.home
                else:
                    goal = self.port
                foes = [
                    e
                    for e in self.state.enemy_units()
                    if not e.carrier
                    and self.region.get(e.hex) == self.home
                    and e.type in ("I", "P", "K", "W")
                ]
                if foes and self.state.city_at(goal).owner == self.player:
                    goal = min(foes, key=lambda e: bb.distance(u.hex, e.hex)).hex
                if goal == self.port and self.state.city_at(goal).owner == self.player:
                    # Leave the pier clear for unloading wheat and building infantry.
                    if self.state.friends_at(goal, excluding=u) >= 1 or u.type == "S":
                        slots = [
                            h
                            for h in bb.neighbors(goal)
                            if h in self.field(self.home)
                            and not self.state.enemy_at(h)
                            and self.state.friends_at(h, excluding=u) < 3
                        ]
                        if slots:
                            goal = min(
                                slots,
                                key=lambda h: (self.field(h).get(u.hex, FAR), -self.map.terrain(h).defense),
                            )
            elif region == self.enemy_home:
                goal = target
            elif region == self.middle[0]:
                targets = [
                    e
                    for e in self.state.enemy_units()
                    if not e.carrier and self.region.get(e.hex) == region
                ]
                if targets:
                    goal = min(targets, key=lambda e: bb.distance(u.hex, e.hex)).hex
                else:
                    goal = min(self.middle, key=lambda h: bb.distance(u.hex, h))
            else:
                continue
            self.approach(u, goal)
            if self.can_capture(u):
                self.capture(u)

    def production(self):
        counts = Counter(u.type for u in self.state.my_units())
        for city in sorted(self.state.my_cities(), key=lambda c: c.hex != self.port):
            if city.built or self.state.enemy_at(city.hex):
                continue
            if self.is_port(city) and city.hex == self.port:
                if not self.launched_galley:
                    code = "G"
                elif not self.launched_transport or (not counts["T"] and self.last_galleys and counts["W"]):
                    code = "T"
                elif not counts["G"]:
                    code = "G"
                elif counts["T"] == 0 or (not self.ferry and counts["T"] < 2):
                    code = "T"
                elif not counts["W"]:
                    code = "W"
                elif self.last_galleys and counts["G"] <= len(self.last_galleys[1]) and counts["G"] < 3:
                    code = "G"
                elif counts["T"] < 2:
                    code = "T"
                elif counts["G"] < 3:
                    code = "G"
                else:
                    code = "I"
            elif city.hex == self.home and self.state.round <= 4 and counts["I"] < 2:
                code = "I"
            elif city.hex == self.home and not self.launched_transport and self.state.round <= 4:
                continue
            elif city.hex == self.home and counts["W"] < 2:
                code = "W"
            else:
                code = "I"
            t = self.rules.unit_types[code]
            if t.naval and not self.launch_hex(city):
                continue
            if not t.naval and self.state.friends_at(city.hex) >= self.rules.stack_limit:
                continue
            if city.bread < t.cost:
                continue
            built = self.build(city, code)
            if built:
                counts[code] += 1
                if code == "G":
                    self.launched_galley = True
                if code == "T" and not self.launched_transport:
                    self.launched_transport = True
                    ship = self.state.units[int(built.values[0])]
                    self.board_escort(ship)
                    wagon = self.state.units.get(self.harvester)
                    if wagon is not None and self.region.get(wagon.hex) == self.home:
                        self.board(wagon, ship)

    def on_turn(self, state):
        self.threat()
        for s in state.sites:
            if s.wheat:
                self.last_pile[s.hex] = state.round, s.wheat
        self.capture_close()
        self.fighting()
        if not self.launched_transport:
            self.march()
        self.supply()
        self.production()
        self.assign_ferry()
        self.invasions()
        self.march()
        self.patrol()
        self.capture_close()
        self.fighting()
        self.production()


if __name__ == "__main__":
    bb.run(Breakwater())
