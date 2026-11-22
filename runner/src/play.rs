//! Runs one game between two bot processes (SPEC §6, §7, §8).

use crate::bot::{Bot, Event};
use crate::replay::ReplayWriter;
use bitbath_engine::mapgen::GENERATOR_VERSION;
use bitbath_engine::rules::Constants;
use bitbath_engine::{Game, Outcome, ENGINE_VERSION, PROTOCOL_VERSION};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// Debug annotations kept per turn (SPEC §8).
const MAX_DEBUG_PER_TURN: usize = 1000;
/// Bytes of stderr kept per turn.
const MAX_STDERR_PER_TURN: usize = 8 * 1024;

pub struct Player {
    pub name: String,
    pub command: String,
}

pub struct Options {
    pub players: [Player; 2],
    pub map_source: String,
    pub seed: Option<u64>,
    pub replay: PathBuf,
    /// Each bot gets its own working directory under this one.
    pub run_dir: PathBuf,
    /// Memory cap per bot process tree, or 0 for none.
    pub memory_mb: u64,
    pub quiet: bool,
}

#[derive(Default)]
struct Stats {
    turns: u64,
    orders: u64,
    invalid: u64,
    late: u64,
    timeouts: u64,
    missed_turns: u64,
    think_ms: u64,
    max_think_ms: u64,
    debug_dropped: u64,
}

impl Stats {
    fn to_json(&self) -> Value {
        json!({
            "turns": self.turns,
            "orders": self.orders,
            "invalid": self.invalid,
            "late": self.late,
            "timeouts": self.timeouts,
            "missed_turns": self.missed_turns,
            "think_ms": self.think_ms,
            "max_think_ms": self.max_think_ms,
            "debug_dropped": self.debug_dropped,
        })
    }
}

struct Seat {
    bot: Option<Bot>,
    bank_ms: u64,
    /// The round whose `END` hasn't arrived yet.
    pending_end: Option<u32>,
    /// Turns in a row that ended without an `END`.
    silent: u32,
    stats: Stats,
}

/// What happened during one turn, as written to the replay.
struct TurnRecord {
    round: u32,
    player: u8,
    time_ms: u64,
    bank_ms: u64,
    used_ms: u64,
    orders: Vec<String>,
    /// How many of `orders` the engine rejected. Re-simulation checks this,
    /// so an engine that plays the orders differently is caught at once.
    rejected: u64,
    late: Vec<String>,
    debug: Vec<String>,
    stderr: String,
    timeout: bool,
    skipped: bool,
    note: String,
}

impl TurnRecord {
    fn to_json(&self) -> Value {
        let mut v = json!({
            "type": "turn",
            "round": self.round,
            "player": self.player,
            "time_ms": self.time_ms,
            "bank_ms": self.bank_ms,
            "used_ms": self.used_ms,
            "orders": self.orders,
            "rejected": self.rejected,
        });
        let obj = v.as_object_mut().expect("object");
        if !self.late.is_empty() {
            obj.insert("late".into(), json!(self.late));
        }
        if !self.debug.is_empty() {
            obj.insert("debug".into(), json!(self.debug));
        }
        if !self.stderr.is_empty() {
            obj.insert("stderr".into(), json!(self.stderr));
        }
        if self.timeout {
            obj.insert("timeout".into(), json!(true));
        }
        if self.skipped {
            obj.insert("skipped".into(), json!(true));
        }
        if !self.note.is_empty() {
            obj.insert("note".into(), json!(self.note));
        }
        v
    }
}

fn unix_time() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// A finished game: its outcome, and the replay's result record, which adds
/// the final round, each side's holdings, and each bot's diagnostics.
pub struct Finished {
    pub outcome: Outcome,
    pub result: Value,
}

