//! The `bitbath` command: plays bot-vs-bot games and tournaments, rates bots,
//! reads replays, shows maps, and serves the viewer's page with bots to play against (SPEC §10).

mod bot;
mod entry;
mod play;
mod rating;
mod replay;
mod serve;
mod tournament;

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
  bitbath tournament --bot <name>=<command> --bot <name>=<command> ... [options]
      Plays every pair of bots on every map, twice with the sides swapped,
      then rates the bots.
      --config <file>                  rules to play under; repeatable, and every pair
                                       plays under each (default: the default rules)
      --seeds <list>                   map seeds, such as 1-10 or 3,7,9 (default: 1-5)
      --set <key>=<value>              one rule override, for every game; repeatable
      --out <dir>                      results directory (default: a new one in runs/).
                                       Games already in its games.jsonl aren't played
                                       again, so a tournament can be resumed or grown.
      --jobs <n>                       games to play at once (default: 1)
      --memory-mb <n>                  memory cap per bot, 0 for none (default: 2048)
      --anchor, --bootstrap            as for rate
  bitbath check <bot directory> [--map seed:<n> | <file>] [--config <file>] [--set <key>=<value>]
      Checks a bot directory, which is a bot with a bot.toml that gives its
      'run' command and, if it needs one, its 'build' command. Builds the bot,
      then plays it against itself (default map: seed:1) and fails if either
      side forfeits. A bot directory can stand for a bot command anywhere:
      --p1 <dir>, or --bot <name>=<dir>.
  bitbath rate <games.jsonl>... [--anchor <name>=<rating>] [--bootstrap <n>] [--json <file>]
      Rates the bots in tournament results: a Bradley-Terry fit on the Elo scale.
      --anchor <name>=<rating>         fix one bot's rating (default: the average is 1500)
      --bootstrap <n>                  resamples for the 95% intervals (default: 1000)
      --json <file>                    also write the ratings as JSON
  bitbath replay <file> [--text] [--orders]
      Re-simulates a replay and checks its result. --text prints a turn-by-turn
      summary; --orders adds every order and its reply.
  bitbath map [--map seed:<n> | <file>] [--config <file>] [--set <key>=<value>] [--lines]
      Prints a map. --lines prints it in the map file format instead.
  bitbath serve [--port <n>] [--bot <name>=<command>]... [--no-discover] [--memory-mb <n>]
      Serves the viewer's page at http://localhost:<port>/web/index.html (default
      port 8000), from the current directory, with bots to play against by
      hand in the page: the Python files in bots/, the programs in
      target/release/, and each --bot. --no-discover offers only the --bot ones.

Example:
  bitbath play --p1 \"python bots/greedy_bot.py\" --p2 \"python bots/random_bot.py\" --map seed:42
";

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match args.first().map(String::as_str) {
        Some("play") => cmd_play(&args[1..]),
        Some("tournament") => cmd_tournament(&args[1..]),
        Some("rate") => cmd_rate(&args[1..]),
        Some("check") => cmd_check(&args[1..]),
        Some("replay") => cmd_replay(&args[1..]),
        Some("map") => cmd_map(&args[1..]),
        Some("serve") => cmd_serve(&args[1..]),
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
    unique_dir(Path::new("runs"), &base)
}

