// @ts-check
// Bitbath replay viewer (SPEC §8). It re-simulates a replay's orders with the
// engine compiled to wasm (wasm/src/lib.rs) and draws the game on a canvas.
/* global BITBATH_WASM_BASE64 */
"use strict";

(function () {
  const SQ3 = Math.sqrt(3);
  const DIRS = [[1, 0], [1, -1], [0, -1], [-1, 0], [-1, 1], [0, 1]];
  const DEBUG_COLORS = ["red", "green", "blue", "yellow", "orange", "purple", "white", "black"];
  const TERRAIN = {
    light: { ".": "#e9e4c8", f: "#a3c48b", h: "#d4b787", C: "#efe7d6", "^": "#97918b", "~": "#8db6de" },
    dark: { ".": "#3b3b30", f: "#30492c", h: "#5b4a31", C: "#4d473d", "^": "#5d5954", "~": "#2a4769" },
  };
  /** @param {string} id */
  const $ = (id) => /** @type {any} */ (document.getElementById(id));

  /** The engine, behind the C ABI in wasm/src/lib.rs. */
  class Engine {
    /** @param {any} exports */
    constructor(exports) {
      this.ex = exports;
      this.enc = new TextEncoder();
      this.dec = new TextDecoder();
    }

    static async create() {
      if (typeof BITBATH_WASM_BASE64 === "undefined") {
        throw new Error("web/bitbath_wasm.js is missing. Build it with: python scripts/build_web.py");
      }
      const bin = atob(BITBATH_WASM_BASE64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const { instance } = await WebAssembly.instantiate(bytes, {});
      return new Engine(instance.exports);
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
    load(setup, script) {
      const [sp, sl] = this.put(setup);
      const [cp, cl] = this.put(script);
      const n = this.ex.bb_load(sp, sl, cp, cl);
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
  }

  /** Splits a replay into records and builds the engine's step script. */
  function parseReplay(text) {
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

  // Viewer state. `pos` counts the steps applied: 0 is the start, and step
  // pos-1 is the one just shown.
  const v = {
    /** @type {Engine | null} */ engine: null,
    /** @type {any} */ replay: null,
    /** @type {any} */ info: null,
    steps: 0,
    pos: 0,
    /** @type {any} */ state: null,
    /** What the current turn's orders did so far: see turnMarks(). @type {any[]} */ marks: [],
    /** The state when turn `ti` started, for turnMarks(). @type {{ti: number, state: any}} */ base: { ti: -1, state: null },
    /** Units sliding along their paths during playback: see animateTo(). @type {any} */ anim: null,
    /** Positions at the start and after each turn. */ ends: [0],
    /** The index into `ends` of the turn shown. */ turnIdx: 0,
    /** The index into `ends` under the pointer on the army chart, or -1. */ chartHover: -1,
    playing: false,
    timer: 0,
    /** @type {[number, number] | null} */ hover: null,
    /** @type {any} */ geo: null,
    /** @type {HTMLCanvasElement | null} */ terrain: null,
    zoom: 1,
    pan: [0, 0],
    listTurn: -1,
    dark: window.matchMedia("(prefers-color-scheme: dark)").matches,
  };

  /** @param {string} name */
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /** @param {number} player */
  function playerColor(player) {
    return cssVar(player === 1 ? "--p1" : player === 2 ? "--p2" : "--neutral");
  }

  /** @param {number} player */
  function playerName(player) {
    const p = v.replay && v.replay.header.players[player - 1];
    return p ? p.name : "player " + player;
  }

  // Geometry: pointy-top hexes, odd rows shifted right (SPEC §3.1).

  function computeGeo() {
    const cv = $("canvas");
    const rect = cv.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(rect.width * dpr);
    cv.height = Math.round(rect.height * dpr);
    const W = v.info.width;
    const H = v.info.height;
    const pad = 12;
    const fit = Math.max(2, Math.min((rect.width - 2 * pad) / (SQ3 * (W + 0.5)), (rect.height - 2 * pad) / (1.5 * H + 0.5)));
    const size = fit * v.zoom;
    const ox = (rect.width - SQ3 * size * (W + 0.5)) / 2 + v.pan[0];
    const oy = (rect.height - size * (1.5 * H + 0.5)) / 2 + v.pan[1];
    v.geo = { size, ox, oy, w: rect.width, h: rect.height, dpr };
  }

  /** @param {number} q @param {number} r @returns {[number, number]} */
  function center(q, r) {
    const g = v.geo;
    const col = q + Math.floor(r / 2);
    return [g.ox + SQ3 * g.size * (col + 0.5 * (r & 1) + 0.5), g.oy + g.size * (1.5 * r + 1)];
  }

  /** @param {number} q @param {number} r */
  function onMap(q, r) {
    const col = q + Math.floor(r / 2);
    return r >= 0 && r < v.info.height && col >= 0 && col < v.info.width;
  }

  /** @param {number} x @param {number} y @returns {[number, number] | null} */
  function hexAt(x, y) {
    const [x0, y0] = center(0, 0);
    const px = (x - x0) / v.geo.size;
    const py = (y - y0) / v.geo.size;
    const qf = (SQ3 / 3) * px - py / 3;
    const rf = (2 / 3) * py;
    let q = Math.round(qf);
    let r = Math.round(rf);
    const s = Math.round(-qf - rf);
    const dq = Math.abs(q - qf);
    const dr = Math.abs(r - rf);
    const ds = Math.abs(s + qf + rf);
    if (dq > dr && dq > ds) q = -r - s;
    else if (dr > ds) r = -q - s;
    return onMap(q, r) ? [q, r] : null;
  }

  /** @param {CanvasRenderingContext2D} ctx @param {number} x @param {number} y @param {number} s */
  function hexPath(ctx, x, y, s) {
    ctx.beginPath();
    for (let i = 0; i < 6; i++) {
      const a = (Math.PI / 180) * (60 * i - 30);
      ctx.lineTo(x + s * Math.cos(a), y + s * Math.sin(a));
    }
    ctx.closePath();
  }

  /** @param {number} q @param {number} r @param {number} player */
  function visibleTo(q, r, player) {
    const col = q + Math.floor(r / 2);
    return (Number(v.state.vis[r * v.info.width + col]) & player) !== 0;
  }

  // Drawing.

  function buildTerrain() {
    const g = v.geo;
    const layer = document.createElement("canvas");
    layer.width = Math.round(g.w * g.dpr);
    layer.height = Math.round(g.h * g.dpr);
    const ctx = /** @type {CanvasRenderingContext2D} */ (layer.getContext("2d"));
    ctx.setTransform(g.dpr, 0, 0, g.dpr, 0, 0);
    const pal = v.dark ? TERRAIN.dark : TERRAIN.light;
    const s = g.size;
    for (let r = 0; r < v.info.height; r++) {
      for (let col = 0; col < v.info.width; col++) {
        const ch = v.info.rows[r][col];
        const [x, y] = center(col - Math.floor(r / 2), r);
        if (x < -s || y < -s || x > g.w + s || y > g.h + s) continue;
        hexPath(ctx, x, y, s * 0.97);
        ctx.fillStyle = pal[ch] || "#888";
        ctx.fill();
        ctx.strokeStyle = v.dark ? "rgba(0,0,0,.25)" : "rgba(0,0,0,.08)";
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.strokeStyle = ctx.fillStyle = v.dark ? "rgba(255,255,255,.18)" : "rgba(0,0,0,.18)";
        ctx.lineWidth = Math.max(1, s * 0.08);
        if (ch === "^") {
          ctx.beginPath();
          ctx.moveTo(x - s * 0.45, y + s * 0.3);
          ctx.lineTo(x - s * 0.1, y - s * 0.35);
          ctx.lineTo(x + s * 0.12, y);
          ctx.lineTo(x + s * 0.25, y - s * 0.18);
          ctx.lineTo(x + s * 0.45, y + s * 0.3);
          ctx.stroke();
        } else if (ch === "f") {
          for (const [dx, dy] of [[-0.25, 0.1], [0.2, -0.15], [0.15, 0.3]]) {
            ctx.beginPath();
            ctx.arc(x + dx * s, y + dy * s, s * 0.13, 0, 2 * Math.PI);
            ctx.fill();
          }
        } else if (ch === "h") {
          ctx.beginPath();
          ctx.arc(x, y + s * 0.35, s * 0.4, Math.PI * 1.15, Math.PI * 1.85);
          ctx.stroke();
        } else if (ch === "~") {
          ctx.beginPath();
          ctx.moveTo(x - s * 0.4, y);
          ctx.quadraticCurveTo(x - s * 0.2, y - s * 0.2, x, y);
          ctx.quadraticCurveTo(x + s * 0.2, y + s * 0.2, x + s * 0.4, y);
          ctx.stroke();
        }
      }
    }
    v.terrain = layer;
  }

  function draw() {
    const cv = $("canvas");
    const ctx = /** @type {CanvasRenderingContext2D} */ (cv.getContext("2d"));
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cv.width, cv.height);
    if (!v.state || !v.geo || !v.terrain) return;
    ctx.drawImage(v.terrain, 0, 0);
    ctx.setTransform(v.geo.dpr, 0, 0, v.geo.dpr, 0, 0);
    const persp = Number($("view").value);
    drawCities(ctx, persp);
    if (persp) drawFog(ctx, persp);
    drawDebug(ctx);
    drawTrails(ctx, persp);
    drawUnits(ctx, persp);
    drawOrder(ctx, persp);
    if (v.hover) {
      const [x, y] = center(v.hover[0], v.hover[1]);
      hexPath(ctx, x, y, v.geo.size * 0.97);
      ctx.strokeStyle = cssVar("--text");
      ctx.lineWidth = 2;
      ctx.stroke();
    }
  }

  /** @param {CanvasRenderingContext2D} ctx @param {number} persp */
  function drawCities(ctx, persp) {
    const s = v.geo.size;
    v.info.cities.forEach((/** @type {number[]} */ c, /** @type {number} */ i) => {
      const [q, r] = c;
      const [owner, capturer, progress] = v.state.cities[i];
      const [x, y] = center(q, r);
      const color = playerColor(owner);
      hexPath(ctx, x, y, s * 0.86);
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(2, s * 0.16);
      ctx.stroke();
      ctx.beginPath();
      const hx = x - s * 0.22;
      const hy = y - s * 0.58;
      ctx.moveTo(hx, hy + s * 0.18);
      ctx.lineTo(hx, hy);
      ctx.lineTo(hx + s * 0.22, hy - s * 0.16);
      ctx.lineTo(hx + s * 0.44, hy);
      ctx.lineTo(hx + s * 0.44, hy + s * 0.18);
      ctx.closePath();
      ctx.fillStyle = color;
      ctx.fill();
      if (capturer && (!persp || visibleTo(q, r, persp))) {
        const frac = Math.min(1, progress / v.info.capture_points);
        ctx.beginPath();
        ctx.arc(x, y, s * 0.68, -Math.PI / 2, -Math.PI / 2 + 2 * Math.PI * frac);
        ctx.strokeStyle = playerColor(capturer);
        ctx.lineWidth = Math.max(2, s * 0.14);
        ctx.stroke();
      }
    });
  }

  /** @param {CanvasRenderingContext2D} ctx @param {number} persp */
  function drawFog(ctx, persp) {
    ctx.fillStyle = v.dark ? "rgba(0,0,0,.5)" : "rgba(20,20,30,.32)";
    for (let r = 0; r < v.info.height; r++) {
      for (let col = 0; col < v.info.width; col++) {
        const q = col - Math.floor(r / 2);
        if (visibleTo(q, r, persp)) continue;
        const [x, y] = center(q, r);
        hexPath(ctx, x, y, v.geo.size * 1.01);
        ctx.fill();
      }
    }
  }

  const STACK = [
    [],
    [[0, 0]],
    [[-0.3, 0], [0.3, 0]],
    [[0, -0.3], [-0.3, 0.24], [0.3, 0.24]],
    [[-0.3, -0.28], [0.3, -0.28], [-0.3, 0.28], [0.3, 0.28]],
  ];

  /** The radius of each unit in a stack of `n`. @param {number} n */
  function stackRadius(n) {
    const s = v.geo.size;
    return n === 1 ? s * 0.5 : n <= 4 ? s * 0.33 : s * 0.26;
  }

  /** @param {CanvasRenderingContext2D} ctx @param {number} persp */
  function drawUnits(ctx, persp) {
    const s = v.geo.size;
    const a = v.anim;
    const hidden = (/** @type {any[]} */ u) => persp && u[1] !== persp && !visibleTo(u[3], u[4], persp);
    // While units slide during playback, the moving ones are drawn on their
    // own, and units killed on the way stay until the slide ends.
    const still = a ? v.state.units.filter((/** @type {any[]} */ u) => !a.paths.has(u[0])).concat(a.ghosts) : v.state.units;
    /** @type {Map<string, any[]>} */
    const byHex = new Map();
    for (const u of still) {
      if (hidden(u)) continue;
      const key = u[3] + "," + u[4];
      if (!byHex.has(key)) byHex.set(key, []);
      /** @type {any[]} */ (byHex.get(key)).push(u);
    }
    for (const list of byHex.values()) {
      const [x, y] = center(list[0][3], list[0][4]);
      const n = list.length;
      list.forEach((u, j) => {
        const off = n <= 4 ? STACK[n][j] : [0.38 * Math.cos((2 * Math.PI * j) / n), 0.38 * Math.sin((2 * Math.PI * j) / n)];
        drawUnit(ctx, u, x + off[0] * s, y + off[1] * s, stackRadius(n));
      });
    }
    if (!a) return;
    for (const u of v.state.units) {
      const path = a.paths.get(u[0]);
      if (!path || hidden(u)) continue;
      // Sized for the stack it ends in, so it doesn't change size on arrival.
      const n = v.state.units.filter((/** @type {any[]} */ w) => w[3] === u[3] && w[4] === u[4]).length;
      const [x, y] = pointAlong(path, Math.min(path.length - 1, a.p * a.len));
      drawUnit(ctx, u, x, y, stackRadius(n));
    }
  }

  /** @param {CanvasRenderingContext2D} ctx @param {any[]} u @param {number} x @param {number} y @param {number} rad */
  function drawUnit(ctx, u, x, y, rad) {
    const [, owner, code, , , hp, , flags] = u;
    const type = v.info.units[code];
    const maxHp = type ? type.max_hp : 100;
    ctx.globalAlpha = owner === v.state.player && flags & 1 ? 0.55 : 1;
    ctx.beginPath();
    ctx.arc(x, y, rad, 0, 2 * Math.PI);
    ctx.fillStyle = playerColor(owner);
    ctx.fill();
    ctx.lineWidth = Math.max(1, rad * 0.14);
    ctx.strokeStyle = v.dark ? "rgba(255,255,255,.7)" : "rgba(0,0,0,.55)";
    if (flags & 4) ctx.setLineDash([2, 2]);
    ctx.stroke();
    ctx.setLineDash([]);
    if (rad >= 5) {
      ctx.fillStyle = "#fff";
      ctx.font = "bold " + Math.round(rad * 1.1) + "px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(code, x, y + rad * 0.06);
    }
    const w = rad * 1.8;
    const h = Math.max(2, rad * 0.26);
    const frac = Math.max(0, Math.min(1, hp / maxHp));
    ctx.fillStyle = "rgba(0,0,0,.55)";
    ctx.fillRect(x - w / 2, y + rad * 0.95, w, h);
    ctx.fillStyle = frac > 0.6 ? "#3fb950" : frac > 0.3 ? "#e3b341" : "#f85149";
    ctx.fillRect(x - w / 2, y + rad * 0.95, w * frac, h);
    ctx.globalAlpha = 1;
  }

  /** @param {CanvasRenderingContext2D} ctx @param {[number, number][]} pts */
  function polyline(ctx, pts) {
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.stroke();
  }

  /** @param {CanvasRenderingContext2D} ctx @param {[number, number][]} pts @param {number} [k] the head's length */
  function arrow(ctx, pts, k = v.geo.size * 0.4) {
    if (pts.length < 2) return;
    polyline(ctx, pts);
    const [x1, y1] = pts[pts.length - 2];
    const [x2, y2] = pts[pts.length - 1];
    const a = Math.atan2(y2 - y1, x2 - x1);
    ctx.beginPath();
    ctx.moveTo(x2, y2);
    ctx.lineTo(x2 - k * Math.cos(a - 0.45), y2 - k * Math.sin(a - 0.45));
    ctx.lineTo(x2 - k * Math.cos(a + 0.45), y2 - k * Math.sin(a + 0.45));
    ctx.closePath();
    ctx.fill();
  }

  /** Draws an X where a unit died. @param {CanvasRenderingContext2D} ctx @param {[number, number]} p @param {number} k */
  function cross(ctx, p, k) {
    const [x, y] = p;
    ctx.beginPath();
    ctx.moveTo(x - k, y - k);
    ctx.lineTo(x + k, y + k);
    ctx.moveTo(x + k, y - k);
    ctx.lineTo(x - k, y + k);
    ctx.stroke();
  }

  /** @param {number[][]} hexes @returns {[number, number][]} */
  function centers(hexes) {
    return hexes.map((h) => center(h[0], h[1]));
  }

  /** The point `d` hex steps along a path. @param {number[][]} path @param {number} d @returns {[number, number]} */
  function pointAlong(path, d) {
    const i = Math.max(0, Math.min(path.length - 2, Math.floor(d)));
    const a = center(path[i][0], path[i][1]);
    if (path.length < 2) return a;
    const b = center(path[i + 1][0], path[i + 1][1]);
    const k = Math.max(0, Math.min(1, d - i));
    return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k];
  }

  /**
   * Works out what each order of the current turn so far did on the map,
   * following units from where they were when the turn started. A mark has
   * the order's step, its letter (`op`), whether it worked (`ok`), and its
   * `hexes`: the path walked for a move, attacker and target for an attack,
   * or the one hex of a capture or build.
   */
  function turnMarks() {
    /** @type {any[]} */
    const marks = [];
    if (v.pos === 0) return marks;
    const ti = v.replay.stepTurn[v.pos - 1];
    const first = v.replay.turnStart[ti];
    if (v.base.ti !== ti) v.base = { ti, state: v.engine.seek(first + 1) };
    /** @type {Map<number, number[]>} */
    const at = new Map();
    // Units built this turn can't move yet, so they are where they are now.
    for (const u of v.state.units) at.set(u[0], [u[3], u[4]]);
    for (const u of v.base.state.units) at.set(u[0], [u[3], u[4]]);
    for (let i = first + 1; i < v.pos; i++) {
      const t = v.replay.script[i].slice(2).trim().split(/\s+/);
      const result = v.info.results[i] || "";
      const res = result.split(/\s+/);
      const ok = !result.startsWith("ERR");
      const from = at.get(Number(t[1]));
      if (t[0] === "M" && from) {
        // A move that worked reports where it ended, which is short of the
        // path's end if it was stopped.
        const end = res[0] === "OK" || res[0] === "PART" ? [Number(res[1]), Number(res[2])] : null;
        let h = from;
        const hexes = [h];
        for (const ch of t[2] || "") {
          const d = DIRS[Number(ch)];
          if (!d || (end && h[0] === end[0] && h[1] === end[1])) break;
          h = [h[0] + d[0], h[1] + d[1]];
          hexes.push(h);
        }
        if (end) at.set(Number(t[1]), end);
        marks.push({ step: i, op: "M", id: Number(t[1]), ok, hexes });
      } else if (t[0] === "A" && from) {
        const target = Number(t[2]);
        const to = at.get(target);
        marks.push({ step: i, op: "A", id: Number(t[1]), target, ok, hexes: to ? [from, to] : [from], kill: res[0] === "OK" && res[1] === "0" });
      } else if (t[0] === "C" && from) {
        marks.push({ step: i, op: "C", ok, hexes: [from] });
      } else if (t[0] === "B") {
        const city = v.info.cities[Number(t[1])];
        if (city) marks.push({ step: i, op: "B", type: t[2], ok, hexes: [[city[0], city[1]]] });
      }
    }
    return marks;
  }

  /**
   * Whether a mark belongs in a player's view: the opponent's only where
   * that player sees all of it.
   * @param {any} m @param {number} persp
   */
  function shown(m, persp) {
    return !persp || persp === v.state.player || m.hexes.every((/** @type {number[]} */ h) => visibleTo(h[0], h[1], persp));
  }

  /**
   * Draws, under the units, what the current turn's orders did so far: the
   * path of each move, and a line for each attack with an X where a unit
   * died. While units slide during playback, their paths grow behind them.
   * @param {CanvasRenderingContext2D} ctx @param {number} persp
   */
  function drawTrails(ctx, persp) {
    const s = v.geo.size;
    const a = v.anim;
    ctx.save();
    ctx.globalAlpha = 0.7;
    ctx.strokeStyle = playerColor(v.state.player);
    ctx.lineWidth = Math.max(2, s * 0.13);
    ctx.lineCap = ctx.lineJoin = "round";
    for (const m of v.marks) {
      if (!m.ok || (a && m.step >= a.from) || !shown(m, persp)) continue;
      if (m.op === "M") {
        polyline(ctx, centers(m.hexes));
      } else if (m.op === "A" && m.hexes.length === 2) {
        const pts = centers(m.hexes);
        polyline(ctx, pts);
        if (m.kill) cross(ctx, pts[1], s * 0.3);
      }
    }
    if (a) {
      for (const path of a.paths.values()) {
        if (!shown({ hexes: path }, persp)) continue;
        const d = Math.min(path.length - 1, a.p * a.len);
        polyline(ctx, centers(path.slice(0, Math.floor(d) + 1)).concat([pointAlong(path, d)]));
      }
    }
    ctx.restore();
  }

  /** Highlights the order just applied. @param {CanvasRenderingContext2D} ctx @param {number} persp */
  function drawOrder(ctx, persp) {
    const m = v.marks[v.marks.length - 1];
    if (!m || m.step !== v.pos - 1 || v.anim || !shown(m, persp)) return;
    // Playing turn by turn, a turn's last order matters no more than the
    // others, so it shows as just its trail.
    if (v.playing && $("speed").value.startsWith("turn")) return;
    const s = v.geo.size;
    ctx.strokeStyle = ctx.fillStyle = m.ok ? playerColor(v.state.player) : "#f03e3e";
    ctx.lineWidth = Math.max(2, s * 0.16);
    ctx.lineCap = ctx.lineJoin = "round";
    if (!m.ok) ctx.setLineDash([5, 4]);
    const pts = centers(m.hexes);
    if (m.op === "M") {
      arrow(ctx, pts);
    } else if (m.op === "A" && pts.length === 2) {
      const [a, b] = pts;
      const shrink = s * 0.35;
      const len = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
      arrow(ctx, [a, [b[0] - ((b[0] - a[0]) * shrink) / len, b[1] - ((b[1] - a[1]) * shrink) / len]]);
      if (m.kill) cross(ctx, b, s * 0.4);
    } else {
      ctx.beginPath();
      ctx.arc(pts[0][0], pts[0][1], s * 0.82, 0, 2 * Math.PI);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }

  /** @param {string} c */
  function debugColor(c) {
    return DEBUG_COLORS.includes(c) || /^#[0-9a-fA-F]{6}$/.test(c) ? c : "gray";
  }

  /** Draws the D HEX and D LINE annotations of the current turn. @param {CanvasRenderingContext2D} ctx */
  function drawDebug(ctx) {
    if (!$("debug").checked || v.pos === 0) return;
    const turn = v.replay.turns[v.replay.stepTurn[v.pos - 1]];
    const s = v.geo.size;
    ctx.lineWidth = Math.max(1.5, s * 0.12);
    for (const line of turn.debug || []) {
      const t = line.split(/\s+/);
      if (t[1] === "HEX" && t.length >= 5) {
        const [x, y] = center(Number(t[2]), Number(t[3]));
        ctx.strokeStyle = ctx.fillStyle = debugColor(t[4]);
        hexPath(ctx, x, y, s * 0.75);
        ctx.stroke();
        const label = t.slice(5).join(" ");
        if (label && s >= 8) {
          ctx.font = Math.max(9, Math.round(s * 0.5)) + "px system-ui, sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "bottom";
          ctx.fillText(label, x, y - s * 0.8);
        }
      } else if (t[1] === "LINE" && t.length >= 7) {
        // Lines point from the first hex to the second, and are see-through
        // so a bot's many lines don't hide the map.
        ctx.strokeStyle = ctx.fillStyle = debugColor(t[6]);
        ctx.globalAlpha = 0.5;
        arrow(ctx, [center(Number(t[2]), Number(t[3])), center(Number(t[4]), Number(t[5]))], s * 0.3);
        ctx.globalAlpha = 1;
      }
    }
  }

  // The side panel.

  /** @param {string} tag @param {string} text @param {string} [cls] */
  function el(tag, text, cls) {
    const e = document.createElement(tag);
    e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }

  // Plain-language summaries for the side panel.

  /** @param {string} s */
  function capitalize(s) {
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  /** A unit name's plural: "infantry" and "cavalry" stay, "pikeman" becomes "pikemen". @param {string} noun */
  function plural(noun) {
    if (/(ry|s)$/.test(noun)) return noun;
    if (noun.endsWith("man")) return noun.slice(0, -3) + "men";
    return noun + "s";
  }

  /** @param {number} n @param {string} noun */
  function count(n, noun) {
    return n + " " + (n === 1 ? noun : plural(noun));
  }

  /** @param {number} n */
  function cityCount(n) {
    return n === 1 ? "1 city" : n + " cities";
  }

  /** Joins phrases as "a, b and c". @param {string[]} parts */
  function join(parts) {
    return parts.length < 2 ? parts.join("") : parts.slice(0, -1).join(", ") + " and " + parts[parts.length - 1];
  }

  /** Counts names into a phrase like "2 infantry and 1 scout", in first-seen order. @param {string[]} names */
  function tally(names) {
    /** @type {Map<string, number>} */
    const counts = new Map();
    for (const name of names) counts.set(name, (counts.get(name) || 0) + 1);
    return join(Array.from(counts, ([name, n]) => count(n, name)));
  }

  /** What a player's units are, in the rules' order of unit types. @param {any[][]} units */
  function composition(units) {
    const names = [];
    for (const code of Object.keys(v.info.units)) {
      for (const u of units) if (u[2] === code) names.push(v.info.units[code].name);
    }
    return names.length ? tally(names) : "no units";
  }

  /** A player's name, with the side added when both players share a name. @param {number} p */
  function who(p) {
    return playerName(p) + (playerName(1) === playerName(2) ? " (P" + p + ")" : "");
  }

  /** Says who is ahead in army value and in cities. @param {number[]} army @param {number[]} cities */
  function standing(army, cities) {
    const ahead = (/** @type {number[]} */ xs) => (xs[0] >= xs[1] ? 1 : 2);
    const a = ahead(army);
    const [hi, lo] = [army[a - 1], army[2 - a]];
    let s;
    if (!lo) s = hi ? who(a) + " has the only army." : "Neither side has an army.";
    else if (hi < lo * 1.1) s = "The armies are about even.";
    else s = who(a) + "'s army is " + (hi / lo < 10 ? (hi / lo).toFixed(1) : Math.round(hi / lo)) + "× " + who(3 - a) + "'s.";
    const c = ahead(cities);
    if (cities[0] === cities[1]) return s + " Each side holds " + cityCount(cities[0]) + ".";
    return s + " " + who(c) + " holds " + cityCount(cities[c - 1]) + " to " + cities[2 - c] + ".";
  }

  /**
   * Sums up the current turn so far in plain sentences, from the marks of
   * its orders and their replies.
   * @param {any} turn @param {number} ti @returns {string[]}
   */
  function turnSummary(turn, ti) {
    const first = v.replay.turnStart[ti];
    /** @type {Map<number, any[]>} */
    const units = new Map();
    for (const u of v.state.units) units.set(u[0], u);
    for (const u of v.base.state.units) units.set(u[0], u);
    const name = (/** @type {number} */ id) => {
      const u = units.get(id);
      return u && v.info.units[u[2]] ? v.info.units[u[2]].name : "unit";
    };
    // Hit points as the turn's fights go, to work out the damage done.
    const hp = new Map(Array.from(units.values(), (u) => [u[0], u[5]]));
    const moved = new Set();
    let stopped = 0;
    let attacks = 0;
    let dealt = 0;
    let taken = 0;
    let spent = 0;
    /** @type {string[]} */ const killed = [];
    /** @type {string[]} */ const lost = [];
    /** @type {string[]} */ const built = [];
    /** @type {number[]} */ const captured = [];
    /** @type {Map<number, string>} */ const capturing = new Map();
    for (const m of v.marks) {
      if (!m.ok) continue;
      const res = (v.info.results[m.step] || "").split(/\s+/);
      if (m.op === "M") {
        moved.add(m.id);
        if (res[0] === "PART") stopped++;
      } else if (m.op === "A" && m.hexes.length === 2) {
        const [dHp, aHp] = [Number(res[1]), Number(res[2])];
        attacks++;
        dealt += (hp.get(m.target) || 0) - dHp;
        taken += (hp.get(m.id) || 0) - aHp;
        hp.set(m.target, dHp);
        hp.set(m.id, aHp);
        if (dHp === 0) killed.push(name(m.target));
        if (aHp === 0) lost.push(name(m.id));
      } else if (m.op === "C") {
        const [q, r] = m.hexes[0];
        const city = v.info.cities.findIndex((/** @type {number[]} */ c) => c[0] === q && c[1] === r);
        capturing.delete(city);
        if (res[1] === "captured") captured.push(city);
        else capturing.set(city, city + " (" + res[1] + "/" + res[2] + ")");
      } else if (m.op === "B") {
        const t = v.info.units[m.type];
        built.push(t ? t.name : m.type);
        spent += t ? t.cost : 0;
      }
    }
    let orders = 0;
    let rejected = 0;
    for (let i = first + 1; i < v.pos; i++) {
      orders++;
      if ((v.info.results[i] || "").startsWith("ERR")) rejected++;
    }

    const lines = [];
    if (turn.skipped) lines.push("Missed this turn.");
    if (!orders) lines.push((turn.orders || []).length ? "No orders yet." : "No orders.");
    if (moved.size) {
      const short = stopped ? "; " + stopped + (stopped === 1 ? " was" : " were") + " stopped early" : "";
      lines.push("Moved " + count(moved.size, "unit") + short + ".");
    }
    if (attacks) {
      lines.push("Attacked " + (attacks === 1 ? "once" : attacks + " times") + ", dealing " + dealt + " damage and taking " + (taken || "none") + ".");
      const parts = [];
      if (killed.length) parts.push("destroyed " + tally(killed));
      if (lost.length) parts.push("lost " + tally(lost));
      if (parts.length) lines.push(capitalize(parts.join("; ")) + ".");
    }
    if (captured.length) lines.push("Captured " + (captured.length === 1 ? "city " : "cities ") + join(captured.map(String)) + ".");
    if (capturing.size) lines.push("Capturing " + (capturing.size === 1 ? "city " : "cities ") + join(Array.from(capturing.values())) + ".");
    if (built.length) lines.push("Built " + tally(built) + " for " + spent + " gold.");
    if (rejected) lines.push(count(rejected, "order") + (rejected === 1 ? " was" : " were") + " rejected.");
    if (turn.late && turn.late.length) lines.push(count(turn.late.length, "order") + " came after the turn ended.");
    if (turn.timeout) lines.push("Ran out of time.");
    lines.push("Used " + turn.used_ms.toLocaleString() + " ms of " + turn.time_ms.toLocaleString() + ".");
    return lines;
  }

  function updateSide() {
    const st = v.state;
    const ti = v.pos > 0 ? v.replay.stepTurn[v.pos - 1] : -1;
    const turn = ti >= 0 ? v.replay.turns[ti] : null;
    let title = "Start";
    if (turn) title = "Round " + st.round + " · " + playerName(st.player) + " (P" + st.player + ")";
    if (v.pos === v.steps && v.replay.result) {
      const res = v.replay.result;
      title = res.winner ? playerName(res.winner) + " wins by " + res.reason : "Draw (" + res.reason + ")";
    }
    $("turn-title").textContent = title;

    const table = $("players");
    table.replaceChildren();
    const head = document.createElement("tr");
    for (const label of ["", "cities", "units", "gold", "army"]) head.append(el("th", label));
    table.append(head);
    const army = [0, 0];
    const cities = [0, 0];
    for (const p of [1, 2]) {
      const units = st.units.filter((/** @type {any[]} */ u) => u[1] === p);
      army[p - 1] = units.reduce((/** @type {number} */ sum, /** @type {any[]} */ u) => {
        const t = v.info.units[u[2]];
        return sum + (t ? Math.floor((t.cost * u[5]) / t.max_hp) : 0);
      }, 0);
      cities[p - 1] = st.cities.filter((/** @type {number[]} */ c) => c[0] === p).length;
      const row = document.createElement("tr");
      const name = el("td", playerName(p));
      const sw = el("span", "", "swatch");
      sw.style.background = playerColor(p);
      name.prepend(sw);
      if (st.player === p && turn) name.style.fontWeight = "600";
      row.append(
        name,
        el("td", String(cities[p - 1]), "num"),
        el("td", String(units.length), "num"),
        el("td", String(st.gold[p - 1]), "num"),
        el("td", String(army[p - 1]), "num"),
      );
      // What the army is made of, under its numbers.
      const mix = el("td", composition(units), "mix");
      mix.colSpan = 5;
      const mixRow = document.createElement("tr");
      mixRow.append(mix);
      table.append(row, mixRow);
    }
    $("standing").textContent = standing(army, cities);

    const summary = $("summary");
    const orders = $("orders");
    $("orders-box").hidden = !turn || !(turn.orders || []).length;
    if (!turn) {
      summary.replaceChildren(el("div", "The game hasn't started.", "muted"));
      orders.replaceChildren();
      v.listTurn = -1;
    } else {
      summary.replaceChildren(...turnSummary(turn, ti).map((s) => el("div", s)));
      const first = v.replay.turnStart[ti] + 1;
      if (v.listTurn !== ti) {
        const list = turn.orders || [];
        let rejected = 0;
        orders.replaceChildren();
        list.forEach((/** @type {string} */ o, /** @type {number} */ j) => {
          const result = v.info.results[first + j] || "";
          if (result.startsWith("ERR")) rejected++;
          const row = el("div", o + "  → " + result, result.startsWith("ERR") ? "err" : "");
          row.title = result;
          row.onclick = () => setPos(first + j + 1);
          orders.append(row);
        });
        $("orders-count").textContent = "All " + count(list.length, "order") + (rejected ? ", " + rejected + " rejected" : "");
        v.listTurn = ti;
      }
      const cur = v.pos - 1 - first;
      Array.from(orders.children).forEach((/** @type {HTMLElement} */ row, /** @type {number} */ j) => {
        row.classList.toggle("current", j === cur);
        if (j === cur) row.scrollIntoView({ block: "nearest" });
      });
    }
    const line = v.pos > 0 ? v.replay.script[v.pos - 1] : "";
    $("reply").textContent = line.startsWith("O ") ? line.slice(2) + "\n→ " + st.reply.join("\n→ ") : "";

    const notes = $("notes");
    notes.replaceChildren();
    if (turn && $("debug").checked) {
      const texts = (turn.debug || []).filter((/** @type {string} */ d) => d.startsWith("D TEXT ")).map((/** @type {string} */ d) => d.slice(7));
      if (texts.length) {
        notes.append(el("h2", "Debug text"), el("pre", texts.join("\n"), "mono"));
      }
    }
    if (turn && turn.stderr) {
      const d = document.createElement("details");
      d.append(el("summary", "stderr (" + turn.stderr.length + " bytes)"), el("pre", turn.stderr, "mono"));
      notes.append(d);
    }
    if (turn && turn.note) notes.append(el("pre", turn.note, "mono"));
    if (v.pos === v.steps && v.replay.result) {
      const stats = v.replay.result.stats || [];
      notes.append(el("h2", "Result"));
      for (const p of [1, 2]) {
        const s = stats[p - 1];
        if (s) {
          const avg = Math.round(s.think_ms / Math.max(1, s.turns));
          notes.append(el("div", playerName(p) + ": " + s.orders + " orders, " + s.invalid + " rejected, " + s.late + " late, " + s.timeouts + " timeouts, " + avg + " ms per turn", "mono"));
        }
      }
    }

    const ends = v.ends;
    let turnIdx = 0;
    while (turnIdx + 1 < ends.length && ends[turnIdx + 1] <= v.pos) turnIdx++;
    v.turnIdx = turnIdx;
    $("slider").value = String(turnIdx);
    $("position").textContent = "turn " + (ti + 1) + "/" + v.replay.turns.length + " · step " + v.pos + "/" + v.steps;
    updateHover();
  }

  function updateHover() {
    const box = $("hover");
    if (!v.hover || !v.state) {
      box.textContent = "";
      return;
    }
    const [q, r] = v.hover;
    const ch = v.info.rows[r][q + Math.floor(r / 2)];
    const t = v.info.terrain[ch];
    const lines = ["(" + q + "," + r + ") " + (t ? t.name + ", defense " + t.defense + "%, move " + (t.move_cost || "-") : ch)];
    v.info.cities.forEach((/** @type {number[]} */ c, /** @type {number} */ i) => {
      if (c[0] === q && c[1] === r) {
        const [owner, capturer, progress] = v.state.cities[i];
        let text = "city " + i + ", " + (owner ? "owned by " + playerName(owner) : "neutral");
        if (capturer) text += ", " + playerName(capturer) + " capturing " + progress + "/" + v.info.capture_points;
        lines.push(text);
      }
    });
    for (const u of v.state.units) {
      if (u[3] === q && u[4] === r) {
        const type = v.info.units[u[2]];
        lines.push("unit " + u[0] + " " + playerName(u[1]) + " " + (type ? type.name : u[2]) + " " + u[5] + " hp, " + u[6] + " mp" + (u[7] & 1 ? ", acted" : "") + (u[7] & 4 ? ", new" : ""));
      }
    }
    box.textContent = lines.join("\n");
  }

  // The army chart over the slider: each player's army value at the start
  // and after each turn, lined up with the slider's positions.

  /** How far the slider's thumb travel is inset from its ends, in pixels. */
  const CHART_INSET = 8;

  /** The x of turn index `k` on a chart `w` pixels wide. @param {number} k @param {number} w */
  function chartX(k, w) {
    return CHART_INSET + ((w - 2 * CHART_INSET) * k) / Math.max(1, v.ends.length - 1);
  }

  /** The turn index nearest to an x on the chart. @param {number} x */
  function chartTurn(x) {
    const w = $("chart").getBoundingClientRect().width;
    const k = Math.round(((x - CHART_INSET) / Math.max(1, w - 2 * CHART_INSET)) * (v.ends.length - 1));
    return Math.max(0, Math.min(v.ends.length - 1, k));
  }

  function buildLegend() {
    const legend = $("army-legend");
    legend.replaceChildren(el("span", "Army value", "muted"));
    for (const p of [1, 2]) {
      const item = el("span", playerName(p));
      item.prepend(el("span", "", "key p" + p));
      legend.append(item);
    }
  }

  function drawChart() {
    const cv = $("chart");
    const box = cv.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(box.width * dpr);
    cv.height = Math.round(box.height * dpr);
    const army = v.info && v.info.army;
    if (!army || !v.replay) return;
    const ctx = /** @type {CanvasRenderingContext2D} */ (cv.getContext("2d"));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const n = Math.min(army.length, v.ends.length);
    let max = 1;
    for (let k = 0; k < n; k++) max = Math.max(max, army[k][0], army[k][1]);
    const w = box.width;
    const base = box.height - 4;
    const y = (/** @type {number} */ value) => base - ((base - 6) * value) / max;
    ctx.lineWidth = 1;
    ctx.strokeStyle = cssVar("--border");
    ctx.beginPath();
    ctx.moveTo(chartX(0, w), base + 0.5);
    ctx.lineTo(chartX(n - 1, w), base + 0.5);
    ctx.stroke();
    if (v.chartHover >= 0) {
      const x = Math.round(chartX(v.chartHover, w)) + 0.5;
      ctx.strokeStyle = cssVar("--muted");
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, base);
      ctx.stroke();
    }
    ctx.lineWidth = 2;
    ctx.lineCap = ctx.lineJoin = "round";
    for (const p of [1, 2]) {
      ctx.strokeStyle = playerColor(p);
      ctx.beginPath();
      for (let k = 0; k < n; k++) {
        if (k) ctx.lineTo(chartX(k, w), y(army[k][p - 1]));
        else ctx.moveTo(chartX(k, w), y(army[k][p - 1]));
      }
      ctx.stroke();
    }
    // The turn shown: a dot on each line, ringed in the surface color.
    const k = Math.min(v.turnIdx, n - 1);
    for (const p of [1, 2]) {
      ctx.beginPath();
      ctx.arc(chartX(k, w), y(army[k][p - 1]), 4, 0, 2 * Math.PI);
      ctx.fillStyle = playerColor(p);
      ctx.fill();
      ctx.strokeStyle = cssVar("--panel");
      ctx.stroke();
    }
  }

  /** Shows both armies' values at the hovered turn. */
  function updateChartTip() {
    const tip = $("chart-tip");
    const army = v.info && v.info.army;
    const k = v.chartHover;
    if (!army || k < 0 || k >= army.length) {
      tip.style.display = "none";
      return;
    }
    const turn = k > 0 ? v.replay.turns[k - 1] : null;
    tip.replaceChildren(el("div", turn ? "turn " + k + " · round " + turn.round : "start", "muted"));
    for (const p of [1, 2]) {
      const row = el("div", " " + playerName(p));
      row.prepend(el("span", "", "key p" + p), el("b", army[k][p - 1].toLocaleString()));
      tip.append(row);
    }
    tip.style.display = "block";
    const w = $("chart").getBoundingClientRect().width;
    const half = tip.offsetWidth / 2;
    tip.style.left = Math.max(half, Math.min(w - half, chartX(k, w))) + "px";
  }

  // Navigation.

  /** @param {number} k */
  function setPos(k) {
    if (!v.engine || !v.replay) return;
    v.anim = null;
    v.pos = Math.max(0, Math.min(v.steps, k));
    v.state = v.engine.seek(v.pos);
    v.marks = turnMarks();
    updateSide();
    draw();
    drawChart();
  }

  function nextTurn() {
    const e = v.ends.find((x) => x > v.pos);
    return e === undefined ? v.steps : e;
  }

  function prevTurn() {
    let best = 0;
    for (const e of v.ends) if (e < v.pos) best = e;
    return best;
  }

  function stop() {
    v.playing = false;
    clearTimeout(v.timer);
    $("play").textContent = "▶";
    draw();
  }

  /**
   * Moves to position `k`, with the units that move on the way sliding along
   * their paths for `ms` milliseconds. Units killed on the way stay until the
   * slide ends.
   * @param {number} k @param {number} ms
   */
  function animateTo(k, ms) {
    const from = v.pos;
    const old = v.state;
    setPos(k);
    /** @type {Map<number, number[][]>} */
    const paths = new Map();
    for (const m of v.marks) {
      if (m.step < from || m.op !== "M" || !m.ok) continue;
      const path = paths.get(m.id);
      paths.set(m.id, path ? path.concat(m.hexes.slice(1)) : m.hexes);
    }
    const alive = new Set(v.state.units.map((/** @type {any[]} */ u) => u[0]));
    const ghosts = old ? old.units.filter((/** @type {any[]} */ u) => !alive.has(u[0])) : [];
    if (ms < 40 || (!paths.size && !ghosts.length)) return;
    // Every unit moves at the same speed, so the longest path sets the time.
    let len = 1;
    for (const path of paths.values()) len = Math.max(len, path.length - 1);
    const anim = { from, paths, ghosts, len, p: 0 };
    const t0 = performance.now();
    v.anim = anim;
    const frame = (/** @type {number} */ now) => {
      if (v.anim !== anim) return;
      const t = Math.max(0, Math.min(1, (now - t0) / ms));
      anim.p = t < 0.5 ? 2 * t * t : 1 - 2 * (1 - t) * (1 - t);
      if (t >= 1) v.anim = null;
      draw();
      if (v.anim) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  function tick() {
    const [mode, ms] = $("speed").value.split(":");
    v.timer = window.setTimeout(() => {
      if (!v.playing) return;
      if (v.pos >= v.steps) return stop();
      animateTo(mode === "turn" ? nextTurn() : v.pos + 1, Number(ms) * 0.6);
      tick();
    }, Number(ms));
  }

  function togglePlay() {
    if (!v.replay) return;
    if (v.playing) return stop();
    if (v.pos >= v.steps) setPos(0);
    v.playing = true;
    $("play").textContent = "⏸";
    tick();
  }

  function redrawAll() {
    if (!v.info) return;
    computeGeo();
    buildTerrain();
    draw();
  }

  /** @param {string} text */
  async function openText(text) {
    stop();
    try {
      const replay = parseReplay(text);
      if (!v.engine) v.engine = await Engine.create();
      const { steps, info } = v.engine.load(replay.header.setup.join("\n"), replay.script.join("\n"));
      v.replay = replay;
      v.info = info;
      v.steps = steps;
      v.ends = [0].concat(replay.turnStart.slice(1), replay.turns.length ? [steps] : []);
      v.zoom = 1;
      v.pan = [0, 0];
      v.hover = null;
      v.listTurn = -1;
      v.marks = [];
      v.base = { ti: -1, state: null };
      v.chartHover = -1;
      // The army chart needs the timeline that a current bitbath_wasm.js adds.
      $("chart-box").hidden = $("army-legend").hidden = !info.army;
      if (info.army) buildLegend();
      updateChartTip();
      $("slider").max = String(v.ends.length - 1);
      $("empty").style.display = "none";
      const h = replay.header;
      $("matchup").textContent = h.players[0].name + " (P1) vs " + h.players[1].name + " (P2) · " + h.map + " · " + info.width + "×" + info.height;
      document.title = h.players[0].name + " vs " + h.players[1].name + " · Bitbath";
      computeGeo();
      buildTerrain();
      setPos(0);
    } catch (e) {
      $("empty").style.display = "";
      $("error").textContent = String(e instanceof Error ? e.message : e);
    }
  }

  /** @param {File} file */
  function openFile(file) {
    file.text().then(openText);
  }

  // Events.

  $("file").addEventListener("change", (/** @type {any} */ e) => e.target.files[0] && openFile(e.target.files[0]));
  $("first").onclick = () => setPos(0);
  $("prev-turn").onclick = () => setPos(prevTurn());
  $("prev").onclick = () => setPos(v.pos - 1);
  $("play").onclick = togglePlay;
  $("next").onclick = () => setPos(v.pos + 1);
  $("next-turn").onclick = () => setPos(nextTurn());
  $("last").onclick = () => setPos(v.steps);
  $("slider").oninput = () => setPos(v.ends[Number($("slider").value)] || 0);
  $("view").onchange = () => draw();
  $("debug").onchange = () => {
    updateSide();
    draw();
  };

  document.addEventListener("keydown", (e) => {
    const tag = /** @type {HTMLElement} */ (e.target).tagName;
    if (!v.replay || tag === "INPUT" || tag === "SELECT") return;
    const keys = {
      " ": togglePlay,
      ArrowRight: () => setPos(e.shiftKey ? nextTurn() : v.pos + 1),
      ArrowLeft: () => setPos(e.shiftKey ? prevTurn() : v.pos - 1),
      ArrowDown: () => setPos(nextTurn()),
      ArrowUp: () => setPos(prevTurn()),
      Home: () => setPos(0),
      End: () => setPos(v.steps),
    };
    const fn = /** @type {any} */ (keys)[e.key];
    if (fn) {
      e.preventDefault();
      fn();
    }
  });

  const board = $("board");
  const canvas = $("canvas");
  /** @type {null | {x: number, y: number, pan: number[], moved: boolean}} */
  let drag = null;
  canvas.addEventListener("mousedown", (/** @type {MouseEvent} */ e) => {
    drag = { x: e.clientX, y: e.clientY, pan: v.pan.slice(), moved: false };
  });
  window.addEventListener("mouseup", () => (drag = null));
  canvas.addEventListener("mousemove", (/** @type {MouseEvent} */ e) => {
    if (!v.info) return;
    if (drag && (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3 || drag.moved)) {
      drag.moved = true;
      v.pan = [drag.pan[0] + e.clientX - drag.x, drag.pan[1] + e.clientY - drag.y];
      redrawAll();
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const h = hexAt(e.clientX - rect.left, e.clientY - rect.top);
    if (String(h) !== String(v.hover)) {
      v.hover = h;
      updateHover();
      draw();
    }
  });
  canvas.addEventListener("mouseleave", () => {
    v.hover = null;
    updateHover();
    draw();
  });
  canvas.addEventListener(
    "wheel",
    (/** @type {WheelEvent} */ e) => {
      if (!v.info) return;
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left - rect.width / 2;
      const my = e.clientY - rect.top - rect.height / 2;
      const old = v.zoom;
      v.zoom = Math.max(1, Math.min(8, v.zoom * Math.exp(-e.deltaY * 0.0015)));
      const k = v.zoom / old;
      v.pan = [mx - (mx - v.pan[0]) * k, my - (my - v.pan[1]) * k];
      if (v.zoom === 1) v.pan = [0, 0];
      redrawAll();
    },
    { passive: false },
  );
  canvas.addEventListener("dblclick", () => {
    v.zoom = 1;
    v.pan = [0, 0];
    redrawAll();
  });

  // Clicking or dragging on the army chart scrubs like the slider.
  const chart = $("chart");
  chart.addEventListener("pointerdown", (/** @type {PointerEvent} */ e) => {
    if (!v.replay) return;
    chart.setPointerCapture(e.pointerId);
    setPos(v.ends[chartTurn(e.offsetX)]);
  });
  chart.addEventListener("pointermove", (/** @type {PointerEvent} */ e) => {
    if (!v.replay) return;
    const k = chartTurn(e.offsetX);
    if (e.buttons & 1 && v.ends[k] !== v.pos) setPos(v.ends[k]);
    if (k !== v.chartHover) {
      v.chartHover = k;
      updateChartTip();
      drawChart();
    }
  });
  chart.addEventListener("pointerleave", () => {
    v.chartHover = -1;
    updateChartTip();
    drawChart();
  });

  board.addEventListener("dragover", (/** @type {DragEvent} */ e) => {
    e.preventDefault();
    board.classList.add("dragging");
  });
  board.addEventListener("dragleave", () => board.classList.remove("dragging"));
  board.addEventListener("drop", (/** @type {DragEvent} */ e) => {
    e.preventDefault();
    board.classList.remove("dragging");
    const f = e.dataTransfer && e.dataTransfer.files[0];
    if (f) openFile(f);
  });

  new ResizeObserver(redrawAll).observe(board);
  new ResizeObserver(drawChart).observe($("chart-box"));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    v.dark = e.matches;
    redrawAll();
    drawChart();
    if (v.state) updateSide();
  });

  const url = new URLSearchParams(location.search).get("replay");
  if (url) {
    fetch(url)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(url + ": HTTP " + r.status))))
      .then(openText)
      .catch((e) => ($("error").textContent = String(e.message || e)));
  }

  // For tests and embedding: window.bitbathViewer.open(text).
  /** @type {any} */ (window).bitbathViewer = { open: openText, state: v, setPos };
})();