/// Plays a game to the end and writes its replay.
pub fn play(mut game: Game, opts: &Options) -> Result<Finished, String> {
    let c = game.rules.c.clone();
    let mut replay = ReplayWriter::create(&opts.replay)?;
    replay.write(&json!({
        "type": "header",
        "bitbath": ENGINE_VERSION,
        "protocol": PROTOCOL_VERSION,
        "generator": GENERATOR_VERSION,
        "map": opts.map_source,
        "seed": opts.seed,
        "setup": game.setup_lines(true),
        "players": opts.players.iter().map(|p| json!({"name": p.name, "command": p.command})).collect::<Vec<_>>(),
        "started": unix_time(),
    }))?;

    let mut seats = Vec::new();
    for (i, p) in opts.players.iter().enumerate() {
        let dir = opts.run_dir.join(format!("p{}", i + 1));
        std::fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        let bot = Bot::spawn(
            &p.command,
            &dir,
            opts.memory_mb,
            c.max_line_bytes as usize,
            MAX_STDERR_PER_TURN,
        )
        .map_err(|e| eprintln!("player {} ({}): {e}", i + 1, p.name))
        .ok();
        seats.push(Seat {
            bot,
            bank_ms: c.time_bank_ms as u64,
            pending_end: None,
            silent: 0,
            stats: Stats::default(),
        });
    }

    init(&mut game, &mut seats, &c, &mut replay)?;

    while game.outcome.is_none() {
        for player in [1u8, 2] {
            game.begin_turn(player);
            let seat = &mut seats[player as usize - 1];
            let (record, forfeit) = run_turn(&mut game, seat, &c);
            replay.write(&record.to_json())?;
            if let Some(reason) = forfeit {
                if !opts.quiet {
                    let note = if record.note.is_empty() {
                        String::new()
                    } else {
                        format!(": {}", record.note)
                    };
                    eprintln!(
                        "player {player} ({}) forfeits ({reason}){note}",
                        opts.players[player as usize - 1].name
                    );
                }
                game.forfeit(player, reason);
            }
            game.end_turn();
            if game.outcome.is_some() {
                break;
            }
        }
        if !opts.quiet && (game.round.is_multiple_of(10) || game.outcome.is_some()) {
            print_status(&game, opts);
        }
    }

    let outcome = game.outcome.clone().expect("the game is over");
    let deadline = Instant::now() + Duration::from_secs(1);
    for seat in seats.iter_mut() {
        if let Some(bot) = seat.bot.as_mut() {
            bot.send(&[format!("GAMEOVER {} {}", outcome.winner, outcome.reason)]);
            bot.close_input();
        }
    }
    let mut final_stderr = Vec::new();
    for seat in seats.iter_mut() {
        final_stderr.push(match seat.bot.as_mut() {
            Some(bot) => {
                bot.shut_down(deadline.saturating_duration_since(Instant::now()));
                bot.take_stderr()
            }
            None => String::new(),
        });
    }
    let result = json!({
        "type": "result",
        "winner": outcome.winner,
        "reason": outcome.reason,
        "round": game.round,
        "cities": [game.cities_of(1), game.cities_of(2)],
        "units": [game.units_of(1), game.units_of(2)],
        "army": [game.army_value(1), game.army_value(2)],
        "stats": [seats[0].stats.to_json(), seats[1].stats.to_json()],
        "stderr": final_stderr,
    });
    replay.write(&result)?;
    Ok(Finished { outcome, result })
}

