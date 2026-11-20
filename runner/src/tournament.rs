//! Round-robin tournaments: every pair of bots plays every map twice, with
//! the sides swapped (SPEC §11). Each finished game is a line in the results
//! directory's `games.jsonl`, which `rating` turns into ratings.

use crate::play;
use bitbath_engine::{mapgen, Game, Rules};
use serde_json::{json, Value};
use std::collections::{HashSet, VecDeque};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Mutex};

pub struct Entrant {
    pub name: String,
    pub command: String,
}

pub struct Options {
    pub bots: Vec<Entrant>,
    /// Rule presets by name. Every pair plays every seed under each one.
    pub configs: Vec<(String, Rules)>,
    pub seeds: Vec<u64>,
    /// Holds `games.jsonl` and a directory per game under `games/`.
    pub out: PathBuf,
    /// Games played at once.
    pub jobs: usize,
    pub memory_mb: u64,
}

/// One finished game, as a line of `games.jsonl`.
#[derive(Clone)]
pub struct Record {
    pub config: String,
    pub seed: u64,
    /// The bots' names, player 1 first.
    pub players: [String; 2],
    /// 1 or 2, or 0 for a draw.
    pub winner: u8,
    pub reason: String,
    /// Each bot's diagnostics, as in the replay's result record.
    pub stats: [Value; 2],
    /// The whole line, which also has the final round and holdings.
    line: Value,
}

impl Record {
    fn new(job: &Job, result: &Value, replay: &str) -> Record {
        let mut line = json!({
            "config": job.config,
            "seed": job.seed,
            "p1": job.players[0].0,
            "p2": job.players[1].0,
        });
        let obj = line.as_object_mut().expect("object");
        for key in ["winner", "reason", "round", "cities", "units", "army", "stats"] {
            obj.insert(key.into(), result[key].clone());
        }
        obj.insert("replay".into(), json!(replay));
        Record::from_json(line).expect("a line this module wrote")
    }

    pub(crate) fn from_json(line: Value) -> Result<Record, String> {
        let text = |key: &str| {
            line[key]
                .as_str()
                .map(String::from)
                .ok_or_else(|| format!("no '{key}'"))
        };
        let number = |key: &str| line[key].as_u64().ok_or_else(|| format!("no '{key}'"));
        let winner = number("winner")?;
        if winner > 2 {
            return Err(format!("bad winner {winner}"));
        }
        Ok(Record {
            config: text("config")?,
            seed: number("seed")?,
            players: [text("p1")?, text("p2")?],
            winner: winner as u8,
            reason: text("reason")?,
            stats: [line["stats"][0].clone(), line["stats"][1].clone()],
            line,
        })
    }

    /// What identifies a game: a tournament plays each key once.
    fn key(&self) -> (String, u64, String, String) {
        (
            self.config.clone(),
            self.seed,
            self.players[0].clone(),
            self.players[1].clone(),
        )
    }
}

/// Reads a `games.jsonl` file.
pub fn read_games(path: &Path) -> Result<Vec<Record>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut games = Vec::new();
    for (n, raw) in text.lines().enumerate() {
        if raw.trim().is_empty() {
            continue;
        }
        let at = |e: String| format!("{}:{}: {e}", path.display(), n + 1);
        let line: Value = serde_json::from_str(raw).map_err(|e| at(e.to_string()))?;
        games.push(Record::from_json(line).map_err(at)?);
    }
    Ok(games)
}

/// Parses a seed list such as `1-10` or `3,7,20-22`.
pub fn parse_seeds(list: &str) -> Result<Vec<u64>, String> {
    let bad = || format!("bad seed list '{list}': expected something like 1-10 or 3,7,9");
    let mut seeds = Vec::new();
    for part in list.split(',') {
        let (lo, hi) = part.split_once('-').unwrap_or((part, part));
        let lo: u64 = lo.trim().parse().map_err(|_| bad())?;
        let hi: u64 = hi.trim().parse().map_err(|_| bad())?;
        if lo > hi || hi - lo >= 100_000 {
            return Err(bad());
        }
        for seed in lo..=hi {
            if !seeds.contains(&seed) {
                seeds.push(seed);
            }
        }
    }
    Ok(seeds)
}

struct Job {
    config: String,
    rules: Rules,
    seed: u64,
    /// Name and command, player 1 first.
    players: [(String, String); 2],
}

