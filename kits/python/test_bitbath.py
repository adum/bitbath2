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
UNITTYPE P pikeman 125 100 3 2 1 1 capture,zoc,fortify
UNITTYPE K cavalry 225 100 5 3 1 1 zoc
UNITTYPE A archer 200 100 3 2 2 2 zoc
UNITTYPE S scout 80 100 6 4 1 1 skirmish
DAMAGE I 55 45 40 65 75
DAMAGE P 45 50 80 55 70
DAMAGE K 70 35 55 85 90
DAMAGE A 50 55 40 45 70
DAMAGE S 20 15 10 25 40
RULE stack_limit 4
RULE fortify_pct 10
RULE fortify_max 2
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
            att = bb.Unit(1, 1, c["attacker"], 1, 1, c["attacker_hp"], c.get("attacker_fort", 0))
            dfn = bb.Unit(2, 2, c["defender"], 1 + c["distance"], 1, c["defender_hp"], c.get("defender_fort", 0))
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

    def test_moving_loses_fortification(self):
        bot = make_bot(["......", "......", "......", "......"])
        pike = bb.Unit(1, 1, "P", 1, 1, 100, 2)
        pike.mp = 3
        bot.state.units = {1: pike}
        replies = iter(["SEE 2 2 P 3 1 100 1", "SEE 3 2 I 3 2 100", "OK 2 1 2"])
        with mock.patch.object(bb, "_read_line", lambda: next(replies)), \
                mock.patch.object(bb, "_out", io.StringIO()), \
                mock.patch.object(bot, "time_left", lambda: 10 ** 6):
            self.assertTrue(bot.move(pike, "0"))
        self.assertEqual((pike.hex, pike.fort, pike.moved), ((2, 1), 0, True))
        # A unit that comes into view shows its level; engines from before
        # fortification send none.
        self.assertEqual((bot.state.units[2].fort, bot.state.units[3].fort), (1, 0))

    def test_turn_message_carries_fortification(self):
        bot = make_bot(["......", "......", "......", "......"])
        lines = iter(["TIME 1000 1000", "GOLD 5", "UNIT 1 1 P 0 0 100 2", "UNIT 2 2 I 1 0 100", "GO"])
        with mock.patch.object(bb, "_read_line", lambda: next(lines)):
            state = bb._parse_turn(bot, "TURN 3")
        self.assertEqual((state.units[1].fort, state.units[2].fort), (2, 0))


# The init message of a supply game (STRATEGIC.md §11): a site, a wagon, and
# the supply rules.
SUPPLY_INIT = DEFAULT_INIT.replace("CITY 0 0 0 1\n", "CITY 0 0 0 1\nSITE 0 1 0 1 60 1700\n").replace(
    "TERRAIN ~ water 0 0 0 0\n", "TERRAIN ~ water 0 0 0 0\nTERRAIN R site 1 0 0 0 0\n").replace(
    "UNITTYPE S scout 80 100 6 4 1 1 skirmish\n",
    "UNITTYPE S scout 80 100 6 4 1 1 skirmish\nUNITTYPE W wagon 100 100 4 2 1 1 carry\n").replace(
    "RULE fortify_max 2\n", "RULE fortify_max 2\nRULE supply 1\nRULE ration_max 30\nRULE eat_base 1\nRULE eat_move 1\nRULE eat_attack 1\n")


