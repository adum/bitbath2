"""The smart reference bot: a fast heuristic player.

Each turn it runs five phases:

1. Capture with every unit already standing on a city it doesn't own.
2. Rush cities that capturers can reach this turn, sending enough units
   together to take a city at once when possible.
3. Fight: repeatedly make the best move-and-attack trade on the board,
   focusing fire on units that can be killed this turn.
4. Advance: assign the remaining units to cities (to capture, or to defend)
   and march them there, holding back from places where the visible enemy
   outweighs nearby friends.
5. Build: spend all gold on the unit mix that trades best, by the damage
   table, against the enemy units seen so far.

Everything is read from the rules message, so it adapts to rule variants.

Usage: python bots/smart_bot.py
"""

import heapq
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kits", "python"))
import bitbath as bb  # noqa: E402

DISK2 = bb.disk((0, 0), 2)
DISK3 = bb.disk((0, 0), 3)

# A unit won't walk to a hex where visible enemy strength within 3 hexes
# exceeds this many times the friendly strength within 2.
CAUTION = 1.3
# An attack is made when its gain exceeds this share of its loss. Below 1,
# because declining to attack hands the enemy the first strike.
LOSS_WEIGHT = 0.8
# Build shares are each type's score relative to the best, raised to this
# power. High, so that gold goes almost entirely to the best-trading type.
SHARE_POWER = 8
# Strength sent to an undefended neutral city: two infantry take it at once.
NEUTRAL_FORCE = 200
# Until the army is this big, build only units that can capture.
EARLY_ARMY = 6
# Stop planning moves when this little of the turn is left, and go build.
RESERVE_MS = 120


