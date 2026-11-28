//! `bitbath serve`: serves the viewer's page, and runs bots for it (SPEC §5).
//!
//! A page can't start a program, so a game played in the page against a bot
//! on disk needs this bridge. The page stays the referee: its engine builds
//! the messages and answers the orders. The bridge only starts a bot and
//! carries lines between the page and the bot's stdin and stdout.
//!
//! It offers the bots it finds in the repository (see `discover`) and the
//! commands named when it was started. It never runs a command the page
//! sends, and it listens on this machine only. Requests must come from its
//! own pages, so no other site open in the browser can start a bot.
//!
//! It also plays a replay's match again, bot against bot, for the page's
//! Redo button: see `Server::redo`. There too it runs only bots on offer.
//! A replay names the commands its bots were run with, and a replay can
//! come from anywhere, so those commands only pick the offers to run, and
//! the arguments the bots are given.
//!
//! The API, all JSON:
//!
//! - `GET /api/bots` lists the bots on offer: each one's `name`, `command`,
//!   and `index`, its record in the bot index (`bots/index.json`) or null.
//!   `redo` is true, to say that this server plays matches again.
//! - `POST /api/redo`, with a replay's header as the body, starts the same
//!   match again: the header's `setup`, between the offers that its
//!   players' commands name. It returns the `job`, and `replay`, the path of
//!   the replay being written.
//! - `GET /api/redo?job=<j>` says how far that game is: `round` of `rounds`,
//!   and `done`, with `result` once it ends or `error` if it couldn't be played.
//! - `POST /api/start?bot=<name>` starts one and returns its `session`.
//! - `POST /api/io?session=<s>&wait=<ms>` writes the body's lines to the
//!   bot, then waits up to `wait` ms for its next line. Without `wait` it
//!   only writes. The answer holds one of `line`, `long` (a line over the
//!   length limit), `eof` (the bot is gone, with `reason` and `note`), or
//!   `timeout`, and always the `stderr` the bot wrote since the last answer.
//! - `POST /api/stop?session=<s>` writes the body's lines, gives the bot a
//!   moment to exit, and kills it.

use crate::bot::{Bot, Event};
use crate::play;
use bitbath_engine::Game;
use serde_json::{json, Value};
use std::collections::hash_map::RandomState;
use std::collections::HashMap;
use std::hash::{BuildHasher, Hasher};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

/// A bot the page may play against.
pub struct Offer {
    pub name: String,
    pub command: String,
    /// Its record in the bot index, which says what it is, and the rules
    /// and maps it plays: see `discover`.
    pub index: Option<Value>,
}

pub struct Options {
    pub port: u16,
    /// The bots named on the command line.
    pub bots: Vec<Offer>,
    /// Whether to offer the bots found in the repository too.
    pub discover: bool,
    pub memory_mb: u64,
    /// The directory served: the repository, with `web/` and `runs/` in it.
    pub root: PathBuf,
}

/// The longest a request may wait for a bot's line.
const MAX_WAIT: Duration = Duration::from_secs(60);
/// Bots running at once. A game's bot stops when the game does, so more
/// than a few means pages that went away without saying so.
const MAX_SESSIONS: usize = 16;
/// A bot nobody has talked to for this long belongs to a page that is gone.
const IDLE: Duration = Duration::from_secs(12 * 3600);
const MAX_LINE: usize = 1024;
const MAX_STDERR: usize = 64 * 1024;
const MAX_BODY: usize = 1024 * 1024;
/// Matches being played again at once. Each runs two bots.
const MAX_REDOS: usize = 2;
/// How long a match played again is remembered after it ends.
const REDO_KEPT: Duration = Duration::from_secs(3600);

struct Session {
    bot: Mutex<Bot>,
    /// Set by a stop, so a request waiting for the bot's line gives up.
    stopped: AtomicBool,
    used: Mutex<Instant>,
}

/// A match being played again, or one that has been: see `Server::redo`.
struct Redo {
    /// The replay being written, as the path the page opens it by.
    replay: String,
    /// The same file on disk.
    file: PathBuf,
    /// The game's round limit.
    rounds: u32,
    started: Instant,
    /// Once the game is over: how it ended, or why it couldn't be played.
    end: Mutex<Option<Result<String, String>>>,
}

