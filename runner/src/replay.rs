//! Replay files (SPEC §8): JSON Lines with a header, one record per turn,
//! and a result. Replays store orders, not states; reading one re-simulates
//! the game with the engine.

use bitbath_engine::Game;
use serde_json::Value;
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;

pub struct ReplayWriter {
    out: BufWriter<File>,
}

impl ReplayWriter {
    pub fn create(path: &Path) -> Result<ReplayWriter, String> {
        if let Some(dir) = path.parent().filter(|d| !d.as_os_str().is_empty()) {
            std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        }
        let file = File::create(path).map_err(|e| format!("{}: {e}", path.display()))?;
        crate::entry::keep_from_bots(path)?;
        Ok(ReplayWriter {
            out: BufWriter::new(file),
        })
    }

    /// Writes one record and flushes, so a partial replay survives a crash.
    pub fn write(&mut self, record: &Value) -> Result<(), String> {
        serde_json::to_writer(&mut self.out, record)
            .map_err(|e| e.to_string())
            .and_then(|_| {
                self.out
                    .write_all(b"\n")
                    .and_then(|_| self.out.flush())
                    .map_err(|e| e.to_string())
            })
    }
}

pub struct Replay {
    pub header: Value,
    pub records: Vec<Value>,
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .map(|a| a.iter().filter_map(|s| s.as_str().map(String::from)).collect())
        .unwrap_or_default()
}

impl Replay {
    pub fn read(path: &Path) -> Result<Replay, String> {
        let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
        let mut records = Vec::new();
        for (n, line) in text.lines().enumerate().filter(|(_, l)| !l.trim().is_empty()) {
            records
                .push(serde_json::from_str::<Value>(line).map_err(|e| format!("{}:{}: {e}", path.display(), n + 1))?);
        }
        if records.is_empty() || records[0]["type"] != "header" {
            return Err(format!("{}: not a bitbath replay", path.display()));
        }
        let header = records.remove(0);
        Ok(Replay { header, records })
    }

    pub fn game(&self) -> Result<Game, String> {
        let setup = strings(&self.header["setup"]);
        Game::from_setup(setup.iter().map(|s| s.as_str()))
    }

    pub fn turns(&self) -> impl Iterator<Item = &Value> {
        self.records.iter().filter(|r| r["type"] == "turn")
    }

    pub fn result(&self) -> Option<&Value> {
        self.records.iter().find(|r| r["type"] == "result")
    }

    pub fn name(&self, player: u8) -> String {
        self.header["players"][player as usize - 1]["name"]
            .as_str()
            .unwrap_or("?")
            .to_string()
    }

    /// Re-simulates every recorded order, calling `visit` with the game, the
    /// turn record, and each order with its reply. Checks the re-simulation
    /// against the recording: each turn's rejected orders (or, in replays
    /// recorded before turns counted them, each player's total), then the
    /// outcome. Forfeits come from the result, since the runner decides them.
    pub fn resimulate(&self, mut visit: impl FnMut(Visit)) -> Result<Game, String> {
        let mut game = self.game()?;
        let mut rejected_by = [0u64; 3];
        for turn in self.turns() {
            if game.outcome.is_some() {
                return Err("the replay continues after the game ended".into());
            }
            let player = turn["player"].as_u64().ok_or("turn without a player")? as u8;
            if player != 1 && player != 2 {
                return Err(format!("bad player {player}"));
            }
            game.begin_turn(player);
            if Some(game.round as u64) != turn["round"].as_u64() {
                return Err(format!("expected round {}, found {}", game.round, turn["round"]));
            }
            visit(Visit::TurnStart { game: &game });
            let mut rejected = 0;
            for order in strings(&turn["orders"]) {
                let before = AttackInfo::of(&game, &order);
                let reply = game.apply(&order);
                if reply.last().is_some_and(|l| l.starts_with("ERR")) {
                    rejected += 1;
                }
                visit(Visit::Order {
                    game: &game,
                    order: &order,
                    reply: &reply,
                    attack: before,
                });
            }
            if let Some(recorded) = turn["rejected"].as_u64().filter(|&r| r != rejected) {
                return Err(mismatch(format!(
                    "in round {}, the engine rejects {rejected} of player {player}'s orders, but {recorded} were rejected in the game",
                    game.round
                )));
            }
            rejected_by[player as usize] += rejected;
            game.end_turn();
            visit(Visit::TurnEnd { turn });
        }
        if let Some(result) = self.result() {
            // The recorded count also includes orders refused for limits,
            // which aren't in the replay, so only a higher count is wrong.
            for p in 1..=2 {
                if let Some(invalid) = result["stats"][p - 1]["invalid"].as_u64() {
                    if rejected_by[p] > invalid {
                        return Err(mismatch(format!(
                            "the engine rejects {} of player {p}'s orders, but {invalid} were rejected in the game",
                            rejected_by[p]
                        )));
                    }
                }
            }
            let winner = result["winner"].as_u64().unwrap_or(0) as u8;
            let reason = result["reason"].as_str().unwrap_or("");
            match &game.outcome {
                // The engine decides these, so re-simulation must reach them.
                None if ["elimination", "cities", "army", "draw"].contains(&reason) => {
                    return Err(mismatch(format!(
                        "the game ended by {reason}, but the re-simulated game doesn't end"
                    )))
                }
                None => game.finish(winner, reason),
                Some(o) if o.winner == winner && o.reason == reason => {}
                Some(o) => {
                    return Err(mismatch(format!(
                        "the re-simulated game ends with {} ({}), but the replay records {winner} ({reason})",
                        o.winner, o.reason
                    )))
                }
            }
        }
        Ok(game)
    }
}

