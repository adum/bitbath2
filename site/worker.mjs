// Site-played games only. Tournament standings ship separately with the homepage.
const finalSeq = 1000000;
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const json = (value, status = 200) => Response.json(value, { status, headers: { "Cache-Control": "no-store" } });

function check(ok) {
  if (!ok) throw new TypeError("Invalid game record");
}

async function save(db, id, request) {
  let records;
  try { records = await request.json(); } catch { return json({ error: "Expected JSON" }, 400); }
  const writes = [];
  let latest = 0, round = 0;
  try {
    check(Array.isArray(records) && records.length > 0 && records.length <= 24);
    for (const record of records) {
      const { seq, revision, at, data } = record;
      check(Number.isSafeInteger(seq) && seq >= 0 && seq <= finalSeq &&
        Number.isSafeInteger(revision) && revision >= 1 && Number.isSafeInteger(at) && at > 0 && data);
      latest = Math.max(latest, Math.min(at, Date.now()));
      if (seq === 0) {
        const { player, opponent, map, seat, header } = data;
        check(typeof player === "string" && player.length <= 48 && typeof opponent === "string" && opponent.length <= 128 &&
          typeof map === "string" && map.length <= 48 && (seat === 1 || seat === 2) &&
          header?.type === "header" && Array.isArray(header.setup) && header.players?.length === 2);
        writes.push(db.prepare(`INSERT INTO games
          (id, player_name, opponent, map_name, human_seat, started_at, updated_at, header)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING`)
          .bind(id, player.trim() || null, opponent, map, seat, at, latest, JSON.stringify(header)));
      } else {
        check(Number.isSafeInteger(data.round) && data.round >= 0);
        round = Math.max(round, data.round);
        if (seq === finalSeq) {
          check(data.type === "result" && [0, 1, 2].includes(data.winner));
          writes.push(db.prepare(`UPDATE games SET result = ?,
            outcome = CASE WHEN ? = 0 THEN 'draw' WHEN ? = human_seat THEN 'win' ELSE 'loss' END
            WHERE id = ? AND result IS NULL`).bind(JSON.stringify(data), data.winner, data.winner, id));
        } else {
          check(data.type === "turn" && [1, 2].includes(data.player) && Array.isArray(data.orders));
          writes.push(db.prepare(`INSERT INTO turns (game_id, seq, revision, data) VALUES (?, ?, ?, ?)
            ON CONFLICT(game_id, seq) DO UPDATE SET revision = excluded.revision, data = excluded.data
            WHERE excluded.revision > turns.revision`).bind(id, seq, revision, JSON.stringify(data)));
        }
      }
    }
  } catch (error) {
    if (error instanceof TypeError) return json({ error: "Invalid game record" }, 400);
    throw error;
  }
  // A final record arriving ahead of its header must remain in the browser's outbox.
  if (!records.some(r => r.seq === 0) && !await db.prepare("SELECT id FROM games WHERE id = ?").bind(id).first()) {
    return json({ error: "Game header pending" }, 409);
  }
  writes.push(db.prepare("UPDATE games SET updated_at = MAX(updated_at, ?), round = MAX(round, ?) WHERE id = ?")
    .bind(latest, round, id));
  await db.batch(writes);
  return new Response(null, { status: 204 });
}

async function scoreboard(db) {
  const [summary, players, recent] = await db.batch([
    db.prepare(`SELECT COUNT(*) AS started, COUNT(outcome) AS finished,
      COUNT(CASE WHEN outcome = 'win' THEN 1 END) AS wins,
      ROUND(AVG(CASE WHEN outcome IS NOT NULL THEN round END), 1) AS avgRounds FROM games`),
    db.prepare(`SELECT MIN(player_name) AS name, COUNT(*) AS games, COUNT(outcome) AS finished,
      COUNT(CASE WHEN outcome = 'win' THEN 1 END) AS wins,
      COUNT(CASE WHEN outcome = 'draw' THEN 1 END) AS draws, COUNT(CASE WHEN outcome = 'loss' THEN 1 END) AS losses
      FROM games WHERE player_name IS NOT NULL GROUP BY player_name COLLATE NOCASE
      ORDER BY (COUNT(outcome) > 0) DESC, wins DESC, 1.0 * COUNT(CASE WHEN outcome = 'win' THEN 1 END)
      / NULLIF(COUNT(outcome), 0) DESC, finished DESC, name COLLATE NOCASE LIMIT 100`),
    db.prepare(`SELECT id, player_name AS player, opponent, human_seat AS seat, map_name AS map, round, outcome, updated_at AS updatedAt
      FROM games ORDER BY updated_at DESC, id LIMIT 12`),
  ]);
  return json({ summary: summary.results[0], players: players.results, recent: recent.results });
}

async function replay(db, id) {
  // Read the header, turns, and optional result from one consistent snapshot.
  const [games, turns] = await db.batch([
    db.prepare("SELECT header, result FROM games WHERE id = ?").bind(id),
    db.prepare("SELECT data FROM turns WHERE game_id = ? ORDER BY seq").bind(id),
  ]);
  const game = games.results[0];
  if (!game) return json({ error: "Game not found" }, 404);
  const init = '{"type":"init","notes":["",""],"stderr":["",""]}';
  const records = [game.header, init, ...turns.results.map(r => r.data)];
  if (game.result) records.push(game.result);
  return new Response(records.join("\n") + "\n",
    { headers: { "Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store" } });
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (!path.startsWith("/api/")) return env.ASSETS.fetch(request);
    try {
      if (path === "/api/bots") return json({ bots: [] });
      if (path === "/api/scoreboard" && request.method === "GET") return await scoreboard(env.DB);
      const [, , route, id, suffix] = path.split("/");
      if (route === "games" && uuid.test(id)) {
        if (!suffix && request.method === "POST") return await save(env.DB, id, request);
        if (suffix === "replay" && request.method === "GET") return await replay(env.DB, id);
      }
      return json({ error: "Unknown endpoint" }, 404);
    } catch (error) {
      console.error("Game database request failed", error);
      return json({ error: "Game database unavailable" }, 503);
    }
  }
};
