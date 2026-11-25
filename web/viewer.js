// @ts-check
// Bitbath replay viewer (SPEC §8). It re-simulates a replay's orders with the
// engine compiled to wasm (wasm/src/lib.rs) and draws the game on a canvas.
// It also shows games as they are played in the page: play.js adds each step
// to a replay that grows, and takes the human player's orders.
/* global BitbathEngine, BitbathPieces */
"use strict";

(function () {
  const SQ3 = Math.sqrt(3);
  const DIRS = [[1, 0], [1, -1], [0, -1], [-1, 0], [-1, 1], [0, 1]];
  const DEBUG_COLORS = ["red", "green", "blue", "yellow", "orange", "purple", "white", "black"];
  const TERRAIN = {
    light: { ".": "#e9e4c8", f: "#a3c48b", h: "#d4b787", C: "#efe7d6", "^": "#97918b", "~": "#8db6de", R: "#ecd98b" },
    dark: { ".": "#3b3b30", f: "#30492c", h: "#5b4a31", C: "#4d473d", "^": "#5d5954", "~": "#2a4769", R: "#5e5324" },
  };
  /** @param {string} id */
  const $ = (id) => /** @type {any} */ (document.getElementById(id));

  // Viewer state. `pos` counts the steps applied: 0 is the start, and step
  // pos-1 is the one just shown.
  const v = {
    /** @type {BitbathEngine | null} */ engine: null,
    /** @type {any} */ replay: null,
    /** The game being played in the page and its controls (play.js), or null for a replay. @type {any} */ play: null,
    /** Where each unit was last drawn, as [x, y, radius] by unit id. @type {Map<number, number[]>} */ unitPos: new Map(),
    /** @type {any} */ info: null,
    steps: 0,
    pos: 0,
    /** @type {any} */ state: null,
    /** What the current turn's orders did so far: see turnMarks(). @type {any[]} */ marks: [],
    /** The state when turn `ti` started, for turnMarks(). @type {{ti: number, state: any}} */ base: { ti: -1, state: null },
    /** The ids of the units built in the current turn so far. @type {Set<number>} */ built: new Set(),
    /** Units sliding along their paths during playback: see animateTo(). @type {any} */ anim: null,
    /** Damage numbers floating over units just hit, and an arrow (with `to`) for each attack: see floatDamage(). @type {{hex: number[], text: string, t0: number, to?: number[], color?: string}[]} */ floats: [],
    /** Whether the damage numbers' frames are running. */ floating: false,
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

  /**
   * The player whose fog of war can't be lifted: the human in a game that is
   * still being played. It is 0 for replays and finished games.
   */
  function fogged() {
    return v.play && !v.play.over ? v.play.human : 0;
  }

  /** The player whose view is drawn, or 0 for both. */
  function viewOf() {
    return fogged() || Number($("view").value);
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
        } else if (ch === "R") {
          // A resource site: an ear of wheat. Its grains sit in pairs up the
          // stalk, leaning outward, with one at the tip and awns above them.
          // It is drawn in a wheat color, warmer than the other terrain marks.
          ctx.strokeStyle = ctx.fillStyle = v.dark ? "rgba(255,221,128,.6)" : "rgba(112,74,0,.55)";
          ctx.lineCap = "round";
          ctx.lineWidth = Math.max(1, s * 0.06);
          ctx.beginPath();
          ctx.moveTo(x, y + s * 0.46);
          ctx.lineTo(x, y - s * 0.3);
          ctx.stroke();
          const grain = (/** @type {number} */ gx, /** @type {number} */ gy, /** @type {number} */ turn) => {
            ctx.beginPath();
            ctx.ellipse(x + gx * s, y + gy * s, s * 0.072, s * 0.135, turn, 0, 2 * Math.PI);
            ctx.fill();
          };
          for (const gy of [0.12, -0.05, -0.22]) {
            grain(-0.115, gy, -0.5);
            grain(0.115, gy, 0.5);
          }
          grain(0, -0.4, 0);
          ctx.lineWidth = Math.max(0.75, s * 0.03);
          ctx.beginPath();
          for (const [ax, ay, bx, by] of [[0, -0.5, 0, -0.68], [-0.17, -0.31, -0.27, -0.5], [0.17, -0.31, 0.27, -0.5]]) {
            ctx.moveTo(x + ax * s, y + ay * s);
            ctx.lineTo(x + bx * s, y + by * s);
          }
          ctx.stroke();
          ctx.lineCap = "butt";
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
    if (!v.state || !v.geo || !v.terrain || !v.terrain.width || !v.terrain.height) return;
    ctx.drawImage(v.terrain, 0, 0);
    ctx.setTransform(v.geo.dpr, 0, 0, v.geo.dpr, 0, 0);
    const persp = viewOf();
    drawCities(ctx, persp);
    if (persp) drawFog(ctx, persp);
    drawDebug(ctx);
    drawTrails(ctx, persp);
    if (v.play) v.play.drawUnder(ctx);
    // A move's line goes under the units, so it never covers their letters.
    drawOrder(ctx, persp, true);
    drawUnits(ctx, persp);
    drawOrder(ctx, persp, false);
    drawStockpiles(ctx, persp);
    if (v.play) v.play.drawOver(ctx);
    drawFloats(ctx);
    if (v.hover) {
      const [x, y] = center(v.hover[0], v.hover[1]);
      hexPath(ctx, x, y, v.geo.size * 0.97);
      ctx.strokeStyle = cssVar("--text");
      ctx.lineWidth = 2;
      // Dotted when the picked unit can't get to the hex this turn.
      if (v.play && v.play.outOfReach(v.hover[0], v.hover[1])) {
        ctx.setLineDash([0.1, 6.5]);
        ctx.lineCap = "round";
        ctx.lineWidth = 3;
      }
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.lineCap = "butt";
    }
    // The forecast of a fight is placed while drawing, and the hover card goes by it.
    placeHover();
  }

  /** How long a damage number floats over a unit that was hit, in ms. */
  const FLOAT_MS = 1000;

  /**
   * Floats the damage of the attacks from step `from` on over the units they
   * hit. A turn's attacks follow each other, so numbers on one hex don't pile up.
   * @param {number} from @param {number} persp @param {number} delay how long to wait before the first, in ms
   */
  function floatDamage(from, persp, delay) {
    const sees = (/** @type {number[]} */ h) => !persp || visibleTo(h[0], h[1], persp);
    let t0 = performance.now() + delay;
    for (const m of v.marks) {
      if (m.step < from || m.op !== "A" || !m.ok || m.hexes.length !== 2) continue;
      const before = v.floats.length;
      if (m.dealt && sees(m.hexes[1])) v.floats.push({ hex: m.hexes[1], text: "−" + m.dealt, t0 });
      if (m.taken && sees(m.hexes[0])) v.floats.push({ hex: m.hexes[0], text: "−" + m.taken, t0 });
      if (v.floats.length > before) {
        // With the numbers, an arrow from the attacker to its target.
        v.floats.push({ hex: m.hexes[0], to: m.hexes[1], color: playerColor(v.state.player), text: "", t0 });
        t0 += 140;
      }
    }
    if (!v.floats.length || v.floating) return;
    v.floating = true;
    const frame = (/** @type {number} */ now) => {
      v.floats = v.floats.filter((/** @type {any} */ f) => now - f.t0 < FLOAT_MS);
      v.floating = v.floats.length > 0;
      draw();
      if (v.floating) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  /** Draws the damage numbers rising from the units just hit. @param {CanvasRenderingContext2D} ctx */
  function drawFloats(ctx) {
    if (!v.floats.length) return;
    const s = v.geo.size;
    const now = performance.now();
    ctx.save();
    ctx.font = "700 " + Math.max(13, s * 0.62) + "px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    ctx.lineWidth = Math.max(3, s * 0.14);
    ctx.strokeStyle = "rgba(0,0,0,.85)";
    ctx.fillStyle = "#ffd43b";
    const fade = (/** @type {number} */ t) => (t < 0.6 ? 1 : (1 - t) / 0.4);
    // The arrows go under the numbers, dark-edged to show on any ground.
    ctx.lineCap = "round";
    for (const f of v.floats) {
      const t = (now - f.t0) / FLOAT_MS;
      if (!f.to || t < 0 || t >= 1) continue;
      const [a, b] = centers([f.hex, f.to]);
      const len = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
      const k = (s * 0.35) / len;
      const pts = /** @type {[number, number][]} */ ([a, [b[0] - (b[0] - a[0]) * k, b[1] - (b[1] - a[1]) * k]]);
      ctx.globalAlpha = fade(t);
      for (const [color, width] of /** @type {[string, number][]} */ ([["rgba(0,0,0,.6)", s * 0.16 + 3], [f.color, s * 0.16]])) {
        ctx.save();
        ctx.strokeStyle = ctx.fillStyle = color;
        ctx.lineWidth = Math.max(2, width);
        arrow(ctx, pts);
        ctx.restore();
      }
    }
    for (const f of v.floats) {
      const t = (now - f.t0) / FLOAT_MS;
      if (f.to || t < 0 || t >= 1) continue;
      const [x, y] = center(f.hex[0], f.hex[1]);
      const rise = s * (0.35 + 0.9 * (1 - (1 - t) * (1 - t)));
      ctx.globalAlpha = fade(t);
      ctx.strokeText(f.text, x, y - rise);
      ctx.fillText(f.text, x, y - rise);
    }
    ctx.restore();
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
    // Resource sites have no owners and can't be captured (STRATEGIC.md §4),
    // so they get no border or capture progress.
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
    [[-0.3, -0.34], [0.3, -0.34], [-0.3, 0.3], [0.3, 0.3]],
  ];

  /** The radius of each unit in a stack of `n`. @param {number} n */
  function stackRadius(n) {
    const s = v.geo.size;
    // Four are a little smaller, so the upper two's health bars fit between the rows.
    return n === 1 ? s * 0.5 : n <= 3 ? s * 0.33 : n === 4 ? s * 0.29 : s * 0.26;
  }

  /** @param {CanvasRenderingContext2D} ctx @param {number} persp */
  function drawUnits(ctx, persp) {
    const s = v.geo.size;
    const a = v.anim;
    const hidden = (/** @type {any[]} */ u) => persp && u[1] !== persp && !visibleTo(u[3], u[4], persp);
    // While units slide during playback, the moving ones are drawn on their
    // own, and units killed on the way stay until the slide ends.
    const still = a ? v.state.units.filter((/** @type {any[]} */ u) => !a.paths.has(u[0])).concat(a.ghosts) : v.state.units;
    v.unitPos = new Map();
    const picked = v.play && !v.play.over && v.play.sel ? v.play.sel.unit : null;
    /** @type {Map<string, any[]>} */
    const byHex = new Map();
    for (const u of still) {
      // Units aboard a transport show as a count on it (see drawUnit).
      if (hidden(u) || u[12]) continue;
      const key = u[3] + "," + u[4];
      if (!byHex.has(key)) byHex.set(key, []);
      /** @type {any[]} */ (byHex.get(key)).push(u);
    }
    for (const list of byHex.values()) {
      const [x, y] = center(list[0][3], list[0][4]);
      const n = list.length;
      const place = (/** @type {any[]} */ u, /** @type {number} */ j) => {
        const off = n <= 4 ? STACK[n][j] : [0.38 * Math.cos((2 * Math.PI * j) / n), 0.38 * Math.sin((2 * Math.PI * j) / n)];
        drawUnit(ctx, u, x + off[0] * s, y + off[1] * s, stackRadius(n), n === 1);
      };
      // The unit picked in a game is drawn last, in its usual place, so its
      // neighbors in the stack don't cover it or its health bar.
      const top = list.findIndex((u) => u[0] === picked);
      list.forEach((u, j) => j !== top && place(u, j));
      if (top >= 0) place(list[top], top);
      // The health bars of a stack go over all its units, so none is covered,
      // and so do the marks of the ones dug in.
      if (n > 1) for (const u of list) drawHealth(ctx, u), drawFort(ctx, u), drawCargo(ctx, u);
    }
    if (!a) return;
    for (const u of v.state.units) {
      const path = a.paths.get(u[0]);
      if (!path) continue;
      const d = Math.min(path.length - 1, a.p * a.len);
      // The other side's unit shows only while it crosses hexes in view.
      const h = path[Math.round(d)];
      if (persp && u[1] !== persp && !visibleTo(h[0], h[1], persp)) continue;
      // Sized for the stack it ends in, so it doesn't change size on arrival.
      const n = v.state.units.filter((/** @type {any[]} */ w) => w[3] === u[3] && w[4] === u[4] && !w[12]).length;
      const [x, y] = pointAlong(path, d);
      drawUnit(ctx, u, x, y, stackRadius(n));
    }
  }

  /** Whether a unit type has a flag, such as "naval". @param {string} code @param {string} flag */
  function hasFlag(code, flag) {
    const t = v.info.units[code];
    return !!t && t.flags.split(",").includes(flag);
  }

  /** @param {CanvasRenderingContext2D} ctx @param {any[]} u @param {number} x @param {number} y @param {number} rad @param {boolean} [bar] whether to draw its health bar too */
  function drawUnit(ctx, u, x, y, rad, bar = true) {
    const [, owner, code, , , , mp, flags] = u;
    v.unitPos.set(u[0], [x, y, rad]);
    // The units of the player whose turn it is darken as they are used up.
    // With no movement left the disc goes dark but its outline, letter and
    // health stay bright; once it has attacked or captured, which ends its
    // turn, it goes darker and those dim too.
    const mine = owner === v.state.player;
    const done = mine && (flags & 1) !== 0;
    const fill = done ? 0.3 : mine && mp === 0 ? 0.55 : 1;
    const ink = done ? 0.55 : 1;
    ctx.beginPath();
    // Ships are rounded squares, and land units discs.
    if (hasFlag(code, "naval")) ctx.roundRect(x - rad, y - rad, 2 * rad, 2 * rad, rad * 0.45);
    else ctx.arc(x, y, rad, 0, 2 * Math.PI);
    if (fill < 1) {
      // Dark under the color, which also hides the move lines under the unit.
      ctx.fillStyle = "#16181c";
      ctx.fill();
    }
    ctx.globalAlpha = fill;
    ctx.fillStyle = playerColor(owner);
    ctx.fill();
    ctx.globalAlpha = ink;
    ctx.lineWidth = Math.max(1, rad * 0.14);
    ctx.strokeStyle = v.dark ? "rgba(255,255,255,.7)" : "rgba(0,0,0,.55)";
    if (flags & 4) ctx.setLineDash([2, 2]);
    ctx.stroke();
    ctx.setLineDash([]);
    if (v.built.has(u[0])) {
      // A small white dot on the corner of a unit built this turn. New units
      // stand on cities, whose outline has the player's color, so the dot
      // doesn't use it.
      ctx.beginPath();
      ctx.arc(x + rad * 0.7, y - rad * 0.7, Math.max(1.5, rad * 0.3), 0, 2 * Math.PI);
      ctx.fillStyle = "#fff";
      ctx.fill();
      ctx.lineWidth = Math.max(1, rad * 0.1);
      ctx.stroke();
    }
    if (rad >= 5) {
      // The unit's symbol or its letter, as the user chose (pieces.js).
      const type = v.info.units[code];
      BitbathPieces.draw(ctx, code, type ? type.name : "", x, y, rad);
    }
    // A transport shows how many units are aboard on its upper left corner.
    const aboard = hasFlag(code, "transport") ? v.state.units.filter((/** @type {any[]} */ w) => w[12] === u[0]).length : 0;
    if (aboard) {
      const br = Math.max(4, rad * 0.45);
      const bx = x - rad * 0.8;
      const by = y - rad * 0.8;
      ctx.beginPath();
      ctx.arc(bx, by, br, 0, 2 * Math.PI);
      ctx.fillStyle = "#16181c";
      ctx.fill();
      ctx.lineWidth = Math.max(1, br * 0.2);
      ctx.strokeStyle = "#fff";
      ctx.stroke();
      ctx.fillStyle = "#fff";
      ctx.font = "bold " + Math.round(br * 1.3) + "px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(aboard), bx, by + br * 0.06);
    }
    ctx.globalAlpha = 1;
    if (bar) drawHealth(ctx, u), drawFort(ctx, u), drawCargo(ctx, u);
  }

  /** The colors of wheat and of bread, wherever the two are told apart. */
  const WHEAT = "#ffd43b";
  const BREAD = "#d9822b";

  /**
   * What a wagon carries, with supply on: a small crate on its lower right,
   * yellow for wheat and brown for bread, half and half for both. An empty
   * wagon has none. Drawn where the unit was last drawn.
   * @param {CanvasRenderingContext2D} ctx @param {any[]} u
   */
  function drawCargo(ctx, u) {
    const at = v.unitPos.get(u[0]);
    const [wheat, bread] = [u[9] || 0, u[10] || 0];
    if (!at || (!wheat && !bread)) return;
    const [x, y, rad] = at;
    const side = Math.max(5, rad * 0.62);
    const [bx, by] = [x + rad * 0.82 - side / 2, y + rad * 0.42 - side / 2];
    // Dimmed with the rest of a unit whose turn is over.
    ctx.globalAlpha = u[1] === v.state.player && u[7] & 1 ? 0.55 : 1;
    ctx.fillStyle = wheat ? WHEAT : BREAD;
    ctx.fillRect(bx, by, side, side);
    if (wheat && bread) {
      ctx.fillStyle = BREAD;
      ctx.fillRect(bx + side / 2, by, side / 2, side);
    }
    ctx.lineWidth = Math.max(1, side * 0.16);
    ctx.strokeStyle = "rgba(0,0,0,.8)";
    ctx.strokeRect(bx, by, side, side);
    // The crate's band.
    ctx.beginPath();
    ctx.moveTo(bx, by + side / 2);
    ctx.lineTo(bx + side, by + side / 2);
    ctx.lineWidth = Math.max(1, side * 0.1);
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  /**
   * With supply on: the bread in each city and the wheat on each site, as a
   * number at the foot of its hex. Through a player's fog, only the ones
   * that player sees, as for capture progress (STRATEGIC.md §8).
   * @param {CanvasRenderingContext2D} ctx @param {number} persp
   */
  function drawStockpiles(ctx, persp) {
    const s = v.geo.size;
    if (!v.info.supply || !v.state.sites || s < 14) return;
    ctx.save();
    ctx.font = "600 " + Math.max(9, Math.round(s * 0.3)) + "px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    ctx.lineWidth = Math.max(2, s * 0.09);
    const label = (/** @type {number} */ q, /** @type {number} */ r, /** @type {number} */ n, /** @type {string} */ color) => {
      if (persp && !visibleTo(q, r, persp)) return;
      const [x, y] = center(q, r);
      const text = n.toLocaleString();
      ctx.strokeStyle = "rgba(0,0,0,.75)";
      ctx.strokeText(text, x, y + s * 0.78);
      ctx.fillStyle = color;
      ctx.fillText(text, x, y + s * 0.78);
    };
    v.info.cities.forEach((/** @type {number[]} */ c, /** @type {number} */ i) => {
      // A neutral city holds nothing until someone takes it.
      if (v.state.cities[i][0]) label(c[0], c[1], v.state.cities[i][4] || 0, "#fff");
    });
    // Every site grows wheat, for whichever wagon loads it.
    v.info.sites.forEach((/** @type {number[]} */ site, /** @type {number} */ i) => {
      label(site[0], site[1], v.state.sites[i][3], WHEAT);
    });
    ctx.restore();
  }

  /**
   * How far a unit is dug in: a white block on its top edge for each
   * fortification level, like the teeth of a wall. Drawn where the unit was
   * last drawn.
   * @param {CanvasRenderingContext2D} ctx @param {any[]} u
   */
  function drawFort(ctx, u) {
    const at = v.unitPos.get(u[0]);
    const n = u[8];
    if (!at || !n) return;
    const [x, y, rad] = at;
    const side = Math.max(3, rad * 0.38);
    const step = side * 1.3;
    const square = hasFlag(u[2], "naval");
    // Dimmed with the rest of a unit whose turn is over.
    ctx.globalAlpha = u[1] === v.state.player && u[7] & 1 ? 0.55 : 1;
    ctx.fillStyle = "#fff";
    ctx.strokeStyle = "rgba(0,0,0,.8)";
    ctx.lineWidth = Math.max(1, side * 0.22);
    for (let i = 0; i < n; i++) {
      const k = (i - (n - 1) / 2) * step;
      ctx.save();
      // Along the top of a ship's square, and around the rim of a disc.
      if (square) {
        ctx.translate(x + k, y - rad);
      } else {
        ctx.translate(x + rad * Math.sin(k / rad), y - rad * Math.cos(k / rad));
        ctx.rotate(k / rad);
      }
      ctx.beginPath();
      ctx.rect(-side / 2, -side / 2, side, side);
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }
    ctx.globalAlpha = 1;
  }

  /** The health bar under a unit, where the unit was last drawn. @param {CanvasRenderingContext2D} ctx @param {any[]} u */
  function drawHealth(ctx, u) {
    const at = v.unitPos.get(u[0]);
    if (!at) return;
    const [x, y, rad] = at;
    const type = v.info.units[u[2]];
    const w = rad * 1.8;
    const h = Math.max(2, rad * 0.26);
    const frac = Math.max(0, Math.min(1, u[5] / (type ? type.max_hp : 100)));
    // Dimmed with the rest of a unit whose turn is over.
    ctx.globalAlpha = u[1] === v.state.player && u[7] & 1 ? 0.55 : 1;
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
    // Hit points as the turn's fights go, to work out the damage each did.
    /** @type {Map<number, number>} */
    const hp = new Map();
    for (const u of v.state.units) hp.set(u[0], u[5]);
    for (const u of v.base.state.units) hp.set(u[0], u[5]);
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
        const m = { step: i, op: "A", id: Number(t[1]), target, ok, hexes: to ? [from, to] : [from], kill: res[0] === "OK" && res[1] === "0", dealt: 0, taken: 0 };
        if (res[0] === "OK") {
          m.dealt = Math.max(0, (hp.get(target) || 0) - Number(res[1]));
          m.taken = Math.max(0, (hp.get(m.id) || 0) - Number(res[2]));
          hp.set(target, Number(res[1]));
          hp.set(m.id, Number(res[2]));
        }
        marks.push(m);
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
    ctx.lineWidth = Math.max(2, s * 0.13);
    ctx.lineCap = ctx.lineJoin = "round";
    const trail = (/** @type {any} */ m) => {
      if (m.op === "M") {
        polyline(ctx, centers(m.hexes));
      } else if (m.op === "A" && m.hexes.length === 2) {
        const pts = centers(m.hexes);
        polyline(ctx, pts);
        if (m.kill) cross(ctx, pts[1], s * 0.3);
      }
    };
    // In a game being played, what the human saw of the opponent's last turn
    // stays on the map through their own.
    if (fogged() && v.state.player === persp) {
      ctx.globalAlpha = 0.2;
      ctx.strokeStyle = playerColor(3 - persp);
      for (const m of v.play.seen) trail(m);
    }
    // A replay's trails are how it shows the turn. The human's own, in a game
    // being played, are only a reminder, and stay well behind the move being
    // planned.
    ctx.globalAlpha = fogged() === v.state.player ? 0.12 : 0.35;
    ctx.strokeStyle = playerColor(v.state.player);
    for (const m of v.marks) {
      if (m.ok && !(a && m.step >= a.from) && shown(m, persp)) trail(m);
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

  /**
   * Highlights the order just applied. It's called twice: with `moves` for a
   * move, before the units are drawn, and without for any other order, after.
   * @param {CanvasRenderingContext2D} ctx @param {number} persp @param {boolean} moves
   */
  function drawOrder(ctx, persp, moves) {
    const m = v.marks[v.marks.length - 1];
    if (!m || m.step !== v.pos - 1 || v.anim || !shown(m, persp)) return;
    if ((m.op === "M") !== moves) return;
    // Playing turn by turn, a turn's last order matters no more than the
    // others, so it shows as just its trail.
    if (v.playing && $("speed").value.startsWith("turn")) return;
    // Nor does the move the human just made: they watched it happen.
    if (moves && m.ok && fogged() === v.state.player) return;
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
    // A bot's annotations are its plans, so its opponent doesn't see them.
    if (!$("debug").checked || v.pos === 0 || fogged()) return;
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

  /**
   * Counts names into a phrase like "2 infantry and 1 scout", in first-seen
   * order, or with `sep` between the counts instead.
   * @param {string[]} names @param {string} [sep]
   */
  function tally(names, sep) {
    /** @type {Map<string, number>} */
    const counts = new Map();
    for (const name of names) counts.set(name, (counts.get(name) || 0) + 1);
    const parts = Array.from(counts, ([name, n]) => count(n, name));
    return sep ? parts.join(sep) : join(parts);
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
    return s + " " + cityStanding(cities);
  }

  /** Says who holds more cities. @param {number[]} cities */
  function cityStanding(cities) {
    const c = cities[0] >= cities[1] ? 1 : 2;
    if (cities[0] === cities[1]) return "Each side holds " + cityCount(cities[0]) + ".";
    return who(c) + " holds " + cityCount(cities[c - 1]) + " to " + cities[2 - c] + ".";
  }

  /** The value of some units: each one's cost scaled by the HP it has left (SPEC §3.8). @param {any[][]} units */
  function armyValue(units) {
    return units.reduce((sum, u) => {
      const t = v.info.units[u[2]];
      return sum + (t ? Math.floor((t.cost * u[5]) / t.max_hp) : 0);
    }, 0);
  }

  /** The turn summary's rows: always these, in this order, so the panel holds still. */
  const SUMMARY_ROWS = ["Moved", "Attacked", "Damage", "Destroyed", "Lost", "Cities", "Built", "Issues", "Time"];

  /**
   * Sums up the current turn so far as rows of label and value, from the
   * marks of its orders and their replies. A row's value is "–" when nothing
   * of its kind happened.
   * @param {any} turn @param {number} ti @returns {string[][]}
   */
  function turnSummary(turn, ti) {
    if (!turn) return SUMMARY_ROWS.map((label) => [label, "–"]);
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
    /** @type {string[]} */ const captured = [];
    /** @type {Map<string, string>} */ const capturing = new Map();
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
        const here = (/** @type {number[]} */ c) => c[0] === q && c[1] === r;
        const city = v.info.cities.findIndex(here);
        const key = city >= 0 ? String(city) : "site " + v.info.sites.findIndex(here);
        capturing.delete(key);
        if (res[1] === "captured") captured.push(key);
        else capturing.set(key, key + " (" + res[1] + "/" + res[2] + ")");
      } else if (m.op === "B") {
        const t = v.info.units[m.type];
        built.push(t ? t.name : m.type);
        spent += t ? t.cost : 0;
      }
    }
    let rejected = 0;
    for (let i = first + 1; i < v.pos; i++) {
      if ((v.info.results[i] || "").startsWith("ERR")) rejected++;
    }

    /** @type {Record<string, string>} */
    const out = {};
    if (moved.size) out.Moved = count(moved.size, "unit") + (stopped ? ", " + stopped + " stopped early" : "");
    if (attacks) {
      out.Attacked = attacks === 1 ? "once" : attacks + " times";
      out.Damage = dealt + " dealt, " + taken + " taken";
    }
    if (killed.length) out.Destroyed = tally(killed, ", ");
    if (lost.length) out.Lost = tally(lost, ", ");
    const cityNews = [];
    if (captured.length) cityNews.push("captured " + captured.join(", "));
    if (capturing.size) cityNews.push("capturing " + Array.from(capturing.values()).join(", "));
    if (cityNews.length) out.Cities = cityNews.join("; ");
    if (built.length) out.Built = tally(built, ", ") + " · " + spent + (v.info.supply ? " bread" : " gold");
    const issues = [];
    if (turn.skipped) issues.push("missed the turn");
    if (rejected) issues.push(rejected + " rejected");
    if (turn.late && turn.late.length) issues.push(turn.late.length + " late");
    if (turn.timeout) issues.push("ran out of time");
    if (issues.length) out.Issues = issues.join(" · ");
    // Games played in the page have no clock, so they show only the time taken.
    if (turn.time_ms) out.Time = turn.used_ms.toLocaleString() + " of " + turn.time_ms.toLocaleString() + " ms";
    else if (turn.used_ms) out.Time = (turn.used_ms / 1000).toFixed(1) + " s";
    return SUMMARY_ROWS.map((label) => [label, out[label] || "–"]);
  }

  function updateSide() {
    const st = v.state;
    const ti = v.pos > 0 ? v.replay.stepTurn[v.pos - 1] : -1;
    const turn = ti >= 0 ? v.replay.turns[ti] : null;
    // In a game being played, the human sees of the opponent only what the
    // fog of war lets them: the units in sight, and none of the orders.
    const fog = fogged();
    const secretTurn = !!(fog && turn && turn.player !== fog);
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
    // With supply on there's no gold; the column shows the bread each player has.
    const money = v.info.supply ? st.bread : st.gold;
    for (const label of ["", "cities", "units", v.info.supply ? "bread" : "gold", "army"]) head.append(el("th", label));
    table.append(head);
    const army = [0, 0];
    const cities = [0, 0];
    for (const p of [1, 2]) {
      const secret = !!fog && p !== fog;
      const units = st.units.filter((/** @type {any[]} */ u) => u[1] === p && !(secret && !visibleTo(u[3], u[4], fog)));
      // With supply on, an army counts its player's bread too (STRATEGIC.md
      // §9), which a hidden opponent's doesn't show.
      army[p - 1] = armyValue(units) + (v.info.supply && !secret ? money[p - 1] : 0);
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
        // Of a hidden opponent, the units in sight and what they are worth:
        // at least that much, hence the plus.
        el("td", !secret ? String(units.length) : units.length ? units.length + "+" : "?", "num"),
        el("td", secret ? "?" : String(money[p - 1]), "num"),
        el("td", !secret ? String(army[p - 1]) : units.length ? army[p - 1] + "+" : "?", "num"),
      );
      // What the army is made of, under its numbers, in a box of fixed size
      // (as are the other parts of the panel) so the panel holds still.
      const text = secret ? (units.length ? "In sight: " + composition(units) : "None in sight") : composition(units);
      const box = el("div", text);
      box.title = text;
      const mix = el("td", "", "mix");
      mix.colSpan = 5;
      mix.append(box);
      const mixRow = document.createElement("tr");
      mixRow.append(mix);
      table.append(row, mixRow);
    }
    const lead = fog ? cityStanding(cities) : standing(army, cities);
    $("standing").textContent = lead;
    $("standing").title = lead;

    const summary = $("summary");
    summary.replaceChildren();
    for (const [label, value] of turnSummary(secretTurn ? null : turn, ti)) {
      const cell = el("div", value, "value");
      cell.title = value;
      summary.append(el("div", label, "label"), cell);
    }
    const orders = $("orders");
    if (!turn || secretTurn) {
      orders.replaceChildren();
      $("orders-count").textContent = secretTurn ? "The opponent's orders are hidden" : "No orders yet";
      v.listTurn = -1;
    } else {
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
          if (!fog) row.onclick = () => setPos(first + j + 1);
          orders.append(row);
        });
        $("orders-count").textContent = list.length ? "All " + count(list.length, "order") + (rejected ? ", " + rejected + " rejected" : "") : "No orders";
        v.listTurn = ti;
      }
      const cur = v.pos - 1 - first;
      Array.from(orders.children).forEach((/** @type {HTMLElement} */ row, /** @type {number} */ j) => {
        row.classList.toggle("current", j === cur);
        if (j === cur) row.scrollIntoView({ block: "nearest" });
      });
    }
    const line = v.pos > 0 ? v.replay.script[v.pos - 1] : "";
    $("reply").textContent = line.startsWith("O ") && !secretTurn ? line.slice(2) + "\n→ " + st.reply.join("\n→ ") : "";

    const notes = $("notes");
    notes.replaceChildren();
    if (turn && $("debug").checked && !fog) {
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
    if (v.play) v.play.update();
    updateHover();
  }

  // The hover card: what is on the hex under the pointer, shown beside it.

  /** How long the pointer rests on the board before the card shows, in ms. Once it shows, it keeps up with the pointer. */
  const HOVER_DELAY = 160;
  const hoverCard = el("div", "", "");
  hoverCard.id = "hover-card";
  hoverCard.hidden = true;
  $("board").append(hoverCard);
  /** Where the pointer is on the board, in CSS pixels. */
  let pointer = [0, 0];
  let hoverTimer = 0;

  function hideHover() {
    clearTimeout(hoverTimer);
    hoverTimer = 0;
    hoverCard.hidden = true;
  }

  /** A labeled bar for the card, `frac` full. @param {string} text @param {number} frac @param {string} color */
  function meter(text, frac, color) {
    const box = el("div", "", "hc-meter");
    const bar = el("div", "", "hc-bar");
    const fill = el("span", "");
    fill.style.width = 100 * Math.max(0, Math.min(1, frac)) + "%";
    fill.style.background = color;
    bar.append(fill);
    box.append(el("div", text), bar);
    return box;
  }

  /** Fills the hover card for the hex under the pointer, and shows it once the pointer has rested. */
  function updateHover() {
    const menu = document.getElementById("menu");
    if (!v.hover || !v.state || (menu && !menu.hidden)) return hideHover();
    const [q, r] = v.hover;
    const ch = v.info.rows[r][q + Math.floor(r / 2)];
    const t = v.info.terrain[ch];
    // In a game being played, the fog of war hides what is on the hex.
    const fog = fogged();
    const seen = !fog || visibleTo(q, r, fog);
    const num = (/** @type {number} */ n) => n.toLocaleString();
    const who = (/** @type {number} */ p) => (!p ? "neutral" : p === fog ? "yours" : playerName(p));
    const dot = (/** @type {number} */ p) => {
      const d = el("span", "", "hc-dot");
      if (p) d.style.background = d.style.borderColor = playerColor(p);
      return d;
    };
    /** @type {HTMLElement[]} */
    const parts = [];

    // The ground.
    const head = el("div", "", "hc-head");
    const chip = el("span", "", "hc-chip");
    chip.style.background = /** @type {any} */ (v.dark ? TERRAIN.dark : TERRAIN.light)[ch] || "#888";
    head.append(chip, el("b", t ? capitalize(t.name) : ch), el("span", q + "," + r, "hc-coord"));
    parts.push(head);
    if (t) {
      const facts = [];
      if (t.move_cost) facts.push("Move " + t.move_cost, "Defense " + t.defense + "%");
      if (t.naval_move_cost) facts.push("Ships move " + t.naval_move_cost);
      if (!t.move_cost && !t.naval_move_cost) facts.push("Can't be entered");
      if (t.vision_bonus) facts.push("Vision +" + t.vision_bonus);
      if (t.conceal) facts.push("Conceals units");
      const row = el("div", "", "hc-facts");
      for (const f of facts) row.append(el("span", f));
      parts.push(row);
    }

    // The city or site on it.
    const cp = v.info.capture_points;
    /** @param {string} name @param {number | null} owner null for a site, which no one owns @param {number} capturer @param {number} progress @param {HTMLElement[]} lines */
    const place = (name, owner, capturer, progress, lines) => {
      const sec = el("div", "", "hc-sec");
      const title = el("div", "", "hc-title");
      title.append(dot(owner || 0), el("b", name), el("span", owner === null ? "no owner" : who(owner), "hc-owner"));
      sec.append(title, ...lines);
      if (capturer && seen) sec.append(meter((capturer === fog ? "You are" : playerName(capturer) + " is") + " capturing it · " + progress + " of " + cp, progress / cp, playerColor(capturer)));
      parts.push(sec);
    };
    v.info.cities.forEach((/** @type {number[]} */ c, /** @type {number} */ i) => {
      if (c[0] !== q || c[1] !== r) return;
      const [owner, capturer, progress, , bread] = v.state.cities[i];
      place("City " + i, owner, capturer, progress, v.info.supply && seen ? [el("div", num(bread) + " bread", "hc-line")] : []);
    });
    (v.state.sites ? v.info.sites : []).forEach((/** @type {number[]} */ s, /** @type {number} */ i) => {
      if (s[0] !== q || s[1] !== r) return;
      const wheat = v.state.sites[i][3];
      const lines = [el("div", "Grows " + s[3] + " wheat a round" + (seen ? "" : ", up to " + num(s[4])), "hc-line")];
      if (seen) lines.push(meter(num(wheat) + " of " + num(s[4]) + " wheat", wheat / s[4], WHEAT));
      place("Site " + i, null, 0, 0, lines);
    });

    // The units on it, each transport followed by the units aboard.
    const here = v.state.units.filter((/** @type {any[]} */ u) => u[3] === q && u[4] === r && (seen || u[1] === fog));
    const listed = here.filter((/** @type {any[]} */ u) => !u[12]).flatMap((/** @type {any[]} */ u) => [u].concat(here.filter((/** @type {any[]} */ p) => p[12] === u[0])));
    if (listed.length) {
      const sec = el("div", "", "hc-sec");
      // The unit picked in a game is marked, since on a transport it can't be told from the board.
      const picked = fog && v.play.sel ? v.play.sel.unit : null;
      for (const u of listed) {
        const type = v.info.units[u[2]];
        const name = type ? type.name : u[2];
        const frac = Math.max(0, Math.min(1, u[5] / (type ? type.max_hp : 100)));
        const row = el("div", "", "hc-unit" + (u[12] ? " aboard" : "") + (u[0] === picked ? " picked" : ""));
        const main = el("div", "", "hc-main");
        const top = el("div", "", "hc-top");
        top.append(el("b", capitalize(name)), el("span", "#" + u[0], "hc-id"));
        if (u[0] === picked) top.append(el("span", "selected", "hc-picked"));
        top.append(el("span", u[5] + " hp", "hc-hp"));
        const bar = el("div", "", "hc-bar");
        const fill = el("span", "");
        fill.style.width = 100 * frac + "%";
        fill.style.background = frac > 0.6 ? "#3fb950" : frac > 0.3 ? "#e3b341" : "#f85149";
        bar.append(fill);
        const tags = [];
        // Movement and actions are known for the side whose turn it is.
        if (u[1] === v.state.player) tags.push(u[7] & 4 ? "new" : u[7] & 1 ? "done" : u[6] + " mp");
        if (u[8]) tags.push("dug in " + u[8]);
        if (v.info.supply) tags.push(u[11] + " rations");
        if (hasFlag(u[2], "carry")) tags.push(u[9] || u[10] ? num(u[9]) + " wheat, " + num(u[10]) + " bread" : "empty");
        if (hasFlag(u[2], "transport")) {
          const riders = here.filter((/** @type {any[]} */ p) => p[12] === u[0]).length;
          tags.push(riders ? riders + " aboard" : "nobody aboard");
        }
        if (u[12]) tags.push("aboard");
        main.append(top, bar, el("div", tags.join(" · "), "hc-tags"));
        row.append(BitbathPieces.icon({ code: u[2], name, naval: hasFlag(u[2], "naval") }, playerColor(u[1]), 22), main);
        sec.append(row);
      }
      parts.push(sec);
    }
    if (fog && !seen) parts.push(el("div", "Out of sight", "hc-sec hc-fog"));

    // What a click would do, in a game being played. A fight has its own card.
    const acts = fog ? v.play.hoverLines(q, r).filter((/** @type {string} */ l) => !l.startsWith("attack:")) : [];
    if (acts.length) {
      const sec = el("div", "", "hc-sec hc-acts");
      for (const a of acts) sec.append(el("div", capitalize(a)));
      parts.push(sec);
    }

    hoverCard.replaceChildren(...parts);
    if (hoverCard.hidden && !hoverTimer) {
      hoverTimer = window.setTimeout(() => {
        hoverTimer = 0;
        if (!v.hover) return;
        hoverCard.hidden = false;
        placeHover();
      }, HOVER_DELAY);
    }
    placeHover();
  }

  /**
   * Puts the hover card beside the pointer: on the side away from the unit
   * picked in a game, so it doesn't lie over the way there. While a fight is
   * forecast, it sits under that card instead, so the two read as one.
   */
  function placeHover() {
    if (hoverCard.hidden) return;
    const menu = document.getElementById("menu");
    if (menu && !menu.hidden) return hideHover();
    const board = $("board");
    const [bw, bh] = [board.clientWidth, board.clientHeight];
    const [w, h] = [hoverCard.offsetWidth, hoverCard.offsetHeight];
    const fc = document.getElementById("forecast");
    let [x, y] = [0, 0];
    if (fc && !fc.hidden) {
      x = fc.offsetLeft;
      y = fc.offsetTop + fc.offsetHeight + 6;
      if (y + h > bh - 4) y = fc.offsetTop - h - 6;
    } else {
      const picked = v.play && v.play.sel && v.play.sel.unit !== null ? v.unitPos.get(v.play.sel.unit) : null;
      const right = !picked || pointer[0] >= picked[0];
      const below = !picked || pointer[1] >= picked[1];
      x = right ? pointer[0] + 18 : pointer[0] - 18 - w;
      y = below ? pointer[1] + 20 : pointer[1] - 20 - h;
      if (x + w > bw - 4) x = pointer[0] - 18 - w;
      if (x < 4) x = pointer[0] + 18;
      if (y + h > bh - 4) y = pointer[1] - 20 - h;
      if (y < 4) y = pointer[1] + 20;
    }
    hoverCard.style.left = Math.max(4, Math.min(bw - w - 4, x)) + "px";
    hoverCard.style.top = Math.max(4, Math.min(bh - h - 4, y)) + "px";
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

  // The rules dialog: the rules the game was played under, in plain words.

  /**
   * Reads the rule constants, base damage and starting units from a
   * replay's setup lines (SPEC §6.2). The terrain and unit types come from
   * the engine's info instead.
   * @param {string[]} setup
   */
  function parseRules(setup) {
    /** @type {{c: Record<string, number>, damage: Map<string, number[]>, start: string[]}} */
    const rules = { c: {}, damage: new Map(), start: [] };
    for (const line of setup) {
      const t = line.trim().split(/\s+/);
      if (t[0] === "RULE") rules.c[t[1]] = Number(t[2]);
      else if (t[0] === "DAMAGE") rules.damage.set(t[1], t.slice(2).map(Number));
      else if (t[0] === "START") rules.start = t.slice(1);
    }
    return rules;
  }

  const ABILITIES = /** @type {Record<string, string>} */ ({
    capture: "captures cities",
    zoc: "zone of control",
    skirmish: "ignores zones of control",
    no_move_attack: "can't move and attack in one turn",
    fortify: "digs in when it holds still",
    carry: "carries wheat and bread",
    naval: "moves on water",
    transport: "carries land units",
  });

  function buildRules() {
    const body = $("rules-body");
    body.replaceChildren();
    const { c, damage, start } = parseRules(v.replay.header.setup);
    const units = Object.entries(v.info.units);
    const name = (/** @type {string} */ code) => (v.info.units[code] ? v.info.units[code].name : code);
    const secs = (/** @type {number} */ ms) => (ms / 1000).toLocaleString() + " s";
    const num = (/** @type {number} */ n) => n.toLocaleString();
    /** @param {string} title @param {string[]} items */
    const section = (title, items) => {
      const list = document.createElement("ul");
      for (const item of items) list.append(el("li", item));
      body.append(el("h3", title), list);
    };
    /** @param {(string | Node)[]} head @param {any[][]} rows @param {number} [textCol] a column of words, aligned left */
    const table = (head, rows, textCol = -1) => {
      const t = document.createElement("table");
      const tr = document.createElement("tr");
      head.forEach((h, i) => {
        const th = el("th", "", i === textCol ? "text" : "");
        th.append(h);
        tr.append(th);
      });
      t.append(tr);
      for (const row of rows) {
        const r = document.createElement("tr");
        row.forEach((cell, i) => {
          const td = el("td", "", i === textCol ? "text" : "");
          td.append(cell instanceof Node ? cell : String(cell));
          r.append(td);
        });
        t.append(r);
      }
      const wrap = el("div", "", "table-wrap");
      wrap.append(t);
      body.append(wrap);
    };

    // A unit's name in a table comes with its symbol, whatever marks the
    // board uses, so the rules are also the key to the symbols. The token is
    // gray: it stands for the type, not for either player's unit.
    const piece = (/** @type {string} */ code, /** @type {string} */ label) => {
      const span = document.createElement("span");
      span.append(BitbathPieces.icon({ code, name: name(code), naval: hasFlag(code, "naval") }, "#5c6672", 18, "symbols"), label);
      return span;
    };

    const neutral = v.info.cities.filter((/** @type {number[]} */ city) => city[2] === 0).length;
    const game = [
      "Two players on a " + v.info.width + " × " + v.info.height + " hex map with " + neutral + " neutral " + (neutral === 1 ? "city" : "cities") + ".",
    ];
    // With supply on (the strategic ruleset), bread takes the place of gold.
    const supply = !!c.supply;
    if (start.length) {
      const money = supply ? num(c.start_bread) + " bread in it" : num(c.start_gold) + " gold";
      game.push("Each player starts with " + tally(start.map(name)) + " on a home city, and " + money + ".");
    }
    game.push(
      "A player who owns no cities loses at once.",
      "After " + c.round_limit + " rounds, the player with more cities wins. If that's tied, the bigger army wins: each unit counts its cost scaled by the HP it has left" + (supply ? ", and all the bread a player holds, in cities and wagons, counts too" : "") + ". Otherwise it's a draw.",
      "A bot that crashes, goes over its memory limit, or fails to finish " + c.hang_turns + " turns in a row forfeits.",
    );
    section("The game", game);

    const eats = "Feeding: each unit eats " + c.eat_base + " ration" + (c.eat_base === 1 ? "" : "s") + ", plus " + c.eat_move + " if it moved and " + c.eat_attack + " if it attacked on its last turn. Then it refills to " + c.ration_max + " from your cities and wagons on or next to its hex, at " + c.ration_cost + " bread a ration: cities first, then wagons, lower ids first. A unit that can't cover what it ate starves: it loses " + c.starve_hp + " HP, and doesn't heal this turn.";
    section("Each turn", [
      supply
        ? "Production: at the start of each round, every site grows its output of wheat, up to its cap, whoever stands on it or near it. Each of your cities makes " + c.city_bread + " bread, unless an enemy unit is on it or next to it."
        : "Income: " + c.city_income + " gold per city owned, minus " + c.unit_upkeep + " per unit. Gold never drops below 0, and nothing is disbanded.",
      ...(supply ? [eats] : []),
      "Units standing on their own cities heal " + c.city_heal + " HP.",
      "A city's capture progress is lost when the capturing player no longer has a unit on it.",
      "Every unit gets its full movement back, and one action: an attack or a capture. A unit can't move after its action.",
    ]);

    if (supply) {
      section("Wheat and bread", [
        "Wheat grows at sites. It becomes bread, one for one, only when it enters a city. Bread builds units and feeds them; nothing eats wheat.",
        "Wagons carry up to " + num(c.cargo_max) + " wheat and bread together. L loads a wagon with the wheat of the site it stands on, or the bread of your city; U unloads it into your city it stands on. A wagon can load and unload as often as it likes in a turn, but can't move afterward.",
        "Sites have no owners, and no one captures them: any wagon on one can load its wheat, and a unit standing on one keeps the enemy's wagons off it. Each city keeps its own bread, which goes to whoever captures it.",
      ]);
    }

    const ground = Object.values(v.info.terrain).filter((t) => t.move_cost);
    const hexes = (/** @type {number} */ n) => (n === 1 ? "1 hex" : n + " hexes");
    const sight = [
      "Players see the hexes within their units' vision (see Units) and within " + c.city_vision + " hexes of their own cities. Enemy units anywhere else are hidden. The map, and who owns each city, are always known.",
    ];
    for (const t of ground.filter((t) => t.vision_bonus > 0)) sight.push("A unit on " + t.name + " sees " + hexes(t.vision_bonus) + " farther.");
    for (const t of ground.filter((t) => t.conceal > 0)) {
      const near = units.filter(([, u]) => u.vision - t.conceal <= 1).map(([, u]) => plural(u.name));
      sight.push(
        capitalize(t.name) + " hides the units in it: observers see into " + t.name + " " + hexes(t.conceal) + " less far than usual, but always see the hexes next to them." +
          (near.length ? " Without a vision bonus, " + join(near) + " spot units in " + t.name + " only from next door." : ""),
      );
    }
    section("Vision", sight);

    const blocked = Object.values(v.info.terrain).filter((t) => !t.move_cost).map((t) => t.name);
    const seas = Object.values(v.info.terrain).filter((t) => t.naval_move_cost);
    section("Moving", [
      "Each step costs the move cost of the hex entered (see Terrain)." + (blocked.length ? " " + capitalize(join(blocked)) + " can't be entered" + (seas.length ? " by land units" : "") + "." : ""),
      "Units may pass through friendly units but must stop on a hex with fewer than " + c.stack_limit + " units, and can't enter a hex with enemy units.",
      "Zone of control: a unit that steps next to an enemy unit with zone of control must stop there. Units that ignore zones of control are exempt, and leaving costs nothing extra.",
      "Moves are checked against what the player can see. A hidden enemy can cut a move short: walking into one is an ambush.",
    ]);

    if (seas.length) {
      const ships = units.filter(([, u]) => u.flags.split(",").includes("naval")).map(([, u]) => plural(u.name));
      section("Ships", [
        capitalize(join(ships)) + " move only on " + join(seas.map((t) => t.name)) + ", by the ships' move cost (see Terrain), and can't enter land or cities.",
        "A port is a city next to water. It builds ships onto the first water hex next to it, in direction order, that has room and no enemy units.",
        "A land unit boards one of its player's transports by moving onto it as the last step of a move, for 1 MP, which ends its move. A transport carries up to " + c.passengers_max + " land units, wagons included.",
        "Units aboard move with their transport. They can't attack, capture, load, or unload, and can't be attacked. They don't count toward the stack limit and exert no zone of control, but they still see and eat.",
        "A unit aboard lands with a one-step move onto land next to its transport, which ends its move but leaves its action, so it can attack or capture where it lands.",
        "When a transport is destroyed, everything aboard is destroyed with it.",
        "Ships heal " + c.city_heal + " HP next to their own cities. Zones of control reach across the shoreline both ways.",
      ]);
    }

    const capturers = units.filter(([, u]) => u.flags.split(",").includes("capture"));
    const hp = capturers.length ? capturers[0][1].max_hp : 0;
    const turns = hp && capturers.every(([, u]) => u.max_hp === hp) ? " At full HP that takes " + Math.ceil(c.capture_points / hp) + " turns." : "";
    section("Cities", [
      "A unit that captures adds its current HP to a city's capture progress; " + c.capture_points + " points take the city." + turns,
      "Each city you own can build one unit per turn, if no enemy unit is on it and it holds fewer than " + c.stack_limit + " of your units" + (supply ? ", paying with its own bread" : "") + ". New units can't move or act until your next turn.",
    ]);

    const diggers = units.filter(([, u]) => u.flags.split(",").includes("fortify")).map(([, u]) => plural(u.name));
    const combat = [
      "A unit attacks one visible enemy unit within its range; any unit in a stack can be picked. Attacking uses the action and ends the unit's movement.",
      diggers.length
        ? "Damage = base damage × attacker's HP ÷ its max HP × (100% − the defender's defense: its terrain's, plus " + c.fortify_pct + "% per fortification level), rounded down, and at least 1."
        : "Damage = base damage × attacker's HP ÷ its max HP × (100% − the defender's terrain defense), rounded down, and at least 1.",
      "If the defender survives and the attacker is within its range, it strikes back the same way with the HP it has left.",
    ];
    if (diggers.length) {
      combat.push(
        "Digging in: " + join(diggers) + " that end a turn without moving, attacking, or capturing gain a fortification level, up to " + c.fortify_max + ". Moving loses them all; attacking or capturing keeps them but adds none.",
      );
    }
    section("Combat", combat);
    table(
      [/** @type {string | Node} */ ("Base damage: attacker ↓ defender →")].concat(units.map(([code, u]) => piece(code, u.name))),
      units.map(([code, u]) => [/** @type {any} */ (piece(code, u.name))].concat(damage.get(code) || [])),
    );

    body.append(el("h3", "Units"));
    table(
      ["Unit", "Cost", "HP", "Move", "Vision", "Range", "Abilities"],
      units.map(([code, u]) => {
        const abilities = u.flags.split(",").filter((/** @type {string} */ f) => ABILITIES[f]).map((/** @type {string} */ f) => ABILITIES[f]);
        if (u.range[0] > 1) abilities.push("can't attack adjacent units");
        const range = u.range[0] === u.range[1] ? String(u.range[0]) : u.range[0] + "–" + u.range[1];
        return [piece(code, u.name + " (" + code + ")"), u.cost, u.max_hp, u.move, u.vision, range, abilities.join(", ") || "–"];
      }),
      6,
    );

    body.append(el("h3", "Terrain"));
    const pal = v.dark ? TERRAIN.dark : TERRAIN.light;
    table(
      ["Terrain", "Move cost", "Defense", "Vision", "Concealment"].concat(seas.length ? ["Ships' move cost"] : []),
      Object.entries(v.info.terrain).map(([ch, t]) => {
        const chip = el("span", "", "chip");
        chip.style.background = /** @type {any} */ (pal)[ch] || "#888";
        const label = document.createElement("span");
        label.append(chip, t.name);
        const ships = seas.length ? [t.naval_move_cost || "can't enter"] : [];
        if (!t.move_cost) return [label, "can't enter", "–", "–", "–"].concat(ships);
        return [label, t.move_cost, t.defense + "%", t.vision_bonus ? "+" + t.vision_bonus : "0", t.conceal || 0].concat(ships);
      }),
    );

    section("Time and limits", [
      "Each turn a bot has its time bank plus " + secs(c.time_increment_ms) + ". " + c.time_keep_pct + "% of the time it doesn't use goes into the bank, up to " + secs(c.time_bank_cap_ms) + "; going over takes from the bank. The bank starts at " + secs(c.time_bank_ms) + ".",
      secs(c.init_ms) + " to start up. At most " + num(c.max_orders_per_turn) + " orders per turn, and " + num(c.max_line_bytes) + " bytes per line.",
    ]);
  }

  // Navigation.

  /** @param {number} k @param {boolean} [playing] whether this is a step of playback, which keeps the damage numbers floating */
  function setPos(k, playing) {
    if (!v.engine || !v.replay) return;
    v.anim = null;
    if (!playing) v.floats = [];
    v.pos = Math.max(0, Math.min(v.steps, k));
    v.state = v.engine.seek(v.pos);
    v.marks = turnMarks();
    v.built = new Set();
    if (v.pos > 0) {
      const had = new Set(v.base.state.units.map((/** @type {any[]} */ u) => u[0]));
      for (const u of v.state.units) if (!had.has(u[0])) v.built.add(u[0]);
    }
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
   * In one player's view, only what that player sees of it is worth a slide.
   * @param {number} k @param {number} ms @param {boolean} [movesOnly] slide only if a unit moved
   * @returns {number} how long the slide takes: 0 if there is nothing to slide
   */
  function animateTo(k, ms, movesOnly) {
    const from = v.pos;
    const old = v.state;
    setPos(k, true);
    /** @type {Map<number, number[][]>} */
    const paths = new Map();
    for (const m of v.marks) {
      if (m.step < from || m.op !== "M" || !m.ok) continue;
      const path = paths.get(m.id);
      paths.set(m.id, path ? path.concat(m.hexes.slice(1)) : m.hexes);
    }
    const alive = new Set(v.state.units.map((/** @type {any[]} */ u) => u[0]));
    let ghosts = old ? old.units.filter((/** @type {any[]} */ u) => !alive.has(u[0])) : [];
    const persp = viewOf();
    if (persp) {
      const sees = (/** @type {number[]} */ h) => visibleTo(h[0], h[1], persp);
      const owner = new Map(v.state.units.concat(ghosts).map((/** @type {any[]} */ u) => [u[0], u[1]]));
      for (const [id, path] of paths) if (owner.get(id) !== persp && !path.some(sees)) paths.delete(id);
      ghosts = ghosts.filter((/** @type {any[]} */ u) => u[1] === persp || sees([u[3], u[4]]));
    }
    const slides = ms >= 40 && (paths.size > 0 || (!movesOnly && ghosts.length > 0));
    // The numbers wait for units on the move to get most of the way there.
    if (ms >= 40 && k > from) floatDamage(from, persp, slides && paths.size ? ms * 0.7 : 0);
    if (!slides) return 0;
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
    return ms;
  }

  /** How long a unit takes to slide when stepping one order: short enough to feel instant. */
  const STEP_MS = 110;

  /**
   * Steps forward one order. A unit that moves slides along its path, to show
   * where it went. Any other navigation, or a held key, snaps instead.
   * @param {boolean} [snap]
   */
  function stepForward(snap) {
    if (snap) setPos(v.pos + 1);
    else animateTo(v.pos + 1, STEP_MS, true);
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

  /** Shows or hides the warning about a replay that plays back differently. @param {string} problem */
  function showMismatch(problem) {
    const banner = document.getElementById("mismatch");
    if (!banner) return; // an index.html from before the warning existed
    $("mismatch-text").textContent = problem;
    banner.hidden = !problem;
  }

  async function engine() {
    if (!v.engine) v.engine = await BitbathEngine.create();
    return v.engine;
  }

  /**
   * Shows a game from its start: a replay, or with `play` a game that is
   * about to be played in the page.
   * @param {any} replay @param {any} [play]
   */
  async function show(replay, play, savedPosition = false) {
    stop();
    if (v.play) v.play.close();
    showMismatch("");
    try {
      const unfinished = !play && !replay.result;
      const { steps, info } = (await engine()).load(replay.header.setup.join("\n"), replay.script.join("\n"), unfinished);
      v.play = play || null;
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
      if (!play) showMismatch(checkReplay());
      if (info.army) buildLegend();
      updateChartTip();
      buildRules();
      $("rules-button").hidden = false;
      $("orders-box").hidden = false;
      $("slider").max = String(v.ends.length - 1);
      $("empty").style.display = "none";
      $("arena").classList.add("has-match");
      const h = replay.header;
      $("matchup").textContent = h.players[0].name + " (P1) vs " + h.players[1].name + " (P2) · " + h.map + " · " + info.width + "×" + info.height;
      document.title = h.players[0].name + " vs " + h.players[1].name + " · Bitbath";
      syncMode();
      computeGeo();
      buildTerrain();
      if (unfinished) $("matchup").textContent += " · Unfinished";
      setPos(savedPosition ? steps : 0);
      if (!$("new-game").open) {
        $("arena").scrollIntoView({ block: "start" });
        $("arena").focus({ preventScroll: true });
      }
      return true;
    } catch (e) {
      v.play = null;
      syncMode();
      $("empty").style.display = "";
      $("error").textContent = String(e instanceof Error ? e.message : e);
      return false;
    }
  }

  /** @param {string} text */
  async function openText(text, savedPosition = false) {
    let replay;
    try {
      replay = BitbathEngine.parseReplay(text);
    } catch (e) {
      $("error").textContent = String(e instanceof Error ? e.message : e);
      return;
    }
    await show(replay, null, savedPosition);
  }

  // Live games. A game played in the page is a replay that grows: each step
  // goes to the engine and onto the end of the replay, so everything that
  // shows a replay shows the game so far. While it is being played, the
  // human sees it through their fog of war, always at its latest step.

  /** Fits the page to what is shown: a replay, or a game being played. */
  function syncMode() {
    const fog = fogged();
    $("arena").classList.toggle("live", !!fog);
    $("arena").classList.toggle("played", !!v.play);
    // The army chart needs the timeline that a current bitbath_wasm.js adds,
    // and would give away the opponent's strength during a game.
    $("chart-box").hidden = $("army-legend").hidden = !!fog || !v.info || !v.info.army;
  }

  /**
   * Starts a game to be played in the page, from a replay header.
   * @param {any} header @param {any} play the game's controls: see play.js
   */
  function openLive(header, play) {
    return show({ header, turns: [], script: [], stepTurn: [], turnStart: [], init: null, result: null }, play);
  }

  /**
   * Adds a step to the game being played and returns its reply lines. With
   * a turn record, the step starts that turn; otherwise it is an order of
   * the current turn. Call setPos or animateTo afterwards to show it.
   * @param {string} line `T <player>` or `O <order>` @param {any} [turn]
   * @returns {string[]}
   */
  function append(line, turn) {
    const r = v.replay;
    const reply = /** @type {BitbathEngine} */ (v.engine).push(line);
    if (turn) {
      r.turnStart.push(r.script.length);
      r.turns.push(turn);
    } else {
      r.turns[r.turns.length - 1].orders.push(line.slice(2));
    }
    r.script.push(line);
    r.stepTurn.push(r.turns.length - 1);
    v.info.results.push(reply[reply.length - 1] || "");
    v.steps++;
    v.ends = [0].concat(r.turnStart.slice(1), [v.steps]);
    v.listTurn = -1;
    $("slider").max = String(v.ends.length - 1);
    return reply;
  }

  /** Ends the game being played: from here on it is a replay like any other. @param {any} result the replay's result record */
  function finishLive(result) {
    v.replay.result = result;
    v.play.over = true;
    v.info.army = v.ends.map((e) => {
      const units = /** @type {BitbathEngine} */ (v.engine).seek(e).units;
      return [1, 2].map((p) => armyValue(units.filter((/** @type {any[]} */ u) => u[1] === p)));
    });
    $("view").value = "0";
    buildLegend();
    syncMode();
    setPos(v.steps);
  }

  /**
   * Compares the re-simulation with what the replay recorded: each turn's
   * rejected orders (or each player's total, in replays recorded before
   * turns counted them), then the outcome. Returns the first difference as a
   * sentence, or "" if they agree.
   */
  function checkReplay() {
    const { turns, turnStart, result } = v.replay;
    const results = v.info.results;
    const totals = [0, 0, 0];
    for (let ti = 0; ti < turns.length; ti++) {
      const t = turns[ti];
      const end = ti + 1 < turns.length ? turnStart[ti + 1] : v.steps;
      let rejected = 0;
      for (let i = turnStart[ti] + 1; i < end; i++) if ((results[i] || "").startsWith("ERR")) rejected++;
      if (typeof t.rejected === "number" && t.rejected !== rejected) {
        return "In round " + t.round + ", the viewer rejects " + rejected + " of " + playerName(t.player) + "'s orders, but the game rejected " + (t.rejected || "none") + ".";
      }
      totals[t.player] += rejected;
    }
    if (!result) return "";
    // The recorded totals also count orders refused for limits, which aren't
    // in the replay, so only a higher count shows a difference.
    for (const p of [1, 2]) {
      const s = (result.stats || [])[p - 1];
      if (s && typeof s.invalid === "number" && totals[p] > s.invalid) {
        return "Over the game, the viewer rejects " + totals[p].toLocaleString() + " of " + playerName(p) + "'s orders, but the game rejected " + (s.invalid ? s.invalid.toLocaleString() : "none") + ".";
      }
    }
    const end = v.engine.seek(v.steps).outcome;
    const decided = ["elimination", "cities", "sites", "army", "draw"].includes(result.reason);
    if (end ? end[0] !== result.winner || end[1] !== result.reason : decided) {
      const recorded = result.winner ? playerName(result.winner) + " winning by " + result.reason : "a draw";
      return "The game ended with " + recorded + ", but the viewer's game " + (end ? "ends differently" : "doesn't end") + ".";
    }
    return "";
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
  $("next").onclick = () => stepForward();
  $("next-turn").onclick = () => setPos(nextTurn());
  $("last").onclick = () => setPos(v.steps);
  $("slider").oninput = () => setPos(v.ends[Number($("slider").value)] || 0);
  $("view").onchange = () => {
    updateSide();
    draw();
  };
  $("debug").onchange = () => {
    updateSide();
    draw();
  };

  $("rules-button").onclick = () => $("rules").showModal();
  // A click on the backdrop lands on the dialog itself, since its content fills it.
  $("rules").addEventListener("click", (/** @type {MouseEvent} */ e) => {
    if (e.target === $("rules")) $("rules").close();
  });

  document.addEventListener("keydown", (e) => {
    const target = /** @type {HTMLElement} */ (e.target);
    // With nothing focused, a key goes to the body. That happens whenever the
    // button just clicked is redrawn away, so those keys are the board's too.
    const here = target === document.body || target === document.documentElement || $("arena").contains(target);
    if (!v.replay || !here || target.matches("input, select") || document.querySelector("dialog[open]")) return;
    if (target.closest("button, label, summary, a") && ["Tab", "Enter", " "].includes(e.key)) return;
    // A game being played stays at its latest step, and has its own keys.
    if (fogged()) return v.play.key(e);
    const keys = {
      " ": togglePlay,
      ArrowRight: () => (e.shiftKey ? setPos(nextTurn()) : stepForward(e.repeat)),
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
  /** Whether the last press on the map became a drag, so it isn't also a click. */
  let dragged = false;
  canvas.addEventListener("mousedown", (/** @type {MouseEvent} */ e) => {
    dragged = false;
    if (e.button === 0) drag = { x: e.clientX, y: e.clientY, pan: v.pan.slice(), moved: false };
  });
  window.addEventListener("mouseup", () => (drag = null));
  // In a game being played, a click picks a unit or gives it an order. A
  // right click gives the order too, and drops the selection where there is none to give.
  canvas.addEventListener("click", (/** @type {MouseEvent} */ e) => {
    if (!v.info || !fogged() || dragged) return;
    const rect = canvas.getBoundingClientRect();
    v.play.click(hexAt(e.clientX - rect.left, e.clientY - rect.top), e.clientX - rect.left, e.clientY - rect.top, e.shiftKey);
  });
  canvas.addEventListener("contextmenu", (/** @type {MouseEvent} */ e) => {
    if (!fogged()) return;
    e.preventDefault();
    if (!v.info || dragged) return;
    const rect = canvas.getBoundingClientRect();
    v.play.context(hexAt(e.clientX - rect.left, e.clientY - rect.top), e.clientX - rect.left, e.clientY - rect.top);
  });
  canvas.addEventListener("mousemove", (/** @type {MouseEvent} */ e) => {
    if (!v.info) return;
    if (drag && (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3 || drag.moved)) {
      drag.moved = dragged = true;
      v.pan = [drag.pan[0] + e.clientX - drag.x, drag.pan[1] + e.clientY - drag.y];
      hideHover();
      redrawAll();
      return;
    }
    const rect = canvas.getBoundingClientRect();
    pointer = [e.clientX - rect.left, e.clientY - rect.top];
    const h = hexAt(pointer[0], pointer[1]);
    if (String(h) !== String(v.hover)) {
      v.hover = h;
      updateHover();
      draw();
    } else placeHover();
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
    if (fogged()) return; // clicks give orders there, and often come in pairs
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
    if (v.replay) buildRules();
  });

  const url = new URLSearchParams(location.search).get("replay");
  if (url) {
    $("arena").scrollIntoView();
    fetch(url)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(url + ": HTTP " + r.status))))
      .then(text => openText(text, new URLSearchParams(location.search).get("position") === "latest"))
      .catch((e) => ($("error").textContent = String(e.message || e)));
  }

  // For play.js, tests and embedding: window.bitbathViewer.open(text).
  /** @type {any} */ (window).bitbathViewer = {
    refreshHover: updateHover,
    open: openText, state: v, setPos, $, el, engine, openLive, append, finishLive, animateTo, draw, updateSide, STEP_MS,
    center, hexPath, polyline, arrow, centers, playerColor, cssVar, visibleTo, shown, capitalize,
  };
})();
