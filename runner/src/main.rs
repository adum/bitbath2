//! The `bitbath` command: plays bot-vs-bot games, reads replays, and shows
//! maps (SPEC §10).

mod bot;
mod play;
mod replay;

use bitbath_engine::{mapgen, parse_setup, Game, Rules};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const USAGE: &str = "\
Usage:
  bitbath play --p1 <command> --p2 <command> [options]
      --name1 <name>, --name2 <name>   display names (default: from the commands)
      --map seed:<n> | <file>          generated map, or a map file (default: random seed)
      --config <file>                  rule overrides, one 'key = value' per line
      --set <key>=<value>              one rule override; repeatable
      --replay <file>                  where to write the replay (default: in runs/)
      --memory-mb <n>                  memory cap per bot, 0 for none (default: 2048)
      --quiet                          print only the result
  bitbath replay <file> [--text] [--orders]
      Re-simulates a replay and checks its result. --text prints a turn-by-turn
      summary; --orders adds every order and its reply.
  bitbath map [--map seed:<n> | <file>] [--config <file>] [--set <key>=<value>] [--lines]
      Prints a map. --lines prints it in the map file format instead.

Example:
  bitbath play --p1 \"python bots/greedy_bot.py\" --p2 \"python bots/random_bot.py\" --map seed:42
";

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match args.first().map(String::as_str) {
        Some("play") => cmd_play(&args[1..]),
        Some("replay") => cmd_replay(&args[1..]),
        Some("map") => cmd_map(&args[1..]),
        None | Some("help" | "--help" | "-h") => {
            print!("{USAGE}");
            Ok(())
        }
        Some(other) => Err(format!("unknown command '{other}'\n\n{USAGE}")),
    };
    if let Err(e) = result {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}

/// Parsed command-line flags. Flags with values may repeat; `get` returns
/// the last one.
struct Args {
    flags: Vec<(String, String)>,
    positional: Vec<String>,
}

impl Args {
    fn parse(args: &[String], with_value: &[&str], switches: &[&str]) -> Result<Args, String> {
        let mut out = Args {
            flags: Vec::new(),
            positional: Vec::new(),
        };
        let mut it = args.iter();
        while let Some(a) = it.next() {
            if with_value.contains(&a.as_str()) {
                let v = it.next().ok_or_else(|| format!("{a} needs a value"))?;
                out.flags.push((a.clone(), v.clone()));
            } else if switches.contains(&a.as_str()) {
                out.flags.push((a.clone(), String::new()));
            } else if a.starts_with("--") {
                return Err(format!("unknown option {a}\n\n{USAGE}"));
            } else {
                out.positional.push(a.clone());
            }
        }
        Ok(out)
    }

    fn get(&self, name: &str) -> Option<&str> {
        self.flags
            .iter()
            .rev()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
    }

    fn all(&self, name: &str) -> Vec<&str> {
        self.flags
            .iter()
            .filter(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
            .collect()
    }

    fn has(&self, name: &str) -> bool {
        self.flags.iter().any(|(k, _)| k == name)
    }
}

/// Default rules, then the config file, then `--set` overrides.
fn load_rules(config: Option<&str>, sets: &[&str]) -> Result<Rules, String> {
    let mut rules = Rules::default();
    if let Some(path) = config {
        let text = std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?;
        for (n, raw) in text.lines().enumerate() {
            let line = raw.split('#').next().unwrap_or("").trim();
            if line.is_empty() {
                continue;
            }
            let (key, value) = line
                .split_once('=')
                .ok_or_else(|| format!("{path}:{}: expected 'key = value'", n + 1))?;
            rules
                .set(key.trim(), value.trim().trim_matches('"'))
                .map_err(|e| format!("{path}:{}: {e}", n + 1))?;
        }
    }
    for s in sets {
        let (key, value) = s
            .split_once('=')
            .ok_or_else(|| format!("--set {s}: expected key=value"))?;
        rules.set(key.trim(), value.trim())?;
    }
    rules.validate()?;
    Ok(rules)
}

/// Builds the game from `--map`. Returns the game, a description of the map
/// source, and the seed if the map was generated.
fn load_game(map: Option<&str>, rules: Rules) -> Result<(Game, String, Option<u64>), String> {
    let seed = match map {
        Some(m) if m.starts_with("seed:") => Some(m[5..].parse::<u64>().map_err(|_| format!("bad map seed in '{m}'"))?),
        Some(path) => {
            let text = std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?;
            let (rules, map) = parse_setup(text.lines(), rules).map_err(|e| format!("{path}: {e}"))?;
            return Ok((Game::new(rules, map), path.to_string(), None));
        }
        None => None,
    };
    let seed = seed.unwrap_or_else(|| {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        (nanos % 1_000_000_000) as u64
    });
    let map = mapgen::generate(&rules, seed)?;
    Ok((Game::new(rules, map), format!("seed:{seed}"), Some(seed)))
}

/// A display name from a bot command: the file name of its last argument
/// that names a file (else of the program), without any `_bot` suffix.
fn default_name(command: &str) -> String {
    let args = bot::split_command(command).unwrap_or_default();
    let file = args.iter().rev().find(|a| Path::new(a).is_file()).or(args.first());
    let file = file.map(String::as_str).unwrap_or("bot");
    let stem = Path::new(file)
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| file.to_string());
    stem.strip_suffix("_bot")
        .filter(|s| !s.is_empty())
        .map(String::from)
        .unwrap_or(stem)
}

