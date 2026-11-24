// @ts-check
// How units look on the board: a symbol for each kind of unit, or its letter.
// The viewer (viewer.js) draws each unit's token and asks this file for the
// mark on it. The choice is the user's, made in the play page's settings
// dialog and kept in the browser.

const BitbathPieces = (() => {
  "use strict";

  /** The ways to mark a unit. The first is the one a new visitor gets. */
  const STYLES = [
    { id: "symbols", name: "Symbols", about: "A picture for each kind of unit." },
    { id: "letters", name: "Letters", about: "Each unit's letter, as orders and replays write it." },
  ];
  const KEY = "bitbath.pieces";

  function stored() {
    try {
      const id = localStorage.getItem(KEY);
      return STYLES.some((s) => s.id === id) ? /** @type {string} */ (id) : STYLES[0].id;
    } catch (e) {
      // A page that may not keep anything still gets the first style.
      return STYLES[0].id;
    }
  }

  let current = stored();
  /** @type {(() => void)[]} */
  const listeners = [];

  /** The style in use: the id of one of STYLES. */
  function style() {
    return current;
  }

  /** @param {string} id */
  function setStyle(id) {
    if (id === current || !STYLES.some((s) => s.id === id)) return;
    current = id;
    try {
      localStorage.setItem(KEY, id);
    } catch (e) {
      // The choice still holds for this visit.
    }
    for (const f of listeners) f();
  }

  /** Calls `f` whenever the style changes. @param {() => void} f */
  function onChange(f) {
    listeners.push(f);
  }

  // The symbols, by unit type name. Each draws in a square from -1 to 1,
  // where the token is the circle of radius 1, with the fill and stroke
  // colors already set. `w` turns a line width into one that stays at least
  // a pixel wide on small tokens. They are silhouettes with no fine detail,
  // since a unit in a stack is only some 16 pixels across.

  /** @typedef {(ctx: CanvasRenderingContext2D, w: (n: number) => number) => void} Glyph */

  /** @param {CanvasRenderingContext2D} ctx @param {number[][]} pts */
  function polygon(ctx, pts) {
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.closePath();
    ctx.fill();
  }

  /** @param {CanvasRenderingContext2D} ctx @param {number} x1 @param {number} y1 @param {number} x2 @param {number} y2 @param {number} width */
  function line(ctx, x1, y1, x2, y2, width) {
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.lineWidth = width;
    ctx.stroke();
  }

  /** @param {CanvasRenderingContext2D} ctx @param {number} x @param {number} y @param {number} r */
  function dot(ctx, x, y, r) {
    ctx.beginPath();
    ctx.arc(x, y, r, 0, 2 * Math.PI);
    ctx.fill();
  }

  /** @type {Record<string, Glyph>} */
  const GLYPHS = {
    // A sword, point up and to the right.
    infantry(ctx, w) {
      ctx.rotate(0.7);
      polygon(ctx, [[0, -0.74], [0.115, -0.58], [0.115, 0.16], [-0.115, 0.16], [-0.115, -0.58]]);
      line(ctx, -0.3, 0.2, 0.3, 0.2, w(0.15));
      line(ctx, 0, 0.26, 0, 0.5, w(0.13));
      dot(ctx, 0, 0.6, 0.115);
    },

    // Three pikes held upright, the middle one tallest.
    pikeman(ctx, w) {
      for (const [x, top] of [[-0.42, -0.6], [0, -0.84], [0.42, -0.6]]) {
        polygon(ctx, [[x, top], [x + 0.15, top + 0.3], [x, top + 0.44], [x - 0.15, top + 0.3]]);
        line(ctx, x, top + 0.4, x, 0.7, w(0.11));
      }
    },

    // A horse's head, as on a chess knight, looking left.
    cavalry(ctx) {
      ctx.beginPath();
      ctx.moveTo(-0.34, 0.64);
      ctx.lineTo(0.5, 0.64);
      ctx.bezierCurveTo(0.6, 0.18, 0.52, -0.32, 0.2, -0.58);
      ctx.lineTo(0.16, -0.8);
      ctx.lineTo(-0.04, -0.6);
      ctx.bezierCurveTo(-0.26, -0.5, -0.46, -0.24, -0.62, 0.02);
      ctx.lineTo(-0.56, 0.22);
      ctx.lineTo(-0.36, 0.24);
      ctx.bezierCurveTo(-0.26, 0.12, -0.16, 0.04, -0.04, 0.04);
      ctx.bezierCurveTo(-0.08, 0.26, -0.3, 0.38, -0.34, 0.64);
      ctx.closePath();
      ctx.fill();
    },

    // A drawn bow, its arrow pointing right.
    archer(ctx, w) {
      ctx.translate(-0.08, 0);
      ctx.beginPath();
      ctx.moveTo(-0.2, -0.68);
      ctx.quadraticCurveTo(0.66, 0, -0.2, 0.68);
      ctx.lineWidth = w(0.14);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(-0.2, -0.68);
      ctx.lineTo(-0.5, 0);
      ctx.lineTo(-0.2, 0.68);
      ctx.lineWidth = w(0.06);
      ctx.stroke();
      line(ctx, -0.5, 0, 0.5, 0, w(0.1));
      polygon(ctx, [[0.8, 0], [0.46, -0.19], [0.46, 0.19]]);
    },

    // An eye.
    scout(ctx, w) {
      ctx.beginPath();
      ctx.moveTo(-0.72, 0);
      ctx.quadraticCurveTo(0, -0.7, 0.72, 0);
      ctx.quadraticCurveTo(0, 0.7, -0.72, 0);
      ctx.closePath();
      ctx.lineWidth = w(0.13);
      ctx.stroke();
      dot(ctx, 0, 0, 0.2);
    },

    // A cart: a box on two wheels, with its pole in front.
    wagon(ctx, w) {
      ctx.beginPath();
      ctx.roundRect(-0.58, -0.4, 0.94, 0.5, 0.07);
      ctx.fill();
      line(ctx, 0.36, -0.02, 0.72, -0.16, w(0.1));
      ctx.lineWidth = w(0.11);
      for (const x of [-0.36, 0.14]) {
        ctx.beginPath();
        ctx.arc(x, 0.38, 0.17, 0, 2 * Math.PI);
        ctx.stroke();
      }
    },

    // A broad ship under a square sail.
    transport(ctx, w) {
      polygon(ctx, [[-0.7, 0.2], [0.7, 0.2], [0.46, 0.56], [-0.46, 0.56]]);
      line(ctx, 0, -0.6, 0, 0.2, w(0.09));
      ctx.beginPath();
      ctx.roundRect(-0.42, -0.6, 0.84, 0.56, 0.06);
      ctx.fill();
    },

    // A warship: a ram at the bow, oars, and a slanted sail.
    galley(ctx, w) {
      polygon(ctx, [[-0.72, -0.12], [-0.5, 0.08], [0.46, 0.08], [0.8, 0.22], [0.42, 0.36], [-0.48, 0.36], [-0.64, 0.2]]);
      for (const x of [-0.26, 0.04, 0.34]) line(ctx, x, 0.36, x - 0.14, 0.68, w(0.08));
      polygon(ctx, [[-0.1, -0.74], [-0.1, -0.08], [0.48, -0.08]]);
    },
  };

  /** Whether a unit type has a symbol. Types a config adds have none, and show their letter. @param {string} name */
  function hasSymbol(name) {
    return Object.prototype.hasOwnProperty.call(GLYPHS, name);
  }

  /**
   * Draws a unit's mark on its token, in the style in use, or in `as`.
   * @param {CanvasRenderingContext2D} ctx @param {string} code the type's letter @param {string} name the type's name
   * @param {number} x @param {number} y the token's center @param {number} rad its radius
   * @param {string} [as] a style's id, to draw in that style whatever the one in use
   */
  function draw(ctx, code, name, x, y, rad, as) {
    ctx.fillStyle = ctx.strokeStyle = "#fff";
    if ((as || current) === "symbols" && hasSymbol(name)) {
      ctx.save();
      ctx.translate(x, y);
      // A little inside the token's outline.
      ctx.scale(rad * 0.94, rad * 0.94);
      ctx.lineCap = ctx.lineJoin = "round";
      GLYPHS[name](ctx, (n) => Math.max(n, 1 / rad));
      ctx.restore();
      return;
    }
    ctx.font = "bold " + Math.round(rad * 1.1) + "px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(code, x, y + rad * 0.06);
  }

  /**
   * A small picture of a unit's token, for buttons and lists.
   * @param {{code: string, name: string, naval?: boolean}} type @param {string} color the owner's color
   * @param {number} [px] its width and height in CSS pixels @param {string} [as] a style's id, as in draw()
   */
  function icon(type, color, px = 18, as) {
    const dpr = window.devicePixelRatio || 1;
    const cv = document.createElement("canvas");
    cv.className = "piece-icon";
    cv.width = cv.height = Math.round(px * dpr);
    cv.style.width = cv.style.height = px + "px";
    cv.setAttribute("aria-hidden", "true");
    const ctx = /** @type {CanvasRenderingContext2D} */ (cv.getContext("2d"));
    ctx.scale(dpr, dpr);
    const rad = px / 2 - 1;
    ctx.beginPath();
    // Ships are rounded squares and land units discs, as on the board.
    if (type.naval) ctx.roundRect(1, 1, 2 * rad, 2 * rad, rad * 0.45);
    else ctx.arc(px / 2, px / 2, rad, 0, 2 * Math.PI);
    ctx.fillStyle = color;
    ctx.fill();
    draw(ctx, type.code, type.name, px / 2, px / 2, rad, as);
    return cv;
  }

  // The settings dialog of the play page.

  /** The types to show when no game is open: the standard five. */
  const SAMPLE = [
    { code: "I", name: "infantry" },
    { code: "P", name: "pikeman" },
    { code: "K", name: "cavalry" },
    { code: "A", name: "archer" },
    { code: "S", name: "scout" },
  ];

  /** The unit types of the game on show, or the standard ones. @returns {{code: string, name: string, naval?: boolean}[]} */
  function typesOnShow() {
    const viewer = /** @type {any} */ (window).bitbathViewer;
    const units = viewer && viewer.state.info ? viewer.state.info.units : null;
    if (!units) return SAMPLE;
    return Object.keys(units).map((code) => ({ code, name: units[code].name, naval: String(units[code].flags || "").split(",").includes("naval") }));
  }

  /** Fills the dialog's list of styles, each with the game's units drawn in it. @param {HTMLElement} list */
  function fillStyles(list) {
    const colors = ["--p1", "--p2"].map((name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888");
    const types = typesOnShow();
    list.replaceChildren();
    for (const s of STYLES) {
      const card = document.createElement("label");
      card.className = "piece-style";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "piece-style";
      radio.value = s.id;
      radio.checked = s.id === current;
      radio.onchange = () => setStyle(s.id);
      const text = document.createElement("span");
      text.className = "piece-style-text";
      const name = document.createElement("b");
      name.textContent = s.name;
      const about = document.createElement("span");
      about.textContent = s.about;
      text.append(name, about);
      const row = document.createElement("span");
      row.className = "piece-style-row";
      types.forEach((type, i) => {
        const cell = document.createElement("span");
        cell.title = type.name;
        // Both players' colors show, turn about.
        cell.append(icon(type, colors[i % 2], 34, s.id));
        row.append(cell);
      });
      card.append(radio, text, row);
      list.append(card);
    }
  }

  const page = typeof document === "undefined" ? null : document;
  const button = page && page.getElementById("settings-button");
  const dialog = /** @type {HTMLDialogElement | null} */ (page && page.getElementById("settings-dialog"));
  const list = page && page.getElementById("piece-styles");
  if (button && dialog && list) {
    button.onclick = () => {
      fillStyles(list);
      dialog.showModal();
    };
    // A click on the backdrop closes it, as with the page's other dialogs.
    dialog.addEventListener("click", (e) => {
      if (e.target === dialog) dialog.close();
    });
  }
  onChange(() => {
    const viewer = /** @type {any} */ (window).bitbathViewer;
    if (viewer && viewer.state.state) viewer.draw();
  });

  return { STYLES, style, setStyle, onChange, hasSymbol, draw, icon };
})();

if (typeof module !== "undefined") module.exports = BitbathPieces;
