"""Solid's city expansion and first-strike strategy. Run with python bots/solid_bot.py."""
from collections import Counter, defaultdict
import heapq
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kits" / "python"))
import bitbath as bb

DISK2 = bb.disk((0, 0), 2)
DISK3 = bb.disk((0, 0), 3)
CAUTION = 1.3
RESERVE_MS = 120


class SolidBot(bb.Bot):
    def on_init(self):
        m = self.map
        self.types = self.rules.unit_types
        self.limit = self.rules.stack_limit
        self.defense = {h: min(m.terrain(h).defense, 100) for h in m.hexes() if m.passable(h)}
        # Passable neighbors of each hex, as (direction, hex, cost to enter).
        self.nbrs = {
            h: [(d, n, m.terrain(n).move_cost) for d in range(6) for n in [bb.step(h, d)] if n in self.defense]
            for h in self.defense
        }
        # Terrain never changes, so each city's distance field is computed once.
        self.city_field = {cid: self.distance_field([h], blocked=set()) for cid, h, _ in m.cities}
        self.city_at = {h: cid for cid, h, _ in m.cities}
        self.concealing = {h for h in self.defense if m.terrain(h).conceal > 0}
        self.sight_cache = {}
        enemy_homes = [h for _, h, owner in self.map.cities if owner not in (0, self.player)]
        self.border = {cid: min((self.city_field[cid].get(h, 10000) for h in enemy_homes),
                                default=0) for cid, _, _ in self.map.cities}

    def forest_sight(self, h, vision):
        """Concealing hexes revealed and obscured inside an observer's normal radius."""
        key = h, vision
        if key not in self.sight_cache:
            visible = self.concealing.intersection(self.map.sight(h, vision))
            obscured = self.concealing.intersection(bb.disk(h, vision)) - visible
            self.sight_cache[key] = visible, obscured
        return self.sight_cache[key]

    def observed_forests(self, units, cities):
        observers = [(u.hex, self.types[u.type].vision + self.map.terrain(u.hex).vision_bonus)
                     for u in units]
        observers.extend((c.hex, self.rules.city_vision) for c in cities)
        seen, obscured = set(), set()
        for h, vision in observers:
            visible, hidden = self.forest_sight(h, vision)
            seen.update(visible)
            obscured.update(hidden)
        return seen, obscured - seen

    def scout_forests(self):
        """Spot hidden approaches before the army spends its actions."""
        scouts = [u for u in self.ready() if self.types[u.type].skirmish
                  and not self.types[u.type].capture and u.mp
                  and u.hex not in self.foreign_cities]
        if not self.concealing or not scouts:
            return
        _, hidden = self.observed_forests(self.state.my_units(), self.state.my_cities())
        # These hexes would already be visible except for concealment.
        for scout in scouts:
            if not hidden or self.out_of_time():
                break
            kind = self.types[scout.type]
            best, path = 0, ""
            for h, dirs in self.reach(scout).items():
                if not dirs:
                    continue
                vision = kind.vision + self.map.terrain(h).vision_bonus
                visible, _ = self.forest_sight(h, vision)
                score = len(hidden & visible) - 0.3 * len(dirs) - self.threat.get(h, 0) / 100
                if score > best:
                    best, path = score, dirs
            if path:
                start = scout.hex
                self.go(scout, path)
                # Interrupted moves reveal only the part of the path actually traveled.
                h = start
                for d in path:
                    if h == scout.hex:
                        break
                    h = bb.step(h, int(d))
                    visible, _ = self.forest_sight(h, kind.vision + self.map.terrain(h).vision_bonus)
                    hidden.difference_update(visible)

    def enemy_blind_spots(self):
        if not self.concealing:
            return set()
        cities = [c for c in self.state.cities if c.owner not in (0, self.player)]
        _, obscured = self.observed_forests(self.enemies.values(), cities)
        return obscured

    def refresh(self):
        """Rebuilds the lookup tables from the current state."""
        self.n_units = len(self.state.units)
        self.enemies = {}       # id -> Unit
        self.enemy_hex = set()
        self.zoc = set()        # hexes where a move ends
        self.threat = {}        # hex -> enemy strength within 3
        self.support = {}       # hex -> friendly strength within 2
        self.occ = {}           # hex -> number of my units
        self.foreign_cities = {c.hex for c in self.state.cities if c.owner != self.player}
        for u in self.state.units.values():
            q, r = u.q, u.r
            w = self.worth(u)
            if u.owner == self.player:
                self.occ[(q, r)] = self.occ.get((q, r), 0) + 1
                table, offsets = self.support, DISK2
            else:
                self.enemies[u.id] = u
                self.enemy_hex.add((q, r))
                if self.types[u.type].zoc:
                    self.zoc.update(bb.neighbors((q, r)))
                table, offsets = self.threat, DISK3
            for dq, dr in offsets:
                h = (q + dq, r + dr)
                table[h] = table.get(h, 0) + w

    def sync(self):
        """Refreshes the caches if a unit appeared or died. Returns whether it did."""
        if len(self.state.units) != self.n_units:
            self.refresh()
            return True
        return False

    def worth(self, u):
        t = self.types[u.type]
        return t.cost * u.hp / t.max_hp

    def ready(self):
        return [u for u in self.state.my_units() if not u.acted and not u.fresh]

    def out_of_time(self):
        return self.time_left() < RESERVE_MS

    def reach(self, u):
        """Where u can end a move this turn, as {hex: dirs}, including where it stands."""
        start = u.hex
        out = {start: ""}
        if u.mp <= 0 or u.acted or u.fresh:
            return out
        skirmish = self.types[u.type].skirmish
        # A unit that can't capture would only block a city we want to take.
        avoid = () if self.types[u.type].capture else self.foreign_cities
        mp, occ, limit, zoc, blocked, nbrs = u.mp, self.occ, self.limit, self.zoc, self.enemy_hex, self.nbrs
        best = {start: 0}
        heap = [(0, start, "")]
        while heap:
            cost, h, path = heapq.heappop(heap)
            if cost > best[h]:
                continue
            if h != start:
                if occ.get(h, 0) < limit and h not in avoid:
                    out[h] = path
                if not skirmish and h in zoc:
                    continue
            for d, n, enter in nbrs[h]:
                c = cost + enter
                if c <= mp and c < best.get(n, c + 1) and n not in blocked:
                    best[n] = c
                    heapq.heappush(heap, (c, n, path + str(d)))
        return out

    def go(self, u, dirs):
        """Moves u and keeps the caches current. Returns whether enemies came into view."""
        self.move(u, dirs)
        if self.sync():
            return True
        self.occ = {}
        for f in self.state.my_units():
            self.occ[f.hex] = self.occ.get(f.hex, 0) + 1
        return False

    def capture_in_place(self):
        for u in self.ready():
            if self.can_capture(u):
                self.capture(u)

    def rush_cities(self):
        """Moves capturers onto cities they can reach this turn."""
        me = self.player
        cities = [c for c in self.state.cities if c.owner != me and c.hex not in self.enemy_hex]
        units = [u for u in self.ready() if u.mp > 0 and self.types[u.type].capture]
        if not cities or not units:
            return
        reaches = {u.id: self.reach(u) for u in units}
        used = set()
        for c in cities:
            room = self.limit - self.occ.get(c.hex, 0)
            group = [u for u in units if u.id not in used and c.hex in reaches[u.id]]
            group = sorted(group, key=lambda u: -u.hp)[:max(room, 0)]
            if not group:
                continue
            progress = c.progress if c.capturer == me else 0
            takes_it = progress + sum(u.hp for u in group) >= self.rules.capture_points
            strength = self.support.get(c.hex, 0) + sum(self.worth(u) for u in group)
            if not takes_it and self.threat.get(c.hex, 0) > CAUTION * strength:
                continue
            for u in group:
                if c.owner == me or self.out_of_time():
                    break
                used.add(u.id)
                path = reaches[u.id].get(c.hex)
                if path is None or c.hex in self.enemy_hex:
                    continue
                if self.go(u, path):    # enemies came into view, so the old paths may be wrong
                    reaches = {f.id: self.reach(f) for f in units if f.id not in used}
                if u.hex == c.hex and self.can_capture(u):
                    self.capture(u)

    def enemy_coverage(self):
        """Estimate next-turn strikes with terrain, blocking units and friendly ZOC."""
        friends = set(self.occ)
        stops = {h for u in self.state.my_units() if self.types[u.type].zoc
                 for h in bb.neighbors(u.hex)}
        danger = defaultdict(float)
        for enemy in self.enemies.values():
            t = self.types[enemy.type]
            budget = 0 if t.no_move_attack else t.move
            dist = {enemy.hex: 0}
            heap = [(0, enemy.hex)]
            while heap:
                cost, h = heapq.heappop(heap)
                if cost != dist[h] or (h != enemy.hex and h in stops and not t.skirmish):
                    continue
                for _, n, enter in self.nbrs[h]:
                    new = cost + enter
                    if new <= budget and new < dist.get(n, budget + 1) and n not in friends:
                        dist[n] = new
                        heapq.heappush(heap, (new, n))
            threatened = {n for h in dist for r in range(t.range_min, t.range_max + 1)
                          for n in bb.ring(h, r)}
            for n in threatened:
                danger[n] += enemy.hp / t.max_hp
        return danger

    def attack_options(self):
        options = {}
        concealed = self.enemy_blind_spots()
        for u in self.ready():
            kind = self.types[u.type]
            if u.id in self.failed or (kind.no_move_attack and u.moved):
                continue
            span = kind.range_max + (0 if kind.no_move_attack else u.mp)
            enemies = [e for e in self.enemies.values() if bb.distance(e.hex, u.hex) <= span]
            if not enemies:
                continue
            reach = {u.hex: ""} if kind.no_move_attack else self.reach(u)
            for e in enemies:
                candidates = [h for r in range(kind.range_min, kind.range_max + 1)
                              for h in bb.ring(e.hex, r) if h in reach]
                if candidates:
                    def cover(h):
                        risk = self.incoming.get(h, 0) * (1 - self.defense[h] / 100)
                        return self.defense[h] - 28 * risk + 5 * (h in concealed), -len(reach[h])
                    h = max(candidates, key=cover)
                    options.setdefault(u.id, []).append((e.id, h, reach[h]))
        return options

    def trade(self, u, e, h, killable):
        """The value of u attacking e from h: what e loses minus what u loses."""
        ut, et = self.types[u.type], self.types[e.type]
        hit = self.damage(u.type, u.hp, e.type, e.hex, e.fort)
        left = e.hp - hit
        gain = et.cost * min(hit, e.hp) / et.max_hp
        if left <= 0:
            gain += et.cost * 0.5
        elif killable:
            gain *= 1.3
        cid = self.city_at.get(e.hex)
        if cid is not None and et.capture and self.state.cities[cid].owner != e.owner:
            gain += 150 if left <= 0 else 50   # it is taking a city
        elif cid is not None and self.state.cities[cid].owner == self.player and left <= 0:
            gain += 100     # it was stopping the city from building
        loss = 0
        if left > 0 and et.in_range(bb.distance(h, e.hex)):
            back = self.damage(e.type, left, u.type, h, u.fort if h == u.hex else 0)   # moving loses it
            loss = ut.cost * min(back, u.hp) / ut.max_hp
            if back >= u.hp:
                loss += ut.cost * 0.5
        value = gain - self.loss_weight * loss
        pressure = self.incoming.get(h, 0)
        if left <= 0:
            pressure = max(0, pressure - e.hp / self.types[e.type].max_hp)
        risk = pressure * (1 - self.defense[h] / 100)
        current = self.incoming.get(u.hex, 0) * (1 - self.defense[u.hex] / 100)
        return value - 10 * (min(3, risk) - min(3, current))

    def fight(self):
        """Makes the best attack on the board until none is worth making."""
        self.incoming = self.enemy_coverage()
        units = self.state.units
        opts = self.attack_options()
        while opts and not self.out_of_time():
            # How much damage could land on each enemy this turn?
            potential = {}
            for uid, found in opts.items():
                u = units[uid]
                for tid, _, _ in found:
                    e = self.enemies.get(tid)
                    if e:
                        potential[tid] = potential.get(tid, 0) + self.damage(u.type, u.hp, e.type, e.hex, e.fort)
            best = None
            for uid, found in opts.items():
                u = units[uid]
                for tid, h, dirs in found:
                    e = self.enemies.get(tid)
                    if not e or (h != u.hex and self.occ.get(h, 0) >= self.limit):
                        continue
                    score = self.trade(u, e, h, potential[tid] >= e.hp)
                    if best is None or score > best[0]:
                        best = (score, uid, tid, h, dirs)
            if best is None or best[0] <= 0:
                break
            _, uid, tid, h, dirs = best
            u = units[uid]
            if dirs:
                spotted = self.go(u, dirs)
                if u.hex != h:
                    self.failed.add(uid)
                if u.hex != h or spotted:
                    opts = self.attack_options()
                    continue
            del opts[uid]
            self.attack(u, tid)
            self.sync()
            opts = {k: v for k, v in opts.items() if k in units}

    def advance(self):
        self.refresh()
        danger = self.enemy_coverage()
        concealed = self.enemy_blind_spots()
        cities = self.state.cities
        homes = self.state.my_cities()
        goals = [c for c in cities if c.owner != self.player or danger[c.hex]]
        load = Counter()

        # Give nearby units the first chance to claim a destination.
        def nearest(u):
            return min((self.city_field[c.id].get(u.hex, 10000) for c in goals), default=10000)
        for u in sorted(self.ready(), key=nearest):
            if self.out_of_time():
                break
            kind = self.types[u.type]
            if (u.hex in self.city_at and cities[self.city_at[u.hex]].owner == self.player
                    and self.state.round >= 8 and self.occ[u.hex] == 1):
                continue
            if kind.skirmish and not kind.capture:
                self.raid(u, danger)
                continue
            best_goal, best_cost = None, float("inf")
            for c in goals:
                travel = self.city_field[c.id].get(u.hex)
                if travel is None:
                    continue
                if c.owner == self.player:
                    priority = -4 - min(5, danger[c.hex])
                    if c.capturer:
                        priority -= 8
                else:
                    priority = 7 if c.owner else -9 + self.border[c.id] * 0.2
                # Keep the first pair together for a one-turn capture.
                batch = 2 if not c.owner else max(4, int(danger[c.hex]) + 3)
                cost = travel + priority + 12 * (load[c.id] // batch)
                if cost < best_cost:
                    best_goal, best_cost = c, cost
            if best_goal is None:
                continue
            wounded = u.hp < self.types[u.type].max_hp * 0.76
            if wounded and homes:
                best_goal = min(homes, key=lambda c: self.city_field[c.id].get(u.hex, 10000) + 4 * danger[c.hex])
            load[best_goal.id] += 1
            field = self.city_field[best_goal.id]

            def position(h):
                risk = danger[h] * (1 - self.defense[h] / 100)
                city = self.city_at.get(h)
                healing = city is not None and cities[city].owner == self.player and u.hp < self.types[u.type].max_hp
                overwatch = (kind.no_move_attack and any(kind.in_range(bb.distance(e.hex, h))
                              for e in self.enemies.values()))
                crowded_city = (city is not None and cities[city].owner == self.player
                                and self.occ.get(h, 0) - (h == u.hex) >= self.limit - 1)
                return (-field.get(h, 10000) - 12 * min(6, risk) + self.defense[h] / 45 - 10 * crowded_city
                        + 6 * healing + 8 * overwatch + 2 * (h in concealed))
            reach = self.reach(u)
            h = max(reach, key=position)
            if reach[h]:
                self.go(u, reach[h])
            if self.can_capture(u):
                self.capture(u)

    def raid(self, u, danger):
        """Use the starting scout to cut enemy recruitment on an empty city."""
        enemies = [c for c in self.state.cities if c.owner not in (0, self.player)]
        if not enemies:
            return
        reach = self.reachable(u, self.zoc)
        reach[u.hex] = ""
        best, path = -float("inf"), ""
        for c in enemies:
            if self.occ.get(c.hex, 0) > int(u.hex == c.hex):
                continue
            field = self.city_field[c.id]
            for h, dirs in reach.items():
                # With a single stacking slot, leave the city open to infantry.
                if self.limit == 1 and h in self.foreign_cities:
                    continue
                score = -field.get(h, 10000) - 4 * danger[h] + 12 * (h == c.hex)
                if score > best:
                    best, path = score, dirs
        if path:
            self.go(u, path)

    def build_all(self):
        """Field as many effective capturers as the economy can support."""
        state = self.state
        goals = [c for c in state.cities if c.owner != self.player]
        fronts = sorted(state.my_cities(), key=lambda c: min(
            (self.city_field[g.id].get(c.hex, float("inf")) for g in goals), default=0))
        capturers = [t for t in self.types.values() if t.capture]
        line = max(capturers or self.types.values(), key=lambda t: t.max_hp * t.move / max(1, t.cost))
        cavalry = [t for t in self.types.values() if t.zoc and t.move > line.move]
        for i, c in enumerate(fronts):
            if c.built or state.enemy_at(c.hex) or state.friends_at(c.hex) >= self.limit:
                continue
            if state.gold < line.cost:
                break
            recruit = line
            if cavalry and state.round > 5:
                heavy = max(cavalry, key=lambda t: t.move)
                if state.gold >= heavy.cost + line.cost * (len(fronts) - i - 1):
                    recruit = heavy
            self.build(c, recruit.code)

    def on_turn(self, state):
        self.failed = set()
        self.loss_weight = 1.0 if state.round >= self.rules.constants.get("round_limit", float("inf")) else 0.8
        self.refresh()
        try:
            self.capture_in_place()
            self.scout_forests()
            self.rush_cities()
            self.fight()
            self.refresh()
            self.rush_cities()  # Combat may have opened a previously occupied city.
            self.advance()
        except bb.GameOver:
            raise
        except Exception:
            traceback.print_exc()
        self.build_all()


if __name__ == "__main__":
    bb.run(SolidBot())