/// A new directory under `runs/`, named by UTC time and map.
fn new_run_dir(map_source: &str) -> Result<PathBuf, String> {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (y, m, d) = civil_from_days((secs / 86400) as i64);
    let rem = secs % 86400;
    let tag: String = map_source
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();
    let tag = tag.trim_matches('-').chars().take(24).collect::<String>();
    let base = format!(
        "{y:04}{m:02}{d:02}-{:02}{:02}{:02}-{tag}",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    );
    for n in 1.. {
        let name = if n == 1 { base.clone() } else { format!("{base}-{n}") };
        let dir = Path::new("runs").join(name);
        if !dir.exists() {
            std::fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
            return Ok(dir);
        }
    }
    unreachable!()
}

/// Converts days since 1970-01-01 to a (year, month, day) date.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + if month <= 2 { 1 } else { 0 };
    (year, month, day)
}

fn cmd_play(args: &[String]) -> Result<(), String> {
    let a = Args::parse(
        args,
        &[
            "--p1",
            "--p2",
            "--name1",
            "--name2",
            "--map",
            "--config",
            "--set",
            "--replay",
            "--memory-mb",
        ],
        &["--quiet"],
    )?;
    if let Some(extra) = a.positional.first() {
        return Err(format!("unexpected argument '{extra}'"));
    }
    let p1 = a.get("--p1").ok_or("missing --p1 <command>")?;
    let p2 = a.get("--p2").ok_or("missing --p2 <command>")?;
    let rules = load_rules(a.get("--config"), &a.all("--set"))?;
    let (game, map_source, seed) = load_game(a.get("--map"), rules)?;
    let run_dir = new_run_dir(&map_source)?;
    let replay = a
        .get("--replay")
        .map(PathBuf::from)
        .unwrap_or_else(|| run_dir.join("replay.bbr"));
    let memory_mb = match a.get("--memory-mb") {
        Some(s) => s.parse().map_err(|_| format!("bad --memory-mb '{s}'"))?,
        None => 2048,
    };
    let opts = play::Options {
        players: [
            play::Player {
                name: a.get("--name1").map(String::from).unwrap_or_else(|| default_name(p1)),
                command: p1.into(),
            },
            play::Player {
                name: a.get("--name2").map(String::from).unwrap_or_else(|| default_name(p2)),
                command: p2.into(),
            },
        ],
        map_source,
        seed,
        replay,
        run_dir,
        memory_mb,
        quiet: a.has("--quiet"),
    };
    if !opts.quiet {
        println!(
            "{} vs {} on {} ({}x{}, {} cities)",
            opts.players[0].name,
            opts.players[1].name,
            opts.map_source,
            game.map.width,
            game.map.height,
            game.cities.len()
        );
    }
    let outcome = play::play(game, &opts)?;
    if outcome.winner == 0 {
        println!("Draw ({}).", outcome.reason);
    } else {
        println!(
            "Player {} ({}) wins by {}.",
            outcome.winner,
            opts.players[outcome.winner as usize - 1].name,
            outcome.reason
        );
    }
    println!("Replay: {}", opts.replay.display());
    Ok(())
}

fn cmd_replay(args: &[String]) -> Result<(), String> {
    let a = Args::parse(args, &[], &["--text", "--orders"])?;
    let path = a.positional.first().ok_or("missing replay file")?;
    let r = replay::Replay::read(Path::new(path))?;
    if a.has("--text") || a.has("--orders") {
        print!("{}", replay::summary(&r, a.has("--orders"))?);
        return Ok(());
    }
    let game = r.resimulate(|_| {})?;
    let o = game.outcome.as_ref().ok_or("the replay has no result")?;
    let who = if o.winner == 0 {
        "draw".to_string()
    } else {
        format!("player {} ({}) wins", o.winner, r.name(o.winner))
    };
    println!(
        "{path}: {} turns re-simulated; {who} by {} in round {}.",
        r.turns().count(),
        o.reason,
        game.round
    );
    Ok(())
}

fn cmd_map(args: &[String]) -> Result<(), String> {
    let a = Args::parse(args, &["--map", "--config", "--set"], &["--lines"])?;
    let rules = load_rules(a.get("--config"), &a.all("--set"))?;
    let (game, source, _) = load_game(a.get("--map"), rules)?;
    if a.has("--lines") {
        println!("# bitbath map {source}");
        for line in game.map.lines(&game.rules) {
            println!("{line}");
        }
        return Ok(());
    }
    println!(
        "{source}: {}x{}, {} cities (1 and 2 are home cities, C neutral)",
        game.map.width,
        game.map.height,
        game.cities.len()
    );
    for r in 0..game.map.height {
        let mut row = String::new();
        if r % 2 == 1 {
            row.push(' ');
        }
        for col in 0..game.map.width {
            let hex = game.map.hex((r * game.map.width + col) as usize);
            let ch = match game.cities.iter().find(|c| c.hex == hex) {
                Some(c) if c.owner > 0 => char::from(b'0' + c.owner),
                _ => game.rules.terrains[game.map.terrain_at(hex)].ch,
            };
            row.push(ch);
            row.push(' ');
        }
        println!("{}", row.trim_end());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(20_394), (2025, 11, 2));
        assert_eq!(civil_from_days(11_016), (2000, 2, 29));
    }

    #[test]
    fn names() {
        assert_eq!(default_name("python ../bots/random_bot.py 7"), "random");
        assert_eq!(default_name("./mybot.exe --fast"), "mybot");
    }
}
