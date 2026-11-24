// @ts-check
/* global BITBATH_WASM_BASE64 */
"use strict";

/** The engine, behind the C ABI in wasm/src/lib.rs. */
class BitbathEngine {
  /** @param {any} exports */
  constructor(exports) {
    this.ex = exports;
    this.enc = new TextEncoder();
    this.dec = new TextDecoder();
  }

  /** Splits a replay into records and builds the engine's step script. @param {string} text */
  static parseReplay(text) {
    const records = text.split(/\r?\n/).filter((l) => l.trim()).map((l) => JSON.parse(l));
    const header = records[0];
    if (!header || header.type !== "header") throw new Error("This isn't a bitbath replay.");
    const turns = records.filter((r) => r.type === "turn");
    const script = [];
    const stepTurn = [];
    const turnStart = [];
    turns.forEach((t, i) => {
      turnStart.push(script.length);
      script.push("T " + t.player);
      stepTurn.push(i);
      for (const o of t.orders || []) {
        script.push("O " + o);
        stepTurn.push(i);
      }
    });
    return {
      header,
      turns,
      script,
      stepTurn,
      turnStart,
      init: records.find((r) => r.type === "init") || null,
      result: records.find((r) => r.type === "result") || null,
    };
  }

  static async create() {
    if (typeof BITBATH_WASM_BASE64 === "undefined") {
      throw new Error("web/bitbath_wasm.js is missing. Build it with: python scripts/build_web.py");
    }
    const bin = atob(BITBATH_WASM_BASE64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const { instance } = await WebAssembly.instantiate(bytes, {});
    return new BitbathEngine(instance.exports);
  }

  /** @param {string} text @returns {[number, number]} */
  put(text) {
    const b = this.enc.encode(text);
    const p = this.ex.bb_alloc(b.length);
    new Uint8Array(this.ex.memory.buffer, p, b.length).set(b);
    return [p, b.length];
  }

  out() {
    return this.dec.decode(new Uint8Array(this.ex.memory.buffer, this.ex.bb_out_ptr(), this.ex.bb_out_len()));
  }

  /** @param {string} setup @param {string} script */
  load(setup, script, openTurn = false) {
    const [sp, sl] = this.put(setup);
    const [cp, cl] = this.put(script);
    const n = this.ex.bb_load(sp, sl, cp, cl, openTurn ? 1 : 0);
    this.ex.bb_free(sp, sl);
    this.ex.bb_free(cp, cl);
    const out = this.out();
    if (n < 0) throw new Error(out);
    return { steps: n, info: JSON.parse(out) };
  }

  /** @param {number} step */
  seek(step) {
    this.ex.bb_seek(step);
    return JSON.parse(this.out());
  }

  // Live games: see "Live games" below.

  /** Calls an export that takes one string, and throws its error. @param {string} fn @param {string} text */
  call(fn, text) {
    const [p, l] = this.put(text);
    const n = this.ex[fn](p, l);
    this.ex.bb_free(p, l);
    if (n < 0) throw new Error(this.out());
    return this.out();
  }

  /** The setup lines of a new game on a generated map. @param {string} config `key = value` lines, with the map's `seed` */
  setup(config) {
    if (!this.ex.bb_setup) throw new Error("web/bitbath_wasm.js is out of date. Rebuild it with: python scripts/build_web.py");
    return this.call("bb_setup", config).split("\n");
  }

  /** The engine, protocol and generator versions, for a replay's header. */
  versions() {
    this.ex.bb_versions();
    return JSON.parse(this.out());
  }

  /** Adds a step to the end of the loaded game and returns its reply lines. @param {string} line `T <player>` or `O <order>` */
  push(line) {
    const out = this.call("bb_push", line);
    return out ? out.split("\n") : [];
  }

  /** Ends the turn being played. Returns whether that ended the game. */
  end() {
    return this.ex.bb_end() === 1;
  }

  /** The init message for a player (SPEC §6.2). @param {number} player @returns {string[]} */
  initMessage(player) {
    if (this.ex.bb_init_message(player) < 0) throw new Error(this.out());
    return this.out().split("\n");
  }

  /**
   * The turn message for the player whose turn it is (SPEC §6.3).
   * @param {number} [timeMs] the time it says the turn has @param {number} [bankMs] and the bank it says is left
   * @returns {string[]}
   */
  turnMessage(timeMs, bankMs) {
    if (this.ex.bb_turn_message(timeMs || 0, bankMs || 0) < 0) throw new Error(this.out());
    return this.out().split("\n");
  }
}
