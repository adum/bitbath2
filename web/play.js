// @ts-check
// Playing Bitbath in the page: a human against one of the built-in bots
// (bots.js). The game runs in the viewer's engine and is shown by the viewer
// (viewer.js) as a replay that grows, through the human's fog of war. This
// file runs the turns, and turns clicks and keys into the human's orders.
/* global BitbathKit, BitbathBots, BitbathPieces, BitbathSounds, GameRecording */
"use strict";

(function () {
  /** @type {any} */ const V = /** @type {any} */ (window).bitbathViewer;
  /** @type {any} */ const Kit = BitbathKit;
  /** @type {any} */ const Bots = BitbathBots;
  const v = V.state;
  const $ = V.$;
  const el = V.el;

  /**
   * The strategic ruleset (STRATEGIC.md): bread in place of gold, sites,
   * wagons, rations, and ships. These are the rules of
   * configs/strategic.toml, without its map.
   */
  const STRATEGIC = [
    "time_bank_ms = 10000", "time_increment_ms = 2000", "time_bank_cap_ms = 20000",
    "supply = 1", "start_bread = 300", "city_bread = 10",
    "site_output_min = 20", "site_output_max = 100", "site_cap_min = 400", "site_cap_max = 3000",
    "cargo_max = 1000", "passengers_max = 4",
    "ration_max = 60", "eat_base = 1", "eat_move = 1", "eat_attack = 1", "ration_cost = 5", "starve_hp = 10",
    'start_units = "I,I,S,W"',
    'terrain.R = "site 1 0 0 0"',
    'unit.W = "wagon 100 100 4 2 1 1 carry"',
    "damage.I.W = 70", "damage.P.W = 60", "damage.K.W = 90", "damage.A.W = 70", "damage.S.W = 40",
    "terrain.water.naval_move_cost = 1",
    'unit.T = "transport 150 100 6 2 1 1 naval,transport"',
    'unit.G = "galley 200 100 5 3 1 1 naval,zoc"',
    "damage.A.T = 40", "damage.A.G = 40",
    "damage.G.I = 25", "damage.G.P = 25", "damage.G.K = 25", "damage.G.A = 30", "damage.G.S = 35", "damage.G.W = 50", "damage.G.T = 80", "damage.G.G = 55",
  ].join("\n");

  /** A map under the strategic rules. @param {string} map its own config lines */
  const strategic = (map) => STRATEGIC + "\n" + map;

  /**
   * The maps a game can be played on, as config lines like those in configs/.
   * Small is configs/small.toml. The ones with `rules: "strategic"` play the
   * strategic ruleset, and need an opponent that knows it. Islets is
   * configs/islets.toml, with its sites growing half as much, Islands is
   * configs/islands.toml, and Strategic is configs/strategic.toml.
   */
  const MAPS = [
    { id: "micro", name: "Micro", about: "12 × 8 hexes, 2 neutral cities, 80 rounds: a quick skirmish.", config: "map_width = 12\nmap_height = 8\nneutral_cities = 2\nround_limit = 80" },
    { id: "small", name: "Small", about: "20 × 14 hexes, 4 neutral cities, 80 rounds.", config: "map_width = 20\nmap_height = 14\nneutral_cities = 4\nround_limit = 80" },
    { id: "medium", name: "Medium", about: "28 × 20 hexes, 6 neutral cities, 120 rounds.", config: "map_width = 28\nmap_height = 20\nneutral_cities = 6\nround_limit = 120" },
    { id: "standard", name: "Standard", about: "40 × 30 hexes, 10 neutral cities, 200 rounds: what bots play, and a lot to run by hand.", config: "" },
    {
      id: "islets", name: "Islets", label: "Islets · ships and food", rules: "strategic",
      about: "The strategic rules on 24 × 18 hexes: bread instead of gold, wagons, rations, and ships. Each side has a home island with a port to take, and the richest sites are on the island between. 300 rounds.",
      config: strategic('map_layout = "islands"\nmap_width = 24\nmap_height = 18\nneutral_cities = 2\nsites = 4\nround_limit = 300\nsite_output_min = 10\nsite_output_max = 50'),
    },
    {
      id: "islands", name: "Islands", label: "Islands · ships and food", rules: "strategic",
      about: "The same islands at 40 × 30 hexes, with longer coasts and more sea to cross. 500 rounds.",
      config: strategic('map_layout = "islands"\nmap_width = 40\nmap_height = 30\nneutral_cities = 2\nsites = 4\nround_limit = 500'),
    },
    {
      id: "seas", name: "Seas", label: "Seas · ships and food", rules: "strategic",
      about: "The strategic rules on 30 × 22 hexes of land and sea: 8 neutral cities and 8 sites, some of them across the water. 400 rounds.",
      config: strategic("map_width = 30\nmap_height = 22\nneutral_cities = 8\nsites = 8\nwater_pct = 45\nround_limit = 400"),
    },
    {
      id: "strategic", name: "Strategic", label: "Strategic · ships and food", rules: "strategic",
      about: "The strategic ruleset in full: 80 × 60 hexes, 46 neutral cities, 32 sites, and up to 5,000 rounds. A very long game by hand.",
      config: strategic("map_width = 80\nmap_height = 60\nneutral_cities = 46\nsites = 32\nwater_pct = 45\nround_limit = 5000"),
    },
  ];

  /**
   * How long a bot on disk may take over a turn, in ms. The page keeps no
   * clock (SPEC §7), so this is only there to end a game with a bot that hangs.
   */
  const BOT_TURN_MS = 30000;

  /** Why a bot on disk can't go on: `reason` is the forfeit's (SPEC §6.4), and the message says what happened. */
  class BotFailure extends Error {
    /** @param {string} reason @param {string} message */
    constructor(reason, message) {
      super(message);
      this.reason = reason;
    }
  }

  /**
   * A bot on disk, run by `bitbath serve` (SPEC §5). It is a player like the
   * built-in ones: the page hands it the init and turn messages, and it
   * sends back order lines. Here those lines go to a process, through the
   * server, one request for each line the bot sends.
   */
  class RemoteBot {
    /** @param {string} name @param {string} command */
    constructor(name, command) {
      this.name = name;
      this.command = command;
      this.session = "";
      /** What the bot wrote to stderr since it was last taken. */ this.stderr = "";
      /** Its turn messages carry a clock, since it may plan its time by it. */ this.clock = true;
      /** How long it has to answer the init message, in ms. */ this.initMs = 10000;
    }

    /** @param {string} path @param {string[]} lines @returns {Promise<any>} */
    async call(path, lines) {
      let res;
      try {
        res = await fetch("/api/" + path, { method: "POST", body: lines.join("\n") });
      } catch (e) {
        throw new BotFailure("crash", "bitbath serve stopped answering");
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new BotFailure("crash", data.error || "bitbath serve answered " + res.status);
      if (data.stderr) this.stderr += data.stderr;
      return data;
    }

    /** Starts the bot's process. */
    async connect() {
      this.session = (await this.call("start?bot=" + encodeURIComponent(this.name), [])).session;
    }

    /**
     * Writes lines to the bot, then reads its next line.
     * @param {string[]} lines @param {number} deadline when to stop waiting, as performance.now() counts
     * @param {string} late the forfeit's reason if the line doesn't come in time
     */
    async read(lines, deadline, late) {
      const wait = Math.max(0, Math.round(deadline - performance.now()));
      const data = await this.call("io?session=" + this.session + "&wait=" + wait, lines);
      if (data.eof) throw new BotFailure(data.reason || "crash", "the bot " + (data.note || "stopped"));
      if (data.timeout) throw new BotFailure(late, late === "init" ? "the bot sent no READY in time" : "the bot didn't end its turn within " + BOT_TURN_MS / 1000 + " seconds");
      return data;
    }

    /** @param {string[]} lines */
    async init(lines) {
      const deadline = performance.now() + this.initMs;
      let pending = lines;
      for (;;) {
        const data = await this.read(pending, deadline, "init");
        pending = [];
        if (data.line !== undefined && data.line.trim() === "READY") return;
      }
    }

    /** @param {string[]} lines @param {(line: string) => string[] | null} send */
    async turn(lines, send) {
      const deadline = performance.now() + BOT_TURN_MS;
      let pending = lines;
      for (;;) {
        const data = await this.read(pending, deadline, "hang");
        pending = [];
        if (data.long !== undefined) {
          // As the runner answers a line over the limit (SPEC §6.6).
          if (!data.long.startsWith("D")) pending = ["ERR limit lines may be at most 1024 bytes"];
          continue;
        }
        const line = data.line.trim();
        if (!line) continue;
        if (line.split(/\s+/)[0] === "END") return;
        const reply = send(line);
        if (!reply) return;
        pending = reply;
      }
    }

    takeStderr() {
      const text = this.stderr;
      this.stderr = "";
      return text;
    }

    /** Stops the bot's process, after sending it some last lines. @param {string[]} [lines] */
    async stop(lines) {
      const session = this.session;
      this.session = "";
      if (!session) return;
      remotes.delete(this);
      try {
        await this.call("stop?session=" + session, lines || []);
      } catch (e) {
        // The bot is gone either way.
      }
    }
  }

  /** The bots on disk now running, to stop them when the page goes away. @type {Set<RemoteBot>} */
  const remotes = new Set();
  window.addEventListener("pagehide", () => {
    for (const bot of remotes) if (bot.session) navigator.sendBeacon("/api/stop?session=" + bot.session);
  });

  /** How long the opponent's turn takes to play out on the map, in ms. */
  const TURN_MS = 700;

  /**
   * With supply on, a unit is marked as short of rations when what it
   * carries wouldn't last this many turns of marching and fighting.
   */
  const HUNGRY_TURNS = 4;

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
      this.recording = null;
      this.turnNumber = 0;
      /** Set by the viewer when the game has ended. */ this.over = false;
      /** Set when a replay or another game takes this one's place. */ this.closed = false;
      /** Not taking orders: the opponent is moving. */ this.busy = true;
      /** What the human saw of the opponent's last turn, as the viewer's marks. @type {any[]} */ this.seen = [];
      /** What that turn did to the human, in words. @type {string[]} */ this.log = [];
      /** The same in numbers, as [label, count, whether it is the human's loss]. @type {[string, number, boolean][]} */ this.tally = [];
      /** The hit points of the human's units as the opponent's turn began, by id. @type {Map<number, number>} */ this.hps = new Map();
      /** @type {any} */ this.world = null;
      /** The human's view of the state shown, and the helpers on it. @type {any} */ this.kit = null;
      /** The hex picked, and the unit picked on it, if any. @type {{hex: number[], unit: number | null} | null} */ this.sel = null;
      /** Where the picked unit can move, by hex index: see Kit.reachable. @type {Map<number, string>} */ this.reach = new Map();
      /** The enemy units the picked unit can attack. @type {any[]} */ this.targets = [];
      /** The enemy units it can attack this turn after a move, each with the hex to attack from. @type {{t: any, from: number[]}[]} */ this.chase = [];
      /** @type {Set<number>} */ this.zoc = new Set();
      /** The ids of the human's units with something left to do. @type {number[]} */ this.ready = [];
      /** Why the last order didn't do what was asked, if it didn't. */ this.problem = "";
      /** What went wrong with a bot on disk, if that ended the game. */ this.failNote = "";
      /** What the attack forecast on show was worked out from, so it is built once. */ this.forecastKey = "";
      /** The turn being played: its record in the replay. @type {any} */ this.turn = null;
      this.turnStart = 0;
      this.stats = [newStats(), newStats()];
      /** The types of the human's units, to name the ones the opponent destroys. @type {Map<number, string>} */ this.types = new Map();
      /** The transports the human's units were aboard as the opponent's turn began, by id. @type {Map<number, number>} */ this.carriers = new Map();
    }

    // Turns.

    /**
     * Starts a player's turn and returns its turn message.
     * @param {number} player @param {boolean} [clock] whether the message carries a clock: the rules' time for a turn that uses none of the bank
     * @returns {string[]}
     */
    begin(player, clock) {
      this.turn = { type: "turn", round: 0, player, time_ms: 0, bank_ms: 0, used_ms: 0, orders: [], rejected: 0 };
      V.append("T " + player, this.turn);
      this.turnStart = performance.now();
      const c = this.world.c;
      const bank = clock ? c.time_bank_ms || 5000 : 0;
      const time = clock ? bank + (c.time_increment_ms || 1000) : 0;
      this.turn.time_ms = time;
      this.turn.bank_ms = bank;
      const lines = v.engine.turnMessage(time, bank);
      this.turn.round = Number(lines[0].split(/\s+/)[1]);
      this.turnNumber++;
      this.recording?.turn(this.turnNumber, this.turn);
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
      this.recording?.turn(this.turnNumber, this.turn);
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
      this.recording?.turn(this.turnNumber, turn);
      return v.engine.end();
    }

    async botTurn() {
      this.busy = true;
      this.sel = null;
      this.problem = "";
      this.hps.clear();
      this.carriers.clear();
      for (const u of this.kit ? this.kit.view.myUnits() : []) {
        this.types.set(u.id, u.type);
        this.hps.set(u.id, u.hp);
        if (u.carrier) this.carriers.set(u.id, u.carrier);
      }
      const lines = this.begin(3 - this.human, this.bot.clock);
      const turn = this.turn;
      let over = false;
      /** @type {any} */ let failure = null;
      const play = this.bot.turn(lines, (/** @type {string} */ line) => {
        // Another game has taken the engine.
        if (this.closed) return null;
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
      }, v.replay.script);
      // A built-in bot has played by now. A bot on disk takes a while, and
      // may fail: then it forfeits, once its turn has been played out.
      try {
        await play;
      } catch (e) {
        failure = e instanceof BotFailure ? e : new BotFailure("crash", String(e));
      }
      if (this.closed) return;
      if (this.bot.takeStderr) {
        const text = this.bot.takeStderr();
        if (text) turn.stderr = text;
      }
      const ended = this.end();
      // Play the turn out on the map, as far as the human sees it.
      const ms = V.animateTo(v.steps, TURN_MS);
      if (ms) await sleep(ms + 40);
      if (this.closed) return;
      if (ended || failure) return this.finish(failure);
      // An attack shows where the attacker stood (SPEC §6.3); a move shows
      // only where the human watched all of it.
      this.seen = v.marks.filter((/** @type {any} */ m) => m.ok && ((m.op === "A" && m.hexes.length === 2) || (m.op === "M" && V.shown(m, this.human))));
      this.humanTurn();
    }

    humanTurn() {
      const lines = this.begin(this.human);
      const view = Kit.View.fromTurn(this.world, this.human, lines);
      const logs = view.logs;
      this.log = this.describe(logs).concat(this.hunger(view, logs));
      this.tally = this.count(logs);
      this.busy = false;
      // What the opponent did to the human's units, then the turn coming back.
      const struck = logs.some((t) => t[0] === "HIT");
      if (struck) BitbathSounds.play("hit");
      BitbathSounds.play("turn", struck ? 0.3 : 0);
      // As a step of playback, so the damage numbers of the opponent's
      // attacks go on floating into the human's turn, and the units that
      // starved as it began are seen to go.
      V.animateTo(v.steps, V.STEP_MS);
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
        if (t[0] === "SITE") return t[2] === String(this.human) ? "You lost site " + t[1] + "." : this.botName + " took site " + t[1] + ".";
        return t.join(" ");
      });
    }

    /**
     * What the turn's log doesn't say, with supply on: the human's units
     * that went down with a transport, the ones that starved to death as
     * this turn began, and the ones now out of rations.
     * @param {any} view the human's view as the turn begins @param {string[][]} logs @returns {string[]}
     */
    hunger(view, logs) {
      if (!this.world.supply) return [];
      const name = (/** @type {string} */ code) => (this.world.unitTypes[code] ? this.world.unitTypes[code].name : "unit");
      const killed = new Set(logs.filter((t) => t[0] === "HIT" && t[6] === "0").map((t) => Number(t[5])));
      const out = [];
      for (const id of this.hps.keys()) {
        if (view.units.has(id) || killed.has(id)) continue;
        const sunk = killed.has(this.carriers.get(id) || 0);
        out.push("Your " + name(this.types.get(id) || "") + (sunk ? " went down with its transport." : " starved to death."));
      }
      // A unit with no rations left that lost hit points to nobody starved.
      const hit = new Set(logs.filter((t) => t[0] === "HIT").map((t) => Number(t[5])));
      for (const u of view.myUnits()) {
        if (u.rations) continue;
        const lost = hit.has(u.id) ? 0 : (this.hps.get(u.id) || 0) - u.hp;
        out.push("Your " + name(u.type) + " at (" + u.q + "," + u.r + ") " + (lost > 0 ? "is starving: it lost " + lost + " hp." : "is out of rations."));
      }
      return out;
    }

    /** Adds the opponent's last turn up, from the same LOG lines. @param {string[][]} logs @returns {[string, number, boolean][]} */
    count(logs) {
      let [hits, damage, lost, killed, cities] = [0, 0, 0, 0, 0];
      for (const t of logs) {
        if (t[0] === "HIT") {
          const [id, hp] = [Number(t[5]), Number(t[6])];
          const before = this.hps.get(id);
          hits++;
          if (before !== undefined) damage += Math.max(0, before - hp);
          this.hps.set(id, hp);
          if (!hp) lost++;
          if (t[7] === "0") killed++;
        } else if (t[0] === "CITY" && t[2] === String(this.human)) cities++;
      }
      return [["attacks", hits, true], ["damage", damage, true], ["units lost", lost, true], ["cities lost", cities, true], ["they lost", killed, false]];
    }

    endTurn() {
      if (!this.myTurn()) return;
      if (this.end()) return this.finish();
      BitbathSounds.play("end");
      this.botTurn();
    }

    /**
     * The game has ended: records the result, and hands the game to the viewer as a finished replay.
     * @param {any} [failure] what went wrong with a bot on disk: it forfeits, unless the game ended by itself
     */
    finish(failure) {
      const st = v.engine.seek(v.steps);
      const [winner, reason] = st.outcome || (failure ? [this.human, failure.reason] : [0, "draw"]);
      this.failNote = failure && !st.outcome ? V.capitalize(failure.message) + "." : "";
      const result = { type: "result" };
      if (this.bot.stop) {
        // The bot hears how it ended (SPEC §6.4), and what it says last goes into the replay.
        const seat = 2 - this.human;
        this.bot.stop(["GAMEOVER " + winner + " " + reason]).then(() => (/** @type {any} */ (result).stderr[seat] = this.bot.takeStderr()));
      }
      const count = (/** @type {(p: number) => number} */ f) => [f(1), f(2)];
      this.busy = true;
      this.sel = null;
      closeMenu();
      BitbathSounds.play(!winner ? "draw" : winner === this.human ? "win" : "lose", 0.25);
      V.finishLive(Object.assign(result, {
        winner,
        reason,
        round: st.round,
        cities: count((p) => st.cities.filter((/** @type {number[]} */ c) => c[0] === p).length),
        units: count((p) => st.units.filter((/** @type {any[]} */ u) => u[1] === p).length),
        stats: this.stats,
        stderr: ["", ""],
      }));
      this.recording?.finish(result);
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
      if (!kit.canAct(u)) return false;
      if (kit.reachable(u, this.zoc).size > 0 || kit.canCapture(u) || (kit.canAttack(u) && kit.targetsInRange(u).length > 0)) return true;
      // A wagon that has stopped still has wheat to turn into bread, or a
      // site's wheat to take on. A city's bread is no reason: it can stay.
      return (u.wheat > 0 && kit.unloadable(u) > 0) || (!!kit.view.siteAt(u.hex) && kit.loadable(u) > 0);
    }

    /** Gives one of the human's orders, and shows what it did. @param {string} line */
    order(line) {
      if (!this.myTurn()) return;
      closeMenu();
      this.show(line, this.send(line));
    }

    /**
     * Moves the unit picked to the hex a plan attacks from, and attacks from
     * there: what a click on an enemy out of range does, and what the
     * forecast for it showed. If a hidden enemy stops the move short, the
     * attack is off.
     * @param {any} unit @param {{t: any, from: number[]}} plan
     */
    advance(unit, plan) {
      const dirs = this.reach.get(this.world.index(plan.from));
      if (!this.myTurn() || dirs === undefined) return;
      closeMenu();
      const move = "M " + unit.id + " " + dirs;
      const reply = this.send(move);
      const t = (reply[reply.length - 1] || "").split(/\s+/);
      if (t[0] !== "OK" || Number(t[1]) !== plan.from[0] || Number(t[2]) !== plan.from[1]) return this.show(move, reply);
      BitbathSounds.play("move");
      // Shown from before the move, so the unit slides there and then strikes.
      const attack = "A " + unit.id + " " + plan.t.id;
      this.show(attack, this.send(attack), 0.1);
    }

    /**
     * Shows what an order of the human's did: plays its sound, slides the
     * units, and ends the game if it did.
     * @param {string} line @param {string[]} reply @param {number} [delay] how long the sound waits, in seconds
     */
    show(line, reply, delay) {
      const t = (reply[reply.length - 1] || "").split(/\s+/);
      this.problem = "";
      if (t[0] === "ERR") this.problem = V.capitalize(t.slice(2).join(" ")) + ".";
      else if (t[0] === "PART") this.problem = t[3] === "ambush" ? "Ambush! A hidden enemy blocked the way." : "Stopped next to a hidden enemy.";
      BitbathSounds.play(soundOf(line[0], t), delay);
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
      // The hover card marks the unit picked.
      V.refreshHover();
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

    /**
     * A wagon's L or U order (STRATEGIC.md §4), for all it can move or at most `amount`.
     * @param {string} op "L" or "U" @param {number} [amount]
     */
    carry(op, amount) {
      const unit = this.unit();
      if (!unit || !this.myTurn()) return;
      if (!(op === "L" ? this.kit.loadable(unit) : this.kit.unloadable(unit))) return;
      // The amount typed is for this one order.
      carryAmount = "";
      this.order(op + " " + unit.id + (amount ? " " + amount : ""));
    }

    /** Why a city of the human's can't build a unit type right now, or "" if it can. @param {any} city @param {any} type */
    cantBuild(city, type) {
      const view = this.kit.view;
      if (city.built) return "This city has built its unit for this turn.";
      if (type.naval) {
        // A ship goes onto the water next to a port (STRATEGIC.md §6).
        if (!this.world.isPort(city.hex)) return "Only a port, a city next to water, builds ships.";
        if (!this.kit.launchHex(city)) return "The water next to this city has no room for a ship.";
      } else if (view.friendsAt(city.hex) >= this.world.c.stack_limit) return "The city is full: move a unit out to build.";
      // With supply on, each city pays from its own bread.
      if (this.world.supply) return city.bread < type.cost ? V.capitalize(type.name) + " costs " + type.cost + " bread, and this city has " + city.bread + "." : "";
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
        // An enemy it can attack after a move: it goes into range and attacks.
        const chase = this.chase.filter((p) => p.t.q === hex[0] && p.t.r === hex[1]);
        if (chase.length === 1) return this.advance(unit, chase[0]);
        if (chase.length > 1) {
          return openMenu(x, y, chase.map((p) => ({ label: "Move and attack the " + this.kit.unitType(p.t).name + " · " + p.t.hp + " hp", run: () => this.advance(unit, p) })));
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
      const on = (/** @type {any} */ t) => !!hex && t.q === hex[0] && t.r === hex[1];
      const can = hex && this.unit() && this.myTurn() && (this.reach.has(this.world.index(hex)) || this.targets.some(on) || this.chase.some((p) => on(p.t)));
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
      else if (key === "L" && unit && this.kit.loadable(unit)) this.carry("L", amountAsked());
      else if (key === "U" && unit && this.kit.unloadable(unit)) this.carry("U", amountAsked());
      else if (this.world.unitTypes[key] && city && city.owner === this.human) this.build(city, key);
      else return;
      e.preventDefault();
    }

    // What the viewer asks for.

    /** Another game or a replay is taking this one's place. */
    close() {
      this.closed = true;
      if (this.bot.stop) this.bot.stop();
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
        // The enemies out of range that it can still attack this turn, by moving first.
        this.chase = [];
        if (unit && mine && !unit.carrier && this.reach.size) {
          const hurts = this.world.damage[unit.type] || {};
          for (const t of kit.view.enemyUnits()) {
            if (t.carrier || !(hurts[t.type] > 0) || this.targets.includes(t)) continue;
            const plan = this.attackFrom(unit, t);
            if (plan.when === "move" && plan.from) this.chase.push({ t, from: plan.from });
          }
        }
        this.ready = mine ? kit.view.myUnits().filter((/** @type {any} */ u) => this.canDo(u)).map((/** @type {any} */ u) => u.id).sort((/** @type {number} */ a, /** @type {number} */ b) => a - b) : [];
      }
      this.render();
    }

    /** Whether a unit is picked that can neither move to the hex this turn nor attack onto it. @param {number} q @param {number} r */
    outOfReach(q, r) {
      const unit = this.unit();
      if (!unit || this.over || !this.myTurn()) return false;
      if (unit.hex[0] === q && unit.hex[1] === r) return false;
      const on = (/** @type {any} */ t) => t.q === q && t.r === r;
      return !this.reach.has(this.world.index([q, r])) && !this.targets.some(on) && !this.chase.some((p) => on(p.t));
    }

    /** Extra lines for the viewer's hover box: what a click on the hex would do. @param {number} q @param {number} r @returns {string[]} */
    hoverLines(q, r) {
      const unit = this.unit();
      if (!unit || !this.myTurn()) return [];
      const out = this.targets.filter((t) => t.q === q && t.r === r).map((t) => "attack: " + this.forecast(t));
      if (this.chase.some((p) => p.t.q === q && p.t.r === r)) out.push("click to move into range and attack");
      const i = this.world.index([q, r]);
      const dirs = this.reach.get(i);
      if (dirs !== undefined) {
        const type = this.kit.unitType(unit);
        // A hex the unit couldn't otherwise enter is a transport to board, for 1 MP.
        const boards = !type.naval && !unit.carrier && !this.world.passable([q, r]);
        const cost = Kit.walk(unit.hex, dirs).slice(1).reduce((/** @type {number} */ sum, /** @type {number[]} */ h) => sum + (this.world.cost(h, type.naval) || 1), 0);
        const stops = this.zoc.has(i) && !type.skirmish;
        if (boards) out.push("board the transport: " + cost + " of " + unit.mp + " mp, and it ends the move");
        else if (unit.carrier) out.push("land: it ends the move, but the unit can still attack or capture");
        else out.push("move: " + cost + " of " + unit.mp + " mp" + (stops ? ", and stop in a zone of control" : ""));
        if (this.kit.view.friendsAt([q, r])) out.push(boards ? "right-click to board" : "right-click to move onto your own units");
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
            if (!inRange(h) || this.world.index(h) < 0 || !this.world.cost(h, type.naval)) continue;
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
      // Not the units aboard a transport, which can't be attacked, nor the
      // ones this unit can't hurt, nor anything for a unit that is aboard.
      const hurts = can && !unit.carrier ? this.world.damage[unit.type] || {} : {};
      const foes = can ? this.kit.view.enemyUnits().filter((/** @type {any} */ t) => t.q === hex[0] && t.r === hex[1] && !t.carrier && hurts[t.type] > 0) : [];
      const plans = foes.map((/** @type {any} */ t) => ({ t, ...this.attackFrom(unit, t) }));
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
      if (when === "move") card.append(el("div", "After moving into range. Click to move there and attack.", "fc-hint"));
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
      // The way to the hex under the pointer, or to where the unit would attack the enemy there from.
      const hover = v.hover;
      const plan = hover ? this.chase.find((p) => p.t.q === hover[0] && p.t.r === hover[1]) : undefined;
      const dirs = hover ? this.reach.get(this.world.index(plan ? plan.from : hover)) : undefined;
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
      const s = v.geo.size;
      // Small and a little see-through: it's a reminder, not a call to act.
      const r = Math.max(3.5, s * 0.14);
      ctx.save();
      ctx.globalAlpha = 0.85;
      ctx.lineCap = "round";
      for (const city of this.kit.view.cities.values()) {
        if (city.owner !== this.human || types.every((t) => this.cantBuild(city, t))) continue;
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

    /**
     * With supply on: a mark on each of the human's units that is short of
     * rations. It is amber when the rations left wouldn't last
     * `HUNGRY_TURNS` turns of marching and fighting, and red when there are
     * none. The viewer draws the bread and wheat on the map.
     * @param {CanvasRenderingContext2D} ctx
     */
    drawSupply(ctx) {
      if (this.over || !this.kit || !this.world.supply) return;
      const view = this.kit.view;
      const c = this.world.c;
      ctx.save();
      const low = HUNGRY_TURNS * ((c.eat_base || 0) + (c.eat_move || 0) + (c.eat_attack || 0));
      for (const u of view.myUnits()) {
        const at = u.carrier ? null : v.unitPos.get(u.id);
        if (!at || u.rations >= low) continue;
        const [x, y, rad] = at;
        const r = Math.max(3, rad * 0.36);
        const [bx, by] = [x - rad * 0.8, y + rad * 0.45];
        ctx.beginPath();
        ctx.arc(bx, by, r, 0, 2 * Math.PI);
        ctx.fillStyle = u.rations ? "#fab005" : "#f03e3e";
        ctx.fill();
        ctx.lineWidth = Math.max(1, r * 0.25);
        ctx.strokeStyle = "rgba(0,0,0,.75)";
        ctx.stroke();
        // An exclamation mark.
        ctx.fillStyle = "rgba(0,0,0,.85)";
        ctx.fillRect(bx - r * 0.14, by - r * 0.6, r * 0.28, r * 0.72);
        ctx.fillRect(bx - r * 0.14, by + r * 0.3, r * 0.28, r * 0.28);
      }
      ctx.restore();
    }

    /** Over the units: a ring on the picked unit or hex, and on each enemy the unit can attack. @param {CanvasRenderingContext2D} ctx */
    drawOver(ctx) {
      this.showForecast();
      this.drawBuildBadges(ctx);
      this.drawSupply(ctx);
      if (this.over || !this.sel) return;
      const s = v.geo.size;
      const unit = this.unit();
      // A unit aboard isn't drawn: the ring goes on its transport.
      const at = unit ? v.unitPos.get(unit.carrier || unit.id) : null;
      ctx.save();
      ctx.strokeStyle = V.cssVar("--text");
      ctx.lineWidth = Math.max(2, s * 0.1);
      if (at) {
        // The ring has the token's shape: a rounded square for a ship, or for the transport a unit is aboard.
        const shown = unit.carrier ? this.kit.view.units.get(unit.carrier) : unit;
        const rad = at[2] + ctx.lineWidth;
        ctx.beginPath();
        if (shown && this.kit.unitType(shown).naval) ctx.roundRect(at[0] - rad, at[1] - rad, 2 * rad, 2 * rad, rad * 0.45);
        else ctx.arc(at[0], at[1], rad, 0, 2 * Math.PI);
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
      // Fainter and thinner, the enemies it can attack after a move.
      ctx.globalAlpha = 0.5;
      ctx.lineWidth = Math.max(1.5, s * 0.08);
      for (const key of new Set(this.chase.map((p) => this.world.index(p.t.hex)))) {
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
      if (this.over) note = (this.failNote ? this.failNote + " " : "") + "Step through the game below, or save it as a replay.";
      else if (this.problem) note = this.problem;
      else if (mine) note = this.ready.length ? this.ready.length + (this.ready.length === 1 ? " unit has" : " units have") + " something left to do." : "No unit has anything left to do.";
      $("play-note").textContent = $("play-note").title = note;
      $("play-note").classList.toggle("warn", !this.over && !!this.problem);
      $("end-turn").hidden = $("next-unit").hidden = this.over;
      $("save-replay").hidden = $("play-again").hidden = !this.over;
      $("selection").hidden = $("play-last").hidden = this.over;
      $("end-turn").disabled = !mine;
      $("next-unit").disabled = !mine || !this.ready.length;
      const tally = $("play-tally");
      tally.replaceChildren();
      for (const [label, n, bad] of this.tally) {
        const cell = el("div", "", n ? (bad ? "bad" : "good") : "");
        cell.append(el("b", String(n)), el("span", label));
        tally.append(cell);
      }
      const log = $("play-log");
      log.replaceChildren();
      for (const line of this.log.length ? this.log : ["Nothing happened to your units or cities."]) log.append(el("div", line));
      this.renderSelection(mine);
    }

    /**
     * What is on the hex picked, and the orders that can be given there, as
     * a card of up to four parts: the place, the units on it, what the unit
     * picked can do, and what the city can build. It is as tall as what it
     * holds, so nothing in it scrolls.
     * @param {boolean} mine whether the human can give orders
     */
    renderSelection(mine) {
      const box = $("selection");
      // Redrawing removes the button that was just clicked or tabbed to.
      // The focus it had goes to the row of the unit picked, or to the
      // board, so someone using the keyboard keeps their place.
      const focused = box.contains(document.activeElement);
      box.replaceChildren();
      box.classList.toggle("empty", !this.sel || !this.kit);
      if (!this.sel || !this.kit) {
        box.append(el("div", "Click one of your units to give it orders, or one of your cities to build."));
        if (focused) $("canvas").focus({ preventScroll: true });
        return;
      }
      this.fillSelection(box, mine);
      if (focused) /** @type {HTMLElement} */ (box.querySelector(".sel-unit.chosen") || $("canvas")).focus({ preventScroll: true });
    }

    /** The parts of the selection card. @param {HTMLElement} box @param {boolean} mine */
    fillSelection(box, mine) {
      const kit = this.kit;
      const view = kit.view;
      const world = this.world;
      const c = world.c;
      const hex = this.sel.hex;
      const city = view.cityAt(hex);
      const site = view.siteAt(hex);
      const unit = this.unit();
      const supply = world.supply;
      const seen = V.visibleTo(hex[0], hex[1], this.human);
      const num = (/** @type {number} */ n) => n.toLocaleString();
      const whose = (/** @type {number} */ p) => (!p ? "neutral" : p === this.human ? "yours" : this.botName + "'s");
      /** A part of the card. @param {string} cls @param {...(Node | string)} kids */
      const part = (cls, ...kids) => {
        const sec = el("div", "", "sel-sec " + cls);
        sec.append(...kids);
        box.append(sec);
        return sec;
      };
      /** A button whose key is shown on it. @param {string} text @param {string} key @param {() => void} run @param {string} cls */
      const action = (text, key, run, cls) => {
        const b = el("button", text, cls);
        b.append(el("kbd", key));
        b.onclick = run;
        return b;
      };
      // Each unit and each thing to build shows its token as on the board, so the symbols explain themselves.
      const token = (/** @type {any} */ type, /** @type {number} */ owner, /** @type {number} */ px) => {
        const info = v.info.units[type.code];
        return BitbathPieces.icon({ code: type.code, name: type.name, naval: !!info && info.flags.split(",").includes("naval") }, V.playerColor(owner), px);
      };

      // The place: its name, whose it is, and what it holds.
      const terrain = world.terrain(hex);
      const name = el("div", "", "sel-name");
      name.append(el("b", city ? "City " + city.id : site ? "Site " + site.id : V.capitalize(terrain.name)));
      const facts = ["(" + hex[0] + "," + hex[1] + ")"];
      if (city) facts.push(whose(city.owner));
      if (city && supply && world.isPort(hex)) facts.push("port");
      if (site) facts.push("no owner", "grows " + site.output + " a round");
      if (!city && !site) facts.push("defense " + terrain.defense + "%");
      name.append(el("span", facts.join(" · ")));
      const head = part("sel-head", name);
      /** What the place holds, as a number over its unit. @param {number} n @param {string} of */
      const stock = (n, of) => {
        const s = el("div", "", "sel-stock");
        s.append(el("b", num(n)), el("span", of));
        head.append(s);
      };
      if (city && supply && seen) stock(city.bread, "bread");
      else if (site && seen) stock(site.wheat, "of " + num(site.cap) + " wheat");
      else if (city && !supply && city.owner === this.human) stock(view.gold, "gold");

      // The units, each transport followed by the units aboard it.
      const here = view.unitsAt(hex);
      const listed = here.filter((/** @type {any} */ u) => !u.carrier).flatMap((/** @type {any} */ u) => [u].concat(here.filter((/** @type {any} */ p) => p.carrier === u.id)));
      const hungry = HUNGRY_TURNS * ((c.eat_base || 0) + (c.eat_move || 0) + (c.eat_attack || 0));
      if (listed.length) {
        const list = part("sel-units");
        for (const u of listed) {
          const type = kit.unitType(u);
          const own = u.owner === this.human;
          const ready = own && this.ready.includes(u.id);
          const cls = "sel-unit" + (unit && unit.id === u.id ? " chosen" : "") + (own && !ready ? " spent" : "") + (u.carrier ? " aboard" : "");
          const row = own ? el("button", "", cls) : el("div", "", cls + " enemy");
          if (own) row.onclick = () => this.select({ hex, unit: u.id });
          const top = el("div", "", "sel-top");
          const state = !own ? "enemy" : u.fresh ? "new" : u.acted ? "done" : u.mp + " mp";
          top.append(el("b", V.capitalize(type.name)), el("span", state, "sel-pill" + (ready ? " ready" : "")));
          if (own && u.fresh) row.title = "Built this turn: it can move and act from your next turn.";
          const bar = el("div", "", "sel-bar");
          const fill = el("span", "");
          const frac = Math.max(0, Math.min(1, u.hp / type.max_hp));
          fill.style.width = 100 * frac + "%";
          fill.style.background = frac > 0.6 ? "#3fb950" : frac > 0.3 ? "#e3b341" : "#f85149";
          bar.append(fill);
          // The hit points, and what else there is to know: digging in, and with supply on, food, cargo and who is aboard.
          const tags = el("div", "", "sel-tags");
          /** @param {string} text @param {string} [cls] */
          const tag = (text, cls) => {
            if (tags.childNodes.length) tags.append(" · ");
            tags.append(cls ? el("span", text, cls) : text);
          };
          tag(u.hp + " hp");
          if (u.fort) tag("dug in " + u.fort);
          if (supply) tag(u.rations + " rations", u.rations < hungry ? "hungry" : "");
          if (type.carry) tag([u.wheat ? num(u.wheat) + " wheat" : "", u.bread ? num(u.bread) + " bread" : ""].filter(Boolean).join(", ") || "empty");
          if (type.transport) tag(view.passengers(u).length + " of " + (c.passengers_max || 0) + " aboard");
          if (u.carrier) tag("aboard");
          const main = el("div", "", "sel-main");
          main.append(top, bar, tags);
          row.append(token(type, u.owner, 24), main);
          list.append(row);
        }
      }

      // What the unit picked can do here besides moving and attacking, which are clicks on the map.
      if (unit && mine) {
        const [load, unload] = [kit.loadable(unit), kit.unloadable(unit)];
        if (kit.canCapture(unit) || load || unload) {
          const acts = part("sel-acts");
          const row = el("div", "", "sel-row");
          acts.append(row);
          if (kit.canCapture(unit)) {
            const prize = kit.prize(unit);
            const progress = (prize.capturer === this.human ? prize.progress : 0) + unit.hp;
            const what = progress >= c.capture_points ? "takes the city" : progress + " of " + c.capture_points;
            row.append(action("Capture · " + what, "C", () => this.capture(), "primary"));
          }
          // A wagon's goods: wheat from a site, bread from a city, and both into a city.
          if (load) row.append(action("Load " + num(load) + (city ? " bread" : " wheat"), "L", () => this.carry("L", amountAsked()), "primary"));
          if (unload) row.append(action("Unload " + num(unload), "U", () => this.carry("U", amountAsked()), load ? "" : "primary"));
          if (load || unload) {
            const amount = /** @type {HTMLInputElement} */ (el("input", "", ""));
            amount.id = "carry-amount";
            amount.type = "number";
            amount.min = "1";
            amount.placeholder = "all";
            amount.title = "The most to load or unload. Empty moves all of it.";
            amount.value = carryAmount;
            amount.oninput = () => (carryAmount = amount.value);
            const most = el("label", "at most ", "sel-amount");
            most.append(amount);
            row.append(most);
            if (unit.mp > 0) acts.append(el("div", "Loading or unloading ends the wagon's move.", "sel-hint"));
          }
        }
      }

      // What the city can build: a tile for each unit type, with its key.
      if (city && city.owner === this.human && mine) {
        const types = /** @type {any[]} */ (Object.values(world.unitTypes));
        const whys = types.map((type) => this.cantBuild(city, type));
        const build = part("sel-build");
        const title = el("div", "", "sel-title");
        title.append(el("b", "Build"), el("span", city.built ? "done for this turn" : "one unit a turn"));
        const grid = el("div", "", "sel-grid");
        // The default game's five types fit one row.
        if (types.length <= 5) grid.style.gridTemplateColumns = "repeat(" + types.length + ", minmax(0, 1fr))";
        types.forEach((type, i) => {
          const b = el("button", "", "sel-tile");
          b.onclick = () => this.build(city, type.code);
          b.disabled = !!whys[i];
          b.title = (whys[i] ? whys[i] + " " : "") + V.capitalize(type.name) + ": move " + type.move + ", vision " + type.vision + ", range " + (type.range_min === type.range_max ? type.range_min : type.range_min + "–" + type.range_max) + ". Key " + type.code + ".";
          b.append(el("kbd", type.code), token(type, this.human, 24), el("span", V.capitalize(type.name)), el("b", String(type.cost)));
          grid.append(b);
        });
        build.append(title, grid);
        // When nothing can be built, the reason the cheapest unit can't.
        if (whys.every(Boolean)) build.append(el("div", whys[types.indexOf(types.reduce((a, b) => (b.cost < a.cost ? b : a)))], "sel-hint"));
      }
    }
  }

  // The tokens in the side panel follow the style picked in the settings.
  BitbathPieces.onChange(() => current && current.render());

  /**
   * The sound of one of the human's orders (sounds.js), from its letter and
   * the tokens of its reply (SPEC §6.4).
   * @param {string} op @param {string[]} reply
   */
  function soundOf(op, reply) {
    if (reply[0] === "ERR") return "error";
    // An ambush stops the move with a blow that never lands.
    if (op === "M") return reply[0] === "PART" && reply[3] === "ambush" ? "hit" : "move";
    // OK <target's hp> <attacker's hp>.
    if (op === "A") return reply[1] === "0" ? "kill" : "attack";
    if (op === "C") return reply[1] === "captured" ? "captured" : "capture";
    if (op === "B") return "build";
    return op === "L" || op === "U" ? "carry" : "";
  }

  /** What is typed in the wagon's amount field, kept while the side panel is redrawn. */
  let carryAmount = "";

  /** Shows, on the help line under the board, the keys the strategic rules add. @param {boolean} on */
  function supplyHelp(on) {
    let extra = document.getElementById("supply-help");
    if (!extra) {
      extra = el("span", " With a wagon, ", "");
      extra.id = "supply-help";
      extra.append(el("kbd", "L"), " loads and ", el("kbd", "U"), " unloads. Right-click one of your transports to board it; a unit aboard lands on a tinted hex.");
      $("live-help").append(extra);
    }
    extra.hidden = !on;
  }

  /** The most a wagon should load or unload, as typed, or undefined for all of it. */
  function amountAsked() {
    const n = Math.floor(Number(carryAmount));
    return n > 0 ? n : undefined;
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
   * Starts a game against a bot: one built into the page, or one on disk.
   * @param {{bot: string, map: string, human: number, seed: number, name?: string}} options
   */
  async function start(options) {
    const kind = opponents.find((b) => b.id === options.bot) || opponents[0];
    const map = MAPS.find((m) => m.id === options.map) || MAPS[0];
    if (!canPlayMap(kind, map)) throw new Error(kind.name + " cannot play " + map.name + ".");
    const human = options.human === 2 ? 2 : 1;
    const engine = await V.engine();
    const setup = engine.setup(map.config + "\nseed = " + options.seed);
    const name = (options.name ?? GameRecording.savedName()).trim().slice(0, 48);
    const names = human === 1 ? [name || "Human", kind.name] : [kind.name, name || "Human"];
    const header = Object.assign({ type: "header" }, engine.versions(), {
      map: "seed:" + options.seed,
      seed: options.seed,
      players: names.map((name, index) => ({ name, command: index + 1 === human ? "a person in the browser" : kind.command })),
      setup,
    });
    // A bot on disk that can't start stops the game before it opens.
    const bot = await kind.create(options.seed);
    const game = new Game(human, bot, kind.name);
    game.world = new Kit.World(setup);
    if (!(await V.openLive(header, game))) {
      if (bot.stop) bot.stop();
      throw new Error($("error").textContent);
    }
    current = game;
    // The address may still name the replay that was open. A refresh must
    // not bring that back in place of this game.
    try {
      const url = new URL(location.href);
      if (url.searchParams.has("replay") || url.searchParams.has("position")) {
        url.searchParams.delete("replay");
        url.searchParams.delete("position");
        history.replaceState(null, "", url);
      }
    } catch (e) {
      // A page opened as a file may not change its address.
    }
    supplyHelp(game.world.supply);
    game.recording = GameRecording.start({ player: name, opponent: kind.name, map: map.name, seat: human, header });
    try {
      if (bot.initMs) bot.initMs = game.world.c.init_ms || bot.initMs;
      await bot.init(engine.initMessage(3 - human), setup);
    } catch (e) {
      if (!game.closed) game.finish(e instanceof BotFailure ? e : new BotFailure("crash", String(e)));
      return game;
    }
    if (game.closed) return game;
    if (human === 1) game.humanTurn();
    else game.botTurn();
    return game;
  }

  // The new game dialog.

  /**
   * The bots to play against: the ones built into the page, then the ones
   * `bitbath serve` offers, if that is what serves the page. A bot's
   * `rules` are the rule sets it knows, and its `maps` the maps it's made
   * for, both from the bot index (bots/index.json). A bot on disk that the
   * index doesn't know may play any.
   * @type {{id: string, name: string, label?: string, about: string, rules: string[] | "any", maps: string[] | "any", command: string, create: (seed: number) => any}[]}
   */
  const opponents = Bots.list.map((/** @type {any} */ b) => Object.assign({ command: "built-in " + b.id + " bot" }, b));

  /** @param {string} id @param {{id: string, name: string, label?: string, about: string}[]} items @param {string} aboutId */
  function fill(id, items, aboutId) {
    const select = $(id);
    for (const item of items) {
      const option = el("option", item.label || item.name);
      option.value = item.id;
      select.append(option);
    }
    const about = () => ($(aboutId).textContent = (items.find((x) => x.id === select.value) || items[0]).about);
    select.onchange = about;
    return about;
  }

  const abouts = [fill("opponent", opponents, "opponent-about"), fill("map-size", MAPS, "map-about")];
  $("opponent").value = "greedy";
  const query = new URLSearchParams(location.search);
  const mapId = query.get("map");
  if (MAPS.some((m) => m.id === mapId)) $("map-size").value = mapId;
  const botId = query.get("bot");
  if (opponents.some((b) => b.id === botId)) $("opponent").value = botId;

  /** Whether a bot is made for a map, and knows its rules. @param {any} bot @param {any} map */
  function canPlayMap(bot, map) {
    const has = (/** @type {string[] | "any"} */ list, /** @type {string} */ name) => list === "any" || list.includes(name);
    return has(bot.maps, map.id) && has(bot.rules, map.rules || "classic");
  }

  /** Offers the opponents that can play the map picked, and picks one of them if the one picked can't. */
  function offerOpponents() {
    const map = MAPS.find((m) => m.id === $("map-size").value) || MAPS[0];
    const select = /** @type {HTMLSelectElement} */ ($("opponent"));
    const fits = (/** @type {string} */ id) => {
      const kind = opponents.find((b) => b.id === id);
      return !!kind && canPlayMap(kind, map);
    };
    for (const option of Array.from(select.options)) option.hidden = option.disabled = !fits(option.value);
    if (!fits(select.value)) select.value = fits("greedy") ? "greedy" : (opponents.find((b) => fits(b.id)) || opponents[0]).id;
    abouts.forEach((about) => about());
  }
  $("map-size").addEventListener("change", offerOpponents);
  offerOpponents();

  // The bots on disk. A page opened as a file, or served by anything but
  // `bitbath serve`, has none.
  if (location.protocol.startsWith("http")) {
    fetch("/api/bots")
      .then((res) => (res.ok ? res.json() : { bots: [] }))
      .then((/** @type {{bots: {name: string, command: string, index: any}[]}} */ data) => {
        for (const b of data.bots || []) {
          // The server sends the bot's record in the bot index, if it has one.
          const known = b.index;
          const name = known ? known.name : b.name;
          const item = {
            id: "disk:" + b.name,
            name,
            label: name + " (on disk)",
            about: (known ? known.about + " " : "") + "Runs on this machine: " + b.command,
            rules: known ? known.rules : "any",
            maps: known ? known.maps : "any",
            command: b.command,
            create: async () => {
              const bot = new RemoteBot(b.name, b.command);
              await bot.connect();
              remotes.add(bot);
              return bot;
            },
          };
          opponents.push(item);
          const option = el("option", item.label);
          option.value = item.id;
          $("opponent").append(option);
        }
        // Hides the ones that can't play the map picked.
        offerOpponents();
      })
      .catch(() => {});
  }

  // Starting a game drops the one being played, so the dialog says so.
  const abandonNote = el("div", "Starting a new game ends the one you are playing. It can't be resumed.", "wide");
  abandonNote.id = "new-game-abandon";
  abandonNote.setAttribute("role", "status");
  $("start").before(abandonNote);

  function openDialog() {
    $("player-name").value = GameRecording.savedName();
    $("new-game-error").textContent = "";
    // A new game gets a new map, unless a seed is typed again.
    $("seed").value = "";
    abandonNote.hidden = !(current && !current.over && !current.closed);
    $("new-game").showModal();
  }

  $("new-game-button").onclick = $("play-button").onclick = $("play-again").onclick = openDialog;
  $("player-name").onchange = () => GameRecording.setName($("player-name").value.trim());
  if (!query.has("replay")) openDialog();
  $("new-game").addEventListener("click", (/** @type {MouseEvent} */ e) => {
    if (e.target === $("new-game")) $("new-game").close();
  });
  $("start").onclick = async () => {
    $("start").disabled = true;
    $("start").textContent = "Starting…";
    $("new-game-error").textContent = "";
    const typed = $("seed").value.trim();
    const name = $("player-name").value.trim();
    GameRecording.setName(name);
    const seed = /^\d{1,15}$/.test(typed) ? Number(typed) : Math.floor(Math.random() * 1e6);
    try {
      await start({ bot: $("opponent").value, map: $("map-size").value, human: Number($("side-choice").value), seed, name });
      $("new-game").close();
      $("arena").scrollIntoView({ block: "start" });
      $("arena").focus({ preventScroll: true });
    } catch (e) {
      $("new-game-error").textContent = String(e instanceof Error ? e.message : e);
    } finally {
      $("start").disabled = false;
      $("start").textContent = "Start";
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
