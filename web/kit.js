// @ts-check
// Bitbath kit for JavaScript: what a player knows, and the helpers to act on
// it. It is a port of the parts of kits/python/bitbath.py that the built-in
// bots (bots.js) and the play UI (play.js) need.
//
// A bot is a player object with two methods, which carry the protocol's own
// lines (SPEC §6), so a bot running somewhere else could stand behind the
// same two calls:
//
//   init(lines)        the init message, up to INIT
//   turn(lines, send)  a turn message, up to GO. send(line) runs one order
//                      or debug line and returns its reply lines, or null
//                      once the game is over. Returning ends the turn.
//
// Hexes are [q, r] arrays in axial coordinates (SPEC §3.1). Sets and maps of
// hexes are keyed by World.index.
//
// With the strategic ruleset on (world.supply, STRATEGIC.md) there is no
// gold: each city keeps its own bread (city.bread), which pays for what it
// builds. Sites (view.sites) have no owners: each grows wheat every round,
// which any wagon on it can load and carry home, and units carry rations. Ships sail on water by each terrain's naval_move_cost; a land unit
// boards a transport by moving onto it, and a unit aboard (unit.carrier)
// lands with a one-step move.
"use strict";

var BitbathKit = (function () {
  const DIRS = [[1, 0], [1, -1], [0, -1], [-1, 0], [-1, 1], [0, 1]];

  /** @param {number[]} h @param {number} d @returns {number[]} */
  function step(h, d) {
    return [h[0] + DIRS[d][0], h[1] + DIRS[d][1]];
  }

  /** @param {number[]} h */
  function neighbors(h) {
    return DIRS.map(([dq, dr]) => [h[0] + dq, h[1] + dr]);
  }

  /** @param {number[]} a @param {number[]} b */
  function distance(a, b) {
    const dq = a[0] - b[0];
    const dr = a[1] - b[1];
    return (Math.abs(dq) + Math.abs(dr) + Math.abs(dq + dr)) / 2;
  }

  /** The hexes a path of direction digits visits, starting with `from`. @param {number[]} from @param {string} dirs */
  function walk(from, dirs) {
    const out = [from];
    for (const ch of dirs) out.push(step(out[out.length - 1], Number(ch)));
    return out;
  }

  /** The map and rules: everything in the setup lines of SPEC §6.2. */
  class World {
    /** @param {string[]} lines an init message or a replay header's setup */
    constructor(lines) {
      this.player = 0;
      this.width = 0;
      this.height = 0;
      /** @type {string[]} */ this.rows = [];
      /** Cities as they start: id, hex, and owner. @type {{id: number, q: number, r: number, owner: number}[]} */
      this.cities = [];
      /** Resource sites as they start, with supply on. @type {{id: number, q: number, r: number, owner: number, output: number, cap: number}[]} */
      this.sites = [];
      /** @type {Record<string, any>} */ this.terrains = {};
      /** Unit types by code, in the engine's order. @type {Record<string, any>} */
      this.unitTypes = {};
      /** Base damage: damage[attacker][defender]. @type {Record<string, Record<string, number>>} */
      this.damage = {};
      /** Rule constants, such as c.stack_limit. @type {Record<string, number>} */
      this.c = {};
      for (const line of lines) {
        const t = line.trim().split(/\s+/);
        const n = t.map(Number);
        if (t[0] === "PLAYER") this.player = n[1];
        else if (t[0] === "MAP") [this.width, this.height] = [n[1], n[2]];
        else if (t[0] === "ROW") this.rows.push(t[1]);
        else if (t[0] === "CITY") this.cities.push({ id: n[1], q: n[2], r: n[3], owner: n[4] });
        else if (t[0] === "SITE") this.sites.push({ id: n[1], q: n[2], r: n[3], owner: n[4], output: n[5], cap: n[6] });
        else if (t[0] === "TERRAIN") {
          // naval_move_cost is what entering costs a ship; 0: ships can't.
          this.terrains[t[1]] = { ch: t[1], name: t[2], move_cost: n[3], defense: n[4], vision_bonus: n[5], conceal: n[6] || 0, naval_move_cost: n[7] || 0 };
        } else if (t[0] === "UNITTYPE") {
          const flags = t[9] === "-" ? [] : t[9].split(",");
          this.unitTypes[t[1]] = {
            code: t[1], name: t[2], cost: n[3], max_hp: n[4], move: n[5], vision: n[6], range_min: n[7], range_max: n[8],
            capture: flags.includes("capture"), zoc: flags.includes("zoc"),
            skirmish: flags.includes("skirmish"), no_move_attack: flags.includes("no_move_attack"),
            fortify: flags.includes("fortify"),
            // The strategic ruleset's: carries wheat and bread, moves on water, carries land units.
            carry: flags.includes("carry"), naval: flags.includes("naval"), transport: flags.includes("transport"),
          };
        } else if (t[0] === "DAMAGE") {
          const codes = Object.keys(this.unitTypes);
          this.damage[t[1]] = {};
          n.slice(2).forEach((d, i) => (this.damage[t[1]][codes[i]] = d));
        } else if (t[0] === "RULE") this.c[t[1]] = n[2];
      }
    }

    /** A hex's place in the map's rows, or -1 if it is off the map. @param {number[]} h */
    index(h) {
      const col = h[0] + Math.floor(h[1] / 2);
      return h[1] >= 0 && h[1] < this.height && col >= 0 && col < this.width ? h[1] * this.width + col : -1;
    }

    /** The hex at an index. @param {number} i @returns {number[]} */
    hex(i) {
      const r = Math.floor(i / this.width);
      return [(i % this.width) - Math.floor(r / 2), r];
    }

    /** The terrain at a hex, or null off the map. @param {number[]} h */
    terrain(h) {
      const i = this.index(h);
      return i < 0 ? null : this.terrains[this.rows[h[1]][i % this.width]] || null;
    }

    /** Whether the strategic ruleset's wheat, bread, and rations are on. */
    get supply() {
      return !!this.c.supply;
    }

    /** Whether land units can enter h. @param {number[]} h */
    passable(h) {
      const t = this.terrain(h);
      return !!t && t.move_cost > 0;
    }

    /** Whether ships can enter h. @param {number[]} h */
    navigable(h) {
      const t = this.terrain(h);
      return !!t && t.naval_move_cost > 0;
    }

    /** What entering h costs a ship, or a land unit. 0: it can't. @param {number[]} h @param {boolean} naval */
    cost(h, naval) {
      const t = this.terrain(h);
      return !t ? 0 : naval ? t.naval_move_cost : t.move_cost;
    }

    /** Whether a city on h would be a port, which builds ships: one next to water. @param {number[]} h */
    isPort(h) {
      return neighbors(h).some((n) => this.navigable(n));
    }

    /** The hex's image under the map's 180° symmetry. @param {number[]} h */
    mirror(h) {
      return this.hex(this.width * this.height - 1 - this.index(h));
    }
  }

  class Unit {
    /** @param {number} id @param {number} owner @param {string} type @param {number} q @param {number} r @param {number} hp @param {number} [fort] */
    constructor(id, owner, type, q, r, hp, fort = 0) {
      this.id = id;
      this.owner = owner;
      this.type = type;
      this.q = q;
      this.r = r;
      this.hp = hp;
      /** Fortification level; moving resets it to 0. */ this.fort = fort;
      // With supply on: the transport it is aboard, or 0, the wheat and
      // bread a wagon carries, and the rations left. A unit aboard has its
      // transport's hex.
      this.carrier = 0;
      this.wheat = 0;
      this.bread = 0;
      this.rations = 0;
      // Known for your own units during your turn.
      this.mp = 0;
      this.acted = false;
      this.moved = false;
      /** Built this turn. */ this.fresh = false;
    }

    get hex() {
      return [this.q, this.r];
    }
  }

  class City {
    /** @param {number} id @param {number} q @param {number} r @param {number} owner */
    constructor(id, q, r, owner) {
      this.id = id;
      this.q = q;
      this.r = r;
      this.owner = owner;
      this.capturer = 0;
      this.progress = 0;
      /** Built a unit or was captured this turn, so it can't build. */ this.built = false;
      /** With supply on: its stockpile. 0 while the player can't see the city. */ this.bread = 0;
    }

    get hex() {
      return [this.q, this.r];
    }
  }

  /**
   * A resource site, with supply on. Sites have no owners: each grows
   * `output` wheat at the start of every round, up to `cap`, and any wagon on
   * it can load the wheat. Its wheat is 0 while the player can't see it.
   * `owner`, `capturer`, and `progress` stay 0; the protocol keeps them so
   * that SITE lines didn't change shape.
   */
  class Site {
    /** @param {number} id @param {number} q @param {number} r @param {number} owner @param {number} output @param {number} cap */
    constructor(id, q, r, owner, output, cap) {
      this.id = id;
      this.q = q;
      this.r = r;
      this.owner = owner;
      this.output = output;
      this.cap = cap;
      this.capturer = 0;
      this.progress = 0;
      this.wheat = 0;
    }

    get hex() {
      return [this.q, this.r];
    }
  }

  /** What a player knows during their turn: their own units and cities, and the enemy units they can see. */
  class View {
    /** @param {World} world @param {number} player */
    constructor(world, player) {
      this.player = player;
      this.round = 0;
      this.gold = 0;
      /** Indexed by city id. */
      this.cities = world.cities.map((c) => new City(c.id, c.q, c.r, c.owner));
      /** Indexed by site id; empty without supply. */
      this.sites = world.sites.map((s) => new Site(s.id, s.q, s.r, s.owner, s.output, s.cap));
      /** @type {Map<number, Unit>} */ this.units = new Map();
      /** LOG lines as token lists, such as ["HIT", "91", "K", ...]. @type {string[][]} */
      this.logs = [];
    }

    /**
     * The view a turn message describes (SPEC §6.3).
     * @param {World} world @param {number} player @param {string[]} lines
     */
    static fromTurn(world, player, lines) {
      const view = new View(world, player);
      for (const line of lines) {
        const t = line.trim().split(/\s+/);
        const n = t.map(Number);
        if (t[0] === "TURN") view.round = n[1];
        else if (t[0] === "GOLD") view.gold = n[1];
        else if (t[0] === "CITY") Object.assign(view.cities[n[1]], { owner: n[2], capturer: n[3], progress: n[4], bread: n[5] || 0 });
        else if (t[0] === "SITE") Object.assign(view.sites[n[1]], { owner: n[2], capturer: n[3], progress: n[4], wheat: n[5] || 0 });
        else if (t[0] === "UNIT") {
          const u = new Unit(n[1], n[2], t[3], n[4], n[5], n[6], n[7] || 0);
          Object.assign(u, { carrier: n[8] || 0, wheat: n[9] || 0, bread: n[10] || 0, rations: n[11] || 0 });
          if (u.owner === player) u.mp = world.unitTypes[u.type].move;
          view.units.set(u.id, u);
        } else if (t[0] === "LOG") view.logs.push(t.slice(1));
      }
      return view;
    }

    /**
     * What `player` knows in a state from the viewer's engine (the JSON of
     * wasm/src/lib.rs), which holds both sides.
     * @param {World} world @param {any} state @param {number} player
     */
    static fromState(world, state, player) {
      const view = new View(world, player);
      const sees = (/** @type {number[]} */ h) => (Number(state.vis[world.index(h)]) & player) !== 0;
      view.round = state.round;
      view.gold = state.gold[player - 1];
      state.cities.forEach((/** @type {number[]} */ c, /** @type {number} */ i) => {
        const city = view.cities[i];
        city.owner = c[0];
        if (sees(city.hex)) [city.capturer, city.progress, city.bread] = [c[1], c[2], c[4] || 0];
        city.built = !!c[3];
      });
      (state.sites || []).forEach((/** @type {number[]} */ s, /** @type {number} */ i) => {
        const site = view.sites[i];
        site.owner = s[0];
        if (sees(site.hex)) [site.capturer, site.progress, site.wheat] = [s[1], s[2], s[3]];
      });
      for (const [id, owner, type, q, r, hp, mp, flags, fort, wheat, bread, rations, carrier] of state.units) {
        if (owner !== player && !sees([q, r])) continue;
        const u = new Unit(id, owner, type, q, r, hp, fort || 0);
        Object.assign(u, { wheat: wheat || 0, bread: bread || 0, rations: rations || 0, carrier: carrier || 0 });
        if (owner === player) Object.assign(u, { mp, acted: !!(flags & 1), moved: !!(flags & 2), fresh: !!(flags & 4) });
        view.units.set(id, u);
      }
      return view;
    }

    myUnits() {
      return Array.from(this.units.values()).filter((u) => u.owner === this.player);
    }

    enemyUnits() {
      return Array.from(this.units.values()).filter((u) => u.owner !== this.player);
    }

    myCities() {
      return this.cities.filter((c) => c.owner === this.player);
    }

    /** @param {number[]} h */
    siteAt(h) {
      return this.sites.find((s) => s.q === h[0] && s.r === h[1]) || null;
    }

    /** The units aboard a transport. @param {Unit} transport */
    passengers(transport) {
      return Array.from(this.units.values()).filter((u) => u.carrier === transport.id);
    }

    /** @param {number[]} h */
    unitsAt(h) {
      return Array.from(this.units.values()).filter((u) => u.q === h[0] && u.r === h[1]);
    }

    /** @param {number[]} h */
    cityAt(h) {
      return this.cities.find((c) => c.q === h[0] && c.r === h[1]) || null;
    }

    /** Whether a visible enemy unit is on h. @param {number[]} h */
    enemyAt(h) {
      return this.unitsAt(h).some((u) => u.owner !== this.player);
    }

    /** How many of the player's units stand on h, as the stack limit counts them: not those aboard transports. @param {number[]} h @param {Unit} [excluding] */
    friendsAt(h, excluding) {
      return this.unitsAt(h).filter((u) => u.owner === this.player && !u.carrier && u !== excluding).length;
    }
  }

  /** A queue of [cost, hex index] pairs that hands them back cheapest first. Costs are small whole numbers. */
  class Buckets {
    constructor() {
      /** @type {number[][]} */ this.at = [];
      this.cost = 0;
      this.size = 0;
    }

    /** @param {number} cost @param {number} i */
    push(cost, i) {
      (this.at[cost] = this.at[cost] || []).push(i);
      this.size++;
    }

    /** @returns {[number, number]} */
    pop() {
      while (!this.at[this.cost] || !this.at[this.cost].length) this.cost++;
      this.size--;
      return [this.cost, /** @type {number} */ (this.at[this.cost].pop())];
    }
  }

  /** The helpers that work out what a player can do, from a world and that player's view. */
  class Kit {
    /** @param {World} world @param {View} view */
    constructor(world, view) {
      this.world = world;
      this.view = view;
    }

    get player() {
      return this.view.player;
    }

    /** @param {Unit} unit */
    unitType(unit) {
      return this.world.unitTypes[unit.type];
    }

    /** @param {Unit} unit */
    canAct(unit) {
      return unit.owner === this.player && !unit.fresh && !unit.acted;
    }

    /** The city unit stands on, if it could be captured: one that isn't the player's. Sites have no owners, so no one captures them. @param {Unit} unit */
    prize(unit) {
      const city = this.view.cityAt(unit.hex);
      return city && city.owner !== this.player ? city : null;
    }

    /** Whether unit can capture the city it stands on right now. @param {Unit} unit */
    canCapture(unit) {
      return this.canAct(unit) && this.unitType(unit).capture && !unit.carrier && !!this.prize(unit);
    }

    /** Whether unit can attack this turn, if a target is in range. A unit aboard a transport can't. @param {Unit} unit */
    canAttack(unit) {
      return this.canAct(unit) && !unit.carrier && !(this.unitType(unit).no_move_attack && unit.moved);
    }

    /**
     * Visible enemy units that unit could attack from `from` (default: where
     * it is): in range, not aboard a transport, and hurt by it.
     * @param {Unit} unit @param {number[]} [from]
     */
    targetsInRange(unit, from) {
      const type = this.unitType(unit);
      const at = from || unit.hex;
      const hurts = this.world.damage[unit.type] || {};
      return this.view.enemyUnits().filter((e) => {
        const d = distance(at, e.hex);
        return type.range_min <= d && d <= type.range_max && !e.carrier && hurts[e.type] > 0;
      });
    }

    /** Hexes next to a visible enemy unit that exerts a zone of control. Units aboard transports exert none. @returns {Set<number>} */
    zocHexes() {
      const out = new Set();
      for (const e of this.view.enemyUnits()) {
        if (this.world.unitTypes[e.type].zoc && !e.carrier) for (const n of neighbors(e.hex)) out.add(this.world.index(n));
      }
      out.delete(-1);
      return out;
    }

    // Ships and wagons (STRATEGIC.md §4, §6).

    /**
     * Where a port puts a new ship: the first hex next to it, in direction
     * order, that ships can enter, with room and no visible enemy. Null if
     * there is none. A hidden enemy there makes the engine pick the next.
     * @param {City} city @returns {number[] | null}
     */
    launchHex(city) {
      for (const n of neighbors(city.hex)) {
        if (this.world.navigable(n) && !this.view.enemyAt(n) && this.view.friendsAt(n) < this.world.c.stack_limit) return n;
      }
      return null;
    }

    /**
     * The player's lowest-numbered transport on h with room for another
     * unit, which is the one a unit boarding on h gets aboard, or null.
     * @param {number[]} h @returns {Unit | null}
     */
    transportWithRoom(h) {
      const most = this.world.c.passengers_max || 0;
      const mine = this.view.unitsAt(h).filter((u) => u.owner === this.player && this.unitType(u).transport).sort((a, b) => a.id - b.id);
      return mine.find((t) => this.view.passengers(t).length < most) || null;
    }

    /** Whether unit is a wagon that can give L and U orders now: one that may act and isn't aboard a transport. @param {Unit} unit */
    canCarry(unit) {
      return this.canAct(unit) && this.unitType(unit).carry && !unit.carrier;
    }

    /**
     * What a wagon could load where it stands: the bread of the player's
     * city, or the wheat of any site, as much as fits. 0 if nothing.
     * @param {Unit} unit
     */
    loadable(unit) {
      if (!this.canCarry(unit)) return 0;
      const room = Math.max(0, (this.world.c.cargo_max || 0) - unit.wheat - unit.bread);
      const city = this.view.cityAt(unit.hex);
      if (city) return city.owner === this.player ? Math.min(city.bread, room) : 0;
      const site = this.view.siteAt(unit.hex);
      return site ? Math.min(site.wheat, room) : 0;
    }

    /** What a wagon could unload where it stands: its wheat and bread, if it is on a city of the player's. @param {Unit} unit */
    unloadable(unit) {
      const city = this.view.cityAt(unit.hex);
      return this.canCarry(unit) && city && city.owner === this.player ? unit.wheat + unit.bread : 0;
    }

    /**
     * Where unit can end a move this turn, as a map from hex index to the
     * path's direction digits. Honors move costs (naval ones for ships), its
     * MP, visible enemies (which block hexes and exert zones of control),
     * and the stack limit on the final hex. A land unit can also end its move
     * by boarding one of the player's transports with room, for 1 MP, and a
     * unit aboard can only land, one step onto land. Hidden enemies can
     * still cut a move short.
     * @param {Unit} unit @param {Set<number>} [zoc] @returns {Map<number, string>}
     */
    reachable(unit, zoc) {
      /** @type {Map<number, string>} */
      const result = new Map();
      if (!this.canAct(unit) || unit.mp <= 0) return result;
      const world = this.world;
      const type = this.unitType(unit);
      const naval = !!type.naval;
      const skirmish = type.skirmish;
      const zone = zoc || this.zocHexes();
      /** @type {Set<number>} */ const enemies = new Set();
      /** @type {Map<number, number>} */ const friends = new Map();
      /** The hexes with a transport to board. @type {Set<number>} */ const ships = new Set();
      for (const u of this.view.units.values()) {
        const i = world.index(u.hex);
        if (u.owner !== this.player) enemies.add(i);
        else if (u !== unit && !u.carrier) {
          friends.set(i, (friends.get(i) || 0) + 1);
          if (!naval && this.unitType(u).transport && this.transportWithRoom(u.hex)) ships.add(i);
        }
      }
      const start = world.index(unit.hex);
      if (unit.carrier) {
        for (let d = 0; d < 6; d++) {
          const n = step(unit.hex, d);
          const ni = world.index(n);
          const t = world.terrain(n);
          if (t && t.move_cost > 0 && t.move_cost <= unit.mp && !enemies.has(ni) && (friends.get(ni) || 0) < world.c.stack_limit) result.set(ni, String(d));
        }
        return result;
      }
      ships.delete(start);
      const best = new Map([[start, 0]]);
      const paths = new Map([[start, ""]]);
      const queue = new Buckets();
      queue.push(0, start);
      while (queue.size) {
        const [cost, i] = queue.pop();
        if (best.get(i) !== cost) continue;
        const path = /** @type {string} */ (paths.get(i));
        if (i !== start) {
          if ((friends.get(i) || 0) < world.c.stack_limit) result.set(i, path);
          if (!skirmish && zone.has(i)) continue; // entering a zone of control ends the move
        }
        const h = world.hex(i);
        for (let d = 0; d < 6; d++) {
          const n = step(h, d);
          const ni = world.index(n);
          const t = world.terrain(n);
          if (!t || enemies.has(ni)) continue;
          const enter = naval ? t.naval_move_cost : t.move_cost;
          if (!enter) {
            // Boarding: the last step of a move, for 1 MP.
            const was = best.get(ni);
            if (ships.has(ni) && cost + 1 <= unit.mp && (was === undefined || cost + 1 < was)) {
              best.set(ni, cost + 1);
              result.set(ni, path + d);
            }
            continue;
          }
          const c = cost + enter;
          const old = best.get(ni);
          if (c <= unit.mp && (old === undefined || c < old)) {
            best.set(ni, c);
            paths.set(ni, path + d);
            queue.push(c, ni);
          }
        }
      }
      return result;
    }

    /**
     * Move cost from every hex to the nearest goal, as a map from hex index
     * to cost. Costs count the terrain of each hex entered, ignoring MP and
     * zones of control. Blocked hexes (default: visible enemies that aren't
     * goals) can't be passed through.
     * @param {number[][]} goals @param {Set<number>} [blocked] @returns {Map<number, number>}
     */
    distanceField(goals, blocked) {
      const world = this.world;
      const goal = new Set(goals.map((g) => world.index(g)));
      const block = blocked || new Set(this.view.enemyUnits().map((e) => world.index(e.hex)).filter((i) => !goal.has(i)));
      /** @type {Map<number, number>} */
      const dist = new Map();
      const queue = new Buckets();
      for (const g of Array.from(goal).sort((a, b) => a - b)) {
        if (g >= 0 && world.passable(world.hex(g))) {
          dist.set(g, 0);
          queue.push(0, g);
        }
      }
      while (queue.size) {
        const [cost, i] = queue.pop();
        if (dist.get(i) !== cost || (block.has(i) && !goal.has(i))) continue;
        const h = world.hex(i);
        const c = cost + /** @type {any} */ (world.terrain(h)).move_cost;
        for (const n of neighbors(h)) {
          const ni = world.index(n);
          if (ni < 0 || block.has(ni) || !world.passable(n)) continue;
          const old = dist.get(ni);
          if (old === undefined || c < old) {
            dist.set(ni, c);
            queue.push(c, ni);
          }
        }
      }
      return dist;
    }

    /**
     * Damage a unit of attType with attHp HP deals to a defType unit on defHex
     * that has dug in defFort levels (SPEC §3.7).
     * @param {string} attType @param {number} attHp @param {string} defType @param {number[]} defHex @param {number} [defFort]
     */
    damage(attType, attHp, defType, defHex, defFort = 0) {
      const base = this.world.damage[attType][defType];
      if (!base) return 0;
      const fortified = defFort * (this.world.c.fortify_pct || 0);
      const defense = Math.min(100, /** @type {any} */ (this.world.terrain(defHex)).defense + fortified);
      return Math.max(1, Math.floor((base * attHp * (100 - defense)) / (this.world.unitTypes[attType].max_hp * 100)));
    }

    /**
     * [defender HP, attacker HP] after attacker attacks defender from `from`
     * (default: where it stands), matching the engine exactly. An attacker
     * that moves there first loses its fortification.
     * @param {Unit} attacker @param {Unit} defender @param {number[]} [from] @returns {[number, number]}
     */
    predict(attacker, defender, from) {
      const at = from || attacker.hex;
      const attFort = at[0] === attacker.q && at[1] === attacker.r ? attacker.fort : 0;
      const defHp = Math.max(0, defender.hp - this.damage(attacker.type, attacker.hp, defender.type, defender.hex, defender.fort));
      let attHp = attacker.hp;
      const type = this.world.unitTypes[defender.type];
      const d = distance(at, defender.hex);
      if (defHp > 0 && type.range_min <= d && d <= type.range_max && this.world.damage[defender.type][attacker.type] > 0) {
        attHp = Math.max(0, attHp - this.damage(defender.type, defHp, attacker.type, at, attFort));
      }
      return [defHp, attHp];
    }
  }

  /** An order's result. `ok` is true for OK and PART. For ERR, `code` holds the error code, such as "path". */
  class Reply {
    /** @param {string} line */
    constructor(line) {
      const t = line.trim().split(/\s+/);
      this.line = line;
      this.kind = t[0] || "ERR";
      this.ok = this.kind === "OK" || this.kind === "PART";
      this.values = t.slice(1);
      this.code = this.kind === "ERR" ? t[1] || "" : "";
    }
  }

  const GAME_OVER = new Error("the game is over");

  /**
   * A bot that plays in the page. Subclass it and override onTurn. `world`
   * is set before onInit runs, and `view` before each onTurn. Each order
   * returns a Reply and updates `view` when it works.
   */
  class Bot extends Kit {
    constructor() {
      super(/** @type {any} */ (null), /** @type {any} */ (null));
      /** @type {(line: string) => string[] | null} */
      this.send = () => null;
    }

    onInit() {}

    /** @param {View} view */
    onTurn(view) {}

    /** @param {string[]} lines */
    init(lines) {
      this.world = new World(lines);
      this.onInit();
    }

    /** @param {string[]} lines @param {(line: string) => string[] | null} send */
    turn(lines, send) {
      this.view = View.fromTurn(this.world, this.world.player, lines);
      this.send = send;
      try {
        this.onTurn(this.view);
      } catch (e) {
        // A bug costs the bot the rest of its turn, not the game.
        if (e !== GAME_OVER) console.error(e);
      }
    }

    /** Sends an order and returns its result, taking in the units it revealed. @param {string} line */
    order(line) {
      const reply = this.send(line);
      if (!reply) throw GAME_OVER;
      for (const l of reply) {
        const t = l.split(/\s+/);
        if (t[0] === "SEE") this.view.units.set(Number(t[1]), new Unit(Number(t[1]), Number(t[2]), t[3], Number(t[4]), Number(t[5]), Number(t[6]), Number(t[7]) || 0));
      }
      return new Reply(reply[reply.length - 1] || "ERR");
    }

    /** @param {Unit} unit @param {string} dirs */
    move(unit, dirs) {
      const reply = this.order("M " + unit.id + " " + dirs);
      if (reply.ok) {
        const [q, r] = [Number(reply.values[0]), Number(reply.values[1])];
        if (q !== unit.q || r !== unit.r) {
          Object.assign(unit, { moved: true, fort: 0 });
          // A unit aboard has landed; a land unit now on water has boarded.
          if (unit.carrier) unit.carrier = 0;
          else if (!this.unitType(unit).naval && !this.world.passable([q, r])) {
            const boarded = this.transportWithRoom([q, r]);
            if (boarded) unit.carrier = boarded.id;
          }
          for (const p of this.view.passengers(unit)) [p.q, p.r] = [q, r];
        }
        [unit.q, unit.r] = [q, r];
        unit.mp = reply.kind === "OK" ? Number(reply.values[2]) : 0;
      }
      return reply;
    }

    /** @param {Unit} unit @param {Unit} target */
    attack(unit, target) {
      const reply = this.order("A " + unit.id + " " + target.id);
      if (reply.ok) {
        [target.hp, unit.hp] = [Number(reply.values[0]), Number(reply.values[1])];
        unit.acted = true;
        unit.mp = 0;
        for (const u of [target, unit]) {
          if (u.hp > 0) continue;
          // A transport sinks with everything aboard.
          for (const p of this.view.passengers(u)) this.view.units.delete(p.id);
          this.view.units.delete(u.id);
        }
      }
      return reply;
    }

    /** @param {Unit} unit */
    capture(unit) {
      const reply = this.order("C " + unit.id);
      const city = this.view.cityAt(unit.hex);
      const place = city || this.view.siteAt(unit.hex);
      if (reply.ok && place) {
        unit.acted = true;
        if (reply.values[0] === "captured") {
          Object.assign(place, { owner: this.player, capturer: 0, progress: 0 });
          if (city) city.built = true;
        } else Object.assign(place, { capturer: this.player, progress: Number(reply.values[0]) });
      }
      return reply;
    }

    /** @param {City} city @param {string} code */
    build(city, code) {
      const type = this.world.unitTypes[code];
      // A port puts a ship on its launch hex.
      const at = (type && type.naval && this.launchHex(city)) || city.hex;
      const reply = this.order("B " + city.id + " " + code);
      if (reply.ok) {
        const unit = new Unit(Number(reply.values[0]), this.player, code, at[0], at[1], type.max_hp);
        unit.fresh = true;
        unit.rations = this.world.supply ? this.world.c.ration_max || 0 : 0;
        this.view.units.set(unit.id, unit);
        // With supply on the city pays, from its own bread.
        if (this.world.supply) city.bread -= type.cost;
        else this.view.gold -= type.cost;
        city.built = true;
      }
      return reply;
    }

    /**
     * Moves unit to the hex it can reach this turn that is closest to the
     * goals of a distanceField. Returns the Reply, or null if no reachable
     * hex is closer than where it stands.
     * @param {Unit} unit @param {Map<number, number>} field @param {Set<number>} [zoc]
     */
    moveToward(unit, field, zoc) {
      const here = field.get(this.world.index(unit.hex));
      /** @type {null | {d: number, dirs: string, i: number}} */
      let best = null;
      for (const [i, dirs] of this.reachable(unit, zoc)) {
        const d = field.get(i);
        if (d === undefined || (here !== undefined && d >= here)) continue;
        if (!best || d < best.d || (d === best.d && (dirs.length < best.dirs.length || (dirs.length === best.dirs.length && i < best.i)))) {
          best = { d, dirs, i };
        }
      }
      return best ? this.move(unit, best.dirs) : null;
    }

    // Debug annotations, drawn by the viewer (SPEC §8).

    /** @param {number[]} h @param {string} color @param {string} [label] */
    debugHex(h, color, label) {
      this.send(("D HEX " + h[0] + " " + h[1] + " " + color + " " + (label || "")).trim());
    }

    /** @param {number[]} a @param {number[]} b @param {string} color */
    debugLine(a, b, color) {
      this.send("D LINE " + a[0] + " " + a[1] + " " + b[0] + " " + b[1] + " " + color);
    }

    /** @param {string} text */
    debugText(text) {
      this.send("D TEXT " + text);
    }
  }

  return { DIRS, step, neighbors, distance, walk, World, Unit, City, Site, View, Kit, Bot, Reply };
})();

if (typeof module !== "undefined") module.exports = BitbathKit;
