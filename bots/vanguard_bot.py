"""Vanguard: a heuristic bot built to beat strong infantry play.

It grew out of the smart reference bot and keeps its five phases, with
four changes that decide games against an aggressive opponent:

- Kills are planned, not stumbled on. Each turn it finds the enemy unit
  that the fewest attackers can destroy, sends exactly those (big hitters
  first, so the target strikes back weakly), and repeats. Only then does
  it make single attacks that trade well.
- It strikes what it uncovers. Units see 2 hexes but strike from 4, so
  marching reveals enemies; whenever it does, the units that can still
  act attack at once instead of waiting for the next turn.
- It concentrates. Neutral cities are still taken first, but every unit
  with nothing better to do marches on one enemy city, the one nearest to
  its own, until that city falls.
- It keeps no garrisons, and takes losing trades less readily than it
  declines them: an idle unit is worth less than an attack.

The phases, each turn:

1. Capture with every unit already standing on a city it doesn't own.
2. Rush cities that capturers can reach this turn, sending enough units
   together to take a city at once when possible. This runs again after
   the fight, which may have cleared a city.
3. Fight: planned kills, then the best remaining trades.
4. Advance: assign the remaining units to cities and march them there,
   staying out of hexes the enemy can strike next turn. Wounded units go
   home to heal.
5. Build: spend all gold on the unit type that trades best, by the damage
   table, against the enemy units seen so far.

The enemy's strike range is worked out from every enemy unit seen in the
last few turns, not only those in view now, and it follows the
concealment rule. Each enemy's part of that map is kept for the turn and
redone only when the enemy moves, is hurt, or dies, which keeps turns
short on large maps.

On the last turn of the game only the tiebreak matters, so it plays for
cities and army value alone.

Everything is read from the rules message, so it adapts to rule variants.

Usage: python bots/vanguard_bot.py
"""

import heapq
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kits", "python"))
import bitbath as bb  # noqa: E402

DISK2 = bb.disk((0, 0), 2)
DISK3 = bb.disk((0, 0), 3)

# rush_cities won't send units onto a city where visible enemy strength within
# 3 hexes exceeds this many times the friendly strength within 2, unless the
# city falls at once.
CAUTION = 1.3
# An attack is made when its gain exceeds this share of its loss. Below 1,
# because declining to attack hands the enemy the first strike.
LOSS_WEIGHT = 0.7
# Build shares are each type's score relative to the best, raised to this
# power. High, so that gold goes almost entirely to the best-trading type;
# types below MIN_SHARE are not built at all.
SHARE_POWER = 8
MIN_SHARE = 0.1
# Until the army is this big, build only units that can capture.
EARLY_ARMY = 6
# Marching goals: what a city is worth, by owner. Neutral cities come first:
# they are free income, and the side that holds more cities wins the long game.
NEUTRAL_VALUE = 1.0
ENEMY_VALUE = 0.2
OWN_VALUE = 1.0
# Strength sent to an undefended neutral city: two infantry take it at once.
NEUTRAL_FORCE = 200
# Marching: what one enemy unit able to strike a hex next turn costs, in
# move points of progress toward the goal.
RISK = 12
# Attacking: what each enemy able to strike the attack hex costs, in gold.
EXPOSE = 10
# Units below this share of full HP go back to a city to heal.
HEAL_BELOW = 0.76
# Enemies that slipped out of sight still count as threats for this many turns.
GHOST = 8
# Stop planning moves when this little of the turn is left, and go build.
RESERVE_MS = 120


