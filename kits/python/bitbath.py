"""Bitbath starter kit for Python bots (SPEC §6, §9).

Write a bot by subclassing Bot and overriding on_turn:

    import bitbath as bb

    class MyBot(bb.Bot):
        def on_turn(self, state):
            for unit in state.my_units():
                reach = self.reachable(unit)
                ...
                self.move(unit, reach[target])

    if __name__ == "__main__":
        bb.run(MyBot())

The kit speaks the protocol for you. It parses the init and turn messages,
flushes every order, reads each reply, and updates `state` as orders succeed.
print() goes to stderr, so a stray print can't corrupt the protocol. If
on_turn raises, the kit logs the traceback and ends the turn, so one bug
costs a turn instead of the game.

Hexes are (q, r) tuples in axial coordinates (SPEC §3.1). Direction d moves
by DIRS[d]: 0 E, 1 NE, 2 NW, 3 W, 4 SW, 5 SE.
"""

import heapq
import sys
import time
import traceback

_out = sys.stdout
sys.stdout = sys.stderr

DIRS = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)]

# Orders stop being sent when less than this much of the turn is left.
TIME_MARGIN_MS = 30


# Hex helpers.

def step(h, d):
    """The neighbor of hex h in direction d."""
    return (h[0] + DIRS[d][0], h[1] + DIRS[d][1])


def neighbors(h):
    return [(h[0] + dq, h[1] + dr) for dq, dr in DIRS]


def distance(a, b):
    dq, dr = a[0] - b[0], a[1] - b[1]
    return (abs(dq) + abs(dr) + abs(dq + dr)) // 2


def direction(a, b):
    """The direction from a to its neighbor b, or None if they aren't neighbors."""
    delta = (b[0] - a[0], b[1] - a[1])
    return DIRS.index(delta) if delta in DIRS else None


