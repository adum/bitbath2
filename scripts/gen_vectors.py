"""Generates tests/vectors/hex.json and tests/vectors/damage.json.

This is a second, independent implementation of the formulas in SPEC §3.1 and
§3.7. The engine and every kit test against its output, so a disagreement
anywhere shows up as a failing test. tests/vectors/orders.json is written by
hand, not generated.
"""

import json
import os
import random

DIRS = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)]

TERRAIN_DEFENSE = {".": 0, "f": 25, "h": 40, "C": 30}
UNITS = "IPKAS"
MAX_HP = 100
RANGE = {"I": (1, 1), "P": (1, 1), "K": (1, 1), "A": (2, 2), "S": (1, 1)}
BASE = {
    "I": [55, 45, 40, 65, 75],
    "P": [45, 50, 80, 55, 70],
    "K": [70, 35, 55, 85, 90],
    "A": [50, 55, 40, 45, 70],
    "S": [20, 15, 10, 25, 40],
}


def distance(a, b):
    dq, dr = a[0] - b[0], a[1] - b[1]
    return (abs(dq) + abs(dr) + abs(dq + dr)) // 2


def col(h):
    return h[0] + h[1] // 2


def on_map(h, w, hgt):
    return 0 <= h[1] < hgt and 0 <= col(h) < w


def mirror(h, w, hgt):
    c, r = w - 1 - col(h), hgt - 1 - h[1]
    return [c - r // 2, r]


def damage(att, att_hp, dfn, terrain):
    base = BASE[att][UNITS.index(dfn)]
    if base == 0:
        return 0
    return max(1, base * att_hp * (100 - TERRAIN_DEFENSE[terrain]) // (MAX_HP * 100))


def combat(att, att_hp, att_terrain, dfn, dfn_hp, dfn_terrain, dist):
    dfn_hp = max(0, dfn_hp - damage(att, att_hp, dfn, dfn_terrain))
    lo, hi = RANGE[dfn]
    if dfn_hp > 0 and lo <= dist <= hi and BASE[dfn][UNITS.index(att)] > 0:
        att_hp = max(0, att_hp - damage(dfn, dfn_hp, att, att_terrain))
    return dfn_hp, att_hp


def hex_vectors(rng):
    pairs = [((0, 0), (3, -1)), ((-2, 5), (1, 1)), ((4, 4), (4, 4)), ((0, 0), (-3, 3))]
    pairs += [((rng.randint(-9, 9), rng.randint(0, 20)), (rng.randint(-9, 9), rng.randint(0, 20))) for _ in range(30)]
    w, hgt = 10, 8
    storage = []
    for r in range(-1, hgt + 1):
        for q in range(-5, 12, 3):
            storage.append({"width": w, "height": hgt, "hex": [q, r], "col": col((q, r)), "row": r,
                            "on_map": on_map((q, r), w, hgt)})
    mirrors = []
    for _ in range(20):
        r = rng.randint(0, 29)
        c = rng.randint(0, 39)
        h = [c - r // 2, r]
        mirrors.append({"width": 40, "height": 30, "hex": h, "mirror": mirror(h, 40, 30)})
    return {
        "directions": [list(d) for d in DIRS],
        "distance": [{"a": list(a), "b": list(b), "distance": distance(a, b)} for a, b in pairs],
        "neighbors": [{"hex": list(h), "neighbors": [[h[0] + dq, h[1] + dr] for dq, dr in DIRS]}
                      for h in [(0, 0), (2, 3), (-3, 7)]],
        "storage": storage,
        "mirror": mirrors,
    }


def damage_vectors(rng):
    cases = []
    for att in UNITS:
        for dfn in UNITS:
            for terrain in TERRAIN_DEFENSE:
                for hp in (100, 67, 33, 1):
                    cases.append({"attacker": att, "attacker_hp": hp, "defender": dfn, "terrain": terrain,
                                  "damage": damage(att, hp, dfn, terrain)})
    fights = [("I", 100, ".", "I", 100, "h", 1)]  # the worked example in SPEC §3.7
    for _ in range(60):
        att, dfn = rng.choice(UNITS), rng.choice(UNITS)
        dist = rng.choice([1, 1, 2]) if "A" in (att, dfn) else 1
        if not RANGE[att][0] <= dist <= RANGE[att][1]:
            continue
        fights.append((att, rng.randint(1, 100), rng.choice(list(TERRAIN_DEFENSE)),
                       dfn, rng.randint(1, 100), rng.choice(list(TERRAIN_DEFENSE)), dist))
    combats = []
    for att, ahp, aterr, dfn, dhp, dterr, dist in fights:
        d_after, a_after = combat(att, ahp, aterr, dfn, dhp, dterr, dist)
        combats.append({"attacker": att, "attacker_hp": ahp, "attacker_terrain": aterr,
                        "defender": dfn, "defender_hp": dhp, "defender_terrain": dterr, "distance": dist,
                        "defender_hp_after": d_after, "attacker_hp_after": a_after})
    return {"rules": "default", "damage": cases, "combat": combats}


def main():
    rng = random.Random(1)
    out = os.path.join(os.path.dirname(__file__), "..", "tests", "vectors")
    for name, data in [("hex.json", hex_vectors(rng)), ("damage.json", damage_vectors(rng))]:
        with open(os.path.join(out, name), "w", newline="\n") as f:
            json.dump(data, f, indent=1)
            f.write("\n")


if __name__ == "__main__":
    main()
