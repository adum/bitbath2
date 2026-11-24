/* global BitbathEngine, loadPyodide */
"use strict";

importScripts("bitbath_wasm.js", "engine.js", "pyodide-0.28.3/pyodide.js");

let engine, driver, botId;
let step = 0;
let orders = [], stderr = "", ended = false;

// The Python kit needs each order's reply before choosing its next order.
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

self.onmessage = async ({ data }) => {
  try {
    if (data.action === "load") {
      botId = data.id;
      const [python, referee, bundle] = await Promise.all([
        loadPyodide({ indexURL: new URL("pyodide-0.28.3/", self.location.href).href }),
        BitbathEngine.create(),
        fetch("reference-bots.zip").then((response) => {
          if (!response.ok) throw new Error("Could not load opponent strategies.");
          return response.arrayBuffer();
        }),
      ]);
      engine = referee;
      python.setStderr({ batched: (line) => { stderr = (stderr + line + "\n").slice(-8192); } });
      python.unpackArchive(bundle, "zip");
      driver = python.pyimport("browser_bot");
    } else if (data.action === "init") {
      engine.load(data.setup.join("\n"), "");
      driver.initialize(botId, data.lines.join("\n"), dispatch);
    } else if (data.action === "turn") {
      for (const line of data.steps) {
        engine.push(line);
        step++;
      }
      driver.turn(data.lines.join("\n"));
    }
    self.postMessage({ orders, stderr });
  } catch (error) {
    self.postMessage({ error: String(error), stderr });
  } finally {
    orders = [];
    stderr = "";
  }
};
