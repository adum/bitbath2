// @ts-check
// The bots the play UI (play.js) offers: those of the bot index,
// bots/index.json, that it can run. Random and Greedy are ports of Python
// reference bots, written here against the JavaScript kit (kit.js). The
// others run in a worker (reference-opponent.js).
/* global ReferenceOpponent */
"use strict";

var BitbathBots = (function () {
  /** @type {any} */
  const Kit = typeof BitbathKit !== "undefined" ? BitbathKit : require("./kit.js");
  /**
   * What the page needs to know of its bots, from the bot index and the
   * ratings (bots/index.json and bots/ratings.json). A web build
   * (scripts/build_web_bots.py) writes it into bot-index.js: `bots`, the
   * page's bots, and `ladders`, each ladder's ratings by bot id.
   * @type {{
   *   bots: {id: string, name: string, about: string, rules: string[], maps: string[] | "any", play_page: string}[],
   *   ladders: Record<string, {map: string, rules: string, date: string, anchor: string | null,
   *     ratings: Record<string, {rating: number, interval: number[] | null, games: number, stale: boolean}>}>,
   * }}
   */
  const Index = typeof BitbathBotIndex !== "undefined" ? BitbathBotIndex : require("./bot-index.js");

  /** A small seeded random number generator (mulberry32). @param {number} seed @returns {() => number} */
  function rng(seed) {
    let a = seed >>> 0;
    return () => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /** Legal orders only, picked at random: a port of bots/random_bot.py. */
  class RandomBot extends Kit.Bot {
    /** @param {number} seed */
    constructor(seed) {
      super();
      this.random = rng(seed);
    }

    /** @param {any[]} list */
    pick(list) {
      return list[Math.floor(this.random() * list.length)];
    }

    /** @param {any} view */
    onTurn(view) {
      const units = view.myUnits();
      for (let i = units.length - 1; i > 0; i--) {
        const j = Math.floor(this.random() * (i + 1));
        [units[i], units[j]] = [units[j], units[i]];
      }
      for (const unit of units) {
        if (!view.units.has(unit.id) || !this.canAct(unit)) continue;
        if (this.canCapture(unit) && this.random() < 0.9) {
          this.capture(unit);
          continue;
        }
        let targets = this.targetsInRange(unit);
        if (targets.length && this.random() < 0.7) {
          this.attack(unit, this.pick(targets));
          continue;
        }
        const reach = Array.from(this.reachable(unit).values());
        if (reach.length && this.random() < 0.9) this.move(unit, this.pick(reach));
        if (!view.units.has(unit.id) || !this.canAct(unit)) continue;
        if (this.canCapture(unit)) {
          this.capture(unit);
        } else if (this.canAttack(unit)) {
          targets = this.targetsInRange(unit);
          if (targets.length) this.attack(unit, this.pick(targets));
        }
      }
      const codes = Object.keys(this.world.unitTypes);
      for (const city of view.myCities()) {
        const code = this.pick(codes);
        const room = view.friendsAt(city.hex) < this.world.c.stack_limit && !view.enemyAt(city.hex);
        if (room && this.random() < 0.6 && view.gold >= this.world.unitTypes[code].cost) this.build(city, code);
      }
    }
  }

  /**
   * Expands to the nearest cities and attacks whatever it can reach, when
   * the trade looks favorable: a port of bots/greedy_bot.py.
   */
  class GreedyBot extends Kit.Bot {
    onInit() {
      this.builds = 0;
      const home = this.world.cities.find((/** @type {any} */ c) => c.owner === this.world.player);
      this.enemyHome = this.world.mirror([home.q, home.r]);
    }

    /** Units that can still act, with any that can't attack after moving first. */
    ready() {
      const units = this.view.myUnits().filter((/** @type {any} */ u) => this.canAct(u));
      const late = (/** @type {any} */ u) => (this.unitType(u).no_move_attack ? 0 : 1);
      return units.sort((/** @type {any} */ a, /** @type {any} */ b) => late(a) - late(b) || a.id - b.id);
    }

    /** What losing `lost` HP costs a unit of this type. @param {any} type @param {number} lost @param {boolean} dies */
    value(type, lost, dies) {
      return (type.cost * lost) / type.max_hp + (dies ? type.cost : 0);
    }

    /** Makes the attack with the best expected trade, if any is favorable. @param {any} unit */
    attackBest(unit) {
      if (!this.view.units.has(unit.id) || !this.canAttack(unit)) return;
      const type = this.unitType(unit);
      let best = null;
      let bestScore = 0;
      for (const enemy of this.targetsInRange(unit)) {
        const [defHp, attHp] = this.predict(unit, enemy);
        const score = this.value(this.unitType(enemy), enemy.hp - defHp, defHp === 0) - this.value(type, unit.hp - attHp, attHp === 0);
        if (score > bestScore) [best, bestScore] = [enemy, score];
      }
      if (best) this.attack(unit, best);
    }

    /** After a move: capture if standing on a city, otherwise attack. @param {any} unit */
    finish(unit) {
      if (!this.view.units.has(unit.id)) return;
      if (this.canCapture(unit)) this.capture(unit);
      else this.attackBest(unit);
    }

    /** @param {any} view */
    onTurn(view) {
      const world = this.world;
      for (const unit of this.ready()) this.finish(unit);

      // Capturers claim the nearest city we don't own, one unit per city
      // first; the rest head for their nearest such city anyway.
      const targets = view.cities.filter((/** @type {any} */ c) => c.owner !== this.player);
      /** @type {Map<number, Map<number, number>>} */
      const fields = new Map(targets.map((/** @type {any} */ c) => [c.id, this.distanceField([c.hex])]));
      const movers = this.ready().filter((/** @type {any} */ u) => u.mp > 0);
      const capturers = movers.filter((/** @type {any} */ u) => this.unitType(u).capture);
      const pairs = [];
      for (const u of capturers) {
        for (const c of targets) {
          const d = /** @type {any} */ (fields.get(c.id)).get(world.index(u.hex));
          if (d !== undefined) pairs.push([d, u.id, c.id]);
        }
      }
      pairs.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]);
      /** @type {Map<number, number>} */ const assigned = new Map();
      const claimed = new Set();
      for (const [, uid, cid] of pairs) {
        if (!assigned.has(uid) && !claimed.has(cid)) {
          assigned.set(uid, cid);
          claimed.add(cid);
        }
      }
      for (const [, uid, cid] of pairs) if (!assigned.has(uid)) assigned.set(uid, cid);
      // Debug annotations show the plan in the viewer once the game is over.
      this.debugText("round " + view.round + ": " + assigned.size + " capturers heading for " + claimed.size + " cities");
      for (const cid of claimed) this.debugHex(view.cities[cid].hex, "yellow", "target");
      for (const unit of capturers) {
        const cid = assigned.get(unit.id);
        if (cid === undefined) continue;
        this.debugLine(unit.hex, view.cities[cid].hex, "orange");
        this.moveToward(unit, /** @type {any} */ (fields.get(cid)), this.zocHexes());
        this.finish(unit);
      }

      // Everyone else hunts the nearest visible enemy, else enemy cities.
      const enemies = view.enemyUnits();
      let goals = enemies.map((/** @type {any} */ e) => e.hex);
      if (!goals.length) goals = view.cities.filter((/** @type {any} */ c) => c.owner && c.owner !== this.player).map((/** @type {any} */ c) => c.hex);
      if (!goals.length) goals = [this.enemyHome];
      const hunt = this.distanceField(goals);
      for (const unit of movers) {
        if (assigned.has(unit.id) || !view.units.has(unit.id) || !this.canAct(unit)) continue;
        if (this.unitType(unit).no_move_attack && this.targetsInRange(unit).length) continue;
        this.moveToward(unit, hunt, this.zocHexes());
        this.finish(unit);
      }

      const cavalry = enemies.filter((/** @type {any} */ e) => e.type === "K").length;
      const order = "IIKIAIP";
      const affordable = (/** @type {string} */ code) => world.unitTypes[code] && view.gold >= world.unitTypes[code].cost;
      for (const city of view.myCities()) {
        if (city.built || view.enemyAt(city.hex) || view.friendsAt(city.hex) >= world.c.stack_limit) continue;
        let code = cavalry >= 2 && this.builds % 2 ? "P" : order[this.builds % order.length];
        if (!affordable(code)) code = "I";
        if (affordable(code) && this.build(city, code).ok) this.builds++;
      }
    }
  }

  /**
   * The players for the bots ported here, by their ids in the index.
   * @type {Record<string, (seed: number) => any>}
   */
  const own = {
    random: (seed) => new RandomBot(seed),
    greedy: () => new GreedyBot(),
  };

  /**
   * The bots a game can be started against, in the index's order. A bot
   * knows the rule sets in its `rules`, and is made for the maps in its
   * `maps`. `create` makes a player for one game: see kit.js for what a
   * player is. A bot whose `play_page` isn't `js` runs in a worker, in
   * Python with Pyodide or as WebAssembly.
   */
  const list = Index.bots.map((b) =>
    Object.assign({}, b, {
      create: b.play_page === "js" ? own[b.id] : () => ReferenceOpponent.create(b.id),
    }),
  );

  return { list, ladders: Index.ladders, RandomBot, GreedyBot };
})();

if (typeof module !== "undefined") module.exports = BitbathBots;
