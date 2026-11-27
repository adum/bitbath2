"""A placeholder bot: it walks to the nearest city it can reach and doesn't
own, captures it, and builds infantry in each city that has the bread. It
builds no ships, so it never leaves its island. Replace it with your own.

bitbath.py beside this file is a copy of the Python kit, so this directory
runs on its own.
"""

import bitbath as bb


class MyBot(bb.Bot):
    def on_turn(self, state):
        targets = [c.hex for c in state.cities if c.owner != self.player]
        field = self.distance_field(targets)
        for unit in state.my_units():
            if not self.can_capture(unit):
                self.move_toward(unit, field)
            if self.can_capture(unit):
                self.capture(unit)
        cost = self.rules.unit_types["I"].cost
        for city in state.my_cities():
            if city.bread >= cost:
                self.build(city, "I")


bb.run(MyBot())
