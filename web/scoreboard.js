"use strict";
(() => {
  const totals = document.getElementById("game-totals");
  const number = new Intl.NumberFormat();
  const date = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const rate = (wins, games) => games ? Math.round(100 * wins / games) + "%" : "—";

  function row(values) {
    const tr = document.createElement("tr");
    for (const value of values) {
      const td = tr.insertCell();
      td.append(value instanceof Node ? value : String(value));
    }
    return tr;
  }

  function display(id, rows, cols, empty) {
    const body = document.getElementById(id);
    if (rows.length) body.replaceChildren(...rows);
    else {
      const tr = row([empty]);
      tr.cells[0].colSpan = cols;
      tr.cells[0].className = "empty-row";
      body.replaceChildren(tr);
    }
  }

  let worker;
  const positions = new Map();
  function positionFor(g) {
    const cell = document.createElement("small");
    cell.className = "position-summary";
    const cached = positions.get(g.id);
    if (cached?.updatedAt === g.updatedAt && !cached.failed) {
      cached.cell = cell;
      cell.textContent = cached.text;
      return cell;
    }
    const entry = { cell, updatedAt: g.updatedAt, text: "Loading position…" };
    positions.set(g.id, entry);
    cell.textContent = entry.text;
    if (!worker) {
      worker = new Worker("scoreboard-position.js");
      worker.onmessage = ({ data }) => {
        const current = positions.get(data.id);
        if (!current || current.updatedAt !== data.updatedAt) return;
        current.failed = !!data.error;
        current.text = data.error ? "Position unavailable" :
          "Cities " + data.cities.join("–") + " · Army " + data.army.map(n => number.format(n)).join("–");
        current.cell.textContent = current.text;
      };
      worker.onerror = () => {
        for (const p of positions.values()) {
          p.failed = true;
          p.cell.textContent = "Position unavailable";
        }
        worker.terminate();
        worker = null;
      };
    }
    worker.postMessage({ id: g.id, updatedAt: g.updatedAt, seat: g.seat });
    return cell;
  }

  function gameRow(g) {
    const updated = new Date(g.updatedAt);
    const time = document.createElement("time");
    time.dateTime = updated.toISOString();
    time.textContent = date.format(updated);
    time.title = updated.toLocaleString();
    const link = document.createElement("a");
    link.href = "play.html?replay=" + encodeURIComponent("/api/games/" + g.id + "/replay") + (g.outcome ? "" : "&position=latest");
    link.textContent = { win: "Win", loss: "Loss", draw: "Draw" }[g.outcome] || "View board";
    link.className = g.outcome || "unfinished";
    const match = (g.player || "Guest") + " vs " + g.opponent;
    link.setAttribute("aria-label", g.outcome ? link.textContent + " · watch " + match : "View latest position · " + match);
    const tr = row([g.player || "Guest", g.opponent, g.map, g.round, link, time]);
    if (!g.outcome) {
      tr.cells[4].prepend("Unfinished · ");
      tr.cells[4].append(positionFor(g));
    }
    return tr;
  }

  let loading = false;
  let previous = "";
  async function refresh() {
    if (loading || document.hidden || location.protocol === "file:") return;
    loading = true;
    try {
      const response = await fetch("/api/scoreboard", { cache: "no-store", signal: AbortSignal.timeout(15000) });
      if (!response.ok) throw new Error();
      const { summary: s, players, recent } = await response.json();
      previous = [number.format(s.started) + " started", number.format(s.finished) + " finished",
        number.format(s.started - s.finished) + " unfinished", number.format(s.wins) + " human wins" +
        (s.finished ? " (" + rate(s.wins, s.finished) + ")" : "")].join(" · ");
      if (s.avgRounds !== null) previous += " · " + s.avgRounds + " rounds on average in finished games";
      totals.textContent = previous;
      display("human-rows", players.map((p, i) => row([p.finished ? i + 1 : "—", p.name, p.games, p.games - p.finished,
        p.wins, p.draws, p.losses, rate(p.wins, p.finished)])), 8, "No named players have started a game yet.");
      const partials = new Set(recent.filter(g => !g.outcome).map(g => g.id));
      for (const id of positions.keys()) if (!partials.has(id)) positions.delete(id);
      document.getElementById("position-key").hidden = partials.size === 0;
      display("recent-games", recent.map(gameRow), 6, "No games have reached round 2 yet.");
    } catch {
      totals.textContent = previous ? previous + " · Update pending" : "Game stats are temporarily unavailable.";
    } finally { loading = false; }
  }
  if (location.protocol === "file:") totals.textContent = "Open the hosted site to see game stats.";
  refresh();
  setInterval(refresh, 30000);
  window.addEventListener("pageshow", refresh);
  window.addEventListener("bitbath-games-saved", refresh);
  document.addEventListener("visibilitychange", refresh);
})();
