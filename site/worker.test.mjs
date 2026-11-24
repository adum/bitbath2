import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import worker from "./worker.mjs";

// Exercise the Worker's D1 statements and transactions against SQLite.
function localDatabase() {
  const db = new DatabaseSync(":memory:");
  const migrations = new URL("./drizzle/", import.meta.url);
  for (const file of readdirSync(migrations).filter(n => n.endsWith(".sql")).sort()) {
    db.exec(readFileSync(new URL(file, migrations), "utf8"));
  }
  return {
    prepare(sql) {
      const stmt = db.prepare(sql);
      return {
        args: [],
        bind(...args) { this.args = args; return this; },
        first() { return stmt.get(...this.args); },
        all() { return { results: stmt.all(...this.args) }; },
      };
    },
    batch(statements) {
      db.exec("BEGIN");
      try {
        const result = statements.map(s => s.all());
        db.exec("COMMIT");
        return result;
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
    },
  };
}

test("retries, partial games, and replay views preserve one result per match", async () => {
  const env = { DB: localDatabase() };
  const url = "https://bitbath.test/api/";
  const get = path => worker.fetch(new Request(url + path), env);
  const score = async () => (await get("scoreboard")).json();
  const save = (id, records) => worker.fetch(new Request(url + "games/" + id, {
    method: "POST", body: JSON.stringify(records),
  }), env);
  const id = crypto.randomUUID(), guest = crypto.randomUUID();
  const header = { type: "header", setup: ["MAP 12 8"], players: [{ name: "A" }, { name: "B" }] };
  const record = (seq, revision, data) => ({ seq, revision, at: 100 + revision, data });
  const start = player => record(0, 1, { player, map: "Micro", opponent: "Smart", seat: 2, header });
  const partial = record(1, 2, { type: "turn", player: 1, round: 1, orders: [] });
  const turn = record(1, 3, { ...partial.data, orders: ["M 3 4"] });
  const end = record(1000000, 5, { type: "result", round: 2, winner: 2 });
  assert.equal((await save(id, [end])).status, 409);
  assert.equal((await save(id, [start("Alice"), turn])).status, 204);
  assert.equal((await save(id, [partial])).status, 204);
  assert.equal((await save(guest, [start("")])).status, 204);
  assert.equal((await score()).summary.started, 2);
  assert.equal((await get("games/" + crypto.randomUUID() + "/replay")).status, 404);
  const unfinished = await get("games/" + id + "/replay");
  assert.equal(unfinished.status, 200);
  const saved = (await unfinished.text()).trim().split("\n").map(JSON.parse);
  assert.deepEqual(saved.map(r => r.type), ["header", "init", "turn"]);
  assert.deepEqual(saved[2].orders, ["M 3 4"]);
  const before = await score();
  assert.deepEqual(before.players, [{ name: "Alice", games: 1, finished: 0, wins: 0, draws: 0, losses: 0 }]);
  assert.deepEqual(before.recent, []);
  const opening = await get("games/" + guest + "/replay");
  assert.deepEqual((await opening.text()).trim().split("\n").map(l => JSON.parse(l).type), ["header", "init"]);
  assert.equal((await save(id, [record(2, 4, { ...partial.data, round: 2 })])).status, 204);
  const [visible] = (await score()).recent;
  assert.equal(visible.id, id);
  assert.equal(visible.round, 2);
  assert.equal(visible.seat, 2);
  assert.equal(visible.outcome, null);
  for (let retry = 0; retry < 2; retry++) assert.equal((await save(id, [end])).status, 204);
  const replay = (await (await get("games/" + id + "/replay")).text()).trim().split("\n").map(JSON.parse);
  assert.deepEqual(replay.map(r => r.type), ["header", "init", "turn", "turn", "result"]);
  assert.deepEqual(replay[2].orders, ["M 3 4"]);
  assert.equal((await save(guest, [record(1000000, 2, { ...end.data, round: 1, winner: 1 })])).status, 204);
  const board = await score();
  assert.deepEqual(board.summary, { started: 2, finished: 2, wins: 1, avgRounds: 1.5 });
  assert.deepEqual(board.players, [{ name: "Alice", games: 1, finished: 1, wins: 1, draws: 0, losses: 0 }]);
  assert.deepEqual(board.recent.map(r => [r.id, r.outcome]), [[id, "win"]]);
  await save(crypto.randomUUID(), [start("Alice")]);
  assert.deepEqual((await score()).players, [{ name: "Alice", games: 2, finished: 1, wins: 1, draws: 0, losses: 0 }]);
});
