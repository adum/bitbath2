"""Bastion: a terrain-aware city racer with coordinated first strikes."""

from collections import Counter, defaultdict, deque
from functools import lru_cache
import heapq
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kits" / "python"))
import bitbath as bb

REPLY_WEIGHT = 0.1
SECOND_FRONT = 45
COLUMN = 22
FRONT_BIAS = 3
RESERVE = 20
TERRAIN_VALUE = 0.02
DENIAL = 25
ROUTE_RISK = 0.1


class Bastion(bb.Bot):
    def on_init(self):
        self.types = self.rules.unit_types
        self.line = max(
            (t for t in self.types.values() if t.capture),
            key=lambda t: t.move * t.max_hp / max(1, t.cost),
            default=min(self.types.values(), key=lambda t: t.cost),
        )
        self.tiles = {h: self.map.terrain(h) for h in self.map.hexes() if self.map.passable(h)}
        self.edges = {
            h: [
                (n, self.tiles[n].move_cost, str(d))
                for d in range(6)
                for n in [bb.step(h, d)]
                if n in self.tiles
            ]
            for h in self.tiles
        }
        self.city = {h: i for i, h, _ in self.map.cities}
        self.compact = len(self.city) <= 6
        # With fewer cities or a tempo disadvantage, cover counterattacks sooner.
        early_defense = self.compact or (self.player == 2 and len(self.city) <= 16)
        self.front_threshold = 8 if early_defense else SECOND_FRONT
        self.column_size = 12 if early_defense else COLUMN
        self.fields = {h: self.distance_field([h], blocked=set()) for h in self.city}
        # On rough ground two 2-MP steps cost two infantry turns, just as
        # two 3-MP steps do. Measure arrivals in whole legal turns.
        reverse = defaultdict(list)
        for h in self.tiles:
            for dest in self.walk(h, self.line.move, (), ()):
                if dest != h:
                    reverse[dest].append(h)
        self.turns = {}
        for city in self.city:
            dist = {city: 0}
            queue = deque([city])
            while queue:
                h = queue.popleft()
                for prev in reverse[h]:
                    if prev not in dist:
                        dist[prev] = dist[h] + 1
                        queue.append(prev)
            self.turns[city] = dist
        self.memory = {}
        self.axis = None
        neutral = [h for _, h, owner in self.map.cities if not owner]
        home = next(h for _, h, owner in self.map.cities if owner == self.player)
        rival = next(h for _, h, owner in self.map.cities if owner not in (0, self.player))
        self.own_forecast = self.capture_schedule(home, neutral)
        self.rival_forecast = self.capture_schedule(rival, neutral)
        self.natural = {
            h for h in neutral if self.turns[h].get(home, 10000) <= self.turns[h].get(rival, 10000)
        }
        self.block_goal = None
        plausible = [
            h
            for h in neutral
            if self.turns[h].get(home, 10000) / 2 <= self.rival_forecast[h] + 1
            and self.rival_forecast[h] <= self.own_forecast[h]
        ]
        if plausible:
            self.block_goal = min(
                plausible, key=lambda h: self.rival_forecast[h] + 0.35 * self.turns[h].get(home, 10000)
            )

    def capture_schedule(self, base, cities):
        """Forecast an uncontested expansion from travel times and local recruitment."""
        units = [(base, 0)] * 2 + [(base, r) for r in range(1, 9)]
        times = {}
        while len(times) < len(cities):
            options = []
            for c in cities:
                if c in times:
                    continue
                arrivals = sorted(
                    (start + max(1, self.turns[c].get(h, 10000)), i) for i, (h, start) in enumerate(units)
                )
                eta, first = arrivals[0]
                due = eta + (self.rules.capture_points - 1) // self.line.max_hp
                group = [first]
                if self.rules.stack_limit >= 2 and arrivals[1][0] < due:
                    due = arrivals[1][0] + (self.rules.capture_points - 1) // (2 * self.line.max_hp)
                    group.append(arrivals[1][1])
                options.append((due, eta, c, group))
            due, _, c, group = min(options)
            times[c] = due
            units = [u for i, u in enumerate(units) if i not in group]
            units.extend([(c, due)] * (len(group) + 1))
            units.extend((c, due + i) for i in (1, 2, 3))
        return times

    def opening_orders(self, free, neutral):
        # Evaluate city chains with a virtual stream of future home recruits.
        # A contested city is worth both our income and the income denied.
        if not free:
            return {}
        neutral = sorted(neutral, key=lambda c: self.own_forecast[c.hex])[:10]
        if not neutral:
            return {}
        army = [(u.hex, 0, u.hp, u.id) for u in free.values()]
        for c in self.state.my_cities():
            army.extend((c.hex, r, self.line.max_hp, 0) for r in (1, 2, 3, 4))
        beam = [(0.0, army, neutral, {})]
        for _ in range(min(len(neutral), 6)):
            if self.time_left() < 350:
                break
            candidates = []
            for value, units, sites, orders in beam:
                for c in sites:
                    arrivals = sorted(
                        (start + max(1, self.turns[c.hex].get(h, 10000)), i)
                        for i, (h, start, _, _) in enumerate(units)
                    )
                    need = self.rules.capture_points - (c.progress if c.capturer == self.player else 0)
                    eta, i = arrivals[0]
                    party = [i]
                    due = eta + (need - 1) // units[i][2]
                    for count in range(min(len(units), self.rules.stack_limit, 2)):
                        if count:
                            join, j = arrivals[1]
                            if join >= due:
                                break
                            due = join + (need - 1 - (join - eta) * units[i][2]) // (
                                units[i][2] + units[j][2]
                            )
                            party.append(j)
                        rival = self.rival_forecast[c.hex]
                        date = self.state.round + due - 1
                        ahead = rival - date + (0.3 if self.player == 1 else -0.3)
                        probability = max(0.08, min(1, 0.65 + 0.19 * ahead))
                        stolen = self.own_forecast[c.hex] >= rival
                        gain = (0.81**due + (0.60 if stolen else 0.28) * 0.96**due) * probability
                        later = [v for n, v in enumerate(units) if n not in party]
                        later.extend([(c.hex, due, self.line.max_hp, 0)] * (len(party) + 1))
                        later.append((c.hex, due + 1, self.line.max_hp, 0))
                        plan = orders.copy()
                        for n in party:
                            if units[n][3]:
                                plan[units[n][3]] = c
                        candidates.append((value + gain, later, [g for g in sites if g != c], plan))
            if not candidates:
                break
            beam = sorted(candidates, key=lambda node: -node[0])[:8]
        return beam[0][3]

    def walk(self, start, mp, blocked, stops):
        paths = {start: ""}
        costs = {start: 0}
        heap = [(0, start)]
        while heap:
            spent, h = heapq.heappop(heap)
            if spent != costs[h] or (h != start and h in stops):
                continue
            for n, cost, direction in self.edges[h]:
                total = cost + spent
                if total <= mp and total < costs.get(n, mp + 1) and n not in blocked:
                    costs[n] = total
                    paths[n] = paths[h] + direction
                    heapq.heappush(heap, (total, n))
        return paths

    @lru_cache(maxsize=24000)
    def sight(self, h, radius):
        return frozenset(self.map.sight(h, radius))

    @lru_cache(maxsize=24000)
    def firing_ring(self, code, h):
        t = self.types[code]
        return tuple(n for r in range(t.range_min, t.range_max + 1) for n in bb.ring(h, r) if n in self.tiles)

    def update(self):
        self.army = self.state.my_units()
        self.foes = self.state.enemy_units()
        self.occ = Counter(u.hex for u in self.army)
        self.blocked = {u.hex for u in self.foes}
        self.stops = {n for u in self.foes if self.types[u.type].zoc for n in bb.neighbors(u.hex)}
        reach_key = self.blocked, self.stops
        if getattr(self, "reach_key", None) != reach_key:
            self.reaches = {}
            self.reach_key = reach_key

    def reach(self, u):
        key = u.hex, u.mp, self.types[u.type].skirmish
        if key not in self.reaches:
            self.reaches[key] = self.walk(u.hex, u.mp, self.blocked, () if key[2] else self.stops)
        return self.reaches[key]

    def go(self, u, path):
        if not path:
            return False
        old, count = u.hex, len(self.state.units)
        reply = self.move(u, path)
        if count != len(self.state.units):
            self.update()
            return True
        self.occ[old] -= 1
        if not self.occ[old]:
            del self.occ[old]
        self.occ[u.hex] += 1
        return reply.kind != "OK"

    def influence(self):
        """Reachable strikes including recent sightings, discarded when the hex is cleared."""
        seen = set()
        for c in self.state.my_cities():
            seen.update(self.sight(c.hex, self.rules.city_vision))
        for u in self.army:
            seen.update(self.sight(u.hex, self.types[u.type].vision + self.tiles[u.hex].vision_bonus))
        turn = self.state.round
        self.memory = {
            i: (e, age) for i, (e, age) in self.memory.items() if turn - age <= 5 and e.hex not in seen
        }
        for e in self.foes:
            self.memory[e.id] = (e, turn)
        self.danger = Counter()
        self.attackers = defaultdict(list)
        self.spotted = set()
        friendly_zoc = {n for u in self.army if self.types[u.type].zoc for n in bb.neighbors(u.hex)}
        for e, last in self.memory.values():
            t = self.types[e.type]
            positions = (
                [e.hex]
                if t.no_move_attack
                else self.walk(e.hex, t.move, self.occ, () if t.skirmish else friendly_zoc)
            )
            hits = {}
            for h in positions:
                for n in self.firing_ring(e.type, h):
                    if n not in hits or self.tiles[h].defense > self.tiles[hits[n]].defense:
                        hits[n] = h
            strength = (
                self.rules.damage[e.type][self.line.code] / 55 * e.hp / t.max_hp * 0.78 ** (turn - last)
            )
            for n in hits:
                self.danger[n] += strength
                self.attackers[n].append((e, hits[n], 0.78 ** (turn - last)))
            self.spotted.update(self.sight(e.hex, t.vision + self.tiles[e.hex].vision_bonus))
        for c in self.state.cities:
            if c.owner not in (0, self.player):
                self.spotted.update(self.sight(c.hex, self.rules.city_vision))
                if c.hex not in seen:
                    # An unseen production city can have a fresh defender next turn.
                    for h in self.walk(c.hex, self.line.move, self.occ, friendly_zoc):
                        for n in self.firing_ring(self.line.code, h):
                            self.danger[n] = max(self.danger[n], 0.85)
        self.seen = seen

    def scout(self):
        """Reveal possible staging areas before choosing the army's attacks."""
        scouts = [
            u
            for u in self.army
            if self.types[u.type].skirmish and not self.types[u.type].capture and not u.acted
        ]
        sites = [c.hex for c in self.state.cities if c.owner != self.player]
        if not sites or not scouts:
            return
        weights = Counter()
        for u in self.army:
            if self.types[u.type].skirmish:
                continue
            for h in bb.disk(u.hex, 4):
                if h in self.tiles and h not in self.seen:
                    weights[h] += 1
        for u in scouts:
            t = self.types[u.type]
            if (
                u.hex in self.city
                and self.state.cities[self.city[u.hex]].owner != self.player
                and self.rules.stack_limit > 1
            ):
                continue
            best, path = -float("inf"), ""
            enemy_cities = [c for c in self.state.cities if c.owner not in (0, self.player)]
            expected = {
                c.hex: min((self.turns[c.hex].get(e.hex, 1000) for e in enemy_cities), default=1000)
                for c in self.state.cities
                if not c.owner
            }
            mine = {
                c.hex: min(
                    (self.turns[c.hex].get(f.hex, 1000) for f in self.army if self.types[f.type].capture),
                    default=1000,
                )
                for c in self.state.cities
                if not c.owner
            }
            for h, p in self.reach(u).items():
                if p and self.occ[h] >= self.rules.stack_limit:
                    continue
                sight = self.sight(h, t.vision + self.tiles[h].vision_bonus)
                score = (
                    sum(min(4, weights[n]) for n in sight) / 5
                    - 10 * self.risk(h)
                    - 0.6 * min(self.fields[g].get(h, 10000) for g in sites)
                )
                if h in expected and expected[h] <= mine[h] + 1 and self.occ[h] - int(h == u.hex) == 0:
                    score += DENIAL / (1 + 0.25 * expected[h])
                if h in self.city and self.state.cities[self.city[h]].owner not in (0, self.player):
                    score += DENIAL
                if self.block_goal and not self.state.cities[self.city[self.block_goal]].owner:
                    score += 70 * (h == self.block_goal) - 2 * self.fields[self.block_goal].get(h, 10000)
                if score > best:
                    best, path = score, p
            self.go(u, path)
            self.influence()

    def risk(self, h):
        return min(3.5, self.danger[h]) * (1 - self.tiles[h].defense / 100)

    def capture_ready(self):
        for u in self.army:
            if self.can_capture(u):
                self.capture(u)

    def seize(self):
        """Reserve immediate captures before spending the same units on trades."""
        for c in sorted(self.state.cities, key=lambda c: c.owner == 0):
            if c.owner == self.player or c.hex in self.blocked:
                continue
            units = [
                u
                for u in self.army
                if not u.acted
                and not u.fresh
                and self.types[u.type].capture
                and bb.distance(c.hex, u.hex) <= u.mp
                and c.hex in self.reach(u)
            ]
            units.sort(key=lambda u: (-u.hp, self.turns[c.hex].get(u.hex, 10000)))
            total = sum(u.hp for u in units[: max(0, self.rules.stack_limit - self.occ[c.hex])])
            progress = c.progress if c.capturer == self.player else 0
            if total + progress < self.rules.capture_points and self.risk(c.hex) > 1.2:
                continue
            for u in units:
                if (
                    c.owner == self.player
                    or self.occ[c.hex] >= self.rules.stack_limit
                    or c.hex in self.blocked
                ):
                    break
                path = self.reach(u).get(c.hex)
                if path:
                    self.go(u, path)
                if u.hex == c.hex and self.can_capture(u):
                    self.capture(u)

    def counterplay(self, group, target, remaining, commit):
        """Play a local enemy reply. Each surviving enemy spends at most one attack."""
        defenders = []
        options = defaultdict(list)
        for u, h, _, _ in group:
            position = h if commit else u.hex
            defenders.append(u.hp)
            for e, at, confidence in self.attackers[position]:
                hp = remaining if e.id == target.id else e.hp
                if hp <= 0 or (confidence == 1 and e.id not in self.state.units):
                    continue
                damage = self.damage(e.type, hp, u.type, position)
                options[e.id].append((len(defenders) - 1, damage, e, hp, at, confidence))
        loss = 0
        # Single-target attackers first; flexible ones can finish survivors.
        for available in sorted(options.values(), key=len):
            best, chosen = 0, None
            for i, damage, e, hp, at, confidence in available:
                if defenders[i] <= 0:
                    continue
                u, h, _, _ = group[i]
                h = h if commit else u.hex
                t = self.types[u.type]
                after = max(0, defenders[i] - damage)
                gain = (defenders[i] - after + 20 * (after == 0)) * t.cost / t.max_hp
                if after and t.in_range(bb.distance(h, at)):
                    back = min(hp, self.damage(u.type, after, e.type, at))
                    gain -= 0.8 * back * self.types[e.type].cost / self.types[e.type].max_hp
                if gain * confidence > best:
                    best, chosen = gain * confidence, (i, after)
            if chosen:
                i, after = chosen
                defenders[i] = after
                loss += best
        return loss

    def attack_plans(self):
        plans = defaultdict(list)
        for u in self.army:
            if u.acted or u.fresh or u.id in self.failed:
                continue
            t = self.types[u.type]
            if t.no_move_attack and u.moved:
                continue
            reach = {u.hex: ""} if t.no_move_attack else self.reach(u)
            for e in self.foes:
                if bb.distance(u.hex, e.hex) > (0 if t.no_move_attack else u.mp) + t.range_max:
                    continue
                options = [
                    h
                    for h in self.firing_ring(u.type, e.hex)
                    if h in reach and (h == u.hex or self.occ[h] < self.rules.stack_limit)
                ]
                if (
                    t.skirmish
                    and u.hex in self.city
                    and self.rules.stack_limit > 1
                    and self.state.cities[self.city[u.hex]].owner != self.player
                ):
                    options = [h for h in options if h == u.hex]
                if not options:
                    continue

                def safety(h):
                    return self.tiles[h].defense - 26 * self.risk(h) - 0.1 * len(reach[h])

                h = max(options, key=safety)
                hit = self.damage(u.type, u.hp, e.type, e.hex)
                plans[e.id].append((u, h, reach[h], hit))
        for group in plans.values():
            group.sort(key=lambda a: (-a[3], self.risk(a[1]), a[0].id))
        return plans

    def fight(self):
        plans = self.attack_plans()
        changed = False
        while plans and self.time_left() > 120:
            choices = []
            for tid, group in plans.items():
                e = self.state.units.get(tid)
                if not e:
                    continue
                target = self.types[e.type]
                hp = e.hp
                group = [
                    a
                    for a in group
                    if not a[0].acted
                    and a[0].hp > 0
                    and a[0].id not in self.failed
                    and (a[1] == a[0].hex or self.occ[a[1]] < self.rules.stack_limit)
                ]
                value = 0
                for count, (u, h, path, hit) in enumerate(group, 1):
                    t = self.types[u.type]
                    value += min(hit, hp) * target.cost / target.max_hp
                    hp -= hit
                    if hp > 0 and target.in_range(bb.distance(h, e.hex)):
                        back = self.damage(e.type, hp, u.type, h)
                        value -= (min(u.hp, back) + 30 * (back >= u.hp)) * t.cost / t.max_hp
                    if hp <= 0:
                        value += 65 + 25 * (e.hex in self.city)
                    score = value / count
                    if score > 0:
                        choices.append((score, group[:count], e, max(0, hp)))
                    if hp <= 0:
                        break
            best, action = 0, None
            for raw, group, e, left in sorted(choices, key=lambda option: -option[0])[:16]:
                reply = self.counterplay(group, e, left, True)
                baseline = self.counterplay(group, e, e.hp, False)
                last_move = self.state.round == self.rules.round_limit and self.player == 2
                score = raw if last_move else raw - REPLY_WEIGHT * (reply - baseline) / len(group)
                if score > best:
                    best, action = score, (group[0], e)
            if not action:
                if changed:
                    plans = self.attack_plans()
                    changed = False
                    continue
                return
            (u, h, path, _), e = action
            revealed = self.go(u, path)
            count = len(self.state.units)
            if u.hex != h or not self.attack(u, e):
                self.failed.add(u.id)
            if count != len(self.state.units):
                self.update()
            changed = True
            if revealed:
                self.influence()
                plans = self.attack_plans()
                changed = False

    def turns_to(self, city, origin):
        return self.turns[city.hex].get(origin, 10000)

    def assign_fronts(self, assignment, free, homes, rival, home_eta, enemy_eta):
        dist = self.turns_to
        garrisons = set()
        if len(self.army) >= self.front_threshold and rival:
            # Reinforce each threatened city with local troops before choosing
            # objectives for the field army. A distant column can't cover a city.
            threats = Counter()
            for e, last in self.memory.values():
                if e.hp <= 0:
                    continue
                c = min(homes, key=lambda c: dist(c, e.hex))
                eta = dist(c, e.hex)
                if eta <= 5:
                    threats[c.id] += (e.hp / self.types[e.type].max_hp) * 0.8 ** (self.state.round - last)
            for c in sorted(homes, key=lambda c: -threats[c.id]):
                quota = min(24, int(1.4 * threats[c.id])) + int(enemy_eta[c.id] <= 3)
                if not quota:
                    continue
                nearby = sorted(
                    (u for u in free.values() if dist(c, u.hex) <= 5), key=lambda u: dist(c, u.hex)
                )
                for u in nearby[:quota]:
                    assignment[u.id] = c
                    garrisons.add(u.id)
                    del free[u.id]
            loads = Counter()
            while free:
                group_size = min(self.column_size, len(free))

                def front_cost(c):
                    distances = sorted(dist(c, u.hex) for u in free.values())[:group_size]
                    return sum(distances) / len(distances) + 5 * loads[c.id] + 0.35 * home_eta[c.id]

                target = min(rival, key=front_cost)
                loads[target.id] += 1
                for u in sorted(free.values(), key=lambda u: dist(target, u.hex))[:group_size]:
                    assignment[u.id] = target
                    del free[u.id]
        elif rival and free:

            def attack_cost(c):
                distances = sorted(dist(c, u.hex) for u in free.values())
                group = distances[: max(2, len(distances) // 2)]
                defenders = sum(
                    e.hp / 100 / (1 + dist(c, e.hex) ** 2) for e, last in self.memory.values() if e.hp > 0
                )
                return (
                    sum(group) / len(group) + 0.6 * home_eta[c.id] - 2 * (c.id == self.axis) + 0.7 * defenders
                )

            target = min(rival, key=attack_cost)
            self.axis = target.id
            assignment.update((u.id, target) for u in free.values())
        return garrisons

    def routes(self, goal):
        """Reverse movement costs including currently visible threats and occupied hexes."""
        costs = {goal.hex: 0}
        queue = [(0, goal.hex)]
        infinity = float("inf")
        while queue:
            value, h = heapq.heappop(queue)
            if value != costs[h]:
                continue
            step = self.tiles[h].move_cost + ROUTE_RISK * min(4, self.danger[h])
            if h in self.blocked:
                step += 12
            for n, _, _ in self.edges[h]:
                v = value + step
                if v < costs.get(n, infinity):
                    costs[n] = v
                    heapq.heappush(queue, (v, n))
        return costs

    def march(self):
        homes = self.state.my_cities()
        foreign = [c for c in self.state.cities if c.owner != self.player]
        if not foreign:
            return
        dist = self.turns_to
        rival = [c for c in foreign if c.owner]
        home_eta = {c.id: min(dist(c, h.hex) for h in homes) for c in self.state.cities}
        enemy_eta = {c.id: min((dist(c, r.hex) for r in rival), default=100) for c in self.state.cities}
        ours = [
            c
            for c in self.state.cities
            if c.owner == self.player or (not c.owner and home_eta[c.id] <= enemy_eta[c.id])
        ]
        theirs = [c for c in foreign if c not in ours] or foreign
        front = {c.id: min(dist(e, c.hex) for e in theirs) for c in ours}
        # Rear factories ship their army forward. A short walk to a safe
        # factory is never a reason to station an entire army there.
        edge = min(front.values())
        posts = [c for c in ours if front[c.id] <= edge + 2 or self.danger[c.hex]]
        backing = Counter()
        for e, last in self.memory.values():
            post = min(posts, key=lambda c: dist(c, e.hex))
            backing[post.id] += 0.8 ** (self.state.round - last) / (1 + 0.2 * dist(post, e.hex))
        safe_fields = {}

        movers = [
            u
            for u in self.army
            if not u.acted and not u.fresh and u.id not in self.failed and not self.types[u.type].skirmish
        ]
        free = {u.id: u for u in movers if self.types[u.type].capture}
        neutral = [c for c in foreign if not c.owner and c.hex not in self.blocked]
        assignment = self.opening_orders(free, neutral)
        free = {uid: u for uid, u in free.items() if uid not in assignment}
        claim = [c for c in neutral if home_eta[c.id] <= enemy_eta[c.id] + 1]
        for c in sorted(claim, key=lambda c: enemy_eta[c.id]):
            if enemy_eta[c.id] > home_eta[c.id] + 4:
                continue
            escort = sorted(free.values(), key=lambda u: dist(c, u.hex))
            for u in escort[: max(1, int(self.danger[c.hex]))]:
                assignment[u.id] = c
                del free[u.id]

        load = Counter()
        garrisons = self.assign_fronts(assignment, free, homes, rival, home_eta, enemy_eta)
        for u in sorted(movers, key=lambda u: (u.id not in assignment, u.id)):
            if u.acted or u.id not in self.state.units:
                continue
            if self.time_left() < 110:
                return
            t = self.types[u.type]
            options = self.reach(u)

            def objective(c):
                pressure = min(5, self.danger[c.hex])
                return (
                    dist(c, u.hex)
                    + FRONT_BIAS * front.get(c.id, 0)
                    - pressure
                    + 4 * load[c.id] / (3 + backing[c.id])
                )

            goal = assignment.get(u.id)
            if goal is None:
                urgent = [c for c in claim if c.hex in self.natural]
                goal = min(urgent or posts, key=objective)
                # From a staging city, pressure its closest enemy production.
                if (
                    dist(goal, u.hex) <= 2
                    and goal.owner == self.player
                    and self.danger[goal.hex] < 1
                    and not claim
                    and (not self.hold or len(self.army) > RESERVE * len(homes))
                ):
                    goal = min(theirs, key=lambda c: dist(c, goal.hex) + 0.5 * dist(c, u.hex))
            if u.hp < t.max_hp * 0.75 and homes:
                goal = min(homes, key=lambda c: dist(c, u.hex) + 1.5 * self.risk(c.hex))
            load[goal.id] += 1
            if goal.hex not in safe_fields:
                safe_fields[goal.hex] = self.routes(goal)
            route = safe_fields[goal.hex]
            best, path = -float("inf"), ""
            for h, p in options.items():
                if p and self.occ[h] >= self.rules.stack_limit:
                    continue
                tile = self.tiles[h]
                score = (
                    -2 * dist(goal, h)
                    - 1.4 * route.get(h, 10000)
                    + tile.defense * (TERRAIN_VALUE if self.hold or self.danger[h] else 0.025)
                    + (2 if tile.conceal and h not in self.spotted else 0)
                )
                if t.no_move_attack:
                    targets = sum(self.enemy_counts[n] for n in self.firing_ring(t.code, h))
                    score += 12 * min(3, targets) - 12 * self.risk(h)
                if self.defending and goal.owner != self.player:
                    # On a settled short map, army value decides a city tie.
                    # Make the enemy cross the open ground to engage.
                    score -= 15 * self.risk(h)
                if not goal.owner and goal.hex in self.natural:
                    score -= 3 * self.fields[goal.hex].get(h, 10000)
                if h in self.city:
                    c = self.state.cities[self.city[h]]
                    if c.owner != self.player:
                        if t.capture:
                            score += 35 + 12 * (c.owner != 0) + 8 * (c.progress > 0)
                        elif self.occ[h] - (h == u.hex):
                            score -= 35
                    else:
                        score += min(t.max_hp - u.hp, self.rules.city_heal) * 0.7
                        if self.occ[h] - (h == u.hex) >= self.rules.stack_limit - 1:
                            score -= 12
                if u.hex in self.city and h != u.hex and self.occ[u.hex] == 1:
                    c = self.state.cities[self.city[u.hex]]
                    if u.id in garrisons and c.owner == self.player and self.danger[u.hex] >= 1:
                        score -= 35
                    if (
                        c.owner == self.player
                        and u.id not in assignment
                        and (self.danger[u.hex] > 0.7 or self.hold)
                    ):
                        score -= 40
                if score > best:
                    best, path = score, p
            revealed = self.go(u, path)
            if self.can_capture(u):
                self.capture(u)
            if revealed and self.time_left() > 200:
                self.influence()
                self.fight()

    def recruit(self):
        foreign = [c for c in self.state.cities if c.owner != self.player]
        home_count = len(self.state.my_cities())
        horses = [t for t in self.types.values() if t.zoc and t.move > self.line.move]
        heavy = max(horses, key=lambda t: t.move, default=None)
        reserve = (
            self.hold and len(self.army) > 7 * home_count and self.state.round > self.rules.round_limit * 0.7
        )
        horse_count = sum(heavy is not None and u.type == heavy.code for u in self.army)

        def urgency(c):
            distance = min((self.turns[f.hex].get(c.hex, 10000) for f in foreign), default=0)
            return distance - min(7, self.danger[c.hex]) - 2 * (self.occ[c.hex] == 0)

        for c in sorted(self.state.my_cities(), key=urgency):
            if self.state.gold < self.line.cost or self.time_left() < 40:
                break
            if not c.built and self.occ[c.hex] < self.rules.stack_limit and c.hex not in self.blocked:
                code = self.line.code
                if (
                    heavy
                    and self.state.gold >= heavy.cost
                    and (
                        (reserve and horse_count * 5 < len(self.army))
                        or (self.state.round == 1 and self.compact)
                        or self.state.round == self.rules.round_limit
                    )
                ):
                    code = heavy.code
                if self.build(c, code):
                    horse_count += bool(heavy and code == heavy.code)

    def on_turn(self, state):
        self.failed = set()
        self.update()
        self.update_memory_from_log(state)
        self.hold = len(state.my_cities()) * 2 >= len(state.cities)
        self.defending = (
            self.compact and self.hold and state.round >= 12 and all(c.owner for c in state.cities)
        )
        self.influence()
        self.scout()
        self.capture_ready()
        self.seize()
        self.fight()
        self.seize()
        self.influence()
        self.enemy_counts = Counter(e.hex for e in self.foes)
        self.march()
        self.fight()
        self.recruit()

    def update_memory_from_log(self, state):
        for log in state.logs:
            if log[0] == "HIT":
                _, uid, code, q, r, _, _, hp = log
                uid, hp = int(uid), int(hp)
                if hp > 0:
                    self.memory[uid] = (
                        bb.Unit(uid, 3 - self.player, code, int(q), int(r), hp),
                        state.round - 1,
                    )
                else:
                    self.memory.pop(uid, None)


if __name__ == "__main__":
    bb.run(Bastion())
