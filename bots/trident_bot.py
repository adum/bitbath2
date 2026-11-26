"""Trident: an aggressive naval and logistics bot built for Islets and island maps.

Strategy overview:
- Turn-3 transport opening: sends all starting home bread to the neutral port
  immediately while both starting infantry capture it, launching a transport on
  round 3-4 with all four starting units (wagon + 2 infantry + scout) straight
  to the rich central resource island.
- Fast galley timing & sea supremacy: brings home the first middle-island wheat
  load immediately to launch 2-3 galleys ahead of rivals, hunts enemy ferries
  and transports, preserves galley first-strike advantage, and blockades enemy
  ports while staying clear of coastal archers.
- Anti-blockade doctrine: builds inland/coastal archers whenever enemy ships
  threaten or blockade a port, driving off or sinking blockading galleys from
  range 2 with zero retaliation.
- Coordinated amphibious invasions & focus-fire combat: runs two-pass combat
  (before and after naval moves/landings), snipes enemy wagons on the middle
  island, holds defensive hills, synchronizes assault transport waves, and
  optimizes focus-fire kill orders.

Usage: python bots/trident_bot.py
"""

from collections import deque
import heapq
import math
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kits", "python"))
import bitbath as bb  # noqa: E402

DISK2 = bb.disk((0, 0), 2)
DISK3 = bb.disk((0, 0), 3)
RESERVE_MS = 120