/// Creates `root/<base>`, or `root/<base>-2` and so on when that's taken.
/// Creating the directory is the test for whether it's taken, so games that
/// start in the same second, as in a tournament, never share a directory.
fn unique_dir(root: &Path, base: &str) -> Result<PathBuf, String> {
    std::fs::create_dir_all(root).map_err(|e| format!("{}: {e}", root.display()))?;
    for n in 1.. {
        let dir = root.join(if n == 1 {
            base.to_string()
        } else {
            format!("{base}-{n}")
        });
        match std::fs::create_dir(&dir) {
            Ok(()) => return Ok(dir),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(format!("{}: {e}", dir.display())),
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
    let outcome = play::play(game, &opts)?.outcome;
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

fn cmd_tournament(args: &[String]) -> Result<(), String> {
    let a = Args::parse(
        args,
        &[
            "--bot",
            "--config",
            "--seeds",
            "--set",
            "--out",
            "--jobs",
            "--memory-mb",
            "--anchor",
            "--bootstrap",
        ],
        &[],
    )?;
    if let Some(extra) = a.positional.first() {
        return Err(format!("unexpected argument '{extra}'"));
    }
    let mut bots: Vec<tournament::Entrant> = Vec::new();
    for spec in a.all("--bot") {
        let (name, command) = parse_offer(spec)?;
        if bots.iter().any(|b| b.name == name) {
            return Err(format!("--bot {spec}: there are two bots named '{name}'"));
        }
        bots.push(tournament::Entrant { name, command });
    }
    if bots.len() < 2 {
        return Err("a tournament needs at least two --bot <name>=<command>".into());
    }
    let sets = a.all("--set");
    let mut configs: Vec<(String, Rules)> = Vec::new();
    for path in a.all("--config") {
        let name = Path::new(path)
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        if configs.iter().any(|(n, _)| *n == name) {
            return Err(format!("--config {path}: there are two configs named '{name}'"));
        }
        configs.push((name, load_rules(Some(path), &sets)?));
    }
    if configs.is_empty() {
        configs.push(("default".into(), load_rules(None, &sets)?));
    }
    let number = |flag: &str, default: u64| match a.get(flag) {
        Some(s) => s.parse::<u64>().map_err(|_| format!("bad {flag} '{s}'")),
        None => Ok(default),
    };
    let rating = rating_options(&a)?;
    let opts = tournament::Options {
        bots,
        configs,
        seeds: tournament::parse_seeds(a.get("--seeds").unwrap_or("1-5"))?,
        out: match a.get("--out") {
            Some(dir) => PathBuf::from(dir),
            None => new_run_dir("tournament")?,
        },
        jobs: number("--jobs", 1)?.max(1) as usize,
        memory_mb: number("--memory-mb", 2048)?,
    };
    let games = tournament::run(&opts)?;
    let rows = rating::rate(&games, &rating)?;
    let path = opts.out.join("ratings.json");
    write_ratings(&path, &rows, games.len(), &rating)?;
    println!(
        "
{}
Ratings: {}",
        rating::table(&rows),
        path.display()
    );
    Ok(())
}

fn cmd_rate(args: &[String]) -> Result<(), String> {
    let a = Args::parse(args, &["--anchor", "--bootstrap", "--json"], &[])?;
    if a.positional.is_empty() {
        return Err("missing games.jsonl file".into());
    }
    let mut games = Vec::new();
    for path in &a.positional {
        games.extend(tournament::read_games(Path::new(path))?);
    }
    let opts = rating_options(&a)?;
    let rows = rating::rate(&games, &opts)?;
    print!("{}", rating::table(&rows));
    if let Some(path) = a.get("--json") {
        write_ratings(Path::new(path), &rows, games.len(), &opts)?;
    }
    Ok(())
}

fn cmd_check(args: &[String]) -> Result<(), String> {
    let a = Args::parse(args, &["--map", "--config", "--set", "--memory-mb"], &[])?;
    let [dir] = a.positional.as_slice() else {
        return Err("expected one bot directory".into());
    };
    let entry = entry::Entry::load(Path::new(dir))?;
    let size = entry.size()?;
    let limit = entry::MAX_BYTES;
    println!("Size: {} KB of at most {} KB", size.div_ceil(1024), limit / 1024);
    if size > limit {
        return Err(format!(
            "the bot directory holds {size} bytes, over the limit of {limit}"
        ));
    }
    if let Some(build) = &entry.build {
        println!("Building: {build}");
        entry.build(std::time::Duration::from_secs(600))?;
    }
    let rules = load_rules(a.get("--config"), &a.all("--set"))?;
    let (game, map_source, seed) = load_game(Some(a.get("--map").unwrap_or("seed:1")), rules)?;
    let run_dir = new_run_dir("check")?;
    println!("Playing the bot against itself on {map_source}: {}", entry.run);
    let opts = play::Options {
        players: ["first", "second"].map(|name| play::Player {
            name: name.into(),
            command: dir.clone(),
        }),
        map_source,
        seed,
        replay: run_dir.join("replay.bbr"),
        run_dir,
        memory_mb: match a.get("--memory-mb") {
            Some(s) => s.parse().map_err(|_| format!("bad --memory-mb '{s}'"))?,
            None => 2048,
        },
        quiet: true,
    };
    let finished = play::play(game, &opts)?;
    let result = &finished.result;
    for (i, stats) in result["stats"].as_array().into_iter().flatten().enumerate() {
        println!(
            "Player {}: {} turns, {} orders, {} rejected, {} timeouts, {} missed turns, slowest turn {} ms",
            i + 1,
            stats["turns"],
            stats["orders"],
            stats["invalid"],
            stats["timeouts"],
            stats["missed_turns"],
            stats["max_think_ms"]
        );
    }
    println!("Replay: {}", opts.replay.display());
    let reason = finished.outcome.reason.as_str();
    if rating::FORFEITS.contains(&reason) {
        return Err(match finished.outcome.winner {
            0 => format!("both sides forfeited ({reason})"),
            w => format!("player {} forfeited ({reason})", 3 - w),
        });
    }
    println!("OK: the game ended by {reason} in round {}.", result["round"]);
    Ok(())
}

/// The rating options from `--anchor` and `--bootstrap`.
fn rating_options(a: &Args) -> Result<rating::Options, String> {
    let anchor = match a.get("--anchor") {
        Some(spec) => {
            let bad = || format!("--anchor {spec}: expected <name>=<rating>");
            let (name, rating) = spec.split_once('=').ok_or_else(bad)?;
            let rating: f64 = rating.trim().parse().map_err(|_| bad())?;
            if !rating.is_finite() {
                return Err(bad());
            }
            Some((name.trim().to_string(), rating))
        }
        None => None,
    };
    let bootstrap = match a.get("--bootstrap") {
        Some(s) => s.parse().map_err(|_| format!("bad --bootstrap '{s}'"))?,
        None => 1000,
    };
    Ok(rating::Options { anchor, bootstrap })
}

fn write_ratings(path: &Path, rows: &[rating::Row], games: usize, opts: &rating::Options) -> Result<(), String> {
    let text = format!(
        "{:#}
",
        rating::to_json(rows, games, opts)
    );
    std::fs::write(path, text).map_err(|e| format!("{}: {e}", path.display()))
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

fn cmd_serve(args: &[String]) -> Result<(), String> {
    let a = Args::parse(args, &["--port", "--bot", "--memory-mb"], &["--no-discover"])?;
    if let Some(extra) = a.positional.first() {
        return Err(format!("unexpected argument '{extra}'"));
    }
    let port = match a.get("--port") {
        Some(s) => s.parse().map_err(|_| format!("bad --port '{s}'"))?,
        None => 8000,
    };
    let memory_mb = match a.get("--memory-mb") {
        Some(s) => s.parse().map_err(|_| format!("bad --memory-mb '{s}'"))?,
        None => 2048,
    };
    let mut bots: Vec<serve::Offer> = Vec::new();
    for spec in a.all("--bot") {
        let (name, command) = parse_offer(spec)?;
        if bots.iter().any(|b| b.name == name) {
            return Err(format!("--bot {spec}: there are two bots named '{name}'"));
        }
        bots.push(serve::Offer { name, command });
    }
    let root = std::env::current_dir().map_err(|e| format!("no current directory: {e}"))?;
    if !root.join("web").join("index.html").is_file() {
        return Err("there is no web/index.html here: run bitbath serve from the repository".into());
    }
    serve::serve(serve::Options {
        port,
        bots,
        discover: !a.has("--no-discover"),
        memory_mb,
        root,
    })
}

/// Splits a `--bot` value into the bot's name and command. The name goes
/// into file paths and page text, so it is kept plain.
fn parse_offer(spec: &str) -> Result<(String, String), String> {
    let (name, command) = spec
        .split_once('=')
        .ok_or_else(|| format!("--bot {spec}: expected <name>=<command>"))?;
    let (name, command) = (name.trim(), command.trim());
    let plain = |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '-';
    if name.is_empty() || name.len() > 32 || !name.chars().all(plain) {
        return Err(format!("--bot {spec}: a name is up to 32 letters, digits, '-' and '_'"));
    }
    if bot::split_command(command)?.is_empty() {
        return Err(format!("--bot {spec}: the command is empty"));
    }
    Ok((name.to_string(), command.to_string()))
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
    fn bot_offers() {
        assert_eq!(
            parse_offer("greedy=python bots/greedy_bot.py").unwrap(),
            ("greedy".to_string(), "python bots/greedy_bot.py".to_string())
        );
        assert_eq!(parse_offer("a=b --x=1").unwrap().1, "b --x=1");
        for bad in ["python bots/greedy_bot.py", "=x", "a b=x", "../a=x", "a=", "a='x"] {
            assert!(parse_offer(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn names() {
        assert_eq!(default_name("python ../bots/random_bot.py 7"), "random");
        assert_eq!(default_name("./mybot.exe --fast"), "mybot");
    }

    #[test]
    fn simultaneous_games_get_their_own_directories() {
        let root = std::env::temp_dir().join(format!("bitbath-runs-{}", std::process::id()));
        let dirs: Vec<PathBuf> = std::thread::scope(|s| {
            let handles: Vec<_> = (0..8).map(|_| s.spawn(|| unique_dir(&root, "same-second"))).collect();
            handles.into_iter().map(|h| h.join().unwrap().unwrap()).collect()
        });
        let mut names: Vec<String> = dirs
            .iter()
            .map(|d| d.file_name().unwrap().to_string_lossy().into())
            .collect();
        names.sort();
        names.dedup();
        std::fs::remove_dir_all(&root).unwrap();
        assert_eq!(names.len(), 8, "{names:?}");
        assert!(names.contains(&"same-second".to_string()) && names.contains(&"same-second-8".to_string()));
    }

    /// Every preset in configs/ loads, and the generator can make its maps.
    #[test]
    fn presets_generate_maps() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../configs");
        let mut presets = 0;
        for entry in std::fs::read_dir(&dir).unwrap() {
            let path = entry.unwrap().path();
            if path.extension().is_none_or(|e| e != "toml") {
                continue;
            }
            let name = path.display().to_string();
            let rules = load_rules(Some(&name), &[]).unwrap_or_else(|e| panic!("{e}"));
            for seed in 0..3 {
                let map = mapgen::generate(&rules, seed).unwrap_or_else(|e| panic!("{name}: {e}"));
                map.validate(&rules).unwrap_or_else(|e| panic!("{name}: {e}"));
            }
            presets += 1;
        }
        assert!(presets >= 3, "expected the default, small, and large presets");
    }
}
