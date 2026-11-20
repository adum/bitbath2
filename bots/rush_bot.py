"""The rush sample bot: all in, straight away.

It builds nothing but infantry and sends them at the nearest enemy city as
fast as they will go. Every unit attacks whatever it can reach, whether or
not the trade is a good one, and picks the attack that does the most for
the least. One unit in three is a settler that takes the nearest city
instead, so that the rush has an income behind it. It never retreats and
never heals.

It tends to win fast or lose fast, which makes it a good test of whether
another bot can survive early pressure.

Usage: python bots/rush_bot.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kits", "python"))
import bitbath as bb  # noqa: E402

INF = float("inf")


class RushBot(bb.Bot):
    SETTLERS = 3  # one unit in this many takes cities instead of charging

    def on_init(self):
        home = next(h for _, h, owner in self.map.cities if owner == self.player)
        self.enemy_home = self.map.mirror(home)
        # Worked out once, here, where the clock is generous.
        self.fields = {cid: self.distance_field([h], blocked=set()) for cid, h, _ in self.map.cities}

    def field(self, cid):
        """Move cost from every hex to a city."""
        return self.fields[cid]

    def worth(self, utype, hp_lost, dies):
        return utype.cost * hp_lost / utype.max_hp + (utype.cost if dies else 0)

    def strike(self, unit, move=True):
        """Makes the best attack unit can make this turn, however bad. With
        move off, it attacks only from where it stands."""
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
        if best is None:
            return False
        _, e, h = best
        if h != unit.hex:
            self.move(unit, spots[h])
            if unit.hex != h:
                return False  # something hidden cut the move short
        return bool(self.attack(unit, e))

    def on_turn(self, state):
        me = self.player
        units = sorted(state.my_units(), key=lambda u: u.id)
        for u in units:
            if self.can_capture(u):
                self.capture(u)
        for u in units:
            self.strike(u)

        theirs = [c.hex for c in state.cities if c.owner not in (0, me)] or [self.enemy_home]
        charge = self.distance_field(theirs)
        targets = [c for c in state.cities if c.owner != me]
        for u in units:
            if u.id not in state.units or not self.can_act(u) or u.mp <= 0:
                continue
            field = charge
            if u.id % self.SETTLERS == 0 and self.unit_type(u).capture and targets:
                city = min(targets, key=lambda c: (self.field(c.id).get(u.hex, INF), c.id))
                field = self.field(city.id)
            self.move_toward(u, field)
            if self.can_capture(u):
                self.capture(u)
            else:
                self.strike(u, move=False)

        types = self.rules.unit_types
        code = "I" if "I" in types else min((t for t in types.values() if t.capture), key=lambda t: t.cost).code
        for city in state.my_cities():
            if city.built or state.enemy_at(city.hex) or state.friends_at(city.hex) >= self.rules.stack_limit:
                continue
            if types[code].cost > state.gold:
                break
            self.build(city, code)


if __name__ == "__main__":
    bb.run(RushBot())
