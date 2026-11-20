"""The raider sample bot: cavalry that picks its fights.

It builds two cavalry for every infantry. Infantry take cities, one unit
per city. Cavalry ride out ahead and hunt: they only make attacks that
gain more than they lose, they chase the units cavalry beats (everything
but pikemen), and a rider below half health goes back to a city to heal
before it fights again. With nothing in view, the riders head for the
enemy's cities to catch new units as they appear.

Usage: python bots/raider_bot.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kits", "python"))
import bitbath as bb  # noqa: E402

INF = float("inf")


class RaiderBot(bb.Bot):
    HEAL_BELOW = 50     # riders this hurt go home
    OPENING = "II"      # then BUILD_ORDER, over and over
    BUILD_ORDER = "KKI"

    def on_init(self):
        home = next(h for _, h, owner in self.map.cities if owner == self.player)
        self.enemy_home = self.map.mirror(home)
        self.city_ids = {h: cid for cid, h, _ in self.map.cities}
        # Worked out once, here, where the clock is generous.
        self.fields = {cid: self.distance_field([h], blocked=set()) for cid, h, _ in self.map.cities}
        self.builds = 0
        self.healing = set()

    def field(self, cid):
        """Move cost from every hex to a city."""
        return self.fields[cid]

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
        """Moves toward a field's goal, then captures or attacks if it can."""
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
        types = self.rules.unit_types
        units = sorted(state.my_units(), key=lambda u: u.id)
        for u in units:
            if self.can_capture(u):
                self.capture(u)

        own = [c.hex for c in state.my_cities()]
        home = self.distance_field(own) if own else {}
        riders = [u for u in units if not self.unit_type(u).capture]
        self.healing &= {u.id for u in riders}
        for u in riders:
            if u.hp < self.HEAL_BELOW:
                self.healing.add(u.id)
            elif u.hp >= self.unit_type(u).max_hp:
                self.healing.discard(u.id)

        # Fit riders strike first: they hit hardest and reach furthest.
        for u in sorted(units, key=lambda u: (self.unit_type(u).capture, u.id)):
            if u.id not in self.healing:
                self.strike(u)

        # Riders chase what they beat; pikemen are left alone.
        prey = [e.hex for e in state.enemy_units()
                if all(self.rules.damage[t][e.type] >= self.rules.damage[e.type][t]
                       for t in types if not types[t].capture and not types[t].skirmish)]
        if not prey:
            # Stand around enemy cities, not on them: the hex is for capturers.
            cities = [c.hex for c in state.cities if c.owner not in (0, me)] or [self.enemy_home]
            prey = [n for h in cities for n in bb.neighbors(h) if self.map.passable(n)]
        hunt = self.distance_field(prey)
        for u in riders:
            if u.id not in state.units:
                continue
            if u.id in self.healing:
                self.debug_hex(u.hex, "red", "heal")
                if u.hex in own:
                    self.strike(u, move=False)
                else:
                    self.walk(u, home)
            else:
                self.walk(u, hunt)

        # Infantry take the nearest cities, one each, and follow the riders
        # when there are no cities left over.
        targets = [c for c in state.cities if c.owner != me]
        takers = [u for u in units if u.id in state.units and self.can_act(u) and u.mp > 0
                  and self.unit_type(u).capture]
        pairs = sorted((self.field(c.id).get(u.hex, INF), u.id, c.id) for u in takers for c in targets)
        assigned, claimed = {}, set()
        for d, uid, cid in pairs:
            if d < INF and uid not in assigned and cid not in claimed:
                assigned[uid] = cid
                claimed.add(cid)
        for u in takers:
            self.walk(u, self.field(assigned[u.id]) if u.id in assigned else hunt)

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
    bb.run(RaiderBot())
