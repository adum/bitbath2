/* global BitbathEngine, createAstraModule, loadPyodide */
"use strict";

importScripts("bitbath_wasm.js", "engine.js");

let engine, driver;
let step = 0;
let orders = [], stderr = "", ended = false;

// Bots need each order's reply before choosing their next order.
// Simulate the same replay steps locally, then return the orders to the live referee.
function dispatch(line) {
  let reply = "";
  if (!line.startsWith("D ")) {
    if (ended) return null;
    const result = engine.push("O " + line);
    step++;
    reply = result.join("\n");
    if (result.at(-1) === "OK captured") ended = !!engine.seek(step).outcome;
  }
  orders.push({ line, reply });
  return reply;
}

function log(line) {
  stderr = (stderr + line + "\n").slice(-8192);
}

async function loadAstra() {
  importScripts("astra.js");
  const gameOver = new Error("Game over");
  const bot = await createAstraModule({
    print() {}, // READY and END are implicit in init/turn returning.
    printErr: log,
    sendOrder(line) {
      const reply = dispatch(line);
      if (reply === null) throw gameOver;
      return reply;
    },
  });
  return {
    init(lines) {
      if (!lines.includes("MAP 12 8")) throw new Error("GPT-6 Astra plays the Micro map only.");
      bot.ccall("astra_init", null, ["string"], [lines.join("\n")]);
    },
    turn(lines) {
      try {
        bot.ccall("astra_turn", null, ["string"], [lines.join("\n")]);
      } catch (error) {
        if (error !== gameOver) throw error;
      }
    },
  };
}

async function loadPythonBot(id) {
  importScripts("pyodide-0.28.3/pyodide.js");
  const [python, bundle] = await Promise.all([
    loadPyodide({ indexURL: new URL("pyodide-0.28.3/", self.location.href).href }),
    fetch("reference-bots.zip").then(response => {
      if (!response.ok) throw new Error("Could not load opponent strategies.");
      return response.arrayBuffer();
    }),
  ]);
  python.setStderr({ batched: log });
  python.unpackArchive(bundle, "zip");
  const bot = python.pyimport("browser_bot");
  return {
    init: lines => bot.initialize(id, lines.join("\n"), dispatch),
    turn: lines => bot.turn(lines.join("\n")),
  };
}

self.onmessage = async ({ data }) => {
  try {
    if (data.action === "load") {
      [engine, driver] = await Promise.all([BitbathEngine.create(),
        data.id === "gpt-6-astra" ? loadAstra() : loadPythonBot(data.id)]);
    } else if (data.action === "init") {
      engine.load(data.setup.join("\n"), "");
      driver.init(data.lines);
    } else if (data.action === "turn") {
      for (const line of data.steps) {
        engine.push(line);
        step++;
      }
      driver.turn(data.lines);
    }
    self.postMessage({ orders, stderr });
  } catch (error) {
    self.postMessage({ error: String(error), stderr });
  } finally {
    orders = [];
    stderr = "";
  }
};
