"""The volley sample bot: archer packs.

It builds two archers for every infantry. Infantry take cities, one unit
per city. Archers gather into a pack before they go anywhere: the archer
nearest the enemy is the point, the others walk to it, and the pack only
advances once enough of them stand together. Archers shoot from 2 hexes
away, where melee units can't strike back, and step back out of contact to
do it. Infantry with no city to take walk with the pack as its screen.

Usage: python bots/volley_bot.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kits", "python"))
import bitbath as bb  # noqa: E402

INF = float("inf")


class VolleyBot(bb.Bot):
    PACK = 3            # archers that must stand together before a pack advances
    OPENING = "II"      # then BUILD_ORDER, over and over
    BUILD_ORDER = "AAI"

    def on_init(self):
        home = next(h for _, h, owner in self.map.cities if owner == self.player)
        self.enemy_home = self.map.mirror(home)
        self.city_ids = {h: cid for cid, h, _ in self.map.cities}
        # Worked out once, here, where the clock is generous.
        self.fields = {cid: self.distance_field([h], blocked=set()) for cid, h, _ in self.map.cities}
        self.builds = 0

    def field(self, cid):
        """Move cost from every hex to a city."""
        return self.fields[cid]

    def ranged(self, unit):
        return self.unit_type(unit).range_min > 1

    def foreign(self, h):
        """Whether h is a city we don't own."""
        cid = self.city_ids.get(h)
        return cid is not None and self.state.cities[cid].owner != self.player

    def worth(self, utype, hp_lost, dies):
        return utype.cost * hp_lost / utype.max_hp + (utype.cost if dies else 0)

    def strike(self, unit, move=True):
        """Makes the best attack unit can make this turn, if it gains more
        than it loses. With move off, it attacks only from where it stands."""
        s = self.state
        if unit.id not in s.units or not self.can_act(unit):
            return False
        t = self.unit_type(unit)
        if t.no_move_attack and unit.moved:
            return False
        move = move and unit.mp > 0 and not t.no_move_attack
        span = t.range_max + (unit.mp if move else 0)
        near = [e for e in s.enemy_units() if bb.distance(unit.hex, e.hex) <= span]
        if not near:
            return False
        spots = {unit.hex: ""}
        if move:
            spots.update(self.reachable(unit))
        best = None
        for e in near:
            et = self.unit_type(e)
            for r in range(t.range_min, t.range_max + 1):
                for h in bb.ring(e.hex, r):
                    if h not in spots:
                        continue
                    def_hp, att_hp = self.predict(unit, e, h)
                    score = (self.worth(et, e.hp - def_hp, def_hp == 0)
                             - self.worth(t, unit.hp - att_hp, att_hp == 0))
                    key = (score, self.map.terrain(h).defense, -len(spots[h]))
                    if best is None or key > best[0]:
                        best = (key, e, h)
        if best is None or best[0][0] <= 0:
            return False
        _, e, h = best
        if h != unit.hex:
            self.move(unit, spots[h])
            if unit.hex != h:
                return False  # something hidden cut the move short
        return bool(self.attack(unit, e))

    def walk(self, unit, field):
        """Moves toward a field's goal, then captures or shoots if it can."""
        if unit.id not in self.state.units or not self.can_act(unit) or unit.mp <= 0:
            return
        # A unit that can't capture keeps off cities that aren't ours, so
        # that it never blocks the units that can.
        reach = self.reachable(unit)
        here = field.get(unit.hex, INF)
        if not self.unit_type(unit).capture:
            reach = {h: d for h, d in reach.items() if not self.foreign(h)}
            if self.foreign(unit.hex):
                here = INF
        spot = min(reach, key=lambda h: (field.get(h, INF), len(reach[h]), h), default=None)
        if spot is not None and field.get(spot, INF) < here:
            self.move(unit, reach[spot])
        if self.can_capture(unit):
            self.capture(unit)
        else:
            self.strike(unit, move=False)

    def on_turn(self, state):
        me = self.player
        units = sorted(state.my_units(), key=lambda u: u.id)
        for u in units:
            if self.can_capture(u):
                self.capture(u)
        # Archers shoot first, so that melee units finish what they weaken.
        for u in sorted(units, key=lambda u: (not self.ranged(u), u.id)):
            self.strike(u)

        # Where the war is: enemies in view, else enemy cities.
        goals = [e.hex for e in state.enemy_units()]
        if not goals:
            # Stand around enemy cities, not on them: the hex is for capturers.
            cities = [c.hex for c in state.cities if c.owner not in (0, me)] or [self.enemy_home]
            goals = [n for h in cities for n in bb.neighbors(h) if self.map.passable(n)]
        hunt = self.distance_field(goals)

        archers = [u for u in units if u.id in state.units and self.ranged(u)]
        point = None
        if archers:
            lead = min(archers, key=lambda u: (hunt.get(u.hex, INF), u.id))
            point = lead.hex
            pack = [u for u in archers if bb.distance(u.hex, point) <= 1]
            formed = len(pack) >= min(self.PACK, len(archers) + (0 if self.builds < 6 else 3))
            self.debug_hex(point, "green" if formed else "yellow", "pack %d" % len(pack))
            muster = self.distance_field([point], blocked=set())
            for u in archers:
                if u in pack:
                    if formed:
                        self.walk(u, hunt)
                else:
                    self.walk(u, muster)

        # Infantry take the nearest cities, one each; the rest screen the pack.
        targets = [c for c in state.cities if c.owner != me]
        takers = [u for u in units if u.id in state.units and self.can_act(u) and u.mp > 0
                  and self.unit_type(u).capture]
        pairs = sorted((self.field(c.id).get(u.hex, INF), u.id, c.id) for u in takers for c in targets)
        assigned, claimed = {}, set()
        for d, uid, cid in pairs:
            if d < INF and uid not in assigned and cid not in claimed:
                assigned[uid] = cid
                claimed.add(cid)
        for u in units:
            if u.id not in state.units or self.ranged(u):
                continue
            if u.id in assigned:
                self.walk(u, self.field(assigned[u.id]))
            else:
                self.walk(u, muster if point else hunt)

        types = self.rules.unit_types
        for city in state.my_cities():
            if city.built or state.enemy_at(city.hex) or state.friends_at(city.hex) >= self.rules.stack_limit:
                continue
            n = self.builds
            code = self.OPENING[n] if n < len(self.OPENING) else self.BUILD_ORDER[(n - len(self.OPENING)) % len(self.BUILD_ORDER)]
            if code not in types:
                code = "I"
            if code not in types or types[code].cost > state.gold:
                break  # save up for the next unit in the order
            if self.build(city, code):
                self.builds += 1


if __name__ == "__main__":
    bb.run(VolleyBot())