struct Server {
    opts: Options,
    sessions: Mutex<HashMap<String, Arc<Session>>>,
    redos: Mutex<HashMap<String, Arc<Redo>>>,
}

struct Request {
    method: String,
    path: String,
    query: HashMap<String, String>,
    headers: HashMap<String, String>,
    body: String,
}

struct Response {
    status: &'static str,
    content_type: &'static str,
    body: Vec<u8>,
}

impl Response {
    fn json(value: Value) -> Response {
        Response {
            status: "200 OK",
            content_type: "application/json",
            body: value.to_string().into_bytes(),
        }
    }

    fn error(status: &'static str, message: &str) -> Response {
        Response {
            status,
            content_type: "application/json",
            body: json!({ "error": message }).to_string().into_bytes(),
        }
    }
}

pub fn serve(opts: Options) -> Result<(), String> {
    let listener = TcpListener::bind(("127.0.0.1", opts.port))
        .map_err(|e| format!("can't listen on port {}: {e}", opts.port))?;
    println!("Serving Bitbath at http://localhost:{}/web/index.html", opts.port);
    let server = Arc::new(Server {
        opts,
        sessions: Mutex::new(HashMap::new()),
        redos: Mutex::new(HashMap::new()),
    });
    let offers = server.offers();
    if offers.is_empty() {
        println!("No bots on offer. Add some with --bot <name>=<command>.");
    }
    for b in &offers {
        println!("  bot {}: {}", b.name, b.command);
    }
    for stream in listener.incoming().flatten() {
        let server = Arc::clone(&server);
        thread::spawn(move || connection(&server, stream));
    }
    Ok(())
}

/// Answers the requests of one connection until it closes or goes quiet.
fn connection(server: &Server, stream: TcpStream) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(120)));
    let _ = stream.set_nodelay(true);
    let Ok(mut out) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(stream);
    while let Some(request) = read_request(&mut reader) {
        let response = server.handle(&request);
        let head = format!(
            "HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nCache-Control: no-cache\r\n\r\n",
            response.status,
            response.content_type,
            response.body.len()
        );
        if out.write_all(head.as_bytes()).is_err() || out.write_all(&response.body).is_err() || out.flush().is_err() {
            break;
        }
        if request.headers.get("connection").is_some_and(|c| c.eq_ignore_ascii_case("close")) {
            break;
        }
    }
}

