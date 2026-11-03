"""The greedy reference bot (SPEC §9): expands to the nearest cities and
attacks whatever it can reach, when the trade looks favorable.

Usage: python bots/greedy_bot.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kits", "python"))
import bitbath as bb  # noqa: E402

INF = float("inf")


class GreedyBot(bb.Bot):
    BUILD_ORDER = "IIKIAIP"

    def on_init(self):
        self.builds = 0
        home = next(h for _, h, owner in self.map.cities if owner == self.player)
        self.enemy_home = self.map.mirror(home)

    def ready(self):
        """Units that can still act, archers first since they can't move and then shoot."""
        units = [u for u in self.state.my_units() if self.can_act(u)]
        return sorted(units, key=lambda u: (not self.unit_type(u).no_move_attack, u.id))

    def value(self, utype, hp_lost, dies):
        return utype.cost * hp_lost / utype.max_hp + (utype.cost if dies else 0)

    def attack_best(self, unit):
        """Makes the attack with the best expected trade, if any is favorable."""
        if unit.id not in self.state.units or not self.can_act(unit):
            return
        utype = self.unit_type(unit)
        if utype.no_move_attack and unit.moved:
            return
        best = None
        for enemy in self.targets_in_range(unit):
            def_hp, att_hp = self.predict(unit, enemy)
            etype = self.rules.unit_types[enemy.type]
            score = (self.value(etype, enemy.hp - def_hp, def_hp == 0)
                     - self.value(utype, unit.hp - att_hp, att_hp == 0))
            if best is None or score > best[0]:
                best = (score, enemy)
        if best and best[0] > 0:
            self.attack(unit, best[1])

    def finish(self, unit):
        """After a move: capture if standing on a city, otherwise attack."""
        if unit.id not in self.state.units:
            return
        if self.can_capture(unit):
            self.capture(unit)
        else:
            self.attack_best(unit)

    def on_turn(self, state):
        for unit in self.ready():
            if self.can_capture(unit):
                self.capture(unit)
            else:
                self.attack_best(unit)

        # Capturers claim the nearest city we don't own, one unit per city
        # first; the rest head for their nearest such city anyway.
        targets = [c for c in state.cities if c.owner != self.player]
        fields = {c.id: self.distance_field([c.hex]) for c in targets}
        movers = [u for u in self.ready() if u.mp > 0]
        capturers = [u for u in movers if self.unit_type(u).capture]
        pairs = sorted((fields[c.id].get(u.hex, INF), u.id, c.id) for u in capturers for c in targets)
        assigned, claimed = {}, set()
        for d, uid, cid in pairs:
            if d < INF and uid not in assigned and cid not in claimed:
                assigned[uid] = cid
                claimed.add(cid)
        for d, uid, cid in pairs:
            if d < INF and uid not in assigned:
                assigned[uid] = cid
        # Debug annotations show the plan in the replay viewer.
        self.debug_text("round %d: %d capturers heading for %d cities" % (state.round, len(assigned), len(claimed)))
        for cid in claimed:
            self.debug_hex(state.cities[cid].hex, "yellow", "target")
        for unit in capturers:
            if unit.id in assigned:
                self.debug_line(unit.hex, state.cities[assigned[unit.id]].hex, "orange")
                self.move_toward(unit, fields[assigned[unit.id]], self.zoc_hexes())
                self.finish(unit)

        # Everyone else hunts the nearest visible enemy, else enemy cities.
        enemies = state.enemy_units()
        goals = [e.hex for e in enemies]
        goals = goals or [c.hex for c in state.cities if c.owner not in (0, self.player)] or [self.enemy_home]
        hunt = self.distance_field(goals)
        for unit in movers:
            if unit.id in assigned or unit.id not in state.units or not self.can_act(unit):
                continue
            if self.unit_type(unit).no_move_attack and self.targets_in_range(unit):
                continue
            self.move_toward(unit, hunt, self.zoc_hexes())
            self.finish(unit)

        enemy_cavalry = sum(1 for e in enemies if e.type == "K")
        for city in state.my_cities():
            if city.built or state.enemy_at(city.hex) or state.friends_at(city.hex) >= self.rules.stack_limit:
                continue
            code = "P" if enemy_cavalry >= 2 and self.builds % 2 else self.BUILD_ORDER[self.builds % len(self.BUILD_ORDER)]
            if code not in self.rules.unit_types or state.gold < self.rules.unit_types[code].cost:
                code = "I"
            if code in self.rules.unit_types and state.gold >= self.rules.unit_types[code].cost:
                if self.build(city, code):
                    self.builds += 1


if __name__ == "__main__":
    bb.run(GreedyBot())
