// @ts-check
// Playing Bitbath in the page: a human against one of the built-in bots
// (bots.js). The game runs in the viewer's engine and is shown by the viewer
// (viewer.js) as a replay that grows, through the human's fog of war. This
// file runs the turns, and turns clicks and keys into the human's orders.
/* global BitbathKit, BitbathBots */
"use strict";

(function () {
  /** @type {any} */ const V = /** @type {any} */ (window).bitbathViewer;
  /** @type {any} */ const Kit = BitbathKit;
  /** @type {any} */ const Bots = BitbathBots;
  const v = V.state;
  const $ = V.$;
  const el = V.el;

  /** Map sizes a game can be played on, as config lines like those in configs/. Small is configs/small.toml. */
  const MAPS = [
    { id: "micro", name: "Micro", about: "12 × 8 hexes, 2 neutral cities, 80 rounds: a quick skirmish.", config: "map_width = 12\nmap_height = 8\nneutral_cities = 2\nround_limit = 80" },
    { id: "small", name: "Small", about: "20 × 14 hexes, 4 neutral cities, 80 rounds.", config: "map_width = 20\nmap_height = 14\nneutral_cities = 4\nround_limit = 80" },
    { id: "medium", name: "Medium", about: "28 × 20 hexes, 6 neutral cities, 120 rounds.", config: "map_width = 28\nmap_height = 20\nneutral_cities = 6\nround_limit = 120" },
    { id: "standard", name: "Standard", about: "40 × 30 hexes, 10 neutral cities, 200 rounds: what bots play, and a lot to run by hand.", config: "" },
  ];

  /** How long the opponent's turn takes to play out on the map, in ms. */
  const TURN_MS = 700;

  /** @param {number} ms */
  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

  /** The stats a replay's result keeps for each player. */
  const newStats = () => ({ turns: 0, orders: 0, invalid: 0, late: 0, timeouts: 0, missed_turns: 0, think_ms: 0, max_think_ms: 0, debug_dropped: 0 });

  /** @type {Game | null} */
  let current = null;

  /** One game, from its first turn until it ends or something else is opened. The viewer calls it as `v.play`. */
  class Game {
    /** @param {number} human the human's player number @param {any} bot the opponent: a player as in kit.js @param {string} botName */
    constructor(human, bot, botName) {
      this.human = human;
      this.bot = bot;
      this.botName = botName;
      /** Set by the viewer when the game has ended. */ this.over = false;
      /** Set when a replay or another game takes this one's place. */ this.closed = false;
      /** Not taking orders: the opponent is moving. */ this.busy = true;
      /** What the human saw of the opponent's last turn, as the viewer's marks. @type {any[]} */ this.seen = [];
      /** What that turn did to the human, in words. @type {string[]} */ this.log = [];
      /** @type {any} */ this.world = null;
      /** The human's view of the state shown, and the helpers on it. @type {any} */ this.kit = null;
      /** The hex picked, and the unit picked on it, if any. @type {{hex: number[], unit: number | null} | null} */ this.sel = null;
      /** Where the picked unit can move, by hex index: see Kit.reachable. @type {Map<number, string>} */ this.reach = new Map();
      /** The enemy units the picked unit can attack. @type {any[]} */ this.targets = [];
      /** @type {Set<number>} */ this.zoc = new Set();
      /** The ids of the human's units with something left to do. @type {number[]} */ this.ready = [];
      /** Why the last order didn't do what was asked, if it didn't. */ this.problem = "";
      /** What the attack forecast on show was worked out from, so it is built once. */ this.forecastKey = "";
      /** The turn being played: its record in the replay. @type {any} */ this.turn = null;
      this.turnStart = 0;
      this.stats = [newStats(), newStats()];
      /** The types of the human's units, to name the ones the opponent destroys. @type {Map<number, string>} */ this.types = new Map();
    }

    // Turns.

    /** Starts a player's turn and returns its turn message. @param {number} player @returns {string[]} */
    begin(player) {
      this.turn = { type: "turn", round: 0, player, time_ms: 0, bank_ms: 0, used_ms: 0, orders: [], rejected: 0 };
      V.append("T " + player, this.turn);
      this.turnStart = performance.now();
      const lines = v.engine.turnMessage();
      this.turn.round = Number(lines[0].split(/\s+/)[1]);
      return lines;
    }

    /** Runs an order of the current turn and returns its reply lines. @param {string} line @returns {string[]} */
    send(line) {
      const reply = V.append("O " + line);
      const stats = this.stats[this.turn.player - 1];
      stats.orders++;
      if ((reply[reply.length - 1] || "").startsWith("ERR")) {
        this.turn.rejected++;
        stats.invalid++;
      }
      return reply;
    }

    /** Ends the current turn. Returns whether that ended the game. */
    end() {
      const turn = this.turn;
      const stats = this.stats[turn.player - 1];
      turn.used_ms = Math.round(performance.now() - this.turnStart);
      stats.turns++;
      stats.think_ms += turn.used_ms;
      stats.max_think_ms = Math.max(stats.max_think_ms, turn.used_ms);
      return v.engine.end();
    }

    async botTurn() {
      this.busy = true;
      this.sel = null;
      this.problem = "";
      for (const u of this.kit ? this.kit.view.myUnits() : []) this.types.set(u.id, u.type);
      const lines = this.begin(3 - this.human);
      const turn = this.turn;
      let over = false;
      this.bot.turn(lines, (/** @type {string} */ line) => {
        if (line.startsWith("D ")) {
          // Debug annotations, kept for the replay (SPEC §8).
          if (!turn.debug) turn.debug = [];
          if (turn.debug.length < 1000) turn.debug.push(line);
          else this.stats[turn.player - 1].debug_dropped++;
          return [];
        }
        if (over) return null;
        const reply = this.send(line);
        // Only taking the last city of a player ends the game mid-turn.
        if (reply[reply.length - 1] === "OK captured" && v.engine.seek(v.steps).outcome) over = true;
        return reply;
      });
      const ended = this.end();
      // Play the turn out on the map, as far as the human sees it.
      const ms = V.animateTo(v.steps, TURN_MS);
      if (ms) await sleep(ms + 40);
      if (this.closed) return;
      if (ended) return this.finish();
      // An attack shows where the attacker stood (SPEC §6.3); a move shows
      // only where the human watched all of it.
      this.seen = v.marks.filter((/** @type {any} */ m) => m.ok && ((m.op === "A" && m.hexes.length === 2) || (m.op === "M" && V.shown(m, this.human))));
      this.humanTurn();
    }

    humanTurn() {
      const lines = this.begin(this.human);
      this.log = this.describe(Kit.View.fromTurn(this.world, this.human, lines).logs);
      this.busy = false;
      // As a step of playback, so the damage numbers of the opponent's
      // attacks go on floating into the human's turn.
      V.setPos(v.steps, true);
    }

    /** Puts the opponent's last turn into words, from the turn message's LOG lines. @param {string[][]} logs */
    describe(logs) {
      const name = (/** @type {string} */ code) => (this.world.unitTypes[code] ? this.world.unitTypes[code].name : "unit");
      return logs.map((t) => {
        if (t[0] === "HIT") {
          const mine = name(this.types.get(Number(t[5])) || "");
          const where = " at (" + t[3] + "," + t[4] + ")";
          if (t[6] === "0") return V.capitalize(name(t[2])) + where + " destroyed your " + mine + ".";
          const hit = V.capitalize(name(t[2])) + where + " hit your " + mine + ", now " + t[6] + " hp";
          return hit + (t[7] === "0" ? ", and died doing it." : ".");
        }
        if (t[0] === "CITY") return t[2] === String(this.human) ? "You lost city " + t[1] + "." : this.botName + " took city " + t[1] + ".";
        return t.join(" ");
      });
    }

    endTurn() {
      if (!this.myTurn()) return;
      if (this.end()) return this.finish();
      this.botTurn();
    }

    /** The game has ended: records the result, and hands the game to the viewer as a finished replay. */
    finish() {
      const st = v.engine.seek(v.steps);
      const [winner, reason] = st.outcome || [0, "draw"];
      const count = (/** @type {(p: number) => number} */ f) => [f(1), f(2)];
      this.busy = true;
      this.sel = null;
      closeMenu();
      V.finishLive({
        type: "result",
        winner,
        reason,
        round: st.round,
        cities: count((p) => st.cities.filter((/** @type {number[]} */ c) => c[0] === p).length),
        units: count((p) => st.units.filter((/** @type {any[]} */ u) => u[1] === p).length),
        stats: this.stats,
        stderr: ["", ""],
      });
    }

    /** The game as the text of a replay file (SPEC §8). */
    replayText() {
      const r = v.replay;
      const records = [r.header, { type: "init", notes: ["", ""], stderr: ["", ""] }].concat(r.turns, r.result ? [r.result] : []);
      return records.map((x) => JSON.stringify(x)).join("\n") + "\n";
    }

    // The human's orders.

    myTurn() {
      return !this.busy && !this.over && !this.closed;
    }

    /** The unit picked, if it is still there. */
    unit() {
      return (this.sel && this.sel.unit !== null && this.kit && this.kit.view.units.get(this.sel.unit)) || null;
    }

    /** Whether a unit of the human's has anything left to do this turn. @param {any} u */
    canDo(u) {
      const kit = this.kit;
      return kit.canAct(u) && (kit.reachable(u, this.zoc).size > 0 || kit.canCapture(u) || (kit.canAttack(u) && kit.targetsInRange(u).length > 0));
    }

    /** Gives one of the human's orders, and shows what it did. @param {string} line */
    order(line) {
      if (!this.myTurn()) return;
      closeMenu();
      const reply = this.send(line);
      const t = (reply[reply.length - 1] || "").split(/\s+/);
      this.problem = "";
      if (t[0] === "ERR") this.problem = V.capitalize(t.slice(2).join(" ")) + ".";
      else if (t[0] === "PART") this.problem = t[3] === "ambush" ? "Ambush! A hidden enemy blocked the way." : "Stopped next to a hidden enemy.";
      V.animateTo(v.steps, V.STEP_MS, true);
      if (v.state.outcome) {
        this.end();
        this.finish();
      }
    }

    /** @param {{hex: number[], unit: number | null} | null} sel */
    select(sel) {
      this.sel = sel;
      this.problem = "";
      this.update();
      V.draw();
    }

    /** Picks the next unit with something left to do. */
    nextUnit() {
      if (!this.myTurn() || !this.ready.length) return;
      const now = this.sel && this.sel.unit !== null ? this.sel.unit : -1;
      const id = this.ready.find((x) => x > now) || this.ready[0];
      this.select({ hex: this.kit.view.units.get(id).hex, unit: id });
    }

    capture() {
      const unit = this.unit();
      if (unit && this.myTurn() && this.kit.canCapture(unit)) this.order("C " + unit.id);
    }

    /** Why a city of the human's can't build a unit type right now, or "" if it can. @param {any} city @param {any} type */
    cantBuild(city, type) {
      const view = this.kit.view;
      if (city.built) return "This city has built its unit for this turn.";
      if (view.friendsAt(city.hex) >= this.world.c.stack_limit) return "The city is full: move a unit out to build.";
      if (view.gold < type.cost) return V.capitalize(type.name) + " costs " + type.cost + " gold, and you have " + view.gold + ".";
      return "";
    }

    /** @param {any} city @param {string} code */
    build(city, code) {
      if (!this.myTurn()) return;
      const why = this.cantBuild(city, this.world.unitTypes[code]);
      if (!why) return this.order("B " + city.id + " " + code);
      this.problem = why;
      this.render();
    }

    /** What attacking a target would do, in words. @param {any} target */
    forecast(target) {
      const unit = this.unit();
      const [theirs, mine] = this.kit.predict(unit, target);
      const after = (/** @type {number} */ hp) => (hp ? hp + " hp" : "destroyed");
      return this.kit.unitType(target).name + " " + target.hp + " → " + after(theirs) + ", yours " + unit.hp + " → " + after(mine);
    }

    /**
     * A click on the map: an order for the unit picked, if the hex is one
     * it can move to or attack, and otherwise a pick of what is on the hex.
     * @param {number[] | null} hex null for a click that drops the selection
     * @param {number} x @param {number} y where on the board @param {boolean} shift
     */
    click(hex, x, y, shift) {
      if (menuWasOpen || !this.kit) return;
      if (!hex) return this.select(null);
      const view = this.kit.view;
      const unit = this.unit();
      const mine = view.unitsAt(hex).filter((/** @type {any} */ u) => u.owner === this.human);
      if (unit && this.myTurn()) {
        const targets = this.targets.filter((t) => t.q === hex[0] && t.r === hex[1]);
        if (targets.length === 1) return this.order("A " + unit.id + " " + targets[0].id);
        if (targets.length > 1) {
          return openMenu(x, y, targets.map((t) => ({ label: "Attack " + this.forecast(t), run: () => this.order("A " + unit.id + " " + t.id) })));
        }
        // A click on the human's own units picks one of them, so joining
        // them takes a right click, or Shift.
        const dirs = this.reach.get(this.world.index(hex));
        if (dirs !== undefined && (shift || !mine.length)) return this.order("M " + unit.id + " " + dirs);
      }
      if (!mine.length) return this.select({ hex, unit: null });
      // Clicking a stack again goes through its units.
      const at = this.sel && this.sel.hex[0] === hex[0] && this.sel.hex[1] === hex[1] ? mine.findIndex((/** @type {any} */ u) => u.id === /** @type {any} */ (this.sel).unit) : -1;
      const pick = at >= 0 ? mine[(at + 1) % mine.length] : mine.find((/** @type {any} */ u) => this.ready.includes(u.id)) || mine[0];
      this.select({ hex, unit: pick.id });
    }

    /**
     * A right click on the map: the picked unit goes to the hex or attacks
     * it, even where a click would pick one of the human's own units instead.
     * Anywhere else, it drops the selection.
     * @param {number[] | null} hex @param {number} x @param {number} y
     */
    context(hex, x, y) {
      if (menuWasOpen || !this.kit) return;
      const can = hex && this.unit() && this.myTurn() && (this.reach.has(this.world.index(hex)) || this.targets.some((t) => t.q === hex[0] && t.r === hex[1]));
      if (can) this.click(hex, x, y, true);
      else this.select(null);
    }

    /** @param {KeyboardEvent} e */
    key(e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const key = e.key.length === 1 ? e.key.toUpperCase() : e.key;
      const city = this.sel && this.kit ? this.kit.view.cityAt(this.sel.hex) : null;
      const unit = this.unit();
      if (key === "E" || key === "Enter") this.endTurn();
      else if (key === "N" || key === "Tab") this.nextUnit();
      else if (key === "Escape") this.select(null);
      else if (key === "C" && unit && this.kit.canCapture(unit)) this.capture();
      else if (this.world.unitTypes[key] && city && city.owner === this.human) this.build(city, key);
      else return;
      e.preventDefault();
    }

    // What the viewer asks for.

    /** Another game or a replay is taking this one's place. */
    close() {
      this.closed = true;
      closeMenu();
      $("forecast").hidden = true;
      if (current === this) current = null;
    }

    /** The state shown has changed: works out again what the human can do, and shows it. */
    update() {
      if (!this.over) {
        const kit = (this.kit = new Kit.Kit(this.world, Kit.View.fromState(this.world, v.state, this.human)));
        this.zoc = kit.zocHexes();
        const unit = this.unit();
        // The selection follows its unit, and lets go of one that died.
        if (this.sel && this.sel.unit !== null) this.sel = unit ? { hex: unit.hex, unit: unit.id } : null;
        const mine = this.myTurn() && v.state.player === this.human;
        this.reach = unit && mine ? kit.reachable(unit, this.zoc) : new Map();
        this.targets = unit && mine && kit.canAttack(unit) ? kit.targetsInRange(unit) : [];
        this.ready = mine ? kit.view.myUnits().filter((/** @type {any} */ u) => this.canDo(u)).map((/** @type {any} */ u) => u.id).sort((/** @type {number} */ a, /** @type {number} */ b) => a - b) : [];
      }
      this.render();
    }

    /** Extra lines for the viewer's hover box: what a click on the hex would do. @param {number} q @param {number} r @returns {string[]} */
    hoverLines(q, r) {
      const unit = this.unit();
      if (!unit || !this.myTurn()) return [];
      const out = this.targets.filter((t) => t.q === q && t.r === r).map((t) => "attack: " + this.forecast(t));
      const i = this.world.index([q, r]);
      const dirs = this.reach.get(i);
      if (dirs !== undefined) {
        const cost = Kit.walk(unit.hex, dirs).slice(1).reduce((/** @type {number} */ sum, /** @type {number[]} */ h) => sum + this.world.terrain(h).move_cost, 0);
        const stops = this.zoc.has(i) && !this.kit.unitType(unit).skirmish;
        out.push("move: " + cost + " of " + unit.mp + " mp" + (stops ? ", and stop in a zone of control" : ""));
        if (this.kit.view.friendsAt([q, r])) out.push("right-click to move onto your own units");
      }
      return out;
    }

    /**
     * Where the picked unit would attack an enemy from, to forecast the fight.
     * `when` is "now" for an enemy in range, "move" for one it can attack
     * this turn after moving, and "later" for any other. For the last two,
     * `from` is the hex in range that leaves the unit the most hit points,
     * taken from those it can move to this turn if there are any. It is
     * undefined for "now", and for an enemy with no free hex in range.
     * @param {any} unit @param {any} target @returns {{when: string, from: number[] | undefined}}
     */
    attackFrom(unit, target) {
      const kit = this.kit;
      const type = kit.unitType(unit);
      if (this.targets.includes(target)) return { when: "now", from: undefined };
      const inRange = (/** @type {number[]} */ h) => {
        const d = Kit.distance(h, target.hex);
        return type.range_min <= d && d <= type.range_max;
      };
      /** @type {number[][]} */
      let spots = [];
      if (kit.canAttack(unit) && !type.no_move_attack) {
        for (const i of this.reach.keys()) if (inRange(this.world.hex(i))) spots.push(this.world.hex(i));
      }
      const when = spots.length ? "move" : "later";
      if (!spots.length) {
        const n = type.range_max;
        for (let dq = -n; dq <= n; dq++) {
          for (let dr = -n; dr <= n; dr++) {
            const h = [target.hex[0] + dq, target.hex[1] + dr];
            if (!inRange(h) || this.world.index(h) < 0 || !this.world.passable(h)) continue;
            if (!kit.view.unitsAt(h).some((/** @type {any} */ u) => u.owner !== this.human)) spots.push(h);
          }
        }
      }
      let from;
      let best = -1;
      for (const h of spots) {
        const mine = kit.predict(unit, target, h)[1];
        if (mine > best) [best, from] = [mine, h];
      }
      return { when, from };
    }

    /**
     * Shows, beside the enemy under the pointer, what an attack by the picked
     * unit would do to each side: hit points before and after, as bars and
     * as numbers. An enemy the unit can't attack from where it stands gets
     * the forecast too, as a help in deciding whether to go there.
     */
    showForecast() {
      const card = $("forecast");
      const unit = this.unit();
      const hex = v.hover;
      const can = unit && hex && this.myTurn() && v.state.player === this.human && !v.anim && $("menu").hidden;
      const plans = can ? this.kit.view.enemyUnits().filter((/** @type {any} */ t) => t.q === hex[0] && t.r === hex[1]).map((/** @type {any} */ t) => ({ t, ...this.attackFrom(unit, t) })) : [];
      const fights = plans.filter((p) => p.when === "now" || p.from);
      if (!unit || !hex || !fights.length) {
        card.hidden = true;
        this.forecastKey = "";
        return;
      }
      const g = v.geo;
      const key = [v.pos, unit.id, hex, g.size, g.ox, g.oy, g.w, g.h].join(" ");
      if (key === this.forecastKey) return;
      this.forecastKey = key;
      const row = (/** @type {any} */ u, /** @type {number} */ after) => {
        const max = this.kit.unitType(u).max_hp;
        const out = el("div", "", "fc-row");
        const who = el("span", (u.owner === this.human ? "Your " : "Enemy ") + this.kit.unitType(u).name, "fc-who");
        const dot = el("span", "", "fc-dot");
        dot.style.background = V.playerColor(u.owner);
        who.prepend(dot);
        const bar = el("span", "", "fc-bar");
        const keep = el("span", "");
        keep.style.background = V.playerColor(u.owner);
        keep.style.width = (100 * after) / max + "%";
        const lose = el("span", "", "fc-lose");
        lose.style.width = (100 * (u.hp - after)) / max + "%";
        bar.append(keep, lose);
        const num = el("span", u.hp + " → ", "fc-num");
        num.append(el("b", String(after)));
        const lost = u.hp - after;
        const delta = el("span", !after ? "destroyed" : lost ? "−" + lost : "no damage", "fc-delta" + (lost ? "" : " none"));
        out.append(who, bar, num, delta);
        return out;
      };
      card.replaceChildren(
        ...fights.map(({ t, from }) => {
          const [theirs, mine] = this.kit.predict(unit, t, from);
          const fight = el("div", "", "fc-fight");
          fight.append(row(t, theirs), row(unit, mine));
          return fight;
        }),
      );
      const when = fights[0].when;
      if (when === "move") card.append(el("div", "If you move into range first.", "fc-hint"));
      else if (when === "later") card.append(el("div", "Not this turn: this unit can't attack it yet.", "fc-hint"));
      else if (fights.length > 1) card.append(el("div", "Click to choose which one to attack.", "fc-hint"));
      card.hidden = false;

      // Beside the target, and preferably beyond it as the attacker sees it.
      // Of the places that fit, it takes the one that covers the fewest
      // units, and never the two that are fighting.
      const s = g.size;
      const [w, h] = [card.offsetWidth, card.offsetHeight];
      const [tx, ty] = V.center(hex[0], hex[1]);
      const [ax, ay] = V.center(unit.hex[0], unit.hex[1]);
      const gap = s * 1.05 + 6;
      /** @type {Map<number, number>} */
      const weight = new Map();
      for (const u of this.kit.view.units.values()) weight.set(this.world.index(u.hex), 1);
      weight.set(this.world.index(hex), 100);
      weight.set(this.world.index(unit.hex), 100);
      let best = null;
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]]) {
        const x = Math.max(4, Math.min(g.w - w - 4, dx > 0 ? tx + gap : dx < 0 ? tx - gap - w : tx - w / 2));
        const y = Math.max(4, Math.min(g.h - h - 4, dy > 0 ? ty + gap : dy < 0 ? ty - gap - h : ty - h / 2));
        let cost = 0;
        for (const [i, k] of weight) {
          const [ux, uy] = V.center(...this.world.hex(i));
          if (ux + s * 0.85 > x && ux - s * 0.85 < x + w && uy + s * 0.85 > y && uy - s * 0.85 < y + h) cost += k;
        }
        // Among equals, the side away from the attacker wins.
        const away = (dx * (tx - ax) + dy * (ty - ay)) / (Math.hypot(dx, dy) * (Math.hypot(tx - ax, ty - ay) || 1));
        const score = cost * 10 - away;
        if (!best || score < best.score) best = { x, y, score };
      }
      card.style.left = /** @type {any} */ (best).x + "px";
      card.style.top = /** @type {any} */ (best).y + "px";
    }

    /** Under the units: where the picked unit can go, and the way to the hex under the pointer. @param {CanvasRenderingContext2D} ctx */
    drawUnder(ctx) {
      const unit = this.unit();
      if (this.over || !unit || !this.myTurn()) return;
      const s = v.geo.size;
      const color = V.playerColor(this.human);
      ctx.save();
      ctx.fillStyle = ctx.strokeStyle = color;
      ctx.globalAlpha = 0.25;
      for (const i of this.reach.keys()) {
        const [x, y] = V.center(...this.world.hex(i));
        V.hexPath(ctx, x, y, s * 0.97);
        ctx.fill();
      }
      const dirs = v.hover ? this.reach.get(this.world.index(v.hover)) : undefined;
      if (dirs !== undefined) {
        // A move not yet made is dashed and in the text's color, so it can't
        // be taken for the trail of one already made, which is solid and in
        // the player's color.
        ctx.globalAlpha = 0.9;
        ctx.fillStyle = ctx.strokeStyle = V.cssVar("--text");
        ctx.lineWidth = Math.max(2, s * 0.11);
        ctx.lineCap = ctx.lineJoin = "round";
        ctx.setLineDash([s * 0.22, s * 0.24]);
        V.arrow(ctx, V.centers(Kit.walk(unit.hex, dirs)));
        ctx.setLineDash([]);
      }
      ctx.restore();
    }

    /**
     * Marks each of the human's cities that can still build this turn with a
     * small gold "+" on its hex's corner, clear of the units on it. A city that has
     * built, is full, or has no gold for even the cheapest unit has none.
     * @param {CanvasRenderingContext2D} ctx
     */
    drawBuildBadges(ctx) {
      if (this.over || !this.kit || !this.myTurn() || v.state.player !== this.human) return;
      const types = /** @type {any[]} */ (Object.values(this.world.unitTypes));
      const cheapest = types.reduce((a, b) => (b.cost < a.cost ? b : a));
      const s = v.geo.size;
      // Small and a little see-through: it's a reminder, not a call to act.
      const r = Math.max(3.5, s * 0.14);
      ctx.save();
      ctx.globalAlpha = 0.85;
      ctx.lineCap = "round";
      for (const city of this.kit.view.cities.values()) {
        if (city.owner !== this.human || this.cantBuild(city, cheapest)) continue;
        const [cx, cy] = V.center(city.hex[0], city.hex[1]);
        const [x, y] = [cx + s * 0.62, cy - s * 0.52];
        ctx.beginPath();
        ctx.arc(x, y, r, 0, 2 * Math.PI);
        ctx.fillStyle = "#fab005";
        ctx.fill();
        ctx.beginPath();
        ctx.moveTo(x - r * 0.5, y);
        ctx.lineTo(x + r * 0.5, y);
        ctx.moveTo(x, y - r * 0.5);
        ctx.lineTo(x, y + r * 0.5);
        ctx.lineWidth = Math.max(1, r * 0.25);
        ctx.strokeStyle = "rgba(0,0,0,.7)";
        ctx.stroke();
      }
      ctx.restore();
    }

    /** Over the units: a ring on the picked unit or hex, and on each enemy the unit can attack. @param {CanvasRenderingContext2D} ctx */
    drawOver(ctx) {
      this.showForecast();
      this.drawBuildBadges(ctx);
      if (this.over || !this.sel) return;
      const s = v.geo.size;
      const unit = this.unit();
      const at = unit ? v.unitPos.get(unit.id) : null;
      ctx.save();
      ctx.strokeStyle = V.cssVar("--text");
      ctx.lineWidth = Math.max(2, s * 0.1);
      if (at) {
        ctx.beginPath();
        ctx.arc(at[0], at[1], at[2] + ctx.lineWidth, 0, 2 * Math.PI);
        ctx.stroke();
      } else {
        const [x, y] = V.center(this.sel.hex[0], this.sel.hex[1]);
        V.hexPath(ctx, x, y, s * 0.9);
        ctx.stroke();
      }
      ctx.strokeStyle = "#f03e3e";
      ctx.lineWidth = Math.max(2, s * 0.14);
      ctx.setLineDash([s * 0.3, s * 0.2]);
      for (const key of new Set(this.targets.map((t) => this.world.index(t.hex)))) {
        const [x, y] = V.center(...this.world.hex(key));
        V.hexPath(ctx, x, y, s * 0.88);
        ctx.stroke();
      }
      ctx.restore();
    }

    // The controls on the side panel.

    render() {
      const mine = this.myTurn();
      const result = v.replay.result;
      let status = this.botName + " is moving…";
      if (this.over && result) status = !result.winner ? "Draw" : (result.winner === this.human ? "You won by " : "You lost by ") + result.reason;
      else if (mine) status = "Your turn · round " + v.state.round + " of " + this.world.c.round_limit;
      $("play-status").textContent = status;
      let note = "";
      if (this.over) note = "Step through the game below, or save it as a replay.";
      else if (this.problem) note = this.problem;
      else if (mine) note = this.ready.length ? this.ready.length + (this.ready.length === 1 ? " unit has" : " units have") + " something left to do." : "No unit has anything left to do.";
      $("play-note").textContent = $("play-note").title = note;
      $("play-note").classList.toggle("warn", !this.over && !!this.problem);
      $("end-turn").hidden = $("next-unit").hidden = this.over;
      $("save-replay").hidden = $("play-again").hidden = !this.over;
      $("selection").hidden = $("play-last").hidden = this.over;
      $("end-turn").disabled = !mine;
      $("next-unit").disabled = !mine || !this.ready.length;
      const log = $("play-log");
      log.replaceChildren();
      for (const line of this.log.length ? this.log : ["Nothing happened to your units or cities."]) log.append(el("div", line));
      this.renderSelection(mine);
    }

    /** What is on the hex picked, and the orders that can be given there. @param {boolean} mine whether the human can give orders */
    renderSelection(mine) {
      const box = $("selection");
      box.replaceChildren();
      if (!this.sel || !this.kit) {
        box.append(el("div", "Click one of your units to give it orders, or one of your cities to build.", "muted"));
        return;
      }
      const kit = this.kit;
      const view = kit.view;
      const world = this.world;
      const hex = this.sel.hex;
      const city = view.cityAt(hex);
      const unit = this.unit();
      const button = (/** @type {string} */ text, /** @type {() => void} */ run, /** @type {string} */ cls) => {
        const b = el("button", text, cls || "");
        b.onclick = run;
        return b;
      };
      box.append(el("div", "(" + hex[0] + "," + hex[1] + ") " + world.terrain(hex).name + (city ? " · city " + city.id : ""), "label"));
      for (const u of view.unitsAt(hex)) {
        const type = kit.unitType(u);
        if (u.owner !== this.human) {
          box.append(el("div", "Enemy " + type.name + " · " + u.hp + " hp"));
          continue;
        }
        const state = u.fresh ? "new, ready next turn" : u.acted ? "done" : u.mp + " mp";
        const cls = "unit" + (unit && unit.id === u.id ? " chosen" : "") + (this.ready.includes(u.id) ? "" : " spent");
        box.append(button(V.capitalize(type.name) + " · " + u.hp + " hp · " + state, () => this.select({ hex, unit: u.id }), cls));
      }
      if (unit && mine) {
        if (kit.canCapture(unit)) {
          const progress = (city.capturer === this.human ? city.progress : 0) + unit.hp;
          const what = progress >= world.c.capture_points ? "takes the city" : progress + " of " + world.c.capture_points;
          const row = el("div", "", "row");
          row.append(button("Capture (C) · " + what, () => this.capture(), "primary"));
          box.append(row);
        }
        if (this.targets.length) {
          box.append(el("div", "Attack", "label"));
          for (const t of this.targets) box.append(button(V.capitalize(this.forecast(t)), () => this.order("A " + unit.id + " " + t.id), "unit"));
        }
      }
      if (city && city.owner === this.human && mine) {
        // A reason that holds for a unit costing nothing holds for them all.
        const why = this.cantBuild(city, { name: "", cost: 0 });
        box.append(el("div", "Build · " + view.gold + " gold", "label"));
        const row = el("div", "", "row");
        for (const type of Object.values(world.unitTypes)) {
          const b = button(V.capitalize(type.name) + " " + type.cost, () => this.build(city, type.code), "");
          b.disabled = !!this.cantBuild(city, type);
          b.title = "Key " + type.code + " · move " + type.move + ", vision " + type.vision + ", range " + (type.range_min === type.range_max ? type.range_min : type.range_min + "–" + type.range_max);
          row.append(b);
        }
        box.append(row);
        if (why) box.append(el("div", why, "muted"));
      }
    }
  }

  // The menu for picking one of several targets on a hex.

  /** Whether the menu was open when the mouse went down, so that click only closes it. */
  let menuWasOpen = false;

  function closeMenu() {
    $("menu").hidden = true;
  }

  /** @param {number} x @param {number} y @param {{label: string, run: () => void}[]} items */
  function openMenu(x, y, items) {
    const menu = $("menu");
    menu.replaceChildren();
    for (const item of items) {
      const b = el("button", item.label);
      b.onclick = () => {
        closeMenu();
        item.run();
      };
      menu.append(b);
    }
    menu.hidden = false;
    const board = $("board").getBoundingClientRect();
    menu.style.left = Math.max(4, Math.min(board.width - menu.offsetWidth - 4, x + 8)) + "px";
    menu.style.top = Math.max(4, Math.min(board.height - menu.offsetHeight - 4, y + 8)) + "px";
  }

  document.addEventListener(
    "mousedown",
    (e) => {
      menuWasOpen = !$("menu").hidden;
      if (menuWasOpen && !$("menu").contains(/** @type {Node} */ (e.target))) closeMenu();
    },
    true,
  );

  // Starting a game.

  /**
   * Starts a game against a built-in bot.
   * @param {{bot: string, map: string, human: number, seed: number}} options the ids of a bot and a map size, the human's player number, and the map's seed
   */
  async function start(options) {
    const kind = Bots.list.find((/** @type {any} */ b) => b.id === options.bot) || Bots.list[0];
    const map = MAPS.find((m) => m.id === options.map) || MAPS[0];
    const human = options.human === 2 ? 2 : 1;
    const engine = await V.engine();
    const setup = engine.setup(map.config + "\nseed = " + options.seed);
    const names = human === 1 ? ["Human", kind.name] : [kind.name, "Human"];
    const header = Object.assign({ type: "header" }, engine.versions(), {
      map: "seed:" + options.seed,
      seed: options.seed,
      players: names.map((name) => ({ name, command: name === "Human" ? "a person in the browser" : "built-in " + kind.id + " bot" })),
      setup,
    });
    const game = new Game(human, kind.create(options.seed), kind.name);
    game.world = new Kit.World(setup);
    if (!(await V.openLive(header, game))) throw new Error($("error").textContent);
    current = game;
    game.bot.init(engine.initMessage(3 - human));
    if (human === 1) game.humanTurn();
    else game.botTurn();
    return game;
  }

  // The new game dialog.

  /** @param {string} id @param {{id: string, name: string, about: string}[]} items @param {string} aboutId */
  function fill(id, items, aboutId) {
    const select = $(id);
    for (const item of items) {
      const option = el("option", item.name);
      option.value = item.id;
      select.append(option);
    }
    const about = () => ($(aboutId).textContent = (items.find((x) => x.id === select.value) || items[0]).about);
    select.onchange = about;
    return about;
  }

  const abouts = [fill("opponent", Bots.list, "opponent-about"), fill("map-size", MAPS, "map-about")];
  $("opponent").value = Bots.list[Bots.list.length - 1].id;
  abouts.forEach((about) => about());

  function openDialog() {
    $("new-game-error").textContent = "";
    $("new-game").showModal();
  }

  $("new-game-button").onclick = $("play-button").onclick = $("play-again").onclick = openDialog;
  $("new-game").addEventListener("click", (/** @type {MouseEvent} */ e) => {
    if (e.target === $("new-game")) $("new-game").close();
  });
  $("start").onclick = async () => {
    const typed = $("seed").value.trim();
    const seed = /^\d{1,15}$/.test(typed) ? Number(typed) : Math.floor(Math.random() * 1e6);
    try {
      await start({ bot: $("opponent").value, map: $("map-size").value, human: Number($("side-choice").value), seed });
      $("new-game").close();
    } catch (e) {
      $("new-game-error").textContent = String(e instanceof Error ? e.message : e);
    }
  };
  $("end-turn").onclick = () => current && current.endTurn();
  $("next-unit").onclick = () => current && current.nextUnit();
  $("save-replay").onclick = () => {
    if (!current) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([current.replayText()], { type: "application/x-ndjson" }));
    a.download = "bitbath-seed-" + v.replay.header.seed + ".bbr";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  // For tests and embedding: window.bitbathPlay.start({bot, map, human, seed}).
  /** @type {any} */ (window).bitbathPlay = { start, MAPS, game: () => current };
})();
