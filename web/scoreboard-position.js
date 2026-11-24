/* global BitbathEngine */
"use strict";
importScripts("bitbath_wasm.js", "engine.js");

const engine = BitbathEngine.create();
self.onmessage = async ({ data: { id, updatedAt, seat } }) => {
  try {
    const response = await fetch("/api/games/" + id + "/replay", { cache: "no-store", signal: AbortSignal.timeout(20000) });
    if (!response.ok) throw new Error("Recording unavailable");
    const replay = BitbathEngine.parseReplay(await response.text());
    const referee = await engine;
    const { steps, info } = referee.load(replay.header.setup.join("\n"), replay.script.join("\n"), !replay.result);
    const state = referee.seek(steps);
    const seats = [seat, 3 - seat];
    const cities = seats.map(p => state.cities.filter(city => city[0] === p).length);
    const army = seats.map(p => info.army.at(-1)[p - 1]);
    self.postMessage({ id, updatedAt, cities, army });
  } catch {
    self.postMessage({ id, updatedAt, error: true });
  }
};