class Supply(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot(["CR....", "......", "......", "......"], SUPPLY_INIT)
        lines = iter(["TIME 1000 1000", "CITY 0 1 0 0 420", "SITE 0 1 0 0 650",
                      "UNIT 41 1 W 1 0 100 0 0 0 0 30", "UNIT 42 1 I 0 0 80 0 0 0 0 12", "GO"])
        self.bot.state = bb._parse_turn(self.bot, "TURN 7", lambda: next(lines))

    def send(self, replies, fn):
        replies = iter(replies)
        with mock.patch.object(bb, "_read_line", lambda: next(replies)), \
                mock.patch.object(bb, "_out", io.StringIO()) as out, \
                mock.patch.object(self.bot, "time_left", lambda: 10 ** 6):
            reply = fn()
        return reply, out.getvalue()

    def test_turn_message(self):
        bot, state = self.bot, self.bot.state
        self.assertTrue(bot.rules.supply)
        self.assertFalse(make_bot(["......"] * 4).rules.supply)
        self.assertTrue(bot.rules.unit_types["W"].carry)
        self.assertEqual(bot.map.sites, [(0, (1, 0), 1, 60, 1700)])
        self.assertEqual((state.gold, state.cities[0].bread), (0, 420))
        site = state.sites[0]
        self.assertEqual((site.hex, site.owner, site.output, site.cap, site.wheat), ((1, 0), 1, 60, 1700, 650))
        self.assertIs(state.site_at((1, 0)), site)
        wagon, foot = state.units[41], state.units[42]
        self.assertEqual((wagon.carrier, wagon.wheat, wagon.bread, wagon.rations), (0, 0, 0, 30))
        self.assertEqual((foot.rations, foot.hp), (12, 80))

    def test_loading_and_unloading(self):
        bot, state = self.bot, self.bot.state
        wagon = state.units[41]
        wagon.mp = 4
        reply, sent = self.send(["OK 650 0 0"], lambda: bot.load(wagon))
        self.assertTrue(reply)
        self.assertEqual((sent, wagon.wheat, wagon.mp, state.sites[0].wheat), ("L 41\n", 650, 0, 0))
        wagon.q, wagon.r = 0, 0  # as if it had moved onto the city
        reply, sent = self.send(["OK 350 0 720"], lambda: bot.unload(wagon, 300))
        self.assertEqual((sent, wagon.wheat, state.cities[0].bread), ("U 41 300\n", 350, 720))
        reply, _ = self.send(["ERR cannot_load unit 41 isn't on a site or city you hold"], lambda: bot.load(wagon))
        self.assertEqual((reply.ok, reply.code), (False, "cannot_load"))

    def test_building_pays_with_the_citys_bread(self):
        bot, state = self.bot, self.bot.state
        reply, _ = self.send(["OK 43"], lambda: bot.build(0, "W"))
        self.assertTrue(reply)
        self.assertEqual((state.cities[0].bread, state.units[43].rations), (320, 30))

    def test_capturing_a_site_and_food(self):
        bot, state = self.bot, self.bot.state
        state.sites[0].owner = 0
        foot = state.units[42]
        foot.q, foot.r = 1, 0
        self.assertTrue(bot.can_capture(foot))
        self.send(["OK 80 200"], lambda: bot.capture(foot))
        self.assertEqual((state.sites[0].capturer, state.sites[0].progress), (1, 80))
        self.assertEqual((bot.meal(), bot.meal(moved=True), bot.meal(True, True)), (1, 2, 3))
        self.assertEqual(bot.food_near((1, 0)), [state.cities[0]])
        self.assertEqual(bot.food_near((3, 2)), [])


# The init message of a game with ships (STRATEGIC.md §11): water that takes
# ships, transports carrying 2 units, and galleys. City 0 is a port, with
# water east and north-east of it.
SHIPS_INIT = """\
PLAYER 1
MAP 6 4
ROW ..~~..
ROW .C~~..
ROW ..~~..
ROW ..~~.C
CITY 0 1 1 1
CITY 1 4 3 2
TERRAIN . plains 1 0 0 0 0
TERRAIN f forest 2 25 0 1 0
TERRAIN h hills 2 40 1 0 0
TERRAIN C city 1 30 0 0 0
TERRAIN ^ mountains 0 0 0 0 0
TERRAIN ~ water 0 0 0 0 1
UNITTYPE I infantry 100 100 3 2 1 1 capture,zoc
UNITTYPE P pikeman 125 100 3 2 1 1 capture,zoc,fortify
UNITTYPE K cavalry 225 100 5 3 1 1 zoc
UNITTYPE A archer 200 100 3 2 2 2 zoc
UNITTYPE S scout 80 100 6 4 1 1 skirmish
UNITTYPE W wagon 100 100 4 2 1 1 carry
UNITTYPE T transport 150 100 6 2 1 1 naval,transport
UNITTYPE G galley 200 100 5 3 1 1 naval,zoc
DAMAGE I 55 45 40 65 75 70 0 0
DAMAGE P 45 50 80 55 70 60 0 0
DAMAGE K 70 35 55 85 90 90 0 0
DAMAGE A 50 55 40 45 70 70 40 40
DAMAGE S 20 15 10 25 40 40 0 0
DAMAGE W 0 0 0 0 0 0 0 0
DAMAGE T 0 0 0 0 0 0 0 0
DAMAGE G 25 25 25 30 35 50 80 55
RULE stack_limit 4
RULE supply 1
RULE passengers_max 2
RULE ration_max 60
INIT
"""


class Ships(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot(["..~~..", ".C~~..", "..~~..", "..~~.C"], SHIPS_INIT)
        lines = iter(["TIME 1000 1000", "CITY 0 1 0 0 900", "CITY 1 2 0 0 0",
                      "UNIT 10 1 T 2 1 100 0 0 0 0 60", "UNIT 11 1 G 3 0 100 0 0 0 0 60",
                      "UNIT 12 1 I 1 1 100 0 0 0 0 60", "UNIT 13 1 I 1 0 100 0 0 0 0 60",
                      "UNIT 20 2 T 2 0 30 0 0 0 0 60", "UNIT 21 2 P 2 0 100 0 20 0 0 60", "GO"])
        self.bot.state = bb._parse_turn(self.bot, "TURN 4", lambda: next(lines))

    send = Supply.send

    def test_rules_terrain_and_ports(self):
        bot, state = self.bot, self.bot.state
        water = bot.rules.terrains["~"]
        self.assertEqual((water.naval_move_cost, water.navigable, water.passable), (1, True, False))
        self.assertEqual(water.cost_for(True), 1)
        self.assertTrue(bot.rules.naval)
        self.assertFalse(make_bot(["......"] * 4).rules.naval)
        t, g = bot.rules.unit_types["T"], bot.rules.unit_types["G"]
        self.assertEqual((t.naval, t.transport, g.naval, g.transport), (True, True, True, False))
        self.assertTrue(bot.is_port(0))
        self.assertFalse(bot.is_port(1))
        self.assertEqual(state.units[21].carrier, 20)
        self.assertTrue(state.units[21].aboard)
        self.assertEqual(state.passengers(state.units[20]), [state.units[21]])
        # Units aboard don't count toward the stack limit, exert no zone of
        # control, and can't be attacked.
        self.assertEqual(state.friends_at((2, 0)), 0)
        self.assertNotIn((1, 0), bot.zoc_hexes())
        self.assertEqual(bot.targets_in_range(state.units[11]), [state.units[20]])
        self.assertEqual(bot.targets_in_range(state.units[13]), [])   # infantry can't hurt ships

    def test_reachable_for_ships_boarding_and_landing(self):
        bot, state = self.bot, self.bot.state
        transport, foot = state.units[10], state.units[12]
        reach = bot.reachable(transport)
        self.assertTrue(all(bot.map.navigable(h) for h in reach))
        self.assertNotIn((1, 1), reach)
        self.assertEqual(reach[(2, 3)], "55")
        self.assertEqual(bot.distance_field([(2, 3)], naval=True)[(2, 1)], 2)
        foot_reach = bot.reachable(foot)
        self.assertEqual(foot_reach[(2, 1)], "0")   # boarding the transport next to it
        self.assertNotIn((3, 1), foot_reach)        # water without a transport
        reply, sent = self.send(["OK 2 1 0"], lambda: bot.move(foot, "0"))
        self.assertTrue(reply)
        self.assertEqual((sent, foot.hex, foot.carrier, foot.mp), ("M 12 0\n", (2, 1), 10, 0))
        self.assertEqual(state.friends_at((2, 1)), 1)
        # The transport sails, and its passengers with it.
        reply, _ = self.send(["OK 2 3 4"], lambda: bot.move(transport, "55"))
        self.assertEqual((transport.hex, foot.hex), ((2, 3), (2, 3)))
        foot.mp = 3   # as on the next turn
        self.assertEqual(bot.reachable(foot), {(3, 3): "0", (3, 2): "1"})
        reply, _ = self.send(["OK 3 3 0"], lambda: bot.move(foot, "0"))
        self.assertEqual((foot.hex, foot.carrier, foot.moved), ((3, 3), 0, True))
        self.assertEqual(state.passengers(transport), [])

    def test_a_full_transport_takes_no_one(self):
        bot, state = self.bot, self.bot.state
        for i in (30, 31):
            state.units[i] = bb.Unit(i, 1, "I", 2, 1, 100, carrier=10)
        self.assertIsNone(bot.transport_with_room((2, 1)))
        self.assertNotIn((2, 1), bot.reachable(state.units[12]))

    def test_sinking_takes_everything_aboard(self):
        bot, state = self.bot, self.bot.state
        galley = state.units[11]
        reply, _ = self.send(["OK 0 100"], lambda: bot.attack(galley, 20))
        self.assertTrue(reply)
        self.assertNotIn(20, state.units)
        self.assertNotIn(21, state.units)

    def test_a_port_builds_ships_on_its_launch_hex(self):
        bot, state = self.bot, self.bot.state
        self.assertEqual(bot.launch_hex(0), (2, 1))
        for i in (40, 41, 42):
            state.units[i] = bb.Unit(i, 1, "G", 2, 1, 100)
        # (2,1) now holds 4 ships, and the enemy transport is on (2,0), so the
        # next water hex in direction order is (1,2).
        self.assertEqual(bot.launch_hex(0), (1, 2))
        reply, _ = self.send(["OK 50"], lambda: bot.build(0, "G"))
        self.assertTrue(reply)
        self.assertEqual((state.units[50].hex, state.cities[0].bread), ((1, 2), 700))
        self.assertIsNone(bot.launch_hex(1))   # city 1 isn't a port


if __name__ == "__main__":
    unittest.main()
