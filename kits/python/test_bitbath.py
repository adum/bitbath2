"""Checks the kit against the golden test vectors in tests/vectors (SPEC §9).

Run with: python kits/python/test_bitbath.py
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bitbath as bb  # noqa: E402

VECTORS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tests", "vectors")

# The default rule tables, as the engine sends them (SPEC §6.2).
DEFAULT_INIT = """\
PLAYER 1
MAP 6 4
ROW C.....
ROW ......
ROW ......
ROW ......
CITY 0 0 0 1
TERRAIN . plains 1 0 0 0
TERRAIN f forest 2 25 0 1
TERRAIN h hills 2 40 1 0
TERRAIN C city 1 30 0 0
TERRAIN ^ mountains 0 0 0 0
TERRAIN ~ water 0 0 0 0
UNITTYPE I infantry 100 100 3 2 1 1 capture,zoc
UNITTYPE P pikeman 150 100 3 2 1 1 capture,zoc
UNITTYPE K cavalry 225 100 5 3 1 1 zoc
UNITTYPE A archer 200 100 3 2 2 2 zoc
UNITTYPE S scout 80 100 6 4 1 1 skirmish
DAMAGE I 55 45 40 65 75
DAMAGE P 45 50 80 55 70
DAMAGE K 70 35 55 85 90
DAMAGE A 50 55 40 45 70
DAMAGE S 20 15 10 25 40
RULE stack_limit 4
INIT
"""


def load(name):
    with open(os.path.join(VECTORS, name)) as f:
        return json.load(f)


def make_bot(rows, init=DEFAULT_INIT):
    """A bot whose map has the given rows, with the default rules."""
    lines = init.replace("MAP 6 4\n", "MAP %d %d\n" % (len(rows[0]), len(rows)))
    lines = lines.replace("ROW C.....\nROW ......\nROW ......\nROW ......\n",
                          "".join("ROW %s\n" % r for r in rows)).splitlines()
    lines = iter(lines)
    bot = bb.Bot()
    bb._parse_init(bot, lambda: next(lines))
    bot.state = bb.State(1, [])
    return bot


class HexVectors(unittest.TestCase):
    def test_hex(self):
        v = load("hex.json")
        self.assertEqual([list(d) for d in bb.DIRS], v["directions"])
        for c in v["distance"]:
            self.assertEqual(bb.distance(tuple(c["a"]), tuple(c["b"])), c["distance"], c)
        for c in v["neighbors"]:
            self.assertEqual([list(n) for n in bb.neighbors(tuple(c["hex"]))], c["neighbors"])
        for c in v["storage"]:
            h = tuple(c["hex"])
            self.assertEqual(bb.to_storage(h), (c["col"], c["row"]), c)
            self.assertEqual(bb.from_storage(c["col"], c["row"]), h)
            m = bb.Map(c["width"], c["height"], ["." * c["width"]] * c["height"], None)
            self.assertEqual(m.contains(h), c["on_map"], c)
        for c in v["mirror"]:
            self.assertEqual(list(bb.mirror(tuple(c["hex"]), c["width"], c["height"])), c["mirror"], c)

    def test_shapes(self):
        for r in range(4):
            self.assertEqual(len(bb.disk((2, 3), r)), 3 * r * (r + 1) + 1)
            self.assertTrue(all(bb.distance((2, 3), h) == r for h in bb.ring((2, 3), r)))
        self.assertEqual(len(bb.ring((0, 0), 2)), 12)
        ln = bb.line((0, 0), (4, -2))
        self.assertEqual(len(ln), 5)
        self.assertTrue(all(bb.distance(a, b) == 1 for a, b in zip(ln, ln[1:])))


class DamageVectors(unittest.TestCase):
    def test_damage(self):
        v = load("damage.json")
        bot = make_bot(["." * 6, "f" * 6, "h" * 6, "C" * 6])
        row_of = {".": 0, "f": 1, "h": 2, "C": 3}
        for c in v["damage"]:
            h = bb.from_storage(0, row_of[c["terrain"]])
            self.assertEqual(bot.damage(c["attacker"], c["attacker_hp"], c["defender"], h), c["damage"], c)

    def test_combat(self):
        v = load("damage.json")
        for c in v["combat"]:
            row = ["."] * 6
            row[1] = c["attacker_terrain"]
            row[1 + c["distance"]] = c["defender_terrain"]
            bot = make_bot(["." * 6, "".join(row), "." * 6, "." * 6])
            att = bb.Unit(1, 1, c["attacker"], 1, 1, c["attacker_hp"])
            dfn = bb.Unit(2, 2, c["defender"], 1 + c["distance"], 1, c["defender_hp"])
            bot.state.units = {1: att, 2: dfn}
            self.assertEqual(bot.predict(att, dfn), (c["defender_hp_after"], c["attacker_hp_after"]), c)


class VisionVectors(unittest.TestCase):
    def test_vision(self):
        v = load("vision.json")
        bot = make_bot(v["map"])
        for c in v["cases"]:
            unit = bb.Unit(1, 1, c["unit"], c["hex"][0], c["hex"][1], 100)
            bot.state.units = {1: unit}
            seen = sorted(bot.unit_sight(unit), key=lambda h: (h[1], h[0]))
            self.assertEqual([list(h) for h in seen], c["visible"], c)

    def test_old_terrain_lines(self):
        # Engines from before concealment send five-field TERRAIN lines.
        old = DEFAULT_INIT.replace("TERRAIN f forest 2 25 0 1", "TERRAIN f forest 2 25 0")
        bot = make_bot(["." * 6, "f" * 6, "." * 6, "." * 6], old)
        self.assertEqual(bot.rules.terrains["f"].conceal, 0)
        self.assertEqual(make_bot(["." * 6] * 4).rules.terrains["f"].conceal, 1)


class Pathfinding(unittest.TestCase):
    def test_reachable_respects_zoc_and_terrain(self):
        bot = make_bot(["......", "..f...", "......", "......"])
        me = bb.Unit(1, 1, "I", 1, 2, 100)
        me.mp = 3
        enemy = bb.Unit(2, 2, "I", 3, 2, 100)
        bot.state.units = {1: me, 2: enemy}
        reach = bot.reachable(me)
        self.assertEqual(reach[(2, 2)], "0")         # next to the enemy: fine as a final hex
        self.assertNotIn((3, 2), reach)              # the enemy's own hex
        self.assertNotIn((4, 1), reach)              # 3 steps away, but every path crosses the zone of control
        self.assertIn((0, 1), reach)
        field = bot.distance_field([enemy.hex])
        self.assertEqual(field[(2, 2)], 1)
        self.assertEqual(field[(0, 2)], 3)


class Orders(unittest.TestCase):
    def test_a_captured_city_cant_build_this_turn(self):
        bot = make_bot(["C.....", "......", "......", "......"])
        city = bb.City(0, 0, 0, 0)
        bot.state = bb.State(1, [city])
        unit = bb.Unit(1, 1, "I", 0, 0, 100)
        bot.state.units = {1: unit}
        with mock.patch.object(bb, "_read_line", lambda: "OK captured"), \
                mock.patch.object(bb, "_out", io.StringIO()), \
                mock.patch.object(bot, "time_left", lambda: 10 ** 6):
            self.assertTrue(bot.capture(unit))
        self.assertEqual(city.owner, 1)
        self.assertTrue(city.built)


if __name__ == "__main__":
    unittest.main()