/// Plays the games of the tournament that `games.jsonl` doesn't have yet,
/// and returns every game in the file, old and new.
pub fn run(opts: &Options) -> Result<Vec<Record>, String> {
    std::fs::create_dir_all(&opts.out).map_err(|e| format!("{}: {e}", opts.out.display()))?;
    let path = opts.out.join("games.jsonl");
    let mut games = if path.is_file() { read_games(&path)? } else { Vec::new() };
    let played: HashSet<_> = games.iter().map(Record::key).collect();

    // Seeds on the outside, so a tournament that stops early has played
    // every pair about equally often.
    let mut queue = VecDeque::new();
    let mut scheduled = 0;
    for &seed in &opts.seeds {
        for (config, rules) in &opts.configs {
            for (i, a) in opts.bots.iter().enumerate() {
                for b in &opts.bots[i + 1..] {
                    for (p1, p2) in [(a, b), (b, a)] {
                        scheduled += 1;
                        let key = (config.clone(), seed, p1.name.clone(), p2.name.clone());
                        if !played.contains(&key) {
                            queue.push_back(Job {
                                config: config.clone(),
                                rules: rules.clone(),
                                seed,
                                players: [
                                    (p1.name.clone(), p1.command.clone()),
                                    (p2.name.clone(), p2.command.clone()),
                                ],
                            });
                        }
                    }
                }
            }
        }
    }
    let total = queue.len();
    println!(
        "{} bots, {} maps: {scheduled} games, {} already played. Results in {}",
        opts.bots.len(),
        opts.seeds.len() * opts.configs.len(),
        scheduled - total,
        opts.out.display()
    );
    if total == 0 {
        return Ok(games);
    }

    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| format!("{}: {e}", path.display()))?;
    crate::entry::keep_from_bots(&path)?;
    let queue = Mutex::new(queue);
    let stop = AtomicBool::new(false);
    let mut failure = None;
    std::thread::scope(|s| {
        let (tx, rx) = mpsc::channel();
        for _ in 0..opts.jobs.clamp(1, total) {
            let tx = tx.clone();
            let (queue, stop) = (&queue, &stop);
            s.spawn(move || {
                while !stop.load(Ordering::Relaxed) {
                    let Some(job) = queue.lock().expect("queue").pop_front() else {
                        break;
                    };
                    if tx.send(play_job(&job, opts)).is_err() {
                        break;
                    }
                }
            });
        }
        drop(tx);
        let mut done = 0;
        for result in rx {
            let saved = result.and_then(|record| {
                writeln!(file, "{}", record.line)
                    .and_then(|()| file.flush())
                    .map_err(|e| format!("{}: {e}", path.display()))?;
                Ok(record)
            });
            match saved {
                Ok(record) => {
                    done += 1;
                    println!(
                        "[{done:>w$}/{total}] {}",
                        describe(&record),
                        w = total.to_string().len()
                    );
                    games.push(record);
                }
                Err(e) => {
                    stop.store(true, Ordering::Relaxed);
                    failure.get_or_insert(e);
                }
            }
        }
    });
    match failure {
        Some(e) => Err(e),
        None => Ok(games),
    }
}

/// Plays one game in its own directory under `games/`.
fn play_job(job: &Job, opts: &Options) -> Result<Record, String> {
    let [(name1, command1), (name2, command2)] = &job.players;
    let name = format!("{}-seed{}-{name1}-vs-{name2}", job.config, job.seed);
    let dir = opts.out.join("games").join(&name);
    // A game that was cut short may have left its bots' files behind.
    if dir.exists() {
        std::fs::remove_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    }
    std::fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let map = mapgen::generate(&job.rules, job.seed).map_err(|e| format!("{} seed {}: {e}", job.config, job.seed))?;
    let finished = play::play(
        Game::new(job.rules.clone(), map),
        &play::Options {
            players: [
                play::Player {
                    name: name1.clone(),
                    command: command1.clone(),
                },
                play::Player {
                    name: name2.clone(),
                    command: command2.clone(),
                },
            ],
            map_source: format!("seed:{}", job.seed),
            seed: Some(job.seed),
            replay: dir.join("replay.bbr"),
            run_dir: dir,
            memory_mb: opts.memory_mb,
            quiet: true,
        },
    )?;
    Ok(Record::new(job, &finished.result, &format!("games/{name}/replay.bbr")))
}

fn describe(r: &Record) -> String {
    let [p1, p2] = &r.players;
    let round = r.line["round"].as_u64().unwrap_or(0);
    let what = match r.winner {
        1 => format!("{p1} beat {p2}"),
        2 => format!("{p2} beat {p1}"),
        _ => format!("{p1} and {p2} drew"),
    };
    let how = match r.reason.as_str() {
        "draw" => String::new(),
        reason => format!(" by {reason}"),
    };
    format!("{} seed {}: {what}{how} in round {round}", r.config, r.seed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn seed_lists() {
        assert_eq!(parse_seeds("1-3").unwrap(), [1, 2, 3]);
        assert_eq!(parse_seeds("7, 2-3,3").unwrap(), [7, 2, 3]);
        for bad in ["", "a", "3-1", "1-", "1,,2", "0-999999999"] {
            assert!(parse_seeds(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn records_survive_a_round_trip() {
        let job = Job {
            config: "micro".into(),
            rules: Rules::default(),
            seed: 7,
            players: [("a".into(), "x".into()), ("b".into(), "y".into())],
        };
        let result = json!({"type": "result", "winner": 2, "reason": "cities", "round": 31,
            "cities": [1, 3], "units": [0, 4], "army": [0, 40], "stats": [{"turns": 31}, {"turns": 30}], "stderr": ["", ""]});
        let record = Record::new(&job, &result, "games/x/replay.bbr");
        let again = Record::from_json(serde_json::from_str(&record.line.to_string()).unwrap()).unwrap();
        assert_eq!(again.key(), ("micro".to_string(), 7, "a".to_string(), "b".to_string()));
        assert_eq!((again.winner, again.reason.as_str()), (2, "cities"));
        assert_eq!(again.stats[1]["turns"], 30);
        assert!(again.line.get("stderr").is_none());
        assert!(Record::from_json(json!({"config": "micro"})).is_err());
    }
}