/// Sends both init messages at once and waits for `READY` (SPEC §6.2).
fn init(game: &mut Game, seats: &mut [Seat], c: &Constants, replay: &mut ReplayWriter) -> Result<(), String> {
    let deadline = Instant::now() + Duration::from_millis(c.init_ms as u64);
    for (i, seat) in seats.iter().enumerate() {
        if let Some(bot) = &seat.bot {
            bot.send(&game.init_message(i as u8 + 1));
        }
    }
    let mut failed: [Option<&'static str>; 2] = [None, None];
    let mut notes = [String::new(), String::new()];
    for (i, seat) in seats.iter_mut().enumerate() {
        let Some(bot) = seat.bot.as_mut() else {
            failed[i] = Some("crash");
            notes[i] = "didn't start".into();
            continue;
        };
        loop {
            match bot.recv(deadline) {
                Some(Event::Line(line)) if line.trim() == "READY" => break,
                Some(Event::Line(_)) | Some(Event::TooLong(_)) => {}
                Some(Event::Eof) => {
                    let (reason, what) = bot.exit_reason();
                    failed[i] = Some(reason);
                    notes[i] = what;
                    break;
                }
                None => {
                    failed[i] = Some("init");
                    notes[i] = format!("sent no READY within {} ms", c.init_ms);
                    break;
                }
            }
        }
    }
    let stderr: Vec<String> = seats
        .iter()
        .map(|s| s.bot.as_ref().map(|b| b.take_stderr()).unwrap_or_default())
        .collect();
    replay.write(&json!({"type": "init", "notes": notes, "stderr": stderr}))?;
    for (i, note) in notes.iter().enumerate() {
        if !note.is_empty() {
            eprintln!("player {}: {note}", i + 1);
        }
    }
    match failed {
        [Some(_), Some(_)] => game.finish(0, "init"),
        [Some(reason), None] => game.forfeit(1, reason),
        [None, Some(reason)] => game.forfeit(2, reason),
        [None, None] => {}
    }
    Ok(())
}

/// Plays the current player's turn. Returns the record and, if the bot
/// forfeits, the reason.
fn run_turn(game: &mut Game, seat: &mut Seat, c: &Constants) -> (TurnRecord, Option<&'static str>) {
    let start = Instant::now();
    let increment = c.time_increment_ms as u64;
    let available = seat.bank_ms + increment;
    let deadline = start + Duration::from_millis(available);
    let mut rec = TurnRecord {
        round: game.round,
        player: game.player,
        time_ms: available,
        bank_ms: seat.bank_ms,
        used_ms: 0,
        orders: Vec::new(),
        rejected: 0,
        late: Vec::new(),
        debug: Vec::new(),
        stderr: String::new(),
        timeout: false,
        skipped: false,
        note: String::new(),
    };
    seat.stats.turns += 1;
    let Some(bot) = seat.bot.as_mut() else {
        return (rec, Some("crash"));
    };

    // Lines sent outside the bot's turn are late. If the END for its previous
    // turn is still missing, wait for it on this turn's clock (SPEC §7).
    loop {
        let event = if seat.pending_end.is_some() {
            bot.recv(deadline)
        } else {
            bot.try_recv()
        };
        match event {
            None => break,
            Some(Event::Eof) => {
                let (reason, what) = bot.exit_reason();
                rec.note = what;
                rec.stderr = bot.take_stderr();
                return (rec, Some(reason));
            }
            Some(Event::Line(line)) => {
                let mut tokens = line.split_whitespace();
                match tokens.next() {
                    None | Some("D") => {}
                    Some("END") => {
                        let round = tokens.next().and_then(|t| t.parse::<u32>().ok());
                        if seat.pending_end.is_some() && (round.is_none() || round == seat.pending_end) {
                            seat.pending_end = None;
                            seat.silent = 0;
                        }
                    }
                    Some(_) => {
                        bot.send(&["ERR late your turn had already ended"]);
                        seat.stats.late += 1;
                        rec.late.push(line);
                    }
                }
            }
            Some(Event::TooLong(prefix)) => {
                if !prefix.starts_with('D') {
                    bot.send(&["ERR late your turn had already ended"]);
                    seat.stats.late += 1;
                }
            }
        }
    }
    if seat.pending_end.is_some() {
        // The whole turn passed without the missing END, so this turn is lost.
        seat.silent += 1;
        seat.stats.missed_turns += 1;
        seat.bank_ms = 0;
        rec.skipped = true;
        rec.used_ms = available;
        rec.stderr = bot.take_stderr();
        let forfeit = (seat.silent >= c.hang_turns).then_some("hang");
        return (rec, forfeit);
    }

    let waited = start.elapsed().as_millis() as u64;
    bot.send(&game.turn_message(available.saturating_sub(waited), seat.bank_ms));

    let mut orders = 0u32;
    let mut ended = false;
    let mut crashed = None;
    while game.outcome.is_none() {
        match bot.recv(deadline) {
            None => break,
            Some(Event::Eof) => {
                crashed = Some(bot.exit_reason());
                break;
            }
            Some(Event::Line(line)) => {
                let line = line.trim();
                match line.split_whitespace().next() {
                    None => {}
                    Some("END") => {
                        ended = true;
                        break;
                    }
                    Some("D") => {
                        if rec.debug.len() < MAX_DEBUG_PER_TURN {
                            rec.debug.push(line.to_string());
                        } else {
                            seat.stats.debug_dropped += 1;
                        }
                    }
                    Some(_) if orders >= c.max_orders_per_turn => {
                        bot.send(&[format!("ERR limit at most {} orders per turn", c.max_orders_per_turn)]);
                        seat.stats.invalid += 1;
                    }
                    Some(_) => {
                        orders += 1;
                        seat.stats.orders += 1;
                        let reply = game.apply(line);
                        if reply.last().is_some_and(|l| l.starts_with("ERR")) {
                            seat.stats.invalid += 1;
                            rec.rejected += 1;
                        }
                        bot.send(&reply);
                        rec.orders.push(line.to_string());
                    }
                }
            }
            Some(Event::TooLong(prefix)) => {
                if prefix.starts_with('D') {
                    seat.stats.debug_dropped += 1;
                } else {
                    bot.send(&[format!("ERR limit lines may be at most {} bytes", c.max_line_bytes)]);
                    seat.stats.invalid += 1;
                }
            }
        }
    }

    let used = (start.elapsed().as_millis() as u64).min(available);
    rec.used_ms = used;
    seat.stats.think_ms += used;
    seat.stats.max_think_ms = seat.stats.max_think_ms.max(used);
    if used <= increment {
        let saved = (increment - used) * c.time_keep_pct as u64 / 100;
        seat.bank_ms = (seat.bank_ms + saved).min(c.time_bank_cap_ms as u64);
    } else {
        seat.bank_ms = seat.bank_ms.saturating_sub(used - increment);
    }

    let mut forfeit = None;
    if let Some((reason, what)) = crashed {
        rec.note = what;
        forfeit = Some(reason);
    } else if !ended && game.outcome.is_none() {
        seat.pending_end = Some(game.round);
        seat.silent += 1;
        seat.stats.timeouts += 1;
        rec.timeout = true;
        if seat.silent >= c.hang_turns {
            forfeit = Some("hang");
        }
    } else {
        seat.silent = 0;
    }
    rec.stderr = bot.take_stderr();
    (rec, forfeit)
}

fn print_status(game: &Game, opts: &Options) {
    let side = |p: u8| {
        let name = &opts.players[p as usize - 1].name;
        if game.rules.supply() {
            format!(
                "{name}: {} cities, {} sites, {} units, {} bread",
                game.cities_of(p),
                game.sites_of(p),
                game.units_of(p),
                game.bread_of(p)
            )
        } else {
            format!(
                "{name}: {} cities, {} units, {} gold",
                game.cities_of(p),
                game.units_of(p),
                game.gold[p as usize]
            )
        }
    };
    println!("round {:>3} | {} | {}", game.round, side(1), side(2));
}
