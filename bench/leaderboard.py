"""Export the newest Micro season's public standings for the static homepage."""

import json
import os
import tempfile
from pathlib import Path


def matches_micro_games(path, expected):
    """Check the ratings against the games, including games inherited on resume."""
    count = 0
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            if json.loads(line)["config"] != "micro":
                return False
            count += 1
    return count == expected


def newest_season(root):
    """The newest Micro season with entries and ratings of all its games, as
    (its directory, its arena.json, its ratings.json), or None."""
    seasons = []
    for manifest in (root / "arena").glob("*/arena.json"):
        state = json.loads(manifest.read_text(encoding="utf-8"))
        maps = state.get("settings", {}).get("maps", [])
        if maps and all(Path(m["config"]).name == "micro.toml" for m in maps):
            seasons.append((state["updated"], manifest.parent, state))

    for updated, season, state in sorted(seasons, key=lambda item: item[:2], reverse=True):
        ratings, games = season / "ratings.json", season / "games.jsonl"
        if not ratings.is_file() or not games.is_file():
            continue
        scores = json.loads(ratings.read_text(encoding="utf-8"))
        has_entries = any(state["bots"][r["bot"]]["kind"] == "entry" for r in scores["ratings"])
        if has_entries and scores["games"] and matches_micro_games(games, scores["games"]):
            return season, state, scores
    return None


def refresh(root):
    found = newest_season(root)
    if not found:
        # A fresh clone has the checked-in standings but no private arena data.
        return
    season, state, scores = found
    updated = state["updated"]

    rows = []
    for row in scores["ratings"]:
        entry = {k: row[k] for k in ("bot", "rating", "interval", "wins", "draws", "losses")}
        entry["kind"] = state["bots"][row["bot"]]["kind"]
        rows.append(entry)
    public = {"season": season.name, "date": updated[:10], "games": scores["games"], "anchor": scores["anchor"], "rows": rows}
    data = "// Generated from Micro arena results by bench/leaderboard.py.\n"
    data += "const MICRO_STANDINGS = " + json.dumps(public, indent=2, allow_nan=False) + ";\n"
    dest = root / "web" / "standings.js"
    with tempfile.NamedTemporaryFile("w", dir=dest.parent, delete=False, encoding="utf-8", newline="\n") as tmp:
        tmp.write(data)
    os.replace(tmp.name, dest)


if __name__ == "__main__":
    refresh(Path(__file__).resolve().parents[1])