def to_storage(h):
    """(column, row) of a hex in the map's ROW strings."""
    return (h[0] + h[1] // 2, h[1])


def from_storage(col, row):
    return (col - row // 2, row)


def mirror(h, width, height):
    """The hex's image under the map's 180° symmetry."""
    col, row = to_storage(h)
    return from_storage(width - 1 - col, height - 1 - row)


def disk(center, radius):
    """Every hex within radius of center."""
    q0, r0 = center
    out = []
    for dr in range(-radius, radius + 1):
        for dq in range(max(-radius, -dr - radius), min(radius, -dr + radius) + 1):
            out.append((q0 + dq, r0 + dr))
    return out


def ring(center, radius):
    """The hexes at exactly radius from center."""
    if radius == 0:
        return [center]
    out = []
    h = (center[0] + DIRS[4][0] * radius, center[1] + DIRS[4][1] * radius)
    for d in range(6):
        for _ in range(radius):
            out.append(h)
            h = step(h, d)
    return out


def line(a, b):
    """Hexes on a straight line from a to b, inclusive."""
    n = distance(a, b)
    if n == 0:
        return [a]
    out = []
    for i in range(n + 1):
        t = i / n
        q = a[0] + (b[0] - a[0]) * t + 1e-6
        r = a[1] + (b[1] - a[1]) * t + 1e-6
        s = -q - r
        rq, rr, rs = round(q), round(r), round(s)
        dq, dr, ds = abs(rq - q), abs(rr - r), abs(rs - s)
        if dq > dr and dq > ds:
            rq = -rr - rs
        elif dr > ds:
            rr = -rq - rs
        out.append((rq, rr))
    return out


# Rules and map.

class Terrain:
    def __init__(self, ch, name, move_cost, defense, vision_bonus, conceal=0):
        self.ch, self.name = ch, name
        self.move_cost, self.defense, self.vision_bonus = move_cost, defense, vision_bonus
        self.conceal = conceal  # seen from at most vision - conceal away, always from next door

    @property
    def passable(self):
        return self.move_cost > 0

    def __repr__(self):
        return "Terrain(%r)" % self.name


class UnitType:
    def __init__(self, code, name, cost, max_hp, move, vision, range_min, range_max, flags):
        self.code, self.name = code, name
        self.cost, self.max_hp, self.move, self.vision = cost, max_hp, move, vision
        self.range_min, self.range_max = range_min, range_max
        self.flags = set() if flags == "-" else set(flags.split(","))

    capture = property(lambda self: "capture" in self.flags)
    zoc = property(lambda self: "zoc" in self.flags)
    skirmish = property(lambda self: "skirmish" in self.flags)
    no_move_attack = property(lambda self: "no_move_attack" in self.flags)
    fortify = property(lambda self: "fortify" in self.flags)

    def in_range(self, dist):
        return self.range_min <= dist <= self.range_max

    def __repr__(self):
        return "UnitType(%r)" % self.name


class Rules:
    """Rule tables from the init message. Constants are attributes too:
    rules.stack_limit, rules.capture_points, and so on."""

    def __init__(self):
        self.terrains = {}      # char -> Terrain
        self.unit_types = {}    # code -> UnitType, in the engine's order
        self.damage = {}        # attacker code -> {defender code: base damage}
        self.constants = {}     # name -> int

    def __getattr__(self, name):
        constants = self.__dict__.get("constants", {})
        if name in constants:
            return constants[name]
        raise AttributeError(name)


class Map:
    def __init__(self, width, height, rows, rules):
        self.width, self.height, self.rows, self.rules = width, height, rows, rules
        self.cities = []        # (id, hex, initial owner)

    def contains(self, h):
        col, row = to_storage(h)
        return 0 <= row < self.height and 0 <= col < self.width

    def terrain(self, h):
        """The Terrain at h, or None if h is off the map."""
        col, row = to_storage(h)
        if not (0 <= row < self.height and 0 <= col < self.width):
            return None
        return self.rules.terrains[self.rows[row][col]]

    def passable(self, h):
        t = self.terrain(h)
        return t is not None and t.passable

    def hexes(self):
        """Every hex on the map, row by row."""
        return [from_storage(c, r) for r in range(self.height) for c in range(self.width)]

    def mirror(self, h):
        return mirror(h, self.width, self.height)

    def sight(self, h, vision):
        """The hexes an observer on h with this vision range sees, matching the
        engine (SPEC §4). A hex whose terrain conceals is seen from at most
        vision - conceal away, but the hexes next to h are always seen. A
        unit's vision includes its hex's vision bonus (see Bot.unit_sight); a
        city sees with rules.city_vision."""
        out = []
        for n in disk(h, vision):
            t = self.terrain(n)
            if t is not None:
                d = distance(h, n)
                if d <= 1 or d + t.conceal <= vision:
                    out.append(n)
        return out


# Game state.

class Unit:
    def __init__(self, id, owner, type, q, r, hp, fort=0):
        self.id, self.owner, self.type = id, owner, type
        self.q, self.r, self.hp = q, r, hp
        self.fort = fort        # fortification level; moving resets it to 0
        # Tracked for your own units during your turn.
        self.mp = 0
        self.acted = False
        self.moved = False
        self.fresh = False      # built this turn

    @property
    def hex(self):
        return (self.q, self.r)

    def __repr__(self):
        return "Unit(%d, p%d %s at (%d,%d), %d hp)" % (self.id, self.owner, self.type, self.q, self.r, self.hp)


class City:
    def __init__(self, id, q, r, owner):
        self.id, self.q, self.r, self.owner = id, q, r, owner
        self.capturer = 0
        self.progress = 0
        self.built = False      # built a unit or was captured this turn, so can't build

    @property
    def hex(self):
        return (self.q, self.r)

    def __repr__(self):
        return "City(%d at (%d,%d), owner %d)" % (self.id, self.q, self.r, self.owner)


class State:
    """What you know at the start of a turn, updated as your orders run."""

    def __init__(self, player, cities):
        self.player = player
        self.round = 0
        self.time_ms = 0
        self.bank_ms = 0
        self.gold = 0
        self.cities = cities        # list, indexed by city id
        self.units = {}             # id -> Unit: yours, then visible enemies
        self.logs = []              # LOG lines as token lists, e.g. ["HIT", "91", "K", ...]

    def my_units(self):
        return [u for u in self.units.values() if u.owner == self.player]

    def enemy_units(self):
        return [u for u in self.units.values() if u.owner != self.player]

    def my_cities(self):
        return [c for c in self.cities if c.owner == self.player]

    def units_at(self, h):
        return [u for u in self.units.values() if u.hex == h]

    def city_at(self, h):
        for c in self.cities:
            if c.hex == h:
                return c
        return None

    def enemy_at(self, h):
        """Whether a visible enemy unit is on h."""
        return any(u.owner != self.player and u.hex == h for u in self.units.values())

    def friends_at(self, h, excluding=None):
        return sum(1 for u in self.units.values() if u.owner == self.player and u.hex == h and u is not excluding)


class Reply:
    """An order's result. ok is True for OK and PART. For ERR, code holds the
    error code (such as "path") and text the explanation."""

    def __init__(self, line):
        self.line = line
        tokens = line.split()
        self.kind = tokens[0] if tokens else "ERR"
        self.ok = self.kind in ("OK", "PART")
        self.values = tokens[1:]
        self.code = tokens[1] if self.kind == "ERR" and len(tokens) > 1 else None
        self.text = line.split(None, 2)[2] if self.kind == "ERR" and len(tokens) > 2 else ""

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return "Reply(%r)" % self.line


class GameOver(Exception):
    pass


# The bot.

class Bot:
    """Subclass this and override on_turn. self.player, self.rules, and
    self.map are set before on_init runs; self.state before each on_turn."""

    def on_init(self):
        pass

    def on_turn(self, state):
        raise NotImplementedError

    def on_game_over(self, winner, reason):
        pass

    # Orders. Each returns a Reply and updates self.state on success.

    def move(self, unit, dirs):
        unit = self._unit(unit)
        reply = self._order("M %d %s" % (unit.id, dirs))
        if reply.ok:
            q, r = int(reply.values[0]), int(reply.values[1])
            if (q, r) != unit.hex:
                unit.moved, unit.fort = True, 0
            unit.q, unit.r = q, r
            unit.mp = int(reply.values[2]) if reply.kind == "OK" else 0
        return reply

    def attack(self, unit, target):
        unit, target = self._unit(unit), self._unit(target)
        reply = self._order("A %d %d" % (unit.id, target.id))
        if reply.ok:
            target.hp, unit.hp = int(reply.values[0]), int(reply.values[1])
            unit.acted, unit.mp = True, 0
            for u in (target, unit):
                if u.hp <= 0:
                    self.state.units.pop(u.id, None)
        return reply

    def capture(self, unit):
        unit = self._unit(unit)
        reply = self._order("C %d" % unit.id)
        if reply.ok:
            unit.acted = True
            city = self.state.city_at(unit.hex)
            if reply.values == ["captured"]:
                city.owner, city.capturer, city.progress = self.player, 0, 0
                city.built = True
            else:
                city.capturer, city.progress = self.player, int(reply.values[0])
        return reply

    def build(self, city, code):
        city = city if isinstance(city, City) else self.state.cities[city]
        reply = self._order("B %d %s" % (city.id, code))
        if reply.ok:
            utype = self.rules.unit_types[code]
            unit = Unit(int(reply.values[0]), self.player, code, city.q, city.r, utype.max_hp)
            unit.fresh = True
            self.state.units[unit.id] = unit
            self.state.gold -= utype.cost
            city.built = True
        return reply

    # Debug annotations, drawn by the replay viewer (SPEC §8).

    def debug_hex(self, h, color, label=""):
        self._send(("D HEX %d %d %s %s" % (h[0], h[1], color, label)).rstrip())

    def debug_line(self, a, b, color):
        self._send("D LINE %d %d %d %d %s" % (a[0], a[1], b[0], b[1], color))

    def debug_text(self, text):
        self._send("D TEXT %s" % text)

    def log(self, *args):
        print(*args, file=sys.stderr)

    def time_left(self):
        """Milliseconds left in this turn, by the bot's own clock."""
        return self.state.time_ms - (time.monotonic() - self._turn_start) * 1000

    # Unit helpers.

    def unit_type(self, unit):
        return self.rules.unit_types[self._unit(unit).type]

    def can_act(self, unit):
        unit = self._unit(unit)
        return unit.owner == self.player and not unit.fresh and not unit.acted

    def can_capture(self, unit):
        """Whether unit can capture the city it stands on right now."""
        unit = self._unit(unit)
        city = self.state.city_at(unit.hex)
        return (self.can_act(unit) and self.unit_type(unit).capture
                and city is not None and city.owner != self.player)

    def unit_sight(self, unit, from_hex=None):
        """The hexes unit would see from from_hex (default: where it stands):
        its vision plus that hex's vision bonus, with concealing terrain seen
        only from close by."""
        unit = self._unit(unit)
        at = from_hex or unit.hex
        return self.map.sight(at, self.unit_type(unit).vision + self.map.terrain(at).vision_bonus)

    def targets_in_range(self, unit, from_hex=None):
        """Visible enemy units that unit could attack from from_hex (default: where it is)."""
        unit = self._unit(unit)
        utype = self.unit_type(unit)
        at = from_hex or unit.hex
        return [e for e in self.state.enemy_units() if utype.in_range(distance(at, e.hex))]

    def zoc_hexes(self):
        """Hexes next to a visible enemy unit that exerts a zone of control."""
        out = set()
        for e in self.state.enemy_units():
            if self.rules.unit_types[e.type].zoc:
                out.update(neighbors(e.hex))
        return out

    # Pathfinding.

    def reachable(self, unit, zoc=None):
        """Where unit can end a move this turn, as {hex: dirs}. Honors move
        costs, its MP, visible enemies (which block hexes and exert zones of
        control), and the stack limit on the final hex. Hidden enemies can
        still cut a move short."""
        unit = self._unit(unit)
        if not self.can_act(unit) or unit.mp <= 0:
            return {}
        utype = self.unit_type(unit)
        if zoc is None:
            zoc = self.zoc_hexes()
        start = unit.hex
        stack_limit = self.rules.stack_limit
        # Who stands where, counted once: asking the state per hex is slow.
        friends, foes = {}, set()
        for u in self.state.units.values():
            if u.owner != self.player:
                foes.add(u.hex)
            elif u is not unit:
                friends[u.hex] = friends.get(u.hex, 0) + 1
        best = {start: 0}
        paths = {start: ""}
        heap = [(0, start)]
        result = {}
        while heap:
            cost, h = heapq.heappop(heap)
            if cost > best[h]:
                continue
            if h != start:
                if friends.get(h, 0) < stack_limit:
                    result[h] = paths[h]
                if not utype.skirmish and h in zoc:
                    continue  # entering a zone of control ends the move
            for d in range(6):
                n = step(h, d)
                t = self.map.terrain(n)
                if t is None or not t.passable or n in foes:
                    continue
                c = cost + t.move_cost
                if c <= unit.mp and c < best.get(n, c + 1):
                    best[n] = c
                    paths[n] = paths[h] + str(d)
                    heapq.heappush(heap, (c, n))
        return result

    def distance_field(self, goals, blocked=None):
        """Move cost from every hex to the nearest goal, as {hex: cost}.
        Costs count the terrain of each hex entered, ignoring MP and zones
        of control. Hexes in blocked (default: visible enemies that aren't
        goals) can't be passed through."""
        goals = set(goals)
        if blocked is None:
            blocked = {e.hex for e in self.state.enemy_units()} - goals
        dist = {}
        heap = [(0, g) for g in sorted(goals) if self.map.passable(g)]
        for _, g in heap:
            dist[g] = 0
        heapq.heapify(heap)
        while heap:
            cost, h = heapq.heappop(heap)
            if cost > dist[h] or (h in blocked and h not in goals):
                continue
            enter = self.map.terrain(h).move_cost
            for n in neighbors(h):
                if n in blocked or not self.map.passable(n):
                    continue
                c = cost + enter
                if c < dist.get(n, c + 1):
                    dist[n] = c
                    heapq.heappush(heap, (c, n))
        return dist

    def move_toward(self, unit, field, zoc=None):
        """Moves unit to the hex it can reach this turn that is closest to
        the goals of a distance_field. Returns the Reply, or None if no
        reachable hex is closer than where it stands."""
        unit = self._unit(unit)
        reach = self.reachable(unit, zoc)
        here = field.get(unit.hex)
        best = None
        for h, dirs in reach.items():
            d = field.get(h)
            if d is not None and (here is None or d < here):
                key = (d, len(dirs), h)
                if best is None or key < best[0]:
                    best = (key, dirs)
        return self.move(unit, best[1]) if best else None

    # Combat prediction, matching the engine exactly (SPEC §3.7).

    def damage(self, att_type, att_hp, def_type, def_hex, def_fort=0):
        """Damage a unit of att_type with att_hp HP deals to a def_type unit on
        def_hex that has dug in def_fort levels."""
        base = self.rules.damage[att_type][def_type]
        if base == 0:
            return 0
        defense = self.map.terrain(def_hex).defense + def_fort * self.rules.constants.get("fortify_pct", 0)
        max_hp = self.rules.unit_types[att_type].max_hp
        return max(1, base * att_hp * (100 - min(defense, 100)) // (max_hp * 100))

    def predict(self, attacker, defender, from_hex=None):
        """(defender HP, attacker HP) after attacker attacks defender from
        from_hex (default: where it stands). An attacker that moves there
        first loses its fortification."""
        attacker, defender = self._unit(attacker), self._unit(defender)
        at = from_hex or attacker.hex
        att_fort = attacker.fort if at == attacker.hex else 0
        def_hp = max(0, defender.hp - self.damage(attacker.type, attacker.hp, defender.type, defender.hex, defender.fort))
        att_hp = attacker.hp
        dtype = self.rules.unit_types[defender.type]
        if def_hp > 0 and dtype.in_range(distance(at, defender.hex)) and self.rules.damage[defender.type][attacker.type] > 0:
            att_hp = max(0, att_hp - self.damage(defender.type, def_hp, attacker.type, at, att_fort))
        return def_hp, att_hp

    # Protocol plumbing.

    def _unit(self, unit):
        return unit if isinstance(unit, Unit) else self.state.units[unit]

    def _send(self, line):
        _out.write(line + "\n")
        _out.flush()

    def _order(self, line):
        """Sends an order and reads its reply, applying SEE events."""
        if self.time_left() < TIME_MARGIN_MS:
            return Reply("ERR late the kit stopped sending orders because the turn's time is up")
        self._send(line)
        while True:
            reply = _read_line()
            kind = reply.split(None, 1)[0] if reply.strip() else ""
            if kind == "SEE":
                t = reply.split()
                u = Unit(int(t[1]), int(t[2]), t[3], int(t[4]), int(t[5]), int(t[6]), _fort(t))
                self.state.units[u.id] = u
            elif kind in ("OK", "PART", "ERR"):
                return Reply(reply)
            elif kind == "GAMEOVER":
                self._game_over = reply.split()
                raise GameOver()


def _fort(t):
    """The fortification level on a split UNIT or SEE line. Engines from
    before fortification don't send it."""
    return int(t[7]) if len(t) > 7 else 0


def _read_line():
    line = sys.stdin.readline()
    if not line:
        raise GameOver()
    return line.rstrip("\r\n")


def _parse_init(bot, read=_read_line):
    """Reads init lines up to INIT with read() and sets bot.player, bot.rules, and bot.map."""
    rules = Rules()
    rows, cities, size = [], [], (0, 0)
    while True:
        t = read().split()
        if not t:
            continue
        kw = t[0]
        if kw == "INIT":
            break
        if kw == "PLAYER":
            bot.player = int(t[1])
        elif kw == "MAP":
            size = (int(t[1]), int(t[2]))
        elif kw == "ROW":
            rows.append(t[1])
        elif kw == "CITY":
            cities.append((int(t[1]), (int(t[2]), int(t[3])), int(t[4])))
        elif kw == "TERRAIN":
            rules.terrains[t[1]] = Terrain(t[1], t[2], *[int(x) for x in t[3:7]])
        elif kw == "UNITTYPE":
            rules.unit_types[t[1]] = UnitType(t[1], t[2], *[int(x) for x in t[3:9]], t[9])
        elif kw == "DAMAGE":
            codes = list(rules.unit_types)
            rules.damage[t[1]] = {codes[i]: int(x) for i, x in enumerate(t[2:])}
        elif kw == "RULE":
            rules.constants[t[1]] = int(t[2])
    bot.rules = rules
    bot.map = Map(size[0], size[1], rows, rules)
    bot.map.cities = cities


def _parse_turn(bot, first):
    cities =[City(cid, h[0], h[1], owner) for cid, h, owner in bot.map.cities]
    state = State(bot.player, cities)
    state.round = int(first.split()[1])
    while True:
        t = _read_line().split()
        if not t:
            continue
        kw = t[0]
        if kw == "GO":
            break
        if kw == "TIME":
            state.time_ms, state.bank_ms = int(t[1]), int(t[2])
        elif kw == "GOLD":
            state.gold = int(t[1])
        elif kw == "CITY":
            c = cities[int(t[1])]
            c.owner, c.capturer, c.progress = int(t[2]), int(t[3]), int(t[4])
        elif kw == "UNIT":
            u = Unit(int(t[1]), int(t[2]), t[3], int(t[4]), int(t[5]), int(t[6]), _fort(t))
            if u.owner == bot.player:
                u.mp = bot.rules.unit_types[u.type].move
            state.units[u.id] = u
        elif kw == "LOG":
            state.logs.append(t[1:])
    return state


def run(bot):
    """Plays one game over stdin and stdout."""
    try:
        _parse_init(bot)
        bot.state = None
        bot.on_init()
        bot._send("READY")
        while True:
            first = _read_line()
            kw = first.split(None, 1)[0] if first.strip() else ""
            if kw == "TURN":
                bot._turn_start = time.monotonic()
                bot.state = _parse_turn(bot, first)
                try:
                    bot.on_turn(bot.state)
                except GameOver:
                    raise
                except Exception:
                    traceback.print_exc()
                bot._send("END %d" % bot.state.round)
            elif kw == "GAMEOVER":
                t = first.split()
                bot.on_game_over(int(t[1]), t[2] if len(t) > 2 else "")
                return
            # Anything else is a leftover reply, such as ERR late.
    except GameOver:
        t = getattr(bot, "_game_over", None)
        if t:
            bot.on_game_over(int(t[1]), t[2] if len(t) > 2 else "")