fn read_request(reader: &mut BufReader<TcpStream>) -> Option<Request> {
    let mut line = String::new();
    if reader.by_ref().take(8192).read_line(&mut line).ok()? == 0 {
        return None;
    }
    let mut parts = line.split_whitespace();
    let method = parts.next()?.to_string();
    let target = parts.next()?;
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let query = query
        .split('&')
        .filter_map(|pair| pair.split_once('='))
        .map(|(k, v)| (percent_decode(k), percent_decode(v)))
        .collect();
    let mut headers = HashMap::new();
    loop {
        let mut header = String::new();
        if reader.by_ref().take(8192).read_line(&mut header).ok()? == 0 {
            return None;
        }
        let header = header.trim_end();
        if header.is_empty() {
            break;
        }
        if let Some((name, value)) = header.split_once(':') {
            headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_string());
        }
    }
    let length: usize = headers.get("content-length").and_then(|v| v.parse().ok()).unwrap_or(0);
    if length > MAX_BODY {
        return None;
    }
    let mut body = vec![0u8; length];
    reader.read_exact(&mut body).ok()?;
    Some(Request {
        method,
        path: percent_decode(path),
        query,
        headers,
        body: String::from_utf8_lossy(&body).into_owned(),
    })
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        let hex = (bytes[i] == b'%')
            .then(|| s.get(i + 1..i + 3).and_then(|h| u8::from_str_radix(h, 16).ok()))
            .flatten();
        match hex {
            Some(byte) => {
                out.push(byte);
                i += 3;
            }
            None => {
                out.push(bytes[i]);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Whether a `Host` header names this machine by a loopback name. A page of
/// another site that reaches this server under its own host name doesn't.
fn local_host(host: &str) -> bool {
    let name = match host.rsplit_once(':') {
        Some((name, port)) if port.chars().all(|c| c.is_ascii_digit()) => name,
        _ => host,
    };
    matches!(name, "localhost" | "127.0.0.1" | "[::1]")
}

/// The bots found in the repository at `root`: each Python file in `bots/`,
/// run with Python, and each program in `target/release/` but `bitbath`
/// itself. A bot that the bot index (`bots/index.json`) runs goes by its id
/// there, and brings its record. It doesn't check that the others are bots:
/// one that isn't fails when a game with it starts, which costs less than
/// naming every bot by hand.
pub fn discover(root: &Path) -> Vec<Offer> {
    let files = |dir: &str| {
        let mut names: Vec<String> = std::fs::read_dir(root.join(dir))
            .into_iter()
            .flatten()
            .flatten()
            .filter(|e| e.path().is_file())
            .filter_map(|e| e.file_name().into_string().ok())
            .collect();
        names.sort();
        names
    };
    let stem = |file: &str| Path::new(file).file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    let python = if cfg!(windows) { "python" } else { "python3" };
    let mut found = Vec::new();
    for file in files("bots") {
        if file.ends_with(".py") {
            found.push((format!("bots/{file}"), format!("{python} \"bots/{file}\"")));
        }
    }
    for file in files("target/release") {
        if is_program(&root.join("target/release").join(&file)) && stem(&file) != "bitbath" {
            found.push((format!("target/release/{file}"), format!("\"target/release/{file}\"")));
        }
    }
    let index = bot_index(root);
    let mut offers: Vec<Offer> = Vec::new();
    for (path, command) in found {
        let known = index.iter().find(|(_, record)| runs(record, &path));
        let base = known.map_or_else(|| stem(&path), |(id, _)| id.clone());
        let name = offer_name(&base, |n| offers.iter().any(|o| o.name == n));
        offers.push(Offer {
            name,
            command,
            index: known.map(|(_, record)| record.clone()),
        });
    }
    offers
}

/// The records of the bot index in the repository at `root`, by id: none
/// if it has no index, or one that can't be read.
fn bot_index(root: &Path) -> Vec<(String, Value)> {
    let text = std::fs::read_to_string(root.join("bots").join("index.json")).unwrap_or_default();
    let index: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
    index["bots"]
        .as_object()
        .map(|bots| bots.clone().into_iter().collect())
        .unwrap_or_default()
}

/// Whether a record of the bot index runs the file at `path`: whether the
/// last word of its `run` command is that path, with or without `.exe`.
fn runs(record: &Value, path: &str) -> bool {
    fn plain(p: &str) -> &str {
        p.strip_suffix(".exe").unwrap_or(p)
    }
    let run = record["run"].as_str().and_then(|run| run.split_whitespace().last());
    run.is_some_and(|run| plain(run) == plain(path))
}

#[cfg(windows)]
fn is_program(path: &Path) -> bool {
    path.extension().is_some_and(|e| e.eq_ignore_ascii_case("exe"))
}

#[cfg(not(windows))]
fn is_program(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.extension().is_none() && std::fs::metadata(path).is_ok_and(|m| m.permissions().mode() & 0o111 != 0)
}

/// A bot's name from its file's stem: without a `_bot` suffix, in plain
/// characters, and with a number added if `taken` says the name is in use.
fn offer_name(stem: &str, taken: impl Fn(&str) -> bool) -> String {
    let base = stem.strip_suffix("_bot").filter(|s| !s.is_empty()).unwrap_or(stem);
    let base: String = base
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
        .take(28)
        .collect();
    let base = if base.is_empty() { "bot".to_string() } else { base };
    let mut name = base.clone();
    let mut n = 2;
    while taken(&name) {
        name = format!("{base}-{n}");
        n += 1;
    }
    name
}

impl Server {
    /// The bots on offer now: the ones named on the command line, then the
    /// ones found in the repository, so a bot built or written while the
    /// server runs is there the next time the page asks.
    fn offers(&self) -> Vec<Offer> {
        let mut offers: Vec<Offer> = self
            .opts
            .bots
            .iter()
            .map(|b| Offer {
                name: b.name.clone(),
                command: b.command.clone(),
                index: b.index.clone(),
            })
            .collect();
        if self.opts.discover {
            for found in discover(&self.opts.root) {
                if offers.iter().any(|o| o.command == found.command) {
                    continue;
                }
                let name = offer_name(&found.name, |n| offers.iter().any(|o| o.name == n));
                offers.push(Offer {
                    name,
                    command: found.command,
                    index: found.index,
                });
            }
        }
        offers
    }

    fn handle(&self, r: &Request) -> Response {
        let host = r.headers.get("host").map(String::as_str).unwrap_or("");
        if !local_host(host) {
            return Response::error("403 Forbidden", "this server answers only as localhost");
        }
        let Some(api) = r.path.strip_prefix("/api/") else {
            return match r.method.as_str() {
                "GET" => self.file(&r.path),
                _ => Response::error("405 Method Not Allowed", "only GET here"),
            };
        };
        // A browser names the page a request comes from, and it must be one
        // of this server's own.
        if r.headers.get("origin").is_some_and(|o| *o != format!("http://{host}")) {
            return Response::error("403 Forbidden", "requests must come from this server's own pages");
        }
        let session = r.query.get("session").map(String::as_str).unwrap_or("");
        match (r.method.as_str(), api) {
            ("GET", "bots") => Response::json(json!({
                "bots": self.offers().iter().map(|b| json!({
                    "name": b.name, "command": b.command, "index": b.index,
                })).collect::<Vec<_>>(),
                "redo": true,
            })),
            ("POST", "redo") => self.redo(&r.body),
            ("GET", "redo") => self.redo_status(r.query.get("job").map(String::as_str).unwrap_or("")),
            ("POST", "start") => self.start(r.query.get("bot").map(String::as_str).unwrap_or("")),
            ("POST", "io") => self.io(session, &r.body, r.query.get("wait").and_then(|w| w.parse().ok())),
            ("POST", "stop") => self.stop(session, &r.body),
            _ => Response::error("404 Not Found", "no such call"),
        }
    }

    fn file(&self, path: &str) -> Response {
        let path = if path == "/" { "/web/index.html" } else { path };
        let relative = Path::new(path.trim_start_matches('/'));
        let plain = relative
            .components()
            .all(|c| matches!(c, std::path::Component::Normal(_)));
        let mut full = self.opts.root.join(relative);
        // The homepage is also served at /. Its sibling assets must resolve
        // there as well as beside /web/index.html.
        if plain && !full.exists() {
            full = self.opts.root.join("web").join(relative);
        }
        if plain && full.is_dir() {
            full.push("index.html");
        }
        let body = match std::fs::read(&full) {
            Ok(body) if plain => body,
            _ => return Response::error("404 Not Found", "no such file"),
        };
        let content_type = match full.extension().and_then(|e| e.to_str()).unwrap_or("") {
            "html" => "text/html; charset=utf-8",
            "js" => "text/javascript; charset=utf-8",
            "css" => "text/css; charset=utf-8",
            "json" => "application/json",
            "wasm" => "application/wasm",
            "svg" => "image/svg+xml",
            "png" => "image/png",
            "bbr" | "jsonl" | "txt" | "md" | "toml" | "py" | "rs" => "text/plain; charset=utf-8",
            _ => "application/octet-stream",
        };
        Response {
            status: "200 OK",
            content_type,
            body,
        }
    }

    fn start(&self, name: &str) -> Response {
        let offers = self.offers();
        let Some(offer) = offers.iter().find(|b| b.name == name) else {
            return Response::error("404 Not Found", &format!("no bot named '{name}' is on offer"));
        };
        let mut sessions = self.sessions.lock().expect("sessions lock");
        sessions.retain(|_, s| s.used.lock().expect("used lock").elapsed() < IDLE);
        if sessions.len() >= MAX_SESSIONS {
            return Response::error("503 Service Unavailable", "too many bots are running; restart bitbath serve");
        }
        // Like a game's bots, it runs in a directory of its own.
        let dir = self.opts.root.join("runs").join("serve").join(&offer.name);
        if let Err(e) = std::fs::create_dir_all(&dir) {
            return Response::error("500 Internal Server Error", &format!("{}: {e}", dir.display()));
        }
        let bot = match Bot::spawn(&offer.command, &dir, self.opts.memory_mb, MAX_LINE, MAX_STDERR) {
            Ok(bot) => bot,
            Err(e) => return Response::error("500 Internal Server Error", &e),
        };
        let token = new_token();
        sessions.insert(
            token.clone(),
            Arc::new(Session {
                bot: Mutex::new(bot),
                stopped: AtomicBool::new(false),
                used: Mutex::new(Instant::now()),
            }),
        );
        Response::json(json!({ "session": token, "name": offer.name, "command": offer.command }))
    }

    fn session(&self, token: &str) -> Option<Arc<Session>> {
        self.sessions.lock().expect("sessions lock").get(token).cloned()
    }

    fn io(&self, token: &str, body: &str, wait_ms: Option<u64>) -> Response {
        let Some(session) = self.session(token) else {
            return Response::error("404 Not Found", "no such session");
        };
        *session.used.lock().expect("used lock") = Instant::now();
        let mut bot = session.bot.lock().expect("bot lock");
        let lines: Vec<&str> = body.lines().collect();
        if !lines.is_empty() {
            bot.send(&lines);
        }
        let Some(wait_ms) = wait_ms else {
            return Response::json(json!({ "stderr": bot.take_stderr() }));
        };
        let deadline = Instant::now() + Duration::from_millis(wait_ms).min(MAX_WAIT);
        let mut answer = loop {
            if session.stopped.load(Ordering::Relaxed) {
                break json!({ "eof": true, "reason": "crash", "note": "stopped" });
            }
            // In short waits, so a stop doesn't have to wait for the bot.
            match bot.recv(deadline.min(Instant::now() + Duration::from_millis(100))) {
                Some(Event::Line(line)) => break json!({ "line": line }),
                Some(Event::TooLong(start)) => break json!({ "long": start }),
                Some(Event::Eof) => {
                    let (reason, note) = bot.exit_reason();
                    break json!({ "eof": true, "reason": reason, "note": note });
                }
                None if Instant::now() >= deadline => break json!({ "timeout": true }),
                None => {}
            }
        };
        answer["stderr"] = Value::String(bot.take_stderr());
        Response::json(answer)
    }

    fn stop(&self, token: &str, body: &str) -> Response {
        let Some(session) = self.sessions.lock().expect("sessions lock").remove(token) else {
            return Response::json(json!({ "stderr": "" }));
        };
        session.stopped.store(true, Ordering::Relaxed);
        let mut bot = session.bot.lock().expect("bot lock");
        let lines: Vec<&str> = body.lines().collect();
        if !lines.is_empty() {
            bot.send(&lines);
        }
        bot.shut_down(Duration::from_millis(if lines.is_empty() { 0 } else { 1000 }));
        Response::json(json!({ "stderr": bot.take_stderr() }))
    }

    /// Starts the match of a replay again from scratch, bot against bot,
    /// and answers at once with where its replay is being written.
    ///
    /// `body` is the replay's header. Its `setup` is the game: the map and
    /// the rules. Its players' commands are not run as they stand. Each
    /// only picks the bot on offer that it starts with, and that offer's
    /// own command is what runs, with any further words of the replay's
    /// command as the bot's arguments (see `offered`). A match with a bot
    /// that isn't on offer is refused.
    fn redo(&self, body: &str) -> Response {
        let bad = |message: &str| Response::error("400 Bad Request", message);
        let Ok(header) = serde_json::from_str::<Value>(body) else {
            return bad("the body isn't a replay's header");
        };
        let setup: Vec<&str> = header["setup"]
            .as_array()
            .map(|lines| lines.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        let game = match Game::from_setup(setup.iter().copied()) {
            Ok(game) => game,
            Err(e) => return bad(&format!("the replay's setup doesn't load: {e}")),
        };
        let offers = self.offers();
        let mut players = Vec::new();
        for i in 0..2 {
            let player = &header["players"][i];
            let command = player["command"].as_str().unwrap_or("");
            let Some((offer, command_run)) = offered(command, &offers) else {
                return Response::error(
                    "409 Conflict",
                    &format!(
                        "Player {} ran '{}', which isn't a bot this server offers. \
                         Start bitbath serve with --bot <name>=<command> to offer it.",
                        i + 1,
                        plain_text(command, 120)
                    ),
                );
            };
            let name = plain_text(player["name"].as_str().unwrap_or(""), 64);
            players.push(play::Player {
                name: if name.is_empty() { offer.name.clone() } else { name },
                command: command_run,
            });
        }
        let Ok(players) = <[play::Player; 2]>::try_from(players) else {
            return bad("a game has two players");
        };

        let mut redos = self.redos.lock().expect("redos lock");
        let over = |r: &Redo| r.end.lock().expect("end lock").is_some();
        redos.retain(|_, r| !over(r) || r.started.elapsed() < REDO_KEPT);
        if redos.values().filter(|r| !over(r)).count() >= MAX_REDOS {
            return Response::error("503 Service Unavailable", "Other matches are still being played again. Try when one ends.");
        }
        let map_source = match plain_text(header["map"].as_str().unwrap_or(""), 64) {
            source if source.is_empty() => "replay".to_string(),
            source => source,
        };
        let run_dir = match crate::new_run_dir(&map_source) {
            Ok(dir) => dir,
            Err(e) => return Response::error("500 Internal Server Error", &e),
        };
        let file = run_dir.join("replay.bbr");
        let names = [players[0].name.clone(), players[1].name.clone()];
        let opts = play::Options {
            players,
            map_source,
            seed: header["seed"].as_u64(),
            replay: file.clone(),
            run_dir,
            memory_mb: self.opts.memory_mb,
            quiet: true,
        };
        let job = Arc::new(Redo {
            replay: format!("/{}", file.to_string_lossy().replace('\\', "/")),
            file,
            rounds: game.rules.c.round_limit,
            started: Instant::now(),
            end: Mutex::new(None),
        });
        println!("redo: {} vs {} on {}, to {}", names[0], names[1], opts.map_source, job.replay);
        let token = new_token();
        redos.insert(token.clone(), Arc::clone(&job));
        let answer = json!({ "job": token, "replay": job.replay });
        thread::spawn(move || {
            // Caught, so a game that panics still ends for the page waiting on it.
            let played = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| play::play(game, &opts)));
            let end = match played {
                Ok(Ok(done)) if done.outcome.winner == 0 => Ok(format!("Draw ({}).", done.outcome.reason)),
                Ok(Ok(done)) => Ok(format!(
                    "{} wins by {}.",
                    names[done.outcome.winner as usize - 1],
                    done.outcome.reason
                )),
                Ok(Err(e)) => Err(e),
                Err(_) => Err("the game stopped with an error; see the server's output".to_string()),
            };
            match &end {
                Ok(result) => println!("redo: {} {result}", job.replay),
                Err(e) => println!("redo: {} failed: {e}", job.replay),
            }
            *job.end.lock().expect("end lock") = Some(end);
        });
        Response::json(answer)
    }

    /// How far a match being played again is. Each turn is a line of its
    /// replay, after the header's and the one for the bots' starts.
    fn redo_status(&self, token: &str) -> Response {
        let Some(job) = self.redos.lock().expect("redos lock").get(token).cloned() else {
            return Response::error("404 Not Found", "no such job");
        };
        let lines = std::fs::read(&job.file).map_or(0, |text| text.iter().filter(|&&b| b == b'\n').count());
        let round = (lines.saturating_sub(2) / 2 + 1).min(job.rounds as usize);
        let mut answer = json!({ "replay": job.replay, "round": round, "rounds": job.rounds, "done": false });
        match &*job.end.lock().expect("end lock") {
            Some(Ok(result)) => {
                answer["done"] = json!(true);
                answer["result"] = json!(result);
            }
            Some(Err(e)) => {
                answer["done"] = json!(true);
                answer["error"] = json!(e);
            }
            None => {}
        }
        Response::json(answer)
    }
}

/// A bot command's words in a form that compares, so the command a replay
/// names finds the offer that runs the same thing however either was typed:
/// quoted or not, with either slash, with or without `./` and `.exe`, and
/// with Python under either of its names. None if the command is empty or
/// can't be split.
fn command_key(command: &str) -> Option<Vec<String>> {
    let words = crate::bot::split_command(command).ok()?;
    let key: Vec<String> = words
        .iter()
        .enumerate()
        .map(|(i, word)| {
            let word = word.replace('\\', "/");
            let word = word.strip_prefix("./").unwrap_or(&word);
            let word = word.strip_suffix(".exe").unwrap_or(word);
            match (i, word) {
                (0, "python3") => "python".to_string(),
                _ => word.to_string(),
            }
        })
        .collect();
    (!key.is_empty()).then_some(key)
}

/// The bot on offer that a replay's `command` ran, and the command to run
/// it with now: none if no offer's command is what `command` starts with.
///
/// The offer's command is run as the server has it. Words of `command`
/// after it are arguments that the bot was given, such as
/// `--opening port`, and are passed on to it. So a replay chooses among
/// the programs on offer and what they are told, and never the program.
/// Of several offers that fit, the one with the most words is taken, and
/// of those the first.
fn offered<'a>(command: &str, offers: &'a [Offer]) -> Option<(&'a Offer, String)> {
    let words = crate::bot::split_command(command).ok()?;
    let key = command_key(command)?;
    let mut best: Option<(&Offer, usize)> = None;
    for offer in offers {
        let Some(theirs) = command_key(&offer.command) else {
            continue;
        };
        if key.starts_with(&theirs) && best.is_none_or(|(_, n)| theirs.len() > n) {
            best = Some((offer, theirs.len()));
        }
    }
    let (offer, used) = best?;
    let mut run = offer.command.clone();
    for word in &words[used..] {
        // Quoted, so the command splits into the same words again.
        let quote = match (word.contains('"'), word.contains('\'')) {
            (false, _) => '"',
            (true, false) => '\'',
            (true, true) => return None,
        };
        if word.chars().any(char::is_control) {
            return None;
        }
        run.push(' ');
        run.push(quote);
        run.push_str(word);
        run.push(quote);
    }
    Some((offer, run))
}