class TridentBot(bb.Bot):
    def on_init(self):
        m = self.map
        c = self.rules.constants
        self.types = self.rules.unit_types
        self.limit = self.rules.stack_limit
        self.supply = self.rules.supply
        self.naval = self.supply and self.rules.naval

        self.cargo_max = c.get("cargo_max", 1000)
        self.passengers_max = c.get("passengers_max", 4)
        self.ration_max = c.get("ration_max", 60)
        self.ration_cost = c.get("ration_cost", 5)
        self.city_bread = c.get("city_bread", 10)
        self.fortify_pct = c.get("fortify_pct", 0)
        self.round_limit = c.get("round_limit", 5000)

        # Dynamically identify unit roles from rules.
        self.wagon_code = next((k for k, t in self.types.items() if t.carry), None)
        self.transport_code = next((k for k, t in self.types.items() if t.transport), None)
        self.galley_code = next(
            (k for k, t in self.types.items()
             if t.naval and not t.transport and any(self.rules.damage.get(k, {}).values())),
            None,
        )
        self.archer_code = next(
            (k for k, t in self.types.items()
             if not t.naval and not t.carry and t.range_min > 1
             and (not self.galley_code or self.rules.damage.get(k, {}).get(self.galley_code, 0) > 0)),
            None,
        )
        capturers = [k for k, t in self.types.items() if t.capture and not t.naval]
        self.inf_code = min(capturers, key=lambda k: self.types[k].cost) if capturers else next(iter(self.types))
        fort_caps = [k for k in capturers if self.types[k].fortify]
        self.pike_code = fort_caps[0] if fort_caps else self.inf_code
        cavs = [k for k, t in self.types.items()
                if not t.naval and not t.carry and not t.capture and t.zoc and t.range_max == 1]
        self.cav_code = max(cavs, key=lambda k: self.types[k].move) if cavs else None

        # Precompute terrain tables.
        self.defense = {}
        self.land_nbrs = {}
        self.sea_nbrs = {}
        for h in m.hexes():
            t = m.terrain(h)
            if t.passable:
                self.defense[h] = min(t.defense, 100)
        for h in self.defense:
            self.land_nbrs[h] = [
                (d, n, m.terrain(n).move_cost)
                for d in range(6) for n in [bb.step(h, d)] if n in self.defense
            ]

        self.sea_hexes = {h for h in m.hexes() if m.navigable(h)}
        for h in self.sea_hexes:
            self.sea_nbrs[h] = [
                (d, n, m.terrain(n).naval_move_cost)
                for d in range(6) for n in [bb.step(h, d)] if n in self.sea_hexes
            ]

        # Connected landmasses and seas.
        self.land = self._areas(self.defense)
        self.sea = self._areas(self.sea_hexes)
        self.coast = [h for h in self.sea_hexes if any(n in self.land for n in bb.neighbors(h))]

        self.city_at = {h: cid for cid, h, _ in m.cities}
        self.site_at = {h: sid for sid, h, *_ in m.sites}
        self.city_field = {cid: self.distance_field([h], blocked=set()) for cid, h, _ in m.cities}
        self.site_field = {sid: self.distance_field([h], blocked=set()) for sid, h, *_ in m.sites}
        self.port_seas = {
            cid: {self.sea[n] for n in bb.neighbors(h) if n in self.sea}
            for cid, h, _ in m.cities
        }
        self.port_water = {
            cid: [n for n in bb.neighbors(h) if n in self.sea]
            for cid, h, _ in m.cities
        }

        self.my_home_cid = next((cid for cid, _, owner in m.cities if owner == self.player), None)
        self.enemy_home_cid = next((cid for cid, _, owner in m.cities if owner not in (0, self.player)), None)
        self.home_land = self.land.get(m.cities[self.my_home_cid][1]) if self.my_home_cid is not None else None
        self.enemy_land = self.land.get(m.cities[self.enemy_home_cid][1]) if self.enemy_home_cid is not None else None
        self.my_port_cid = next(
            (cid for cid, h, _ in m.cities if self.land.get(h) == self.home_land and self.port_seas[cid]),
            None,
        )
        self.enemy_port_cid = next(
            (cid for cid, h, _ in m.cities if self.land.get(h) == self.enemy_land and self.port_seas[cid]),
            None,
        )

        # Precompute best coastal sea-vision lookout hex on home island for starting Scout
        self.lookout_hex = self._pick_lookout_hex()
        self.mid_loaded_t_seen = {}
        self.sea_fields = {}
        self.landing_cache = {}
        self.port_sea_dist = 999
        if self.my_port_cid is not None and self.enemy_port_cid is not None:
            en_pw = self.port_water[self.enemy_port_cid]
            my_pw = self.port_water[self.my_port_cid]
            if en_pw and my_pw:
                ef = self.sea_field(en_pw)
                self.port_sea_dist = min((ef.get(h, 999) for h in my_pw), default=999)

        # Persistent memory across turns.
        self.opening_shuttle_done = False
        self.shuttle_wagon = None
        self.ferries = {}          # tid -> {"isle", "port", "phase", "waited", "held", "trips"}
        self.ferry_wagons = {}     # wid -> isle
        self.assaults = {}         # tid -> {"phase", "port", "target", "waited"}
        self.crew_of = {}          # uid -> tid
        self.enemy_ships_seen = {}      # eid -> round
        self.enemy_galleys_seen = {}    # eid -> round
        self.enemy_transports_seen = {} # eid -> round
        self.enemy_transports_first_seen = {} # eid -> first round seen
        self.en_trans_mem = {}          # eid -> (hex, hp, round, has_cargo, is_assault, has_wagon)
        self.enemy_types_seen = {}      # eid -> type code
        self.ghosts = {}                # eid -> (Unit, round)
        self.known_site_wheat = {sid: 0 for sid, *_ in m.sites}
        self.ever_built_galley = False
        self.first_galley_round = None
        self.ever_killed_enemy_galley = False
        self.ever_sank_enemy_transport = False
        self.ever_sank_enemy_ferry = False
        self.sunk_enemy_transports = 0
        self.killed_galley_ids = set()
        self.last_galley_kill_round = None
        self.swept_middle = False
        self.swept_my_crossing = False
        self.mid_patrol_seen = {}       # sea_hex -> last_round_seen
        self.saw_unescorted_mid_foe = False
        self.ever_saw_mid_capturer = False
        self.isle_enemies = {}          # isle -> {eid: (type_code, last_round)}
        self.home_invasion_alert_until = 0
        self.home_raid_seen_hex = None
        self.home_kills_during_alert = 0
        self.saw_home_raid = False
        self.en_assault_t_ids = set()
        self.en_mid_w_seen_round = -99

    def _pick_lookout_hex(self):
        """Find the best coastal hex (preferring hills with +1 vision) on our home island
        for our Scout to spot approaching enemy galleys 4-5 hexes out to sea."""
        if self.home_land is None or self.enemy_port_cid is None or self.my_port_cid is None:
            return None
        en_port_h = self.map.cities[self.enemy_port_cid][1]
        my_port_h = self.map.cities[self.my_port_cid][1]
        home_hexes = [
            h for h, l in self.land.items()
            if l == self.home_land and bb.distance(h, my_port_h) <= 4
            and any(n in self.sea_hexes for n in bb.disk(h, 2))
        ]
        if not home_hexes:
            return None
        scout_code = next((k for k, t in self.types.items() if t.skirmish and not t.naval), None)
        base_vis = self.types[scout_code].vision if scout_code else 4
        best = None
        for h in home_hexes:
            vis = base_vis + self.map.terrain(h).vision_bonus
            sea_seen = [x for x in self.map.sight(h, vis) if x in self.sea_hexes]
            if not sea_seen:
                continue
            min_en = min(bb.distance(x, en_port_h) for x in sea_seen)
            # Prefer hexes that see closest toward enemy port, lots of sea hexes, not sitting on our port itself
            sc = (-min_en * 5.0 + len(sea_seen) * 0.15 - (2.0 if h == my_port_h else 0.0)
                  + self.defense.get(h, 0) * 0.02, -bb.distance(h, my_port_h))
            if best is None or sc > best[0]:
                best = (sc, h)
        return best[1] if best else None

    def _areas(self, valid):
        out, n = {}, 0
        for start in sorted(valid):
            if start in out:
                continue
            out[start] = n
            q = deque([start])
            while q:
                h = q.popleft()
                for x in bb.neighbors(h):
                    if x in valid and x not in out:
                        out[x] = n
                        q.append(x)
            n += 1
        return out

    # ------------------------------------------------------------------
    # State caches and helpers
    # ------------------------------------------------------------------

    def out_of_time(self):
        return self.time_left() < RESERVE_MS

    def worth(self, u):
        t = self.types[u.type]
        return t.cost * u.hp / t.max_hp

    def refresh(self):
        s = self.state
        self.occ = {}
        self.enemies = {}
        self.enemy_hex = set()
        self.zoc = set()
        self.threat = {}
        self.support = {}
        for u in s.units.values():
            if u.carrier:
                continue
            w = self.worth(u)
            q, r = u.q, u.r
            if u.owner == self.player:
                self.occ[u.hex] = self.occ.get(u.hex, 0) + 1
                for dq, dr in DISK2:
                    h = (q + dq, r + dr)
                    self.support[h] = self.support.get(h, 0) + w
            else:
                self.enemies[u.id] = u
                self.enemy_hex.add(u.hex)
                self.enemy_types_seen[u.id] = u.type
                if self.types[u.type].transport and s.round <= 11:
                    pax_rf = s.passengers(u)
                    if (
                        len(pax_rf) >= 3
                        and any(self.types[p.type].capture for p in pax_rf)
                        and not any(self.types[p.type].carry for p in pax_rf)
                    ):
                        self.en_assault_t_ids.add(u.id)
                if self.types[u.type].zoc:
                    self.zoc.update(bb.neighbors(u.hex))
                for dq, dr in DISK3:
                    h = (q + dq, r + dr)
                    self.threat[h] = self.threat.get(h, 0) + w
        vis = self.my_vision()
        for vh in vis:
            if vh in self.sea_hexes:
                self.mid_patrol_seen[vh] = self.state.round
        for site in s.sites:
            if site.hex in vis:
                self.known_site_wheat[site.id] = site.wheat
            else:
                self.known_site_wheat[site.id] = min(site.cap, self.known_site_wheat.get(site.id, 0) + site.output)
        self.n_units = len(s.units)

    def sync(self):
        if len(self.state.units) != self.n_units:
            self.refresh()
            return True
        return False

    def my_vision(self):
        seen = set()
        for c in self.state.my_cities():
            seen.update(self.map.sight(c.hex, self.rules.city_vision))
        for u in self.state.my_units():
            seen.update(self.unit_sight(u))
        return seen

    def sea_field(self, goals):
        key = frozenset(goals)
        if key not in self.sea_fields:
            self.sea_fields[key] = self.distance_field(goals, blocked=set(), naval=True)
        return self.sea_fields[key]

    def landings_for_field(self, key, field, land_id):
        if key not in self.landing_cache:
            best = {}
            for w in self.coast:
                d = min((field[n] for n in bb.neighbors(w)
                         if n in field and self.land.get(n) == land_id), default=None)
                if d is not None:
                    best[w] = d
            low = min(best.values(), default=0)
            self.landing_cache[key] = {w: d for w, d in best.items() if d <= low + 2}
        return self.landing_cache[key]

    def city_landings(self, cid):
        h = self.map.cities[cid][1]
        return self.landings_for_field(("city", cid), self.city_field[cid], self.land.get(h))

    def isle_landings(self, isle):
        sids = [x.id for x in self.state.sites if self.land.get(x.hex) == isle]
        if not sids:
            return {}
        key = ("isle", isle)
        if key not in self.landing_cache:
            best = {}
            for w in self.coast:
                d = min(
                    (self.site_field[sid][n]
                     for sid in sids for n in bb.neighbors(w)
                     if n in self.site_field[sid] and self.land.get(n) == isle),
                    default=None,
                )
                if d is not None:
                    best[w] = d
            low = min(best.values(), default=0)
            self.landing_cache[key] = {w: d for w, d in best.items() if d <= low + 1}
        return self.landing_cache[key]

    def go(self, u, dirs):
        if not dirs:
            return False
        old = u.hex
        old_carrier = u.carrier
        reply = self.move(u, dirs)
        if self.sync():
            return True
        if reply.ok and (u.hex != old or u.carrier != old_carrier):
            self.refresh()
        return False

    def strike_target(self, attacker, target):
        """Attack target and purge enemy memory caches if target dies."""
        tid = target.id
        was_galley = self.types[target.type].naval and not self.types[target.type].transport
        was_transport = self.types[target.type].transport
        pax_t = self.state.passengers(target) if was_transport else ()
        was_assault_t = (
            was_transport
            and len(pax_t) >= 3
            and sum(1 for p in pax_t if self.types[p.type].capture) >= 2
            and not any(self.types[p.type].carry for p in pax_t)
        )
        was_home_land_foe = (not self.types[target.type].naval) and self.land.get(target.hex) == self.home_land
        reply = self.attack(attacker, target)
        if reply and tid not in self.state.units:
            if was_galley:
                self.ever_killed_enemy_galley = True
                self.killed_galley_ids.add(tid)
                self.last_galley_kill_round = self.state.round
            if was_transport:
                self.ever_sank_enemy_transport = True
                self.sunk_enemy_transports += 1
                self.en_assault_t_ids.discard(tid)
                if (not was_assault_t) or self.sunk_enemy_transports >= 2:
                    self.ever_sank_enemy_ferry = True
                if was_assault_t and not any(
                    not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
                    for e in self.enemies.values()
                ):
                    # Drowned the entire enemy invasion force at sea! Clear home raid alert immediately.
                    self.home_invasion_alert_until = 0
                    self.saw_home_raid = False
            if was_home_land_foe and self.state.round <= self.home_invasion_alert_until + 2:
                self.home_kills_during_alert += 1
            self.enemy_ships_seen.pop(tid, None)
            self.enemy_galleys_seen.pop(tid, None)
            self.enemy_transports_seen.pop(tid, None)
            self.en_trans_mem.pop(tid, None)
            self.enemy_types_seen.pop(tid, None)
            self.ghosts.pop(tid, None)
            for mem in self.isle_enemies.values():
                mem.pop(tid, None)
        elif reply and was_transport and tid in self.state.units:
            t_now = self.state.units[tid]
            pax_now = self.state.passengers(t_now)
            has_wag = any(self.types[p.type].carry for p in pax_now)
            is_aslt = (
                len(pax_now) >= 3
                and any(self.types[p.type].capture for p in pax_now)
                and not has_wag
            ) or was_assault_t
            self.en_trans_mem[tid] = (t_now.hex, t_now.hp, self.state.round, len(pax_now) > 0, is_aslt, has_wag)
        self.sync()
        return reply

    def my_ports(self, sea=None):
        return [
            c for c in self.state.my_cities()
            if self.port_seas[c.id] and (sea is None or sea in self.port_seas[c.id])
        ]

    def blockaded(self, city):
        return any(n in self.enemy_hex and n in self.sea_hexes for n in bb.neighbors(city.hex))

    def galley_blockaded(self, city):
        """True only if an enemy combat warship (Galley, NOT a passing Transport!) occupies port water."""
        return any(
            not e.carrier and e.hex in self.sea_hexes and bb.distance(e.hex, city.hex) == 1
            and self.types[e.type].naval and not self.types[e.type].transport
            for e in self.enemies.values()
        )

    def naval_danger(self):
        """Water hexes reachable next turn by visible (+ recent ghost) enemy galleys or shore archers."""
        galley_danger = set()
        shore_danger = set()
        foes = list(self.enemies.values()) + [
            g for gid, (g, rnd) in self.ghosts.items()
            if gid not in self.enemies
            and self.state.round - rnd <= (10 if self.types[g.type].range_min > 1 else 2)
        ]
        for e in foes:
            if e.carrier:
                continue
            et = self.types[e.type]
            if et.naval and any(self.rules.damage.get(e.type, {}).values()):
                # Exact sea distance field so islands don't distort galley strike range!
                sf = self.sea_field([e.hex])
                max_d = et.move + et.range_max
                for h, d in sf.items():
                    if d <= max_d:
                        galley_danger.add(h)
            elif not et.naval and self.transport_code:
                hurts_ship = (
                    self.rules.damage.get(e.type, {}).get(self.transport_code, 0) > 0
                    or (self.galley_code and self.rules.damage.get(e.type, {}).get(self.galley_code, 0) > 0)
                )
                if hurts_ship:
                    for h in bb.disk(e.hex, et.move + et.range_max):
                        if h in self.sea_hexes:
                            shore_danger.add(h)
        return galley_danger, shore_danger

    def land_danger(self):
        """Approximate strike count on each land hex from visible + ghost enemy units."""
        danger = {}
        my_zoc = {n for u in self.state.my_units() if not u.carrier and self.types[u.type].zoc
                  for n in bb.neighbors(u.hex)}
        all_foes = list(self.enemies.values()) + [
            g for gid, (g, _) in self.ghosts.items() if gid not in self.enemies
        ]
        for e in all_foes:
            if e.carrier:
                continue
            et = self.types[e.type]
            if not any(self.rules.damage.get(e.type, {}).values()):
                continue
            if et.naval:
                for h in bb.disk(e.hex, et.move + et.range_max):
                    if h in self.defense:
                        danger[h] = danger.get(h, 0.0) + 0.35 * e.hp / et.max_hp
                continue
            budget = 0 if et.no_move_attack else et.move
            dist = {e.hex: 0}
            heap = [(0, e.hex)]
            while heap:
                cost, h = heapq.heappop(heap)
                if cost > dist[h]:
                    continue
                if h != e.hex and not et.skirmish and h in my_zoc:
                    continue
                for _, n, enter in self.land_nbrs.get(h, ()):
                    c = cost + enter
                    if c <= budget and c < dist.get(n, budget + 1) and self.occ.get(n, 0) == 0:
                        dist[n] = c
                        heapq.heappush(heap, (c, n))
            w = e.hp / et.max_hp
            struck = set()
            for h in dist:
                for r in range(et.range_min, et.range_max + 1):
                    for x in bb.ring(h, r):
                        if x in self.defense:
                            struck.add(x)
            for x in struck:
                danger[x] = danger.get(x, 0.0) + w
        return danger

    def wagon_incoming_damage(self):
        """Exact max 1-turn incoming damage a wagon would take on each land hex from visible + recent ghost enemies."""
        dmg_map = {}
        if not self.wagon_code:
            return dmg_map
        my_zoc = {
            n for u in self.state.my_units()
            if not u.carrier and self.types[u.type].zoc
            for n in bb.neighbors(u.hex)
        }
        all_foes = list(self.enemies.values()) + [
            g for gid, (g, rnd) in self.ghosts.items()
            if gid not in self.enemies and self.state.round - rnd <= 2
        ]
        for e in all_foes:
            if e.carrier:
                continue
            et = self.types[e.type]
            if et.transport:
                pax_caps = [
                    p for p in self.state.passengers(e)
                    if self.rules.damage.get(p.type, {}).get(self.wagon_code, 0) > 0
                ]
                if pax_caps:
                    sf_e = self.sea_field([e.hex])
                    my_heavy_units = [
                        u for u in self.state.my_units()
                        if not u.carrier and not self.types[u.type].naval
                        and not self.types[u.type].carry and not self.types[u.type].skirmish
                    ]
                    my_guarded_lands = {self.land.get(u.hex) for u in my_heavy_units}
                    beaches = {
                        b for sh, sd in sf_e.items() if sd <= et.move
                        for b in bb.neighbors(sh)
                        if b in self.defense and self.occ.get(b, 0) == 0
                        and (
                            sd <= 2
                            or self.land.get(b) not in my_guarded_lands
                            or (
                                self.land.get(b) == self.home_land
                                and len(pax_caps) >= 2
                                and not any(bb.distance(u.hex, b) <= 2 for u in my_heavy_units)
                            )
                        )
                    }
                    struck = {
                        x for b in beaches for x in bb.ring(b, 1)
                        if x in self.defense
                    }
                    for x in struck:
                        # A city actively garrisoned by our own Infantry (+30% city defense!) cannot be
                        # disembarked onto or 1-shot-wiped by a seaborne transport!
                        if x in self.city_at and any(
                            u.hex == x and not u.carrier and self.types[u.type].capture
                            for u in self.state.my_units()
                        ):
                            continue
                        for p in pax_caps:
                            dmg_map[x] = dmg_map.get(x, 0) + self.damage(p.type, p.hp, self.wagon_code, x)
                elif (
                    self.state.round <= 14
                    and self.home_land is not None
                    and any(self.land.get(n) == self.home_land for n in bb.disk(e.hex, 2))
                ):
                    # An empty enemy Transport hugging our home coast early (r<=14) JUST dropped 2 I + S into fog!
                    for x in bb.disk(e.hex, 4):
                        if self.land.get(x) != self.home_land or x not in self.defense:
                            continue
                        if x in self.city_at and any(
                            u.hex == x and not u.carrier and self.types[u.type].capture
                            for u in self.state.my_units()
                        ):
                            continue
                        dmg_map[x] = dmg_map.get(x, 0) + 140
                continue
            base = self.rules.damage.get(e.type, {}).get(self.wagon_code, 0)
            if base <= 0:
                continue
            if et.naval:
                sf_e = self.sea_field([e.hex])
                struck = {
                    h for sh, sd in sf_e.items() if sd <= et.move
                    for h in bb.ring(sh, 1) if h in self.defense
                }
                for h in struck:
                    dmg_map[h] = dmg_map.get(h, 0) + self.damage(e.type, e.hp, self.wagon_code, h)
                continue
            budget = 0 if et.no_move_attack else et.move
            dist = {e.hex: 0}
            heap = [(0, e.hex)]
            while heap:
                cost, h = heapq.heappop(heap)
                if cost > dist[h]:
                    continue
                if h != e.hex and not et.skirmish and h in my_zoc:
                    continue
                for _, n, enter in self.land_nbrs.get(h, ()):
                    c = cost + enter
                    if c <= budget and c < dist.get(n, budget + 1) and self.occ.get(n, 0) == 0:
                        dist[n] = c
                        heapq.heappush(heap, (c, n))
            struck = set()
            for h in dist:
                for r in range(et.range_min, et.range_max + 1):
                    for x in bb.ring(h, r):
                        if x in self.defense:
                            struck.add(x)
            for x in struck:
                dmg_map[x] = dmg_map.get(x, 0) + self.damage(e.type, e.hp, self.wagon_code, x)
        return dmg_map

    def update_isle_memory(self):
        """Maintain fog-of-war memory of enemy land forces on resource islands so we never
        send unescorted wagons into an island after losing vision of it."""
        s = self.state
        r_isles = self.resource_isles()
        vis = self.my_vision()
        for vh in vis:
            if vh in self.sea_hexes:
                self.mid_patrol_seen[vh] = self.state.round
        for e in self.enemies.values():
            if e.carrier or self.types[e.type].naval:
                continue
            el = self.land.get(e.hex)
            for isle, mem in self.isle_enemies.items():
                if isle != el:
                    mem.pop(e.id, None)
            if el in r_isles:
                self.isle_enemies.setdefault(el, {})[e.id] = (e.type, s.round)
                if self.types[e.type].capture:
                    self.ever_saw_mid_capturer = True
                if self.types[e.type].carry:
                    self.en_mid_w_seen_round = s.round

        for isle, sites in r_isles.items():
            mem = self.isle_enemies.get(isle)
            if not mem:
                continue
            # Check if we currently see all sites and all their adjacent land hexes
            isle_hexes = {h for h, l in self.land.items() if l == isle}
            full_vis = isle_hexes.issubset(vis)
            vis_ids = {
                e.id for e in self.enemies.values()
                if not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == isle
            }
            for eid in list(mem):
                if eid in vis_ids:
                    continue
                _, rnd = mem[eid]
                if full_vis or (s.round - rnd > 24):
                    del mem[eid]

    def isle_known_enemies(self, isle):
        """Count of visible + remembered enemy combat land units on a resource island."""
        vis_combat = {
            e.id for e in self.enemies.values()
            if not e.carrier and not self.types[e.type].naval and not self.types[e.type].carry
            and self.land.get(e.hex) == isle
        }
        mem_combat = {
            eid for eid, (code, _) in self.isle_enemies.get(isle, {}).items()
            if not self.types[code].carry
        }
        return len(vis_combat | mem_combat)

    def isle_known_capturers(self, isle):
        """Count of visible + remembered enemy non-skirmish combat land units (Infantry/Pike/etc.) on a resource island."""
        vis_heavy = {
            e.id for e in self.enemies.values()
            if not e.carrier and not self.types[e.type].naval
            and not self.types[e.type].carry and not self.types[e.type].skirmish
            and self.land.get(e.hex) == isle
        }
        mem_heavy = {
            eid for eid, (code, _) in self.isle_enemies.get(isle, {}).items()
            if not self.types[code].carry and not self.types[code].skirmish
        }
        return len(vis_heavy | mem_heavy)

    def is_home_raid_transport(self, e, allow_empty_early=False):
        """True only if enemy transport e is genuinely threatening our home island (NOT an enemy
        middle-island ferry parked next to a resource island waiting for its wagon!)."""
        s = self.state
        if not self.types[e.type].transport or self.home_land is None:
            return False
        home_hexes = [c.hex for c in s.my_cities() if self.land.get(c.hex) == self.home_land]
        home_hexes += [x.hex for x in s.sites if self.land.get(x.hex) == self.home_land]
        if not home_hexes:
            return False
        d_home = min(bb.distance(e.hex, h) for h in home_hexes)
        pax = s.passengers(e)
        cap_cnt = sum(1 for p in pax if self.types[p.type].capture)
        has_cap = cap_cnt > 0
        pure_combat_raid = len(pax) >= 3 and has_cap and not any(self.types[p.type].carry for p in pax)
        # A 3+ combat-unit transport with NO wagon (Breakwater v7's 3 I + S rush!) is ALWAYS a home raid,
        # even when sailing past a middle resource island on its way to our home coast!
        if pure_combat_raid and d_home <= 11:
            return True
        r_isles = self.resource_isles()
        if any(self.land.get(n) in r_isles for n in bb.neighbors(e.hex)):
            return False
        mid_sites = [x.hex for x in s.sites if self.land.get(x.hex) not in (self.home_land, self.enemy_land)]
        d_mid = min((bb.distance(e.hex, h) for h in mid_sites), default=99)
        en_home_hexes = [c.hex for c in s.cities if c.owner not in (0, self.player) and self.land.get(c.hex) == self.enemy_land]
        if self.enemy_port_cid is not None:
            en_home_hexes.append(s.cities[self.enemy_port_cid].hex)
        d_en_home = min((bb.distance(e.hex, h) for h in en_home_hexes), default=99)
        # Armada 2-I (+ W + S) Home-Port Raid: sails past middle toward our Home Port keeping 2 Infantry aboard!
        if cap_cnt >= 2 and d_home <= 7 and d_home < d_en_home:
            return True
        near_home_coast = any(self.land.get(n) == self.home_land for n in bb.disk(e.hex, 2))
        if has_cap and (near_home_coast or (d_home <= 3 and d_home < d_mid)):
            return True
        if allow_empty_early and d_home <= 3 and d_home < d_mid and s.round <= 11 and self.port_sea_dist <= 21:
            return True
        return False

    def update_home_alert(self):
        """Track imminent or active amphibious invasions of our home island (even if invaders land in fog!)."""
        s = self.state
        if self.home_land is None:
            return
        home_hexes = [c.hex for c in s.my_cities() if self.land.get(c.hex) == self.home_land]
        home_hexes += [x.hex for x in s.sites if self.land.get(x.hex) == self.home_land]
        if not home_hexes:
            return
        r_isles = self.resource_isles()
        mid_sites_a = [x.hex for sites_a in r_isles.values() for x in sites_a]
        en_caps_mid = sum(
            1 for e in self.enemies.values()
            if not e.carrier and not self.types[e.type].naval and self.types[e.type].capture
            and self.land.get(e.hex) in r_isles
        )
        my_caps_mid = sum(
            1 for u in s.my_units()
            if not u.carrier and self.types[u.type].capture and self.land.get(u.hex) in r_isles
        )
        my_caps_home = sum(
            1 for u in s.my_units()
            if not u.carrier and self.types[u.type].capture and self.land.get(u.hex) == self.home_land
        )
        my_port_h = s.cities[self.my_port_cid].hex if self.my_port_cid is not None else home_hexes[0]
        # Track loaded enemy 2-I Transports spotted near middle early (r<=9) so if one bypasses middle
        # into fog without unloading (Armada RAIDS=True!), we instantly trigger Home-Raid alert!
        vanished_mid_raid = False
        if s.round <= 10 and mid_sites_a:
            for e in self.enemies.values():
                if self.types[e.type].transport:
                    cap_e = sum(1 for p in s.passengers(e) if self.types[p.type].capture)
                    if cap_e >= 2 and min(bb.distance(e.hex, sh) for sh in mid_sites_a) <= 7:
                        prev = self.mid_loaded_t_seen.get(e.id)
                        if (
                            prev is not None
                            and s.round > prev[1]
                            and my_caps_home == 0
                            and en_caps_mid == 0
                            and bb.distance(e.hex, my_port_h) < bb.distance(prev[0], my_port_h)
                        ):
                            vanished_mid_raid = True
                        self.mid_loaded_t_seen[e.id] = (e.hex, s.round)
            if my_caps_home == 0 and my_caps_mid >= 2 and en_caps_mid == 0:
                for eid, (prev_h, seen_r) in list(self.mid_loaded_t_seen.items()):
                    if 1 <= s.round - seen_r <= 2 and eid not in self.enemies:
                        vanished_mid_raid = True
                    elif s.round - seen_r > 3:
                        self.mid_loaded_t_seen.pop(eid, None)
        en_home = [
            e for e in self.enemies.values()
            if not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
        ]
        en_trans_near = [e for e in self.enemies.values() if self.is_home_raid_transport(e, allow_empty_early=True)]
        if en_home:
            if s.round > self.home_invasion_alert_until:
                self.home_kills_during_alert = 0
            self.home_invasion_alert_until = max(self.home_invasion_alert_until, s.round + 3)
            self.home_raid_seen_hex = en_home[0].hex
            if s.round <= 12:
                self.saw_home_raid = True
        elif en_trans_near:
            if s.round > self.home_invasion_alert_until:
                self.home_kills_during_alert = 0
            # Loaded transport near our coast means troops may land next turn — short 2-turn alert
            # (refreshes automatically if they actually land; avoids freezing our economy after they abort!).
            if any(len(s.passengers(e)) > 0 for e in en_trans_near):
                self.home_invasion_alert_until = max(self.home_invasion_alert_until, s.round + 2)
            else:
                self.home_invasion_alert_until = max(self.home_invasion_alert_until, s.round + 1)
            self.home_raid_seen_hex = en_trans_near[0].hex
        elif vanished_mid_raid:
            if s.round > self.home_invasion_alert_until:
                self.home_kills_during_alert = 0
            self.home_invasion_alert_until = max(self.home_invasion_alert_until, s.round + 2)
            self.home_raid_seen_hex = my_port_h
        elif s.round <= self.home_invasion_alert_until and (
            self.home_kills_during_alert >= 2
            or en_caps_mid >= 1
            or (
                my_caps_home >= 2
                and s.round >= self.home_invasion_alert_until - 1
                and (not self.en_assault_t_ids or self.ever_sank_enemy_transport)
            )
        ):
            # Invasion wave crushed or aborted (enemy Infantry landed on middle or home garrison in place)!
            self.home_invasion_alert_until = 0
            self.home_kills_during_alert = 0

    def home_invasion_active(self):
        s = self.state
        if any(
            c.owner not in (0, self.player) and self.land.get(c.hex) == self.home_land
            for c in s.cities
        ):
            return True
        if any(not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
               for e in self.enemies.values()):
            return True
        return s.round <= self.home_invasion_alert_until

    def safe_sail(self, ship, field, avoid=(), hard_avoid=()):
        """Move ship toward goals of field while respecting danger hexes."""
        if ship.fresh or ship.acted or ship.mp <= 0:
            return False
        reach = self.reachable(ship)
        reach[ship.hex] = ""
        best = None
        for h, dirs in reach.items():
            d = field.get(h)
            if d is None:
                continue
            pen = (14 if h in hard_avoid else 0) + (5 if h in avoid else 0)
            if self.types[ship.type].transport and h != ship.hex:
                if any(u.id != ship.id and self.types[u.type].transport and u.hex == h
                       for u in self.state.my_units()):
                    pen += 6
            key = (d + pen, len(dirs), h)
            if best is None or key < best[0]:
                best = (key, dirs)
        if best and best[1]:
            self.go(ship, best[1])
            return True
        return False

    def make_room(self, city):
        """Move a non-wagon unit off a full city so a wagon can enter."""
        if self.occ.get(city.hex, 0) < self.limit:
            return
        cities = {c.hex for c in self.state.cities}
        for u in sorted(self.state.my_units(), key=lambda x: x.id):
            if u.hex != city.hex or u.carrier or u.fresh or u.acted or u.mp <= 0:
                continue
            if self.types[u.type].carry or u.id in self.crew_of:
                continue
            reach = self.reachable(u)
            opts = [
                (self.defense.get(h, 0), -len(dirs), dirs)
                for h, dirs in reach.items()
                if dirs and h in self.defense and h not in cities
            ]
            if opts:
                self.go(u, max(opts)[2])
                return

    def move_unit_toward(self, u, field, danger, caution=1.0, prefer_city=None):
        if u.fresh or u.acted or u.mp <= 0 or u.carrier:
            return False
        reach = self.reachable(u)
        reach[u.hex] = ""
        ut = self.types[u.type]
        best = None
        for h, dirs in reach.items():
            if h not in self.defense:
                continue
            d = field.get(h)
            if d is None:
                continue
            if not ut.capture and not ut.carry and h in self.city_at:
                if self.state.cities[self.city_at[h]].owner != self.player:
                    continue
            risk = danger.get(h, 0.0) * (1.0 - self.defense[h] / 100.0)
            score = -d - caution * 9.0 * min(5.0, risk)
            if risk > 0 or self.threat.get(h, 0) > 0:
                score += self.defense[h] / 34.0
            if prefer_city is not None and h == prefer_city:
                score += 0.5
            key = (score, -len(dirs))
            if best is None or key > best[0]:
                best = (key, dirs)
        if best and best[1]:
            self.go(u, best[1])
            return True
        return False

    def enter_city(self, w, city, danger):
        if w.hex == city.hex or w.fresh or w.acted or w.mp <= 0 or w.carrier:
            return
        if bb.distance(w.hex, city.hex) <= self.types[w.type].move:
            self.make_room(city)
        reach = self.reachable(w)
        if city.hex in reach:
            self.go(w, reach[city.hex])
        else:
            self.move_unit_toward(w, self.city_field[city.id], danger, caution=1.5, prefer_city=city.hex)

    def board_transport(self, u, t):
        if u.carrier or u.fresh or u.acted or u.mp <= 0:
            return False
        reach = self.reachable(u)
        if t.hex not in reach:
            return False
        first = self.transport_with_room(t.hex)
        if first is not None and first.id == t.id:
            self.go(u, reach[t.hex])
            return True
        return False

    def trip_home_rations(self, ship):
        sea = self.sea.get(ship.hex)
        goals = [h for c in self.my_ports(sea) for h in self.port_water[c.id] if self.sea.get(h) == sea]
        if not goals:
            return 0
        d = self.sea_field(goals).get(ship.hex)
        if d is None:
            return 0
        return 2 * (d // max(1, self.types[ship.type].move) + 1)

    def berth_hexes(self, t, port, is_ferry=False):
        sea = self.sea.get(t.hex)
        pw = [h for h in self.port_water[port.id] if self.sea.get(h) == sea]
        taken = {
            u.hex for u in self.state.my_units()
            if u.id != t.id and self.types[u.type].transport
        }
        free = [h for h in pw if h not in taken]
        if free or is_ferry or t.rations < self.ration_max // 3:
            return free or pw
        home_l = self.land.get(port.hex)
        near_coast = [
            h for h in self.coast
            if self.sea.get(h) == sea and h not in taken
            and bb.distance(h, port.hex) <= 3
            and any(self.land.get(n) == home_l for n in bb.neighbors(h))
        ]
        if near_coast:
            low = min(bb.distance(h, port.hex) for h in near_coast)
            return [h for h in near_coast if bb.distance(h, port.hex) == low]
        return pw

    def dock_transport(self, t, port_id, is_ferry=False):
        s = self.state
        sea = self.sea.get(t.hex)
        ports = self.my_ports(sea)
        if not ports:
            return None, False
        port = s.cities[port_id] if (port_id is not None and s.cities[port_id].owner == self.player
                                      and sea in self.port_seas[port_id]) else None
        if port is None:
            port = min(
                ports,
                key=lambda c: (
                    self.land.get(c.hex) != self.home_land,
                    self.sea_field(self.port_water[c.id]).get(t.hex, 1 << 30),
                    c.id,
                ),
            )
        berths = self.berth_hexes(t, port, is_ferry=is_ferry)
        if t.hex in berths:
            return port, True
        galley_d, shore_d = self.naval_danger()
        self.safe_sail(t, self.sea_field(berths), avoid=shore_d, hard_avoid=galley_d)
        return port, (t.id in s.units and t.hex in berths)

    # ------------------------------------------------------------------
    # Food & anti-starvation
    # ------------------------------------------------------------------

    def food_spots(self):
        if not self.supply:
            return set()
        enough = 3 * self.ration_cost
        sources = [c.hex for c in self.state.my_cities() if c.bread >= enough]
        sources += [
            u.hex for u in self.state.my_units()
            if self.types[u.type].carry and u.bread >= enough
        ]
        spots = set()
        for h in sources:
            for n in [h] + bb.neighbors(h):
                if n in self.defense:
                    spots.add(n)
        return spots

    def handle_starving_units(self, danger):
        self.hungry_busy = set()
        if not self.supply:
            return
        spots = self.food_spots()
        self.food_field = self.distance_field(spots) if spots else {}
        if not spots:
            return
        for u in self.state.my_units():
            if u.carrier or u.fresh or u.acted or self.types[u.type].naval or self.types[u.type].carry:
                continue
            d = self.food_field.get(u.hex)
            if d is None:
                continue
            turns = math.ceil(d / max(1, self.types[u.type].move))
            if u.rations > 2 * turns + 4:
                continue
            self.hungry_busy.add(u.id)
            if self.can_capture(u):
                self.capture(u)
            elif d > 0 and not self.out_of_time():
                self.move_unit_toward(u, self.food_field, danger, caution=1.0)

    # ------------------------------------------------------------------
    # Phase 0: Captures & Turn-1 Full-Bread Shuttle to Port
    # ------------------------------------------------------------------

    def capture_in_place(self):
        by_city = {}
        for u in self.state.my_units():
            if self.can_capture(u):
                cid = self.city_at[u.hex]
                by_city.setdefault(cid, []).append(u)
        for cid, units in by_city.items():
            c = self.state.cities[cid]
            need = self.rules.capture_points - (c.progress if c.capturer == self.player else 0)
            finishers = [u for u in units if u.hp >= need]
            if finishers:
                self.capture(min(finishers, key=lambda u: (u.hp, u.id)))
                continue
            for u in sorted(units, key=lambda u: (-u.hp, u.id)):
                if c.owner == self.player:
                    break
                self.capture(u)

    def rush_local_cities(self):
        s = self.state
        me = self.player
        foreign = [c for c in s.cities if c.owner != me and c.hex not in self.enemy_hex]
        if not foreign:
            return
        foreign.sort(key=lambda c: (
            0 if (c.owner == 0 and self.land.get(c.hex) == self.home_land and self.port_seas[c.id]) else
            1 if (c.owner != 0 and self.port_seas[c.id]) else
            2 if c.owner != 0 else 3,
            c.id,
        ))
        caps = [
            u for u in s.my_units()
            if not u.carrier and not u.fresh and not u.acted and u.mp > 0
            and self.types[u.type].capture and u.id not in self.crew_of and u.id not in self.hungry_busy
        ]
        if not caps:
            return
        reaches = {u.id: self.reachable(u) for u in caps}
        used = set()
        for c in foreign:
            if c.owner == me or c.hex in self.enemy_hex or self.out_of_time():
                continue
            room = self.limit - self.occ.get(c.hex, 0)
            if room <= 0:
                continue
            group = [u for u in caps if u.id not in used and c.hex in reaches[u.id]]
            if not group:
                continue
            group.sort(key=lambda u: (-u.hp, u.id))
            need = self.rules.capture_points - (c.progress if c.capturer == me else 0)
            if sum(u.hp for u in group[:room]) >= need:
                chosen, acc = [], 0
                for u in group[:room]:
                    chosen.append(u)
                    acc += u.hp
                    if acc >= need:
                        break
            else:
                strength = self.support.get(c.hex, 0) + sum(self.worth(u) for u in group[:room])
                if self.threat.get(c.hex, 0) > 1.3 * strength:
                    continue
                chosen = group[:room]
            for u in chosen:
                if c.owner == me or c.hex in self.enemy_hex or self.out_of_time():
                    break
                used.add(u.id)
                dirs = reaches[u.id].get(c.hex)
                if not dirs:
                    continue
                if self.go(u, dirs):
                    reaches = {x.id: self.reachable(x) for x in caps if x.id not in used and x.id in s.units}
                if u.id in s.units and u.hex == c.hex and self.can_capture(u):
                    self.capture(u)

    def run_opening_shuttle(self, danger):
        """Load ALL starting home bread on Round 1 and rush it to our port so G1
        (or T1 on no-home-site maps) launches on Round 3-4 with maximum leftover bread!"""
        if not self.naval or self.opening_shuttle_done or self.my_home_cid is None or not self.wagon_code:
            return
        s = self.state
        if any(u.type in (self.transport_code, self.galley_code) for u in s.my_units()):
            self.opening_shuttle_done = True
            self.shuttle_wagon = None
            return
        home_ports = [c for c in s.cities if self.land.get(c.hex) == self.home_land and self.port_seas[c.id]]
        if not home_ports:
            self.opening_shuttle_done = True
            return
        port = home_ports[0]
        t_cost = self.types[self.transport_code].cost if self.transport_code else 150
        g_cost = self.types[self.galley_code].cost if self.galley_code else t_cost
        galley_open = (self.port_sea_dist <= 21 and bool(self.resource_isles()) and self.galley_code is not None)
        need_open_cost = g_cost if galley_open else t_cost
        if port.owner == self.player and port.bread >= need_open_cost:
            self.opening_shuttle_done = True
            self.shuttle_wagon = None
            return

        if self.shuttle_wagon is None or self.shuttle_wagon not in s.units:
            wagons = [
                u for u in s.my_units()
                if u.type == self.wagon_code and not u.fresh and not u.carrier
                and self.land.get(u.hex) == self.home_land
            ]
            if not wagons:
                return
            self.shuttle_wagon = min(wagons, key=lambda u: u.id).id
        w = s.units[self.shuttle_wagon]
        if w.fresh or w.acted or w.carrier:
            return

        home = s.cities[self.my_home_cid]
        if w.bread > 0:
            reach = self.reachable(w)
            if port.owner == self.player and not port.built and port.hex in reach:
                self.make_room(port)
                reach = self.reachable(w)
                if port.hex in reach:
                    self.go(w, reach[port.hex])
                    if w.hex == port.hex and (w.bread > 0 or w.wheat > 0):
                        self.unload(w)
                        self.opening_shuttle_done = True
                        self.shuttle_wagon = None
                    return
            # Minimize ration drain while carrying starting bread to Port!
            # Staying clear of troops on r2 preserves ~285-295 bread -> G1 on r3 with ~90 spare bread
            # so Port can IMMEDIATELY build an emergency 3rd Infantry on r5 if rushed!
            reach[w.hex] = ""
            stride = max(1, self.types[w.type].move)
            others = [u for u in s.my_units() if u.id != w.id and not u.carrier]
            best = None
            for h, dirs in reach.items():
                if h not in self.defense:
                    continue
                d = self.city_field[port.id].get(h)
                if d is None:
                    continue
                turns = max(1, (d + stride - 1) // stride)
                drain = sum(
                    (self.ration_max - u.rations + 2)
                    for u in others if bb.distance(h, u.hex) <= 1
                )
                # NEVER end a turn adjacent to troops if their ration refill would drop our bread below opening ship cost!
                proj_left = w.bread - (2 * self.ration_cost) * turns - drain * self.ration_cost
                shortfall = 1 if (proj_left < need_open_cost) else 0
                key = (shortfall, turns, drain, bb.distance(h, port.hex) <= 1, d, len(dirs), h)
                if best is None or key < best[0]:
                    best = (key, dirs)
            if best and best[1]:
                self.go(w, best[1])
            return

        # Load starting bread for Port to launch r3 G1 (200) on compact Islets (crushing Transport-first
        # Breakwater v10 & Armada while I1+I2 garrison Home Port!) or r3 T1 (150) on wide Islands,
        # while leaving enough bread in Home City to build W2 on Round 3-4!
        if home.owner == self.player and home.bread > 0:
            if w.hex != home.hex:
                self.enter_city(w, home, danger)
            if w.hex == home.hex and home.bread > 0 and (self.cargo_max - w.wheat - w.bread) > 0:
                home_sites_exist = any(self.land.get(x.hex) == self.home_land for x in s.sites)
                if s.round == 1 and home_sites_exist and self.transport_code and self.wagon_code:
                    w_cost = self.types[self.wagon_code].cost
                    if galley_open:
                        shipment = min(home.bread, max(g_cost + 25, home.bread - 75))
                    else:
                        shipment = min(home.bread, max(t_cost + 20, home.bread - w_cost))
                    if 0 < shipment < home.bread:
                        self.load(w, shipment)
                    else:
                        self.load(w)
                else:
                    self.load(w)

    # ------------------------------------------------------------------
    # Phase 1: Sea Warfare (Galleys), Ferries, and Assault Transports
    # ------------------------------------------------------------------

    def resource_isles(self):
        s = self.state
        city_lands = {self.land.get(c.hex) for c in s.cities}
        out = {}
        for x in s.sites:
            l = self.land.get(x.hex)
            if l is not None and l not in city_lands:
                out.setdefault(l, []).append(x)
        return out

    def isle_status(self, isle):
        s = self.state
        my_g = [
            u for u in s.my_units()
            if not u.carrier and self.land.get(u.hex) == isle
            and not self.types[u.type].naval and not self.types[u.type].carry
        ]
        en_l = [
            e for e in self.enemies.values()
            if not e.carrier and self.land.get(e.hex) == isle and not self.types[e.type].naval
        ]
        my_w = [
            u for u in s.my_units()
            if not u.carrier and self.land.get(u.hex) == isle and self.types[u.type].carry
        ]
        return my_g, en_l, my_w

    def sea_turn(self, danger):
        if not self.naval:
            return
        s = self.state
        for e in self.enemies.values():
            if self.types[e.type].naval:
                self.enemy_ships_seen[e.id] = s.round
                if not self.types[e.type].transport:
                    self.enemy_galleys_seen[e.id] = s.round
            if self.types[e.type].transport:
                self.enemy_transports_seen[e.id] = s.round
                self.enemy_transports_first_seen.setdefault(e.id, s.round)
                pax_st = s.passengers(e)
                has_wag_st = any(self.types[p.type].carry for p in pax_st)
                is_aslt_st = (
                    len(pax_st) >= 3
                    and any(self.types[p.type].capture for p in pax_st)
                    and not has_wag_st
                )
                prev_m = self.en_trans_mem.get(e.id)
                if prev_m and len(prev_m) >= 5 and prev_m[4] and len(pax_st) > 0:
                    is_aslt_st = True
                self.en_trans_mem[e.id] = (e.hex, e.hp, s.round, len(pax_st) > 0, is_aslt_st, has_wag_st)

        self.ferries = {tid: f for tid, f in self.ferries.items() if tid in s.units}
        self.ferry_wagons = {wid: i for wid, i in self.ferry_wagons.items() if wid in s.units}
        self.assaults = {tid: a for tid, a in self.assaults.items() if tid in s.units and tid not in self.ferries}
        self.crew_of = {uid: tid for uid, tid in self.crew_of.items() if uid in s.units and tid in s.units}

        self._assign_fleet_roles()
        self.run_galleys()
        ships_before = {e.id for e in self.enemies.values() if self.types[e.type].naval}
        self.run_ferries(danger)
        self.run_assaults(danger)
        self.refresh()
        ships_after = {e.id for e in self.enemies.values() if self.types[e.type].naval}
        if (ships_after - ships_before) and self.galley_code and any(
            u.type == self.galley_code and not u.fresh and not u.acted and u.mp > 0
            for u in s.my_units()
        ):
            self.run_galleys()
            self.refresh()

    def _assign_fleet_roles(self):
        s = self.state
        if not self.transport_code:
            return
        r_isles = self.resource_isles()
        served = {f["isle"] for f in self.ferries.values()}
        transports = sorted(
            (u for u in s.my_units() if u.type == self.transport_code),
            key=lambda u: u.id,
        )
        for isle in sorted(r_isles):
            if isle in served:
                continue
            spots = self.isle_landings(isle)
            if not spots:
                continue
            isle_seas = {self.sea[w] for w in spots}
            cand = None
            for t in transports:
                if t.id in self.ferries or self.sea.get(t.hex) not in isle_seas:
                    continue
                if t.id in self.assaults and s.passengers(t):
                    continue
                cand = t
                break
            if cand is not None:
                self.assaults.pop(cand.id, None)
                for uid in [u for u, tid in self.crew_of.items() if tid == cand.id]:
                    del self.crew_of[uid]
                ports = self.my_ports(self.sea.get(cand.hex))
                pid = ports[0].id if ports else None
                self.ferries[cand.id] = {"isle": isle, "port": pid, "phase": "dock", "waited": 0, "held": 0, "trips": 0}
                served.add(isle)

        for t in transports:
            if t.id not in self.ferries and t.id not in self.assaults:
                ports = self.my_ports(self.sea.get(t.hex))
                pid = ports[0].id if ports else None
                self.assaults[t.id] = {"phase": "gather", "port": pid, "target": None, "waited": 0}

    def run_galleys(self):
        """Coordinated focus-fire naval warfare with step-by-step fog scouting,
        exact first-strike intercept spacing, and port blockading."""
        s = self.state
        if not self.galley_code:
            return

        # Re-enter combat if step-by-step patrol spots a new enemy ship mid-turn!
        retreating = set()
        my_pw_heal = set(self.port_water[self.my_port_cid]) if self.my_port_cid is not None else set()
        my_port_h_heal = s.cities[self.my_port_cid].hex if self.my_port_cid is not None else None
        for _pass in range(3):
            galleys = sorted(
                (u for u in s.my_units() if u.type == self.galley_code and not u.fresh and not u.acted),
                key=lambda u: u.id,
            )
            if not galleys or self.out_of_time():
                break

            healthy = []
            need_home_galleys = []
            for g in galleys:
                if g.id not in s.units or self.out_of_time():
                    continue
                if g.id in retreating:
                    continue
                en_g_vis = any(
                    self.types[e.type].naval and not self.types[e.type].transport
                    for e in self.enemies.values()
                )
                en_t_near_port = my_port_h_heal is not None and any(
                    self.types[e.type].transport and bb.distance(e.hex, my_port_h_heal) <= 7
                    for e in self.enemies.values()
                )
                en_home_vis = any(
                    not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
                    for e in self.enemies.values()
                )
                # Heal at Port whenever <= 57 HP (a fresh 100-HP enemy Galley deals 55 dmg and one-shots 50-HP galleys!)
                # CRITICAL EXCEPTION (post_kill_t1_hunt): Right after killing enemy G1 when enemy built T1 / landed
                # on middle (and no other enemy Galley is visible!), STAY AT SEA for 5 turns at >= 42 HP to immediately
                # chase down & sink enemy returning T1 (800-1000 wheat!) BEFORE enemy can deliver wheat to fund G2+G3!
                mid_foes_known = any(self.isle_known_enemies(i) > 0 for i in self.resource_isles())
                en_t_vis_h = any(self.types[e.type].transport for e in self.enemies.values())
                en_pw_h = [h for h in self.port_water[self.enemy_port_cid] if self.sea.get(h) == self.sea.get(g.hex)] if self.enemy_port_cid is not None else []
                en_f_h = self.sea_field(en_pw_h) if en_pw_h else {}
                en_t_safe_vis = any(
                    self.types[e.type].transport and en_f_h.get(e.hex, 99) >= 7
                    for e in self.enemies.values()
                )
                hunt_window = 4 if en_t_safe_vis else 3
                post_kill_t1_hunt = (
                    self.last_galley_kill_round is not None
                    and s.round <= self.last_galley_kill_round + hunt_window
                    and (bool(self.enemy_transports_seen) or mid_foes_known)
                    and not en_g_vis
                    and not en_home_vis
                    and g.hex not in my_pw_heal
                )
                at_port_under_attack = (
                    g.hex in my_pw_heal
                    and g.hp >= 35
                    and any(
                        self.types[e.type].naval and not self.types[e.type].transport
                        and bb.distance(g.hex, e.hex) <= self.types[e.type].move + 1
                        for e in self.enemies.values()
                    )
                )
                post_ferry_heal = (
                    self.ever_sank_enemy_ferry
                    and not en_g_vis
                    and not en_t_vis_h
                    and not en_home_vis
                    and len(galleys) == 1
                    and s.round <= 17
                )
                hp_thresh = (
                    0.88
                    if post_ferry_heal
                    else (
                        0.42
                        if (post_kill_t1_hunt or (en_home_vis and not en_g_vis))
                        else (0.75 if (en_g_vis and g.hex in my_pw_heal and len(galleys) == 1 and not at_port_under_attack) else 0.58)
                    )
                )
                topup_ratio = (
                    0.92
                    if post_ferry_heal
                    else (
                        0.70
                        if (self.ever_killed_enemy_galley and not en_g_vis and s.round <= 15)
                        else (0.85 if (self.ever_killed_enemy_galley and (self.enemy_transports_seen or mid_foes_known)) else 0.95)
                    )
                )
                at_port_topup = (
                    g.hex in my_pw_heal
                    and not at_port_under_attack
                    and g.hp < self.types[g.type].max_hp * topup_ratio
                    and (not en_g_vis or (len(galleys) == 1 and g.hp < 75))
                    and not en_t_near_port
                    and not en_home_vis
                )
                need_home = (
                    (not at_port_under_attack and g.hp < self.types[g.type].max_hp * hp_thresh)
                    or at_port_topup
                    or (self.supply and g.rations <= self.trip_home_rations(g) + 6)
                )
                if need_home:
                    need_home_galleys.append(g)
                else:
                    healthy.append(g)

            need_home_ids = {g.id for g in need_home_galleys}
            while healthy and not self.out_of_time():
                healthy = [g for g in healthy if g.id in s.units and not g.acted]
                if not healthy:
                    break
                galley_d, shore_d = self.naval_danger()
                opts_by_enemy = {}
                all_singles = []
                en_g_list = [
                    f for f in self.enemies.values()
                    if not f.carrier and self.types[f.type].naval and not self.types[f.type].transport
                ]
                en_g_inc_list = list(en_g_list) + [
                    fg for gid, (fg, rnd) in self.ghosts.items()
                    if gid not in self.enemies and s.round - rnd <= 2
                    and self.types[fg.type].naval and not self.types[fg.type].transport
                ]
                en_g_fields = {f.id: self.sea_field([f.hex]) for f in en_g_inc_list}
                en_shore_list = [
                    f for f in self.enemies.values()
                    if not f.carrier and not self.types[f.type].naval
                    and self.rules.damage.get(f.type, {}).get(self.galley_code, 0) > 0
                ]
                my_g_count = sum(1 for u in s.my_units() if u.type == self.galley_code)
                en_p_h = s.cities[self.enemy_port_cid].hex if self.enemy_port_cid is not None else None
                en_pw_all = self.port_water[self.enemy_port_cid] if self.enemy_port_cid is not None else []
                my_pw_list = self.port_water[self.my_port_cid] if self.my_port_cid is not None else []
                my_pw_set = set(my_pw_list)
                en_p_field = self.sea_field(en_pw_all) if en_pw_all else {}
                d_my_port = min((en_p_field.get(h, 999) for h in my_pw_list), default=999)
                raid_ambush_guard = False

                my_p_h = s.cities[self.my_port_cid].hex if self.my_port_cid is not None else None
                en_home_caps_cnt = sum(
                    1 for f in self.enemies.values()
                    if not f.carrier and self.types[f.type].capture and self.land.get(f.hex) == self.home_land
                )
                en_home_caps = en_home_caps_cnt > 0
                my_home_caps_rg = sum(
                    1 for u in s.my_units()
                    if not u.carrier and self.types[u.type].capture and self.land.get(u.hex) == self.home_land
                )
                en_g_live_rg = sum(
                    1 for eid, r in self.enemy_galleys_seen.items()
                    if s.round - r <= 14 and eid not in self.killed_galley_ids
                )
                hot_en_t_mem = any(
                    s.round - tr <= 1 and (thp <= 55 or twag) and tid not in self.enemies
                    for tid, (th, thp, tr, tc, taslt, twag) in self.en_trans_mem.items()
                )
                solo_early_hunter = (
                    my_g_count <= 2
                    and s.round <= 14
                    and not self.enemy_galleys_seen
                    and not self.ever_killed_enemy_galley
                    and self.first_galley_round is not None
                    and self.first_galley_round <= 4
                )
                any_en_t_vis_rg = any(self.types[f.type].transport for f in self.enemies.values())
                wounded_finishers = [
                    g for g in need_home_galleys
                    if g.id in s.units and not g.acted and g.hp >= 20
                    and (not self.supply or g.rations > self.trip_home_rations(g) + 3)
                ]
                for g in healthy + wounded_finishers:
                    is_wounded = g.id in need_home_ids
                    reach = self.reachable(g)
                    reach[g.hex] = ""
                    gt = self.types[g.type]
                    for e in list(self.enemies.values()):
                        if e.carrier:
                            continue
                        base = self.rules.damage[g.type].get(e.type, 0)
                        if base <= 0:
                            continue
                        et = self.types[e.type]
                        if is_wounded and not et.naval:
                            continue
                        # Never waste a Galley's turn shelling a middle Scout/Infantry when a wounded or
                        # wagon-loaded enemy Transport JUST slipped 1 hex into fog and needs immediate pursuit!
                        if (
                            hot_en_t_mem
                            and not et.naval
                            and not et.carry
                            and self.land.get(e.hex) != self.home_land
                            and not self.home_invasion_active()
                        ):
                            continue
                        pass_val = sum(self.worth(p) for p in s.passengers(e))
                        spots = [h for h in reach if gt.in_range(bb.distance(h, e.hex))]
                        if raid_ambush_guard and not et.naval and my_pw_list:
                            # When Breakwater's Round-8 Galley is inbound, NEVER sail forward off our Port
                            # to shell land units — only shell from our Port or sheltered back-side hexes!
                            spots = [h for h in spots if h in my_pw_set or en_p_field.get(h, 999) >= d_my_port]
                        if my_g_count == 1 and s.round >= 7 and not et.naval and en_p_field and self.land.get(e.hex) != self.home_land:
                            # NEVER park our solo Galley inside enemy Port's 6-hex fresh-Galley launch zone
                            # on r7+ just to shell a stranded wagon or land unit!
                            spots = [h for h in spots if en_p_field.get(h, 99) >= 7]
                        if not spots:
                            continue
                        pax_e = s.passengers(e) if et.transport else ()
                        is_assault_t = (
                            et.transport
                            and bool(pax_e)
                            and not any(self.types[p.type].carry for p in pax_e)
                            and (len(pax_e) >= 3 or (en_p_field.get(e.hex, 99) <= 1 and any(self.types[p.type].capture for p in pax_e)))
                        )
                        t_heading_home = (
                            et.transport
                            and my_p_h is not None
                            and en_p_h is not None
                            and (is_assault_t or bb.distance(e.hex, my_p_h) < bb.distance(e.hex, en_p_h))
                        )
                        best_h = max(
                            spots,
                            key=lambda h: (
                                -(1 if h in shore_d else 0),
                                -(1 if h in galley_d else 0),
                                (-bb.distance(h, my_p_h) if (et.transport and t_heading_home and pass_val > 0) else (
                                    -en_p_field.get(h, 99) if (et.transport and en_p_field and not t_heading_home) else 0
                                )),
                                (1 if (not et.naval and h in my_pw_set) else 0),
                                (bb.distance(h, en_p_h) if (not et.naval and en_p_h is not None) else 0),
                                -len(reach[h]),
                            ),
                        )
                        hit = self.damage(g.type, g.hp, e.type, e.hex, e.fort)
                        left = max(0, e.hp - hit)
                        ret_dmg = 0
                        ret_loss = 0.0
                        if left > 0 and et.in_range(bb.distance(best_h, e.hex)) and self.rules.damage[e.type].get(g.type, 0) > 0:
                            ret_dmg = self.damage(e.type, left, g.type, best_h)
                            ret_loss = gt.cost * min(ret_dmg, g.hp) / gt.max_hp
                            if ret_dmg >= g.hp:
                                ret_loss += gt.cost * 0.5
                        # Never sacrifice a wounded (<58 HP) Galley inside enemy Galley / enemy Port launch range
                        # alone — EXCEPT allow wounded Galleys into multi-ship `opts_by_enemy` when healthy
                        # wingmen are present (best_kill enforces wounded ONLY deals the 0-retaliation killing blow!).
                        if is_wounded and (
                            (not et.naval)
                            or (left > 0 and len(healthy) == 0)
                            or (left == 0 and (best_h in galley_d or en_p_field.get(best_h, 99) <= 6) and len(healthy) == 0)
                        ):
                            continue
                        # Never dive solo G1 inside enemy Port's 6-hex Galley launch zone to finish an EMPTY Transport
                        # once an enemy Galley is live (or late enough that a fresh G can spawn next turn)!
                        if (
                            my_g_count == 1
                            and et.transport
                            and pass_val == 0
                            and en_p_field.get(best_h, 99) <= 6
                            and (en_g_live_rg >= 1 or (s.round >= 10 and en_p_field.get(g.hex, 99) > 3))
                        ):
                            continue
                        # Solo early G1 Transport-Hunter discipline: NEVER waste G1's turn shelling land units
                        # (even on our home island!) when our home Infantry can handle the landing — keep G1 sailing
                        # to sink T1/T2 & cork enemy Port! (Still allow killing loaded wagons or desperate 0-defender saves.)
                        if (
                            solo_early_hunter
                            and not et.naval
                            and not (et.carry and e.wheat >= 200)
                            and not (
                                self.land.get(e.hex) == self.home_land
                                and et.capture
                                and en_home_caps_cnt > my_home_caps_rg
                                and bb.distance(g.hex, e.hex) <= 3
                                and not any_en_t_vis_rg
                                and not hot_en_t_mem
                            )
                        ):
                            continue
                        # Always allow 1-hit kills on enemy Transports (sinking a 20-HP wheat ferry T2
                        # saves 950 wheat and ALWAYS beats a 25-dmg coastal land scratch!).
                        opts_by_enemy.setdefault(e.id, []).append((g, best_h, reach[best_h], hit, ret_loss, is_wounded))
                        if is_wounded:
                            continue

                        gain = et.cost * (e.hp - left) / et.max_hp
                        inv_active = self.home_invasion_active()
                        if left == 0:
                            gain += et.cost * 0.75 + pass_val * 1.5
                            if et.transport and pass_val > 0:
                                gain += 260
                            elif et.transport:
                                gain += 160
                            elif et.naval and not et.transport:
                                gain += 220
                        elif et.transport:
                            if pass_val > 0:
                                # When enemy Galleys are actively fighting right here, don't waste a non-lethal
                                # scratch on a Transport instead of killing/crippling their Galleys!
                                if en_g_list:
                                    gain += 0.30 * pass_val + 90
                                else:
                                    gain += 0.55 * pass_val + 220  # Loaded assault transport = top target!
                            elif not inv_active:
                                mid_t_bonus = 0
                                if s.round >= 6 and any(
                                    bb.distance(e.hex, x.hex) <= 4
                                    for sites_m in self.resource_isles().values() for x in sites_m
                                ):
                                    mid_t_bonus = 140
                                gain += ((80 if en_g_list else 155) if s.round <= 16 else 85) + mid_t_bonus
                        elif et.naval and not et.transport:
                            # NEVER 1v1 attack an enemy Galley sitting in/right outside its own Port water
                            # (where it heals +20 HP/turn and enemy spawns G2 next turn to double-team!) OR
                            # 1v2 suicide into outnumbered enemy Galleys for a non-kill when we can escape!
                            in_en_port_water = (
                                e.hex in en_pw_all
                                or en_p_field.get(e.hex, 99) <= 2
                                or (s.round >= 13 and en_p_field.get(best_h, 99) <= 6 and not self.ever_sank_enemy_ferry)
                            )
                            local_my_g = sum(
                                1 for u in s.my_units()
                                if u.type == self.galley_code and u.hp >= 58 and bb.distance(u.hex, e.hex) <= 8
                            )
                            local_en_g = sum(
                                1 for f_g in en_g_list
                                if bb.distance(f_g.hex, best_h) <= 6
                            )
                            can_escape_g = any(h not in galley_d and h not in shore_d for h in reach)
                            if (
                                left > 0
                                and (can_escape_g or g.mp >= 4)
                                and (
                                    ((my_g_count == 1 or local_my_g <= 1) and in_en_port_water)
                                    or (local_en_g > local_my_g and can_escape_g)
                                )
                            ):
                                gain -= 320
                            elif g.hp >= 60:
                                gain += 235
                            else:
                                gain += 40
                        if not et.naval:
                            # While staying at sea wounded (< 58 HP) strictly for post_kill_t1_hunt, NEVER waste
                            # turns poking Scouts or non-kill land units when we should be chasing/sinking T1!
                            if g.hp < 58 and not self.home_invasion_active() and (left > 0 or et.skirmish):
                                continue
                            # Early/mid-game solo-Galley Transport-Hunter discipline: NEVER stall for 4 turns
                            # bombarding a non-kill middle-island Scout/Infantry (21 dmg/turn) while an enemy
                            # Transport is live or enemy Port is uncorked — sail past to sink their 750-wheat ferry!
                            if (
                                left > 0
                                and not inv_active
                                and self.land.get(e.hex) != self.home_land
                                and not et.carry
                                and et.range_min <= 1
                                and (
                                    any(self.types[f_t.type].transport for f_t in self.enemies.values())
                                    or any(s.round - tr <= 2 for _, (_, _, tr, _, _, _) in self.en_trans_mem.items())
                                    or (s.round <= 16 and not self.enemy_galleys_seen and my_g_count == 1)
                                )
                            ):
                                continue
                            if self.land.get(e.hex) == self.home_land:
                                # Prioritize enemy Infantry/capturers over Scouts, but NEVER outrank an enemy Galley!
                                gain += 75 if et.capture else (12 if et.skirmish else 50)
                            if et.carry:
                                gain += 75
                            elif e.hex in self.city_at:
                                c_at = s.cities[self.city_at[e.hex]]
                                e_land_id = self.land.get(e.hex)
                                friends_on_isle = any(
                                    not u.carrier and not self.types[u.type].naval
                                    and not self.types[u.type].carry and self.land.get(u.hex) == e_land_id
                                    for u in s.my_units()
                                )
                                if c_at.owner not in (0, self.player) and left > 0 and hit <= 22 and not friends_on_isle:
                                    # A single Galley deals ~17 dmg to city-garrisoned Infantry vs +20 HP/turn
                                    # city heal (zero net progress!) — don't stall for 8 turns shelling it!
                                    gain -= 120
                                else:
                                    gain += 30
                        risk_pen = 0.0
                        shore_inc = 0
                        if best_h in shore_d and not (left == 0 and et.range_min > 1):
                            shore_inc = sum(
                                self.damage(f.type, f.hp, g.type, best_h)
                                for f in en_shore_list
                                if bb.distance(f.hex, best_h) <= self.types[f.type].move + self.types[f.type].range_max
                            )
                            if shore_inc >= g.hp or (shore_inc >= 40 and not (et.naval and (left == 0 or pass_val > 0))):
                                risk_pen += 280.0
                            else:
                                risk_pen += 65.0
                        # Exact incoming counter-fire from ALL OTHER enemy galleys (+ target e if it survives!):
                        # NEVER suicide our Galley into 2 enemy Galleys (110+ counter dmg!) to scratch a full-HP
                        # Galley or poke an empty Transport!
                        inc_other_g = 0
                        for f in en_g_inc_list:
                            if f.id == e.id:
                                continue
                            ft = self.types[f.type]
                            if en_g_fields[f.id].get(best_h, 99) <= ft.move + ft.range_max:
                                inc_other_g += self.damage(f.type, f.hp, g.type, best_h)
                        inc_target_g = (
                            self.damage(e.type, left, g.type, best_h)
                            if (left > 0 and et.naval and not et.transport) else 0
                        )
                        g_hp_after = g.hp - ret_dmg
                        lethal_trade_ok = (left == 0) and (
                            (et.naval and not et.transport) or (et.transport and pass_val > 0)
                        )
                        if inc_other_g > 0 and not lethal_trade_ok:
                            if g_hp_after <= 0 or (inc_other_g + inc_target_g + shore_inc >= g_hp_after):
                                risk_pen += 520.0
                            elif ((left > 0 and et.naval and not et.transport) or (et.transport and pass_val == 0)) and inc_other_g >= 35:
                                # NEVER poke a Galley for a non-kill OR attack an EMPTY Transport (even to kill it!)
                                # while another live enemy Galley is already in strike range to First-Strike us!
                                risk_pen += 340.0
                            elif not et.naval:
                                risk_pen += 260.0
                            else:
                                risk_pen += 95.0 if g.hp < 80 else 45.0
                        score = gain - 0.85 * ret_loss - risk_pen
                        all_singles.append((score, g, e, best_h, reach[best_h]))

                best_kill = None
                for eid, cand in opts_by_enemy.items():
                    e = self.enemies.get(eid)
                    if not e or sum(x[3] for x in cand) < e.hp:
                        continue
                    et = self.types[e.type]
                    pass_val = sum(self.worth(p) for p in s.passengers(e))
                    # Sort healthy galleys BEFORE wounded finishers so wounded galleys only deal the
                    # zero-retaliation killing blow (left == 0)!
                    cand.sort(key=lambda x: (1 if x[5] else 0, -x[3], x[4]))
                    plan, rem, total_cost = [], e.hp, 0.0
                    worth_lethal = (et.naval and not et.transport) or (et.transport and pass_val > 0)
                    wounded_ok = True
                    for g, h, dirs, hit, _, is_w in cand:
                        if rem <= 0:
                            break
                        left = max(0, rem - hit)
                        if is_w and left > 0:
                            # Wounded retreating galley may ONLY execute a zero-retaliation finishing blow!
                            wounded_ok = False
                            break
                        ret_d = 0
                        ret = 0.0
                        if left > 0 and et.in_range(bb.distance(h, e.hex)) and self.rules.damage[e.type].get(g.type, 0) > 0:
                            ret_d = self.damage(e.type, left, g.type, h)
                            ret = self.types[g.type].cost * min(ret_d, g.hp) / self.types[g.type].max_hp
                        shore_pen = 45.0 if (h in shore_d and not et.range_min > 1) else 0.0
                        lethal_g_pen = 0.0
                        if not worth_lethal:
                            inc_og = sum(
                                self.damage(f.type, f.hp, g.type, h)
                                for f in en_g_inc_list
                                if f.id != e.id and en_g_fields[f.id].get(h, 99) <= self.types[f.type].move + self.types[f.type].range_max
                            )
                            inc_sh = sum(
                                self.damage(f.type, f.hp, g.type, h)
                                for f in en_shore_list
                                if bb.distance(f.hex, h) <= self.types[f.type].move + self.types[f.type].range_max
                            ) if h in shore_d else 0
                            if inc_og > 0 and inc_og + inc_sh >= g.hp - ret_d:
                                lethal_g_pen = self.types[g.type].cost * (1.85 if (et.naval and not et.transport) else 1.65)
                            elif inc_og >= 35:
                                # NEVER sacrifice Galley First Strike (taking 35-55 counter-hit from a live enemy Galley!)
                                # just to snipe a land unit or empty transport (empty-T val=320 < 450)!
                                lethal_g_pen = self.types[g.type].cost * 2.25
                        total_cost += 0.85 * ret + shore_pen + lethal_g_pen
                        rem = left
                        plan.append((g, h, dirs))
                    if rem == 0 and wounded_ok:
                        val = et.cost * (e.hp / et.max_hp + 0.80) + pass_val * 1.6
                        if et.transport:
                            val += 120
                        elif et.naval:
                            val += 60
                        net = val - total_cost
                        if net > 0 and (best_kill is None or net > best_kill[0]):
                            best_kill = (net, e, plan)

                if best_kill is not None:
                    _, e, plan = best_kill
                    et_bk = self.types[e.type]
                    for g, h, dirs in plan:
                        if e.id not in s.units or g.id not in s.units or self.out_of_time():
                            break
                        if dirs:
                            prev_nav = {u.id for u in self.enemies.values() if self.types[u.type].naval}
                            aborted_bk = False
                            if not (et_bk.naval and not et_bk.transport):
                                # Step 1 hex at a time toward non-Galley targets so a newly spotted enemy Galley
                                # in fog lets us abort BEFORE burning our last MP inside its 6-hex strike zone!
                                for d_ch in dirs:
                                    if g.id not in s.units or g.mp <= 0:
                                        break
                                    self.go(g, d_ch)
                                    if g.id not in s.units:
                                        aborted_bk = True
                                        break
                                    if not et_bk.naval and g.mp > 0 and any(
                                        u.id not in prev_nav and self.types[u.type].naval
                                        and bb.distance(g.hex, u.hex) <= g.mp + self.types[g.type].range_max
                                        for u in self.enemies.values()
                                    ):
                                        aborted_bk = True
                                        break
                                    if not s.passengers(e) and g.mp > 0 and any(
                                        u.id not in prev_nav and self.types[u.type].naval and not self.types[u.type].transport
                                        and bb.distance(g.hex, u.hex) <= 6
                                        for u in self.enemies.values()
                                    ):
                                        aborted_bk = True
                                        break
                                if aborted_bk or g.id not in s.units or g.hex != h:
                                    break
                            else:
                                self.go(g, dirs)
                                if g.id not in s.units or g.hex != h:
                                    break
                        self.strike_target(g, e)
                    continue

                if not all_singles:
                    break
                best_s = max(all_singles, key=lambda x: x[0])
                if best_s[0] <= 5.0:
                    break
                _, g, e, h, dirs = best_s
                et_bs = self.types[e.type]
                if dirs:
                    prev_nav = {u.id for u in self.enemies.values() if self.types[u.type].naval}
                    aborted_bs = False
                    if not (et_bs.naval and not et_bs.transport):
                        for d_ch in dirs:
                            if g.id not in s.units or g.mp <= 0:
                                break
                            self.go(g, d_ch)
                            if g.id not in s.units:
                                aborted_bs = True
                                break
                            if not et_bs.naval and g.mp > 0 and any(
                                u.id not in prev_nav and self.types[u.type].naval
                                and bb.distance(g.hex, u.hex) <= g.mp + self.types[g.type].range_max
                                for u in self.enemies.values()
                            ):
                                aborted_bs = True
                                break
                            if not s.passengers(e) and g.mp > 0 and any(
                                u.id not in prev_nav and self.types[u.type].naval and not self.types[u.type].transport
                                and bb.distance(g.hex, u.hex) <= 6
                                for u in self.enemies.values()
                            ):
                                aborted_bs = True
                                break
                        if aborted_bs or g.id not in s.units or g.hex != h:
                            continue
                    else:
                        self.go(g, dirs)
                        if g.id not in s.units or g.hex != h:
                            continue
                self.strike_target(g, e)

            # Now send any remaining un-acted wounded/low-ration galleys home (after giving them a chance
            # to execute a 0-retaliation finishing kill in best_kill above!).
            for g in need_home_galleys:
                if g.id not in s.units or g.acted or self.out_of_time():
                    continue
                retreating.add(g.id)
                if not self._galley_opportunistic_kill(g):
                    self._galley_go_home(g)

            if _pass >= 2:
                break

            galley_d, shore_d = self.naval_danger()
            spotted_any = False
            for g in sorted((u for u in s.my_units() if u.type == self.galley_code and not u.fresh and not u.acted and u.mp > 0 and u.id not in retreating),
                            key=lambda u: u.id):
                if self.out_of_time():
                    break
                if self._galley_patrol(g, galley_d, shore_d):
                    spotted_any = True
            if not spotted_any:
                break

    def _galley_opportunistic_kill(self, g):
        """Even wounded galleys will move & finish a target with a guaranteed 0-retaliation kill!"""
        gt = self.types[g.type]
        reach = self.reachable(g)
        reach[g.hex] = ""
        best = None
        for e in list(self.enemies.values()):
            if e.carrier or self.rules.damage[g.type].get(e.type, 0) <= 0:
                continue
            if self.damage(g.type, g.hp, e.type, e.hex, e.fort) < e.hp:
                continue
            spots = [h for h in reach if gt.in_range(bb.distance(h, e.hex))]
            if not spots:
                continue
            h = min(spots, key=lambda x: len(reach[x]))
            pri = (2 if self.types[e.type].naval else 1, self.worth(e))
            if best is None or pri > best[0]:
                best = (pri, e, h, reach[h])
        if best is not None:
            _, e, h, dirs = best
            if dirs:
                self.go(g, dirs)
            if g.id in self.state.units and g.hex == h:
                self.strike_target(g, e)
                return True
        return False

    def _step_sail_galley(self, ship, field, max_steps=None, avoid=(), hard_avoid=()):
        """Move galley 1 hex at a time toward field so newly spotted enemy ships trigger immediate first-strike combat!"""
        if ship.fresh or ship.acted or ship.mp <= 0:
            return False
        limit = ship.mp if max_steps is None else min(ship.mp, max_steps)
        if limit <= 0:
            return False
        seen_before = {e.id for e in self.enemies.values() if self.types[e.type].naval}
        for _ in range(limit):
            if ship.id not in self.state.units or ship.mp <= 0 or self.out_of_time():
                break
            cur_d = field.get(ship.hex, 1 << 30)
            best = None
            for d, n, cost in self.sea_nbrs.get(ship.hex, ()):
                if cost > ship.mp or n in self.enemy_hex or self.occ.get(n, 0) >= self.limit:
                    continue
                nd = field.get(n)
                if nd is None:
                    continue
                pen = (14 if n in hard_avoid else 0) + (5 if n in avoid else 0)
                cur_pen = (14 if ship.hex in hard_avoid else 0) + (5 if ship.hex in avoid else 0)
                if nd + pen >= cur_d + cur_pen:
                    continue
                key = (nd + pen, n)
                if best is None or key < best[0]:
                    best = (key, str(d))
            if best is None:
                break
            self.go(ship, best[1])
            s_cur = self.state
            for e in self.enemies.values():
                if self.types[e.type].transport:
                    self.enemy_transports_seen[e.id] = s_cur.round
                    self.enemy_transports_first_seen.setdefault(e.id, s_cur.round)
                    pax_sp = s_cur.passengers(e)
                    has_w_sp = any(self.types[p.type].carry for p in pax_sp)
                    is_a_sp = (
                        len(pax_sp) >= 3
                        and any(self.types[p.type].capture for p in pax_sp)
                        and not has_w_sp
                    )
                    pm_sp = self.en_trans_mem.get(e.id)
                    if pm_sp and len(pm_sp) >= 5 and pm_sp[4] and len(pax_sp) > 0:
                        is_a_sp = True
                    self.en_trans_mem[e.id] = (e.hex, e.hp, s_cur.round, len(pax_sp) > 0, is_a_sp, has_w_sp)
            new_ships = {e.id for e in self.enemies.values() if self.types[e.type].naval} - seen_before
            if new_ships:
                return True
        return False

    def _galley_go_home(self, g):
        sea = self.sea.get(g.hex)
        ports = self.my_ports(sea)
        if not ports:
            return
        fed = [c for c in ports if c.bread >= 100] or ports
        goals = [h for c in fed for h in self.port_water[c.id] if self.sea.get(h) == sea]
        if goals and g.hex not in goals:
            galley_d, shore_d = self.naval_danger()
            self.safe_sail(g, self.sea_field(goals), avoid=shore_d, hard_avoid=galley_d)

    def _galley_patrol(self, g, galley_d, shore_d):
        s = self.state
        sea = self.sea.get(g.hex)
        my_galleys = sum(1 for u in s.my_units() if u.type == self.galley_code and self.sea.get(u.hex) == sea)
        en_galleys = [
            e for e in self.enemies.values()
            if self.types[e.type].naval and not self.types[e.type].transport and self.sea.get(e.hex) == sea
        ]
        en_g_ghosts = [
            eg for gid, (eg, rnd) in self.ghosts.items()
            if gid not in self.enemies and s.round - rnd <= 3
            and self.types[eg.type].naval and not self.types[eg.type].transport
            and self.sea.get(eg.hex) == sea
        ]
        en_g_recent_live = sum(
            1 for eid, rnd in self.enemy_galleys_seen.items()
            if s.round - rnd <= 8 and eid not in self.killed_galley_ids
        )
        en_g_threat_cnt = max(len(en_galleys) + len(en_g_ghosts), en_g_recent_live)
        local_near_g = sum(
            1 for u in s.my_units()
            if u.type == self.galley_code and u.hp >= 58 and self.sea.get(u.hex) == sea
            and bb.distance(u.hex, g.hex) <= 5
        )
        # Respect galley first-strike danger (including RECENT GHOST galleys!) unless we LOCALLY outnumber them!
        respect_g = galley_d if (my_galleys <= en_g_threat_cnt or local_near_g <= len(en_galleys)) else ()

        # 0. Outnumbered / In-Danger Galley Survival: if we are currently inside enemy galley strike range
        # without outnumbering them (and did NOT take a favorable attack in run_galleys), IMMEDIATELY
        # step out of galley_d toward our Port instead of chasing transports or feeding a free 2v1 kill!
        if my_galleys <= en_g_threat_cnt and g.hex in galley_d and (en_galleys or en_g_ghosts):
            my_pw_s = [h for h in self.port_water[self.my_port_cid] if self.sea.get(h) == sea] if self.my_port_cid is not None else []
            # If we are already docked at our own Port (healing +20/turn) with even numbers (not outnumbered!), hold!
            if not (my_galleys >= len(en_galleys) and len(en_galleys) <= 1 and g.hex in my_pw_s):
                reach = self.reachable(g)
                safe_out = [h for h in reach if h not in galley_d and h not in shore_d]
                my_f_s = self.sea_field(my_pw_s) if my_pw_s else {}
                en_pw_0 = [h for h in self.port_water[self.enemy_port_cid] if self.sea.get(h) == sea] if self.enemy_port_cid is not None else []
                en_f_0 = self.sea_field(en_pw_0) if en_pw_0 else {}
                if safe_out:
                    best_r = min(
                        safe_out,
                        key=lambda h: (
                            0 if en_f_0.get(h, 99) >= 7 else 1,
                            -en_f_0.get(h, 99) if en_f_0.get(h, 99) < 7 else 0,
                            my_f_s.get(h, 99),
                            len(reach[h]),
                        ),
                    )
                    self.go(g, reach[best_r])
                    return False
                elif en_f_0:
                    far_opts = [h for h in reach if h not in shore_d and self.sea.get(h) == sea]
                    if far_opts:
                        best_far = max(far_opts, key=lambda h: (en_f_0.get(h, 0), -my_f_s.get(h, 99), -len(reach[h])))
                        if best_far != g.hex:
                            self.go(g, reach[best_far])
                        return False
                if my_pw_s and g.hex not in my_pw_s:
                    self.safe_sail(g, self.sea_field(my_pw_s), avoid=shore_d)
                    return False

        # Track Transport-first (Armada) vs Galley-first (Breakwater v4/v5) opening signature upfront
        # so Section 1 (en_trans chase) NEVER pulls our solo Galley off its 2b-creep / 2b-post ambush
        # to chase an unreachable Breakwater Transport into Breakwater's charging Galley!
        if (
            not self.ever_saw_mid_capturer
            and s.round <= 9
            and any(
                mem and all(not self.types[tc].capture for tc, _ in mem.values())
                for mem in self.isle_enemies.values()
            )
        ):
            self.saw_unescorted_mid_foe = True
        unescorted_mid_foe = self.saw_unescorted_mid_foe
        early_en_transport = any(r <= 7 for r in self.enemy_transports_first_seen.values())
        galley_first_foe = (not early_en_transport) and (not unescorted_mid_foe)
        opening_ambush_armed = False
        post_g1_ambush_armed = False
        for vh in self.my_vision():
            if vh in self.sea_hexes:
                self.mid_patrol_seen[vh] = s.round

        # 1. Hunt visible enemy TRANSPORTS — EXCEPT:
        #   - NEVER ignore a visible enemy GALLEY (en_galleys) to chase an unreachable/empty Transport
        #     around the wrong side of middle (Section 2 MUST intercept en_galleys first unless we strictly
        #     outnumber enemy galleys AND the transport is loaded!).
        #   - during an active home invasion, ignore EMPTY fleeing transports so G stays off home coast!
        #   - while opening_ambush_armed (Breakwater v5 P2 guard-mode G1 is still unseen & charging!),
        #     NEVER abandon dMy == 2 to chase an unreachable empty/far T1 across middle!
        #   - while post_g1_ambush_armed (solo G1 waiting for Breakwater's G2+G3 wave!), only chase T1
        #     if it is safely outside enemy Port's 8-hex G2-launch zone!
        inv_active = self.home_invasion_active()
        en_pw_sec1 = [h for h in self.port_water[self.enemy_port_cid] if self.sea.get(h) == sea] if self.enemy_port_cid is not None else []
        en_f_sec1 = self.sea_field(en_pw_sec1) if en_pw_sec1 else {}
        my_pw_s1 = [h for h in self.port_water[self.my_port_cid] if self.sea.get(h) == sea] if self.my_port_cid is not None else []
        my_f_s1 = self.sea_field(my_pw_s1) if my_pw_s1 else {}
        d_cur_s1 = en_f_sec1.get(g.hex, 99) if en_f_sec1 else 99
        d_my_s1 = my_f_s1.get(g.hex, 99) if my_f_s1 else 99
        my_home_caps_p = sum(
            1 for u in s.my_units()
            if not u.carrier and self.land.get(u.hex) == self.home_land and self.types[u.type].capture
        )
        solo_press_en = (
            my_galleys <= 2
            and s.round <= 14
            and not self.enemy_galleys_seen
            and not self.ever_killed_enemy_galley
            and self.first_galley_round is not None
            and self.first_galley_round <= 4
            and my_home_caps_p >= 2
        )
        en_home_caps_s1 = any(
            not e.carrier and self.types[e.type].capture and self.land.get(e.hex) == self.home_land
            for e in self.enemies.values()
        )
        en_g_away_at_my_port = (
            my_galleys == 1
            and len(en_galleys) == 1
            and en_g_threat_cnt <= 1
            and en_f_sec1.get(en_galleys[0].hex, 99) >= 8
            and my_f_s1.get(en_galleys[0].hex, 99) <= 6
            and d_cur_s1 < en_f_sec1.get(en_galleys[0].hex, 99)
        )
        en_trans = [
            e for e in self.enemies.values()
            if self.types[e.type].transport and self.sea.get(e.hex) == sea
            and (not inv_active or len(s.passengers(e)) > 0 or (not en_home_caps_s1 and bb.distance(g.hex, e.hex) <= 5))
            and (not post_g1_ambush_armed or en_f_sec1.get(e.hex, 99) >= 10)
            and (not (my_galleys == 1 and s.round >= 10 and d_cur_s1 > 3 and len(s.passengers(e)) == 0 and en_f_sec1.get(e.hex, 99) <= 6))
            and (not (my_galleys == 1 and d_cur_s1 <= 2 and len(s.passengers(e)) == 0 and bb.distance(g.hex, e.hex) > 4))
            and (not en_galleys or en_g_away_at_my_port or (my_galleys > len(en_galleys) and len(s.passengers(e)) > 0))
            and not (
                solo_press_en
                and d_cur_s1 <= d_my_s1
                and my_f_s1.get(e.hex, 99) < en_f_sec1.get(e.hex, 99)
                and bb.distance(g.hex, e.hex) > 6
            )
        ]
        if en_trans and not opening_ambush_armed:
            target = min(en_trans, key=lambda e: (len(s.passengers(e)) == 0, bb.distance(g.hex, e.hex), e.id))
            goals = [n for n in bb.neighbors(target.hex) if self.sea.get(n) == sea]
            if goals:
                return self._step_sail_galley(g, self.sea_field(goals), avoid=shore_d, hard_avoid=respect_g)

        # 1a. Hunt recently-seen WOUNDED (<= 55 HP), WAGON-CARRYING, or ASSAULT enemy Transport that slipped into fog!
        # - If it is an ASSAULT transport (3+ combat troops, no wagon), ONLY chase toward our home coast if we
        #   lack home defenders OR can genuinely intercept a wounded transport right next to us (NEVER recall solo
        #   G1 back across the map from enemy Port while enemy T2 harvests 1,000 middle wheat!).
        # - If it is a wounded/returning wheat ferry (hp <= 55, carrying wagon, or r >= 9), intercept along the
        #   shortest corridor to ENEMY PORT on the same side of middle as `th` (never detouring around the far coast!).
        en_home_vis_1a = any(
            not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
            for e in self.enemies.values()
        )
        if (
            not en_galleys
            and not opening_ambush_armed
            and not post_g1_ambush_armed
            and not (my_galleys < en_g_threat_cnt or (my_galleys == 1 and en_g_recent_live >= 1))
            and self.en_trans_mem
        ):
            recent_t = [
                (tid, th, thp, tr, tc, taslt, twag)
                for tid, (th, thp, tr, tc, taslt, twag) in self.en_trans_mem.items()
                if s.round - tr <= 2 and self.sea.get(th) == sea
                and tid not in self.enemies
                and ((taslt and tc) or thp <= 55 or (not inv_active and (twag or (tc and s.round >= 8))))
                and (not en_home_vis_1a or thp <= 55)
                and not (
                    taslt
                    and solo_press_en
                    and (thp > 55 or bb.distance(g.hex, th) > 6)
                )
            ]
            if recent_t:
                recent_t.sort(key=lambda x: (0 if x[2] <= 55 else 1, 0 if (x[5] and x[4]) else 1, 0 if x[6] else 1, -x[3], bb.distance(g.hex, x[1])))
                _, th, thp, tr, _, taslt, twag = recent_t[0]
                t_f = self.sea_field([th])
                step_lim = 6 * max(1, s.round - tr)
                if taslt:
                    home_coast_1a = [
                        h for h in self.coast
                        if self.sea.get(h) == sea and h not in shore_d
                        and any(self.land.get(n) == self.home_land for n in bb.neighbors(h))
                    ]
                    if home_coast_1a:
                        hc_f = self.sea_field(home_coast_1a)
                        d_th = hc_f.get(th, 99)
                        if not (solo_press_en and d_th <= 2 and thp > 55):
                            corridor = [
                                h for h, dt in t_f.items()
                                if dt <= step_lim and self.sea.get(h) == sea and h not in shore_d
                                and (hc_f.get(h, 99) < d_th or (thp <= 55 and hc_f.get(h, 99) <= d_th))
                            ]
                            if corridor:
                                best_cut = sorted(corridor, key=lambda h: (hc_f.get(h, 99), t_f.get(h, 99), h))[:3]
                                if g.hex not in best_cut:
                                    return self._step_sail_galley(g, self.sea_field(best_cut), avoid=shore_d, hard_avoid=respect_g)
                                return False
                else:
                    en_home_coast_1a = [
                        h for h in self.coast
                        if self.sea.get(h) == sea and h not in shore_d
                        and any(self.land.get(n) == self.enemy_land for n in bb.neighbors(h))
                    ] or en_pw_sec1
                    if en_home_coast_1a:
                        ehc_f = self.sea_field(en_home_coast_1a)
                        en_f_t = self.sea_field(en_pw_sec1) if en_pw_sec1 else {}
                        d_th = ehc_f.get(th, 99)
                        avoid_blind = (my_galleys == 1 and s.round >= 10)
                        if d_th <= 3 and thp <= 55 and s.round <= 11:
                            # We JUST hit a fresh enemy ferry T2 to <=55 HP heading out from enemy Port —
                            # enemy just spent 150 bread on T2 (no Galley ready when 0 seen!), so chase it
                            # toward the middle resource island at full speed to sink it before 1,000 wheat!
                            mid_l_1a = [
                                h for isle in sorted(self.resource_isles())
                                for h in self.isle_landings(isle)
                                if self.sea.get(h) == sea and h not in shore_d
                            ]
                            if mid_l_1a:
                                mid_f_1a = self.sea_field(mid_l_1a)
                                d_mid_th = mid_f_1a.get(th, 99)
                                blind_out = avoid_blind and bool(self.enemy_galleys_seen)
                                corridor = [
                                    h for h, dt in t_f.items()
                                    if dt <= step_lim and self.sea.get(h) == sea and h not in shore_d
                                    and mid_f_1a.get(h, 99) < d_mid_th
                                    and (not blind_out or en_f_t.get(h, 99) not in (4, 5, 6))
                                ]
                                if corridor:
                                    best_cut = sorted(
                                        corridor,
                                        key=lambda h: (mid_f_1a.get(h, 99), t_f.get(h, 99), h),
                                    )[:3]
                                    if g.hex not in best_cut:
                                        return self._step_sail_galley(g, self.sea_field(best_cut), avoid=shore_d, hard_avoid=respect_g)
                                    return False
                        corridor = [
                            h for h, dt in t_f.items()
                            if dt <= step_lim and self.sea.get(h) == sea and h not in shore_d
                            and ehc_f.get(h, 99) < d_th
                            and (not avoid_blind or en_f_t.get(h, 99) not in (4, 5, 6))
                        ]
                        if corridor:
                            best_cut = sorted(
                                corridor,
                                key=lambda h: (ehc_f.get(h, 99), abs(en_f_t.get(h, 99) - 2), t_f.get(h, 99), h),
                            )[:3]
                            if g.hex not in best_cut:
                                return self._step_sail_galley(g, self.sea_field(best_cut), avoid=shore_d, hard_avoid=respect_g)
                            return False

        # 1b. Home-invasion coastal gunfire support: stay right off our home coast shelling invaders!
        # NEVER recall solo G1 back from enemy Port / middle when our 2 home Infantry can handle the landing!
        raid_ambush_guard = False
        en_home = [
            e for e in self.enemies.values()
            if not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
        ]
        if en_home:
            en_home_caps_p = [e for e in en_home if self.types[e.type].capture]
            need_g_support = (
                (not solo_press_en and (len(en_home_caps_p) >= 2 or (len(en_home_caps_p) >= 1 and my_home_caps_p <= len(en_home_caps_p))))
                or (len(en_home_caps_p) > my_home_caps_p and d_my_s1 <= 6)
            )
            if need_g_support:
                coastal_shots = [
                    n for e in en_home_caps_p
                    for n in bb.neighbors(e.hex)
                    if self.sea.get(n) == sea and n not in shore_d
                ]
                if coastal_shots:
                    if g.hex not in coastal_shots:
                        self.safe_sail(g, self.sea_field(coastal_shots), avoid=shore_d, hard_avoid=respect_g)
                    return False

        if inv_active and not en_home and not solo_press_en and self.my_port_cid is not None:
            # Only patrol home coast if an enemy raid transport hasn't been sunk/landed yet!
            if (
                not self.ever_sank_enemy_transport
                and self.home_kills_during_alert == 0
                and s.round <= self.home_invasion_alert_until - 2
            ):
                home_coast = [
                    h for h in self.coast
                    if self.sea.get(h) == sea and h not in shore_d
                    and any(self.land.get(n) == self.home_land for n in bb.neighbors(h))
                ]
                if self.home_raid_seen_hex and home_coast:
                    near_raid = sorted(home_coast, key=lambda h: bb.distance(h, self.home_raid_seen_hex))[:4]
                    if g.hex not in near_raid:
                        return self._step_sail_galley(g, self.sea_field(near_raid), avoid=shore_d, hard_avoid=respect_g)
                    return False

        # 2. Intercept nearby enemy galleys (staying strictly outside their 6-hex first-strike zone if even numbers!)
        # Don't abandon an active enemy-port blockade to chase a far-away enemy galley across the whole map.
        if en_galleys:
            en_pw_all = self.port_water[self.enemy_port_cid] if self.enemy_port_cid is not None else []
            en_in_port = all(e.hex in en_pw_all for e in en_galleys)
            local_my_g_p = sum(
                1 for u in s.my_units()
                if u.type == self.galley_code and u.hp >= 58 and self.sea.get(u.hex) == sea
                and any(bb.distance(u.hex, e.hex) <= 9 for e in en_galleys)
            )
            if my_galleys < len(en_galleys) or ((my_galleys == 1 or local_my_g_p <= 1) and en_in_port):
                # Outnumbered (1v2!) or solo against an enemy Galley hiding inside its own Port (+20 HP/turn heal,
                # waiting for G2 to spawn!): fall back to our own Port out of galley_d instead of camping into 2G!
                my_pw_r = [h for h in self.port_water[self.my_port_cid] if self.sea.get(h) == sea] if self.my_port_cid is not None else []
                if my_pw_r and g.hex not in my_pw_r:
                    self.safe_sail(g, self.sea_field(my_pw_r), avoid=shore_d, hard_avoid=galley_d)
                return False
            if (my_galleys <= len(en_galleys) or local_near_g <= len(en_galleys)) and g.hex in galley_d:
                # If we spotted an enemy galley mid-turn without enough MP to strike it (or our wingman is far behind!),
                # NEVER step closer inside its 6-hex strike zone — retreat out of galley_d (or toward our Port)!
                reach = self.reachable(g)
                safe_out = [h for h in reach if h not in galley_d and h not in shore_d]
                if safe_out:
                    my_pw_r = [h for h in self.port_water[self.my_port_cid] if self.sea.get(h) == sea] if self.my_port_cid is not None else []
                    my_f_r = self.sea_field(my_pw_r) if my_pw_r else {}
                    best_r = min(safe_out, key=lambda h: (my_f_r.get(h, 99), len(reach[h])))
                    self.go(g, reach[best_r])
                elif self.my_port_cid is not None:
                    my_pw_r = [h for h in self.port_water[self.my_port_cid] if self.sea.get(h) == sea]
                    if my_pw_r and g.hex not in my_pw_r:
                        self.safe_sail(g, self.sea_field(my_pw_r), avoid=shore_d)
                return False
            at_en_port = bool(en_pw_all) and self.sea_field(en_pw_all).get(g.hex, 99) <= 2
            target = min(en_galleys, key=lambda e: (bb.distance(g.hex, e.hex), e.id))
            # If enemy's ONLY Galley bypassed us to blockade OUR Port while our solo Galley is already closer
            # to THEIR undefended Port, counter-blockade their Port & sink their T1 instead of freezing at dist 7!
            if not en_g_away_at_my_port and ((not at_en_port) or bb.distance(g.hex, target.hex) <= 8):
                goals = [n for n in bb.neighbors(target.hex) if self.sea.get(n) == sea]
                if goals:
                    self.safe_sail(g, self.sea_field(goals), avoid=shore_d, hard_avoid=respect_g)
                    return False

        # 2b. Opening Mid-Sea Spacing (Islands D > 21 only):
        # On compact Islets (D <= 21), launch G1 IMMEDIATELY on Round 4 straight into 2c (enemy-facing middle
        # sweep) via step-by-step _step_sail_galley so we catch & sink Round-3 Transport-first ferries
        # (Breakwater v6, Armada, Naval!) on Round 5-7 BEFORE they can ever deliver middle wheat!
        if (
            self.first_galley_round is not None
            and my_galleys == 1
            and not self.enemy_galleys_seen
            and not self.ever_killed_enemy_galley
            and not self.enemy_transports_seen
            and self.enemy_port_cid is not None
            and self.my_port_cid is not None
            and self.port_sea_dist > 25
        ):
            en_pw = [h for h in self.port_water[self.enemy_port_cid] if self.sea.get(h) == sea]
            my_pw = [h for h in self.port_water[self.my_port_cid] if self.sea.get(h) == sea]
            if en_pw and my_pw:
                en_field = self.sea_field(en_pw)
                d_now = en_field.get(g.hex)
                if d_now is not None and s.round <= self.first_galley_round + 6:
                    en_moves = (s.round - self.first_galley_round) if self.player == 2 else (s.round - self.first_galley_round - 1)
                    delta_now = d_now - self.types[g.type].move * max(0, en_moves)
                    if delta_now <= 6:
                        return self._step_sail_galley(g, en_field, max_steps=g.mp, avoid=shore_d, hard_avoid=respect_g)
                    elif delta_now <= 9:
                        return False
                    else:
                        steps = min(g.mp, delta_now - 9)
                        return self._step_sail_galley(g, en_field, max_steps=steps, avoid=shore_d, hard_avoid=respect_g)

        # 2b-open: Universal Home-Corridor Mod-5 First-Strike Trap (Islets D <= 25)!
        # Beats BOTH Galley-first rivals (Breakwater v11 charging North OR South!) AND Transport-first rivals
        # (Armada/Naval!): hold on the shortest-path `ef == 5*m + 2` ring right in front of our own Port where
        # North & South sea channels converge. Any charging enemy r3/r4 G1 lands 2-3 hexes away with 0 MP left
        # (guaranteed open-water First Strike & Kill!); if no G1 appears after m enemy moves (2 turns on D<=16,
        # 3 turns on D>=17), immediately launch step-by-step into 2c -> Stage A Cork in time to catch Armada T1!
        if (
            self.first_galley_round is not None
            and my_galleys == 1
            and not self.enemy_galleys_seen
            and not self.ever_killed_enemy_galley
            and not self.enemy_transports_seen
            and not self.saw_home_raid
            and not self.swept_my_crossing
            and not any(self.isle_known_enemies(i) > 0 for i in self.resource_isles())
            and self.port_sea_dist <= 25
            and self.my_port_cid is not None
            and self.enemy_port_cid is not None
            and s.round <= self.first_galley_round + 5
        ):
            en_pw = [h for h in self.port_water[self.enemy_port_cid] if self.sea.get(h) == sea]
            my_pw = [h for h in self.port_water[self.my_port_cid] if self.sea.get(h) == sea]
            if en_pw and my_pw:
                ef = self.sea_field(en_pw)
                mf = self.sea_field(my_pw)
                g_move = max(1, self.types[g.type].move)
                # Always ambush on the `ef == 12` (m=2) ring on the shortest-path middle-island corridor!
                # - 100% immune to enemy move 1 (ef<=6) AND move 2 (ef<=11)!
                # - On D=12..20, catches charging enemy G1 at ef=9..10 on r5-7 with guaranteed First Strike!
                # - Against Transport-first rivals (Armada!), our G1 ALREADY advances to ef=12 on r4-6 and
                #   releases on k_before == 2 (r6/r7), arriving at enemy Port on r7-8 with ZERO delay!
                m_moves = 2
                trap_ring = g_move * m_moves + 2
                mid_l_tr = [
                    h for isle in sorted(self.resource_isles())
                    for h in self.isle_landings(isle)
                    if self.sea.get(h) == sea
                ]
                trap_cands = [
                    h for h in self.sea_hexes
                    if self.sea.get(h) == sea and h not in shore_d and h not in respect_g
                    and ef.get(h, -1) == trap_ring
                ]
                if not trap_cands:
                    m_moves = 1
                    trap_ring = g_move * m_moves + 2
                    trap_cands = [
                        h for h in self.sea_hexes
                        if self.sea.get(h) == sea and h not in shore_d and h not in respect_g
                        and ef.get(h, -1) == trap_ring
                    ]
                k_before = (s.round - self.first_galley_round) if self.player == 2 else (s.round - self.first_galley_round - 1)
                if k_before >= m_moves:
                    # Enemy G1 would ALREADY have reached ef == 10 and didn't appear — release into step-by-step!
                    self.swept_my_crossing = True
                elif trap_cands:
                    # Stand on the shortest-path corridor (`ef + mf` minimal) biased toward the middle-island
                    # crossing so we catch enemy G1 charging around middle AND spot Armada's T1 early!
                    best_trap = min(
                        trap_cands,
                        key=lambda h: (
                            ef.get(h, 99) + mf.get(h, 99),
                            min((bb.distance(h, m) for m in mid_l_tr), default=mf.get(h, 99)),
                            mf.get(h, 99),
                            bb.distance(g.hex, h),
                            h,
                        ),
                    )
                    if g.hex != best_trap:
                        return self._step_sail_galley(
                            g, self.sea_field([best_trap]), avoid=shore_d, hard_avoid=respect_g
                        )
                    return False

        # 2b-post. Anti-G2 Home-Port Ambush (Universal Mod-5 First-Strike Trap!):
        # After killing opening G1 against a Galley-first rival (Breakwater v3/v4/v5!), enemy IMMEDIATELY
        # builds replacement G2 (+ G3!) on r11-18 and charges straight at our Port at 5 MP/turn.
        # Hold our healed 100-HP solo G1 near my_pw on a hex with en_field[h] % 5 == 2 (until our G2 joins
        # or r20): a charging G2 moving 5/turn ALWAYS lands at d == 2..3 with 0 MP left -> guaranteed First
        # Strike right beside our Port (with +20 HP/turn heal & incoming G2/Archers!) instead of dying solo!
        if post_g1_ambush_armed:
            en_pw = [h for h in self.port_water[self.enemy_port_cid] if self.sea.get(h) == sea]
            my_pw = [h for h in self.port_water[self.my_port_cid] if self.sea.get(h) == sea]
            if en_pw and my_pw:
                en_f = self.sea_field(en_pw)
                my_f = self.sea_field(my_pw)
                d_p = min((en_f.get(h, 999) for h in my_pw), default=999)
                ambush = [
                    h for h, d in my_f.items()
                    if d <= 2 and self.sea.get(h) == sea and h not in shore_d
                    and en_f.get(h, -1) % 5 == 2 and abs(en_f.get(h, 999) - d_p) <= 2
                ]
                goals = ambush or my_pw
                if g.hex not in goals:
                    self.safe_sail(g, self.sea_field(goals), avoid=shore_d, hard_avoid=respect_g)
                return False

        # 2c. Fast Round-4/5 Enemy-Facing Middle Sweep -> Stage A Enemy-Port Cork!
        # Launch G1 at full 5 MP/turn straight to the enemy-facing side of the middle island on Round 4-5
        # to catch & sink Round-3 Transport-first ferries (Breakwater v9, Armada 77f027b, Naval!) on Round 5-6,
        # then immediately transition into Stage A Cork (d == 2 off enemy Port) to cut off returning wheat ferries
        # AND spot fresh r7-10 enemy Galleys at spawn (retreating 2 -> 7 for guaranteed open-water First Strike!).
        if (
            not self.enemy_galleys_seen
            and not self.ever_killed_enemy_galley
            and not self.saw_home_raid
            and not self.swept_middle
            and not (early_en_transport and s.round >= 8)
            and s.round <= 16
        ):
            self.swept_my_crossing = True
            en_p_h = s.cities[self.enemy_port_cid].hex if self.enemy_port_cid is not None else None
            en_pw_2c = [h for h in self.port_water[self.enemy_port_cid] if self.sea.get(h) == sea] if self.enemy_port_cid is not None else []
            en_f_2c = self.sea_field(en_pw_2c) if (en_pw_2c and my_galleys == 1 and s.round >= 8) else {}
            for isle in sorted(self.resource_isles()):
                spots = [
                    h for h in self.isle_landings(isle)
                    if self.sea.get(h) == sea and h not in shore_d
                    and (not en_f_2c or en_f_2c.get(h, 99) >= 7)
                ]
                if spots:
                    if en_p_h is not None:
                        spots.sort(key=lambda h: bb.distance(h, en_p_h))
                        target_spots = spots[:max(1, min(3, len(spots)))]
                    else:
                        target_spots = spots
                    if g.hex in target_spots or min(bb.distance(g.hex, h) for h in target_spots) <= 2:
                        self.swept_middle = True
                    else:
                        return self._step_sail_galley(g, self.sea_field(target_spots), avoid=shore_d, hard_avoid=respect_g)
                elif en_f_2c:
                    self.swept_middle = True

        # 3. Enemy-port blockade vs Post-Raid Home-Port Ambush!
        # - After a home raid (Breakwater!), hold AT OUR OWN PORT (r9-15) to ambush their Round-8/9 Galley.
        # - Against middle-ferry rivals (Armada/Naval!), cork their Port DIRECTLY (avoiding shore Archers!):
        #   at d <= 2 our vision-3 sees every fresh enemy Galley the instant it spawns -> FREE FIRST STRIKE!
        #   Only avoid ENDING a turn in the blind dead-zone d in (4, 5, 6) once s.round >= 8 (jump 7 -> 2!).
        en_ports = [
            c for c in s.cities
            if (c.owner not in (0, self.player) or c.id == self.enemy_port_cid)
            and c.owner != self.player
            and sea in self.port_seas[c.id]
        ]
        if en_ports:
            off = [h for c in en_ports for h in self.port_water[c.id] if self.sea.get(h) == sea]
            field = self.sea_field(off)
            # ANTI-2-GALLEY PORT TRAP: When we are outnumbered (my_galleys < en_g_threat_cnt) OR we have only
            # 1 solo Galley while a live enemy Galley guards their Port (en_g_recent_live >= 1), NEVER march
            # solo into their Port to get double-teamed! Hold at our own Port until G2/G3 joins (never freeze 2v2!).
            # EXCEPTION: if enemy's ONLY Galley is visibly blockading OUR Port (en_g_away_at_my_port), their Port
            # is wide open — counter-blockade it and sink their T1 while our Home City Archer shreds their Galley!
            if (not en_g_away_at_my_port) and (my_galleys < en_g_threat_cnt or (my_galleys == 1 and en_g_recent_live >= 1)) and self.my_port_cid is not None:
                my_pw_def = [h for h in self.port_water[self.my_port_cid] if self.sea.get(h) == sea]
                if my_pw_def and g.hex not in my_pw_def:
                    self.safe_sail(g, self.sea_field(my_pw_def), avoid=shore_d, hard_avoid=galley_d)
                return False
            other_g_near = any(
                u.id != g.id and u.type == self.galley_code and not u.fresh and u.hp >= 65
                and self.sea.get(u.hex) == sea and bb.distance(u.hex, g.hex) <= 7
                for u in s.my_units()
            )
            act_solo_cork = (my_galleys == 1) or (my_galleys == 2 and not other_g_near and field.get(g.hex, 99) <= 6)
            if act_solo_cork and s.round >= 7:
                # After killing opening G1 when enemy built T1 / landed on middle (Anti-T1 patrol!):
                # Patrol the ENEMY-FACING side of the middle island (staying outside enemy Port blind zone
                # [4..6] — and outside d <= 7 when g.hp <= 55 so a fresh G2 can NEVER one-shot kill us!)
                if len(self.killed_galley_ids) == 1 and not self.ever_sank_enemy_ferry and self.port_sea_dist <= 21 and s.round <= 18:
                    # After sinking enemy's opening G1 (0 live Galleys left!), cork their Port at d == 2
                    # (seeing ALL port water hexes, escaping 2+5=7>6 if G2 spawns!) so their returning T1
                    # carrying 1,000 middle wheat is ALWAYS intercepted & sunk instead of slipping around middle!
                    if s.round <= 16 and not en_galleys and en_g_recent_live == 0:
                        cork2_pg = [
                            h for h, d in field.items()
                            if d == 2 and self.sea.get(h) == sea and h not in shore_d
                        ]
                        if cork2_pg:
                            my_p_h = s.cities[self.my_port_cid].hex if self.my_port_cid is not None else g.hex
                            mid_l_pg = [
                                h for isle in sorted(self.resource_isles())
                                for h in self.isle_landings(isle)
                                if self.sea.get(h) == sea
                            ]
                            best_pg = sorted(
                                cork2_pg,
                                key=lambda h: (
                                    1 if max((bb.distance(h, p) for p in off), default=0) > 3 else 0,
                                    max((bb.distance(h, p) for p in off), default=0),
                                    min((bb.distance(h, m) for m in mid_l_pg), default=bb.distance(h, my_p_h)),
                                    bb.distance(h, my_p_h),
                                    h,
                                ),
                            )[:2]
                            if g.hex not in best_pg:
                                return self._step_sail_galley(g, self.sea_field(best_pg), avoid=shore_d, hard_avoid=respect_g)
                            return False
                    mid_guard = []
                    en_p_h = s.cities[self.enemy_port_cid].hex if self.enemy_port_cid is not None else g.hex
                    danger_en_d = (0, 1, 2, 3, 4, 5, 6, 7) if g.hp < 58 else (4, 5, 6)
                    for isle in sorted(self.resource_isles()):
                        spots = [
                            h for h in self.isle_landings(isle)
                            if self.sea.get(h) == sea and h not in shore_d and field.get(h, 99) not in danger_en_d
                        ]
                        if not spots:
                            spots = [h for h in self.isle_landings(isle) if self.sea.get(h) == sea and h not in shore_d and (g.hp > 55 or field.get(h, 99) > 6)]
                        if spots:
                            spots.sort(key=lambda h: bb.distance(h, en_p_h))
                            mid_guard.extend(spots[:3])
                    if mid_guard:
                        if g.hex not in mid_guard:
                            return self._step_sail_galley(g, self.sea_field(mid_guard), avoid=shore_d, hard_avoid=respect_g)
                        return False
                d_cur = field.get(g.hex, 99)
                # TWO-STAGE SOLO GALLEY CHECKMATE (r6..22):
                #   Stage A (r6..8 while enemy T1 has NOT been sunk yet!): hold at `d == 2` off enemy Port!
                #     - Both North & South channels from middle converge at enemy Port, so T1 returning with
                #       650-1000 wheat CANNOT bypass us on the opposite side of the island!
                #     - At `d == 2`, vision-3 sees `d == 0` (enemy Port) AND `2 + 5 = 7 > 6`, so if enemy
                #       spawns a fresh G1 at Port, we escape cleanly to `d == 7` outside its 6-hex strike zone!
                #   Stage B (once T1 is sunk OR r9..22!): hold at `d == 7` (Universal Mod-5 Open-Water Picket):
                #     - Any fresh enemy G1 moving 5/turn out of Port (0 -> 5) lands at dist 2 with 0 MP and
                #       ZERO port heal -> 100% open-water First Strike & Kill!
                #     - Once our 2nd Galley G2 joins (my_galleys >= 2), BOTH advance together to cork enemy Port!
                if (not en_galleys) or en_g_away_at_my_port:
                    # Stage A Cork (d == 2 from enemy port_water, i.e. dist 3 from enemy Port city!):
                    # - Vision-3 SEES every enemy port_water hex (so any fresh G1 spawned at d == 0 with 0 MP is
                    #   immediately spotted next turn -> we retreat 2 + 5 = 7 > 6 cleanly for open-water First Strike!).
                    # - Stay active until T1 is sunk OR enemy spawns G1 (up to first_galley_round + 8, covering BOTH
                    #   r3-port maps and r4-port maps where T1 returns on r9-11!).
                    # - Position on the MIDDLE-ISLAND side of enemy Port (covering all enemy port_water hexes!)
                    #   so returning T1 sails straight through our vision + ZOC and gets sunk BEFORE unloading!
                    cork_lo = (self.first_galley_round + 1) if self.first_galley_round is not None else 6
                    cork_hi = 30 if (self.first_galley_round is not None and self.first_galley_round <= 4) else ((self.first_galley_round + 12) if self.first_galley_round is not None else 20)
                    # Enter/hold Stage A cork whenever:
                    # - we already have vision of enemy Port (d_cur <= 3) — NEVER abandon an empty enemy Port
                    #   on r14+ just to sit idle at d == 7 while enemy rebuilds r16 T2! — OR
                    # - enemy opened Transport-first (early_en_transport), OR
                    # - we opened r3/r4 G1 (first_galley_round <= 4) and saw 0 enemy Galleys by r8-11
                    #   (proving enemy did NOT open Galley-first even if their flanking T1 stayed in fog!).
                    opened_g1_uncontested = (
                        self.first_galley_round is not None
                        and self.first_galley_round <= 4
                        and not self.enemy_galleys_seen
                        and not self.ever_killed_enemy_galley
                    )
                    # Enter/hold Stage A cork even after saw_home_raid (Breakwater v10 sends T1/T2 for 1000
                    # middle wheat after a home landing and has 0 Galleys until r12-15! At d == 2 our vision-3
                    # sees enemy Port every turn so any fresh enemy G1 is spotted at spawn -> safe retreat 2->7!).
                    cork_entry_safe = (
                        (d_cur <= 3)
                        or en_g_away_at_my_port
                        or (s.round <= (14 if (early_en_transport or opened_g1_uncontested) else 7))
                    )
                    if (
                        ((not self.enemy_galleys_seen and not self.ever_killed_enemy_galley) or en_g_away_at_my_port)
                        and cork_lo <= s.round <= cork_hi
                        and cork_entry_safe
                    ):
                        # Never end a late approach (r >= 9) inside the blind dead-zone d in (4, 5, 6):
                        # stage at d == 7 this turn, then jump 7 -> 2 cleanly next turn!
                        if s.round >= 9 and d_cur > g.mp + 3 and d_cur - g.mp in (4, 5, 6):
                            picket_a = [
                                h for h, d in field.items()
                                if d == 7 and self.sea.get(h) == sea and h not in shore_d
                            ]
                            if picket_a:
                                my_p_h = s.cities[self.my_port_cid].hex if self.my_port_cid is not None else g.hex
                                best_pa = sorted(picket_a, key=lambda h: (bb.distance(h, g.hex), bb.distance(h, my_p_h), h))[:2]
                                if g.hex not in best_pa:
                                    return self._step_sail_galley(g, self.sea_field(best_pa), avoid=shore_d, hard_avoid=respect_g)
                                return False
                        # If enemy Port has ONLY 1 port-water hex (len(off) == 1) and 0 enemy Galleys exist,
                        # park DIRECTLY ON off[0] (d == 0): 100% hard-blockades enemy Port so they can NEVER
                        # launch T2 or G1 or unload middle wheat! Otherwise hold d == 2 on the middle side.
                        if len(off) == 1 and off[0] not in shore_d and s.round <= 25:
                            best_cork = off
                        else:
                            cork2 = [
                                h for h, d in field.items()
                                if d == 2 and self.sea.get(h) == sea and h not in shore_d
                            ]
                            best_cork = []
                            if cork2:
                                my_p_h = s.cities[self.my_port_cid].hex if self.my_port_cid is not None else g.hex
                                mid_l = [
                                    h for isle in sorted(self.resource_isles())
                                    for h in self.isle_landings(isle)
                                    if self.sea.get(h) == sea
                                ]
                                best_cork = sorted(
                                    cork2,
                                    key=lambda h: (
                                        1 if max((bb.distance(h, p) for p in off), default=0) > 3 else 0,
                                        max((bb.distance(h, p) for p in off), default=0),
                                        min((bb.distance(h, m) for m in mid_l), default=bb.distance(h, my_p_h)),
                                        bb.distance(h, my_p_h),
                                        h,
                                    ),
                                )[:2]
                        if best_cork:
                            if g.hex not in best_cork:
                                return self._step_sail_galley(g, self.sea_field(best_cork), avoid=shore_d, hard_avoid=respect_g)
                            return False
                    elif 7 <= s.round <= 22:
                        picket = [
                            h for h, d in field.items()
                            if d == 7 and self.sea.get(h) == sea and h not in shore_d
                        ] or [
                            h for h, d in field.items()
                            if d == 8 and self.sea.get(h) == sea and h not in shore_d
                        ]
                        if picket:
                            my_pw_pk = [h for h in self.port_water[self.my_port_cid] if self.sea.get(h) == sea] if self.my_port_cid is not None else []
                            my_pf_pk = self.sea_field(my_pw_pk) if my_pw_pk else {}
                            my_p_h = s.cities[self.my_port_cid].hex if self.my_port_cid is not None else g.hex
                            mid_pk = [
                                h for isle in sorted(self.resource_isles())
                                for h in self.isle_landings(isle)
                                if self.sea.get(h) == sea
                            ]
                            if mid_pk and not self.ever_sank_enemy_ferry:
                                # Position our d == 7 picket ON the Middle <-> Enemy Port wheat highway so we
                                # intercept & sink enemy's returning ferry while STILL staying safe from fresh enemy G1!
                                best_pickets = sorted(
                                    picket,
                                    key=lambda h: (
                                        min(bb.distance(h, m) for m in mid_pk),
                                        my_pf_pk.get(h, 999),
                                        bb.distance(h, my_p_h),
                                        h,
                                    ),
                                )[:2]
                            else:
                                best_pickets = sorted(
                                    picket,
                                    key=lambda h: (my_pf_pk.get(h, 999), bb.distance(h, my_p_h), h),
                                )[:2]
                            if g.hex not in best_pickets:
                                return self._step_sail_galley(g, self.sea_field(best_pickets), avoid=shore_d, hard_avoid=respect_g)
                            return False
                if s.round > 22 and d_cur > g.mp + 3:
                    picket = [
                        h for h, d in field.items()
                        if d == 7 and self.sea.get(h) == sea and h not in shore_d
                    ]
                    if picket and d_cur - g.mp in (4, 5, 6):
                        my_p_h = s.cities[self.my_port_cid].hex if self.my_port_cid is not None else g.hex
                        best_pickets = sorted(picket, key=lambda h: bb.distance(h, my_p_h))[:3]
                        if g.hex not in best_pickets:
                            return self._step_sail_galley(g, self.sea_field(best_pickets), avoid=shore_d, hard_avoid=respect_g)
                        return False
            elif my_galleys == 2 and s.round >= 8 and not en_galleys:
                d_cur = field.get(g.hex, 99)
                # Fleet Cohesion: if our 2nd healthy Galley is catching up from behind (3..16 hexes further
                # from enemy Port!), hold at d == 7..14 so BOTH Galleys jump into enemy Port TOGETHER (2v1 kill!)
                # instead of arriving one-by-one and feeding a free first strike!
                trailing = [
                    u for u in s.my_units()
                    if u.id != g.id and u.type == self.galley_code and u.hp >= 65
                    and self.sea.get(u.hex) == sea and field.get(u.hex, 99) > d_cur + 2
                    and field.get(u.hex, 99) <= d_cur + 16
                ]
                if trailing and 6 <= d_cur <= 14 and d_cur not in (4, 5, 6):
                    return False
                # Universal Mod-5 First-Strike Stepping (12 -> 7 -> 2): NEVER end in blind dead-zones
                # (9, 10, 11) against Galley-first rivals or (4, 5, 6) when enemy already has Galleys!
                # When 2 full-HP Galleys are UNITED with 0 enemy Galleys seen (r<=14), rush straight in to
                # 1-turn sink enemy T2 / hard-blockade Port before 1,000 wheat unloads!
                stage_d = None
                if d_cur > g.mp + 3 and d_cur - g.mp in (4, 5, 6) and (
                    bool(self.enemy_galleys_seen) or self.ever_killed_enemy_galley or s.round > 14 or not other_g_near
                ):
                    stage_d = 7
                elif galley_first_foe and s.round >= 11 and d_cur >= 12 and d_cur - g.mp in (9, 10, 11):
                    stage_d = 12
                if stage_d is not None:
                    picket = [
                        h for h, d in field.items()
                        if d == stage_d and self.sea.get(h) == sea and h not in shore_d
                    ]
                    if picket:
                        my_p_h = s.cities[self.my_port_cid].hex if self.my_port_cid is not None else g.hex
                        best_pickets = sorted(picket, key=lambda h: bb.distance(h, my_p_h))[:3]
                        if g.hex not in best_pickets:
                            return self._step_sail_galley(g, self.sea_field(best_pickets), avoid=shore_d, hard_avoid=respect_g)
                        return False
            safe_off = [h for h in off if h not in shore_d]
            if safe_off:
                taken = {
                    u.hex for u in s.my_units()
                    if u.id != g.id and u.type == self.galley_code and u.hex in safe_off
                }
                free_off = [h for h in safe_off if h not in taken] or safe_off
                if g.hex not in free_off:
                    return self._step_sail_galley(g, self.sea_field(free_off), avoid=shore_d, hard_avoid=respect_g)
                return False
            safe = [h for h in field if h not in shore_d and self.sea.get(h) == sea]
            if safe:
                low = min(field[h] for h in safe)
                goals = [h for h in safe if field[h] == low]
                if g.hex not in goals:
                    return self._step_sail_galley(g, self.sea_field(goals), avoid=shore_d, hard_avoid=respect_g)
                return False

        # 4. Patrol off the middle island to guard our ferry & catch enemy raiders!
        for isle in sorted(self.resource_isles()):
            spots = [h for h in self.isle_landings(isle) if self.sea.get(h) == sea and h not in shore_d]
            if spots and g.hex not in spots:
                return self._step_sail_galley(g, self.sea_field(spots), avoid=shore_d, hard_avoid=respect_g)
        return False

    # ------------------------------------------------------------------
    # Ferries: Turn-4 Full-Boat Middle Rush + Fast First-Galley Return!
    # ------------------------------------------------------------------

    def run_ferries(self, danger):
        s = self.state
        r_isles = self.resource_isles()
        for tid in sorted(self.ferries):
            if self.out_of_time() or tid not in s.units:
                continue
            t = s.units[tid]
            f = self.ferries[tid]
            isle = f["isle"]
            sites = r_isles.get(isle, [])
            if not sites:
                continue
            self._hire_ferry_wagons(t, f, isle)
            if not t.fresh:
                self._step_ferry(t, f, isle, sites, danger)

    def _hire_ferry_wagons(self, t, f, isle):
        """Immediately assign our starting wagon on Turn 4 to ferry the middle island!
        Keep a 2nd wagon at home once we have >= 2 wagons (and assign a 2nd ferry wagon at >= 3)."""
        s = self.state
        if not self.wagon_code:
            return
        sea = self.sea.get(t.hex)
        ports = self.my_ports(sea)
        if not ports:
            return
        port = s.cities[f["port"]] if (f["port"] is not None and s.cities[f["port"]].owner == self.player) else ports[0]
        f["port"] = port.id
        home_l = self.land.get(port.hex)

        # Register any wagon already aboard t (e.g. boarded fresh T1 on Round 3!) before hiring home wagons!
        for p in s.passengers(t):
            if p.type == self.wagon_code and p.id not in self.ferry_wagons:
                self.ferry_wagons[p.id] = isle
        cur = [wid for wid, i in self.ferry_wagons.items() if i == isle and wid in s.units]
        home_wagons = [
            u for u in s.my_units()
            if u.type == self.wagon_code and not u.carrier and self.land.get(u.hex) == home_l
            and u.id not in self.ferry_wagons and u.id != self.shuttle_wagon
        ]
        total_wagons = sum(1 for u in s.my_units() if u.type == self.wagon_code)
        want = 2 if total_wagons >= 3 else 1
        want = min(want, max(1, self.passengers_max - 1))
        need = want - len(cur)
        if need <= 0:
            return

        # If middle island is held/contested by enemy HEAVY combat units (Infantry/Pike, NOT a lone Armada Scout!),
        # keep our last home wagon harvesting our home site instead of pulling it off to sit idle!
        my_guards, _, _ = self.isle_status(isle)
        known_en_heavy = self.isle_known_capturers(isle)
        if known_en_heavy > 0 and (len(my_guards) == 0 or (len(my_guards) <= known_en_heavy and len(home_wagons) <= 1)):
            return

        # CRITICAL: If we currently have ZERO ferry wagons for the middle island, send our first wagon
        # immediately (home site caps at 850 wheat = 28 rounds, while middle makes 100/round!).
        home_sites = [x for x in s.sites if self.land.get(x.hex) == home_l]
        if len(cur) == 0:
            can_spare = len(home_wagons)
        else:
            can_spare = len(home_wagons) - (1 if home_sites else 0)
        if can_spare <= 0:
            return
        # When we have 0 ferry wagons on a clear/scout-only middle, also hire a returning home wagon
        # (w.wheat > 0) so _step_ferry unloads it straight into Port and boards T1 without ping-ponging!
        if len(cur) == 0 and known_en_heavy == 0:
            cands = [w for w in home_wagons if not w.fresh]
        else:
            cands = [w for w in home_wagons if not w.fresh and w.wheat == 0]
        cands.sort(key=lambda w: (w.wheat > 0, self.city_field[port.id].get(w.hex, 1 << 30), w.id))
        for w in cands[:min(need, can_spare)]:
            self.ferry_wagons[w.id] = isle

    def _ferry_guard_bread_need(self, w, isle):
        fws = sorted(wid for wid, i in self.ferry_wagons.items() if i == isle and wid in self.state.units)
        if not fws or w.id != fws[0]:
            return 0
        guards, _, _ = self.isle_status(isle)
        if not guards:
            return 0
        # Only haul guard rations when middle troops are actually getting low (< 26 rations),
        # never stealing 180 bread from our Port on r13-16 while troops still have 40+ rations!
        if all(g.rations >= 26 for g in guards):
            return 0
        return min(240, 50 * len(guards))

    def _step_ferry(self, t, f, isle, sites, danger):
        s = self.state
        my_guards, en_land, _ = self.isle_status(isle)
        known_en = self.isle_known_enemies(isle)
        my_fws = [s.units[wid] for wid, i in sorted(self.ferry_wagons.items()) if i == isle and wid in s.units]
        w_dmg = self.wagon_incoming_damage()

        # Instant Raid-Abort Counter-Strike: if we evacuated I1+I2 home to block an Armada raid and Armada
        # immediately aborted and landed Infantry onto middle (en_caps_top >= 1, home safe!), do NOT unload
        # I1+I2 onto Home Port (zero Port ration drain -> keeps r7 G1 on schedule!) — flip straight to "out"
        # and Amphibious-First-Strike Armada's newly landed Infantry THIS TURN!
        if f["phase"] == "back" and t.id in s.units:
            combat_ab_top = [p for p in s.passengers(t) if not self.types[p.type].carry]
            en_caps_top = [e for e in en_land if self.types[e.type].capture]
            en_home_top = any(
                not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
                for e in self.enemies.values()
            )
            en_raid_top = any(self.is_home_raid_transport(e) for e in self.enemies.values())
            if (
                combat_ab_top
                and en_caps_top
                and not en_home_top
                and not en_raid_top
                and any(p.mp > 0 for p in combat_ab_top)
            ):
                f["phase"], f["waited"] = "out", 0

        # Emergency Transport Survival: ONLY when wounded (<= 85 HP, so 1 more Galley hit = SINK!)
        # inside an enemy Galley's strike zone — unload combat troops (and any empty Wagon onto a safe beach!)
        # if at middle, then IMMEDIATELY flee out of galley_d!
        galley_d_em, shore_d_em = self.naval_danger()
        if not t.fresh and not t.acted and t.mp > 0 and t.hex in galley_d_em and t.hp <= 85:
            if t.hex in self.isle_landings(isle):
                for p in list(s.passengers(t)):
                    if not self.types[p.type].carry and p.mp > 0:
                        reach_p = self.reachable(p)
                        opts_p = [h for h in reach_p if self.land.get(h) == isle]
                        if opts_p:
                            best_hp = max(
                                opts_p,
                                key=lambda h: (
                                    -4.0 * danger.get(h, 0.0) * (1.0 - self.defense.get(h, 0) / 100.0)
                                    + self.defense.get(h, 0) / 8.0
                                    - min(self.site_field[x.id].get(h, 99) for x in sites) * 0.4,
                                ),
                            )
                            self.go(p, reach_p[best_hp])
                # Also drop an empty ferry wagon onto a safe middle beach (screened by our troops!) so it
                # harvests 600-1000 middle wheat while our G1 kills enemy G1, instead of being hauled back home empty!
                empty_ws = [p for p in list(s.passengers(t)) if self.types[p.type].carry and p.wheat == 0 and p.mp > 0]
                if empty_ws:
                    self.refresh()
                    w_dmg_em = self.wagon_incoming_damage()
                    claims_em = {}
                    for pw in empty_ws:
                        self._land_wagon_on_isle(pw, sites, claims_em, danger, w_dmg_em)
            reach_t = self.reachable(t)
            safe_t = [h for h in reach_t if h not in galley_d_em and h not in shore_d_em]
            if safe_t:
                my_pw_t = self.port_water[self.my_port_cid] if self.my_port_cid is not None else []
                my_ft = self.sea_field(my_pw_t) if my_pw_t else {}
                best_t = min(safe_t, key=lambda h: (my_ft.get(h, 99), len(reach_t[h])))
                self.go(t, reach_t[best_t])
            else:
                en_gs = [e.hex for e in self.enemies.values() if self.types[e.type].naval and not self.types[e.type].transport]
                if en_gs and reach_t:
                    best_t = max(reach_t, key=lambda h: (min(bb.distance(h, eh) for eh in en_gs), -len(reach_t[h])))
                    if best_t != t.hex:
                        self.go(t, reach_t[best_t])
            # If we dropped our wagon ashore on middle (and carry no wheat home), return directly ("out")
            # to pick up its harvest the instant the enemy Galley dies instead of running all the way to Port!
            has_ashore_w = any(
                w.id in s.units and not w.carrier and self.land.get(w.hex) == isle
                for w in my_fws
            )
            has_wheat_aboard = any(self.types[p.type].carry and p.wheat > 0 for p in s.passengers(t))
            f["phase"], f["waited"] = ("out" if (has_ashore_w and not has_wheat_aboard) else "dock"), 0
            return

        # On our VERY FIRST trip (trips == 0 & no guards ashore & no enemy heavy infantry on middle),
        # pack a FULL BOAT (W1 + 2 I + S)! A lone enemy Scout on middle (Armada!) does NOT block W1!
        # If enemy infantry control or deadlock the middle island, switch this ferry into a combat reinforcement shuttle!
        known_en_heavy = self.isle_known_capturers(isle)
        my_caps_now = sum(1 for u in my_guards if self.types[u.type].capture)
        first_rush = (f.get("trips", 0) == 0 and len(my_guards) == 0 and known_en_heavy == 0)
        contested_need_troops = known_en > 0 and len(my_guards) <= known_en
        enemy_controls_isle = known_en_heavy >= 1 and len(my_guards) == 0
        isle_unsafe_for_wagon = (
            enemy_controls_isle
            or (known_en_heavy >= 1 and my_caps_now <= known_en_heavy)
            or (f.get("aborted_trip", -99) >= s.round - 4 and known_en_heavy >= 1)
        )
        want_guard = first_rush or len(my_guards) < 2 or contested_need_troops

        if f["phase"] == "dock":
            port, there = self.dock_transport(t, f["port"], is_ferry=True)
            if port is None:
                return
            f["port"] = port.id
            home_l = self.land.get(port.hex)

            for p in list(s.passengers(t)):
                if self.types[p.type].carry and there and (p.wheat > 0 or isle_unsafe_for_wagon):
                    self._land_wagon_in_port(p, port, isle)
                    if isle_unsafe_for_wagon and not p.carrier:
                        # Release empty wagon to harvest home wheat while we build troops to win middle!
                        has_other_home_w = any(
                            u.id != p.id and u.type == self.wagon_code and u.id not in self.ferry_wagons
                            and not u.carrier and self.land.get(u.hex) == home_l
                            for u in s.my_units()
                        )
                        if not has_other_home_w:
                            self.ferry_wagons.pop(p.id, None)

            # Emergency home-raid unload: if enemy troops or a loaded transport threaten our Home Port,
            # immediately disembark any combat passengers aboard T1 onto our Home Island to defend!
            home_crisis_dock = (
                self.home_invasion_active()
                or any(
                    not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == home_l
                    for e in self.enemies.values()
                )
                or any(
                    self.types[e.type].transport and len(s.passengers(e)) > 0 and bb.distance(e.hex, port.hex) <= 6
                    for e in self.enemies.values()
                )
            )
            if there and home_crisis_dock:
                for p in list(s.passengers(t)):
                    if not self.types[p.type].carry and p.mp > 0:
                        reach = self.reachable(p)
                        opts = [h for h in reach if self.land.get(h) == home_l]
                        if opts:
                            best_h = min(opts, key=lambda h: (self.city_field[port.id].get(h, 99), -self.defense.get(h, 0)))
                            self.go(p, reach[best_h])

            # Refresh ferry wagons list in case one was released to harvest home site
            my_fws = [s.units[wid] for wid, i in sorted(self.ferry_wagons.items()) if i == isle and wid in s.units]

            # Don't board wagons if middle island is hostile/unsafe for wagons, OR if our own Port is currently
            # Galley-blockaded (release wagon so it escapes Galley fire inland and funds an Anti-Blockade Archer!).
            port_g_block = self.galley_blockaded(port)
            for w in my_fws:
                if not w.carrier and self.land.get(w.hex) == home_l and not w.fresh:
                    if port_g_block and not there:
                        self.ferry_wagons.pop(w.id, None)
                        continue
                    if w.wheat > 0:
                        self.enter_city(w, port, danger)
                        if w.hex == port.hex:
                            self._jit_unload(w, port, w_dmg)
                            if w.wheat == 0:
                                self._load_guard_bread(w, port, isle)
                        if w.wheat > 0:
                            continue
                    if isle_unsafe_for_wagon:
                        # Release to harvest home wheat if no other home wagon exists!
                        has_other_home_w = any(
                            u.id != w.id and u.type == self.wagon_code and u.id not in self.ferry_wagons
                            and not u.carrier and self.land.get(u.hex) == home_l
                            for u in s.my_units()
                        )
                        if not has_other_home_w:
                            self.ferry_wagons.pop(w.id, None)
                        continue
                    need_b = self._ferry_guard_bread_need(w, isle)
                    if need_b - w.bread >= 50 and port.bread >= 150:
                        self.enter_city(w, port, danger)
                        if w.hex == port.hex:
                            self._load_guard_bread(w, port, isle)
                        continue
                    if there and self.board_transport(w, t):
                        continue
                    adj = [n for n in [port.hex] + bb.neighbors(port.hex) if n in self.defense]
                    if w.hex not in adj:
                        self.move_unit_toward(w, self.distance_field(adj), danger, caution=1.4)

            my_fws = [s.units[wid] for wid, i in sorted(self.ferry_wagons.items()) if i == isle and wid in s.units]

            if there and want_guard and len(s.passengers(t)) < self.passengers_max:
                guard_aboard = sum(1 for p in s.passengers(t) if not self.types[p.type].carry)
                wagons_behind = 0 if isle_unsafe_for_wagon else sum(
                    1 for w in my_fws if not w.carrier and self.land.get(w.hex) == home_l
                )
                free_seats = max(0, self.passengers_max - len(s.passengers(t)) - wagons_behind)
                want_g = (4 if isle_unsafe_for_wagon else (3 if first_rush else (3 if contested_need_troops else 1))) - guard_aboard
                if want_g > 0 and free_seats > 0:
                    en_g_active = any(s.round - r <= 20 for r in self.enemy_galleys_seen.values())
                    home_fighters = [
                        u for u in s.my_units()
                        if not u.carrier and not u.fresh and not u.acted and u.mp > 0
                        and self.land.get(u.hex) == home_l
                        and not self.types[u.type].naval and not self.types[u.type].carry
                        and u.id not in self.crew_of and u.id not in self.hungry_busy
                        and not (en_g_active and u.type == self.archer_code)
                    ]
                    home_fighters.sort(key=lambda u: (
                        self.city_field[port.id].get(u.hex, 1 << 30) > self.types[u.type].move,
                        not self.types[u.type].capture,
                        self.types[u.type].skirmish,
                        self.city_field[port.id].get(u.hex, 1 << 30),
                        u.id,
                    ))
                    enemy_on_home = any(
                        self.land.get(e.hex) == home_l and not e.carrier and not self.types[e.type].naval
                        for e in self.enemies.values()
                    )
                    enemy_trans_near_home = any(
                        self.types[e.type].transport and bb.distance(e.hex, port.hex) <= 7
                        for e in self.enemies.values()
                    )
                    # NEVER strip our Home Island of its last capturer! Always reserve 1 Infantry on our Home Port
                    # on compact maps (Islets) so an enemy Transport raid can NEVER capture an empty Home Port.
                    # CRITICAL: When we already have >= 3 home capturers on first_rush (I1 & I2 at Port + I3
                    # walking from Home City!), keep the FURTHEST capturer (I3) as home keeper so I1 & I2 board T1
                    # IMMEDIATELY with zero stall (landing 2 I + S + W1 on middle 1-2 turns faster!).
                    # On our Round-4/5 first middle rush (T1 + W2 opening!), send ALL 4 starting units
                    # (W1 + 2 I + S) straight to middle unless an enemy raid is visibly threatening home!
                    # After r7 (or on later trips), always reserve 1 keeper Infantry at Home Port!
                    have_g_dk = bool(self.galley_code) and any(u.type == self.galley_code for u in s.my_units())
                    en_g_live_dk = any(
                        s.round - r <= 14 and eid not in self.killed_galley_ids
                        for eid, r in self.enemy_galleys_seen.items()
                    )
                    sea_supremacy_dk = (
                        have_g_dk
                        and not en_g_live_dk
                        and self.ever_sank_enemy_transport
                        and not enemy_on_home
                        and not enemy_trans_near_home
                        and not self.home_invasion_active()
                    )
                    my_caps_isle_dk = sum(1 for u in my_guards if self.types[u.type].capture)
                    home_caps_dk = sum(
                        1 for u in s.my_units()
                        if not u.carrier and self.land.get(u.hex) == home_l and self.types[u.type].capture
                    )
                    en_aslt_live = (
                        bool(self.en_assault_t_ids)
                        and s.round <= 11
                        and not self.ever_sank_enemy_transport
                        and self.home_kills_during_alert == 0
                    )
                    raid_risk = (
                        enemy_on_home
                        or enemy_trans_near_home
                        or en_aslt_live
                        or self.home_invasion_active()
                        or (
                            not sea_supremacy_dk
                            and not (first_rush and s.round <= 9 and (have_g_dk or home_caps_dk <= 2))
                            and not (
                                s.round <= 11
                                and my_caps_isle_dk == 0
                                and (self.isle_known_capturers(isle) >= 2 or have_g_dk)
                                and not enemy_on_home
                                and not enemy_trans_near_home
                            )
                            and (
                                self.port_sea_dist <= 21
                                or (not self.ever_killed_enemy_galley and not self.enemy_galleys_seen and s.round <= 14)
                            )
                        )
                    )
                    if raid_risk:
                        all_home_caps = [
                            u for u in s.my_units()
                            if not u.carrier and self.land.get(u.hex) == home_l and self.types[u.type].capture
                        ]
                        if all_home_caps:
                            if first_rush and len(all_home_caps) >= 3 and not enemy_on_home and not enemy_trans_near_home:
                                keeper = max(
                                    all_home_caps,
                                    key=lambda u: (self.city_field[port.id].get(u.hex, -1), u.id),
                                )
                            else:
                                keeper = min(
                                    all_home_caps,
                                    key=lambda u: (u.hex != port.hex, self.city_field[port.id].get(u.hex, 999), u.id),
                                )
                            home_fighters = [u for u in home_fighters if u.id != keeper.id]
                    if enemy_on_home or enemy_trans_near_home or self.home_invasion_active() or (en_aslt_live and home_caps_dk <= 2):
                        spare = 0
                    elif first_rush and s.round <= 13:
                        spare = len(home_fighters)
                    elif (contested_need_troops or isle_unsafe_for_wagon) and len(home_fighters) >= 1:
                        spare = len(home_fighters)
                    else:
                        spare = max(0, len(home_fighters) - (0 if raid_risk else 1))
                    for g in home_fighters[:min(want_g, free_seats, spare)]:
                        if len(s.passengers(t)) >= self.passengers_max:
                            break
                        if not self.board_transport(g, t):
                            adj = [n for n in bb.neighbors(t.hex) if n in self.defense]
                            if adj:
                                self.move_unit_toward(g, self.distance_field(adj), danger, caution=1.0)

            aboard_wagons = [w for w in my_fws if w.carrier == t.id]
            aboard_guards = [p for p in s.passengers(t) if not self.types[p.type].carry]
            behind_wagons = [] if isle_unsafe_for_wagon else [
                w for w in my_fws if not w.carrier and self.land.get(w.hex) == home_l
            ]
            stranded = [w for w in my_fws if not w.carrier and self.land.get(w.hex) == isle]
            # If we saw a home raid and an incoming enemy galley is due (r9-14) and not killed yet,
            # shelter T1 on the BACK SIDE of our home island (out of Breakwater G1's reach!) while G1 ambushes at Port!
            galley_ambush_wait = False
            # Don't launch unarmed T1 out into fog ahead of our own G1 on r6-7 before G1 sweeps our crossing
            # (prevents sailing T1 straight into a watch_channel camping G1, with ZERO delay vs Armada!).
            escort_sweep_wait = (
                there
                and self.port_sea_dist <= 21
                and self.first_galley_round is not None
                and s.round <= self.first_galley_round + 3
                and known_en == 0
                and not self.ever_killed_enemy_galley
                and not self.enemy_galleys_seen
                and not self.swept_my_crossing
                and any(u.type == self.galley_code for u in s.my_units())
                and f["waited"] < 1
            )
            if galley_ambush_wait:
                if self.enemy_port_cid is not None:
                    sea = self.sea.get(t.hex)
                    en_pw = [h for h in self.port_water[self.enemy_port_cid] if self.sea.get(h) == sea]
                    my_pw = [h for h in self.port_water[port.id] if self.sea.get(h) == sea]
                    if en_pw and my_pw:
                        en_f = self.sea_field(en_pw)
                        my_f = self.sea_field(my_pw)
                        shelter = [
                            h for h in self.sea_hexes
                            if self.sea.get(h) == sea and 2 <= my_f.get(h, 99) <= 5
                        ]
                        if shelter:
                            best_sh = max(shelter, key=lambda h: (en_f.get(h, 0), -my_f.get(h, 99)))
                            if t.hex != best_sh:
                                galley_d, shore_d = self.naval_danger()
                                self.safe_sail(t, self.sea_field([best_sh]), avoid=shore_d, hard_avoid=galley_d)
            elif escort_sweep_wait:
                f["waited"] += 1
            elif (
                there
                and first_rush
                and len(s.passengers(t)) < self.passengers_max
                and (
                    (
                        sum(1 for p in aboard_guards if self.types[p.type].capture) < 2
                        and sum(
                            1 for u in s.my_units()
                            if not u.carrier and self.land.get(u.hex) == home_l
                            and self.types[u.type].capture
                            and u.id not in self.crew_of and u.id not in self.hungry_busy
                        ) > 1
                        and f["waited"] < 2
                    )
                    or (
                        len(aboard_guards) < 2
                        and sum(
                            1 for u in s.my_units()
                            if not u.carrier and self.land.get(u.hex) == home_l
                            and not self.types[u.type].naval and not self.types[u.type].carry
                            and u.id not in self.crew_of and u.id not in self.hungry_busy
                        ) > 1
                        and f["waited"] < 1
                    )
                )
            ):
                # Only wait for a spare guard that actually exists at home (never stall 2 turns when r4-5 W2
                # leaves exactly 1 spare Infantry + 1 Scout + 1 Wagon to board T1 immediately!).
                f["waited"] += 1
            elif there and isle_unsafe_for_wagon:
                need_force = min(self.passengers_max, max(1 if my_guards else 3, known_en - len(my_guards) + 1))
                # Don't sit idle in Port waiting for a 4th guard when no more spare home fighters exist!
                more_home_fighters = sum(
                    1 for u in s.my_units()
                    if not u.carrier and self.land.get(u.hex) == home_l
                    and not self.types[u.type].naval and not self.types[u.type].carry
                    and u.id not in self.crew_of and u.id not in self.hungry_busy
                )
                no_more_spare = more_home_fighters <= 1
                aboard_caps_d = sum(1 for p in aboard_guards if self.types[p.type].capture)
                # Never suicide a lone Scout onto enemy Infantry, but allow launching with >= 1 capturer when
                # no more spare home fighters exist (prevents permanent T1 deadlock at Port!).
                min_launch_ok = (not enemy_controls_isle) or aboard_caps_d >= 1
                if len(aboard_guards) >= need_force or (aboard_guards and min_launch_ok and (no_more_spare or f["waited"] >= 2)):
                    f["phase"], f["waited"] = "out", 0
                elif aboard_guards:
                    f["waited"] += 1
            elif there and aboard_wagons and (not behind_wagons or f["waited"] >= 2):
                f["phase"], f["waited"] = "out", 0
            elif there and aboard_guards and not aboard_wagons and not behind_wagons:
                f["phase"], f["waited"] = "out", 0
            elif there and not aboard_wagons and not behind_wagons and stranded:
                # Always go back to rescue/fetch a stranded wagon!
                f["phase"], f["waited"] = "out", 0
            elif aboard_wagons:
                f["waited"] += 1

        if f["phase"] == "out" and t.id in s.units:
            # Emergency recall: if a loaded enemy transport or invasion hits our Home Island while we only
            # have <= 1 capturer at home and T1 still has combat troops aboard, bring them straight home!
            combat_aboard = [p for p in s.passengers(t) if not self.types[p.type].carry]
            if combat_aboard and self.my_port_cid is not None:
                my_ph_rec = s.cities[self.my_port_cid].hex
                enemy_landed_out = any(
                    not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
                    for e in self.enemies.values()
                )
                home_crisis_out = (
                    self.home_invasion_active()
                    or enemy_landed_out
                    or any(
                        self.is_home_raid_transport(e)
                        for e in self.enemies.values()
                    )
                )
                home_caps_left = sum(
                    1 for u in s.my_units()
                    if not u.carrier and self.land.get(u.hex) == self.home_land and self.types[u.type].capture
                )
                if home_crisis_out and home_caps_left <= (1 if enemy_landed_out else 0):
                    f["phase"], f["waited"] = "dock", 0
                    port, there = self.dock_transport(t, f["port"], is_ferry=True)
                    if port is not None and there:
                        home_l = self.land.get(port.hex)
                        for p in list(combat_aboard):
                            if p.id in s.units and p.carrier == t.id and p.mp > 0:
                                reach = self.reachable(p)
                                opts = [h for h in reach if self.land.get(h) == home_l]
                                if opts:
                                    best_h = min(opts, key=lambda h: (self.city_field[port.id].get(h, 99), -self.defense.get(h, 0)))
                                    self.go(p, reach[best_h])
                    return
            spots = self.isle_landings(isle)
            sea = self.sea.get(t.hex)
            goals = [w for w in spots if self.sea.get(w) == sea]
            en_caps_isle = [e for e in en_land if self.types[e.type].capture]
            my_caps_isle = [u for u in my_guards if self.types[u.type].capture]
            ready_caps_aboard = [p for p in combat_aboard if self.types[p.type].capture and p.mp > 0]

            def _seaborne_en_caps():
                return sum(
                    sum(1 for p in s.passengers(e) if self.types[p.type].capture)
                    for e in self.enemies.values()
                    if self.types[e.type].transport
                    and any(bb.distance(e.hex, x.hex) <= 7 for x in sites)
                )

            en_caps_seaborne = _seaborne_en_caps()

            # Helper: AMPHIBIOUS FIRST STRIKE (crushes P1-first-strike advantage on middle when Trident is P2!):
            # When enemy Infantry are already ashore on middle (or poised aboard an enemy transport off middle!)
            # and we have >= 2 ready Infantry aboard T1 (with 0 friendly Infantry ashore yet):
            #   1) If T1 can reach a safe coastal berth adjacent to a beach next to an enemy Infantry,
            #      pull right up beside them and disembark so land_fight() STRIKES AS THEY LAND (instant 1-turn kill)!
            #   2) Otherwise, if our ferry wagon STILL has 0 MP (just boarded), HOLD all troops safely aboard T1
            #      for 1 turn (zero wagon delay!) — pulling any scout from Scout-First right back aboard!
            def _try_amphibious_strike(cur_en_caps, cur_ready_caps, cur_seaborne=0, scout_u=None):
                if not (goals and (cur_en_caps or cur_seaborne >= 2) and len(my_caps_isle) == 0 and len(cur_ready_caps) >= 2):
                    return False
                galley_d_a, shore_d_a = self.naval_danger()
                reach_t = self.reachable(t) if (not t.fresh and not t.acted and t.mp > 0) else {}
                reach_t[t.hex] = ""
                strike_opt = None
                if cur_en_caps:
                    for w in goals:
                        if w not in reach_t or w in galley_d_a or w in shore_d_a:
                            continue
                        for b in bb.neighbors(w):
                            if self.land.get(b) != isle or b in self.enemy_hex:
                                continue
                            if self.occ.get(b, 0) + len(cur_ready_caps) > self.limit:
                                continue
                            for e in cur_en_caps:
                                if bb.distance(b, e.hex) == 1:
                                    # Don't amphibious-suicide onto 0% plains into a 40% Hill enemy when 2+ enemy Infantry can counter-kill us!
                                    if (
                                        self.defense.get(e.hex, 0) >= 35
                                        and self.defense.get(b, 0) < 25
                                        and len(cur_en_caps) >= 2
                                        and e.hp > 65
                                    ):
                                        continue
                                    key_s = (
                                        self.defense.get(e.hex, 0),
                                        e.hp,
                                        -self.defense.get(b, 0),
                                        len(reach_t[w]),
                                        w,
                                        b,
                                    )
                                    if strike_opt is None or key_s < strike_opt[0]:
                                        strike_opt = (key_s, w, b, e)
                if strike_opt is not None:
                    _, w_s, b_s, e_s = strike_opt
                    if reach_t[w_s]:
                        self.go(t, reach_t[w_s])
                    if t.id in s.units and t.hex == w_s:
                        f["ambush_wait"] = 0
                        aboard_s = sorted(
                            s.passengers(t),
                            key=lambda u: (self.types[u.type].carry, self.types[u.type].skirmish, u.id),
                        )
                        for p in aboard_s:
                            if p.mp <= 0 or self.types[p.type].carry:
                                continue
                            reach_p = self.reachable(p)
                            if b_s in reach_p and self.occ.get(b_s, 0) < self.limit:
                                self.go(p, reach_p[b_s])
                            else:
                                opts_p = [
                                    h for h in reach_p
                                    if self.land.get(h) == isle and self.occ.get(h, 0) < self.limit
                                ]
                                if opts_p:
                                    best_p = max(
                                        opts_p,
                                        key=lambda h: (
                                            (10.0 if bb.distance(h, e_s.hex) == 1 else 0.0)
                                            - 3.5 * danger.get(h, 0.0) * (1.0 - self.defense.get(h, 0) / 100.0)
                                            + self.defense.get(h, 0) / 8.0,
                                        ),
                                    )
                                    self.go(p, reach_p[best_p])
                        # Also unload any ready Wagon aboard T1 right behind our Amphibious First Strike
                        # (using refreshed ZOC!) so we don't lose a full harvest turn while enemy W steals middle!
                        ready_ws_s = [p for p in list(s.passengers(t)) if self.types[p.type].carry and p.mp > 0]
                        if ready_ws_s:
                            self.refresh()
                            w_dmg_s = self.wagon_incoming_damage()
                            claims_s = {}
                            for pw in ready_ws_s:
                                self._land_wagon_on_isle(pw, sites, claims_s, danger, w_dmg_s)
                        f["phase"], f["waited"] = "wait", 0
                        return True
                else:
                    # If our ferry wagon STILL has 0 MP (just boarded this turn so it CANNOT unload anyway!),
                    # hold our ready combat troops safely aboard T1 for this ONE turn (zero delay for W!)
                    # instead of dumping them into P1's 3-hex walk-and-strike kill zone — next turn T1 pulls
                    # right up for an instant Amphibious First Strike OR lands all 4 together on a safe beach!
                    wagon_sleeping = any(self.types[p.type].carry and p.mp <= 0 for p in s.passengers(t))
                    if wagon_sleeping and t.hex not in galley_d_a and f.get("ambush_wait", 0) < 1:
                        # If Scout-First just stepped ashore to reveal the island, pull Scout right back aboard T1
                        # before holding so we NEVER abandon Scout alone to die 3v1!
                        if scout_u is not None and scout_u.id in s.units and not scout_u.carrier:
                            self.board_transport(scout_u, t)
                            if scout_u.id in s.units and not scout_u.carrier and danger.get(scout_u.hex, 0.0) > 0:
                                f["ambush_wait"] = 0
                                return False
                        safe_goals = [w for w in goals if w not in galley_d_a and w not in shore_d_a]
                        if safe_goals:
                            if cur_en_caps:
                                best_wait = min(
                                    safe_goals,
                                    key=lambda w: (min(bb.distance(w, e.hex) for e in cur_en_caps), w),
                                )
                            else:
                                en_ts = [
                                    e for e in self.enemies.values()
                                    if self.types[e.type].transport
                                ]
                                best_wait = max(
                                    safe_goals,
                                    key=lambda w: (
                                        max((self.defense.get(b, 0) for b in bb.neighbors(w) if self.land.get(b) == isle), default=0),
                                        min((bb.distance(w, et.hex) for et in en_ts), default=0),
                                    ),
                                )
                            if t.hex != best_wait:
                                self.safe_sail(t, self.sea_field([best_wait]), avoid=shore_d_a, hard_avoid=galley_d_a)
                        f["ambush_wait"] = 1
                        return True
                    f["ambush_wait"] = 0
                    return False

            if _try_amphibious_strike(en_caps_isle, ready_caps_aboard, cur_seaborne=en_caps_seaborne):
                return
            if goals and t.hex not in goals:
                galley_d, shore_d = self.naval_danger()
                self.safe_sail(t, self.sea_field(goals), avoid=shore_d, hard_avoid=galley_d)
            if t.id in s.units and t.hex in goals:
                # CRITICAL: Refresh vision & island enemies IMMEDIATELY after safe_sail moves T1 to middle
                # BEFORE Scout-First runs — otherwise stale en_caps_isle=[] causes Scout-First to dump S#7
                # alone in front of enemy Infantry while T1 enters ambush_wait and sails away!
                self.refresh()
                danger = self.land_danger()
                my_guards, en_land, _ = self.isle_status(isle)
                en_caps_isle = [e for e in en_land if self.types[e.type].capture]
                my_caps_isle = [u for u in my_guards if self.types[u.type].capture]
                ready_caps_aboard = [
                    p for p in s.passengers(t)
                    if self.types[p.type].capture and p.mp > 0
                ]
                en_caps_seaborne = _seaborne_en_caps()
                if _try_amphibious_strike(en_caps_isle, ready_caps_aboard, cur_seaborne=en_caps_seaborne):
                    return
                # SCOUT-FIRST VISION REVEAL: T1 only has vision 2, so when arriving at an apparently
                # empty middle island carrying a ready Scout (vision 4!) + >= 2 ready Infantry, unload the
                # Scout FIRST to reveal the far beach before deciding where (or whether!) to unload Infantry!
                ready_scouts = [
                    p for p in s.passengers(t)
                    if self.types[p.type].skirmish and not self.types[p.type].capture and p.mp > 0
                ]
                if (
                    not en_caps_isle
                    and en_caps_seaborne < 2
                    and len(my_caps_isle) == 0
                    and ready_scouts
                    and len(ready_caps_aboard) >= 2
                ):
                    sc_u = ready_scouts[0]
                    reach_sc = self.reachable(sc_u)
                    opts_sc = [h for h in reach_sc if self.land.get(h) == isle]
                    if opts_sc:
                        best_sc = max(
                            opts_sc,
                            key=lambda h: (
                                self.map.terrain(h).vision_bonus * 10
                                - 4.0 * danger.get(h, 0.0)
                                + self.defense.get(h, 0) / 20.0
                                - min(self.site_field[x.id].get(h, 99) for x in sites) * 0.5,
                            ),
                        )
                        g_before_sc = {
                            e.id for e in self.enemies.values()
                            if self.types[e.type].naval and not self.types[e.type].transport
                        }
                        self.go(sc_u, reach_sc[best_sc])
                        self.refresh()
                        g_after_sc = {
                            e.id for e in self.enemies.values()
                            if self.types[e.type].naval and not self.types[e.type].transport
                        }
                        if (g_after_sc - g_before_sc) and self.galley_code and any(
                            u.type == self.galley_code and not u.fresh and not u.acted and u.mp > 0
                            for u in s.my_units()
                        ):
                            # Scout disembark just spotted a lurking enemy Galley! Immediately strike it
                            # with our waiting Galley BEFORE enemy G1 can hit T1 next turn!
                            self.run_galleys()
                            self.refresh()
                        danger = self.land_danger()
                        my_guards, en_land, _ = self.isle_status(isle)
                        en_caps_isle = [e for e in en_land if self.types[e.type].capture]
                        my_caps_isle = [u for u in my_guards if self.types[u.type].capture]
                        ready_caps_aboard = [
                            p for p in s.passengers(t)
                            if self.types[p.type].capture and p.mp > 0
                        ]
                        en_caps_seaborne = _seaborne_en_caps()
                        if _try_amphibious_strike(
                            en_caps_isle,
                            ready_caps_aboard,
                            cur_seaborne=en_caps_seaborne,
                            scout_u=sc_u,
                        ):
                            return
                # SAFE-BEACH REPOSITION: When enemy Infantry hold middle and our current berth's adjacent
                # beaches are ALL exposed 0% plains inside enemy 1-turn kill range (danger >= 1.5), sail T1
                # to a reachable safe berth with >= 25% cover or safe (danger < 1.5) beach BEFORE unloading!
                if en_caps_isle and len(my_caps_isle) == 0 and ready_caps_aboard and t.mp > 0 and not t.acted:
                    cur_beaches = [
                        b for b in bb.neighbors(t.hex)
                        if self.land.get(b) == isle and b not in self.enemy_hex and self.occ.get(b, 0) < self.limit
                    ]
                    if cur_beaches and all(
                        self.defense.get(b, 0) < 25
                        and danger.get(b, 0.0) >= 1.5
                        and not any(bb.distance(b, e.hex) == 1 for e in en_caps_isle)
                        for b in cur_beaches
                    ):
                        galley_d_sb, shore_d_sb = self.naval_danger()
                        reach_sb = self.reachable(t)
                        alt_berths = []
                        for w_b in goals:
                            if w_b == t.hex or w_b not in reach_sb or w_b in galley_d_sb or w_b in shore_d_sb:
                                continue
                            b_list = [
                                b for b in bb.neighbors(w_b)
                                if self.land.get(b) == isle and b not in self.enemy_hex and self.occ.get(b, 0) < self.limit
                            ]
                            good_b = [b for b in b_list if self.defense.get(b, 0) >= 25 or danger.get(b, 0.0) < 1.5]
                            if good_b:
                                best_gb = max(
                                    good_b,
                                    key=lambda b: (
                                        1 if (self.defense.get(b, 0) >= 25 and danger.get(b, 0.0) < 1.5) else 0,
                                        1 if self.defense.get(b, 0) >= 25 else 0,
                                        -danger.get(b, 0.0),
                                        self.defense.get(b, 0),
                                    ),
                                )
                                alt_berths.append((
                                    -(1 if (self.defense.get(best_gb, 0) >= 25 and danger.get(best_gb, 0.0) < 1.5) else 0),
                                    -(1 if self.defense.get(best_gb, 0) >= 25 else 0),
                                    danger.get(best_gb, 0.0),
                                    -self.defense.get(best_gb, 0),
                                    len(reach_sb[w_b]),
                                    w_b,
                                ))
                        if alt_berths:
                            best_w_sb = min(alt_berths)[-1]
                            self.go(t, reach_sb[best_w_sb])
                            if t.id not in s.units:
                                return
                            self.refresh()
                            danger = self.land_danger()
                            my_guards, en_land, _ = self.isle_status(isle)
                            en_caps_isle = [e for e in en_land if self.types[e.type].capture]
                aboard = sorted(s.passengers(t), key=lambda u: (self.types[u.type].carry, self.types[u.type].skirmish, u.id))
                claims = {}
                recomputed_wdmg = False
                guards_ready = len(my_guards) + sum(1 for p in aboard if not self.types[p.type].carry and not self.types[p.type].skirmish and p.mp > 0)
                sleeping_guards_aboard = [
                    p for p in aboard
                    if not self.types[p.type].carry and not self.types[p.type].skirmish and p.mp <= 0
                ]
                en_t_near_mid = any(
                    self.types[e.type].transport and any(bb.distance(e.hex, x.hex) <= 7 for x in sites)
                    for e in self.enemies.values()
                ) or any(
                    s.round - tr <= 2 and any(bb.distance(th, x.hex) <= 7 for x in sites)
                    for _, (th, _, tr, _, _, _) in self.en_trans_mem.items()
                )
                hold_wagon_for_escort = (
                    guards_ready == 0
                    and bool(sleeping_guards_aboard)
                    and (known_en > 0 or bool(en_land) or en_t_near_mid)
                )
                for p in aboard:
                    if p.mp <= 0:
                        continue
                    if self.types[p.type].carry:
                        if hold_wagon_for_escort:
                            continue
                        # Recompute w_dmg AFTER our guards just landed so their ZOC protects our wagon!
                        if not recomputed_wdmg:
                            self.refresh()
                            w_dmg = self.wagon_incoming_damage()
                            recomputed_wdmg = True
                        self._land_wagon_on_isle(p, sites, claims, danger, w_dmg)
                    else:
                        reach = self.reachable(p)
                        opts = [h for h in reach if self.land.get(h) == isle]
                        if opts:
                            # Scouts avoid landing adjacent to enemy melee (stay safe to snipe wagons next turn!)
                            if self.types[p.type].skirmish and not self.types[p.type].capture:
                                best_h = max(
                                    opts,
                                    key=lambda h: (
                                        -4.0 * danger.get(h, 0.0)
                                        + self.defense.get(h, 0) / 20.0
                                        - min(self.site_field[x.id].get(h, 99) for x in sites) * 0.5,
                                    ),
                                )
                            elif en_caps_isle:
                                # When enemy Infantry are already ashore on middle (P2!), NEVER unload onto
                                # exposed 0% plains inside their 1-turn kill zone unless we can strike THIS turn!
                                best_h = max(
                                    opts,
                                    key=lambda h: (
                                        (12.0 if (guards_ready >= 2 and any(bb.distance(h, e.hex) == 1 for e in en_caps_isle)) else 0.0)
                                        - (25.0 if (
                                            danger.get(h, 0.0) >= 1.5
                                            and self.defense.get(h, 0) < 25
                                            and not (guards_ready >= 2 and any(bb.distance(h, e.hex) == 1 for e in en_caps_isle))
                                        ) else 0.0)
                                        - 4.5 * danger.get(h, 0.0) * (1.0 - self.defense.get(h, 0) / 100.0)
                                        + self.defense.get(h, 0) / 5.0
                                        - min(self.site_field[x.id].get(h, 99) for x in sites) * 0.45
                                        - (0.8 if h in self.site_at else 0.0),
                                    ),
                                )
                            else:
                                # Strongly prefer >=25% Forest/Hill landing hexes so arriving enemy transports
                                # (Armada ONSIGHT / Breakwater!) can NEVER amphibious 2-shot kill our Infantry on 0% plains!
                                best_h = max(
                                    opts,
                                    key=lambda h: (
                                        self.defense.get(h, 0) / 6.0
                                        - min(self.site_field[x.id].get(h, 99) for x in sites) * 0.85
                                        - (1.2 if h in self.site_at else 0.0),
                                    ),
                                )
                            self.go(p, reach[best_h])
                left = s.passengers(t)
                # CRITICAL: If passengers (especially combat guards!) have mp == 0 (just boarded this turn!),
                # STAY in "out" so they (and any escorted wagon) unload together next turn!
                if left and (all(p.mp <= 0 for p in left) or any(not self.types[p.type].carry and p.mp <= 0 for p in left)) and f["waited"] < 2:
                    f["waited"] += 1
                else:
                    f["phase"], f["waited"] = "wait", 0

        if f["phase"] == "wait" and t.id in s.units:
            f["waited"] += 1
            claims = {}
            w_dmg = self.wagon_incoming_damage()
            en_caps_isle_w = [e for e in en_land if self.types[e.type].capture]
            for p in list(s.passengers(t)):
                if p.mp > 0:
                    if self.types[p.type].carry:
                        self._land_wagon_on_isle(p, sites, claims, danger, w_dmg)
                    else:
                        reach = self.reachable(p)
                        opts = [h for h in reach if self.land.get(h) == isle]
                        if opts:
                            if en_caps_isle_w and not self.types[p.type].skirmish:
                                best_h = max(
                                    opts,
                                    key=lambda h: (
                                        (12.0 if any(bb.distance(h, e.hex) == 1 for e in en_caps_isle_w) else 0.0)
                                        - (25.0 if (
                                            danger.get(h, 0.0) >= 1.5
                                            and self.defense.get(h, 0) < 25
                                            and not any(bb.distance(h, e.hex) == 1 for e in en_caps_isle_w)
                                        ) else 0.0)
                                        - 4.5 * danger.get(h, 0.0) * (1.0 - self.defense.get(h, 0) / 100.0)
                                        + self.defense.get(h, 0) / 5.0
                                        - min(self.site_field[x.id].get(h, 99) for x in sites) * 0.45,
                                    ),
                                )
                            else:
                                best_h = max(
                                    opts,
                                    key=lambda h: (
                                        self.defense.get(h, 0) / 6.0
                                        - min(self.site_field[x.id].get(h, 99) for x in sites) * 0.85,
                                    ),
                                )
                            self.go(p, reach[best_h])

            port = s.cities[f["port"]] if f["port"] is not None else None
            g_cost = self.types[self.galley_code].cost if self.galley_code else 200
            need_first_galley = (not self.ever_built_galley) and port is not None
            my_guards, en_land, _ = self.isle_status(isle)
            enemy_on_isle = len(en_land) > 0
            enemy_outnumbers = len(en_land) > len(my_guards)

            # Emergency Home-Raid Evacuation from Middle Island (stops Armada r5-12 2-I Home-Port Raid!):
            # If our Home Island is under active raid/invasion with <= 1 capturer left at home while no enemy
            # capturers are ashore on middle (ignore lone Scout decoys!), board our Infantry and rush straight home!
            en_caps_on_isle = any(self.types[e.type].capture for e in en_land)
            evacuated_home = False
            if not en_caps_on_isle and my_guards and self.my_port_cid is not None:
                enemy_landed_w = any(
                    not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
                    for e in self.enemies.values()
                )
                home_crisis_w = (
                    self.home_invasion_active()
                    or enemy_landed_w
                    or any(
                        self.is_home_raid_transport(e)
                        for e in self.enemies.values()
                    )
                )
                home_caps_w = sum(
                    1 for u in s.my_units()
                    if not u.carrier and self.land.get(u.hex) == self.home_land and self.types[u.type].capture
                )
                if home_crisis_w and home_caps_w <= (1 if enemy_landed_w else 0):
                    # Evacuate capturers (I1 + I2) home while keeping Scout on middle for vision so our
                    # Instant Raid-Abort Counter-Strike immediately spots Armada landing and whips T1 right back!
                    evac_caps = sorted(
                        (u for u in my_guards if self.types[u.type].capture and not u.fresh and not u.acted and u.mp > 0),
                        key=lambda u: (bb.distance(u.hex, t.hex), u.id),
                    )
                    boarded_any = False
                    for u in evac_caps:
                        if len(s.passengers(t)) >= self.passengers_max:
                            break
                        if self.board_transport(u, t):
                            boarded_any = True
                        else:
                            adj = [n for n in bb.neighbors(t.hex) if n in self.defense and self.land.get(n) == isle]
                            if adj:
                                self.move_unit_toward(u, self.distance_field(adj), danger, caution=1.0)
                    if boarded_any or any(not self.types[p.type].carry for p in s.passengers(t)):
                        f["phase"], f["waited"] = "back", 0
                        evacuated_home = True

            # Compute seaborne ambush threat to middle wheat sites from CURRENTLY VISIBLE loaded enemy
            # Transports approaching middle (sd <= 5 and not at our Home Port): prevents W#1 from walking
            # onto an exposed beach-adjacent site and getting 1-shot after load() sets mp=0 (WITHOUT
            # panicking W#1 back onto T1!).
            seaborne_site_dmg = {}
            my_ph_sb = s.cities[self.my_port_cid].hex if self.my_port_cid is not None else None
            for e_sb in self.enemies.values():
                if e_sb.carrier or not self.types[e_sb.type].transport:
                    continue
                if my_ph_sb is not None and bb.distance(e_sb.hex, my_ph_sb) < 3:
                    continue
                pax_sb = [
                    p for p in s.passengers(e_sb)
                    if self.rules.damage.get(p.type, {}).get(self.wagon_code, 0) > 0
                ]
                if len(pax_sb) < 2:
                    continue
                sf_sb = self.sea_field([e_sb.hex])
                beaches_sb = {
                    b for sh, sd in sf_sb.items() if sd <= 5
                    for b in bb.neighbors(sh)
                    if self.land.get(b) == isle and self.occ.get(b, 0) == 0
                }
                for b in beaches_sb:
                    for xh in bb.ring(b, 1):
                        if self.land.get(xh) == isle:
                            seaborne_site_dmg[xh] = max(
                                seaborne_site_dmg.get(xh, 0),
                                sum(self.damage(p.type, p.hp, self.wagon_code, xh) for p in pax_sb),
                            )

            for w in my_fws:
                if w.id not in s.units or w.carrier or self.land.get(w.hex) != isle or w.fresh:
                    continue
                room = self.cargo_max - w.wheat - w.bread
                avail_sites = [
                    x for x in sites
                    if x.hex not in self.enemy_hex and x.wheat - claims.get(x.id, 0) >= 80
                    and w_dmg.get(x.hex, 0) < w.hp
                    and seaborne_site_dmg.get(x.hex, 0) < w.hp
                ]
                # Fast first-return: once wagon has >= 250 wheat before G1 is built — OR before G2 is built when
                # Port needs bread for G2 and no big >= 150 wheat pile remains (or enemy has live galleys!) —
                # return immediately to port so we launch 2 galleys ahead of rivals!
                have_w_total = sum(1 for u in s.my_units() if u.type == self.wagon_code)
                have_g_now = sum(1 for u in s.my_units() if u.type == self.galley_code) if self.galley_code else 2
                en_g_live_f = any(
                    s.round - r <= 14 and eid not in self.killed_galley_ids
                    for eid, r in self.enemy_galleys_seen.items()
                )
                need_g2_fast = (
                    have_g_now < 2
                    and port is not None
                    and port.bread < g_cost
                    and w.wheat >= (550 if (have_w_total >= 2 and not en_g_live_f and any(x.wheat - claims.get(x.id, 0) >= 180 for x in avail_sites)) else 250)
                    and port.bread + w.wheat >= g_cost + 50
                    and (en_g_live_f or not any(x.wheat - claims.get(x.id, 0) >= 150 for x in avail_sites))
                )
                # When W2 is already harvesting home wheat to fund r7 G1, top up BOTH middle wheat sites
                # (>= 600 wheat -> funds G2+G3+T2 in one trip and denies Site 2 to Breakwater/Armada!)
                # unless an enemy Galley is already live or our wagon is threatened!
                first_wheat_target = 600 if (have_w_total >= 2 and not en_g_live_f and any(x.wheat - claims.get(x.id, 0) >= 180 for x in avail_sites)) else 250
                fast_galley_ready = (
                    (need_first_galley and w.wheat >= first_wheat_target and (port.bread + w.wheat >= g_cost + 60 or enemy_on_isle))
                    or need_g2_fast
                    or (enemy_on_isle and w.wheat >= 600)
                )
                # Evacuate wagon onto transport immediately if in 1-turn lethal danger!
                # NEVER ping-pong an EMPTY wagon back onto T1 when we are NOT outnumbered and w's hex is non-lethal
                # (staging ashore beside the site saves 2-3 harvest turns once our Infantry clear the enemy Scout/Wagon!).
                wagon_in_danger = (
                    w_dmg.get(w.hex, 0) >= w.hp
                    or (w_dmg.get(w.hex, 0) >= w.hp * 0.65 and w.wheat >= 100)
                    or (enemy_outnumbers and (w.wheat > 0 or not avail_sites))
                    or (enemy_on_isle and w.wheat > 0 and not avail_sites)
                )
                should_board = (
                    fast_galley_ready
                    or wagon_in_danger
                    or room < 150
                    or (w.wheat >= (100 if enemy_on_isle else 250) and (not avail_sites or (f["waited"] >= 5 and (w.wheat >= 450 or enemy_on_isle))))
                )
                if not should_board and avail_sites:
                    site = max(
                        avail_sites,
                        key=lambda x: (
                            (x.wheat - claims.get(x.id, 0)) - 220 * danger.get(x.hex, 0.0) - 3 * w_dmg.get(x.hex, 0),
                            -bb.distance(w.hex, x.hex),
                            -x.id,
                        ),
                    )
                    claims[site.id] = claims.get(site.id, 0) + room
                    if w.hex != site.hex:
                        # Only move onto hexes that won't get our wagon 1-turn killed!
                        reach = self.reachable(w)
                        reach[w.hex] = ""
                        safe_opts = [
                            h for h in reach
                            if h in self.site_field[site.id] and w_dmg.get(h, 0) < w.hp
                        ]
                        if safe_opts:
                            best_h = min(
                                safe_opts,
                                key=lambda h: (
                                    self.site_field[site.id][h] + 0.04 * w_dmg.get(h, 0),
                                    len(reach[h]),
                                    h,
                                ),
                            )
                            if reach[best_h]:
                                self.go(w, reach[best_h])
                        else:
                            if not self.board_transport(w, t):
                                adj = [n for n in bb.neighbors(t.hex) if n in self.defense and self.land.get(n) == isle]
                                if adj:
                                    self.move_unit_toward(w, self.distance_field(adj), danger, caution=2.0)
                            continue
                    if w.hex == site.hex and site.wheat > 0 and (self.cargo_max - w.wheat - w.bread) > 0:
                        self.load(w)
                elif not should_board and not avail_sites and w.wheat == 0 and sites:
                    # Stay ashore safely near the richest site (don't waste 2-3 turns ping-ponging into T1!)
                    target_s = max(sites, key=lambda x: (x.wheat, -bb.distance(w.hex, x.hex), -x.id))
                    reach = self.reachable(w)
                    reach[w.hex] = ""
                    safe_stage = [
                        h for h in reach
                        if self.land.get(h) == isle and h in self.site_field[target_s.id]
                        and w_dmg.get(h, 0) < w.hp and seaborne_site_dmg.get(h, 0) < w.hp
                    ] or [
                        h for h in reach
                        if self.land.get(h) == isle and h in self.site_field[target_s.id]
                        and w_dmg.get(h, 0) < w.hp
                    ]
                    if safe_stage:
                        best_st = min(
                            safe_stage,
                            key=lambda h: (
                                self.site_field[target_s.id][h] + 0.05 * w_dmg.get(h, 0),
                                -self.defense.get(h, 0),
                                len(reach[h]),
                                h,
                            ),
                        )
                        if reach[best_st]:
                            self.go(w, reach[best_st])
                else:
                    if not self.board_transport(w, t):
                        adj = [n for n in bb.neighbors(t.hex) if n in self.defense and self.land.get(n) == isle]
                        if adj:
                            self.move_unit_toward(w, self.distance_field(adj), danger, caution=2.0)

            ashore = [w for w in my_fws if w.id in s.units and not w.carrier and self.land.get(w.hex) == isle]
            aboard_now = s.passengers(t)
            aboard_has_wheat = any(self.types[p.type].carry and p.wheat > 0 for p in aboard_now)
            aboard_empty_wagon = bool(aboard_now) and all(self.types[p.type].carry and p.wheat == 0 for p in aboard_now)
            # NEVER sail home empty leaving a live wagon stranded on the middle island!
            # ONLY when middle has NO visible enemies (tight-beach stack-lock like Seed 15!), if our wagon
            # stayed inside T with 0 wheat because the beach hex is full (occ == limit), shift T to an OPEN beach!
            if not evacuated_home and not ashore and aboard_empty_wagon and not enemy_on_isle and len(my_guards) > 0 and not t.fresh and not t.acted and t.mp > 0:
                sea_w = self.sea.get(t.hex)
                galley_d_w, shore_d_w = self.naval_danger()
                open_berths = [
                    h for h in self.isle_landings(isle)
                    if h != t.hex and self.sea.get(h) == sea_w and h not in galley_d_w and h not in shore_d_w
                    and any(
                        self.land.get(n) == isle and n not in self.enemy_hex
                        and self.occ.get(n, 0) < self.limit and w_dmg.get(n, 0) < 100
                        for n in bb.neighbors(h)
                    )
                ]
                if open_berths:
                    self.safe_sail(t, self.sea_field(open_berths), avoid=shore_d_w, hard_avoid=galley_d_w)
                    if t.id in s.units and t.hex in open_berths:
                        for p in list(s.passengers(t)):
                            if self.types[p.type].carry and p.mp > 0:
                                self._land_wagon_on_isle(p, sites, claims, danger, w_dmg)
                        ashore = [w for w in my_fws if w.id in s.units and not w.carrier and self.land.get(w.hex) == isle]
                        aboard_now = s.passengers(t)
                        aboard_has_wheat = any(self.types[p.type].carry and p.wheat > 0 for p in aboard_now)
                        aboard_empty_wagon = bool(aboard_now) and all(self.types[p.type].carry and p.wheat == 0 for p in aboard_now)
            # Evasive pickup protection (even at 100 HP!): if an enemy Galley threatens our waiting ferry
            # while our wagon is still ashore harvesting on middle, immediately reposition T1 to a safe
            # middle-coast berth out of galley_d (within 1-turn wagon boarding reach when possible) so no
            # enemy Galley can ever get free hits on our waiting Transport!
            if not evacuated_home and ashore and not t.fresh and not t.acted and t.mp > 0:
                galley_d_w2, shore_d_w2 = self.naval_danger()
                if t.hex in galley_d_w2 or t.hex in shore_d_w2:
                    sea_w2 = self.sea.get(t.hex)
                    reach_tw2 = self.reachable(t)
                    reach_tw2[t.hex] = ""
                    w_ref = ashore[0]
                    w_stride = max(1, self.types[w_ref.type].move)
                    my_pw_w2 = self.port_water[self.my_port_cid] if self.my_port_cid is not None else []
                    my_f_w2 = self.sea_field(my_pw_w2) if my_pw_w2 else {}
                    safe_berths = [
                        h for h in self.isle_landings(isle)
                        if h in reach_tw2 and self.sea.get(h) == sea_w2
                        and h not in galley_d_w2 and h not in shore_d_w2
                    ]
                    if safe_berths:
                        best_sb = min(
                            safe_berths,
                            key=lambda h: (
                                0 if bb.distance(h, w_ref.hex) <= w_stride else 1,
                                my_f_w2.get(h, 99),
                                bb.distance(h, w_ref.hex),
                                h,
                            ),
                        )
                        if reach_tw2[best_sb]:
                            self.go(t, reach_tw2[best_sb])
                    else:
                        safe_sea = [h for h in reach_tw2 if h not in galley_d_w2 and h not in shore_d_w2]
                        if safe_sea:
                            best_ss = min(safe_sea, key=lambda h: (bb.distance(h, w_ref.hex), my_f_w2.get(h, 99), h))
                            if reach_tw2[best_ss]:
                                self.go(t, reach_tw2[best_ss])

            if not evacuated_home:
                if not ashore:
                    if aboard_has_wheat or not aboard_empty_wagon:
                        f["phase"], f["waited"] = "back", 0
                    elif f["waited"] >= (6 if (my_guards and t.hex not in galley_d_em) else 2):
                        f["aborted_trip"] = s.round
                        f["phase"], f["waited"] = "back", 0
                elif aboard_has_wheat and f["waited"] >= 8:
                    f["phase"], f["waited"] = "back", 0

        if f["phase"] == "back" and t.id in s.units:
            # Safe-Beach Wheat Drop: when returning with loaded wheat while enemy Galleys threaten our
            # Port or sea lane, IMMEDIATELY land our loaded wagon onto ANY reachable safe home-island beach
            # (closest to Port!) so it walks overland with 700-1000 wheat even under enemy blockade!
            loaded_aboard = [p for p in s.passengers(t) if self.types[p.type].carry and p.wheat > 0]
            combat_aboard_bk = [p for p in s.passengers(t) if not self.types[p.type].carry]
            # Empty-Ferry Ration-Drain Shield: NEVER dock an empty T1 (+ empty wagon) directly on Port water
            # overnight when Port is at 110..199 bread saving for G1/G2 — it burns 105 bread of rations and
            # kills our Galley build for 6+ turns! Hold 2 hexes off Port until G1/G2 spawns!
            if (
                not loaded_aboard
                and not combat_aboard_bk
                and not self.home_invasion_active()
                and self.galley_code is not None
                and self.my_port_cid is not None
                and s.cities[self.my_port_cid].owner == self.player
                and t.rations >= 25
            ):
                my_port_sh = s.cities[self.my_port_cid]
                g_cost_sh = self.types[self.galley_code].cost
                have_g_sh = sum(1 for u in s.my_units() if u.type == self.galley_code)
                empty_fw_waiting = any(
                    not w.carrier and w.wheat == 0 and self.land.get(w.hex) == self.home_land
                    for w in my_fws
                )
                need_standoff = (
                    (have_g_sh == 0 and 10 <= my_port_sh.bread < g_cost_sh)
                    or (have_g_sh == 1 and not empty_fw_waiting and 40 <= my_port_sh.bread < g_cost_sh)
                )
                if need_standoff:
                    galley_d_sh, shore_d_sh = self.naval_danger()
                    sea_sh = self.sea.get(t.hex)
                    standoff = [
                        h for h in self.sea_hexes
                        if self.sea.get(h) == sea_sh
                        and bb.distance(h, my_port_sh.hex) == 2
                        and h not in galley_d_sh and h not in shore_d_sh
                    ]
                    if standoff and t.hex not in standoff:
                        self.safe_sail(t, self.sea_field(standoff), avoid=shore_d_sh, hard_avoid=galley_d_sh)
                    return
            if loaded_aboard and self.my_port_cid is not None and self.home_land is not None:
                galley_d_bk, shore_d_bk = self.naval_danger()
                my_port_bk = s.cities[self.my_port_cid]
                my_pw_bk = self.port_water[self.my_port_cid]
                port_threatened = bool(galley_d_bk) and (
                    any(h in galley_d_bk for h in my_pw_bk)
                    or t.hex in galley_d_bk
                    or t.hp <= 85
                )
                if port_threatened and my_port_bk.owner == self.player and not t.fresh and not t.acted and t.mp > 0:
                    reach_bk = self.reachable(t)
                    reach_bk[t.hex] = ""
                    sea_bk = self.sea.get(t.hex)
                    cand_drop = []
                    if any(w.mp > 0 for w in loaded_aboard):
                        for h in reach_bk:
                            if self.sea.get(h) != sea_bk or h in shore_d_bk:
                                continue
                            for b in bb.neighbors(h):
                                if self.land.get(b) != self.home_land or b in self.enemy_hex or self.occ.get(b, 0) >= self.limit:
                                    continue
                                risk_b = 100 if (h in galley_d_bk and t.hp <= 85) else (25 if h in galley_d_bk else 0)
                                sc_b = (risk_b, self.city_field[my_port_bk.id].get(b, 99) - (8 if b == my_port_bk.hex else 0), len(reach_bk[h]), h, b)
                                cand_drop.append(sc_b)
                    if cand_drop:
                        _, _, _, berth_d, beach_d = min(cand_drop)
                        if reach_bk[berth_d]:
                            self.go(t, reach_bk[berth_d])
                        if t.id in s.units and t.hex == berth_d:
                            for w in list(loaded_aboard):
                                if w.id in s.units and w.carrier == t.id and w.mp > 0:
                                    r_w = self.reachable(w)
                                    if beach_d in r_w:
                                        self.go(w, r_w[beach_d])
                                        if w.id in s.units and w.hex == my_port_bk.hex and (w.wheat > 0 or w.bread > 0):
                                            self.unload(w)
                            if not any(self.types[p.type].carry and p.wheat > 0 for p in s.passengers(t)):
                                f["phase"], f["waited"], f["held"] = "dock", 0, 0
                                f["trips"] = f.get("trips", 0) + 1
                                return
            port, there = self.dock_transport(t, f["port"], is_ferry=True)
            if port is None:
                # Our Port was just captured while we have troops/cargo aboard! Land on our Home Island
                # coast CLOSEST to our lost Port / Home City immediately to recapture Port & defend Home!
                if s.passengers(t) and self.home_land is not None:
                    sea_b = self.sea.get(t.hex)
                    h_coast = [
                        h for h in self.coast
                        if self.sea.get(h) == sea_b
                        and any(self.land.get(n) == self.home_land for n in bb.neighbors(h))
                    ]
                    if h_coast:
                        ref_cid = self.my_port_cid if self.my_port_cid is not None else self.my_home_cid
                        if ref_cid is not None:
                            ref_h_bk = s.cities[ref_cid].hex
                            best_d_bk = min(bb.distance(h, ref_h_bk) for h in h_coast)
                            h_coast = [h for h in h_coast if bb.distance(h, ref_h_bk) <= best_d_bk + 1] or h_coast
                        if t.hex not in h_coast:
                            galley_d_b, shore_d_b = self.naval_danger()
                            self.safe_sail(t, self.sea_field(h_coast), avoid=shore_d_b, hard_avoid=galley_d_b)
                        if t.id in s.units and t.hex in h_coast:
                            self._land_anywhere(t)
                return
            f["port"] = port.id
            if not there and s.passengers(t):
                f["held"] = f.get("held", 0) + 1
                if f["held"] >= 2:
                    pw = [h for h in self.port_water[port.id] if self.sea.get(h) == self.sea.get(t.hex)]
                    if pw:
                        self.safe_sail(t, self.sea_field(pw))
                        there = t.id in s.units and t.hex in pw
            if there:
                f["held"] = 0
                for p in list(s.passengers(t)):
                    if self.types[p.type].carry:
                        had_wheat = p.wheat > 0
                        self._land_wagon_in_port(p, port, isle)
                        if not had_wheat and isle_unsafe_for_wagon and not p.carrier:
                            home_l = self.land.get(port.hex)
                            has_other_home_w = any(
                                u.id != p.id and u.type == self.wagon_code and u.id not in self.ferry_wagons
                                and not u.carrier and self.land.get(u.hex) == home_l
                                for u in s.my_units()
                            )
                            if not has_other_home_w:
                                self.ferry_wagons.pop(p.id, None)
                    elif p.mp > 0:
                        reach = self.reachable(p)
                        if reach:
                            h = min(reach, key=lambda x: (x != port.hex, x))
                            self.go(p, reach[h])
                if not s.passengers(t):
                    f["trips"] = f.get("trips", 0) + 1
                    f["phase"], f["waited"] = "dock", 0

    def _jit_unload(self, w, city, w_dmg=None):
        """Just-In-Time partial wheat unload: because wheat inside a Wagon does NOT feed
        overnight ration refills (which burn 5 bread/ration = 150-300 bread/turn from Port!),
        unload ONLY the exact bread needed for this turn's ship/unit build when w holds a large
        surplus (>= 100 extra wheat), keeping the rest safely stored as wheat for tomorrow's G3/G4!"""
        if w.wheat <= 0 or w.hex != city.hex:
            return
        s = self.state
        is_port = bool(self.port_seas[city.id])
        # If city is genuinely threatened (>=45 incoming dmg — NOT a 28-dmg Scout poke or distant raid alert!)
        # or nearby units are genuinely low on rations (< 22), unload everything immediately.
        w_in_danger = (w_dmg is not None and w_dmg.get(city.hex, 0) >= 45)
        any_hungry = any(
            not u.carrier and u.rations < 22 and bb.distance(u.hex, city.hex) <= 1
            for u in s.my_units()
        )
        # Only ration-shield during r7..26 when building our core 2-4 Galley navy (never on a galley-blockaded
        # port where we need full bread immediately to pump coastal Archers!).
        if w_in_danger or any_hungry or city.built or (is_port and self.galley_blockaded(city)) or s.round < 7 or s.round > 26:
            self.unload(w, w.wheat)
            return
        if is_port and self.launch_hex(city) is None and not self.galley_blockaded(city) and s.round <= 15:
            # A passing enemy Transport is temporarily sitting on our Port water for 1 turn —
            # hold wheat safely inside the Wagon until next turn so overnight rations don't burn 100 bread!
            return
        if is_port:
            have_g_j = sum(1 for u in s.my_units() if u.type == self.galley_code) if self.galley_code else 4
            en_g_j = sum(1 for eid, r in self.enemy_galleys_seen.items() if s.round - r <= 14 and eid not in self.killed_galley_ids)
            # Only JIT-split when we actually want another ship/galley next turn!
            if have_g_j >= (4 if en_g_j >= 2 else 2):
                self.unload(w, w.wheat)
                return
            g_cost_j = self.types[self.galley_code].cost if self.galley_code else 200
            # Match build_all()'s exact bread threshold (0 keep during r<=20 or naval arms race!)
            extra_keep = 0 if (s.round <= 20 or en_g_j >= 1) else 50
            target_b = g_cost_j + extra_keep
        else:
            # Inland Home City: fund 1 Archer/Infantry (120 + 30 keep = 150) without burning leftover 180 bread!
            a_cost_j = self.types[self.archer_code].cost if self.archer_code else 120
            target_b = max(self.types[self.inf_code].cost, a_cost_j) + 30
        need = max(0, target_b - city.bread)
        if need == 0 and (not is_port or self.launch_hex(city) is not None):
            # City ALREADY has enough bread for this turn's single build (cities can only build 1 unit/turn!).
            # Keep our wheat safely inside the Wagon until next turn so overnight rations don't burn 180 bread!
            return
        if is_port and city.bread + w.wheat < target_b and s.round <= 11:
            have_t_j = any(u.type == self.transport_code for u in s.my_units()) if self.transport_code else True
            t_cost_j = self.types[self.transport_code].cost if self.transport_code else 150
            saving_for_g2_j = (
                (not have_t_j)
                and self.ever_built_galley
                and self.port_sea_dist <= 21
                and bool(self.resource_isles())
                and have_g_j < 2
                and (
                    self.saw_home_raid
                    or bool(self.en_assault_t_ids)
                    or self.home_invasion_active()
                    or en_g_j > have_g_j
                )
            )
            tot_b = city.bread + w.wheat
            if saving_for_g2_j and (tot_b >= target_b - 25 or tot_b < t_cost_j or en_g_j > have_g_j):
                # Port is saving for G2 (200) and either hits 200 within 1-2 turns, cannot even afford T1 yet,
                # or faces a live enemy Galley — hold wheat safely inside Wagon so overnight rations don't vaporize it!
                # (If 150 <= tot_b < 175 with 0 live enemy Galleys, unload now so build_all() launches T1 immediately!)
                return
        if need > 0 and w.wheat >= need + 100:
            self.unload(w, need)
        else:
            self.unload(w, w.wheat)

    def _load_guard_bread(self, w, port, isle):
        need_b = self._ferry_guard_bread_need(w, isle) - w.bread
        # NEVER steal bread needed to build G2/G3/G4 at our Port before build_all() runs!
        have_g_lg = sum(1 for u in self.state.my_units() if u.type == self.galley_code) if self.galley_code else 4
        en_g_lg = sum(
            1 for eid, r in self.enemy_galleys_seen.items()
            if self.state.round - r <= 18 and eid not in self.killed_galley_ids
        )
        need_ship_res = self.galley_code and (have_g_lg < 2 or (en_g_lg >= 1 and have_g_lg < 4))
        reserve = (self.types[self.galley_code].cost + 10) if need_ship_res else 80
        avail = min(need_b, port.bread - reserve, self.cargo_max - w.wheat - w.bread)
        if avail >= 50:
            self.load(w, avail)

    def _land_wagon_in_port(self, w, port, isle):
        if w.fresh or w.mp <= 0:
            return
        reach = self.reachable(w)
        if port.hex not in reach:
            self.make_room(port)
            reach = self.reachable(w)
        if port.hex in reach:
            self.go(w, reach[port.hex])
        elif reach:
            h = min(reach, key=lambda x: (self.city_field[port.id].get(x, 1 << 30), x))
            self.go(w, reach[h])
        if not w.carrier and w.hex == port.hex:
            if w.wheat > 0:
                self._jit_unload(w, port, self.wagon_incoming_damage())
            if w.wheat == 0:
                self._load_guard_bread(w, port, isle)

    def _land_wagon_on_isle(self, w, sites, claims, danger, w_dmg=None):
        if w_dmg is None:
            w_dmg = self.wagon_incoming_damage()
        avail = [x for x in sites if x.hex not in self.enemy_hex and w_dmg.get(x.hex, 0) < w.hp]
        if not avail:
            return
        site = max(
            avail,
            key=lambda x: (x.wheat - claims.get(x.id, 0) - 200 * danger.get(x.hex, 0.0) - 3 * w_dmg.get(x.hex, 0), -x.id),
        )
        reach = self.reachable(w)
        # NEVER unload a wagon onto a hex where enemy troops can 1-turn kill it!
        opts = [
            h for h in reach
            if h in self.site_field[site.id] and w_dmg.get(h, 0) < w.hp
        ]
        if not opts:
            return
        claims[site.id] = claims.get(site.id, 0) + (self.cargo_max - w.wheat - w.bread)
        h = min(opts, key=lambda x: (self.site_field[site.id][x] + 3 * danger.get(x, 0.0) + 0.04 * w_dmg.get(x, 0), x))
        self.go(w, reach[h])
        if not w.carrier and w.hex == site.hex and site.wheat > 0 and (self.cargo_max - w.wheat - w.bread) > 0:
            self.load(w)

    # ------------------------------------------------------------------
    # Assault Transports: Synchronized Amphibious Waves
    # ------------------------------------------------------------------

    def pick_invasion_target(self, sea, home_l):
        s = self.state
        for isle in sorted(self.resource_isles()):
            my_g, en_l, _ = self.isle_status(isle)
            if en_l and len(my_g) <= len(en_l) + 1:
                spots = [w for w in self.isle_landings(isle) if self.sea.get(w) == sea]
                if spots:
                    return ("isle", isle)

        cands = []
        for c in s.cities:
            if c.owner == self.player or self.land.get(c.hex) == home_l:
                continue
            spots = [w for w in self.city_landings(c.id) if self.sea.get(w) == sea]
            if not spots:
                continue
            pri = 0 if (c.owner != 0 and self.port_seas[c.id]) else (1 if c.owner != 0 else 2)
            cands.append((pri, self.threat.get(c.hex, 0), c.id))
        if cands:
            cands.sort()
            return ("city", cands[0][2])
        return None

    def run_assaults(self, danger):
        s = self.state
        assault_ts = sorted(
            (s.units[tid] for tid in self.assaults if tid in s.units),
            key=lambda u: u.id,
        )
        if not assault_ts:
            return

        enemy_on_home = any(
            not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
            for e in self.enemies.values()
        )
        recent_en_trans = any(s.round - r <= 20 for r in self.enemy_transports_seen.values())
        home_garrison = 99 if enemy_on_home else (2 if recent_en_trans else 1)

        gathering = [t for t in assault_ts if self.assaults[t.id]["phase"] == "gather"]
        if gathering:
            home_fighters = [
                u for u in s.my_units()
                if not u.carrier and self.land.get(u.hex) == self.home_land
                and not self.types[u.type].naval and not self.types[u.type].carry
            ]
            # Keep coastal archers ashore if enemy ships are near our home island!
            en_ships_near = any(
                self.types[e.type].naval and any(
                    bb.distance(e.hex, c.hex) <= 5
                    for c in s.my_cities() if self.land.get(c.hex) == self.home_land
                )
                for e in self.enemies.values()
            )
            unassigned = [
                u for u in home_fighters
                if u.id not in self.crew_of and u.id not in self.hungry_busy
                and not u.fresh and not u.acted and u.mp > 0
                and not (en_ships_near and u.type == self.archer_code)
            ]
            spare = max(0, len(home_fighters) - len([u for u in home_fighters if u.id in self.crew_of]) - home_garrison)
            for t in gathering:
                a = self.assaults[t.id]
                port, there = self.dock_transport(t, a["port"], is_ferry=False)
                if port is None:
                    continue
                a["port"] = port.id
                sea = self.sea.get(t.hex)
                target = self.pick_invasion_target(sea, self.land.get(port.hex))
                if target is None:
                    continue
                a["target"] = target

                aboard = s.passengers(t)
                coming = [uid for uid, tid in self.crew_of.items() if tid == t.id and not s.units[uid].carrier]
                want = self.passengers_max - len(aboard) - len(coming)
                if want > 0 and spare > 0:
                    caps_in = sum(1 for u in aboard if self.types[u.type].capture) + sum(
                        1 for uid in coming if self.types[s.units[uid].type].capture
                    )
                    cands = [u for u in unassigned if u.id not in self.crew_of]
                    cands.sort(key=lambda u: (
                        (0 if self.types[u.type].capture else 1) if caps_in < 2 else 0,
                        self.types[u.type].skirmish,
                        self.city_field[port.id].get(u.hex, 1 << 30),
                        u.id,
                    ))
                    for u in cands[:min(want, spare)]:
                        self.crew_of[u.id] = t.id
                        spare -= 1
                        want -= 1
                        if self.types[u.type].capture:
                            caps_in += 1

        for uid, tid in sorted(list(self.crew_of.items())):
            if self.out_of_time() or uid not in s.units or tid not in s.units:
                continue
            u, t = s.units[uid], s.units[tid]
            if u.carrier or u.fresh or u.acted or u.mp <= 0:
                continue
            if self.board_transport(u, t):
                continue
            adj = [n for n in bb.neighbors(t.hex) if n in self.defense]
            if adj:
                self.move_unit_toward(u, self.distance_field(adj), danger, caution=1.0)

        # Launch logic: coordinate waves if we have 2+ assault transports!
        other_almost_ready = sum(
            1 for t in assault_ts
            if self.assaults[t.id]["phase"] == "gather" and len(s.passengers(t)) >= 2
        )
        for t in assault_ts:
            if self.out_of_time() or t.id not in s.units or t.fresh:
                continue
            a = self.assaults[t.id]
            aboard = s.passengers(t)

            if a["phase"] == "gather":
                coming = [uid for uid, tid in self.crew_of.items() if tid == t.id and not s.units[uid].carrier]
                if aboard:
                    urgent = a.get("target") and a["target"][0] == "isle"
                    # Wait briefly (up to 2 turns) for a full 4-unit boat or partner transport when invading enemy island
                    full = len(aboard) >= self.passengers_max
                    pair_wait = (
                        not urgent and len(assault_ts) >= 2 and other_almost_ready < 2 and a["waited"] < 2
                    )
                    if (
                        (full and not pair_wait)
                        or (urgent and len(aboard) >= 2)
                        or (len(aboard) >= 3 and a["waited"] >= 2)
                        or (len(aboard) >= 2 and not coming and a["waited"] >= 3)
                        or a["waited"] >= 5
                    ):
                        for uid in coming:
                            del self.crew_of[uid]
                        a["phase"], a["waited"] = "sail", 0
                    else:
                        a["waited"] += 1

            if a["phase"] == "sail" and t.id in s.units:
                if not s.passengers(t):
                    a["phase"], a["waited"] = "return", 0
                elif self.supply and t.rations < self.trip_home_rations(t) + 6:
                    self._land_assault(t, a.get("target"))
                    if s.passengers(t):
                        _, there = self.dock_transport(t, a["port"], is_ferry=False)
                        if there:
                            self._land_home_all(t)
                            a["phase"], a["waited"] = "gather", 0
                else:
                    sea = self.sea.get(t.hex)
                    port_land = self.land.get(s.cities[a["port"]].hex) if a["port"] is not None else self.home_land
                    target = a.get("target")
                    if target is None or (target[0] == "city" and s.cities[target[1]].owner == self.player):
                        target = self.pick_invasion_target(sea, port_land)
                        a["target"] = target
                    if target is None:
                        _, there = self.dock_transport(t, a["port"], is_ferry=False)
                        if there:
                            self._land_home_all(t)
                            a["phase"], a["waited"] = "gather", 0
                    else:
                        self._sail_and_land_assault(t, a, target)

            if a["phase"] == "return" and t.id in s.units:
                _, there = self.dock_transport(t, a["port"], is_ferry=False)
                if there:
                    a["phase"], a["waited"] = "gather", 0

    def _sail_and_land_assault(self, t, a, target):
        s = self.state
        sea = self.sea.get(t.hex)
        spots = self.city_landings(target[1]) if target[0] == "city" else self.isle_landings(target[1])
        goals = [w for w in spots if self.sea.get(w) == sea]
        if not goals:
            return
        galley_d, shore_d = self.naval_danger()
        safe_goals = [w for w in goals if w not in shore_d] or goals
        if t.hex not in goals:
            self.safe_sail(t, self.sea_field(safe_goals), avoid=shore_d, hard_avoid=galley_d)
        if t.id in s.units and t.hex in goals:
            self._land_assault(t, target)
            if not s.passengers(t):
                a["phase"], a["waited"] = "return", 0
            else:
                a["waited"] += 1
                if a["waited"] >= 3:
                    self._land_anywhere(t)

    def _land_assault(self, t, target):
        s = self.state
        if target and target[0] == "city":
            field = self.city_field[target[1]]
            target_land = self.land.get(s.cities[target[1]].hex)
        elif target and target[0] == "isle":
            sites = [x for x in s.sites if self.land.get(x.hex) == target[1]]
            field = self.site_field[sites[0].id] if sites else {}
            target_land = target[1]
        else:
            field, target_land = {}, None

        aboard = sorted(s.passengers(t), key=lambda u: (not self.types[u.type].capture, -u.hp, u.id))
        for u in aboard:
            if u.mp <= 0 or self.out_of_time():
                continue
            reach = self.reachable(u)
            opts = [h for h in reach if target_land is None or self.land.get(h) == target_land]
            if not opts:
                continue
            best_h = min(
                opts,
                key=lambda h: (
                    field.get(h, 999),
                    -self.defense.get(h, 0) * 0.04,
                    self.threat.get(h, 0),
                    h,
                ),
            )
            self.go(u, reach[best_h])
            if u.id in s.units and not u.carrier:
                self.crew_of.pop(u.id, None)
                if self.can_capture(u):
                    self.capture(u)

    def _land_anywhere(self, t):
        for u in list(self.state.passengers(t)):
            if u.mp <= 0:
                continue
            reach = self.reachable(u)
            if reach:
                h = max(reach, key=lambda x: (self.defense.get(x, 0), -self.threat.get(x, 0)))
                self.go(u, reach[h])
                if not u.carrier:
                    self.crew_of.pop(u.id, None)
                    if self.can_capture(u):
                        self.capture(u)

    def _land_home_all(self, t):
        for u in list(self.state.passengers(t)):
            if u.mp <= 0:
                continue
            reach = self.reachable(u)
            if reach:
                h = min(reach, key=lambda x: (x not in self.city_at, x))
                self.go(u, reach[h])
                if not u.carrier:
                    self.crew_of.pop(u.id, None)

    # ------------------------------------------------------------------
    # Phase 2: Coordinated Focus-Fire Land Combat (incl. Wagon Snipes & Coastal Archers)
    # ------------------------------------------------------------------

    def land_fight(self, danger):
        s = self.state
        if not self.enemies:
            return
        last_turn = s.round >= self.round_limit
        loss_w = 1.0 if last_turn else 0.80
        expose_w = 0.0 if (last_turn and self.player == 2) else 12.0
        failed = set()

        while not self.out_of_time() and self.enemies:
            ready = [
                u for u in s.my_units()
                if not u.carrier and not u.fresh and not u.acted
                and not self.types[u.type].naval and not self.types[u.type].carry
                and u.id not in failed and u.id not in self.crew_of
                and not (self.types[u.type].no_move_attack and u.moved)
            ]
            if not ready:
                break

            opts_by_enemy = {}
            all_singles = []
            en_g_near_any = any(
                self.types[e.type].naval and not self.types[e.type].transport
                for e in self.enemies.values()
            ) or any(
                s.round - r <= 12 and eid not in self.killed_galley_ids
                for eid, r in self.enemy_galleys_seen.items()
            )
            for u in ready:
                ut = self.types[u.type]
                span = ut.range_max + (0 if ut.no_move_attack else u.mp)
                near = [
                    e for e in self.enemies.values()
                    if not e.carrier and self.rules.damage[u.type].get(e.type, 0) > 0
                    and bb.distance(u.hex, e.hex) <= span
                ]
                if not near:
                    continue
                reach = {u.hex: ""} if ut.no_move_attack else self.reachable(u)
                reach[u.hex] = ""
                for e in near:
                    et = self.types[e.type]
                    spots = []
                    for r in range(ut.range_min, ut.range_max + 1):
                        for h in bb.ring(e.hex, r):
                            if h in reach and h in self.defense:
                                if h != u.hex and self.occ.get(h, 0) >= self.limit:
                                    continue
                                spots.append(h)
                    if not spots:
                        continue
                    spots.sort(
                        key=lambda h: (
                            (65.0 if (ut.range_min > 1 and en_g_near_any and not any(n in self.sea_hexes for n in bb.neighbors(h))) else 0.0)
                            + self.defense[h] + (u.fort * self.fortify_pct if h == u.hex else 0)
                            - 28.0 * danger.get(h, 0.0) * (1.0 - self.defense[h] / 100.0),
                            -len(reach[h]),
                        ),
                        reverse=True,
                    )
                    best_h = spots[0]
                    # NEVER let a home Infantry chase a harmless Scout in land_fight() while an unguarded
                    # friendly home city is directly reachable THIS turn and threatened by an invasion!
                    if (
                        ut.capture
                        and self.land.get(u.hex) == self.home_land
                        and et.skirmish
                        and not et.capture
                        and not et.naval
                        and self.home_invasion_active()
                    ):
                        en_home_caps_lf = [
                            f for f in self.enemies.values()
                            if not f.carrier and not self.types[f.type].naval
                            and self.types[f.type].capture and self.land.get(f.hex) == self.home_land
                        ]
                        need_city_garr = any(
                            self.land.get(c.hex) == self.home_land
                            and c.hex in reach
                            and best_h != c.hex
                            and not any(
                                x.hex == c.hex and not x.carrier and self.types[x.type].capture
                                for x in s.my_units()
                            )
                            and (
                                any(bb.distance(c.hex, f.hex) <= 6 for f in en_home_caps_lf)
                                or (self.home_raid_seen_hex is not None and bb.distance(c.hex, self.home_raid_seen_hex) <= 6)
                            )
                            for c in s.my_cities()
                        )
                        if need_city_garr:
                            continue
                    alt_spots = [(sh, reach[sh]) for sh in spots[:4]]
                    d = bb.distance(best_h, e.hex)
                    is_free = (not et.in_range(d)) or self.rules.damage[e.type].get(u.type, 0) == 0
                    dmg = self.damage(u.type, u.hp, e.type, e.hex, e.fort)
                    opts_by_enemy.setdefault(e.id, []).append((u, best_h, reach[best_h], is_free, dmg, alt_spots))

                    sc = self._land_trade_score(u, best_h, e, e.hp, False, danger, loss_w, expose_w)
                    # NEVER let our Scout move into danger to suicide solo (non-kill!) into 2+ enemy Infantry for ~12 dmg!
                    # (If Scout is ALREADY standing adjacent to a wounded enemy Infantry <= 45 HP, let it finish/cripple!)
                    scout_suicide = (
                        ut.skirmish and not ut.capture and et.capture
                        and dmg < e.hp and danger.get(best_h, 0.0) >= 1.4
                        and not (best_h == u.hex and e.hp <= 45)
                    )
                    # NEVER let Infantry walk onto exposed terrain in range of 2+ enemy Infantry just to
                    # chase a harmless Scout while enemy Infantry are on the same island!
                    scout_bait = (
                        ut.capture and et.skirmish and not et.capture and not et.naval
                        and danger.get(best_h, 0.0) >= 1.4 and self.defense.get(best_h, 0) < 25
                        and any(
                            f.id != e.id and not f.carrier and self.types[f.type].capture
                            and self.land.get(f.hex) == self.land.get(e.hex)
                            for f in self.enemies.values()
                        )
                    )
                    if is_free:
                        # Don't inflate zero-retaliation Wagon pokes when enemy Infantry are right beside them
                        # on the same island (stops both Infantry AND Scout from wasting their turn on an empty
                        # Wagon before our Infantry weaken/kill enemy Infantry!).
                        wagon_distraction = (
                            et.carry and e.wheat < 250
                            and any(
                                f.id != e.id and not f.carrier and not self.types[f.type].naval
                                and not self.types[f.type].carry and not self.types[f.type].skirmish
                                and self.land.get(f.hex) == self.land.get(e.hex)
                                for f in self.enemies.values()
                            )
                        )
                        if not wagon_distraction:
                            sc += 18.0
                    if not scout_suicide and not scout_bait:
                        all_singles.append((sc, u, e, best_h, reach[best_h]))

            if not all_singles:
                break

            best_kill = None
            for eid, cand in opts_by_enemy.items():
                e = self.enemies.get(eid)
                if not e or sum(x[4] for x in cand) < e.hp:
                    continue
                free_a = sorted([x for x in cand if x[3]], key=lambda x: -x[4])
                melee_a = sorted([x for x in cand if not x[3]], key=lambda x: (-x[4], -self.defense[x[1]]))

                plan, rem, total_sc = [], e.hp, 0.0
                used_occ = dict(self.occ)
                for u, h, dirs, _, _, alts in free_a:
                    if rem <= 0:
                        break
                    chosen = None
                    for sh, sdirs in alts:
                        if sh == u.hex or used_occ.get(sh, 0) < self.limit:
                            chosen = (sh, sdirs)
                            break
                    if chosen is None:
                        continue
                    sh, sdirs = chosen
                    if sh != u.hex:
                        used_occ[sh] = used_occ.get(sh, 0) + 1
                    total_sc += self._land_trade_score(u, sh, e, rem, True, danger, loss_w, expose_w)
                    rem = max(0, rem - self.damage(u.type, u.hp, e.type, e.hex, e.fort))
                    plan.append((u, sh, sdirs))

                if rem > 0:
                    singles = [x for x in melee_a if x[4] >= rem]
                    if singles:
                        pick = max(
                            singles,
                            key=lambda x: self._land_trade_score(x[0], x[1], e, rem, True, danger, loss_w, expose_w)
                            + 0.25 * max(0, 100 - x[0].hp),
                        )
                        total_sc += self._land_trade_score(pick[0], pick[1], e, rem, True, danger, loss_w, expose_w)
                        rem = 0
                        plan.append((pick[0], pick[1], pick[2]))
                    else:
                        top_m = melee_a[:5]
                        best_pair = None
                        for i in range(len(top_m)):
                            for j in range(len(top_m)):
                                if i == j or top_m[i][4] + top_m[j][4] < rem:
                                    continue
                                u1, h1, d1, _, dmg1, alts1 = top_m[i]
                                u2, h2, d2, _, dmg2, alts2 = top_m[j]
                                sh2, sd2 = h2, d2
                                if h1 == h2 and used_occ.get(h1, 0) + (h1 != u1.hex) + (h2 != u2.hex) > self.limit:
                                    alt_ok = next(
                                        ((ax, ad) for ax, ad in alts2 if ax != h1 and (ax == u2.hex or used_occ.get(ax, 0) < self.limit)),
                                        None,
                                    )
                                    if alt_ok is None:
                                        continue
                                    sh2, sd2 = alt_ok
                                rem1 = max(0, rem - dmg1)
                                sc1 = self._land_trade_score(u1, h1, e, rem, True, danger, loss_w, expose_w)
                                sc2 = self._land_trade_score(u2, sh2, e, rem1, True, danger, loss_w, expose_w)
                                if best_pair is None or sc1 + sc2 > best_pair[0]:
                                    best_pair = (sc1 + sc2, (u1, h1, d1), (u2, sh2, sd2))
                        if best_pair is not None:
                            total_sc += best_pair[0]
                            rem = 0
                            plan.append(best_pair[1])
                            plan.append(best_pair[2])
                        else:
                            # Stack-aware 3+ unit focus-kill (enables I1+I2+S3 = 41+41+19 = 101 KILL on 25% Forest!)
                            for u, h, dirs, _, dmg, alts in melee_a:
                                if rem <= 0:
                                    break
                                chosen = None
                                for sh, sdirs in alts:
                                    if sh == u.hex or used_occ.get(sh, 0) < self.limit:
                                        chosen = (sh, sdirs)
                                        break
                                if chosen is None:
                                    continue
                                sh, sdirs = chosen
                                if sh != u.hex:
                                    used_occ[sh] = used_occ.get(sh, 0) + 1
                                total_sc += self._land_trade_score(u, sh, e, rem, True, danger, loss_w, expose_w)
                                rem = max(0, rem - dmg)
                                plan.append((u, sh, sdirs))

                if rem == 0 and total_sc > 0:
                    et_k = self.types[e.type]
                    # NEVER waste 2+ combat units on a best_kill against a harmless Scout when enemy Infantry/capturers
                    # are standing right next to those same units on the same island!
                    skip_plan = False
                    if et_k.skirmish and not et_k.capture and not et_k.naval:
                        plan_uids = {u.id for u, _, _ in plan}
                        if len(plan) >= 2 and any(
                            sc > 10.0 and u.id in plan_uids and self.types[f.type].capture
                            and self.land.get(f.hex) == self.land.get(e.hex)
                            for sc, u, f, _, _ in all_singles
                        ):
                            skip_plan = True
                        elif any(
                            f.id != e.id and not f.carrier and self.types[f.type].capture
                            and self.land.get(f.hex) == self.land.get(e.hex)
                            for f in self.enemies.values()
                        ) and any(
                            self.types[u.type].capture and danger.get(h, 0.0) >= 1.4 and self.defense.get(h, 0) < 25
                            for u, h, _ in plan
                        ):
                            # Don't take Scout-bait into 2+ enemy Infantry on exposed plains!
                            skip_plan = True
                    # NEVER let heavy combat units waste their turn in best_kill finishing an enemy Wagon
                    # when enemy Infantry are right beside them — UNLESS the wagon is carrying >= 500 wheat,
                    # OR >= 250 wheat while only <= 1 enemy Infantry remains (vaporizes Breakwater's r7 350-750 wheat!)!
                    if not skip_plan and et_k.carry and any(not self.types[u.type].skirmish for u, _, _ in plan):
                        en_heavy_k = sum(
                            1 for f in self.enemies.values()
                            if f.id != e.id and not f.carrier and self.types[f.type].capture
                            and self.land.get(f.hex) == self.land.get(e.hex)
                        )
                        low_value_w = e.wheat < (250 if en_heavy_k <= 1 else 500)
                        if low_value_w:
                            plan_uids = {u.id for u, _, _ in plan}
                            if any(
                                sc > -20.0 and u.id in plan_uids and self.types[f.type].capture
                                and self.land.get(f.hex) == self.land.get(e.hex)
                                for sc, u, f, _, _ in all_singles
                            ):
                                skip_plan = True
                            elif en_heavy_k >= 1 and any(
                                not self.types[u.type].skirmish and danger.get(h, 0.0) >= 1.4 and self.defense.get(h, 0) < 25
                                for u, h, _ in plan
                            ):
                                # Don't suicide Infantry onto exposed 0% plains to kill an empty Wagon in front of 2+ enemy Infantry!
                                skip_plan = True
                    # NEVER let a multi-unit best_kill strip ALL friendly capturers OFF a defended home city
                    # onto exposed (<25% def) terrain while another enemy capturer is right outside that city!
                    if not skip_plan and self.land.get(e.hex) == self.home_land and self.home_invasion_active():
                        for c in s.my_cities():
                            if self.land.get(c.hex) != self.home_land:
                                continue
                            caps_here = [
                                x for x in s.my_units()
                                if not x.carrier and x.hex == c.hex and self.types[x.type].capture
                            ]
                            if not caps_here:
                                continue
                            leaving = [u for u, h, _ in plan if u.hex == c.hex and h != c.hex and self.types[u.type].capture]
                            if len(leaving) >= len(caps_here):
                                foes_recent = list(self.enemies.values()) + [
                                    g for gid, (g, rnd) in self.ghosts.items()
                                    if gid not in self.enemies and s.round - rnd <= 2
                                ]
                                other_en_cap = any(
                                    f.id != e.id and not f.carrier and self.types[f.type].capture
                                    and bb.distance(f.hex, c.hex) <= 3
                                    for f in foes_recent
                                )
                                if other_en_cap:
                                    skip_plan = True
                                    break
                    if not skip_plan:
                        rank = total_sc / (len(plan) ** 0.3)
                        if best_kill is None or rank > best_kill[0]:
                            best_kill = (rank, e, plan)

            if best_kill is not None:
                _, e, plan = best_kill
                for u, h, dirs in plan:
                    if e.id not in s.units or u.id not in s.units or self.out_of_time():
                        break
                    if dirs:
                        if h != u.hex and self.occ.get(h, 0) >= self.limit:
                            failed.add(u.id)
                            break
                        self.go(u, dirs)
                        if u.id not in s.units or u.hex != h:
                            failed.add(u.id)
                            break
                    self.strike_target(u, e)
                continue

            best_s = max(all_singles, key=lambda x: x[0])
            if best_s[0] <= 0:
                break
            _, u, e, h, dirs = best_s
            if dirs:
                self.go(u, dirs)
                if u.id not in s.units or u.hex != h:
                    failed.add(u.id)
                    continue
            self.strike_target(u, e)

    def _land_trade_score(self, u, h, e, e_hp_now, is_kill_plan, danger, loss_w, expose_w):
        ut, et = self.types[u.type], self.types[e.type]
        hit = self.damage(u.type, u.hp, e.type, e.hex, e.fort)
        new_e_hp = max(0, e_hp_now - hit)
        gain = et.cost * (e_hp_now - new_e_hp) / et.max_hp
        pass_val = sum(self.worth(p) for p in self.state.passengers(e))
        if new_e_hp == 0:
            gain += et.cost * 0.65 + pass_val * 1.5
        elif is_kill_plan:
            gain *= 1.25
        # Tactical target priority: NEVER waste Infantry turns killing harmless 20-dmg Scouts
        # while enemy Infantry/capturers are standing right next to us on the same island!
        e_land = self.land.get(e.hex)
        if et.skirmish and not et.capture and not et.naval:
            en_heavy_here = any(
                f.id != e.id and not f.carrier and not self.types[f.type].naval
                and not self.types[f.type].carry and not self.types[f.type].skirmish
                and self.land.get(f.hex) == e_land
                for f in self.enemies.values()
            )
            if en_heavy_here:
                gain *= 0.42
        elif et.capture and not et.naval:
            gain *= 1.35
            if e_land == self.home_land:
                gain += 65 if new_e_hp == 0 else 25
            elif e_land in self.resource_isles():
                gain += (75 if ut.skirmish else 32) if new_e_hp == 0 else 18
        # Killing enemy wagons (especially LOADED wagons or unguarded wagons!) cripples enemy economy,
        # but NEVER throw away a contested middle-island Infantry fight by letting Infantry focus-fire a
        # 0-retaliation Wagon while full-HP enemy Infantry wipe our army!
        if et.carry:
            cargo_val = (e.wheat + e.bread) * 0.4
            en_heavy_cnt = sum(
                1 for f in self.enemies.values()
                if f.id != e.id and not f.carrier and not self.types[f.type].naval
                and not self.types[f.type].carry and not self.types[f.type].skirmish
                and self.land.get(f.hex) == e_land
            )
            if en_heavy_cnt > 0:
                if ut.skirmish:
                    if new_e_hp == 0:
                        gain += 75 + 0.35 * cargo_val
                    elif e.wheat >= 250:
                        gain += 25 + 0.20 * cargo_val
                    else:
                        # Let I1+I2 strike enemy Infantry FIRST so our Scout can finish a 15-25 HP survivor!
                        gain *= 0.45
                elif (e.wheat >= 500 or (e.wheat >= 250 and en_heavy_cnt <= 1)) and (new_e_hp == 0 or is_kill_plan):
                    # Vaporizing a loaded 250-750 wheat middle wagon denies enemy G2+G3+T2!
                    gain += (120 + 0.45 * cargo_val) if new_e_hp == 0 else (40 + 0.25 * cargo_val)
                else:
                    # Heavy combat units MUST kill/weaken enemy Infantry first before cleaning up empty Wagons!
                    gain *= 0.12
            else:
                gain += (220 + cargo_val) if new_e_hp == 0 else (75 + 0.15 * cargo_val)
        elif et.naval:
            gain += 110 if new_e_hp == 0 else 45
        if e.hex in self.city_at:
            c = self.state.cities[self.city_at[e.hex]]
            if et.capture and c.owner == self.player:
                # Top emergency priority: kill enemy Infantry actively capturing our city BEFORE chasing Scouts!
                gain += 420 if new_e_hp == 0 else 260
            elif et.capture and c.owner != e.owner:
                gain += 170 if new_e_hp == 0 else 55
            elif c.owner == self.player and new_e_hp == 0:
                gain += 130
            elif c.owner != self.player and new_e_hp == 0:
                gain += 70

        loss = 0.0
        new_u_hp = u.hp
        d = bb.distance(h, e.hex)
        if new_e_hp > 0 and et.in_range(d) and self.rules.damage[e.type].get(u.type, 0) > 0:
            u_fort = u.fort if h == u.hex else 0
            back = self.damage(e.type, new_e_hp, u.type, h, u_fort)
            new_u_hp = max(0, u.hp - back)
            loss = ut.cost * (u.hp - new_u_hp) / ut.max_hp
            if new_u_hp == 0:
                loss += ut.cost * 0.55

        eff_lw = loss_w if (is_kill_plan or new_e_hp == 0) else max(loss_w, 0.92)
        score = gain - eff_lw * loss

        if h != u.hex and expose_w > 0:
            after = danger.get(h, 0.0) * (1.0 - self.defense[h] / 100.0)
            if new_e_hp == 0:
                after = max(0.0, after - (e.hp / et.max_hp) * (1.0 - self.defense[h] / 100.0))
            before = danger.get(u.hex, 0.0) * (1.0 - self.defense[u.hex] / 100.0)
            cost_scale = max(0.8, ut.cost / 100.0) * max(0.35, new_u_hp / ut.max_hp)
            home_def = (self.land.get(u.hex) == self.home_land and self.home_invasion_active())
            mid_fight = (e_land in self.resource_isles())
            eff_expose = expose_w * (2.0 if (home_def and after >= 1.3 and self.defense[h] < 25) else 1.0)
            if mid_fight and ut.skirmish and et.capture and new_e_hp == 0:
                eff_expose *= 0.5
            score -= eff_expose * cost_scale * (min(3.5, after) - min(3.5, before))
            # NEVER abandon a defended Home City / Home Port during an active home invasion
            # to chase a scout or step onto exposed plains in front of enemy infantry!
            if home_def and u.hex in self.city_at and self.state.cities[self.city_at[u.hex]].owner == self.player:
                other_caps = sum(
                    1 for x in self.state.my_units()
                    if x.id != u.id and not x.carrier and x.hex == u.hex and self.types[x.type].capture
                )
                foes_recent = list(self.enemies.values()) + [
                    g for gid, (g, rnd) in self.ghosts.items()
                    if gid not in self.enemies and self.state.round - rnd <= 2
                ]
                en_caps_near = sum(
                    1 for f in foes_recent
                    if not f.carrier and self.types[f.type].capture and bb.distance(f.hex, u.hex) <= 3
                    and not (f.id == e.id and new_e_hp == 0)
                )
                if other_caps == 0 and en_caps_near > 0:
                    score -= 500.0
                elif after >= 1.4 and self.defense[h] < 25 and not (et.capture and new_e_hp == 0):
                    score -= 65.0
            elif home_def and after >= 1.5 and self.defense[h] < 25 and et.skirmish and not et.capture:
                # Never suicide an Infantry onto exposed 0% plains into enemy infantry just to hit a Scout!
                score -= 75.0
            if home_def and (not is_kill_plan) and new_e_hp > 0 and ut.capture and not ut.naval:
                # NEVER let a single home-defense Infantry suicide solo onto exposed (<25% def) terrain into
                # 2+ enemy Infantry (after >= 1.75) for a NON-KILL scratch! Hold cover/city and kill together!
                if after >= 1.75 and self.defense[h] < 25 and not (
                    e.hex in self.city_at and self.state.cities[self.city_at[e.hex]].owner != e.owner
                ):
                    score -= 120.0
            if mid_fight and (not is_kill_plan) and new_e_hp > 0 and ut.capture and not ut.naval:
                # NEVER walk Infantry onto 0% plains into 2+ counter-strikes (after >= 1.5) to deal a non-kill
                # scratch against an enemy Infantry standing on >= 25% Forest/Hill! Hold cover/fortify and punish
                # them when they step onto plains!
                if self.defense[h] < 20 and self.defense[e.hex] >= 25 and after >= 1.5:
                    score -= 120.0
        elif h == u.hex and ut.capture and not ut.naval and (not is_kill_plan) and new_e_hp > 0:
            # Lone Port/City Garrison Fortify Hold: when our sole capturer holds a +30% Home City/Port
            # against 2+ adjacent/nearby enemy capturers (e.g. Breakwater v10 2I+S blitz!), NEVER throw away
            # fortify (+20% = 50% total def!) and take retaliation on a non-kill scratch — hold & let them crash!
            if (
                self.land.get(u.hex) == self.home_land
                and u.hex in self.city_at
                and self.state.cities[self.city_at[u.hex]].owner == self.player
            ):
                other_caps_ip = sum(
                    1 for x in self.state.my_units()
                    if x.id != u.id and not x.carrier and x.hex == u.hex and self.types[x.type].capture
                )
                en_caps_near_ip = sum(
                    1 for f in self.enemies.values()
                    if not f.carrier and self.types[f.type].capture and bb.distance(f.hex, u.hex) <= 3
                )
                if other_caps_ip == 0 and en_caps_near_ip >= 2:
                    score -= 180.0
        return score

    # ------------------------------------------------------------------
    # Phase 3: Land Advancement, Middle Domination, Coastal Archers, & Wagons
    # ------------------------------------------------------------------

    def land_advance(self, danger):
        s = self.state
        r_isles = self.resource_isles()
        ready = [
            u for u in s.my_units()
            if not u.carrier and not u.fresh and not u.acted and u.mp > 0
            and not self.types[u.type].naval and not self.types[u.type].carry
            and u.id not in self.crew_of and u.id not in self.hungry_busy
        ]

        for u in sorted(ready, key=lambda x: x.id):
            if self.out_of_time() or u.id not in s.units or u.acted or u.mp <= 0:
                continue
            ut = self.types[u.type]
            l = self.land.get(u.hex)

            # A. Middle resource island: snipe enemy wagons, hold 40% hills, deny enemy sites!
            if l in r_isles:
                en_isle = [e for e in self.enemies.values() if not e.carrier and self.land.get(e.hex) == l]
                en_caps_here = [e for e in en_isle if self.types[e.type].capture]
                en_wagons = [e for e in en_isle if self.types[e.type].carry]
                if en_wagons and (ut.skirmish or not en_caps_here):
                    goals = [e.hex for e in en_wagons]
                    if ut.skirmish and en_caps_here:
                        # NEVER walk our Scout solo into 2+ enemy Infantry (danger >= 1.5) chasing an empty wagon!
                        reach_sw = self.reachable(u)
                        wf_sw = self.distance_field(goals)
                        safe_sw = [
                            h for h in reach_sw
                            if self.land.get(h) == l and h in wf_sw and danger.get(h, 0.0) < 1.4
                        ]
                        cur_dw = wf_sw.get(u.hex, 99)
                        better_sw = [h for h in safe_sw if wf_sw[h] < cur_dw]
                        if better_sw:
                            best_sw = min(better_sw, key=lambda h: (wf_sw[h], danger.get(h, 0.0), -self.defense.get(h, 0), h))
                            self.go(u, reach_sw[best_sw])
                    else:
                        self.move_unit_toward(u, self.distance_field(goals), danger, caution=0.5)
                elif en_isle:
                    # Hold >=25% Forest/Hill cover and FORTIFY instead of walking onto 0% plains inside
                    # enemy Infantry's 2-shot kill range (forces Breakwater's march() onto 0% plains!).
                    if (
                        ut.capture
                        and en_caps_here
                        and self.defense.get(u.hex, 0) >= 25
                        and min(bb.distance(u.hex, e.hex) for e in en_caps_here) <= 3
                    ):
                        # Only step closer if there is a >=25% cover hex closer to enemy Infantry!
                        reach_m = self.reachable(u)
                        cur_d = min(bb.distance(u.hex, e.hex) for e in en_caps_here)
                        cover_step = [
                            h for h in reach_m
                            if self.land.get(h) == l
                            and self.defense.get(h, 0) >= 25
                            and min(bb.distance(h, e.hex) for e in en_caps_here) < cur_d
                        ]
                        if cover_step:
                            best_cs = max(
                                cover_step,
                                key=lambda h: (
                                    self.defense.get(h, 0),
                                    -min(bb.distance(h, e.hex) for e in en_caps_here),
                                    -danger.get(h, 0.0),
                                ),
                            )
                            self.go(u, reach_m[best_cs])
                        continue
                    goals = [e.hex for e in en_caps_here] if (ut.capture and en_caps_here) else [e.hex for e in en_isle]
                    self.move_unit_toward(u, self.distance_field(goals), danger, caution=1.15)
                else:
                    sites = r_isles[l]
                    site_hexes = {x.hex for x in sites}
                    # Hold INLAND >=25% Hills/Forests near the sites so wagons load freely while
                    # arriving enemy Transports (Armada ONSIGHT / Breakwater!) can NEVER amphibious 2-shot
                    # kill our Infantry sitting on a 0% plains beach!
                    near_sites = [
                        h for h, hl in self.land.items()
                        if hl == l and h not in site_hexes
                        and min(bb.distance(h, x.hex) for x in sites) <= 2
                    ]
                    guard_hexes = [
                        n for x in sites for n in bb.neighbors(x.hex)
                        if n in self.defense and n not in site_hexes
                    ]
                    inland_cover = [
                        h for h in near_sites
                        if self.defense.get(h, 0) >= 25
                        and not any(n in self.sea_hexes for n in bb.neighbors(h))
                    ]
                    any_cover = [h for h in near_sites if self.defense.get(h, 0) >= 25]
                    inland_any = [
                        h for h in near_sites
                        if not any(n in self.sea_hexes for n in bb.neighbors(h))
                    ]
                    pool = inland_cover or any_cover or inland_any or guard_hexes
                    if ut.skirmish and not ut.capture and near_sites:
                        # Keep our Scout off enemy-reachable landing beaches so an arriving enemy Transport
                        # (Breakwater v10 / Armada!) can NEVER 1-turn amphibious one-shot kill our Scout on
                        # the landing turn while preserving vision 4 & counter-strike!
                        isle_beaches = [
                            h for h, hl in self.land.items()
                            if hl == l and any(n in self.sea_hexes for n in bb.neighbors(h))
                        ]
                        en_t_mid = [
                            e for e in self.enemies.values()
                            if self.types[e.type].transport
                            and sum(1 for p in s.passengers(e) if self.types[p.type].capture) >= 1
                            and any(bb.distance(e.hex, b) <= 7 for b in isle_beaches)
                        ]
                        if en_t_mid:
                            threat_b = {
                                b for e in en_t_mid
                                for sh, sd in self.sea_field([e.hex]).items()
                                if sd <= self.types[e.type].move
                                for b in bb.neighbors(sh)
                                if b in isle_beaches and self.occ.get(b, 0) < self.limit
                            }
                            if threat_b:
                                all_mid_l = [h for h, hl in self.land.items() if hl == l and h not in site_hexes]
                                safe_from_t = [
                                    h for h in all_mid_l
                                    if min(bb.distance(h, b) for b in threat_b) >= 2
                                ]
                                if safe_from_t:
                                    pool = safe_from_t
                                else:
                                    en_p_sc = s.cities[self.enemy_port_cid].hex if self.enemy_port_cid is not None else None
                                    if en_p_sc is not None:
                                        far_t = max(min(bb.distance(h, b) for b in threat_b) for h in all_mid_l)
                                        pool = [h for h in all_mid_l if min(bb.distance(h, b) for b in threat_b) == far_t] or pool
                        if not en_t_mid:
                            deep_sc = [
                                h for h in near_sites
                                if not isle_beaches or min(bb.distance(h, b) for b in isle_beaches) >= 2
                            ]
                            if deep_sc:
                                pool = deep_sc
                            else:
                                # Prefer cover hexes on the FRIENDLY/HOME-PORT side (away from enemy Port's
                                # approach beaches!) so an enemy Transport landing from their side cannot 1-shot Scout!
                                en_p_sc = s.cities[self.enemy_port_cid].hex if self.enemy_port_cid is not None else None
                                my_p_sc = s.cities[self.my_port_cid].hex if self.my_port_cid is not None else None
                                if en_p_sc is not None and my_p_sc is not None:
                                    best_sc_h = max(
                                        pool,
                                        key=lambda h: (
                                            self.defense.get(h, 0) >= 25,
                                            bb.distance(h, en_p_sc) - bb.distance(h, my_p_sc),
                                            self.defense.get(h, 0),
                                        ),
                                    )
                                    pool = [best_sc_h]
                    if pool:
                        top_def = max(self.defense.get(h, 0) for h in pool)
                        best_hills = [h for h in pool if self.defense.get(h, 0) >= max(25, top_def - 15)] or pool
                        if u.hex not in best_hills:
                            self.move_unit_toward(u, self.distance_field(best_hills), danger, caution=1.0)
                        elif u.hex in site_hexes:
                            reach = self.reachable(u)
                            opts = [h for h in reach if h in best_hills]
                            if opts:
                                best_h = max(opts, key=lambda h: self.defense[h])
                                self.go(u, reach[best_h])
                continue

            # B. Coastal Archers actively hunt enemy ships from 2 HEXES INLAND (immune to Galley range-1 bombardment!)
            if ut.range_min > 1 and self.galley_code and self.rules.damage[u.type].get(self.galley_code, 0) > 0:
                near_ships = [
                    e for e in self.enemies.values()
                    if self.types[e.type].naval and bb.distance(u.hex, e.hex) <= 10
                ]
                if near_ships:
                    goals = [
                        h for e in near_ships for h in bb.ring(e.hex, ut.range_max)
                        if h in self.defense and self.land.get(h) == l
                    ]
                    # Strongly prefer inland hexes (not adjacent to water) so enemy Galleys can NEVER bombard our Archer!
                    inland_goals = [h for h in goals if not any(n in self.sea_hexes for n in bb.neighbors(h))]
                    use_goals = inland_goals or goals
                    if use_goals and u.hex not in use_goals:
                        self.move_unit_toward(u, self.distance_field(use_goals), danger, caution=0.7)
                        continue
                # Position our home Archer 2 hexes inland covering our Port water when facing enemy Galleys
                # (Breakwater's Round-8 Galley OR Armada's 2G fleet!) so ships get shredded with 0 retaliation!
                en_g_live_adv = any(
                    s.round - r <= 14 and eid not in self.killed_galley_ids
                    for eid, r in self.enemy_galleys_seen.items()
                )
                if (
                    l == self.home_land
                    and ((self.saw_home_raid and not self.ever_killed_enemy_galley and s.round <= 16) or en_g_live_adv)
                    and self.my_port_cid is not None
                ):
                    my_pw = self.port_water[self.my_port_cid]
                    cover = [
                        h for w in my_pw for h in bb.ring(w, ut.range_max)
                        if h in self.defense and self.land.get(h) == l
                    ]
                    inland_cov = [h for h in cover if not any(n in self.sea_hexes for n in bb.neighbors(h))]
                    use_cov = inland_cov or cover
                    if use_cov and u.hex not in use_cov:
                        port_h = s.cities[self.my_port_cid].hex
                        best_cov = sorted(
                            set(use_cov),
                            key=lambda h: (any(n in self.sea_hexes for n in bb.neighbors(h)), h != port_h, -self.defense.get(h, 0), bb.distance(h, port_h)),
                        )[:2]
                        self.move_unit_toward(u, self.distance_field(best_cov), danger, caution=0.8)
                        continue

            local_foreign = [
                c for c in s.cities
                if c.owner != self.player and self.land.get(c.hex) == l
            ]
            local_enemies = [
                e for e in self.enemies.values()
                if not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == l
            ]

            if l == self.home_land and local_enemies:
                # Defend the targeted home city UNITED (2 Infantry stacked ON target_c for +30% defense
                # so Breakwater v7's 3-Infantry stack can NEVER one-shot kill a lone defender 3v1!).
                local_mine_h = [c for c in s.my_cities() if self.land.get(c.hex) == l]
                en_caps_here = [e for e in local_enemies if self.types[e.type].capture]
                en_ref_list = en_caps_here or local_enemies
                my_home_caps_cnt = sum(
                    1 for x in s.my_units()
                    if not x.carrier and self.land.get(x.hex) == l and self.types[x.type].capture
                )
                target_c = (
                    min(
                        local_mine_h,
                        key=lambda c: (
                            min(bb.distance(c.hex, e.hex) for e in en_ref_list),
                            not bool(self.port_seas[c.id]),
                            c.id,
                        ),
                    )
                    if (local_mine_h and en_ref_list) else None
                )
                want_garr = min(2, my_home_caps_cnt) if (len(en_caps_here) >= 2 or (not en_caps_here and s.round <= 11)) else 1
                if ut.capture and u.hex in self.city_at and s.cities[self.city_at[u.hex]].owner == self.player:
                    caps_here = sum(
                        1 for x in s.my_units()
                        if not x.carrier and x.hex == u.hex and self.types[x.type].capture
                    )
                    en_right_outside = any(
                        bb.distance(e.hex, u.hex) <= 2 for e in en_caps_here
                    )
                    is_main_target = target_c is not None and u.hex == target_c.hex and any(
                        bb.distance(e.hex, u.hex) <= 6 for e in en_ref_list
                    )
                    if (is_main_target and caps_here <= want_garr) or (caps_here <= 1 and en_right_outside):
                        # Hold the +30%-defense city!
                        continue
                if ut.capture and target_c is not None and min(bb.distance(target_c.hex, e.hex) for e in en_ref_list) <= 6:
                    caps_on_tc = sum(
                        1 for x in s.my_units()
                        if not x.carrier and x.hex == target_c.hex and self.types[x.type].capture
                    )
                    if caps_on_tc < want_garr and self.occ.get(target_c.hex, 0) < self.limit:
                        # Sprint straight into target_c if directly reachable this turn; otherwise ALWAYS advance
                        # STRICTLY CLOSER to target_c (never flee backwards!), preferring cover / safe hexes!
                        if u.hex != target_c.hex:
                            reach_tc = self.reachable(u)
                            if target_c.hex in reach_tc:
                                self.go(u, reach_tc[target_c.hex])
                            else:
                                cf_tc = self.city_field[target_c.id]
                                cur_d_tc = cf_tc.get(u.hex, 999)
                                closer = [h for h in reach_tc if cf_tc.get(h, 999) < cur_d_tc]
                                if closer:
                                    # When leaving a +30% city while target_c already has >=1 defender, NEVER walk
                                    # into 0% open ground within 4 hexes of 2+ enemy Infantry (2v1 instant kill even
                                    # if a friendly Scout temporarily stands between them!).
                                    in_safe_city = (
                                        u.hex in self.city_at
                                        and s.cities[self.city_at[u.hex]].owner == self.player
                                        and caps_on_tc >= 1
                                        and len(en_caps_here) >= 2
                                    )
                                    def _exposed_2v1(hh):
                                        if self.defense.get(hh, 0) >= 25 or hh in self.city_at:
                                            return False
                                        if danger.get(hh, 0.0) >= 1.5:
                                            return True
                                        return sum(1 for e in en_caps_here if bb.distance(hh, e.hex) <= 4) >= 2
                                    if in_safe_city:
                                        safe_cl = [h for h in closer if not _exposed_2v1(h)]
                                        if not safe_cl:
                                            continue
                                        closer = safe_cl
                                    best_cl = min(
                                        closer,
                                        key=lambda h: (
                                            1 if (len(en_caps_here) >= 2 and _exposed_2v1(h)) else 0,
                                            cf_tc[h] + (0.45 * danger.get(h, 0.0) if self.defense.get(h, 0) < 25 else 0.0),
                                            -self.defense.get(h, 0),
                                            len(reach_tc[h]),
                                            h,
                                        ),
                                    )
                                    self.go(u, reach_tc[best_cl])
                                else:
                                    self.move_unit_toward(u, cf_tc, danger, caution=0.0, prefer_city=target_c.hex)
                        if self.can_capture(u):
                            self.capture(u)
                        continue
                # Any other city with an enemy capturer right next to it (<= 3 hexes) and 0 defenders:
                other_threat = [
                    c for c in local_mine_h
                    if c != target_c
                    and any(bb.distance(c.hex, e.hex) <= 3 for e in en_caps_here)
                    and sum(1 for x in s.my_units() if x.hex == c.hex and not x.carrier and self.types[x.type].capture) == 0
                ]
                if ut.capture and other_threat:
                    tc2 = other_threat[0]
                    if u.hex != tc2.hex:
                        self.move_unit_toward(u, self.city_field[tc2.id], danger, caution=0.0, prefer_city=tc2.hex)
                else:
                    # 3rd+ Infantry or Scout: stay united near target_c / move to counter-attack TOGETHER (never flee!)
                    if ut.capture and target_c is not None and len(en_caps_here) >= 2:
                        adj_tc = [
                            n for n in bb.neighbors(target_c.hex)
                            if n in self.defense and self.occ.get(n, 0) < self.limit
                        ]
                        if adj_tc and bb.distance(u.hex, target_c.hex) > 1:
                            ref_e = min(en_caps_here, key=lambda e: bb.distance(target_c.hex, e.hex)).hex
                            best_adj = sorted(adj_tc, key=lambda h: (-self.defense.get(h, 0), bb.distance(h, ref_e)))[:2]
                            self.move_unit_toward(u, self.distance_field(best_adj), danger, caution=0.3)
                            if self.can_capture(u):
                                self.capture(u)
                            continue
                    target = min(local_enemies, key=lambda e: (not self.types[e.type].capture, bb.distance(u.hex, e.hex), e.id))
                    self.move_unit_toward(u, self.distance_field([target.hex]), danger, caution=0.55)
                if self.can_capture(u):
                    self.capture(u)
                continue

            if l == self.home_land and self.home_invasion_active() and not local_enemies:
                # Invaders are approaching or landed in fog!
                # Pull Scout inland if a loaded enemy transport is right next to it (<= 4 hexes) so it never dies for free!
                local_mine_h = [c for c in s.my_cities() if self.land.get(c.hex) == l]
                ref_h = self.home_raid_seen_hex
                target_c = (
                    min(local_mine_h, key=lambda c: (bb.distance(c.hex, ref_h), not bool(self.port_seas[c.id])))
                    if (local_mine_h and ref_h is not None)
                    else next((c for c in local_mine_h if self.port_seas[c.id]), local_mine_h[0] if local_mine_h else None)
                )
                if ut.skirmish and not ut.capture:
                    near_raid_t = any(
                        self.types[e.type].transport and len(s.passengers(e)) > 0 and bb.distance(e.hex, u.hex) <= 4
                        for e in self.enemies.values()
                    )
                    if near_raid_t and target_c is not None:
                        self.move_unit_toward(u, self.city_field[target_c.id], danger, caution=0.5)
                        continue
                    if ref_h:
                        home_coast_land = [
                            h for h, lid in self.land.items()
                            if lid == l and any(n in self.sea_hexes for n in bb.neighbors(h))
                            and not any(self.types[e.type].transport and len(s.passengers(e)) > 0 and bb.distance(e.hex, h) <= 4 for e in self.enemies.values())
                        ]
                        if home_coast_land:
                            sweep = min(home_coast_land, key=lambda h: bb.distance(h, ref_h))
                            if u.hex != sweep:
                                self.move_unit_toward(u, self.distance_field([sweep]), danger, caution=0.4)
                            continue
                if ut.capture and target_c is not None:
                    my_home_caps_i = sum(
                        1 for x in s.my_units()
                        if not x.carrier and self.land.get(x.hex) == l and self.types[x.type].capture
                    )
                    want_g = min(2, my_home_caps_i)
                    caps_on_tc = sum(
                        1 for x in s.my_units()
                        if not x.carrier and x.hex == target_c.hex and self.types[x.type].capture
                    )
                    if u.hex == target_c.hex:
                        if caps_on_tc <= want_g:
                            continue
                    elif caps_on_tc < want_g and self.occ.get(target_c.hex, 0) < self.limit:
                        # Unite 2 Infantry ON the threatened city (30% def, blocks 3v1 one-shot!)
                        self.move_unit_toward(u, self.city_field[target_c.id], danger, caution=0.0, prefer_city=target_c.hex)
                        continue
                    # 3rd+ Infantry stages directly adjacent to target_c toward the raid so ALL 3 fight UNITED!
                    stage = [n for n in bb.neighbors(target_c.hex) if n in self.defense and self.occ.get(n, 0) < self.limit]
                    if stage and u.hex not in stage:
                        ref_p = ref_h or target_c.hex
                        best_stage = sorted(stage, key=lambda h: (-self.defense.get(h, 0), bb.distance(h, ref_p)))[:2]
                        self.move_unit_toward(u, self.distance_field(best_stage), danger, caution=0.3)
                    continue

            local_mine = [c for c in s.my_cities() if self.land.get(c.hex) == l]
            if u.hp < ut.max_hp * 0.70 and local_mine and not local_foreign:
                home_c = min(local_mine, key=lambda c: self.city_field[c.id].get(u.hex, 999))
                if self.city_field[home_c.id].get(u.hex, 999) <= ut.move * 3:
                    self.move_unit_toward(u, self.city_field[home_c.id], danger, caution=1.0, prefer_city=home_c.hex)
                    continue

            if local_foreign:
                # On home island opening (rounds 1-4): move Scout straight to Port so it boards T1 on Round 4!
                target_c = min(
                    local_foreign,
                    key=lambda c: (
                        0 if self.port_seas[c.id] else 1,
                        self.city_field[c.id].get(u.hex, 999),
                        c.id,
                    ),
                )
                if ut.skirmish and not ut.capture and l == self.home_land and s.round <= 8:
                    # Stage Scout 2-3 hexes from our Home Port (away from Home City & shuttle wagon so 0 ration
                    # drain!) so it ALWAYS has MP ready to board fresh T1 on Round 3-4 alongside I1 + I2!
                    have_t = any(x.type == self.transport_code for x in s.my_units()) if self.transport_code else False
                    if not have_t and self.transport_code and self.my_port_cid is not None:
                        p_h_sc = s.cities[self.my_port_cid].hex
                        h_h_sc = s.cities[self.my_home_cid].hex if self.my_home_cid is not None else p_h_sc
                        sh_w = s.units.get(self.shuttle_wagon) if self.shuttle_wagon else None
                        if s.round >= 3 and 1 <= bb.distance(u.hex, p_h_sc) <= 3:
                            continue
                        reach_sc = self.reachable(u)
                        reach_sc[u.hex] = ""
                        staging_sc = [
                            h for h in reach_sc
                            if self.land.get(h) == self.home_land
                            and 2 <= bb.distance(h, p_h_sc) <= 3
                            and bb.distance(h, h_h_sc) > 1
                            and (sh_w is None or bb.distance(h, sh_w.hex) >= 2)
                        ]
                        if staging_sc:
                            en_p_sc = s.cities[self.enemy_port_cid].hex if self.enemy_port_cid is not None else p_h_sc
                            best_st = min(staging_sc, key=lambda h: (bb.distance(h, en_p_sc), bb.distance(h, u.hex), h))
                            if reach_sc[best_st]:
                                self.go(u, reach_sc[best_st])
                            continue
                    adj = [n for n in bb.neighbors(target_c.hex) if n in self.defense]
                    if adj and u.hex not in adj:
                        self.move_unit_toward(u, self.distance_field(adj), danger, caution=0.0)
                    continue
                # On Round 1, when Infantry march to our neutral Port, prefer stopping at dist >= 2 from Home City
                # if it STILL reaches Port in the same number of turns (prevents draining 20 bread from Home City!).
                if s.round == 1 and ut.capture and l == self.home_land and self.my_home_cid is not None:
                    home_h = s.cities[self.my_home_cid].hex
                    reach = self.reachable(u)
                    stride = max(1, ut.move)
                    pfield = self.city_field[target_c.id]
                    opts = [
                        (
                            (pfield[h] + stride - 1) // stride,
                            1 if bb.distance(h, home_h) <= 1 else 0,
                            pfield[h],
                            len(dirs),
                            dirs,
                        )
                        for h, dirs in reach.items()
                        if dirs and h in pfield
                    ]
                    if opts:
                        self.go(u, min(opts)[4])
                        if self.can_capture(u):
                            self.capture(u)
                        continue
                caution = 0.0 if not local_enemies else 1.0
                self.move_unit_toward(u, self.city_field[target_c.id], danger, caution=caution)
                if self.can_capture(u):
                    self.capture(u)
                continue

            if l == self.home_land:
                # Keep Scout on coastal lookout hill while waiting for enemy's first galley;
                # once enemy G1 is dead or T1 is built, move Scout back to Port to board T1 for vision 4 on middle!
                if ut.skirmish and not ut.capture and self.lookout_hex:
                    near_t_sc = any(
                        self.types[e.type].transport and len(s.passengers(e)) > 0 and bb.distance(e.hex, u.hex) <= 4
                        for e in self.enemies.values()
                    )
                    if near_t_sc and local_mine:
                        ret_c = min(local_mine, key=lambda c: self.city_field[c.id].get(u.hex, 99))
                        self.move_unit_toward(u, self.city_field[ret_c.id], danger, caution=0.5)
                        continue
                    have_t = any(x.type == self.transport_code for x in s.my_units()) if self.transport_code else False
                    if (not have_t) and self.transport_code and s.round <= (11 if self.ever_built_galley else 5) and self.my_port_cid is not None:
                        p_h_sc = s.cities[self.my_port_cid].hex
                        h_h_sc = s.cities[self.my_home_cid].hex if self.my_home_cid is not None else p_h_sc
                        sh_w = s.units.get(self.shuttle_wagon) if self.shuttle_wagon else None
                        u_on_beach = any(n in self.sea_hexes for n in bb.neighbors(u.hex))
                        # Hold MP intact 1-3 hexes INLAND from Port (off coastal beaches so amphibious raiders
                        # can NEVER 1-shot Scout on their landing turn!) so Scout instant-boards fresh T1!
                        if 1 <= bb.distance(u.hex, p_h_sc) <= 3 and not (self.ever_built_galley and u_on_beach):
                            continue
                        reach_sc = self.reachable(u)
                        staging_sc = [
                            h for h in reach_sc
                            if self.land.get(h) == self.home_land
                            and 1 <= bb.distance(h, p_h_sc) <= 3
                            and h != p_h_sc
                            and bb.distance(h, h_h_sc) > 1
                            and (sh_w is None or bb.distance(h, sh_w.hex) >= 2)
                        ]
                        inland_st = [h for h in staging_sc if not any(n in self.sea_hexes for n in bb.neighbors(h))]
                        use_st = inland_st or staging_sc
                        if use_st:
                            best_st = min(use_st, key=lambda h: (bb.distance(h, p_h_sc), bb.distance(h, u.hex), h))
                            if reach_sc[best_st]:
                                self.go(u, reach_sc[best_st])
                            continue
                    if (not have_t) and (not self.ever_killed_enemy_galley) and not self.saw_home_raid and not self.en_assault_t_ids and s.round > (11 if self.ever_built_galley else 5):
                        if u.hex != self.lookout_hex:
                            self.move_unit_toward(u, self.distance_field([self.lookout_hex]), danger, caution=0.5)
                        continue
                ports = [c for c in local_mine if self.port_seas[c.id]]
                inlands = [c for c in local_mine if not self.port_seas[c.id]]
                if ports:
                    port = ports[0]
                    # Keep 1 capturer directly ON our Port (30% city defense, blocks instant enemy raid capture!)
                    port_garrisoned = any(
                        x.hex == port.hex and not x.carrier and self.types[x.type].capture
                        for x in s.my_units()
                    )
                    if ut.capture and not port_garrisoned and self.occ.get(port.hex, 0) < self.limit:
                        if u.hex != port.hex:
                            self.move_unit_toward(u, self.city_field[port.id], danger, caution=0.0, prefer_city=port.hex)
                        continue
                    # Before T1 launches (r3-9):
                    # - Keep 1 Infantry ON our Port (+30% def, occ=1 so W never gets stack-blocked!), AND
                    # - Station our 2nd/3rd Infantry at the MIDPOINT between Home City and Port (<= 3 hexes
                    #   from BOTH!) so it reinforces Home City in 1 turn against back-beach blitzes AND still
                    #   boards fresh T1 on Round 6-7 in 1 turn!
                    have_t_idle = any(x.type == self.transport_code for x in s.my_units()) if self.transport_code else True
                    if ut.capture and not have_t_idle and s.round <= 12 and inlands:
                        home_caps_i = [
                            x for x in s.my_units()
                            if not x.carrier and self.land.get(x.hex) == l and self.types[x.type].capture
                        ]
                        caps_on_port_i = sum(1 for x in home_caps_i if x.hex == port.hex)
                        home_c = inlands[0]
                        if len(home_caps_i) >= 3:
                            # Keep I#1 & I#2 right at Port (ready to instant-board T1 with 0 delay!), and
                            # garrison I#3 DIRECTLY INSIDE Home City (+30% defense + fortify, blocks all back-beach blitzes!).
                            home_keeper = min(
                                home_caps_i,
                                key=lambda x: (self.city_field[home_c.id].get(x.hex, 999), -x.id),
                            )
                            if u.id == home_keeper.id:
                                if u.hex != home_c.hex and self.occ.get(home_c.hex, 0) < self.limit:
                                    self.move_unit_toward(u, self.city_field[home_c.id], danger, caution=0.0, prefer_city=home_c.hex)
                                continue
                    stage = [n for n in bb.neighbors(port.hex) if n in self.defense]
                    caps_on_port_e = sum(
                        1 for x in s.my_units()
                        if not x.carrier and x.hex == port.hex and self.types[x.type].capture
                    )
                    if u.hex == port.hex and self.occ.get(port.hex, 0) >= self.limit and stage and not (ut.capture and caps_on_port_e <= 1):
                        self.move_unit_toward(u, self.distance_field(stage), danger, caution=0.5)
                    elif stage and bb.distance(u.hex, port.hex) > 2:
                        self.move_unit_toward(u, self.distance_field(stage), danger, caution=0.5)
                continue

            if local_enemies:
                target = min(local_enemies, key=lambda e: bb.distance(u.hex, e.hex))
                self.move_unit_toward(u, self.distance_field([target.hex]), danger, caution=0.8)

    def run_land_wagons(self, danger):
        if not self.supply or not self.wagon_code:
            return
        s = self.state
        wagons = sorted(
            (u for u in s.my_units()
             if u.type == self.wagon_code and not u.fresh and not u.acted and not u.carrier
             and u.id not in self.ferry_wagons and u.id != self.shuttle_wagon),
            key=lambda u: u.id,
        )
        # Don't drive our ONLY wagon away from the port right when the port is ALREADY able to build T1 this turn!
        have_t = any(u.type == self.transport_code for u in s.my_units()) if self.transport_code else True
        total_w = sum(1 for u in s.my_units() if u.type == self.wagon_code)
        t_cost = self.types[self.transport_code].cost if self.transport_code else 150
        home_ports = [c for c in s.my_cities() if self.land.get(c.hex) == self.home_land and self.port_seas[c.id]]
        port_ready_for_t1 = bool(home_ports) and home_ports[0].bread >= t_cost and not self.galley_blockaded(home_ports[0])
        waiting_for_t1 = (not have_t) and total_w == 1 and bool(self.resource_isles()) and self.opening_shuttle_done and port_ready_for_t1

        w_dmg = self.wagon_incoming_damage()
        inv_active = self.home_invasion_active()

        for w in wagons:
            if self.out_of_time() or w.id not in s.units or w.mp <= 0:
                continue
            l = self.land.get(w.hex)
            if waiting_for_t1 and not inv_active and l == self.home_land and w.wheat == 0 and w.bread == 0:
                # Stay right by our port so we board T1 on the exact turn it spawns!
                ports = [c for c in s.my_cities() if self.land.get(c.hex) == l and self.port_seas[c.id]]
                if ports and bb.distance(w.hex, ports[0].hex) > 1:
                    self.move_unit_toward(w, self.city_field[ports[0].id], danger, caution=1.0)
                continue
            local_cities = [c for c in s.my_cities() if self.land.get(c.hex) == l and c.hex not in self.enemy_hex]
            local_sites = [x for x in s.sites if self.land.get(x.hex) == l and x.hex not in self.enemy_hex]
            if not local_cities:
                continue

            # Unload to non-galley-blockaded Port (so ships/troops flow! Never divert 210 wheat to Home City
            # just because a passing enemy Transport peeks at Port water for 1 turn!).
            local_ports = [c for c in local_cities if self.port_seas[c.id] and not self.galley_blockaded(c)]
            safe_inland = [c for c in local_cities if not self.port_seas[c.id]]
            en_home_vis_now = any(
                not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
                for e in self.enemies.values()
            )
            en_home_cap_vis = any(
                not e.carrier and not self.types[e.type].naval and self.types[e.type].capture
                and self.land.get(e.hex) == self.home_land
                for e in self.enemies.values()
            )
            home_caps_lw = sum(
                1 for u in s.my_units()
                if not u.carrier and self.land.get(u.hex) == self.home_land and self.types[u.type].capture
            )
            caps_ret_lw = sum(
                1 for tid, f_r in self.ferries.items()
                if f_r["phase"] == "back" and tid in s.units
                for p in s.passengers(s.units[tid]) if self.types[p.type].capture
            )
            have_a_now = sum(1 for u in s.my_units() if self.archer_code and u.type == self.archer_code)
            have_g_lw0 = sum(1 for u in s.my_units() if self.galley_code and u.type == self.galley_code)
            en_g_live_lw0 = sum(
                1 for eid, r in self.enemy_galleys_seen.items()
                if s.round - r <= 14 and eid not in self.killed_galley_ids
            )
            a_cost = self.types[self.archer_code].cost if self.archer_code else 999
            can_fund_home_archer = (
                self.saw_home_raid
                and have_t
                and not self.ever_killed_enemy_galley
                and have_a_now < 1
                and s.round <= 15
                and bool(safe_inland)
                and (not local_ports or en_g_live_lw0 > have_g_lw0)
                and (w.wheat + safe_inland[0].bread >= a_cost)
            )
            need_land_build = (
                l == self.home_land
                and w.wheat > 0
                and (
                    (en_home_cap_vis and (home_caps_lw < 2 or (local_ports and w_dmg.get(local_ports[0].hex, 0) >= 45)))
                    or (inv_active and (home_caps_lw + caps_ret_lw) == 0)
                    or can_fund_home_archer
                )
            )
            if need_land_build:
                # During a home invasion or post-raid Archer build, deliver wheat to a SAFE city with an
                # OPEN LAND BUILD SLOT (occ == 0, typically Home City!) so after W enters (occ == 1 < limit),
                # that city IMMEDIATELY builds an Archer (120) or Infantry (100) THIS TURN instead of letting
                # 150 bread get eaten overnight by rations in a full Port!
                dest = min(
                    local_cities,
                    key=lambda c: (
                        w_dmg.get(c.hex, 0) >= w.hp,
                        self.occ.get(c.hex, 0) >= self.limit - 1,
                        w_dmg.get(c.hex, 0) >= 45,
                        w_dmg.get(c.hex, 0),
                        self.city_field[c.id].get(w.hex, 999),
                        c.id,
                    ),
                )
            elif local_ports and local_ports[0].bread < 1000 and (
                w_dmg.get(local_ports[0].hex, 0) < w.hp
                or ((home_caps_lw + caps_ret_lw) >= 1 and not en_home_cap_vis)
            ):
                dest = local_ports[0]
            elif safe_inland:
                dest = safe_inland[0]
            else:
                dest = min(local_cities, key=lambda c: (self.city_field[c.id].get(w.hex, 999), c.id))

            # Survival check: if wagon is seriously threatened (not a 28-dmg Scout poke against a >=70-HP Wagon!)
            # OR genuinely wounded (< 70 HP) during home invasion, retreat into the safest city immediately!
            dmg_alarm = 45 if w.hp >= 70 else 15
            if l == self.home_land and (w_dmg.get(w.hex, 0) >= dmg_alarm or (inv_active and w.hp < 70)):
                # If already wounded (< 70 HP), shelter in the NEAREST SAFE CITY (even if carrying wheat!)
                # instead of running 6 hexes across the island getting pecked to death by a Scout!
                safe_dest = dest if (w.wheat > 0 and w.hp >= 70) else min(
                    local_cities,
                    key=lambda c: (w_dmg.get(c.hex, 0) >= w.hp, w_dmg.get(c.hex, 0) >= 45, self.city_field[c.id].get(w.hex, 999), c.id),
                )
                if w.wheat > 0 and bb.distance(w.hex, safe_dest.hex) <= self.types[w.type].move:
                    self.make_room(safe_dest)
                reach = self.reachable(w)
                reach[w.hex] = ""
                if w.wheat > 0 and safe_dest.hex in reach and w_dmg.get(safe_dest.hex, 0) < w.hp:
                    self.go(w, reach[safe_dest.hex])
                    if w.hex == safe_dest.hex:
                        if safe_dest == dest or w_dmg.get(safe_dest.hex, 0) > 0:
                            self.unload(w, w.wheat)
                    continue
                safest = min(
                    reach,
                    key=lambda h: (
                        w_dmg.get(h, 0) >= w.hp,
                        w_dmg.get(h, 0) >= dmg_alarm,
                        h != safe_dest.hex,
                        self.city_field[safe_dest.id].get(h, 999),
                        w_dmg.get(h, 0),
                        len(reach[h]),
                    ),
                )
                if reach[safest]:
                    self.go(w, reach[safest])
                if w.hex == safe_dest.hex and (w.wheat > 0 or w.bread > 0):
                    self.unload(w)
                continue

            if w.wheat > 0:
                if l == self.home_land and inv_active:
                    # Only advance toward dest along non-lethal hexes (don't freeze 4 turns over a 28-dmg Scout!)
                    if bb.distance(w.hex, dest.hex) <= self.types[w.type].move:
                        self.make_room(dest)
                    reach = self.reachable(w)
                    reach[w.hex] = ""
                    if dest.hex in reach and w_dmg.get(dest.hex, 0) < w.hp:
                        self.go(w, reach[dest.hex])
                    else:
                        zero_opts = [h for h in reach if h in self.city_field[dest.id] and w_dmg.get(h, 0) < dmg_alarm]
                        safe_opts = zero_opts or [h for h in reach if h in self.city_field[dest.id] and w_dmg.get(h, 0) < w.hp]
                        if safe_opts:
                            best_h = min(
                                safe_opts,
                                key=lambda h: (
                                    self.city_field[dest.id][h] + 0.08 * w_dmg.get(h, 0),
                                    len(reach[h]),
                                    h,
                                ),
                            )
                            if reach[best_h]:
                                self.go(w, reach[best_h])
                else:
                    self.enter_city(w, dest, danger)
                if w.hex == dest.hex:
                    self._jit_unload(w, dest, w_dmg)
                continue

            if w.bread >= 100 and local_ports and local_ports[0].bread < 500:
                self.enter_city(w, local_ports[0], danger)
                if w.hex == local_ports[0].hex:
                    self.unload(w)
                continue

            # If bread got stranded in Home City while Port needs bread for T1/G1/G2, shuttle it to Port!
            have_g_lw = sum(1 for u in s.my_units() if self.galley_code and u.type == self.galley_code)
            if (
                l == self.home_land and not en_home_cap_vis and (not inv_active or home_caps_lw >= 3)
                and w.wheat == 0 and w.bread == 0
                and local_ports and safe_inland
                and (
                    (not have_t and local_ports[0].bread < 150 and safe_inland[0].bread >= 160)
                    or ((have_t or home_caps_lw >= 3) and have_g_lw < 2 and local_ports[0].bread < 200 and safe_inland[0].bread >= 120)
                )
            ):
                if w.hex != safe_inland[0].hex:
                    self.enter_city(w, safe_inland[0], danger)
                if w.hex == safe_inland[0].hex and safe_inland[0].bread >= 100:
                    self.load(w, min(self.cargo_max, safe_inland[0].bread - 30))
                continue

            if local_sites:
                # Only hold wagons back from our home site BEFORE invaders land if a loaded enemy Transport
                # is literally right next to the site (dist <= 3) ready to drop Infantry directly on our wagon!
                if l == self.home_land and inv_active and not en_home_vis_now:
                    imminent_drop = any(
                        self.types[e.type].transport and len(s.passengers(e)) > 0
                        and any(bb.distance(e.hex, x.hex) <= 3 for x in local_sites)
                        for e in self.enemies.values()
                    )
                    if imminent_drop:
                        safe_dest = min(
                            local_cities,
                            key=lambda c: (w_dmg.get(c.hex, 0), self.city_field[c.id].get(w.hex, 999), c.id),
                        )
                        if w.hex != safe_dest.hex and w_dmg.get(safe_dest.hex, 0) == 0:
                            self.enter_city(w, safe_dest, danger)
                        continue
                safe_sites = [
                    x for x in local_sites
                    if (w_dmg.get(x.hex, 0) < dmg_alarm if (l == self.home_land and inv_active) else w_dmg.get(x.hex, 0) < w.hp * 0.7)
                    and not (
                        l == self.home_land and inv_active
                        and any(
                            not e.carrier and self.types[e.type].capture and self.land.get(e.hex) == l
                            and bb.distance(e.hex, x.hex) <= 5
                            for e in self.enemies.values()
                        )
                    )
                ]
                if not safe_sites:
                    # Invaders are sitting on/near our home site — shelter at safest city!
                    safe_dest = min(
                        local_cities,
                        key=lambda c: (w_dmg.get(c.hex, 0), self.city_field[c.id].get(w.hex, 999), c.id),
                    )
                    if w.hex != safe_dest.hex and w_dmg.get(safe_dest.hex, 0) < w.hp:
                        self.enter_city(w, safe_dest, danger)
                    continue
                site = max(safe_sites, key=lambda x: (self.known_site_wheat.get(x.id, 0), -self.site_field[x.id].get(w.hex, 999)))
                # Don't park our 2nd wagon motionless on an empty (< 90 wheat) home site while a raid is active
                # or an enemy assault transport is inbound — shelter safely in a city until wheat is ready!
                if (
                    l == self.home_land
                    and total_w >= 2
                    and (inv_active or bool(self.en_assault_t_ids))
                    and self.known_site_wheat.get(site.id, 0) < 90
                ):
                    safe_dest = min(
                        local_cities,
                        key=lambda c: (w_dmg.get(c.hex, 0), self.city_field[c.id].get(w.hex, 999), c.id),
                    )
                    if w.hex != safe_dest.hex and w_dmg.get(safe_dest.hex, 0) < w.hp:
                        self.enter_city(w, safe_dest, danger)
                    continue
                # Hold our ONLY wagon safely inside a city for 1 turn when an enemy assault transport JUST
                # vanished into fog off our home coast (not yet sunk, raiders not yet spotted!) — prevents
                # walking blind with 0 MP left straight into an unseen 2-Infantry landing!
                if (
                    l == self.home_land
                    and total_w == 1
                    and w.wheat == 0
                    and w.hex in self.city_at
                    and inv_active
                    and not en_home_vis_now
                    and bool(self.en_assault_t_ids)
                    and not self.ever_sank_enemy_transport
                    and self.home_kills_during_alert == 0
                    and s.round <= 9
                ):
                    continue
                if w.hex != site.hex:
                    if l == self.home_land and (inv_active or (bool(self.en_assault_t_ids) and s.round <= 15)):
                        # Step 1 hex at a time during a home raid alert so if our ONLY wagon spots newly-landed
                        # enemy Infantry in fog mid-walk, it STILL has MP left to retreat safely back into a city!
                        for _step_w in range(max(1, w.mp)):
                            if w.id not in s.units or w.mp <= 0 or w.hex == site.hex or self.out_of_time():
                                break
                            reach_w = self.reachable(w)
                            reach_w[w.hex] = ""
                            sf_s = self.site_field[site.id]
                            safe_opts = [h for h in reach_w if h in sf_s and w_dmg.get(h, 0) < dmg_alarm]
                            if not safe_opts:
                                break
                            best_h = min(safe_opts, key=lambda h: (sf_s[h], len(reach_w[h]), h))
                            path_w = reach_w[best_h]
                            if not path_w:
                                break
                            # Pick shortest prefix whose landing hex is NOT stack_full (never stop on an occ==limit hex!)
                            step_dirs = ""
                            cur_h = w.hex
                            for ch in path_w:
                                step_dirs += ch
                                cur_h = bb.step(cur_h, int(ch))
                                if self.occ.get(cur_h, 0) < self.limit:
                                    break
                            prev_h = w.hex
                            self.go(w, step_dirs)
                            if w.id not in s.units or w.hex == prev_h:
                                break
                            w_dmg = self.wagon_incoming_damage()
                            if w_dmg.get(w.hex, 0) >= dmg_alarm:
                                # Spotted lethal ambush in fog! Immediately retreat with remaining MP!
                                safe_ret = min(
                                    local_cities,
                                    key=lambda c: (w_dmg.get(c.hex, 0) >= w.hp, w_dmg.get(c.hex, 0) >= dmg_alarm, self.city_field[c.id].get(w.hex, 999), c.id),
                                )
                                reach_ret = self.reachable(w)
                                reach_ret[w.hex] = ""
                                ret_h = min(
                                    reach_ret,
                                    key=lambda h: (
                                        w_dmg.get(h, 0) >= w.hp,
                                        w_dmg.get(h, 0) >= dmg_alarm,
                                        h != safe_ret.hex,
                                        self.city_field[safe_ret.id].get(h, 999),
                                        len(reach_ret[h]),
                                    ),
                                )
                                if reach_ret[ret_h]:
                                    self.go(w, reach_ret[ret_h])
                                break
                    else:
                        self.move_unit_toward(w, self.site_field[site.id], danger, caution=1.5)
                about_to_build_g1 = (
                    (not self.ever_built_galley)
                    and self.galley_code is not None
                    and bool(local_ports)
                    and local_ports[0].bread >= self.types[self.galley_code].cost
                )
                want_g2_first_lw = (
                    (not have_t)
                    and self.ever_built_galley
                    and self.port_sea_dist <= 21
                    and bool(self.resource_isles())
                    and self.galley_code is not None
                    and have_g_lw0 < 2
                    and s.round <= 11
                    and (self.saw_home_raid or bool(self.en_assault_t_ids) or inv_active or en_home_vis_now or en_g_live_lw0 > have_g_lw0)
                )
                if (not have_t) and (self.ever_built_galley or about_to_build_g1) and local_ports:
                    # Wait 1 extra turn ONLY if needed so ONE single home-site load guarantees G2 (during a home raid!)
                    # or T1 immediately, accounting for ration drain at Port!
                    port0 = local_ports[0]
                    g_cost = self.types[self.galley_code].cost if self.galley_code else 200
                    g_deduct = g_cost if about_to_build_g1 else 0
                    rem_bread = max(0, port0.bread - g_deduct)
                    walk_turns = max(1, math.ceil(self.city_field[port0.id].get(site.hex, 4) / max(1, self.types[w.type].move)))
                    walk_income = 0 if (want_g2_first_lw or inv_active or rem_bread < 50) else self.city_bread * max(0, walk_turns - 1)
                    eff_rem = max(0, rem_bread - (20 if (want_g2_first_lw or inv_active) else 0))
                    ship_need = g_cost if want_g2_first_lw else t_cost
                    min_load = max(120, min(site.cap, ship_need - eff_rem - walk_income))
                elif (have_t or total_w >= 2) and self.galley_code and local_ports and sum(1 for u in s.my_units() if u.type == self.galley_code) < 2:
                    # Prevent the 190-bread cliff (including 4-turn port maps where W2 reaches the home site on r4
                    # before T1 is built at end of r4!): compute exact port bread upon arrival so ONE load hits G1 (200)!
                    port0 = local_ports[0]
                    g_cost_w = self.types[self.galley_code].cost
                    proj_port_bread = max(0, port0.bread - t_cost) if (not have_t and port0.bread >= t_cost) else port0.bread
                    walk_turns = max(1, math.ceil(self.city_field[port0.id].get(site.hex, 4) / max(1, self.types[w.type].move)))
                    if inv_active or caps_ret_lw > 0:
                        # Returning evacuation ferry + Infantry drain all Port bread + walk income overnight;
                        # load 210 instead of 180 so Port hits >= 200 for G1 on arrival instead of stranding at 180!
                        proj_port_bread = 0
                        walk_income = 0
                    else:
                        walk_income = self.city_bread * walk_turns
                    g_need = g_cost_w - proj_port_bread - walk_income
                    if 100 < g_need <= min(site.cap, self.cargo_max) and g_need <= site.wheat + 90:
                        min_load = g_need
                    else:
                        min_load = 100 if (s.round <= 12 or port0.bread < 250) else 180
                else:
                    min_load = 100 if (s.round <= 12 or (local_ports and local_ports[0].bread < 250)) else 180
                if w.hex == site.hex and site.wheat >= min(min_load, site.cap) and (self.cargo_max - w.wheat - w.bread) > 0:
                    self.load(w)

    # ------------------------------------------------------------------
    # Phase 4: Strategic Production (Turn-3 T1 -> Turn-8 G1+G2 -> Anti-Blockade Archers!)
    # ------------------------------------------------------------------

    def estimate_food_cap(self):
        s = self.state
        if not self.supply:
            return 999
        income = self.city_bread * len(s.my_cities())
        owned_lands = {self.land.get(c.hex) for c in s.my_cities()}
        for x in s.sites:
            if self.land.get(x.hex) in owned_lands:
                income += x.output
        for f in self.ferries.values():
            for x in s.sites:
                if self.land.get(x.hex) == f["isle"]:
                    income += x.output
        stock = sum(c.bread for c in s.my_cities()) + sum(u.bread + u.wheat for u in s.my_units())
        eats_per_unit = max(1.0, 1.35 * self.ration_cost)
        return max(8, int((income + stock / 70.0) / eats_per_unit))

    def build_all(self):
        s = self.state
        self.refresh()
        if not self.supply:
            self._build_classic()
            return

        total_units = len(s.my_units())
        food_cap = self.estimate_food_cap()
        counts = {k: 0 for k in self.types}
        for u in s.my_units():
            counts[u.type] += 1

        have_t = counts.get(self.transport_code, 0)
        have_g = counts.get(self.galley_code, 0)
        have_w = counts.get(self.wagon_code, 0)
        have_a = counts.get(self.archer_code, 0) if self.archer_code else 0
        if have_g > 0:
            self.ever_built_galley = True

        r_isles = self.resource_isles()
        en_galleys_recent = sum(1 for r in self.enemy_galleys_seen.values() if s.round - r <= 22)
        en_g_live = sum(
            1 for eid, r in self.enemy_galleys_seen.items()
            if s.round - r <= 14 and eid not in self.killed_galley_ids
        )
        en_galleys_vis = sum(
            1 for e in self.enemies.values()
            if self.types[e.type].naval and not self.types[e.type].transport
        )
        en_cav_seen = sum(1 for t in self.enemy_types_seen.values() if t == self.cav_code) if self.cav_code else 0
        enemy_on_home = any(
            not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
            for e in self.enemies.values()
        )
        enemy_trans_near_home = any(
            self.is_home_raid_transport(e)
            for e in self.enemies.values()
        )
        home_ports = [c for c in self.my_ports() if self.land.get(c.hex) == self.home_land]
        home_port_blockaded = any(self.blockaded(c) for c in home_ports)
        home_port_galley_blockaded = any(self.galley_blockaded(c) for c in home_ports)
        enemy_ships_near_home = home_port_galley_blockaded or any(
            self.types[e.type].naval and not self.types[e.type].transport and any(
                bb.distance(e.hex, c.hex) <= 5
                for c in s.my_cities() if self.land.get(c.hex) == self.home_land
            )
            for e in self.enemies.values()
        )
        inv_active = self.home_invasion_active()
        home_combat_now = sum(
            1 for u in s.my_units()
            if not u.carrier and self.land.get(u.hex) == self.home_land
            and not self.types[u.type].naval and not self.types[u.type].carry
        )
        home_caps_now = sum(
            1 for u in s.my_units()
            if not u.carrier and self.land.get(u.hex) == self.home_land
            and self.types[u.type].capture
        )
        caps_returning_home = sum(
            1 for tid, f_r in self.ferries.items()
            if f_r["phase"] == "back" and tid in s.units
            for p in s.passengers(s.units[tid]) if self.types[p.type].capture
        )
        eff_home_caps = home_caps_now + caps_returning_home
        en_home_vis = sum(
            1 for e in self.enemies.values()
            if not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
        )
        need_home_inf = inv_active and (home_combat_now + caps_returning_home) < ((4 if en_home_vis >= 2 else en_home_vis + 1) if en_home_vis > 0 else 2)

        combat_now = sum(
            n for k, n in counts.items()
            if not self.types[k].naval and not self.types[k].carry
        )
        # Check if middle island is contested or hostile by enemy HEAVY units (not a lone Armada Scout!)
        middle_contested = any(
            self.isle_known_capturers(isle) > 0
            and sum(1 for u in self.isle_status(isle)[0] if self.types[u.type].capture) <= self.isle_known_capturers(isle)
            for isle in r_isles
        )
        home_sites_exist = any(self.land.get(x.hex) == self.home_land for x in s.sites)
        g_open_islets = (self.port_sea_dist <= 21 and bool(r_isles) and home_sites_exist and self.galley_code is not None)
        home_w_near_g2 = any(
            u.type == self.wagon_code and not u.carrier
            and self.land.get(u.hex) == self.home_land
            and u.wheat >= 175
            for u in s.my_units()
        )
        port_bread_max = max((c.bread for c in home_ports), default=0)
        t_cost_b = self.types[self.transport_code].cost if self.transport_code else 150
        g2_achievable_now = (en_g_live > have_g) or (port_bread_max >= 175) or (port_bread_max < t_cost_b) or home_w_near_g2
        want_g2_before_t1 = (
            g_open_islets
            and self.ever_built_galley
            and have_g < 2
            and have_t == 0
            and s.round <= 11
            and g2_achievable_now
            and (
                self.saw_home_raid
                or bool(self.en_assault_t_ids)
                or inv_active
                or enemy_on_home
                or enemy_trans_near_home
                or en_g_live > have_g
            )
        )
        naval_arms_race = (en_galleys_recent >= 2) or (en_g_live >= 2) or (en_galleys_vis >= 2)
        if naval_arms_race:
            want_g = min(5, max(4, en_galleys_recent + 2, en_galleys_vis + 2))
        else:
            want_g = min(4, max(2 if (have_t >= 1 or g_open_islets) else 0, en_galleys_recent + 1, en_galleys_vis + 2))
        if r_isles:
            if have_g >= (4 if naval_arms_race else 2) and combat_now >= 7 and sum(c.bread for c in self.my_ports()) >= 450:
                want_t = 3
            elif have_g >= (3 if naval_arms_race else 2) and combat_now >= 4:
                want_t = 2
            else:
                want_t = 1
        else:
            want_t = 2 if combat_now >= 4 else 1

        # Build a 2nd wagon once T1 is out (or after r5 I#3 on r10+) so our home wheat site is harvested continuously!
        want_w = (
            3 if (self.ferries and have_g >= 2 and combat_now >= 6)
            else (2 if (home_sites_exist and (have_t >= 1 or (g_open_islets and s.round >= 10 and counts.get(self.inf_code, 0) >= 3))) else 1)
        )

        build_cities = [
            c for c in sorted(s.my_cities(), key=lambda c: (not bool(self.port_seas[c.id]), -c.bread, c.id))
            if not c.built and c.hex not in self.enemy_hex
        ]

        for c in build_cities:
            if self.out_of_time():
                break
            is_port = bool(self.port_seas[c.id])
            launch = self.launch_hex(c) if is_port else None
            blockaded = is_port and self.blockaded(c)

            # Opening discipline: do NOT spend Home City bread before shuttle wagon loads opening ship bread!
            # Once shuttle wagon leaves Home City, build W2 (100 bread) on Round 3-4 so we have TWO wagons
            # harvesting our Home Wheat Site continuously (funding r6-7 T1 after our r3 G1 on Islets, or r7 G1 on Islands!).
            g_open_islets = (self.port_sea_dist <= 21 and bool(r_isles) and home_sites_exist and self.galley_code is not None)
            open_ship_cost = self.types[self.galley_code].cost if g_open_islets else (self.types[self.transport_code].cost if self.transport_code else 150)
            shuttle_loaded = self.opening_shuttle_done or (
                self.shuttle_wagon in s.units and s.units[self.shuttle_wagon].bread >= open_ship_cost
            )
            if not is_port and not shuttle_loaded and s.round <= 2:
                continue
            if (
                not is_port
                and g_open_islets
                and home_sites_exist
                and s.round <= 8
                and counts.get(self.inf_code, 0) < 3
                and home_caps_now < 3
                and self.occ.get(c.hex, 0) < self.limit
            ):
                # On compact Islets (r3/4 G1 opening), build r5 I#3 directly INSIDE Home City (0 delay to G1,
                # 0 delay to T1, 100% garrisoned Home City + Port, crushes 2 I + S home raids 3v2 while keeping
                # I#1 + I#2 ready at Port to instant-board T1!) instead of feeding a naked r5 W2 to raiders!
                if c.bread >= self.types[self.inf_code].cost:
                    if self.build(c, self.inf_code):
                        counts[self.inf_code] += 1
                        home_combat_now += 1
                        home_caps_now += 1
                        combat_now += 1
                        total_units += 1
                        self.occ[c.hex] = self.occ.get(c.hex, 0) + 1
                    continue
            if (
                not is_port
                and not g_open_islets
                and home_sites_exist
                and self.wagon_code is not None
                and s.round <= 8
                and have_w < 2
                and not enemy_on_home
                and self.occ.get(c.hex, 0) < self.limit
            ):
                if c.bread >= self.types[self.wagon_code].cost:
                    if self.build(c, self.wagon_code):
                        counts[self.wagon_code] += 1
                        have_w += 1
                        total_units += 1
                        self.occ[c.hex] = self.occ.get(c.hex, 0) + 1
                    continue
            if not is_port and not self.opening_shuttle_done and s.round <= 2:
                continue

            # Keep small reserve early while units have full rations, bigger later!
            keep = 10 if s.round <= 12 else (50 if is_port else 30)
            budget = c.bread - keep

            # 1. Emergency home land defense (active invasion OR loaded transport just approached in fog!)
            # CRITICAL: If our Port is unblocked, can launch G2 RIGHT NOW (have_g < 2, bread >= 200),
            # our Port already has >= 1 capturer garrison (home_caps_now >= 1), and NO enemy capturer is
            # within 2 hexes of our Port (Home City handles land reinforcements!), let Priority 3 build G2!
            en_cap_near_port = is_port and any(
                not e.carrier and not self.types[e.type].naval and self.types[e.type].capture
                and bb.distance(e.hex, c.hex) <= 2
                for e in self.enemies.values()
            )
            # 0b. Emergency ZERO-WAGON recovery: if our only wagon died on home island, our economy is
            # permanently frozen until we rebuild a wagon (85 bread, keep=0)! Rebuild immediately unless
            # enemy capturers are literally about to capture this city with <= 1 defender left!
            if (
                self.wagon_code is not None
                and have_w == 0
                and home_sites_exist
                and s.round >= 5
                and self.land.get(c.hex) == self.home_land
                and self.occ.get(c.hex, 0) < self.limit
                and c.bread >= self.types[self.wagon_code].cost
                and not (enemy_on_home and home_caps_now <= 1)
                and not en_cap_near_port
            ):
                if self.build(c, self.wagon_code):
                    counts[self.wagon_code] += 1
                    have_w += 1
                    total_units += 1
                    self.occ[c.hex] = self.occ.get(c.hex, 0) + 1
                continue

            port_prioritize_g2 = (
                is_port
                and not blockaded
                and launch is not None
                and self.galley_code is not None
                and have_g < (4 if naval_arms_race else 2)
                and not en_cap_near_port
                and home_caps_now >= 1
                and c.bread >= self.types[self.galley_code].cost
            )
            en_near_port_5 = is_port and any(
                not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
                and bb.distance(e.hex, c.hex) <= 4
                for e in self.enemies.values()
            )
            port_prioritize_t1 = (
                is_port
                and not blockaded
                and launch is not None
                and self.transport_code is not None
                and have_t == 0
                and not want_g2_before_t1
                and not en_cap_near_port
                and not en_near_port_5
                and home_caps_now >= 2
                and c.bread >= self.types[self.transport_code].cost
            )
            inland_raid_inf = (not is_port) and inv_active and eff_home_caps < (4 if en_home_vis > 0 else 2)
            en_trans_at_door = is_port and any(
                self.types[e.type].transport
                and sum(1 for p in s.passengers(e) if self.types[p.type].capture) >= 2
                and bb.distance(e.hex, c.hex) <= 4
                for e in self.enemies.values()
            )
            port_save_g1_calm = (
                is_port
                and not blockaded
                and launch is not None
                and have_g == 0
                and not enemy_on_home
                and (not en_trans_at_door or eff_home_caps >= 2)
                and (not enemy_trans_near_home or home_caps_now >= 1)
                and c.bread >= 150
            )
            port_save_g2_calm = (
                is_port
                and not blockaded
                and launch is not None
                and want_g2_before_t1
                and not en_cap_near_port
                and home_caps_now >= 2
                and c.bread >= 120
            )
            need_emerg_inf_now = (enemy_on_home and (home_caps_now < 3 or en_home_vis >= 4))
            if (not port_prioritize_g2) and (not port_prioritize_t1) and (not port_save_g1_calm) and (not port_save_g2_calm) and (need_emerg_inf_now or (enemy_trans_near_home and eff_home_caps < 2) or (need_home_inf and home_caps_now < 3) or (inland_raid_inf and home_caps_now < 3)) and self.land.get(c.hex) == self.home_land and self.occ.get(c.hex, 0) < self.limit:
                if (
                    self.saw_home_raid
                    and not enemy_on_home
                    and self.archer_code
                    and have_a < 1
                    and home_caps_now >= 3
                    and c.bread >= self.types[self.archer_code].cost
                ):
                    if self.build(c, self.archer_code):
                        counts[self.archer_code] += 1
                        have_a += 1
                        home_combat_now += 1
                        total_units += 1
                        self.occ[c.hex] = self.occ.get(c.hex, 0) + 1
                    continue
                code = self.pike_code if (en_cav_seen >= 2 and c.bread >= self.types[self.pike_code].cost) else self.inf_code
                if c.bread >= self.types[code].cost:
                    if self.build(c, code):
                        counts[code] += 1
                        home_combat_now += 1
                        if self.types[code].capture:
                            home_caps_now += 1
                        total_units += 1
                        self.occ[c.hex] = self.occ.get(c.hex, 0) + 1
                        need_home_inf = inv_active and home_combat_now < ((4 if en_home_vis >= 2 else en_home_vis + 1) if en_home_vis > 0 else 2)
                continue

            # 1b. Post-raid Anti-Galley Archer: ONLY at inland Home City when enemy Galleys already outnumber us!
            # NEVER burn Port's 200 bread for G2 on an Archer when Port is unblocked and can launch G2!
            need_raid_archer = (
                self.saw_home_raid
                and not is_port
                and en_g_live > have_g
                and not self.ever_killed_enemy_galley
                and have_t >= 1
                and self.archer_code is not None
                and have_a < 1
                and s.round <= 15
                and self.ever_built_galley
                and self.land.get(c.hex) == self.home_land
            )
            if need_raid_archer and c.bread >= self.types[self.archer_code].cost:
                if self.occ.get(c.hex, 0) >= self.limit:
                    self.make_room(c)
                if self.occ.get(c.hex, 0) < self.limit and self.build(c, self.archer_code):
                    counts[self.archer_code] += 1
                    have_a += 1
                    home_combat_now += 1
                    total_units += 1
                    self.occ[c.hex] = self.occ.get(c.hex, 0) + 1
                continue

            # 2. ANTI-BLOCKADE ARCHERS: if enemy ships blockade/threaten our home coast AND outnumber our galleys,
            # OR enemy has more live galleys than us (e.g. Armada 2G vs our 1G/0G!) at inland Home City / blockaded Port,
            # build Archers to shred enemy galleys from range 2 without taking retaliation!
            # EXCEPTION: If our Port is unblocked, we have G1, and Port can afford G2 RIGHT NOW, build G2 first!
            port_can_build_g2_now = (
                is_port
                and not blockaded
                and launch is not None
                and self.galley_code is not None
                and have_g < (4 if naval_arms_race else 2)
                and c.bread >= self.types[self.galley_code].cost
            )
            need_coastal_archer = (
                (not port_can_build_g2_now)
                and (
                    home_port_galley_blockaded
                    or (enemy_ships_near_home and (have_g == 0 or en_galleys_vis > have_g))
                    or (en_g_live > have_g and s.round >= 10 and ((not is_port) or blockaded or have_g == 0))
                    or (naval_arms_race and (not is_port) and have_w >= 2 and have_a < 2 and not enemy_on_home)
                )
            )
            if need_coastal_archer and self.land.get(c.hex) == self.home_land and self.archer_code and have_a < 3 and self.occ.get(c.hex, 0) < self.limit:
                if c.bread >= self.types[self.archer_code].cost + (0 if en_g_live > have_g else 15):
                    if self.build(c, self.archer_code):
                        counts[self.archer_code] += 1
                        have_a += 1
                        total_units += 1
                    continue
                # If our port is blockaded OR enemy outnumbers our galleys, save bread for an Archer —
                # EXCEPT never starve our 2nd Wagon (W2 for our home wheat site!) when Port is NOT blockaded!
                if (home_port_galley_blockaded or (en_g_live > have_g and (have_w >= 2 or not home_sites_exist))) and have_a < 2 and have_w >= 1:
                    continue

            # 3. Port ship production:
            # - On compact Islets (g_open_islets): GALLEY-FIRST META BREAKER (r3 G1 + r3 W2 -> r6-7 T1 -> r9-10 G2!)
            #   I#1+I#2 garrison our Home Port on r3-6 (0% blitz risk!), r3 G1 sinks enemy r3 T1 & r7 T2 BEFORE
            #   rivals ever build a Galley, and W1+W2 launch T1 on r6-7 under total sea supremacy!
            # - On wide Islands: TRANSPORT+W2 POWER OPENING (r3 T1 + r3 W2 -> r7 G1!).
            if is_port and launch is not None and not blockaded:
                if g_open_islets and not self.ever_built_galley:
                    if have_g == 0 and self.galley_code and c.bread >= self.types[self.galley_code].cost:
                        if self.build(c, self.galley_code):
                            have_g += 1
                            self.ever_built_galley = True
                            if self.first_galley_round is None:
                                self.first_galley_round = s.round
                            total_units += 1
                        continue
                    # Save Port bread for r3-4 G1 (200) instead of spending 150 on T1 first!
                    continue
                # Priority 1: Opening Transport T1 (immediately on r6-7 after r3 G1 on Islets, or r3-4 on Islands)!
                # When facing a home raid / assault ship (want_g2_before_t1), build r8 G2 FIRST so 2G seal enemy
                # Port & 1-turn sink T2/G1 while I1+I2 clear our home island before launching T1 on r10-12!
                if have_t == 0 and not want_g2_before_t1 and (s.round <= 10 or have_g >= max(1, en_g_live)) and self.transport_code and c.bread >= self.types[self.transport_code].cost:
                    if self.build(c, self.transport_code):
                        have_t += 1
                        total_units += 1
                    continue
                # Priority 2: First Galley G1 (or replacement G1 if lost)!
                if have_g == 0 and self.galley_code and c.bread >= self.types[self.galley_code].cost:
                    if self.build(c, self.galley_code):
                        have_g += 1
                        self.ever_built_galley = True
                        if self.first_galley_round is None:
                            self.first_galley_round = s.round
                        total_units += 1
                    continue
                if total_units < food_cap + 3:
                    # Priority 3: Build 2nd Galley (and replace lost G1) to maintain total sea supremacy!
                    if have_g < 2 and have_g < want_g and self.galley_code and c.bread >= self.types[self.galley_code].cost + (0 if (s.round <= 20 or en_g_live >= have_g) else keep):
                        if self.build(c, self.galley_code):
                            have_g += 1
                            self.ever_built_galley = True
                            if self.first_galley_round is None:
                                self.first_galley_round = s.round
                            total_units += 1
                        continue
                    # Priority 4: More Galleys if enemy has galleys (0 extra keep buffer during a naval arms race
                    # so G3/G4 NEVER get skipped in favor of T2 when Port has 200-245 bread!)
                    g_race_keep = 0 if (naval_arms_race or en_g_live >= 1) else keep
                    if have_g < want_g and (en_galleys_recent + en_galleys_vis > 0) and self.galley_code and c.bread >= self.types[self.galley_code].cost + g_race_keep:
                        if self.build(c, self.galley_code):
                            have_g += 1
                            self.ever_built_galley = True
                            total_units += 1
                        continue
                    # Priority 5: 2nd Transport (Assault ship #1!) — hold off during naval arms race until >= 3G,
                    # and NEVER let Priority 5/6 build T1 before G2 when want_g2_before_t1 is active!
                    if have_t < 2 and have_t < want_t and not want_g2_before_t1 and (not naval_arms_race or have_g >= 3) and self.transport_code and budget >= self.types[self.transport_code].cost:
                        if self.build(c, self.transport_code):
                            have_t += 1
                            total_units += 1
                        continue
                    # Priority 6: 3rd+ Galley or 3rd Transport
                    if have_g < want_g and self.galley_code and budget >= self.types[self.galley_code].cost:
                        if self.build(c, self.galley_code):
                            have_g += 1
                            self.ever_built_galley = True
                            total_units += 1
                        continue
                    if have_t < want_t and not want_g2_before_t1 and self.transport_code and budget >= self.types[self.transport_code].cost:
                        if self.build(c, self.transport_code):
                            have_t += 1
                            total_units += 1
                        continue

            if self.occ.get(c.hex, 0) >= self.limit:
                continue

            # Save port bread for G1, T1, G2, and (during a 4-galley naval arms race!) G3/G4 before Infantry!
            if is_port and not blockaded and (have_t == 0 or (not self.ever_built_galley and home_sites_exist)):
                continue
            save_port_for_g = (
                is_port
                and not blockaded
                and (
                    (have_g < 2 and (s.round <= 28 or en_g_live >= have_g))
                    or (naval_arms_race and have_g < min(4, want_g))
                )
            )
            if save_port_for_g:
                g_cost = self.types[self.galley_code].cost if self.galley_code else 200
                if c.bread < g_cost:
                    home_wagons_now = sum(
                        1 for u in s.my_units()
                        if u.type == self.wagon_code and u.id not in self.ferry_wagons
                    )
                    # Build W2 once we have >= 2G (so home wheat site's 350+ wheat IMMEDIATELY funds G3+G4!),
                    # or after G1 if enemy doesn't already match/exceed our galleys and Port is > 20 bread away from G2.
                    can_build_w2_port = (have_g >= 2) or (en_g_live < max(1, have_g) and c.bread < g_cost - 20)
                    if can_build_w2_port and self.wagon_code and home_sites_exist and have_w < 2 and s.round >= 7 and c.bread >= self.types[self.wagon_code].cost:
                        if self.build(c, self.wagon_code):
                            have_w += 1
                            total_units += 1
                            self.occ[c.hex] = self.occ.get(c.hex, 0) + 1
                        continue
                    # If enemy ALREADY has live galleys matching/exceeding ours (or naval arms race!), save remaining Port bread for G2/G3/G4!
                    if en_g_live >= max(1, have_g) or naval_arms_race:
                        continue
                    # Never burn Port bread on Infantry before G2 during r<=16 (delays G2 by 5+ turns!).
                    if middle_contested and have_g >= 2 and s.round >= 14 and c.bread >= self.types[self.inf_code].cost:
                        if self.build(c, self.inf_code):
                            counts[self.inf_code] += 1
                            combat_now += 1
                            total_units += 1
                            self.occ[c.hex] = self.occ.get(c.hex, 0) + 1
                        continue
                continue

            # Home-site wagon (never build a replacement ferry wagon over infantry if middle is hostile and we already have a home wagon,
            # and only delay W2 at Home City if an ACTUAL home raid/invasion is active while home_caps_now <= 1!)
            home_wagons_now = sum(
                1 for u in s.my_units()
                if u.type == self.wagon_code and u.id not in self.ferry_wagons
            )
            hold_for_home_guard = (
                (not is_port)
                and self.land.get(c.hex) == self.home_land
                and home_caps_now <= 1
                and s.round >= 11
                and (inv_active or enemy_trans_near_home or enemy_on_home)
                and have_w >= 1
            )
            need_home_w = home_sites_exist and home_wagons_now == 0 and have_w < 2 and not hold_for_home_guard
            if self.wagon_code and not blockaded and not hold_for_home_guard and (need_home_w or (have_w < want_w and not middle_contested)):
                w_cost = self.types[self.wagon_code].cost
                if c.bread >= w_cost + (0 if (have_w == 0 or need_home_w) else keep):
                    if self.build(c, self.wagon_code):
                        have_w += 1
                        total_units += 1
                        self.occ[c.hex] = self.occ.get(c.hex, 0) + 1
                    continue

            if total_units >= food_cap and s.round < self.round_limit:
                continue
            # Do NOT burn Home City bread on extra early Infantry while Port still needs G1/G2/T1
            # and we already have >= 2 home capturers (let our wagons shuttle it to Port!).
            if (
                not is_port
                and not enemy_on_home
                and (have_g == 0 or (g_open_islets and (have_t == 0 or have_g < 2)))
                and home_caps_now >= 2
                and s.round <= 12
                and bool(home_ports)
                and not home_port_galley_blockaded
            ):
                continue

            combat_total = sum(
                n for k, n in counts.items()
                if not self.types[k].naval and not self.types[k].carry
            )
            cap_total = sum(n for k, n in counts.items() if self.types[k].capture)

            # Combined arms assault army: ~65% capturers (I/P) + ~35% Archers (A)!
            pick = None
            if cap_total * 10 < combat_total * 6 or combat_total < 5:
                if en_cav_seen >= 2 and budget >= self.types[self.pike_code].cost:
                    pick = self.pike_code
                elif budget >= self.types[self.inf_code].cost:
                    pick = self.inf_code
            elif self.archer_code and have_a * 3 < combat_total and budget >= self.types[self.archer_code].cost:
                pick = self.archer_code
            elif budget >= self.types[self.inf_code].cost:
                pick = self.inf_code

            if pick is not None:
                if self.build(c, pick):
                    counts[pick] += 1
                    if pick == self.archer_code:
                        have_a += 1
                    total_units += 1
                    self.occ[c.hex] = self.occ.get(c.hex, 0) + 1

    def _build_classic(self):
        s = self.state
        for c in s.my_cities():
            if c.built or c.hex in self.enemy_hex or self.occ.get(c.hex, 0) >= self.limit:
                continue
            if s.gold >= self.types[self.inf_code].cost:
                if self.build(c, self.inf_code):
                    self.occ[c.hex] = self.occ.get(c.hex, 0) + 1

    # ------------------------------------------------------------------
    # Main turn loop (Two-Pass Combat: Before & After Naval Operations!)
    # ------------------------------------------------------------------

    def on_turn(self, state):
        for entry in state.logs:
            if entry[0] == "HIT" and len(entry) >= 8:
                eid, code = int(entry[1]), entry[2]
                q, r, att_hp = int(entry[3]), int(entry[4]), int(entry[7])
                if att_hp > 0:
                    self.enemy_types_seen[eid] = code
                    self.ghosts[eid] = (bb.Unit(eid, 3 - self.player, code, q, r, att_hp), state.round - 1)
                    hl = self.land.get((q, r))
                    if hl in self.resource_isles() and not self.types[code].naval:
                        self.isle_enemies.setdefault(hl, {})[eid] = (code, state.round - 1)
                    if self.types[code].naval:
                        self.enemy_ships_seen[eid] = state.round - 1
                        if not self.types[code].transport:
                            self.enemy_galleys_seen[eid] = state.round - 1
                else:
                    if self.types[code].naval and not self.types[code].transport:
                        self.ever_killed_enemy_galley = True
                        self.killed_galley_ids.add(eid)
                        self.last_galley_kill_round = state.round - 1
                    if (not self.types[code].naval) and self.land.get((q, r)) == self.home_land:
                        if state.round <= self.home_invasion_alert_until + 2:
                            self.home_kills_during_alert += 1
                    self.ghosts.pop(eid, None)
                    self.enemy_ships_seen.pop(eid, None)
                    self.enemy_galleys_seen.pop(eid, None)
                    self.enemy_transports_seen.pop(eid, None)
                    for mem in self.isle_enemies.values():
                        mem.pop(eid, None)

        self.refresh()
        self.update_isle_memory()
        self.update_home_alert()
        vis = self.my_vision()
        for vh in vis:
            if vh in self.sea_hexes:
                self.mid_patrol_seen[vh] = self.state.round
        self.ghosts = {
            eid: (g, rnd) for eid, (g, rnd) in self.ghosts.items()
            if eid not in self.enemies and state.round - rnd < 8 and g.hex not in vis
        }

        try:
            danger = self.land_danger()
            # 1. Immediate captures in place
            self.capture_in_place()
            # 2. Protect critically hungry units from starving
            self.handle_starving_units(danger)
            # 3. Rush capturers onto empty cities (home port on Round 2!)
            self.rush_local_cities()
            # 4. Opening full-bread shuttle: load on Round 1 (or unload immediately if Port is owned & reachable!)
            if state.round == 1:
                self.run_opening_shuttle(danger)
            # 5. Combat Pass 1: units ALREADY ashore clear enemy wagons/defenders before ferries & landings!
            danger = self.land_danger()
            self.land_fight(danger)
            self.refresh()
            # 6. Naval operations: Galleys strike/blockade -> Ferries haul middle -> Assaults land!
            danger = self.land_danger()
            self.sea_turn(danger)
            self.update_home_alert()
            # 7. Combat Pass 2: newly landed troops & coastal archers strike immediately!
            danger = self.land_danger()
            self.land_fight(danger)
            self.refresh()
            # 8. Capture any cities cleared by combat!
            self.rush_local_cities()
            # 9. Advance land units (middle hills, coastal archers, enemy cities, staging)
            danger = self.land_danger()
            self.land_advance(danger)
            # 9b. Move opening bread shuttle AFTER land_advance on r2+ so W sees final troop positions
            # and avoids draining 40 bread into I1/I2 on the way to Port!
            if state.round > 1 and not self.opening_shuttle_done:
                self.run_opening_shuttle(danger)
            # 10. Run home-island & land wagons
            self.run_land_wagons(danger)
        except bb.GameOver:
            raise
        except Exception:
            traceback.print_exc()

        mid_sites_end = [x.hex for sites_e in self.resource_isles().values() for x in sites_e]
        for e in self.enemies.values():
            self.ghosts[e.id] = (bb.Unit(e.id, e.owner, e.type, e.q, e.r, e.hp, e.fort, e.carrier), state.round)
            if self.types[e.type].transport:
                self.enemy_transports_seen[e.id] = state.round
                self.enemy_transports_first_seen.setdefault(e.id, state.round)
                pax_end = state.passengers(e)
                cap_end = sum(1 for p in pax_end if self.types[p.type].capture)
                if state.round <= 9 and cap_end >= 2 and mid_sites_end and min(bb.distance(e.hex, sh) for sh in mid_sites_end) <= 7:
                    self.mid_loaded_t_seen[e.id] = (e.hex, state.round)
                has_w_end = any(self.types[p.type].carry for p in pax_end)
                is_a_end = (
                    len(pax_end) >= 3
                    and any(self.types[p.type].capture for p in pax_end)
                    and not has_w_end
                )
                pm_end = self.en_trans_mem.get(e.id)
                if pm_end and len(pm_end) >= 5 and pm_end[4] and len(pax_end) > 0:
                    is_a_end = True
                if is_a_end and state.round <= 11:
                    self.en_assault_t_ids.add(e.id)
                self.en_trans_mem[e.id] = (e.hex, e.hp, state.round, len(pax_end) > 0, is_a_end, has_w_end)
        if state.round >= 12 or self.home_kills_during_alert >= 2 or any(
            self.isle_known_capturers(i) >= 2 for i in self.resource_isles()
        ):
            self.en_assault_t_ids.clear()

        self.build_all()
        # Instant Round 3-4 Fresh-Transport Boarding: board our starting W1 + 2 I + S onto our freshly-built
        # T1 ON THE SAME TURN T1 IS BUILT (zero overnight Port ration drain + full MP to unload onto middle
        # on Round 4 — 1 full turn ahead of rivals!).
        # Hold Infantry at home if we spotted an un-sunk enemy assault transport (3+ combat units, 0 wagon) en route!
        en_aslt_fresh_block = (
            bool(self.en_assault_t_ids)
            and state.round <= 11
            and not self.ever_sank_enemy_transport
            and self.home_kills_during_alert == 0
        )
        if (
            self.naval
            and self.transport_code
            and state.round <= 9
            and self.resource_isles()
            and not self.home_invasion_active()
            and not en_aslt_fresh_block
            and not any(
                not e.carrier and not self.types[e.type].naval and self.land.get(e.hex) == self.home_land
                for e in self.enemies.values()
            )
        ):
            self.refresh()
            fresh_ts = [
                u for u in state.my_units()
                if u.type == self.transport_code and u.fresh
                and len(state.passengers(u)) < self.passengers_max
            ]
            if fresh_ts:
                ft = fresh_ts[0]
                cands_b = sorted(
                    (
                        u for u in state.my_units()
                        if not u.carrier and not u.fresh and not u.acted and u.mp > 0
                        and self.land.get(u.hex) == self.home_land
                        and not self.types[u.type].naval
                        and (not self.types[u.type].carry or (u.wheat == 0 and u.bread == 0))
                    ),
                    key=lambda u: (
                        0 if self.types[u.type].carry else (1 if self.types[u.type].capture else 2),
                        bb.distance(u.hex, ft.hex),
                        u.id,
                    ),
                )
                cap_boarded = sum(1 for p in state.passengers(ft) if self.types[p.type].capture)
                for u in cands_b:
                    if len(state.passengers(ft)) >= self.passengers_max:
                        break
                    if self.types[u.type].capture and cap_boarded >= 2:
                        continue
                    if self.board_transport(u, ft) and self.types[u.type].capture:
                        cap_boarded += 1
        if self.supply:
            bread = sum(c.bread for c in state.my_cities())
            ships = sum(1 for u in state.my_units() if self.types[u.type].naval)
            self.debug_text(
                "trident r%d: %d units (%d ships, %d ferries, %d assaults), %d bread" % (
                    state.round, len(state.my_units()), ships, len(self.ferries), len(self.assaults), bread
                )
            )


if __name__ == "__main__":
    bb.run(TridentBot())
