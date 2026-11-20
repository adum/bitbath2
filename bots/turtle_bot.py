"""The turtle sample bot: as defensive as it gets.

It claims the cities on its own half of the map and nothing else. Every
claimed city gets a garrison of pikemen and archers, half and half, up to
the stack limit. Pikemen dig in, so a garrison that holds still is hard to
shift. Units at their post only attack what comes into range; units on the
way to a post also strike enemies they can reach.

Once every post is full, further units form a reserve, as large as the
cities' income can keep. The reserve waits
at the claimed city nearest the enemy and goes after any enemy that enters
the bot's half of the map, then comes back.

It never crosses the middle, so it can't eliminate anyone: it wins, when it
does, on the round-limit tiebreak or by wearing attackers down.

Usage: python bots/turtle_bot.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kits", "python"))
import bitbath as bb  # noqa: E402

INF = float("inf")


class TurtleBot(bb.Bot):
    SPEND = 0.8  # the share of income that upkeep may eat

    def on_init(self):
        home = next(h for _, h, owner in self.map.cities if owner == self.player)
        away = self.map.mirror(home)
        # The cities nearer to our home than to the enemy's.
        self.ours = lambda h: bb.distance(h, home) < bb.distance(h, away)
        self.claims = [cid for cid, h, _ in self.map.cities if self.ours(h)]
        self.front = min(self.claims, key=lambda c: (bb.distance(self.map.cities[c][1], away), c))
        # Worked out once, here, where the clock is generous.
        self.fields = {cid: self.distance_field([self.map.cities[cid][1]], blocked=set()) for cid in self.claims}
        self.post = {}  # unit id -> the city it garrisons, or None in the reserve

    def field(self, cid):
        """Move cost from every hex to a city."""
        return self.fields[cid]

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

    def on_turn(self, state):
        size = self.rules.stack_limit
        mine = {u.id: u for u in state.my_units()}
        self.post = {uid: cid for uid, cid in self.post.items() if uid in mine}
        count = {cid: 0 for cid in self.claims}
        for cid in self.post.values():
            if cid is not None:
                count[cid] += 1

        def unclaimed(c):
            return state.cities[c].owner != self.player and count[c] == 0

        def neediest(h, capturer=True):
            """The claimed city to send a unit to from h: one still to be
            taken that nobody is heading for, else the thinnest garrison.
            The nearest of those."""
            return min(self.claims, key=lambda c: (count[c] >= size, not (capturer and unclaimed(c)),
                                                   count[c], self.field(c).get(h, INF), c))

        for u in sorted(mine.values(), key=lambda u: u.id):
            if u.id not in self.post:
                self.post[u.id] = neediest(u.hex, self.unit_type(u).capture)
                count[self.post[u.id]] += 1

        # The reserve heads for intruders, and otherwise waits at the front.
        intruders = [e.hex for e in state.enemy_units() if self.ours(e.hex)]
        rally = self.distance_field(intruders) if intruders else self.field(self.front)

        for u in sorted(mine.values(), key=lambda u: u.id):
            if not self.can_act(u):
                continue
            if self.can_capture(u):
                self.capture(u)
                continue
            if self.post[u.id] is None:
                if not self.strike(u) and u.id in state.units and u.mp > 0:
                    if intruders or rally.get(u.hex, 0) > 1:
                        self.move_toward(u, rally)
                        self.strike(u, move=False)
                continue
            city = state.cities[self.post[u.id]]
            if u.hex == city.hex:
                self.strike(u, move=False)  # hold the post, and keep the fortification
                continue
            if self.strike(u):
                continue
            if u.id in state.units and u.mp > 0:
                self.debug_line(u.hex, city.hex, "blue")
                self.move_toward(u, self.field(city.id))
                if self.can_capture(u):
                    self.capture(u)
                else:
                    self.strike(u, move=False)

        # Each city builds for the neediest post: infantry to take a city,
        # then a pikeman, then archers and pikemen in turn.
        types = self.rules.unit_types
        income = self.rules.city_income * len(state.my_cities())
        for city in state.my_cities():
            if city.built or state.enemy_at(city.hex) or state.friends_at(city.hex) >= size:
                continue
            target = neediest(city.hex)
            if count[target] >= size:
                target = None  # every post is full: build for the reserve
            there = [state.units[uid].type for uid, cid in self.post.items() if cid == target]
            if target is None and (len(state.my_units()) + 1) * self.rules.unit_upkeep > self.SPEND * income:
                break  # the reserve is as big as the cities can pay for
            code = "A" if there.count("P") > there.count("A") else "P"
            if target is not None and unclaimed(target):
                code = "I"
            for code in (code, "P", "I"):
                if code in types and types[code].cost <= state.gold:
                    reply = self.build(city, code)
                    if reply.ok:
                        self.post[int(reply.values[0])] = target
                        if target is not None:
                            count[target] += 1
                    break
        self.debug_text("round %d: %d units on %d posts" % (state.round, len(self.post), len(self.claims)))


if __name__ == "__main__":
    bb.run(TurtleBot())