class VanguardBot(bb.Bot):
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
        self.rings = {}
        self.disks = {}
        self.vbonus = {h: m.terrain(h).vision_bonus for h in self.defense}
        # Terrain that hides the units on it from distant observers.
        self.conceal = {h: c for h in self.defense for c in [getattr(m.terrain(h), "conceal", 0)] if c}
        self.views = {}
        self.ghosts = {}    # enemy id -> (Unit as last seen, round)
        self.loss_weight, self.expose, self.risk_weight, self.last_turn = LOSS_WEIGHT, EXPOSE, RISK, False
        self.menace, self.menaces = {}, {}
        capturers = [t for t in self.types.values() if t.capture] or list(self.types.values())
        line = min(capturers, key=lambda t: t.cost)
        self.par = self.first_strike(line.code, line.max_hp, line.code) or 1
        self.cover = {}
        self.risk_at = {}
        self.seen = {}      # enemy unit id -> type, for every enemy seen and not known dead
        self.failed = set()
        self.focus = None

    # Per-turn caches.

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
                self.seen[u.id] = u.type
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

    # Movement.

    def reach(self, u, block_cities=True):
        """Where u can end a move this turn, as {hex: dirs}, including where it stands."""
        start = u.hex
        out = {start: ""}
        if u.mp <= 0 or u.acted or u.fresh:
            return out
        skirmish = self.types[u.type].skirmish
        # A unit that can't capture would only block a city we want to take.
        avoid = () if self.types[u.type].capture or not block_cities else self.foreign_cities
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
        old = u.hex
        self.move(u, dirs)
        spotted = self.sync()   # a refresh recounts the stacks
        if not spotted and u.hex != old:
            occ = self.occ
            if occ.get(old, 0) <= 1:
                occ.pop(old, None)
            else:
                occ[old] -= 1
            occ[u.hex] = occ.get(u.hex, 0) + 1
        return spotted

    def coverage(self, anew=False):
        """How many enemy units could strike each hex next turn, as {hex:
        count}, counting a wounded unit as its share of full HP. Also fills
        self.menace. Each enemy's part is kept for the turn and redone only
        when that enemy moves, is hurt, or dies."""
        current = {e.id: e for e in self.enemies.values()}
        for i, (g, _) in self.ghosts.items():
            current.setdefault(i, g)
        cache, cover, menace = self.cov_cache, self.cover_tab, self.menace
        if anew:    # our own units have moved, and they shape where the enemy can go
            cache.clear()
            cover.clear()
            for table in menace.values():
                table.clear()
        for eid in list(cache):
            sig, struck, w, ws = cache[eid]
            e = current.get(eid)
            if e is None or (e.type, e.hex, e.hp) != sig:
                del cache[eid]
                self.spread(cover, struck, -w)
                for code, x in ws.items():
                    self.spread(menace[code], struck, -x)
        if len(cache) == len(current):
            return cover
        friends = set(self.occ)
        stops = {h for u in self.state.my_units() if self.types[u.type].zoc for h in bb.neighbors(u.hex)}
        nbrs = self.nbrs
        for eid, e in current.items():
            if eid in cache:
                continue
            t = self.types[e.type]
            budget = 0 if t.no_move_attack else t.move
            dist = {e.hex: 0}
            heap = [(0, e.hex)]
            while heap:
                cost, h = heapq.heappop(heap)
                if cost != dist[h] or (h != e.hex and h in stops and not t.skirmish):
                    continue
                for _, n, enter in nbrs[h]:
                    c = cost + enter
                    if c <= budget and c < dist.get(n, budget + 1) and n not in friends:
                        dist[n] = c
                        heapq.heappush(heap, (c, n))
            key = (t.range_min, t.range_max)
            if key not in self.rings:
                self.rings[key] = [o for r in range(key[0], key[1] + 1) for o in bb.ring((0, 0), r)]
            offsets = self.rings[key]
            struck = {(q + dq, r + dr) for q, r in dist for dq, dr in offsets}
            w = e.hp / t.max_hp
            self.spread(cover, struck, w)
            ws = {}
            for code in menace:
                x = self.menace_of(e, code)
                if x:
                    ws[code] = x
                    self.spread(menace[code], struck, x)
            cache[eid] = ((e.type, e.hex, e.hp), struck, w, ws)
        return cover

    @staticmethod
    def spread(table, hexes, w):
        if w > 0:
            for h in hexes:
                table[h] = table.get(h, 0) + w
        else:
            for h in hexes:
                v = table.get(h, 0) + w
                if v < 1e-9:
                    table.pop(h, None)
                else:
                    table[h] = v

    def menace_of(self, e, code):
        """How much an enemy unit wants to strike one of our units of a type:
        its gain from a first strike on open ground, relative to a fight
        between two of the cheapest capturers. 0 if it would lose by it."""
        key = (e.type, e.hp, code)
        if key not in self.menaces:
            self.menaces[key] = self.first_strike(e.type, e.hp, code) / self.par
        return self.menaces[key]

    def first_strike(self, att, att_hp, dfn):
        types, damage = self.types, self.rules.damage
        a, d = types[att], types[dfn]
        hit = min(d.max_hp, damage[att][dfn] * att_hp // a.max_hp)
        gain = d.cost * hit / d.max_hp
        back = 0
        if hit < d.max_hp and d.range_min <= a.range_min <= d.range_max:
            back = min(att_hp, damage[dfn][att] * (d.max_hp - hit) // d.max_hp)
        return max(0, gain - LOSS_WEIGHT * a.cost * back / a.max_hp)

    def risk(self, h, code):
        """Enemy strikes that threaten a unit of a type on h, discounted by terrain."""
        return self.menace.get(code, {}).get(h, 0) * (1 - self.defense.get(h, 0) / 100)

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
        elif cid is not None and self.state.cities[cid].owner == self.player and left <= 0:
            gain += 100     # it was stopping the city from building
        loss = 0
        if left > 0 and et.in_range(bb.distance(h, e.hex)):
            back = self.dmg(e.type, left, u.type, h)
            loss = ut.cost * min(back, u.hp) / ut.max_hp
            if back >= u.hp:
                loss += ut.cost * 0.5
        return gain - self.loss_weight * loss

    def attack_options(self):
        """For each unit that can attack: [(enemy id, hex to attack from, dirs to get there)]."""
        opts = {}
        at = {}
        for e in self.enemies.values():
            at.setdefault(e.hex, []).append(e)
        self.cover = self.coverage()
        self.risk_at = {}
        for u in self.ready():
            self.risk_at[u.type, u.hex] = self.risk(u.hex, u.type)
            if u.id in self.failed:
                continue
            t = self.types[u.type]
            if t.no_move_attack and u.moved:
                continue
            span = t.range_max + (0 if t.no_move_attack else u.mp)
            near = [e for h in self.disk(u.hex, span) if h in at for e in at[h]]
            if not near:
                continue
            reach = {u.hex: ""} if t.no_move_attack else self.reach(u)
            found = []
            for e in near:
                best = None
                for radius in range(t.range_min, t.range_max + 1):
                    for h in bb.ring(e.hex, radius):
                        if h in reach:
                            key = (self.defense[h] - 28 * self.risk(h, u.type), -len(reach[h]))
                            if best is None or key > best[0]:
                                best = (key, h)
                if best:
                    self.risk_at[u.type, best[1]] = self.risk(best[1], u.type)
                    found.append((e.id, best[1], reach[best[1]]))
            if found:
                opts[u.id] = found
        return opts

    def kill_plan(self, opts):
        """The cheapest kill on the board: (enemy id, [(unit id, hex, dirs)]) using
        the fewest attackers, or None. Big hitters go first, so that the
        target strikes back as weakly as possible."""
        units = self.state.units
        by_target = {}
        for uid, found in opts.items():
            u = units[uid]
            for tid, h, dirs in found:
                e = self.enemies.get(tid)
                if e:
                    by_target.setdefault(tid, []).append((self.dmg(u.type, u.hp, e.type, e.hex), uid, h, dirs))
        best = None
        for tid, hits in by_target.items():
            e = self.enemies[tid]
            hits.sort(key=lambda x: (-x[0], x[1]))
            room, hp, team = {}, e.hp, []
            for hit, uid, h, dirs in hits:
                if h != units[uid].hex:
                    if self.occ.get(h, 0) + room.get(h, 0) >= self.limit:
                        continue
                    room[h] = room.get(h, 0) + 1
                team.append((uid, h, dirs))
                hp -= hit
                if hp <= 0:
                    break
            if hp > 0:
                continue
            # Swap the last blow for the weakest unit that still finishes it.
            need = hp + [x[0] for x in hits if x[1] == team[-1][0]][0]
            used = {t[0] for t in team[:-1]}
            last_uid, last_hex = team[-1][:2]
            frees = last_hex if last_hex != units[last_uid].hex else None
            for hit, uid, h, dirs in reversed(hits):
                if uid not in used and hit >= need and (h == units[uid].hex or h == frees
                                                        or self.occ.get(h, 0) + room.get(h, 0) < self.limit):
                    team[-1] = (uid, h, dirs)
                    break
            t = self.types[e.type]
            value = t.cost * (0.5 + e.hp / t.max_hp)
            cid = self.city_at.get(e.hex)
            if cid is not None and self.state.cities[cid].owner != e.owner:
                value += 150
            key = (value / len(team), -len(team), -tid)
            if best is None or key > best[0]:
                best = (key, tid, team)
        return best and best[1:]

    def fight_kills(self):
        """Carries out kills, cheapest first, until none is left."""
        units = self.state.units
        opts, fresh = self.attack_options(), True
        while not self.out_of_time():
            plan = self.kill_plan(opts)
            if not plan:
                if fresh:
                    return
                opts, fresh = self.attack_options(), True   # kills may have opened paths
                continue
            tid, team = plan
            fresh = False
            stale = False
            for uid, h, dirs in team:
                u = units.get(uid)
                if u is None or tid not in units:
                    break
                opts.pop(uid, None)
                if dirs:
                    spotted = self.go(u, dirs)
                    if u.hex != h:
                        self.failed.add(uid)
                        stale = True
                        break
                    if spotted:
                        stale = True
                if not self.attack(u, tid):
                    self.failed.add(uid)
                    stale = True
                    break
                self.sync()
                if stale:
                    break
            if tid not in units:
                self.seen.pop(tid, None)
                self.ghosts.pop(tid, None)
            if stale:
                opts, fresh = self.attack_options(), True
            else:
                opts = {k: v for k, v in opts.items() if k in units}

    def fight(self):
        """Makes the best attack on the board until none is worth making."""
        units = self.state.units
        self.fight_kills()
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
                    if (u.type, h) in self.risk_at and h != u.hex:
                        # Standing where the enemy can strike back costs something.
                        after = self.risk_at[u.type, h]
                        if self.dmg(u.type, u.hp, e.type, e.hex) >= e.hp:
                            after = max(0, after - self.menace_of(e, u.type) * (1 - self.defense[h] / 100))
                        score -= self.expose * (min(3, after) - min(3, self.risk_at.get((u.type, u.hex), 0)))
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
                self.ghosts.pop(tid, None)
            self.sync()
            opts = {k: v for k, v in opts.items() if k in units}

    # Phase 4: marching.

    def advance(self):
        self.refresh()
        s, me = self.state, self.player
        self.cover = self.coverage(True)
        # Goal cities: id -> (value, strength wanted there).
        goals = {}
        for c in s.cities:
            threat = self.threat.get(c.hex, 0)
            if c.owner == me:
                if threat or c.capturer not in (0, me):
                    goals[c.id] = (OWN_VALUE, 1.2 * threat + 50)
            elif c.owner == 0:
                goals[c.id] = (NEUTRAL_VALUE, NEUTRAL_FORCE + threat)
            else:
                goals[c.id] = (ENEMY_VALUE, 300 + 1.5 * threat)
        # Units with nothing better to do all march on one enemy city, the
        # nearest to ours, until it falls: strength in one place wins fights.
        theirs = [c for c in s.cities if c.owner not in (0, me)]
        ours = s.my_cities()
        if theirs and ours:
            if self.focus is None or s.cities[self.focus].owner in (0, me):
                self.focus = min(theirs, key=lambda c: (min(self.city_field[o.id].get(c.hex, 1 << 30) for o in ours), c.id)).id
            goals[self.focus] = (ENEMY_VALUE, 1e9)
        if not goals:
            return
        contested = [cid for cid in goals if s.cities[cid].owner != 0 or self.threat.get(s.cities[cid].hex, 0)]
        mine = s.my_cities()
        plans, scouts = [], []
        for u in self.ready():
            if u.mp <= 0:
                continue
            t = self.types[u.type]
            if t.skirmish and not t.capture:
                scouts.append(u)
                continue
            if u.hp < t.max_hp * HEAL_BELOW and mine:
                home = min(mine, key=lambda c: (self.city_field[c.id].get(u.hex, 1 << 30) + 4 * self.cover.get(c.hex, 0), c.id))
                plans.append((-1, u.id, u, {home.id: 0}))
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
            if len(turns) == 1:
                cid = next(iter(turns))
            else:
                cid = max(turns, key=lambda c: goals[c][0] / (turns[c] + 2) / (1 + load[c] / goals[c][1]))
            if cid in load:
                load[cid] += self.worth(u)
            self.debug_line(u.hex, s.cities[cid].hex, "orange")
            if u.id not in s.units or u.acted:
                continue
            if self.march(u, cid):
                # The march uncovered enemies: strike them with whoever still can.
                self.fight()
                self.refresh()
                self.cover = self.coverage(True)
        self.post_scouts([u for u in scouts if u.id in s.units])
        self.debug_text("round %d: %d units marching on %d cities" % (s.round, len(plans), len(goals)))

    def march(self, u, cid):
        """Moves u toward a city, weighing progress against the enemy's reach."""
        field, cities, me = self.city_field[cid], self.state.cities, self.player
        t = self.types[u.type]
        hurt = u.hp < t.max_hp
        best = None
        for h, dirs in self.reach(u).items():
            d = field.get(h)
            if d is None:
                continue
            score = -d - self.risk_weight * min(6, self.risk(h, u.type)) + self.defense[h] / 45
            here = self.city_at.get(h)
            if here is not None and cities[here].owner == me:
                if hurt:
                    score += 6      # it heals there
                if self.occ.get(h, 0) - (h == u.hex) >= self.limit - 1:
                    score -= 10     # leave room to build
            if t.no_move_attack and any(t.in_range(bb.distance(e.hex, h)) for e in self.enemies.values()):
                score += 8          # it can shoot from there next turn
            key = (score, -len(dirs))
            if best is None or key > best[0]:
                best = (key, dirs)
        spotted = False
        if best and best[1]:
            spotted = self.go(u, best[1])
        if self.can_capture(u):
            self.capture(u)
        return spotted

    def disk(self, h, radius):
        if radius not in self.disks:
            self.disks[radius] = bb.disk((0, 0), radius)
        q, r = h
        return [(q + dq, r + dr) for dq, dr in self.disks[radius]]

    def view(self, h, vision):
        """The hexes an observer on h sees. Concealing terrain is seen from
        that much closer only, but always from next door."""
        if not self.conceal:
            return self.disk(h, vision)
        if vision not in self.views:
            self.views[vision] = [(dq, dr, bb.distance((0, 0), (dq, dr))) for dq, dr in bb.disk((0, 0), vision)]
        q, r = h
        conceal = self.conceal
        return [n for dq, dr, d in self.views[vision] for n in [(q + dq, r + dr)]
                if d <= 1 or n not in conceal or d + conceal[n] <= vision]

    def unit_view(self, u, h=None):
        h = h or u.hex
        return self.view(h, self.types[u.type].vision + self.vbonus.get(h, 0))

    def sight(self, units):
        """The hexes our cities and the given units can see."""
        seen = set()
        for c in self.state.my_cities():
            seen.update(self.view(c.hex, self.rules.city_vision))
        for u in units:
            seen.update(self.unit_view(u))
        return seen

    def post_scouts(self, scouts):
        """Posts scouts where they show the most unseen ground around the
        army, out of the enemy's reach. With nothing to watch, they raid."""
        if not scouts:
            return
        known = [e.hex for e in self.enemies.values()] + [g.hex for g, _ in self.ghosts.values()]
        known += [c.hex for c in self.state.cities if c.owner not in (0, self.player)]
        army = [u for u in self.state.my_units() if not (self.types[u.type].skirmish and not self.types[u.type].capture)]
        front = [u for u in army if any(bb.distance(u.hex, k) <= 8 for k in known)]
        want = set()
        for u in front:
            want.update(self.disk(u.hex, 5))
        want -= self.sight(army)
        want &= set(self.defense)
        for u in scouts:
            if self.out_of_time():
                return
            t = self.types[u.type]
            best = None
            for h, dirs in self.reach(u).items():
                gain = sum(1 for v in self.unit_view(u, h) if v in want)
                key = (gain - 30 * self.risk(h, u.type), -len(dirs))
                if best is None or key > best[0]:
                    best = (key, h, dirs, gain)
            if not best or best[3] == 0:
                self.scout(u)
                continue
            if best[2]:
                self.go(u, best[2])
            want -= set(self.unit_view(u))

    def scout(self, u):
        """Scouts sit on an empty enemy city, which stops it building."""
        theirs = [c for c in self.state.cities if c.owner not in (0, self.player)]
        reach = self.reach(u, block_cities=self.limit == 1)
        best = None
        for c in theirs:
            if self.occ.get(c.hex, 0) > (u.hex == c.hex):
                continue    # someone is there already
            field = self.city_field[c.id]
            for h, dirs in reach.items():
                d = field.get(h)
                if d is None or (h in self.foreign_cities and h != c.hex):
                    continue
                key = (-d - 4 * self.cover.get(h, 0) + 12 * (h == c.hex), -len(dirs))
                if best is None or key > best[0]:
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

        sites = [c for c in sorted(s.my_cities(), key=urgency)
                 if not (c.built or c.hex in self.enemy_hex or self.occ.get(c.hex, 0) >= self.limit)]
        if self.last_turn:
            # Unspent gold counts for nothing; a unit counts for its cost.
            plans = {0: []}
            for _ in sites:
                for total, picks in list(plans.items()):
                    if len(picks) < len(sites):
                        for code, t in types.items():
                            if total + t.cost <= s.gold and total + t.cost not in plans:
                                plans[total + t.cost] = picks + [code]
            for city, code in zip(sites, plans[max(plans)]):
                self.build(city, code)
            return
        for city in sites:
            total = sum(count.values())
            can_capture = sum(n for code, n in count.items() if types[code].capture)
            options = [code for code, t in types.items() if t.cost <= s.gold and share[code] >= MIN_SHARE]
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
        self.cov_cache, self.cover_tab = {}, {}
        self.menace = {u.type: {} for u in state.my_units()}
        # On the last turn only the tiebreak counts: cities, then army value.
        self.last_turn = state.round >= self.rules.constants.get("round_limit", 1 << 30)
        if self.last_turn and self.player == 2:
            # Nothing can hit back after the final move of the game.
            self.loss_weight, self.expose, self.risk_weight = 1.0, 0.0, 0.0
        elif self.last_turn:
            # The enemy still has a turn, so give it as little to hit as possible.
            self.loss_weight, self.expose, self.risk_weight = 1.0, 4 * EXPOSE, 4 * RISK
        self.refresh()
        visible = self.sight(state.my_units())
        self.ghosts = {i: (g, seen) for i, (g, seen) in self.ghosts.items()
                       if i not in self.enemies and state.round - seen < GHOST and g.hex not in visible}
        try:
            self.capture_in_place()
            self.rush_cities()
            self.fight()
            self.refresh()
            self.rush_cities()      # the fight may have cleared a city
            self.advance()
        except bb.GameOver:
            raise
        except Exception:
            traceback.print_exc()   # a bug in the plan shouldn't cost the builds too
        for e in self.enemies.values():
            self.ghosts[e.id] = (bb.Unit(e.id, e.owner, e.type, e.q, e.r, e.hp), state.round)
        self.build_all()


if __name__ == "__main__":
    bb.run(VanguardBot())