/// Explains a re-simulation that disagrees with its replay.
fn mismatch(what: String) -> String {
    format!(
        "this engine doesn't play the replay the way it was recorded: {what}. \
         The replay was probably recorded by a different version of bitbath."
    )
}

pub enum Visit<'a> {
    TurnStart {
        game: &'a Game,
    },
    Order {
        game: &'a Game,
        order: &'a str,
        reply: &'a [String],
        attack: Option<AttackInfo>,
    },
    TurnEnd {
        turn: &'a Value,
    },
}

/// Unit types in an attack, captured before the order runs, since a dead
/// unit is gone afterwards.
pub struct AttackInfo {
    attacker: char,
    target: char,
}

impl AttackInfo {
    fn of(game: &Game, order: &str) -> Option<AttackInfo> {
        let t: Vec<&str> = order.split_whitespace().collect();
        if t.len() != 3 || t[0] != "A" {
            return None;
        }
        let code = |tok: &str| {
            tok.parse()
                .ok()
                .and_then(|id| game.unit(id))
                .map(|u| game.rules.units[u.ty].code)
        };
        Some(AttackInfo {
            attacker: code(t[1])?,
            target: code(t[2])?,
        })
    }
}

/// A turn-by-turn summary for humans and LLMs (`bitbath replay --text`).
pub fn summary(replay: &Replay, all_orders: bool) -> Result<String, String> {
    let mut out = String::new();
    let h = &replay.header;
    let game = replay.game()?;
    let _ = writeln!(
        out,
        "Bitbath replay (engine {}, protocol {})",
        h["bitbath"].as_str().unwrap_or("?"),
        h["protocol"]
    );
    let _ = writeln!(
        out,
        "Map {}: {}x{}, {} cities",
        h["map"].as_str().unwrap_or("?"),
        game.map.width,
        game.map.height,
        game.cities.len()
    );
    for p in 1..=2u8 {
        let cmd = h["players"][p as usize - 1]["command"].as_str().unwrap_or("");
        let _ = writeln!(out, "Player {p}: {} ({cmd})", replay.name(p));
    }

    let mut events: Vec<String> = Vec::new();
    let mut built: BTreeMap<char, u32> = BTreeMap::new();
    let mut rejected = 0;
    let mut header = String::new();
    let game = replay.resimulate(|visit| match visit {
        Visit::TurnStart { game, .. } => {
            let p = game.player;
            if p == 1 {
                let _ = writeln!(out, "\nRound {}", game.round);
            }
            events.clear();
            built.clear();
            rejected = 0;
            header = format!(
                "  P{p} {:<10} gold {:<5} cities {:<3} units {:<4}",
                replay.name(p),
                game.gold[p as usize],
                game.cities_of(p),
                game.units_of(p)
            );
        }
        Visit::Order {
            game,
            order,
            reply,
            attack,
        } => {
            let result = reply.last().map(String::as_str).unwrap_or("");
            if result.starts_with("ERR") {
                rejected += 1;
            }
            if all_orders {
                events.push(format!("{order} -> {}", reply.join(" | ")));
                return;
            }
            let t: Vec<&str> = order.split_whitespace().collect();
            let r: Vec<&str> = result.split_whitespace().collect();
            match (t.first().copied(), r.first().copied()) {
                (Some("A"), Some("OK")) if r.len() == 3 => {
                    let (Some(a), true) = (attack, t.len() == 3) else {
                        return;
                    };
                    if r[1] == "0" {
                        events.push(format!(
                            "unit {} ({}) kills unit {} ({})",
                            t[1], a.attacker, t[2], a.target
                        ));
                    } else if r[2] == "0" {
                        events.push(format!(
                            "unit {} ({}) dies attacking unit {} ({})",
                            t[1], a.attacker, t[2], a.target
                        ));
                    }
                }
                (Some("C"), Some("OK")) if result == "OK captured" => {
                    let city = t
                        .get(1)
                        .and_then(|id| id.parse().ok())
                        .and_then(|id| game.unit(id))
                        .and_then(|u| game.cities.iter().find(|c| c.hex == u.hex).map(|c| c.id));
                    if let Some(city) = city {
                        events.push(format!("unit {} captures city {city}", t[1]));
                    }
                }
                (Some("B"), Some("OK")) => {
                    if let Some(code) = t.get(2).and_then(|c| c.chars().next()) {
                        *built.entry(code).or_default() += 1;
                    }
                }
                (_, Some("PART")) if r.len() == 4 => {
                    events.push(format!(
                        "unit {} stops at ({},{}): {}",
                        t.get(1).unwrap_or(&"?"),
                        r[1],
                        r[2],
                        r[3]
                    ));
                }
                _ => {}
            }
        }
        Visit::TurnEnd { turn } => {
            let orders = turn["orders"].as_array().map_or(0, |a| a.len());
            let mut line = format!("{header} {orders} orders, {rejected} rejected, {} ms", turn["used_ms"]);
            if turn["skipped"] == true {
                line.push_str(", MISSED TURN");
            } else if turn["timeout"] == true {
                line.push_str(", TIMED OUT");
            }
            if let Some(late) = turn["late"].as_array() {
                let _ = write!(line, ", {} late", late.len());
            }
            let _ = writeln!(out, "{line}");
            if !built.is_empty() {
                let list: Vec<String> = built.iter().map(|(code, n)| format!("{n} {code}")).collect();
                let _ = writeln!(out, "      builds {}", list.join(", "));
            }
            for e in &events {
                let _ = writeln!(out, "      {e}");
            }
            if let Some(note) = turn["note"].as_str() {
                let _ = writeln!(out, "      {note}");
            }
        }
    })?;

    let o = game.outcome.as_ref().ok_or("the replay has no result")?;
    let _ = writeln!(out);
    if o.winner == 0 {
        let _ = writeln!(out, "Result: draw ({}) after round {}.", o.reason, game.round);
    } else {
        let _ = writeln!(
            out,
            "Result: player {} ({}) wins by {} in round {}.",
            o.winner,
            replay.name(o.winner),
            o.reason,
            game.round
        );
    }
    if let Some(stats) = replay.result().map(|r| &r["stats"]) {
        for p in 1..=2u8 {
            let s = &stats[p as usize - 1];
            let turns = s["turns"].as_u64().unwrap_or(0).max(1);
            let _ = writeln!(
                out,
                "  P{p} {}: {} orders, {} rejected, {} late, {} timeouts, {} missed turns, {} ms average think time",
                replay.name(p),
                s["orders"],
                s["invalid"],
                s["late"],
                s["timeouts"],
                s["missed_turns"],
                s["think_ms"].as_u64().unwrap_or(0) / turns
            );
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// A one-turn replay in which player 1 builds twice from the same city,
    /// so the engine accepts the first order and rejects the second.
    fn replay(rejected: Option<u64>, result: Option<Value>) -> Replay {
        let setup = [
            "MAP 6 4",
            "ROW C.....",
            "ROW ......",
            "ROW ......",
            "ROW .....C",
            "CITY 0 0 0 1",
            "CITY 1 4 3 2",
        ];
        let mut turn = json!({"type": "turn", "round": 1, "player": 1, "orders": ["B 0 I", "B 0 I"]});
        if let Some(r) = rejected {
            turn["rejected"] = json!(r);
        }
        Replay {
            header: json!({"type": "header", "setup": setup}),
            records: [Some(turn), result].into_iter().flatten().collect(),
        }
    }

    fn forfeit(invalid: u64) -> Value {
        let stats = json!({"invalid": invalid});
        json!({"type": "result", "winner": 2, "reason": "crash", "stats": [stats, stats]})
    }

    #[test]
    fn rejected_counts_must_match() {
        assert!(replay(Some(1), None).resimulate(|_| {}).is_ok());
        let err = replay(Some(0), None).resimulate(|_| {}).unwrap_err();
        assert!(
            err.contains("in round 1, the engine rejects 1 of player 1's orders, but 0"),
            "{err}"
        );
    }

    #[test]
    fn older_replays_check_each_players_total() {
        assert!(replay(None, Some(forfeit(1))).resimulate(|_| {}).is_ok());
        let err = replay(None, Some(forfeit(0))).resimulate(|_| {}).unwrap_err();
        assert!(err.contains("rejects 1 of player 1's orders, but 0"), "{err}");
    }

    #[test]
    fn engine_outcomes_must_be_reached() {
        let won = json!({"type": "result", "winner": 1, "reason": "elimination"});
        let err = replay(Some(1), Some(won)).resimulate(|_| {}).unwrap_err();
        assert!(err.contains("doesn't end"), "{err}");
        let game = replay(Some(1), Some(forfeit(1))).resimulate(|_| {}).unwrap();
        assert_eq!(game.outcome.map(|o| o.reason), Some("crash".to_string()));
    }
}
