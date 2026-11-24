/* global MICRO_STANDINGS */
"use strict";

(() => {
  if (new URLSearchParams(location.search).has("replay") || location.hash === "#arena") {
    location.replace("play.html" + location.search);
    return;
  }
  const $ = (id) => document.getElementById(id);
  const board = MICRO_STANDINGS;
  const models = board.rows.filter((r) => r.kind === "entry");
  const number = new Intl.NumberFormat("en-US");
  const pct = new Intl.NumberFormat("en-US", {style: "percent", maximumFractionDigits: 1});
  const titleCase = (text) => text.charAt(0).toUpperCase() + text.slice(1);

  function botName(id) {
    return id.replace(/^gpt-/i, "GPT‑").replace(/(\d)-(\d)(?=-|$)/g, "$1.$2")
      .split("-").map(titleCase).join(" ");
  }

  function badge(id) {
    const vendor = id.split("-")[0];
    const mark = document.createElement("span");
    mark.className = "bot-mark mark-" + vendor;
    mark.setAttribute("aria-hidden", "true");
    const paths = {
      gpt: '<g fill="none" stroke="currentColor" stroke-width="1.7"><path d="m12 2 7 4v8l-7 4-7-4V6Z"/><path d="m8.5 8 7-4 6 3.5v7L17 17m-4 5-7-4v-8l7-4 4 2.5v5l-5 3-4-2.5V9"/></g>',
      gemini: '<path fill="currentColor" d="M12 1.5C13 8 16 11 22.5 12c-6.5 1-9.5 4-10.5 10.5C11 16 8 13 1.5 12 8 11 11 8 12 1.5Z"/>',
      claude: '<g stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m12 2 .3 20M2 12l20-.6M5 5l14 14M19.4 5 5 19M8.5 3l7 18M3 8.5l18 7M15.6 3 8.5 21m12-13-17 8"/></g>',
    };
    mark.innerHTML = '<svg viewBox="0 0 24 24">' + (paths[vendor] || '<g fill="none" stroke="currentColor" stroke-width="1.6"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9Z"/><path d="m8 10 4 2.5 4-2.5m-4 2.5v4"/></g>') + "</svg>";
    return mark;
  }

  function showStandings(all) {
    const rows = all ? board.rows : models;
    $("models-only").setAttribute("aria-pressed", String(!all));
    $("all-bots").setAttribute("aria-pressed", String(all));
    const result = document.createDocumentFragment();
    for (const [i, bot] of rows.entries()) {
      const tr = document.createElement("tr");
      function cell(value, css) {
        const td = tr.insertCell();
        if (css) td.className = css;
        td.textContent = value;
        return td;
      }
      cell(String(i + 1).padStart(2, "0"), "place");
      const type = cell("", "bot-type");
      if (bot.kind === "entry") {
        tr.className = "llm-entry";
        const origin = document.createElement("span");
        origin.className = "origin-label";
        origin.textContent = "LLM";
        type.append(origin);
      }
      const name = document.createElement("span");
      name.className = "bot-name";
      name.append(badge(bot.bot), botName(bot.bot));
      cell("").append(name);
      const elo = cell(String(Math.round(bot.rating)), "rating");
      if (bot.interval) elo.title = "95% interval: " + bot.interval.map(Math.round).join("–");
      const share = bot.wins / (bot.wins + bot.draws + bot.losses);
      const rate = document.createElement("span");
      rate.className = "rate";
      const bar = document.createElement("span");
      bar.className = "rate-bar";
      bar.setAttribute("aria-hidden", "true");
      const fill = document.createElement("i");
      fill.style.width = (100 * share) + "%";
      bar.append(fill);
      rate.append(bar, pct.format(share));
      cell("").append(rate);
      result.append(tr);
    }
    $("standings-rows").replaceChildren(result);
  }

  $("season").textContent = board.season.replace(/^micro-(\d+)$/, "Season $1");
  $("match-count").textContent = number.format(board.games) + " games";
  $("models-only").textContent = "LLM bots (" + models.length + ")";
  $("all-bots").textContent = "All bots (" + board.rows.length + ")";
  $("rating-note").textContent = board.anchor
    ? "Elo ratings are anchored to " + botName(board.anchor.bot) + " at " + number.format(board.anchor.rating) + "."
    : "Elo ratings are centered at 1,500.";
  $("models-only").onclick = () => showStandings(false);
  $("all-bots").onclick = () => showStandings(true);
  showStandings(true);
})();
