"""The random reference bot (SPEC §9): legal orders only, picked at random.

Usage: python bots/random_bot.py [seed]
"""

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kits", "python"))
import bitbath as bb  # noqa: E402


class RandomBot(bb.Bot):
    def __init__(self, seed=None):
        self.rng = random.Random(seed)

    def on_turn(self, state):
        rng = self.rng
        units = state.my_units()
        rng.shuffle(units)
        for unit in units:
            if unit.id not in state.units or not self.can_act(unit):
                continue
            if self.can_capture(unit) and rng.random() < 0.9:
                self.capture(unit)
                continue
            targets = self.targets_in_range(unit)
            if targets and rng.random() < 0.7:
                self.attack(unit, rng.choice(targets))
                continue
            reach = self.reachable(unit)
            if reach and rng.random() < 0.9:
                self.move(unit, reach[rng.choice(sorted(reach))])
            if unit.id not in state.units or not self.can_act(unit):
                continue
            if self.can_capture(unit):
                self.capture(unit)
            elif not (self.unit_type(unit).no_move_attack and unit.moved):
                targets = self.targets_in_range(unit)
                if targets:
                    self.attack(unit, rng.choice(targets))

        codes = list(self.rules.unit_types)
        for city in state.my_cities():
            code = rng.choice(codes)
            room = state.friends_at(city.hex) < self.rules.stack_limit and not state.enemy_at(city.hex)
            if room and rng.random() < 0.6 and state.gold >= self.rules.unit_types[code].cost:
                self.build(city, code)


if __name__ == "__main__":
    bb.run(RandomBot(int(sys.argv[1]) if len(sys.argv) > 1 else None))