/// Text from a page for a name or a message: one line, trimmed, of no more
/// than `max` characters, without control characters.
fn plain_text(text: &str, max: usize) -> String {
    text.trim().chars().filter(|c| !c.is_control()).take(max).collect()
}

/// A session's name: unguessable, so it also keeps one page's bot from another's.
fn new_token() -> String {
    let word = || RandomState::new().build_hasher().finish();
    format!("{:016x}{:016x}", word(), word())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_percent_escapes() {
        assert_eq!(percent_decode("/runs/a%20b/replay.bbr"), "/runs/a b/replay.bbr");
        assert_eq!(percent_decode("100%"), "100%");
        assert_eq!(percent_decode("%zz"), "%zz");
    }

    #[test]
    fn only_loopback_hosts_are_local() {
        assert!(local_host("localhost:8000"));
        assert!(local_host("127.0.0.1"));
        assert!(local_host("[::1]:8000"));
        assert!(!local_host("example.com:8000"));
        assert!(!local_host("localhost.example.com"));
        assert!(!local_host(""));
    }

    #[test]
    fn names_bots_by_their_files() {
        assert_eq!(offer_name("greedy_bot", |_| false), "greedy");
        assert_eq!(offer_name("_bot", |_| false), "_bot");
        assert_eq!(offer_name("my bot.v2", |_| false), "my_bot_v2");
        assert_eq!(offer_name("greedy_bot", |n| n == "greedy" || n == "greedy-2"), "greedy-3");
    }

    #[test]
    fn discovers_the_repository_bots() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
        let found = discover(&root);
        let greedy = found.iter().find(|o| o.name == "greedy").expect("bots/greedy_bot.py");
        assert!(greedy.command.ends_with("\"bots/greedy_bot.py\""), "{}", greedy.command);
        // The bot index says what it is, and what it plays.
        let record = greedy.index.as_ref().expect("greedy's record in bots/index.json");
        assert_eq!(record["name"], "Greedy");
        assert_eq!(record["rules"], json!(["classic"]));
        assert!(found.iter().all(|o| !o.command.contains("bitbath.") && !o.command.ends_with("bitbath\"")));
        assert!(discover(&root.join("no-such-directory")).is_empty());
    }

    #[test]
    fn finds_bots_in_the_index_by_what_they_run() {
        let python = json!({"run": "python bots/naval_bot.py"});
        assert!(runs(&python, "bots/naval_bot.py"));
        assert!(!runs(&python, "bots/supply_bot.py"));
        let program = json!({"run": "target/release/forager_bot.exe"});
        assert!(runs(&program, "target/release/forager_bot.exe"));
        assert!(runs(&program, "target/release/forager_bot"));
        assert!(!runs(&json!({"run": null}), "bots/naval_bot.py"));
    }

    #[test]
    fn commands_compare_however_they_were_typed() {
        let key = command_key("python bots/naval_bot.py");
        assert!(key.is_some());
        assert_eq!(command_key("python \"bots/naval_bot.py\""), key);
        assert_eq!(command_key("python3 bots\\naval_bot.py"), key);
        assert_eq!(command_key("python ./bots/naval_bot.py"), key);
        assert_ne!(command_key("python bots/naval_bot.py --fast"), key);
        assert_ne!(command_key("python bots/supply_bot.py"), key);
        assert_eq!(command_key("\"target/release/forager_bot.exe\""), command_key("target/release/forager_bot"));
        assert_eq!(command_key(""), None);
        assert_eq!(command_key("python 'bots"), None);
    }

    #[test]
    fn a_replay_picks_among_the_offers_and_their_arguments() {
        let offer = |name: &str, command: &str| Offer {
            name: name.into(),
            command: command.into(),
            index: None,
        };
        let offers = [
            offer("brig", "python \"bots/brigantine_bot.py\""),
            offer("forager", "\"target/release/forager_bot.exe\""),
            offer("brig-fast", "python bots/brigantine_bot.py --fast"),
        ];
        let run = |command: &str| offered(command, &offers).map(|(o, run)| (o.name.as_str(), run));
        assert_eq!(
            run("python bots/brigantine_bot.py"),
            Some(("brig", "python \"bots/brigantine_bot.py\"".to_string()))
        );
        // The bot's own arguments go with it, and split into the same words again.
        let (name, command) = run("python bots/brigantine_bot.py --opening port 'two words'").unwrap();
        assert_eq!(name, "brig");
        assert_eq!(
            crate::bot::split_command(&command).unwrap(),
            ["python", "bots/brigantine_bot.py", "--opening", "port", "two words"]
        );
        // The offer that says the most of the command is the one.
        assert_eq!(run("python bots/brigantine_bot.py --fast -v").unwrap().0, "brig-fast");
        assert_eq!(
            run("target/release/forager_bot -q"),
            Some(("forager", "\"target/release/forager_bot.exe\" \"-q\"".to_string()))
        );
        // Nothing may come before the bot: not another program, and not a word for Python.
        assert_eq!(run("python -c \"import os\" bots/brigantine_bot.py"), None);
        assert_eq!(run("cmd /c python bots/brigantine_bot.py"), None);
        assert_eq!(run("python bots/other_bot.py"), None);
        assert_eq!(run("python"), None);
        assert_eq!(run(""), None);
    }

    /// A replay's commands are never run: one that names no bot on offer is refused.
    #[test]
    fn redo_runs_only_bots_on_offer() {
        let server = Server {
            opts: Options {
                port: 0,
                bots: vec![Offer {
                    name: "mine".into(),
                    command: "python bots/mine.py".into(),
                    index: None,
                }],
                discover: false,
                memory_mb: 0,
                root: PathBuf::from("."),
            },
            sessions: Mutex::new(HashMap::new()),
            redos: Mutex::new(HashMap::new()),
        };
        let rules = bitbath_engine::Rules::default();
        let map = bitbath_engine::mapgen::generate(&rules, 1).unwrap();
        let setup = Game::new(rules, map).setup_lines(true);
        let header = |p2: &str| {
            json!({
                "setup": setup,
                "map": "seed:1",
                "seed": 1,
                "players": [{"name": "a", "command": "python \"bots/mine.py\""}, {"name": "b", "command": p2}],
            })
            .to_string()
        };
        let refused = server.redo(&header("cmd /c calc.exe"));
        assert_eq!(refused.status, "409 Conflict");
        assert!(String::from_utf8_lossy(&refused.body).contains("Player 2 ran 'cmd /c calc.exe'"));
        assert_eq!(server.redo(&header("")).status, "409 Conflict");
        assert_eq!(server.redo("{\"setup\": [\"MAP 2\"]}").status, "400 Bad Request");
        assert_eq!(server.redo("not json").status, "400 Bad Request");
        assert!(server.redos.lock().unwrap().is_empty());
        assert_eq!(server.redo_status("none").status, "404 Not Found");
    }

    #[test]
    fn tokens_differ() {
        assert_ne!(new_token(), new_token());
        assert_eq!(new_token().len(), 32);
    }
}