class SmartBot(bb.Bot):
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
        self.seen = {}      # enemy unit id -> type, for every enemy seen and not known dead
        self.failed = set()

    # Per-turn caches.

    def refresh(self):
        """Rebuilds the lookup tables from the current state."""
        self.n_units = len(self.state.units)
        self.enemies = {}       # id -> Unit
        self.enemy_hex = set()
        self.zoc = set()        # hexes where a move ends
        self.adjacent = set()   # hexes next to any visible enemy
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
                self.seen[u.id] = u.type
                self.enemy_hex.add((q, r))
                near = bb.neighbors((q, r))
                self.adjacent.update(near)
                if self.types[u.type].zoc:
                    self.zoc.update(near)
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

    # Movement.

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

    def dominated(self, u, h):
        """Whether the visible enemy near h outweighs u and its friends there."""
        threat = self.threat.get(h, 0)
        if not threat:
            return False
        support = self.support.get(h, 0)
        if bb.distance(h, u.hex) > 2:
            support += self.worth(u)
        return threat > CAUTION * support

    # Phase 1 and 2: captures.

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

    # Phase 3: combat.

    def dmg(self, att_type, att_hp, def_type, def_hex):
        base = self.rules.damage[att_type][def_type]
        if base == 0:
            return 0
        return max(1, base * att_hp * (100 - self.defense[def_hex]) // (self.types[att_type].max_hp * 100))

    def trade(self, u, e, h, killable):
        """The value of u attacking e from h: what e loses minus what u loses."""
        ut, et = self.types[u.type], self.types[e.type]
        hit = self.dmg(u.type, u.hp, e.type, e.hex)
        left = e.hp - hit
        gain = et.cost * min(hit, e.hp) / et.max_hp
        if left <= 0:
            gain += et.cost * 0.5
        elif killable:
            gain *= 1.3
        cid = self.city_at.get(e.hex)
        if cid is not None and et.capture and self.state.cities[cid].owner != e.owner:
            gain += 150 if left <= 0 else 50   # it is taking a city
        loss = 0
        if left > 0 and et.in_range(bb.distance(h, e.hex)):
            back = self.dmg(e.type, left, u.type, h)
            loss = ut.cost * min(back, u.hp) / ut.max_hp
            if back >= u.hp:
                loss += ut.cost * 0.5
        return gain - LOSS_WEIGHT * loss

    def attack_options(self):
        """For each unit that can attack: [(enemy id, hex to attack from, dirs to get there)]."""
        opts = {}
        enemies = list(self.enemies.values())
        for u in self.ready():
            if u.id in self.failed:
                continue
            t = self.types[u.type]
            if t.no_move_attack and u.moved:
                continue
            span = t.range_max + (0 if t.no_move_attack else u.mp)
            near = [e for e in enemies if bb.distance(u.hex, e.hex) <= span]
            if not near:
                continue
            reach = {u.hex: ""} if t.no_move_attack else self.reach(u)
            found = []
            for e in near:
                best = None
                for radius in range(t.range_min, t.range_max + 1):
                    for h in bb.ring(e.hex, radius):
                        if h in reach:
                            key = (self.defense[h], -len(reach[h]))
                            if best is None or key > best[0]:
                                best = (key, h)
                if best:
                    found.append((e.id, best[1], reach[best[1]]))
            if found:
                opts[u.id] = found
        return opts

    def fight(self):
        """Makes the best attack on the board until none is worth making."""
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
                        potential[tid] = potential.get(tid, 0) + self.dmg(u.type, u.hp, e.type, e.hex)
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
            if self.attack(u, tid) and tid not in units:
                self.seen.pop(tid, None)
            self.sync()
            opts = {k: v for k, v in opts.items() if k in units}

    # Phase 4: marching.

    def advance(self):
        self.refresh()
        s, me = self.state, self.player
        # Goal cities: id -> (value, strength wanted there).
        goals = {}
        for c in s.cities:
            threat = self.threat.get(c.hex, 0)
            if c.owner == me:
                if threat or c.capturer not in (0, me):
                    goals[c.id] = (2.0, 1.2 * threat + 50)
            elif c.owner == 0:
                goals[c.id] = (1.0, NEUTRAL_FORCE + threat)
            else:
                goals[c.id] = (1.5, 300 + 1.5 * threat)
        if not goals:
            return
        contested = [cid for cid in goals if s.cities[cid].owner != 0 or self.threat.get(s.cities[cid].hex, 0)]
        plans = []
        for u in self.ready():
            if u.mp <= 0:
                continue
            t = self.types[u.type]
            if t.skirmish and not t.capture:
                self.scout(u)
                continue
            # Units that can't capture go where there is something to fight.
            wanted = goals if t.capture or not contested else contested
            turns = {}
            for cid in wanted:
                d = self.city_field[cid].get(u.hex)
                if d is not None:
                    turns[cid] = d / t.move
            if turns:
                plans.append((min(turns.values()), u.id, u, turns))
        plans.sort(key=lambda p: p[:2])
        load = dict.fromkeys(goals, 0)
        for _, _, u, turns in plans:
            if self.out_of_time():
                break
            cid = max(turns, key=lambda c: goals[c][0] / (turns[c] + 2) / (1 + load[c] / goals[c][1]))
            load[cid] += self.worth(u)
            self.debug_line(u.hex, s.cities[cid].hex, "orange")
            self.march(u, cid)
        self.debug_text("round %d: %d units marching on %d cities" % (s.round, len(plans), len(goals)))

    def march(self, u, cid):
        """Moves u toward a city, stopping short of fights it would lose."""
        field, goal = self.city_field[cid], self.state.cities[cid].hex
        best = None
        for h, dirs in self.reach(u).items():
            d = field.get(h)
            if d is None:
                continue
            if h != u.hex and ((h in self.adjacent and h != goal) or self.dominated(u, h)):
                continue
            key = (d - self.defense[h] / 50, len(dirs))
            if best is None or key < best[0]:
                best = (key, h, dirs)
        if best[1] == u.hex:
            cid_here = self.city_at.get(u.hex)
            if self.dominated(u, u.hex) and (cid_here is None or self.state.cities[cid_here].owner != self.player):
                self.retreat(u)
            return
        self.go(u, best[2])
        if self.can_capture(u):
            self.capture(u)

    def retreat(self, u):
        """Falls back toward the nearest city we own."""
        if self._home_field is None:
            self._home_field = self.distance_field([c.hex for c in self.state.my_cities()], blocked=set())
        field = self._home_field
        best = (field.get(u.hex, 1 << 30), "")
        for h, dirs in self.reach(u).items():
            d = field.get(h)
            if d is not None and h not in self.adjacent and d < best[0]:
                best = (d, dirs)
        if best[1]:
            self.go(u, best[1])

    def scout(self, u):
        """Scouts watch the nearest enemy city from outside the enemy's reach."""
        theirs = [c.id for c in self.state.cities if c.owner not in (0, self.player)]
        here = u.hex
        dists = {cid: self.city_field[cid].get(here) for cid in theirs}
        dists = {cid: d for cid, d in dists.items() if d is not None}
        if not dists:
            return
        field = self.city_field[min(dists, key=lambda cid: (dists[cid], cid))]
        best = None
        for h, dirs in self.reach(u).items():
            d = field.get(h)
            if d is None:
                continue
            key = (self.threat.get(h, 0), d, len(dirs))
            if best is None or key < best[0]:
                best = (key, dirs)
        if best and best[1]:
            self.go(u, best[1])

    # Phase 5: building.

    def unit_scores(self):
        """How well each unit type trades against the enemy units seen, per gold spent."""
        types, damage = self.types, self.rules.damage
        mix = {}
        for code in self.seen.values():
            mix[code] = mix.get(code, 0) + 1
        # Until we have seen much, assume the enemy fields cheap capturers.
        capturers = [t for t in types.values() if t.capture]
        if capturers:
            cheapest = min(capturers, key=lambda t: t.cost).code
            mix[cheapest] = mix.get(cheapest, 0) + 2
        scores = {}
        for code, t in types.items():
            dealt = sum(n * damage[code][e] * types[e].cost / types[e].max_hp for e, n in mix.items())
            taken = sum(n * damage[e][code] for e, n in mix.items()) or 1
            score = dealt * t.max_hp / (taken * t.cost)
            if t.range_min > 1:
                score *= 1.5    # melee units can't hit back at it
            scores[code] = score
        return scores

    def build_all(self):
        s, types = self.state, self.types
        if not types:
            return
        self.sync()
        scores = self.unit_scores()
        top = max(scores.values()) or 1
        share = {code: (score / top) ** SHARE_POWER for code, score in scores.items()}
        count = dict.fromkeys(types, 0)
        for u in s.my_units():
            count[u.type] += 1
        # Build first where the enemy is closest.
        targets = [c for c in s.cities if c.owner != self.player]

        def urgency(city):
            near = min([self.city_field[c.id].get(city.hex, 1 << 30) for c in targets], default=0)
            return (-self.threat.get(city.hex, 0), near, city.id)

        for city in sorted(s.my_cities(), key=urgency):
            if city.built or city.hex in self.enemy_hex or self.occ.get(city.hex, 0) >= self.limit:
                continue
            total = sum(count.values())
            can_capture = sum(n for code, n in count.items() if types[code].capture)
            options = [code for code, t in types.items() if t.cost <= s.gold and share[code] >= 0.05]
            if can_capture * 2 < total + 1 or total < EARLY_ARMY:
                options = [code for code in options if types[code].capture] or options
            if not options:
                break
            # The type furthest below its share of the army.
            code = max(options, key=lambda c: (share[c] / (count[c] + 1), share[c]))
            if self.build(city, code):
                count[code] += 1
                self.occ[city.hex] = self.occ.get(city.hex, 0) + 1

    def on_turn(self, state):
        for entry in state.logs:
            if entry[0] == "HIT":
                self.seen[int(entry[1])] = entry[2]
        self.failed = set()
        self._home_field = None
        self.refresh()
        try:
            self.capture_in_place()
            self.rush_cities()
            self.fight()
            self.advance()
        except bb.GameOver:
            raise
        except Exception:
            traceback.print_exc()   # a bug in the plan shouldn't cost the builds too
        self.build_all()


if __name__ == "__main__":
    bb.run(SmartBot())
