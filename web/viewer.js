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
    /** @type {any} */ prev: null,
    /** Positions at the start and after each turn. */ ends: [0],
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
    drawUnits(ctx, persp);
    drawOrder(ctx);
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

  /** @param {CanvasRenderingContext2D} ctx @param {number} persp */
  function drawUnits(ctx, persp) {
    const s = v.geo.size;
    /** @type {Map<string, any[]>} */
    const byHex = new Map();
    for (const u of v.state.units) {
      if (persp && u[1] !== persp && !visibleTo(u[3], u[4], persp)) continue;
      const key = u[3] + "," + u[4];
      if (!byHex.has(key)) byHex.set(key, []);
      /** @type {any[]} */ (byHex.get(key)).push(u);
    }
    for (const list of byHex.values()) {
      const [x, y] = center(list[0][3], list[0][4]);
      const n = list.length;
      const rad = n === 1 ? s * 0.5 : n <= 4 ? s * 0.33 : s * 0.26;
      list.forEach((u, j) => {
        const off = n <= 4 ? STACK[n][j] : [0.38 * Math.cos((2 * Math.PI * j) / n), 0.38 * Math.sin((2 * Math.PI * j) / n)];
        drawUnit(ctx, u, x + off[0] * s, y + off[1] * s, rad);
      });
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
  function arrow(ctx, pts) {
    if (pts.length < 2) return;
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.stroke();
    const [x1, y1] = pts[pts.length - 2];
    const [x2, y2] = pts[pts.length - 1];
    const a = Math.atan2(y2 - y1, x2 - x1);
    const k = v.geo.size * 0.4;
    ctx.beginPath();
    ctx.moveTo(x2, y2);
    ctx.lineTo(x2 - k * Math.cos(a - 0.45), y2 - k * Math.sin(a - 0.45));
    ctx.lineTo(x2 - k * Math.cos(a + 0.45), y2 - k * Math.sin(a + 0.45));
    ctx.closePath();
    ctx.fill();
  }

  /** Highlights the order just applied. @param {CanvasRenderingContext2D} ctx */
  function drawOrder(ctx) {
    if (v.pos === 0 || !v.prev) return;
    const line = v.replay.script[v.pos - 1];
    if (!line.startsWith("O ")) return;
    const t = line.slice(2).trim().split(/\s+/);
    const result = v.state.reply[v.state.reply.length - 1] || "";
    const res = result.split(/\s+/);
    const ok = !result.startsWith("ERR");
    const color = ok ? playerColor(v.state.player) : "#f03e3e";
    const s = v.geo.size;
    const before = new Map(v.prev.units.map((/** @type {any[]} */ u) => [u[0], u]));
    ctx.strokeStyle = ctx.fillStyle = color;
    ctx.lineWidth = Math.max(2, s * 0.16);
    ctx.lineCap = ctx.lineJoin = "round";
    if (!ok) ctx.setLineDash([5, 4]);
    const ring = (/** @type {number} */ q, /** @type {number} */ r) => {
      const [x, y] = center(q, r);
      ctx.beginPath();
      ctx.arc(x, y, s * 0.82, 0, 2 * Math.PI);
      ctx.stroke();
    };
    const unit = before.get(Number(t[1]));
    if (t[0] === "M" && unit) {
      let h = [unit[3], unit[4]];
      const pts = [center(h[0], h[1])];
      const end = res[0] === "OK" || res[0] === "PART" ? [Number(res[1]), Number(res[2])] : null;
      for (const ch of t[2] || "") {
        const d = DIRS[Number(ch)];
        if (!d || (end && h[0] === end[0] && h[1] === end[1])) break;
        h = [h[0] + d[0], h[1] + d[1]];
        pts.push(center(h[0], h[1]));
      }
      arrow(ctx, pts);
    } else if (t[0] === "A" && unit) {
      const target = before.get(Number(t[2]));
      if (target) {
        const a = center(unit[3], unit[4]);
        const b = center(target[3], target[4]);
        const shrink = s * 0.35;
        const len = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
        arrow(ctx, [a, [b[0] - ((b[0] - a[0]) * shrink) / len, b[1] - ((b[1] - a[1]) * shrink) / len]]);
        if (res[0] === "OK" && res[1] === "0") {
          ctx.beginPath();
          ctx.moveTo(b[0] - s * 0.4, b[1] - s * 0.4);
          ctx.lineTo(b[0] + s * 0.4, b[1] + s * 0.4);
          ctx.moveTo(b[0] + s * 0.4, b[1] - s * 0.4);
          ctx.lineTo(b[0] - s * 0.4, b[1] + s * 0.4);
          ctx.stroke();
        }
      } else {
        ring(unit[3], unit[4]);
      }
    } else if (t[0] === "C" && unit) {
      ring(unit[3], unit[4]);
    } else if (t[0] === "B") {
      const city = v.info.cities[Number(t[1])];
      if (city) ring(city[0], city[1]);
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
        const a = center(Number(t[2]), Number(t[3]));
        const b = center(Number(t[4]), Number(t[5]));
        ctx.strokeStyle = debugColor(t[6]);
        ctx.beginPath();
        ctx.moveTo(a[0], a[1]);
        ctx.lineTo(b[0], b[1]);
        ctx.stroke();
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
    for (const p of [1, 2]) {
      const units = st.units.filter((/** @type {any[]} */ u) => u[1] === p);
      const army = units.reduce((/** @type {number} */ sum, /** @type {any[]} */ u) => {
        const t = v.info.units[u[2]];
        return sum + (t ? Math.floor((t.cost * u[5]) / t.max_hp) : 0);
      }, 0);
      const row = document.createElement("tr");
      const name = el("td", playerName(p));
      const sw = el("span", "", "swatch");
      sw.style.background = playerColor(p);
      name.prepend(sw);
      if (st.player === p && turn) name.style.fontWeight = "600";
      row.append(
        name,
        el("td", String(st.cities.filter((/** @type {number[]} */ c) => c[0] === p).length), "num"),
        el("td", String(units.length), "num"),
        el("td", String(st.gold[p - 1]), "num"),
        el("td", String(army), "num"),
      );
      table.append(row);
    }

    const info = $("turn-info");
    const orders = $("orders");
    if (!turn) {
      info.textContent = "";
      orders.replaceChildren();
      v.listTurn = -1;
    } else {
      const flags = [];
      if (turn.skipped) flags.push("missed turn");
      if (turn.timeout) flags.push("timed out");
      if (turn.late) flags.push(turn.late.length + " late");
      info.textContent = "used " + turn.used_ms + " of " + turn.time_ms + " ms" + (flags.length ? " · " + flags.join(", ") : "");
      const first = v.replay.turnStart[ti] + 1;
      if (v.listTurn !== ti) {
        orders.replaceChildren();
        (turn.orders || []).forEach((/** @type {string} */ o, /** @type {number} */ j) => {
          const result = v.info.results[first + j] || "";
          const row = el("div", o + "  → " + result, result.startsWith("ERR") ? "err" : "");
          row.title = result;
          row.onclick = () => setPos(first + j + 1);
          orders.append(row);
        });
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

  // Navigation.

  /** @param {number} k */
  function setPos(k) {
    if (!v.engine || !v.replay) return;
    v.pos = Math.max(0, Math.min(v.steps, k));
    v.state = v.engine.seek(v.pos);
    v.prev = v.pos > 0 ? v.engine.seek(v.pos - 1) : null;
    updateSide();
    draw();
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
  }

  function tick() {
    const [mode, ms] = $("speed").value.split(":");
    v.timer = window.setTimeout(() => {
      if (!v.playing) return;
      if (v.pos >= v.steps) return stop();
      setPos(mode === "turn" ? nextTurn() : v.pos + 1);
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
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    v.dark = e.matches;
    redrawAll();
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
